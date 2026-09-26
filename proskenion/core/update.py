"""Application updates: staging, preparation, the ordered apply, and rollback.

§14.2 to §14.5. The admin may press Apply and walk out of the hall, so every
step here is written for the case where nobody is watching and the next thing
to happen is a power cut.

Standalone on purpose
    This module imports the standard library, :mod:`proskenion.core.packages`,
    :mod:`proskenion.core.platform`, :mod:`proskenion.core.helper` and
    :mod:`proskenion.core.snapshots` (standard library only) — no
    database driver, no web framework, nothing from the application's own
    dependency set. It is the code that replaces the application, so it must
    be runnable when the application's environment is the thing in doubt, and
    it must be drivable from a plain interpreter in the systemd harness. The
    wiring that needs the rest of the system — banners, alerts, the audit
    trail, progress frames — lives in
    :mod:`proskenion.core.update_service`.

The order, and why each step is where it is (§14.2, §14.3, Appendix B33)
    1. **Extract beside the current version.** ``/data/app/<version>/`` is the
       destination, not a staging area that is copied afterwards, so there is
       no second copy step to be interrupted. The previous version is never
       touched, which is what makes rollback a symlink change.
    2. **Build the Python environment**, named for the interpreter's ABI, from
       the wheels the package carries. It happens before anything is committed
       because a package whose wheels do not install is a package that would
       never have started.
    3. **Dry-run the migrations on a copy of the database, in the new
       environment.** The new code's migration runner is the one that will run
       at startup, so it is the one that has to be asked. A failure here has
       changed nothing: no snapshot, no marker, no symlink.
    4. **Take the snapshot.** Immediately before the swap, and not at step 1:
       a snapshot taken when the package was uploaded would lose every change
       made between then and an update applied at 3 a.m.
    5. **Write the ``update`` record.** Before the swap, so that a machine
       that dies between the record and the swap is described by a record that
       is merely premature, while one that dies after a swap with no record
       would be a new version nothing knows how to undo.
    6. **Swap ``current`` by rename.** ``ln -sfn`` unlinks before it links and
       leaves a window in which ``current`` does not resolve; a temporary
       symlink moved over the target is ``rename(2)``, which is atomic.
    7. **Restart through the helper.** The only privileged step, and the last
       one, because everything before it is reversible by doing nothing.

    Migrations are applied by the new version at startup and nowhere else
    (§15.2). This module tests them; it never runs them against the live
    database.

Rollback (§14.3) is the reverse in the one order that fails safe: the symlink
first, the snapshot second. Old code against a new schema is refused by the
schema-ahead guard, which is loud. New code against an old schema migrates
forward and silently undoes the rollback, which is not.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Final

from proskenion.core.helper import (
    DEFAULT_WATCHDOG_WINDOW_S,
    HelperClient,
    HelperError,
)
from proskenion.core.packages import (
    Manifest,
    PackageError,
    extract_verified,
    is_version,
    parse_version,
    verify_package,
)
from proskenion.core.platform import BootStateStore, iso_now, resolve_version
from proskenion.core.snapshots import PRE_UPDATE_PREFIX, take_snapshot_sync
from proskenion.core.snapshots import SnapshotFailed as _SnapshotNotTaken

log = logging.getLogger(__name__)

#: Q9: an OS package carries a root filesystem image, so the update upload is
#: capped far above §4.13's 256 MB — and never held in memory on a machine
#: with 4 GB of it.
MAX_UPLOAD_BYTES: Final = 2 * 1024 * 1024 * 1024

#: §14.2: "the previous two versions are retained; older ones are pruned".
RETAINED_PREVIOUS_VERSIONS: Final = 2

#: The §16.8 progress operations this module reports under (contracts §6).
VERIFY_OPERATION: Final = "update_verify"
APPLY_OPERATION: Final = "update_apply"

#: Where a verified-but-unapplied package is remembered, so the manifest the
#: admin reviewed is the manifest that gets applied and a restart between the
#: two does not orphan a two gigabyte file in /data/tmp.
PENDING_FILENAME: Final = "update-pending.json"

#: Written into a prepared version directory once its environment is built and
#: its migrations have been tested. Its absence is what distinguishes a
#: directory that is ready to be switched to from one an interrupted apply
#: left half-extracted.
PREPARED_FILENAME: Final = ".prepared.json"

SNAPSHOT_PREFIX: Final = PRE_UPDATE_PREFIX
UPLOAD_PREFIX: Final = "upload-"


def link_bootstrap_config(destination: Path, target: Path) -> None:
    """Point ``destination/config.toml`` at this machine's bootstrap file (§4.14).

    Whatever a package put at that name is replaced: the bootstrap file names
    this machine's database and log paths, and nothing in a package may choose
    them.
    """
    link = destination / CONFIG_LINK
    if link.is_symlink() or link.is_file():
        link.unlink()
    elif link.is_dir():
        shutil.rmtree(link)
    os.symlink(target, link)


def venv_name(executable: str | None = None) -> str:
    """The environment directory name for an interpreter ABI (Q11).

    One environment per interpreter version, so an OS upgrade that brings a
    new interpreter does not silently inherit an environment built against
    the old one — every compiled extension in it would be wrong. The name is
    the ABI tag, ``venv-cp313``, and ``venv`` is a symlink to whichever of
    them is current.
    """
    del executable  # the ABI is this interpreter's, whatever runs the build
    return f"venv-cp{sys.version_info.major}{sys.version_info.minor}"


#: The symlink :data:`auditorium-core.service`'s ``ExecStart`` goes through.
VENV_LINK: Final = "venv"

#: Where the package's wheels are, relative to the extracted version.
WHEELS_DIRNAME: Final = "wheels"

#: §4.14 and §5.2: each version directory's ``config.toml`` is a symlink to
#: the one bootstrap file under ``/data/config``. ``ExecStart`` and every tool
#: read ``/opt/auditorium/config.toml``, which resolves into the version
#: directory; a package cannot carry the link, because the verifier refuses
#: every link member (contracts §3), so the installer makes it — here, and in
#: the privileged helper's ``apply-update``.
CONFIG_LINK: Final = "config.toml"
BOOTSTRAP_CONFIG_RELATIVE: Final = Path("config") / "auditorium.toml"

#: ``/data/app``'s mode, which build.sh also gives it: nginx's workers (www-data)
#: serve the web interface from beneath it. The version directories inside get
#: the same from :func:`~proskenion.core.packages.extract_verified`.
APP_DIR_MODE: Final = 0o755


# --------------------------------------------------------------------------
# Failures
# --------------------------------------------------------------------------


class UpdateError(Exception):
    """An update was refused or could not be carried out.

    :attr:`rule` names which rule refused it and is what the API puts in the
    ``detail`` of a §16.1 ``validation_failed`` envelope, exactly as a
    :class:`~proskenion.core.packages.PackageError` does. :attr:`summary` is
    the sentence for the screen; ``str(e)`` carries the specifics and is for
    the log.
    """

    rule: ClassVar[str] = "update"
    summary: ClassVar[str] = "This update could not be applied."


class UploadTooLarge(UpdateError):
    """The upload exceeded :data:`MAX_UPLOAD_BYTES` (Q9)."""

    rule = "size"
    summary = "This file is larger than the appliance accepts."


class UploadEmpty(UpdateError):
    """Nothing arrived, so there is nothing to verify."""

    rule = "upload"
    summary = "No package was received."


class WheelsMissing(UpdateError):
    """The package carries no ``wheels/``, so no environment can be built."""

    rule = "wheels"
    summary = (
        "This package carries no Python wheels, so the environment it needs "
        "cannot be built without a network."
    )


class VenvBuildFailed(UpdateError):
    """``python -m venv`` or the offline wheel install failed."""

    rule = "venv"
    summary = "The Python environment for this package could not be built."


class MigrationDryRunFailed(UpdateError):
    """The new version's migrations failed against a copy of the database.

    Nothing has been changed: this is the whole point of testing them on a
    copy before the snapshot is taken (§14.2).
    """

    rule = "migration"
    summary = (
        "This package's database migrations failed against a copy of the "
        "live database, so nothing was changed."
    )


class SnapshotFailed(UpdateError):
    """The pre-update snapshot could not be taken, so the update stops."""

    rule = "snapshot"
    summary = (
        "A pre-update database snapshot could not be taken, so the update "
        "was not applied. An update with no way back is worse than no update."
    )


class NoPendingUpdate(UpdateError):
    """Apply was asked for with nothing verified and waiting."""

    rule = "no_pending"
    summary = "There is no verified package waiting to be applied."


class NotPrepared(UpdateError):
    """A version directory exists but was never finished."""

    rule = "not_prepared"
    summary = "This version was not prepared successfully; upload the package again."


class NothingToRollBackTo(UpdateError):
    """No previous version directory survives, so there is nowhere to go."""

    rule = "no_previous"
    summary = "No previous version is installed, so there is nothing to roll back to."


class RestartRefused(UpdateError):
    """The privileged helper would not restart the application."""

    rule = "restart"
    summary = "The appliance could not restart the application."


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UpdatePaths:
    """Everywhere an update reads or writes. One object, so tests move them all."""

    app_dir: Path
    tmp_dir: Path
    snapshots_dir: Path
    database: Path
    boot_state: Path

    @classmethod
    def for_appliance(cls, data_dir: Path, state_dir: Path, database: Path) -> UpdatePaths:
        return cls(
            app_dir=data_dir / "app",
            tmp_dir=data_dir / "tmp",
            snapshots_dir=data_dir / "backups" / "snapshots",
            database=database,
            boot_state=state_dir / "boot-state.json",
        )

    @property
    def current(self) -> Path:
        return self.app_dir / "current"

    def version_dir(self, version: str) -> Path:
        return self.app_dir / version

    @property
    def bootstrap_config(self) -> Path:
        """``/data/config/auditorium.toml``: ``app_dir`` is ``/data/app``."""
        return self.app_dir.parent / BOOTSTRAP_CONFIG_RELATIVE

    def pending_path(self) -> Path:
        return self.tmp_dir / PENDING_FILENAME

    def snapshot_path(self, version: str) -> Path:
        return self.snapshots_dir / f"{SNAPSHOT_PREFIX}{version}.db"

    def store(self) -> BootStateStore:
        return BootStateStore(self.boot_state, app_dir=self.app_dir)


# --------------------------------------------------------------------------
# The upload (Q9)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StagedUpload:
    """A package on disk in ``/data/tmp``, with the digest taken as it arrived."""

    path: Path
    sha256: str
    size: int


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte. ``os.write`` is allowed to write fewer than it is given."""
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


async def stage_upload(
    chunks: AsyncIterator[bytes],
    paths: UpdatePaths,
    *,
    max_bytes: int = MAX_UPLOAD_BYTES,
) -> StagedUpload:
    """Stream an upload to ``/data/tmp``, hashing it on the way through (Q9).

    Nothing is buffered: each chunk is written and discarded, so a two
    gigabyte package costs one chunk of memory rather than two gigabytes on a
    machine that has four. The digest is taken here rather than by reading the
    file again, because reading it again is a second pass over two gigabytes
    on an SSD that also holds the database.

    A refused or interrupted upload takes its file with it.
    """
    paths.tmp_dir.mkdir(parents=True, exist_ok=True)
    target = paths.tmp_dir / f"{UPLOAD_PREFIX}{uuid.uuid4()}.tar"
    digest = hashlib.sha256()
    size = 0
    # 0600: the package is read again by root, and until it has been verified
    # it is exactly the kind of file nothing else should be able to swap.
    #
    # The descriptor is used raw rather than through a buffered writer: a
    # buffer would hold part of the body in memory behind our back, which is
    # the one thing this function exists to avoid. The chunks are already a
    # megabyte each, so there is nothing for a buffer to win.
    # O_BINARY matters on Windows, where the C runtime would otherwise
    # translate every newline in a package and corrupt it. It does not exist
    # on the appliance's own platform, where there is nothing to translate.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(target, flags, 0o600)
    try:
        try:
            async for chunk in chunks:
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise UploadTooLarge(
                        f"the upload passed {max_bytes} bytes and was abandoned"
                    )
                digest.update(chunk)
                await asyncio.to_thread(_write_all, fd, chunk)
            await asyncio.to_thread(os.fsync, fd)
        finally:
            os.close(fd)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    if size == 0:
        target.unlink(missing_ok=True)
        raise UploadEmpty("the upload carried no bytes")
    return StagedUpload(path=target, sha256=digest.hexdigest(), size=size)


# --------------------------------------------------------------------------
# What is waiting to be applied
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PendingUpdate:
    """A verified package awaiting the admin's decision (§21.24)."""

    package: Path
    version: str
    sha256: str
    size: int
    received_at: str
    manifest: Mapping[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "package": str(self.package),
            "version": self.version,
            "sha256": self.sha256,
            "size": self.size,
            "received_at": self.received_at,
            "manifest": dict(self.manifest),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> PendingUpdate | None:
        try:
            return cls(
                package=Path(str(data["package"])),
                version=str(data["version"]),
                sha256=str(data["sha256"]),
                size=int(data["size"]),
                received_at=str(data["received_at"]),
                manifest=dict(data.get("manifest") or {}),
            )
        except (KeyError, TypeError, ValueError):
            return None


def manifest_json(manifest: Manifest) -> dict[str, Any]:
    """The §21.24 review card: what was in the package, and which key signed it."""
    return {
        "type": manifest.type,
        "version": manifest.version,
        "created_at": manifest.created_at,
        "min_app_version": manifest.min_app_version,
        "description": manifest.description,
        "changes": list(manifest.changes),
        "key_id": manifest.key_id,
        "members": len(manifest.members),
        "payload_bytes": manifest.total_size,
    }


def read_pending(paths: UpdatePaths) -> PendingUpdate | None:
    """The pending record, or ``None`` when its package has gone."""
    try:
        data = json.loads(paths.pending_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    pending = PendingUpdate.from_json(data)
    if pending is None or not pending.package.is_file():
        return None
    return pending


def write_pending(paths: UpdatePaths, pending: PendingUpdate) -> None:
    """Replace the pending record atomically, discarding any earlier package."""
    previous = read_pending(paths)
    path = paths.pending_path()
    tmp = path.with_name(f".{path.name}.tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(pending.to_json(), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    if previous is not None and previous.package != pending.package:
        previous.package.unlink(missing_ok=True)


def clear_pending(paths: UpdatePaths, *, remove_package: bool = True) -> None:
    """Forget the pending package, and delete it unless it is now installed."""
    pending = read_pending(paths)
    paths.pending_path().unlink(missing_ok=True)
    if pending is not None and remove_package:
        pending.package.unlink(missing_ok=True)


def discard_stale_uploads(paths: UpdatePaths) -> int:
    """Remove uploads no pending record names. Returns how many went."""
    pending = read_pending(paths)
    keep = pending.package if pending is not None else None
    removed = 0
    if not paths.tmp_dir.is_dir():
        return 0
    for candidate in paths.tmp_dir.glob(f"{UPLOAD_PREFIX}*.tar"):
        if candidate == keep:
            continue
        with contextlib.suppress(OSError):
            candidate.unlink()
            removed += 1
    return removed


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------


def installed_version(paths: UpdatePaths) -> str | None:
    """The directory name ``current`` resolves to (contracts §1)."""
    return resolve_version(paths.app_dir)


async def verify_upload(
    staged: StagedUpload,
    paths: UpdatePaths,
    *,
    anchors_dir: Path | None = None,
) -> Manifest:
    """Verify a staged package before anything is extracted (contracts §3).

    The downgrade and ``min_app_version`` rules both need to know what is
    installed, and both read the directory name ``current`` points at rather
    than the running code's ``__version__``: those are two different kinds of
    string, and comparing them would make every package either always newer
    or never.
    """
    version = installed_version(paths)
    return await asyncio.to_thread(
        verify_package,
        staged.path,
        expect_type="app",
        anchors_dir=anchors_dir,
        installed_version=version,
        app_version=version,
    )


# --------------------------------------------------------------------------
# The database snapshot
# --------------------------------------------------------------------------


def snapshot_database(source: Path, destination: Path) -> bool:
    """Copy a live SQLite database with the online backup API.

    Returns ``False`` when there is no database yet — a first install — and
    raises :class:`SnapshotFailed` when there is one and it could not be
    copied. The copy is written beside its destination and renamed, so a
    reader never sees a half-written snapshot.
    """
    if not source.exists():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(destination.name + ".tmp")
    tmp.unlink(missing_ok=True)
    try:
        with contextlib.closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as live:
            with contextlib.closing(sqlite3.connect(tmp)) as copy:
                live.backup(copy)
        os.replace(tmp, destination)
    except (sqlite3.Error, OSError) as exc:
        tmp.unlink(missing_ok=True)
        raise SnapshotFailed(f"could not snapshot {source}: {exc}") from exc
    return True


def take_pre_update_snapshot(database: Path, target: Path, *, from_version: str | None) -> bool:
    """§14.2's pre-update snapshot, through the one snapshot helper.

    ``VACUUM INTO`` with a sidecar, like every other pre-change snapshot
    (:mod:`proskenion.core.snapshots`). Nothing is pruned here: §15.3 prunes
    during the nightly job and under disk pressure, and never the snapshot a
    rollback relies on. Returns ``False`` when there is no database yet.
    """
    try:
        info = take_snapshot_sync(
            database,
            target,
            reason=f"update to {target.name.removeprefix(SNAPSHOT_PREFIX).removesuffix('.db')}",
            actor="update",
            app_version=from_version or "unknown",
        )
    except _SnapshotNotTaken as exc:
        raise SnapshotFailed(str(exc)) from exc
    return info is not None


def newest_snapshot(directory: Path) -> Path | None:
    if not directory.is_dir():
        return None
    found = sorted(
        (p for p in directory.glob(f"{SNAPSHOT_PREFIX}*.db") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return found[0] if found else None


def restore_snapshot(snapshot: Path, database: Path) -> None:
    """Put a snapshot back, sidecars and all (§14.3).

    The write-ahead log and shared-memory files belong to the database that
    was replaced; leaving them would let SQLite replay transactions that the
    restored file knows nothing about.
    """
    tmp = database.with_name(database.name + ".restore-tmp")
    with open(snapshot, "rb") as source, open(tmp, "wb") as target:
        shutil.copyfileobj(source, target)
        target.flush()
        os.fsync(target.fileno())
    os.replace(tmp, database)
    for suffix in ("-wal", "-shm"):
        database.with_name(database.name + suffix).unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Version directories
# --------------------------------------------------------------------------


def installed_versions(app_dir: Path) -> list[str]:
    """Version directories, newest first.

    Ordered by the version the directory is named for rather than by its
    modification time: "older ones are pruned" is a statement about versions,
    and a directory's timestamp moves for reasons that have nothing to do with
    which release it holds — a rollback, a restore, a filesystem check.
    Anything not named ``vX.Y.Z`` sorts last and is pruned first, with its
    timestamp as the tiebreak.

    ``is_dir()`` follows symlinks, so ``current`` itself would otherwise
    appear here as a version; a version is a real directory.
    """
    if not app_dir.is_dir():
        return []
    found = [
        path
        for path in app_dir.iterdir()
        if path.is_dir() and not path.is_symlink() and not path.name.startswith(".")
    ]
    found.sort(key=_version_order, reverse=True)
    return [path.name for path in found]


def _version_order(path: Path) -> tuple[int, tuple[int, int, int], float]:
    parsed = parse_version(path.name) if is_version(path.name) else (0, 0, 0)
    return (1 if is_version(path.name) else 0, parsed, path.stat().st_mtime)


def previous_versions(paths: UpdatePaths) -> list[str]:
    """Installed versions that are not the running one, newest first."""
    current = installed_version(paths)
    return [name for name in installed_versions(paths.app_dir) if name != current]


def prune_versions(
    paths: UpdatePaths,
    *,
    keep_previous: int = RETAINED_PREVIOUS_VERSIONS,
    protect: Sequence[str] = (),
) -> list[str]:
    """Retain the current version and the previous two (§14.2). Returns what went.

    ``protect`` names versions that must survive whatever their age — the
    rollback target recorded in ``boot-state.json``, which is the one
    directory the unattended path in §14.5 depends on existing.
    """
    keep = {name for name in protect if name}
    current = installed_version(paths)
    if current:
        keep.add(current)
    removed: list[str] = []
    candidates = [name for name in installed_versions(paths.app_dir) if name not in keep]
    for name in candidates[keep_previous:]:
        directory = paths.app_dir / name
        try:
            shutil.rmtree(directory)
        except OSError as exc:  # pragma: no cover - a directory we cannot remove
            log.warning("could not prune %s: %s", directory, exc)
            continue
        removed.append(name)
    return removed


#: Windows' ``MoveFileEx`` refuses to replace one directory symlink with
#: another, so the same code cannot swap ``current`` there the way it does on
#: the appliance. The non-atomic fallback below exists only so this module
#: develops and tests on a Windows machine; a test pins this to ``False`` to
#: prove the production behaviour without patching ``os.name``, which
#: ``pathlib`` reads directly.
_WINDOWS_NON_ATOMIC_FALLBACK = os.name == "nt"


def swap_current(app_dir: Path, version: str) -> None:
    """§14.2's atomic swap: a symlink beside the target, renamed over it.

    ``ln -sfn`` on its own unlinks before it links, and a process that dies in
    that window leaves an appliance whose ``current`` does not resolve —
    unrecoverable in a school hall. ``rename(2)`` has no such window.

    The target is the bare directory name, not a path, because that is what
    the version is: ``current -> v1.3.0``, resolved relative to ``/data/app``,
    so a tree that is moved or restored elsewhere still resolves.
    """
    tmp = app_dir / f".tmp-{os.getpid()}"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(version, tmp, target_is_directory=True)
    try:
        os.replace(tmp, app_dir / "current")
    except OSError:
        if not _WINDOWS_NON_ATOMIC_FALLBACK:  # pragma: no cover - Windows only
            tmp.unlink(missing_ok=True)
            raise
        current = app_dir / "current"
        if current.is_symlink() or current.exists():
            current.unlink()
        os.rename(tmp, current)


# --------------------------------------------------------------------------
# Preparation and the apply
# --------------------------------------------------------------------------

#: ``(key, message)`` for each of §14.2's steps, in order. The keys are stable
#: and are what an observer keys off; the messages are what §21.24's progress
#: panel shows.
APPLY_STEPS: Final[tuple[tuple[str, str], ...]] = (
    ("extract", "Extracting {version}"),
    ("venv", "Building the Python environment"),
    ("dry_run", "Testing the database migrations"),
    ("snapshot", "Capturing a database snapshot"),
    ("record", "Recording the update"),
    ("swap", "Switching to {version}"),
    ("restart", "Restarting the application"),
)
APPLY_STEP_COUNT: Final = len(APPLY_STEPS)
_STEP_INDEX: Final[Mapping[str, int]] = {
    key: index for index, (key, _message) in enumerate(APPLY_STEPS, start=1)
}

#: ``progress(operation, step, of, message)``.
ProgressSink = Callable[[str, int, int, str], None]

#: ``observer(step_key, phase)`` where phase is ``"start"`` or ``"done"``.
#: The service uses it for logging; the systemd harness uses it to kill the
#: process at a named point and prove the next start still works.
StepObserver = Callable[[str, str], None]

#: Runs before the snapshot is taken, and before a snapshot is restored: the
#: service checkpoints and closes the database so what is copied is settled
#: and what is replaced is not open.
Hook = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class PreparedUpdate:
    """A version extracted, built and proved against a copy of the database."""

    version: str
    directory: Path
    venv: Path
    prepared_at: str
    manifest: Mapping[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "venv": self.venv.name,
            "prepared_at": self.prepared_at,
            "manifest": dict(self.manifest),
        }


@dataclass(frozen=True, slots=True)
class AppliedUpdate:
    """What an apply did, for the audit row and the status endpoint."""

    from_version: str | None
    to_version: str
    snapshot: str | None
    at: str


@dataclass(frozen=True, slots=True)
class RolledBack:
    """What a rollback did."""

    from_version: str
    to_version: str
    snapshot: str | None
    at: str


class UpdateRunner:
    """Carries out §14.2 and §14.3 in order, reporting each step as it goes."""

    def __init__(
        self,
        paths: UpdatePaths,
        *,
        helper: HelperClient | None = None,
        progress: ProgressSink | None = None,
        observer: StepObserver | None = None,
        anchors_dir: Path | None = None,
        python: str | None = None,
        now: Callable[[], str] = iso_now,
        command_timeout_s: float = 900.0,
        watchdog_window_s: int = DEFAULT_WATCHDOG_WINDOW_S,
    ) -> None:
        self.paths = paths
        self._helper = helper
        self._progress = progress
        self._observer = observer
        self._anchors_dir = anchors_dir
        self._python = python or sys.executable
        self._now = now
        self._timeout = command_timeout_s
        self._watchdog_window_s = watchdog_window_s

    def now(self) -> str:
        """This runner's clock, so a caller timestamps the same way it does."""
        return self._now()

    async def verify_from(self, staged: StagedUpload) -> Manifest:
        """Verify a staged upload against this runner's anchors."""
        return await verify_upload(staged, self.paths, anchors_dir=self._anchors_dir)

    # -- reporting ---------------------------------------------------------

    def _start(self, key: str, **fields: str) -> None:
        index = _STEP_INDEX[key]
        message = APPLY_STEPS[index - 1][1].format(**fields)
        if self._progress is not None:
            with contextlib.suppress(Exception):
                self._progress(APPLY_OPERATION, index, APPLY_STEP_COUNT, message)
        self._notify(key, "start")

    def _finish(self, key: str) -> None:
        self._notify(key, "done")

    def _notify(self, key: str, phase: str) -> None:
        if self._observer is None:
            return
        # Deliberately not guarded: the harness's observer ends the process,
        # and an observer that raises is a fault the caller should see.
        self._observer(key, phase)

    # -- steps 1 to 3: nothing committed -----------------------------------

    async def prepare(self, pending: PendingUpdate) -> PreparedUpdate:
        """Extract, build the environment, and test the migrations on a copy.

        A failure leaves the appliance exactly as it was: the destination is
        removed, the snapshot has not been taken, ``current`` has not moved
        and no marker has been written.
        """
        version = pending.version
        destination = self.paths.version_dir(version)
        if installed_version(self.paths) == version:
            raise NotPrepared(f"{version} is already the running version")

        self._start("extract", version=version)
        await asyncio.to_thread(self._extract, pending, destination)
        self._finish("extract")

        try:
            self._start("venv")
            venv = await asyncio.to_thread(self._build_venv, destination)
            self._finish("venv")

            self._start("dry_run")
            await asyncio.to_thread(self._dry_run_migrations, destination, venv)
            self._finish("dry_run")
        except BaseException:
            shutil.rmtree(destination, ignore_errors=True)
            raise

        prepared = PreparedUpdate(
            version=version,
            directory=destination,
            venv=venv,
            prepared_at=self._now(),
            manifest=dict(pending.manifest),
        )
        (destination / PREPARED_FILENAME).write_text(
            json.dumps(prepared.to_json(), indent=2) + "\n", encoding="utf-8"
        )
        log.info("update prepared", extra={"version": version, "directory": str(destination)})
        return prepared

    def _extract(self, pending: PendingUpdate, destination: Path) -> None:
        """Verify again, and write only what the signed manifest names.

        The verification is repeated rather than carried over from the upload:
        the file has been sitting in ``/data/tmp`` in between, and the pass
        that writes is the pass that checks.
        """
        if destination.exists():
            # Only ever a directory an interrupted attempt left behind: a
            # version that is installed would have been refused as a
            # downgrade, and the running version is refused above.
            shutil.rmtree(destination, ignore_errors=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # /data/app, like the version directory extract_verified makes inside
        # it, must be traversable by nginx's workers (www-data), which serve
        # the web interface from there; this process's UMask=0027 would
        # otherwise make a missing one 0750. Nothing in it is secret.
        with contextlib.suppress(OSError):
            destination.parent.chmod(APP_DIR_MODE)
        version = installed_version(self.paths)
        extract_verified(
            pending.package,
            destination,
            expect_type="app",
            anchors_dir=self._anchors_dir,
            installed_version=version,
            app_version=version,
        )
        self._unpack_inner_archive(destination)
        link_bootstrap_config(destination, self.paths.bootstrap_config)

    @staticmethod
    def _unpack_inner_archive(destination: Path) -> None:
        """Expand a payload that is one archive rather than a tree.

        ``tools/package.py build --source`` produces a tree, which needs
        nothing here. A package built the other way carries a single
        ``app.tar*`` at the root; the privileged helper expands that shape
        too, and the two halves have to agree about what an application
        package looks like.
        """
        for name in ("app.tar.zst", "app.tar.gz", "app.tar"):
            archive = destination / name
            if not archive.is_file():
                continue
            if name.endswith(".zst"):
                # Python 3.13 has no zstd module; Debian 13 ships the tool.
                with subprocess.Popen(
                    ["zstd", "-dc", str(archive)], stdout=subprocess.PIPE
                ) as decompress:
                    with tarfile.open(fileobj=decompress.stdout, mode="r|") as inner:
                        inner.extractall(destination, filter="data")
                    if decompress.wait() != 0:
                        raise UpdateError(f"zstd could not decompress {name}")
            else:
                with tarfile.open(archive, mode="r:*") as inner:
                    inner.extractall(destination, filter="data")
            archive.unlink()
            # tarfile creates the directories a member implies under this
            # process's UMask=0027, so web/ and everything under it would be
            # 0750 and nginx's workers could not read it: the same modes as
            # extract_verified gives a tree-shaped package, set the same way.
            for root, directories, files in os.walk(destination):
                for name in directories:
                    with contextlib.suppress(OSError):
                        (Path(root) / name).chmod(0o755)
                for name in files:
                    path = Path(root) / name
                    with contextlib.suppress(OSError):
                        executable = path.stat().st_mode & 0o100
                        path.chmod(0o755 if executable else 0o644)
            return

    def _build_venv(self, destination: Path) -> Path:
        """Build the environment for this interpreter ABI, offline (Q11).

        ``--no-index`` is the point: the appliance sits on a school VLAN with
        restricted internet access, and an update that reached out to an index
        would succeed on the bench and fail in the hall. Every wheel the
        application needs travels in the package.
        """
        wheels = destination / WHEELS_DIRNAME
        found = sorted(wheels.glob("*.whl")) if wheels.is_dir() else []
        if not found:
            raise WheelsMissing(f"{wheels} holds no wheels")
        name = venv_name()
        target = destination / name
        staging = destination / f".{name}.incoming"
        shutil.rmtree(staging, ignore_errors=True)
        self._run([self._python, "-m", "venv", str(staging)], VenvBuildFailed, "venv")
        python = _venv_python(staging)
        self._run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                "--disable-pip-version-check",
                "--find-links",
                str(wheels),
                *[str(wheel) for wheel in found],
            ],
            VenvBuildFailed,
            "wheel install",
        )
        shutil.rmtree(target, ignore_errors=True)
        os.rename(staging, target)
        # `venv` is what the service unit's ExecStart goes through, and it is
        # a symlink so a later interpreter can be given its own environment
        # beside this one rather than over it.
        link = destination / VENV_LINK
        if link.is_symlink() or link.exists():
            link.unlink()
        os.symlink(name, link)
        return target

    def _dry_run_migrations(self, destination: Path, venv: Path) -> None:
        """Run the new version's migrations against a copy of the database (§14.2).

        The copy is taken with the online backup API rather than by copying
        the file, so it is a consistent database rather than whatever was on
        disk mid-transaction. The runner that is asked is the **new**
        version's, in the **new** environment, because that is the one that
        will run at startup and nothing else answers the question.
        """
        self.paths.tmp_dir.mkdir(parents=True, exist_ok=True)
        copy = self.paths.tmp_dir / f"migration-check-{uuid.uuid4()}.db"
        try:
            snapshot_database(self.paths.database, copy)
            python = _venv_python(venv)
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(destination)
            done = subprocess.run(  # noqa: S603 - a path this module built
                [str(python), "-m", "proskenion.db.migrations", "--check", str(copy)],
                capture_output=True,
                text=True,
                cwd=str(destination),
                env=environment,
                timeout=self._timeout,
                check=False,
            )
            if done.returncode != 0:
                detail = (done.stderr or done.stdout or "").strip()[-2000:]
                raise MigrationDryRunFailed(
                    f"the migrations of {destination.name} failed on a copy "
                    f"(exit {done.returncode}): {detail}"
                )
        except subprocess.SubprocessError as exc:
            raise MigrationDryRunFailed(f"the migration check could not run: {exc}") from exc
        finally:
            copy.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                copy.with_name(copy.name + suffix).unlink(missing_ok=True)

    def _run(self, argv: Sequence[str], failure: type[UpdateError], what: str) -> None:
        try:
            done = subprocess.run(  # noqa: S603 - argv is built here, never from input
                list(argv),
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise failure(f"{what} could not run: {exc}") from exc
        if done.returncode != 0:
            detail = (done.stderr or done.stdout or "").strip()[-2000:]
            raise failure(f"{what} failed (exit {done.returncode}): {detail}")

    # -- steps 4 to 7: the commit ------------------------------------------

    async def commit(
        self,
        prepared: PreparedUpdate,
        *,
        before_snapshot: Hook | None = None,
    ) -> AppliedUpdate:
        """Snapshot, record, swap and restart — §14.2's second half, in order."""
        version = prepared.version
        previous = installed_version(self.paths)
        store = self.paths.store()

        self._start("snapshot")
        if before_snapshot is not None:
            await before_snapshot()
        snapshot = self.paths.snapshot_path(version)
        took = await asyncio.to_thread(
            take_pre_update_snapshot, self.paths.database, snapshot, from_version=previous
        )
        self._finish("snapshot")

        at = self._now()
        self._start("record")
        await store.update(
            update={
                "from": previous,
                "to": version,
                "snapshot": str(snapshot) if took else None,
                "at": at,
            }
        )
        self._finish("record")

        self._start("swap", version=version)
        await asyncio.to_thread(swap_current, self.paths.app_dir, version)
        self._finish("swap")

        self._start("restart")
        await asyncio.to_thread(
            prune_versions, self.paths, protect=[previous] if previous else []
        )
        await self._restart(watchdog_window_s=self._watchdog_window_s)
        self._finish("restart")
        log.info(
            "update applied", extra={"from": previous, "to": version, "snapshot": str(snapshot)}
        )
        return AppliedUpdate(
            from_version=previous,
            to_version=version,
            snapshot=str(snapshot) if took else None,
            at=at,
        )

    async def _restart(self, *, watchdog_window_s: int | None = None) -> None:
        """Ask the helper to restart the application (contracts §2).

        ``settle="running"``, inside :meth:`~HelperClient.restart_core`:
        the restart kills this process, so the status file's final line is
        written to a reader that no longer exists, and waiting for it would
        mean waiting out the timeout every single time.

        An apply passes ``watchdog_window_s`` and a rollback does not. §14.5
        widens the window for a version that has never run on this machine
        before, so that a slow first start is not mistaken for a hang; a
        rollback goes back to a version that has already run here, and giving
        it extra slack would only delay the watchdog noticing if it hangs.
        """
        if self._helper is None:
            return
        try:
            await self._helper.restart_core(watchdog_window_s=watchdog_window_s)
        except HelperError as exc:
            raise RestartRefused(str(exc)) from exc

    # -- §14.3 --------------------------------------------------------------

    async def roll_back(
        self,
        *,
        to: str | None = None,
        before_restore: Hook | None = None,
    ) -> RolledBack:
        """Repoint the symlink, then restore the snapshot — in that order.

        The intuitive order is the wrong one. Restoring the data first leaves
        new code against an old schema: it migrates forward at startup and
        silently undoes the rollback. Repointing first leaves old code against
        a new schema, which §15.2's schema-ahead guard refuses to start on —
        loud, and recoverable by the service that is already retrying.
        """
        current = installed_version(self.paths)
        if current is None:
            raise NothingToRollBackTo(f"{self.paths.current} does not resolve to a version")
        state = await self.paths.store().read()
        record = dict(state.update or {})
        target = to or _rollback_target(self.paths, record, current)
        if target is None:
            raise NothingToRollBackTo("no previous version directory is installed")
        directory = self.paths.version_dir(target)
        if target == current or not directory.is_dir() or directory.is_symlink():
            raise NothingToRollBackTo(f"{target} is not an installed previous version")

        snapshot = _rollback_snapshot(self.paths, record, current)
        at = self._now()
        await asyncio.to_thread(swap_current, self.paths.app_dir, target)
        if snapshot is not None:
            if before_restore is not None:
                await before_restore()
            await asyncio.to_thread(restore_snapshot, snapshot, self.paths.database)
        else:
            log.warning(
                "no pre-update snapshot for %s; the code is rolled back and the "
                "schema-ahead guard will report the rest",
                current,
            )
        # The update record described a version that is no longer running, so
        # it must not be left for the unattended path to act on (contracts §1).
        await self.paths.store().clear("update")
        await self._restart()
        log.info("rolled back", extra={"from": current, "to": target})
        return RolledBack(
            from_version=current,
            to_version=target,
            snapshot=str(snapshot) if snapshot else None,
            at=at,
        )


def _rollback_target(paths: UpdatePaths, record: Mapping[str, Any], current: str) -> str | None:
    """The version to go back to, most trustworthy source first.

    The recorded ``update.from`` is the answer whenever it is still installed,
    because it is what this version replaced. Failing that, the highest
    installed version **below** the running one — never above it. An
    interrupted apply leaves a newer directory extracted and unused beside the
    running version, and "roll back" must not mean "switch to the version that
    was never finished".
    """
    recorded = record.get("from")
    if isinstance(recorded, str) and recorded and recorded != current:
        directory = paths.version_dir(recorded)
        if directory.is_dir() and not directory.is_symlink():
            return recorded
    if not is_version(current):
        return None
    here = parse_version(current)
    for name in previous_versions(paths):
        if is_version(name) and parse_version(name) < here:
            return name
    return None


def _rollback_snapshot(
    paths: UpdatePaths, record: Mapping[str, Any], current: str
) -> Path | None:
    """The snapshot taken before ``current`` was installed, if it is still there."""
    if record.get("to") == current:
        recorded = record.get("snapshot")
        if isinstance(recorded, str) and recorded and Path(recorded).is_file():
            return Path(recorded)
    by_name = paths.snapshot_path(current)
    if by_name.is_file():
        return by_name
    return newest_snapshot(paths.snapshots_dir)


def _venv_python(venv: Path) -> Path:
    """The interpreter inside an environment, on either layout."""
    posix = venv / "bin" / "python"
    return posix if posix.exists() else venv / "Scripts" / "python.exe"


# --------------------------------------------------------------------------
# "The next quiet moment" (§14.2, Q17)
# --------------------------------------------------------------------------

#: The nightly window the backup job, the log prune and the firewall
#: re-resolution all run in. An update that restarted the application in the
#: middle of it would compete with the one job that must not be interrupted.
NIGHTLY_WINDOW: Final[tuple[tuple[int, int], tuple[int, int]]] = ((2, 30), (3, 30))

#: How long the appliance must have had no staff or hirer socket.
QUIET_IDLE_S: Final = 600.0

#: Q17: the quiet check runs once a minute, not continuously.
QUIET_INTERVAL_S: Final = 60.0

#: Every condition, in the order the interface lists them. Naming them makes
#: "why has it not applied yet" answerable rather than a shrug.
QUIET_CONDITIONS: Final[tuple[str, ...]] = (
    "hirer_access_disabled",
    "no_scene_running",
    "no_recent_connection",
    "outside_nightly_window",
)


@dataclass(frozen=True, slots=True)
class QuietReport:
    """Each of Q17's four conditions, and whether all of them hold."""

    hirer_access_disabled: bool
    no_scene_running: bool
    no_recent_connection: bool
    outside_nightly_window: bool

    @property
    def quiet(self) -> bool:
        return all(getattr(self, name) for name in QUIET_CONDITIONS)

    def blocking(self) -> list[str]:
        """The conditions that do not hold, in :data:`QUIET_CONDITIONS` order."""
        return [name for name in QUIET_CONDITIONS if not getattr(self, name)]

    def to_json(self) -> dict[str, Any]:
        body: dict[str, Any] = {name: getattr(self, name) for name in QUIET_CONDITIONS}
        body["quiet"] = self.quiet
        return body


def outside_nightly_window(when: datetime) -> bool:
    """Whether ``when`` falls outside 02:30–03:30 local time (Q17)."""
    (from_hour, from_minute), (to_hour, to_minute) = NIGHTLY_WINDOW
    minutes = when.hour * 60 + when.minute
    return not (from_hour * 60 + from_minute <= minutes < to_hour * 60 + to_minute)


def evaluate_quiet(
    *,
    hirer_enabled: bool,
    scenes_running: int,
    seconds_since_connection: float,
    when: datetime,
    idle_s: float = QUIET_IDLE_S,
) -> QuietReport:
    """Q17's definition, all four conditions at once.

    ``seconds_since_connection`` is time since the last staff or hirer socket
    was open — not since the last message. A page left open on the booth
    tablet is a person who may come back to it, which is exactly the case this
    is defending.
    """
    return QuietReport(
        hirer_access_disabled=not hirer_enabled,
        no_scene_running=scenes_running == 0,
        no_recent_connection=seconds_since_connection >= idle_s,
        outside_nightly_window=outside_nightly_window(when),
    )


# --------------------------------------------------------------------------
# Driving the sequence from a plain interpreter
# --------------------------------------------------------------------------


def _observer_that_dies(after: str) -> StepObserver:
    """Kill this process the instant ``after`` completes.

    ``SIGKILL`` rather than an exit: an update must survive the power being
    pulled, and anything that runs on the way out — a flush, an ``atexit``
    hook, an exception handler tidying up — would be testing a kinder failure
    than the one the appliance has to withstand.
    """

    def observe(key: str, phase: str) -> None:
        if key == after and phase == "done":
            os.kill(os.getpid(), 9)

    return observe


async def _main(argv: Sequence[str]) -> int:  # pragma: no cover - driven by the harness
    import argparse

    parser = argparse.ArgumentParser(
        prog="proskenion.core.update",
        description="Apply a verified application package (§14.2), for the appliance harness.",
    )
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--anchors", type=Path, default=None)
    parser.add_argument(
        "--kill-after",
        choices=[key for key, _ in APPLY_STEPS],
        help="SIGKILL this process once the named step has finished",
    )
    parser.add_argument("--prepare-only", action="store_true")
    options = parser.parse_args(list(argv))

    paths = UpdatePaths.for_appliance(
        options.data_dir, options.state_dir, options.database
    )
    helper = HelperClient(options.data_dir)
    runner = UpdateRunner(
        paths,
        helper=helper,
        anchors_dir=options.anchors,
        observer=_observer_that_dies(options.kill_after) if options.kill_after else None,
        progress=lambda operation, step, of, message: print(
            f"{operation} {step}/{of} {message}", flush=True
        ),
    )
    pending = PendingUpdate(
        package=options.package,
        version=options.version,
        sha256="",
        size=options.package.stat().st_size,
        received_at=iso_now(),
        manifest={},
    )
    try:
        prepared = await runner.prepare(pending)
        if not options.prepare_only:
            await runner.commit(prepared)
    except (UpdateError, PackageError) as exc:
        print(f"update failed [{exc.rule}]: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - driven by the harness
    start = time.monotonic()
    code = asyncio.run(_main(sys.argv[1:]))
    log.debug("apply finished in %.1f s", time.monotonic() - start)
    raise SystemExit(code)
