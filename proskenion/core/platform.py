"""Platform abstraction layer (spec §5.4).

Everything hardware-specific is confined to this module. The health screen
(§11.2), the update mechanism (§14.4) and the boot logic (§14.5) ask the
``Platform`` for what they need; nothing else in the codebase reads ``/sys``,
``/proc`` or ``/dev`` directly. A test in ``tests/unit/core/test_platform.py``
greps the package to keep it that way.

Three implementations:

* ``RaspberryPiPlatform`` — the reference CM5 appliance.
* ``GenericLinuxPlatform`` — x86 or any other Linux board. Root-slot staging is
  not implemented here; see ``docs/hardware/porting.md``.
* ``DevelopmentPlatform`` — a developer's laptop (Windows, macOS, or Linux
  without appliance partitions). Every metric is "not available".

Every method that might block is ``async`` and wraps its blocking work in
``asyncio.to_thread``, even where a given implementation only reads a small
file. Only the pure-lookup methods stay synchronous. Shelling out
(``smartctl``, ``nvme``, ``lsblk``, ``mount``) goes through an injectable
command runner with a ten-second timeout; on expiry ``storage_health()``
returns ``StorageHealth(partial=True)`` rather than raising, because a hung
``smartctl`` on a dying drive must degrade the reading, not stall health polling.

Returning ``None`` for a metric is the contract for "not available"; the health
screen renders it as such. An implementation must never return a wrong number.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

Slot = Literal["a", "b"]
PowerSource = Literal["poe", "mains", "unknown"]
StorageCondition = Literal["healthy", "warning", "failing", "unknown"]

#: Production timeout for every external command (§5.4). Tests inject a smaller one.
DEFAULT_COMMAND_TIMEOUT_S = 10.0

APPLIANCE_STATE_DIR = Path("/srv/appliance")
DATA_DIR = Path("/data")
BOOT_FIRMWARE_DIR = Path("/boot/firmware")
BOOT_STATE_FILENAME = "boot-state.json"

#: Mount points the appliance cares about (§2.3, §4.4), in partition order.
APPLIANCE_MOUNTS: tuple[str, ...] = ("/", "/boot/firmware", "/srv/appliance", "/data", "/srv/local")

# Kernel and firmware paths, relative to the platform root. Kept in one place so a
# port only has to look here.
_DEVICE_TREE_MODEL = "/proc/device-tree/model"
_DEVICE_TREE_HAT_PRODUCT = "/proc/device-tree/hat/product"
_DMI_PRODUCT_NAME = "/sys/class/dmi/id/product_name"
_DMI_SYS_VENDOR = "/sys/class/dmi/id/sys_vendor"
_KERNEL_CMDLINE = "/proc/cmdline"
_MEMINFO = "/proc/meminfo"
_UPTIME = "/proc/uptime"
_MOUNTS = "/proc/mounts"
_THERMAL_ZONES = "/sys/class/thermal"
_HWMON = "/sys/class/hwmon"
_POWER_SUPPLY = "/sys/class/power_supply"
_WATCHDOG_DEVICE = "/dev/watchdog"

PORTING_GUIDE = "docs/hardware/porting.md"


class PlatformError(RuntimeError):
    """A platform operation could not be carried out on this machine."""


# --------------------------------------------------------------------------- types


@dataclass(frozen=True, slots=True)
class StorageHealth:
    """Self-reported health of the root block device (smartctl or nvme-cli).

    ``partial`` is ``True`` when the reading is incomplete for any reason — the
    query timed out, the tool is not installed, the output could not be parsed.
    ``health`` is the drive's own assessment; the §11.2 colour thresholds are
    applied separately by :func:`proskenion.core.vitals.classify`.
    """

    model: str | None
    health: StorageCondition
    life_used_percent: float | None
    temperature_c: float | None
    media_errors: int | None
    partial: bool
    detail: str | None

    @classmethod
    def unavailable(cls, detail: str, *, partial: bool = True) -> StorageHealth:
        return cls(
            model=None,
            health="unknown",
            life_used_percent=None,
            temperature_c=None,
            media_errors=None,
            partial=partial,
            detail=detail,
        )


@dataclass(frozen=True, slots=True)
class PartitionUsage:
    """Usage of one appliance partition (§2.3). ``slot`` is set for the root filesystem."""

    mount: str
    total_bytes: int
    used_bytes: int
    slot: Slot | None = None

    @property
    def free_bytes(self) -> int:
        return self.total_bytes - self.used_bytes


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


#: ``runner(argv, timeout_s) -> CommandResult``. Runs synchronously; the platform
#: layer calls it from ``asyncio.to_thread``. It may raise ``FileNotFoundError``
#: (tool not installed) or ``subprocess.TimeoutExpired``.
CommandRunner = Callable[[Sequence[str], float], CommandResult]


def run_command(argv: Sequence[str], timeout_s: float) -> CommandResult:
    """The production command runner: ``subprocess.run`` with a hard timeout."""
    completed = subprocess.run(
        list(argv), capture_output=True, text=True, timeout=timeout_s, check=False
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


# ----------------------------------------------------------------- boot-state.json


if sys.platform == "win32":  # pragma: no cover - development machines only

    def _lock_exclusive(fd: int) -> bool:
        """Windows has no appliance and one writer, so there is nothing to lock."""
        return True

else:
    import fcntl

    def _lock_exclusive(fd: int) -> bool:
        """Take the file's exclusive lock, or report that someone else holds it."""
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            return False
        return True


def _same_file(path: Path, fd: int) -> bool:
    """Whether ``fd`` is still the inode ``path`` names."""
    try:
        return os.stat(path).st_ino == os.fstat(fd).st_ino
    except FileNotFoundError:
        return False


def iso_now() -> str:
    """Local time, ISO 8601 with offset, second precision (§4.9)."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class Marker:
    """A ``{"version": …, "at": …}`` record (contracts §1).

    ``version`` is a **directory name** — the name ``/data/app/current``
    resolves to, ``v1.3.0`` — and is ``None`` when that cannot be resolved.
    It is never the running code's ``__version__``: the rollback service
    compares this value against the same directory name, and comparing
    ``"0.1.0"`` against ``"v1.3.0"`` made every third failed start look like a
    failed update, so three restarts would have rolled back a version that had
    been healthy for months.
    """

    version: str | None
    at: str | None

    def to_json(self) -> dict[str, Any]:
        return {"version": self.version, "at": self.at}

    @classmethod
    def from_json(cls, data: object) -> Marker | None:
        if not isinstance(data, Mapping):
            return None
        return cls(version=_str_or_none(data.get("version")), at=_str_or_none(data.get("at")))


@dataclass(frozen=True, slots=True)
class BootState:
    """Contents of ``/srv/appliance/boot-state.json`` (§2.3, §14.4, §14.5).

    Three processes write this file — the application, ``auditorium-helper``
    and ``auditorium-update-rollback`` — so contracts §1 makes every writer
    read, merge and write: it replaces the keys it owns and preserves the
    rest, **including keys it does not model**. ``extra`` is where the
    unmodelled ones are kept, so a start marker cannot drop the ``update``
    record the rollback path depends on.

    ``started`` is written at every application start and ``healthy`` after
    thirty seconds of healthy operation. They are kept separately because the
    rollback service compares them: a start marker for the new version with a
    healthy marker still from the previous one means the update never came up.
    ``slots`` maps slot letter to root PARTUUID and is written by the image
    builder. ``update``, ``rollback`` and ``trial`` belong to the updater, the
    rollback script and the OS trial; they are carried here as plain mappings
    because this side only has to preserve them.
    """

    active_slot: Slot | None = None
    last_known_good: Slot | None = None
    staged: Slot | None = None
    slots: Mapping[str, str] = field(default_factory=dict)
    started: Marker | None = None
    healthy: Marker | None = None
    update: Mapping[str, Any] | None = None
    rollback: Mapping[str, Any] | None = None
    trial: Mapping[str, Any] | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    #: Keys this dataclass models. Everything else goes to ``extra``.
    KNOWN: ClassVar[tuple[str, ...]] = (
        "active_slot",
        "last_known_good",
        "staged",
        "slots",
        "started",
        "healthy",
        "update",
        "rollback",
        "trial",
    )

    @property
    def started_version(self) -> str | None:
        return self.started.version if self.started else None

    @property
    def healthy_version(self) -> str | None:
        return self.healthy.version if self.healthy else None

    def slot_for_partuuid(self, partuuid: str) -> Slot | None:
        wanted = partuuid.lower()
        for slot, uuid in self.slots.items():
            if uuid.lower() == wanted and slot in ("a", "b"):
                return "a" if slot == "a" else "b"
        return None

    def to_json(self) -> dict[str, Any]:
        """The document, schema keys in the contract's order then the rest."""
        body: dict[str, Any] = {
            "active_slot": self.active_slot,
            "last_known_good": self.last_known_good,
            "staged": self.staged,
            "slots": dict(self.slots),
            "started": self.started.to_json() if self.started else None,
            "healthy": self.healthy.to_json() if self.healthy else None,
            "update": dict(self.update) if self.update is not None else None,
            "rollback": dict(self.rollback) if self.rollback is not None else None,
            "trial": dict(self.trial) if self.trial is not None else None,
        }
        for key in sorted(self.extra):
            body[key] = self.extra[key]
        return body

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> BootState:
        slots_raw = data.get("slots") or {}
        slots: dict[str, str] = {}
        if isinstance(slots_raw, Mapping):
            slots = {str(k): str(v) for k, v in slots_raw.items()}
        return cls(
            active_slot=_slot_or_none(data.get("active_slot")),
            last_known_good=_slot_or_none(data.get("last_known_good")),
            staged=_slot_or_none(data.get("staged")),
            slots=slots,
            started=Marker.from_json(data.get("started")),
            healthy=Marker.from_json(data.get("healthy")),
            update=_mapping_or_none(data.get("update")),
            rollback=_mapping_or_none(data.get("rollback")),
            trial=_mapping_or_none(data.get("trial")),
            extra={k: v for k, v in data.items() if k not in cls.KNOWN},
        )


def _slot_or_none(value: object) -> Slot | None:
    if value == "a":
        return "a"
    if value == "b":
        return "b"
    return None


def _str_or_none(value: object) -> str | None:
    return None if value is None else str(value)


def _mapping_or_none(value: object) -> Mapping[str, Any] | None:
    return dict(value) if isinstance(value, Mapping) else None


def resolve_version(app_dir: Path) -> str | None:
    """The version directory ``app_dir/current`` points at, or ``None``.

    Contracts §1: a version is a directory name. A ``current`` that is not a
    symlink — a development checkout, a half-finished install — yields
    ``None``, which is written as ``null``. Guessing would put a value in the
    file that the rollback service compares against a directory name and can
    never match.
    """
    try:
        target = os.readlink(app_dir / "current")
    except OSError:
        return None
    return os.path.basename(target.rstrip("/")) or None


class BootStateStore:
    """Reads and atomically rewrites ``boot-state.json`` (contracts §1).

    Every write is a read-merge-write under an exclusive lock on the file, so
    a marker written while the helper is recording an update cannot lose
    either record. The lock is revalidated against the file's inode once
    acquired, because the write finishes with ``rename`` and a writer that
    queued on the old inode would otherwise merge into a document nobody will
    read. ``appliance/lib/auditorium_bootstate.py`` is the same discipline for
    the two root-side writers, and the two are checked against each other in
    ``tests/unit/appliance/test_boot_state_agreement.py``.

    Public methods are async and run the file I/O in a thread; the ``*_sync``
    variants exist for the platform implementations, which are already off-loop.
    """

    #: How long to wait for another writer before giving up.
    lock_timeout_s: ClassVar[float] = 10.0
    lock_poll_s: ClassVar[float] = 0.01
    file_mode: ClassVar[int] = 0o664

    def __init__(
        self,
        path: Path,
        *,
        now: Callable[[], str] = iso_now,
        app_dir: Path | None = None,
    ) -> None:
        self.path = path
        self._now = now
        #: Where ``current`` lives, for resolving a marker's version.
        self.app_dir = app_dir if app_dir is not None else DATA_DIR / "app"

    # -- reading -----------------------------------------------------------

    def read_sync(self) -> BootState:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return BootState()
        return self._parse(text)

    def _parse(self, text: str) -> BootState:
        if not text.strip():
            return BootState()
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PlatformError(f"{self.path} is not valid JSON: {exc}") from exc
        if not isinstance(data, Mapping):
            raise PlatformError(f"{self.path} must contain a JSON object")
        return BootState.from_json(data)

    # -- writing -----------------------------------------------------------

    def _open_for_lock(self) -> int:
        """Open the document for locking, creating it only when it is absent.

        Deliberately not ``O_RDWR | O_CREAT`` in one call. ``/srv/appliance``
        is sticky and group-writable so that this application can rename its
        own marker over a file the root helper last wrote, and Linux's
        ``fs.protected_regular`` refuses an ``O_CREAT`` open of an *existing*
        file in such a directory unless the opener owns it — root included.
        Asking for "create it if missing" is therefore what denies the write
        to whichever of the two writers does not own the file.
        """
        try:
            return os.open(self.path, os.O_RDWR)
        except FileNotFoundError:
            pass
        try:
            return os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL, self.file_mode)
        except FileExistsError:
            return os.open(self.path, os.O_RDWR)  # somebody else created it first

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold the file's lock for a whole read-merge-write.

        The lock is on ``boot-state.json`` itself, so it is the same lock the
        two root-side writers take — a lock file of our own would only
        serialise this process against itself. Once acquired it is checked
        against the file's inode: every writer finishes with ``rename``, so a
        writer that queued behind one of them holds a lock on an inode that is
        no longer the file, and must start again rather than merge into a
        document nobody will read.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":  # pragma: no cover - development machines only
            # No appliance, one writer, and Windows will not let a rename
            # replace a file something still has open.
            yield
            return
        deadline = time.monotonic() + self.lock_timeout_s
        while True:
            fd = self._open_for_lock()
            try:
                while not _lock_exclusive(fd):
                    if time.monotonic() >= deadline:
                        raise PlatformError(
                            f"could not lock {self.path} within {self.lock_timeout_s:g} s"
                        )
                    time.sleep(self.lock_poll_s)
                if _same_file(self.path, fd):
                    yield
                    return
                if time.monotonic() >= deadline:
                    raise PlatformError(f"{self.path} was replaced repeatedly while locking it")
            finally:
                os.close(fd)

    def _carry_over_ownership(self, tmp: Path) -> None:
        """Give the replacement the ownership and mode the document already had.

        The file is replaced by rename, so the new inode belongs to whoever
        wrote it. Two of its three writers are root: without this, the first
        root write turns the file over to root:root, and ``/srv/appliance`` is
        sticky, so this process could never rename its start marker over it
        again.
        """
        try:
            existing = os.stat(self.path)
        except FileNotFoundError:
            return
        if sys.platform != "win32":
            try:
                os.chown(tmp, existing.st_uid, existing.st_gid)
            except OSError:
                pass  # unprivileged: our own uid and gid are already the right answer
        try:
            os.chmod(tmp, stat.S_IMODE(existing.st_mode))
        except OSError:
            pass

    def _write_atomic(self, state: BootState) -> None:
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, self.file_mode)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(state.to_json(), indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._carry_over_ownership(tmp)
            os.replace(tmp, self.path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        _fsync_directory(self.path.parent)

    def write_sync(self, state: BootState) -> None:
        """Replace the whole document. Used where the caller already merged."""
        with self._locked():
            self._write_atomic(state)

    def update_sync(self, **changes: Any) -> BootState:
        """Read, apply ``changes``, write — all under one lock (contracts §1)."""
        with self._locked():
            merged = replace(self.read_sync(), **changes)
            self._write_atomic(merged)
            return merged

    def clear_sync(self, *keys: str) -> BootState:
        """Drop ``keys`` — how ``update`` and ``rollback`` are retired."""
        return self.update_sync(**dict.fromkeys(keys, None))

    # -- async -------------------------------------------------------------

    async def read(self) -> BootState:
        return await asyncio.to_thread(self.read_sync)

    async def write(self, state: BootState) -> None:
        await asyncio.to_thread(self.write_sync, state)

    async def update(self, **changes: Any) -> BootState:
        return await asyncio.to_thread(lambda: self.update_sync(**changes))

    async def clear(self, *keys: str) -> BootState:
        return await asyncio.to_thread(lambda: self.clear_sync(*keys))

    def resolve_version(self) -> str | None:
        """The deployed version, as a directory name (contracts §1)."""
        return resolve_version(self.app_dir)

    async def write_start_marker(self) -> BootState:
        """Record that the deployed version is starting now (§14.5).

        Clears nothing else — ``update`` and ``rollback`` in particular, which
        the updater and the rollback script own and which the application must
        still be able to read at its next start.
        """
        return await asyncio.to_thread(
            lambda: self.update_sync(started=Marker(self.resolve_version(), self._now()))
        )

    async def write_healthy_marker(self) -> BootState:
        """Record that the deployed version has been running healthily (§14.5)."""
        return await asyncio.to_thread(
            lambda: self.update_sync(healthy=Marker(self.resolve_version(), self._now()))
        )


def _fsync_directory(directory: Path) -> None:
    """Make the rename durable. Not every platform allows opening a directory."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# --------------------------------------------------------------------- protocol


@runtime_checkable
class Platform(Protocol):
    """The §5.4 protocol, plus the vitals accessors §11.2 needs.

    Everything that may block is async; only the pure lookups are synchronous.
    """

    def name(self) -> str: ...
    async def cpu_temperature(self) -> float | None: ...
    async def storage_health(self) -> StorageHealth: ...  # smartctl / nvme-cli
    async def power_source(self) -> PowerSource: ...
    def watchdog_device(self) -> Path | None: ...
    def boot_config_path(self) -> Path | None: ...
    async def active_root_slot(self) -> Slot: ...
    async def stage_root_slot(self, slot: Slot) -> None: ...  # arm the next boot
    async def confirm_root_slot(self) -> None: ...  # make it permanent

    # Additions for the health screen (§11.2) — kept on the protocol so the health
    # screen never touches /sys itself.
    async def partition_usage(self) -> list[PartitionUsage]: ...
    async def memory(self) -> tuple[int, int] | None: ...  # (used, total) bytes
    async def uptime_seconds(self) -> float | None: ...
    def appliance_state_dir(self) -> Path: ...
    def data_dir(self) -> Path: ...


def boot_state_store(platform: Platform) -> BootStateStore:
    """The ``boot-state.json`` store for a platform's appliance state directory."""
    return BootStateStore(
        platform.appliance_state_dir() / BOOT_STATE_FILENAME,
        app_dir=platform.data_dir() / "app",
    )


# ------------------------------------------------------------------- detection


def detect_platform(
    *,
    root: Path | None = None,
    runner: CommandRunner = run_command,
    command_timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S,
    appliance_dir: Path | None = None,
    data_dir: Path | None = None,
) -> Platform:
    """Select the platform from the device tree or DMI (§5.4).

    ``root`` prefixes every kernel and firmware path, so tests can build a fake
    tree. With the default root, a Linux host that exposes neither a device-tree
    model nor DMI still gets ``GenericLinuxPlatform``; anything else is a
    developer machine.

    ``appliance_dir`` and ``data_dir`` are honoured only for
    :class:`DevelopmentPlatform`: on a real appliance ``root`` already fixes
    ``/srv/appliance`` and ``/data`` as real mounts, and §5.4 keeps that
    behaviour unchanged. A development machine has neither, so a caller passes
    ``config.app.state_dir`` and ``config.app.data_dir`` to keep it from
    writing under the real, hard-coded paths (e.g. ``C:\\data`` on Windows).
    """
    root = Path("/") if root is None else root
    model = _read_text(_under(root, _DEVICE_TREE_MODEL))
    if model is not None and model.lower().startswith("raspberry pi"):
        return RaspberryPiPlatform(
            root=root, runner=runner, command_timeout_s=command_timeout_s, model=model
        )
    if model is None:
        product = _read_text(_under(root, _DMI_PRODUCT_NAME))
        vendor = _read_text(_under(root, _DMI_SYS_VENDOR))
        if product is not None:
            model = f"{vendor} {product}".strip() if vendor else product
    is_linux = model is not None or _under(root, _KERNEL_CMDLINE).exists()
    if root == Path("/") and sys.platform == "linux":
        is_linux = True
    if is_linux:
        return GenericLinuxPlatform(
            root=root, runner=runner, command_timeout_s=command_timeout_s, model=model
        )
    return DevelopmentPlatform(appliance_dir=appliance_dir, data_dir=data_dir)


def _under(root: Path, absolute: str) -> Path:
    """``/proc/cmdline`` under ``root`` — ``root/proc/cmdline``."""
    return root.joinpath(*Path(absolute).parts[1:])


def _read_text(path: Path) -> str | None:
    """Read a small kernel-exported file; ``None`` if absent or unreadable."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    return raw.rstrip(b"\0").decode("utf-8", errors="replace").strip()


# --------------------------------------------------------------- Linux common


class _LinuxPlatform:
    """Behaviour shared by every Linux implementation.

    Subclasses set ``PLATFORM_NAME`` and ``THERMAL_ZONE_PREFERENCE`` and override
    what genuinely differs (power source, boot configuration, root slots).
    """

    PLATFORM_NAME = "linux"
    #: Thermal zone types tried in order before falling back to any readable zone.
    THERMAL_ZONE_PREFERENCE: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        root: Path | None = None,
        boot_dir: Path | None = None,
        appliance_dir: Path | None = None,
        data_dir: Path | None = None,
        runner: CommandRunner = run_command,
        command_timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S,
        model: str | None = None,
        now: Callable[[], str] = iso_now,
    ) -> None:
        self._root = Path("/") if root is None else root
        self._boot_dir = boot_dir or _under(self._root, str(BOOT_FIRMWARE_DIR))
        self._appliance_dir = appliance_dir or _under(self._root, str(APPLIANCE_STATE_DIR))
        self._data_dir = data_dir or _under(self._root, str(DATA_DIR))
        self._runner = runner
        self._timeout_s = command_timeout_s
        self._model = model
        self.boot_state = BootStateStore(
            self._appliance_dir / BOOT_STATE_FILENAME, now=now, app_dir=self._data_dir / "app"
        )
        self._root_device: str | None = None

    # -- pure lookups ----------------------------------------------------------

    def name(self) -> str:
        return self.PLATFORM_NAME

    @property
    def model(self) -> str | None:
        """Human-readable board or machine name, when the firmware exposes one."""
        return self._model

    @property
    def command_timeout_s(self) -> float:
        return self._timeout_s

    def watchdog_device(self) -> Path | None:
        return Path(_WATCHDOG_DEVICE) if self._path(_WATCHDOG_DEVICE).exists() else None

    def boot_config_path(self) -> Path | None:
        return None

    def appliance_state_dir(self) -> Path:
        return self._appliance_dir

    def data_dir(self) -> Path:
        return self._data_dir

    # -- vitals ----------------------------------------------------------------

    async def cpu_temperature(self) -> float | None:
        return await asyncio.to_thread(self._cpu_temperature_sync)

    async def memory(self) -> tuple[int, int] | None:
        return await asyncio.to_thread(self._memory_sync)

    async def uptime_seconds(self) -> float | None:
        return await asyncio.to_thread(self._uptime_sync)

    async def partition_usage(self) -> list[PartitionUsage]:
        return await asyncio.to_thread(self._partition_usage_sync)

    async def power_source(self) -> PowerSource:
        return await asyncio.to_thread(self._power_source_sync)

    async def storage_health(self) -> StorageHealth:
        """Query the root block device, bounded by the command timeout (§5.4).

        Two guards: the runner itself is given the timeout (so a hung ``smartctl``
        child is killed), and the whole query is wrapped in ``wait_for`` (so a
        runner that ignores its timeout still cannot stall the caller).
        """
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._storage_health_sync), self._timeout_s
            )
        except TimeoutError:
            return StorageHealth.unavailable(
                f"storage health query exceeded {self._timeout_s:g} s", partial=True
            )

    # -- root slots ------------------------------------------------------------

    async def active_root_slot(self) -> Slot:
        return await asyncio.to_thread(self._active_slot_sync)

    async def stage_root_slot(self, slot: Slot) -> None:
        raise NotImplementedError(
            f"root-slot staging is not implemented for {self.name()}; see {PORTING_GUIDE}"
        )

    async def confirm_root_slot(self) -> None:
        raise NotImplementedError(
            f"root-slot confirmation is not implemented for {self.name()}; see {PORTING_GUIDE}"
        )

    # -- synchronous helpers (always called off the event loop) ----------------

    def _path(self, absolute: str) -> Path:
        return _under(self._root, absolute)

    def _run(self, argv: Sequence[str]) -> CommandResult:
        return self._runner(argv, self._timeout_s)

    def _cpu_temperature_sync(self) -> float | None:
        zones: dict[str, Path] = {}
        zone_dir = self._path(_THERMAL_ZONES)
        if not zone_dir.is_dir():
            return None
        for zone in sorted(zone_dir.glob("thermal_zone*")):
            zone_type = _read_text(zone / "type")
            if zone_type is not None and zone_type not in zones:
                zones[zone_type] = zone
        ordered = [zones[t] for t in self.THERMAL_ZONE_PREFERENCE if t in zones]
        ordered += [z for t, z in zones.items() if t not in self.THERMAL_ZONE_PREFERENCE]
        for zone in ordered:
            raw = _read_text(zone / "temp")
            if raw is None:
                continue
            try:
                return int(raw) / 1000.0
            except ValueError:
                continue
        return None

    def _memory_sync(self) -> tuple[int, int] | None:
        text = _read_text(self._path(_MEMINFO))
        if text is None:
            return None
        values: dict[str, int] = {}
        for line in text.splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if key in ("MemTotal", "MemAvailable") and parts:
                try:
                    values[key] = int(parts[0]) * 1024
                except ValueError:
                    return None
        if "MemTotal" not in values or "MemAvailable" not in values:
            return None
        total = values["MemTotal"]
        return total - values["MemAvailable"], total

    def _uptime_sync(self) -> float | None:
        text = _read_text(self._path(_UPTIME))
        if not text:
            return None
        try:
            return float(text.split()[0])
        except ValueError:
            return None

    def _mounted_points_sync(self) -> list[str]:
        text = _read_text(self._path(_MOUNTS))
        if text is None:
            return []
        mounted: list[str] = []
        for line in text.splitlines():
            fields = line.split()
            if len(fields) >= 2:
                # /proc/mounts escapes spaces as \040.
                mounted.append(fields[1].replace("\\040", " "))
        return mounted

    def _partition_usage_sync(self) -> list[PartitionUsage]:
        mounted = set(self._mounted_points_sync())
        result: list[PartitionUsage] = []
        for mount in APPLIANCE_MOUNTS:
            if mount not in mounted:
                continue
            try:
                usage = shutil.disk_usage(self._path(mount))
            except OSError:
                continue
            slot = self._active_slot_or_none_sync() if mount == "/" else None
            result.append(
                PartitionUsage(
                    mount=mount, total_bytes=usage.total, used_bytes=usage.used, slot=slot
                )
            )
        return result

    def _power_source_sync(self) -> PowerSource:
        supply_dir = self._path(_POWER_SUPPLY)
        if supply_dir.is_dir():
            for supply in sorted(supply_dir.iterdir()):
                if _read_text(supply / "type") == "Mains":
                    online = _read_text(supply / "online")
                    if online in (None, "1"):
                        return "mains"
        return "unknown"

    def _kernel_root_spec_sync(self) -> str | None:
        """The ``root=`` value from the kernel command line."""
        cmdline = _read_text(self._path(_KERNEL_CMDLINE))
        if cmdline is None:
            return None
        for token in cmdline.split():
            if token.startswith("root="):
                return token[len("root=") :]
        return None

    def _active_slot_or_none_sync(self) -> Slot | None:
        spec = self._kernel_root_spec_sync()
        state = self.boot_state.read_sync()
        if spec is not None and spec.upper().startswith("PARTUUID="):
            slot = state.slot_for_partuuid(spec[len("PARTUUID=") :])
            if slot is not None:
                return slot
        return state.active_slot

    def _active_slot_sync(self) -> Slot:
        slot = self._active_slot_or_none_sync()
        if slot is None:
            raise PlatformError(
                "cannot determine the active root slot: the kernel command line's root= "
                f"does not match a PARTUUID in {self.boot_state.path} and no active_slot "
                "is recorded there"
            )
        return slot

    def _root_block_device_sync(self) -> str | None:
        """Resolve the whole-disk device (``/dev/nvme0n1``) holding the root partition.

        Uses ``lsblk`` rather than walking ``/sys/class/block`` by hand: the root is
        an overlay (§4.3), so the mount table alone does not name the partition.
        """
        if self._root_device is not None:
            return self._root_device
        spec = self._kernel_root_spec_sync()
        if spec is None:
            return None
        result = self._run(["lsblk", "-J", "-o", "NAME,PATH,PKNAME,PARTUUID,UUID,TYPE"])
        if result.returncode != 0:
            return None
        try:
            tree = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        devices = tree.get("blockdevices", []) if isinstance(tree, dict) else []
        node = _find_block_device(devices, spec)
        if node is None:
            return None
        parent = node.get("pkname")
        if parent:
            device = f"/dev/{parent}"
        else:
            device = str(node.get("path") or f"/dev/{node.get('name')}")
        self._root_device = device
        return device

    def _storage_health_sync(self) -> StorageHealth:
        try:
            device = self._root_block_device_sync()
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            return StorageHealth.unavailable(f"could not resolve root block device: {exc}")
        if device is None:
            return StorageHealth.unavailable("could not resolve root block device")
        try:
            return self._query_smart(device)
        except subprocess.TimeoutExpired:
            return StorageHealth.unavailable(
                f"storage health query on {device} exceeded {self._timeout_s:g} s"
            )
        except OSError as exc:
            return StorageHealth.unavailable(f"storage health query on {device} failed: {exc}")

    def _query_smart(self, device: str) -> StorageHealth:
        try:
            result = self._run(["smartctl", "-j", "-a", device])
        except FileNotFoundError:
            result = None
        if result is not None:
            data = _json_object(result.stdout)
            if data is not None:
                return _parse_smartctl(data)
            if not Path(device).name.startswith("nvme"):
                return StorageHealth.unavailable(
                    f"smartctl produced no usable output for {device}: "
                    f"{(result.stderr or result.stdout).strip()[:200]}"
                )
        if Path(device).name.startswith("nvme"):
            result = self._run(["nvme", "smart-log", "-o", "json", device])
            data = _json_object(result.stdout)
            if data is not None:
                return _parse_nvme_smart_log(data)
            return StorageHealth.unavailable(
                f"nvme smart-log produced no usable output for {device}: "
                f"{(result.stderr or result.stdout).strip()[:200]}"
            )
        return StorageHealth.unavailable("smartctl is not installed")

    @contextmanager
    def _boot_partition_writable(self) -> Iterator[Path]:
        """Remount the boot partition read-write for the duration (§2.3)."""
        rw = self._run(["mount", "-o", "remount,rw", str(self._boot_dir)])
        if rw.returncode != 0:
            raise PlatformError(
                f"could not remount {self._boot_dir} read-write: {rw.stderr.strip()}"
            )
        try:
            yield self._boot_dir
        finally:
            self._run(["mount", "-o", "remount,ro", str(self._boot_dir)])


def _find_block_device(nodes: list[Any], spec: str) -> dict[str, Any] | None:
    """Depth-first search of lsblk's tree for the node matching a ``root=`` spec."""
    key, _, value = spec.partition("=")
    for node in nodes:
        if not isinstance(node, dict):
            continue
        matched = False
        if key.upper() == "PARTUUID" and value:
            matched = str(node.get("partuuid") or "").lower() == value.lower()
        elif key.upper() == "UUID" and value:
            matched = str(node.get("uuid") or "").lower() == value.lower()
        elif spec.startswith("/dev/"):
            matched = node.get("path") == spec
        if matched:
            return node
        found = _find_block_device(node.get("children") or [], spec)
        if found is not None:
            return found
    return None


def _json_object(text: str) -> dict[str, Any] | None:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _assess(
    *,
    passed: bool | None,
    critical_warning: int,
    life_used: float | None,
    media_errors: int | None,
    temperature: float | None,
) -> StorageCondition:
    if passed is False or critical_warning != 0:
        return "failing"
    if (media_errors or 0) > 0 or (life_used is not None and life_used > 80):
        return "warning"
    if passed is None and life_used is None and media_errors is None and temperature is None:
        return "unknown"
    return "healthy"


def _parse_smartctl(data: dict[str, Any]) -> StorageHealth:
    model = _str_or_none(data.get("model_name"))
    temp_block = data.get("temperature")
    temperature = _number(temp_block.get("current")) if isinstance(temp_block, dict) else None
    status = data.get("smart_status")
    passed = status.get("passed") if isinstance(status, dict) else None
    passed = passed if isinstance(passed, bool) else None
    life_used: float | None = None
    media_errors: int | None = None
    critical = 0
    nvme_log = data.get("nvme_smart_health_information_log")
    if isinstance(nvme_log, dict):
        life_used = _number(nvme_log.get("percentage_used"))
        errors = _number(nvme_log.get("media_errors"))
        media_errors = int(errors) if errors is not None else None
        critical = int(_number(nvme_log.get("critical_warning")) or 0)
    else:
        ata = data.get("ata_smart_attributes")
        table = ata.get("table") if isinstance(ata, dict) else None
        for attr in table or []:
            if not isinstance(attr, dict):
                continue
            attr_id = attr.get("id")
            raw = attr.get("raw")
            raw_value = _number(raw.get("value")) if isinstance(raw, dict) else None
            normalised = _number(attr.get("value"))
            # 231 SSD_Life_Left, 177 Wear_Leveling_Count, 233 Media_Wearout_Indicator:
            # the normalised value is the percentage of life remaining.
            if attr_id in (231, 177, 233) and normalised is not None and life_used is None:
                life_used = max(0.0, 100.0 - normalised)
            # 187 Reported_Uncorrect, 198 Offline_Uncorrectable: raw is an error count.
            if attr_id in (187, 198) and raw_value is not None:
                media_errors = (media_errors or 0) + int(raw_value)
    health = _assess(
        passed=passed,
        critical_warning=critical,
        life_used=life_used,
        media_errors=media_errors,
        temperature=temperature,
    )
    return StorageHealth(
        model=model,
        health=health,
        life_used_percent=life_used,
        temperature_c=temperature,
        media_errors=media_errors,
        partial=False,
        detail=None,
    )


def _parse_nvme_smart_log(data: dict[str, Any]) -> StorageHealth:
    life_used = _number(data.get("percent_used"))
    if life_used is None:
        life_used = _number(data.get("percentage_used"))
    temperature = _number(data.get("temperature"))
    if temperature is not None and temperature > 170:
        temperature = round(temperature - 273.15, 1)  # nvme-cli reports kelvin
    errors = _number(data.get("media_errors"))
    media_errors = int(errors) if errors is not None else None
    critical = int(_number(data.get("critical_warning")) or 0)
    health = _assess(
        passed=None,
        critical_warning=critical,
        life_used=life_used,
        media_errors=media_errors,
        temperature=temperature,
    )
    return StorageHealth(
        model=None,
        health=health,
        life_used_percent=life_used,
        temperature_c=temperature,
        media_errors=media_errors,
        partial=False,
        detail="nvme-cli smart-log does not report the model name",
    )


# ------------------------------------------------------------------ Raspberry Pi


class RaspberryPiPlatform(_LinuxPlatform):
    """The reference CM5 appliance (§4.8, §14.4).

    Root slots use the firmware's ``tryboot`` mechanism. The boot partition holds
    ``slot-a/`` and ``slot-b/`` directories (kernel, initramfs, cmdline.txt,
    overlays) and ``config.txt`` selects one with ``os_prefix=slot-a/``. Staging a
    slot writes ``tryboot.txt`` — ``config.txt`` with the ``os_prefix`` line
    changed — and the OS-upgrade orchestrator then reboots with
    ``reboot "0 tryboot"``. If that boot fails and the watchdog reboots, the
    firmware falls back to ``config.txt``. Confirming copies ``tryboot.txt`` over
    ``config.txt``.
    """

    PLATFORM_NAME = "raspberry-pi"
    THERMAL_ZONE_PREFERENCE = ("cpu-thermal",)

    def boot_config_path(self) -> Path | None:
        return self._boot_dir / "config.txt"

    def tryboot_path(self) -> Path:
        return self._boot_dir / "tryboot.txt"

    async def stage_root_slot(self, slot: Slot) -> None:
        await asyncio.to_thread(self._stage_root_slot_sync, slot)

    async def confirm_root_slot(self) -> None:
        await asyncio.to_thread(self._confirm_root_slot_sync)

    def _power_source_sync(self) -> PowerSource:
        hat = _read_text(self._path(_DEVICE_TREE_HAT_PRODUCT))
        if hat is not None and "poe" in hat.lower():
            return "poe"
        hwmon_dir = self._path(_HWMON)
        if hwmon_dir.is_dir():
            for hwmon in hwmon_dir.iterdir():
                name = _read_text(hwmon / "name")
                if name is not None and "poe" in name.lower():
                    return "poe"
        return super()._power_source_sync()

    def _stage_root_slot_sync(self, slot: Slot) -> None:
        if slot not in ("a", "b"):
            raise ValueError(f"root slot must be 'a' or 'b', not {slot!r}")
        active = self._active_slot_sync()
        if slot == active:
            raise PlatformError(f"slot {slot} is the running root slot; nothing to stage")
        config = self.boot_config_path()
        assert config is not None
        try:
            text = config.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise PlatformError(f"{config} does not exist") from exc
        with self._boot_partition_writable():
            self.tryboot_path().write_text(_with_os_prefix(text, slot), encoding="utf-8")
        self.boot_state.update_sync(staged=slot)

    def _confirm_root_slot_sync(self) -> None:
        active = self._active_slot_sync()
        state = self.boot_state.read_sync()
        tryboot = self.tryboot_path()
        if not tryboot.exists():
            raise PlatformError(f"{tryboot} does not exist; no root slot is staged")
        if state.staged is not None and state.staged != active:
            raise PlatformError(
                f"slot {state.staged} was staged but slot {active} is running; "
                "refusing to confirm"
            )
        config = self.boot_config_path()
        assert config is not None
        with self._boot_partition_writable():
            staged_config = config.with_name("config.txt.new")
            shutil.copyfile(tryboot, staged_config)
            os.replace(staged_config, config)
        self.boot_state.update_sync(active_slot=active, last_known_good=active, staged=None)


def _with_os_prefix(config_text: str, slot: Slot) -> str:
    """``config.txt`` with every ``os_prefix=`` line pointing at ``slot``."""
    prefix_line = f"os_prefix=slot-{slot}/"
    lines = config_text.splitlines()
    replaced = False
    for index, line in enumerate(lines):
        if line.strip().startswith("os_prefix="):
            lines[index] = prefix_line
            replaced = True
    if not replaced:
        # Before any [section] filter, so it applies unconditionally.
        lines.insert(0, prefix_line)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- generic Linux


class GenericLinuxPlatform(_LinuxPlatform):
    """Any other Linux machine — x86 with UEFI, or a Rockchip board.

    Root-slot staging depends on the bootloader (systemd-boot one-shot entries,
    U-Boot bootcount) and is left to the port: see ``docs/hardware/porting.md``.
    """

    PLATFORM_NAME = "generic-linux"
    THERMAL_ZONE_PREFERENCE = ("x86_pkg_temp", "acpitz")


# ------------------------------------------------------------------ development


class DevelopmentPlatform:
    """A developer's machine. Every metric is "not available"; nothing is touched."""

    def __init__(
        self,
        *,
        appliance_dir: Path | None = None,
        data_dir: Path | None = None,
    ) -> None:
        self._appliance_dir = appliance_dir or APPLIANCE_STATE_DIR
        self._data_dir = data_dir or DATA_DIR

    def name(self) -> str:
        return "development"

    async def cpu_temperature(self) -> float | None:
        return None

    async def storage_health(self) -> StorageHealth:
        return StorageHealth.unavailable(
            "not available on the development platform", partial=False
        )

    async def power_source(self) -> PowerSource:
        return "unknown"

    def watchdog_device(self) -> Path | None:
        return None

    def boot_config_path(self) -> Path | None:
        return None

    async def active_root_slot(self) -> Slot:
        raise PlatformError("root slots are not available on the development platform")

    async def stage_root_slot(self, slot: Slot) -> None:
        raise PlatformError("root slots are not available on the development platform")

    async def confirm_root_slot(self) -> None:
        raise PlatformError("root slots are not available on the development platform")

    async def partition_usage(self) -> list[PartitionUsage]:
        return []

    async def memory(self) -> tuple[int, int] | None:
        return None

    async def uptime_seconds(self) -> float | None:
        return None

    def appliance_state_dir(self) -> Path:
        return self._appliance_dir

    def data_dir(self) -> Path:
        return self._data_dir
