"""The room the Phase 6 milestone is attacked in (spec §18, ``docs/plans/phase-6.md``).

Phase 5's room was a hired auditorium. This one is the cupboard the appliance
lives in: a ``/data``, a ``/srv/appliance``, a boot partition with two slot
trees, a trust anchor directory on a read-only root, and a privileged helper
that is the real script from ``appliance/bin``. Nothing here is a mock of the
mechanism under test — the packages are built and signed by ``tools/package.py``,
the verifier is :mod:`proskenion.core.packages`, and the helper's request
validation and verb handlers are the ones that run as root on the appliance.

Three things are stood in for, each deliberately:

* **Block devices.** A root slot is a partition, and writing one needs a loop
  device and privilege. Where a property needs real media it is proved in
  ``appliance/tests/systemd-cases.sh`` instead, which runs against systemd as
  PID 1 with loop devices for both slots. What is proved here is everything
  that happens *before* a device is opened, which is where every refusal is.
* **Where the root image's verifier is found.**
  :mod:`auditorium_packages` will only run a verifier from the read-only root
  (``/usr/local/lib``), which no developer machine has.
  :func:`trust_the_checkouts_verifier` puts the checkout's copy in its place
  for the duration of a test. The decision it bypasses — *which* copy of the
  verifier the helper may run — is a separate property with its own tests in
  ``tests/unit/appliance/test_helper.py``; the verification itself, which is
  what the tests here attack, is entirely real.
* **systemd.** The helper shells out to ``systemctl``; :class:`FakeSystemctl`
  records the argv instead. A restart really restarting is the systemd
  harness's property.

A note on the hostile package corpus. Every member of it starts life as a
genuine signed package and is then damaged in exactly one way, so a refusal is
attributable: "this was refused because the signature no longer covers the
manifest" rather than "this tar was never valid to begin with".
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from proskenion.core import packages as core_packages
from proskenion.core.update import StagedUpload, UpdatePaths, swap_current
from tests.package_factory import (
    Signing,
    build_package,
    make_os_source,
    make_signing,
    make_source,
)

REPOSITORY = Path(__file__).resolve().parents[2]
APPLIANCE = REPOSITORY / "appliance"
APPLIANCE_LIB = APPLIANCE / "lib"
APPLIANCE_BIN = APPLIANCE / "bin"

#: A fixed request id, so a status file is easy to find by hand in a failure.
REQUEST_ID = "0c2a4f61-1111-4000-8000-000000000001"


# --------------------------------------------------------------------------
# The appliance's own root-side scripts
# --------------------------------------------------------------------------


def load_appliance_script(name: str, module_name: str) -> ModuleType:
    """Import a suffix-less script from ``appliance/bin`` by path.

    The same loader ``tests/unit/appliance/conftest.py`` uses, repeated rather
    than imported because a conftest is not importable from another package.
    Bytecode writing is turned off for the duration: the image build installs
    ``appliance/bin`` wholesale and a ``__pycache__`` left beside the scripts
    is a broken install.
    """
    if module_name in sys.modules:
        return sys.modules[module_name]
    if str(APPLIANCE_LIB) not in sys.path:
        sys.path.insert(0, str(APPLIANCE_LIB))
    loader = importlib.machinery.SourceFileLoader(module_name, str(APPLIANCE_BIN / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    written = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = written
    return module


def load_appliance_lib(name: str) -> ModuleType:
    """Import one of ``appliance/lib``'s modules the way the scripts do."""
    if str(APPLIANCE_LIB) not in sys.path:
        sys.path.insert(0, str(APPLIANCE_LIB))
    written = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        return importlib.import_module(name)
    finally:
        sys.dont_write_bytecode = written


@contextmanager
def helper_confined_to(helper: ModuleType, roots: tuple[Path, ...]) -> Iterator[None]:
    """Point the helper's two confinement roots at this test's directories.

    ``CONFINED_ROOTS`` is ``("/data/tmp", "/srv/local")`` — absolute appliance
    paths, so off the appliance no path argument can ever be accepted and no
    handler that takes one can be reached. What is substituted is the pair of
    roots, never the rule: a path must still be absolute, must still contain
    no ``..``, must still be strictly inside one of them, and must still be
    opened with ``O_NOFOLLOW`` at every component. The literal values are
    asserted against the contract in ``tests/unit/appliance/test_helper.py``.
    """
    original = helper.CONFINED_ROOTS
    helper.CONFINED_ROOTS = tuple(root.as_posix() for root in roots)
    try:
        yield
    finally:
        helper.CONFINED_ROOTS = original


@contextmanager
def trust_the_checkouts_verifier(anchors: Path, image_anchors: Path) -> Iterator[None]:
    """Let the helper's verification run, against ``anchors``, off the appliance.

    Two substitutions, both restored on the way out:

    * :func:`auditorium_packages._from_installed_module` normally refuses a
      ``proskenion.core.packages`` that does not resolve under ``/usr/local/lib``
      — which is the whole point on the appliance, and means it finds nothing
      in a checkout. It is replaced with one that returns the checkout's copy.
    * the verifier's two anchor directories are absolute paths on the
      appliance's read-only root. They are pointed at the test's anchors, which
      is the only way a package signed by a test key can verify at all.
    """
    auditorium_packages = load_appliance_lib("auditorium_packages")
    original_loader = auditorium_packages._from_installed_module
    original_trusted = core_packages.TRUSTED_KEYS_DIR
    original_image = core_packages.IMAGE_KEYS_DIR
    auditorium_packages._from_installed_module = lambda: core_packages
    core_packages.TRUSTED_KEYS_DIR = anchors  # type: ignore[misc]
    core_packages.IMAGE_KEYS_DIR = image_anchors  # type: ignore[misc]
    try:
        yield
    finally:
        auditorium_packages._from_installed_module = original_loader
        core_packages.TRUSTED_KEYS_DIR = original_trusted  # type: ignore[misc]
        core_packages.IMAGE_KEYS_DIR = original_image  # type: ignore[misc]


# --------------------------------------------------------------------------
# systemd, recorded rather than run
# --------------------------------------------------------------------------


class FakeSystemctl:
    """Records every command the helper runs, and answers what it asks for."""

    def __init__(self, active_state: str = "active") -> None:
        self.argv: list[list[str]] = []
        self.active_state = active_state

    def __call__(self, argv: list[str] | tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        self.argv.append(list(argv))
        if "ActiveState" in argv:
            return subprocess.CompletedProcess(list(argv), 0, self.active_state + "\n", "")
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    @property
    def commands(self) -> list[str]:
        return [" ".join(argv) for argv in self.argv]


# --------------------------------------------------------------------------
# The appliance's filesystem
# --------------------------------------------------------------------------


@dataclass
class Appliance:
    """A ``/data``, a ``/srv/appliance``, a boot partition and a trust anchor
    directory, laid out the way §2.3 and contracts §1 say.

    ``/data/tmp`` and ``/srv/local`` are the two directories the helper will
    accept a path argument inside (contracts §2), so they are created here;
    everything the tests hand it lives in one of them.
    """

    root: Path
    signing: Signing
    image_signing: Signing
    #: The name the image key was generated under, which is also its anchor's
    #: filename. Only this machine holds the private half (Q13).
    image_signing_name: str = "this-machine"

    data: Path = field(init=False)
    state: Path = field(init=False)
    boot: Path = field(init=False)
    local: Path = field(init=False)
    tmp: Path = field(init=False)
    paths: UpdatePaths = field(init=False)

    def __post_init__(self) -> None:
        self.data = self.root / "data"
        self.state = self.root / "srv-appliance"
        self.boot = self.root / "boot"
        self.local = self.root / "srv-local"
        self.tmp = self.data / "tmp"
        for directory in (
            self.tmp,
            self.local,
            self.state,
            self.data / "run" / "helper",
            self.data / "config",
            self.data / "backups" / "snapshots",
            self.boot / "slot-a",
            self.boot / "slot-b",
        ):
            directory.mkdir(parents=True, exist_ok=True)
        (self.data / "app" / "v1.2.0").mkdir(parents=True, exist_ok=True)
        swap_current(self.data / "app", "v1.2.0")
        (self.boot / "config.txt").write_text(
            "arm_64bit=1\nos_prefix=slot-a/\n", encoding="utf-8"
        )
        self.set_slot_version("a", "v1.0.0")
        self.paths = UpdatePaths.for_appliance(
            self.data, self.state, self.data / "auditorium.db"
        )
        self.write_boot_state(
            {
                "active_slot": "a",
                "last_known_good": "a",
                "staged": None,
                "slots": {"a": "5a1b2c3d-02", "b": "5a1b2c3d-03"},
            }
        )

    # -- the boot partition ------------------------------------------------

    def set_slot_version(self, slot: str, version: str | None) -> None:
        path = self.boot / f"slot-{slot}" / "os-version.txt"
        if version is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(version + "\n", encoding="utf-8")

    def boot_tree_fingerprint(self) -> dict[str, str]:
        """Every file under the boot partition, by SHA-256.

        The assertion "nothing unverified reached the boot partition" is this
        mapping being unchanged, rather than one file being checked: a write
        that landed somewhere unexpected is exactly the failure worth catching.
        """
        found: dict[str, str] = {}
        for path in sorted(self.boot.rglob("*")):
            if path.is_file():
                found[str(path.relative_to(self.boot).as_posix())] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
        return found

    # -- boot-state.json (contracts §1) ------------------------------------

    def write_boot_state(self, document: dict[str, Any]) -> None:
        (self.state / "boot-state.json").write_text(
            json.dumps(document, indent=2) + "\n", encoding="utf-8"
        )

    def boot_state(self) -> dict[str, Any]:
        return json.loads((self.state / "boot-state.json").read_text(encoding="utf-8"))

    def merge_boot_state(self, patch: dict[str, Any]) -> None:
        document = self.boot_state()
        document.update(patch)
        self.write_boot_state(document)

    # -- packages ----------------------------------------------------------

    def app_package(self, version: str = "v1.3.0", **kwargs: Any) -> Path:
        return build_package(
            self.root,
            version,
            self.signing,
            source=make_source(self.root, version),
            **kwargs,
        )

    def os_package(self, version: str = "v2.0.0", **kwargs: Any) -> Path:
        return build_package(
            self.root,
            version,
            self.signing,
            source=make_os_source(self.root, version),
            package_type="os",
            **kwargs,
        )

    def image_package(self, version: str = "v2.0.0") -> Path:
        """A system image, signed by *this machine's* key rather than the
        release key (Q13) — the distinction the two anchor directories exist
        for, and the one a restore rests on."""
        return build_package(
            self.root,
            version,
            self.image_signing,
            source=make_os_source(self.root, f"image-{version}"),
            package_type="image",
            key_name=self.image_signing_name,
        )

    def staged(self, package: Path, *, name: str | None = None) -> StagedUpload:
        """Copy a package into ``/data/tmp`` as an upload, hashed as it lands."""
        body = package.read_bytes()
        target = self.tmp / (name or f"upload-{uuid.uuid4()}.tar")
        target.write_bytes(body)
        return StagedUpload(
            path=target, sha256=hashlib.sha256(body).hexdigest(), size=len(body)
        )

    def manifest_beside(self, package: Path, *, name: str | None = None) -> Path:
        """The package's signed manifest, as ``write-slot`` is handed it."""
        with tarfile.open(package, mode="r:*") as archive:
            member = archive.extractfile("manifest.json")
            assert member is not None
            data = member.read()
        target = self.tmp / (name or f"manifest-{uuid.uuid4()}.json")
        target.write_bytes(data)
        return target


def make_appliance(root: Path) -> Appliance:
    """An appliance with a release key and a machine image key, both real."""
    release = make_signing(root / "release")
    image = make_signing(root / "image-keys", name="this-machine")
    return Appliance(root=root, signing=release, image_signing=image)


# --------------------------------------------------------------------------
# Driving the helper
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HelperOutcome:
    """What one request did: the status file, and every command that ran."""

    state: str
    error: str | None
    step: int
    of: int
    commands: list[str]

    @property
    def failed(self) -> bool:
        return self.state == "failed"


def run_helper(
    helper: ModuleType,
    appliance: Appliance,
    verb: str,
    args: dict[str, Any] | None = None,
    *,
    request_id: str = REQUEST_ID,
    requested_at: str | None = None,
    systemctl: FakeSystemctl | None = None,
    context: dict[str, Any] | None = None,
) -> HelperOutcome:
    """Write a request, validate it and run its handler, as the root unit does.

    This is ``auditorium-helper``'s own ``read_request`` → ``handle`` pair with
    a :class:`Context` whose paths are the test's. It is not ``dispatch()``,
    which builds a context pointing at ``/data`` and ``/boot/firmware``: the
    handlers take their paths from the context precisely so they can be aimed
    somewhere else, and pointing them at the real ones would be a test that
    rewrote the machine it ran on.
    """
    directory = appliance.data / "run" / "helper"
    body = {
        "verb": verb,
        "id": request_id,
        "requested_at": requested_at
        or dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "args": args or {},
    }
    path = directory / f"{request_id}.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    os.chmod(path, 0o600)

    runner = systemctl if systemctl is not None else FakeSystemctl()
    try:
        request = helper.read_request(path, now=dt.datetime.now().astimezone())
    except helper.Refused as exc:
        return HelperOutcome(
            state="failed", error=str(exc), step=0, of=1, commands=runner.commands
        )

    verb_spec = helper.VERBS[request.verb]
    status = helper.Status(directory, request_id, of=verb_spec.steps)
    status.write("running", step=0, message=f"Starting {verb}")
    ctx = helper.Context(
        status=status,
        request=request,
        boot_state=appliance.state / "boot-state.json",
        app_dir=appliance.data / "app",
        db=appliance.data / "auditorium.db",
        snapshots=appliance.data / "backups" / "snapshots",
        dropin_dir=appliance.root / "run-dropin",
        watchdog_suspend_s=0.0,
        boot_dir=appliance.boot,
        by_partuuid=appliance.root / "by-partuuid",
        proc_cmdline=appliance.root / "proc-cmdline",
        slot_mount=appliance.root / "slot-mount",
        partitions_env=appliance.root / "partitions.env",
        host_fstab=appliance.root / "host-fstab",
        # Never the machine running the tests: absent here, so a slot keeps
        # its own machine-id and nothing depends on the test host.
        host_machine_id=appliance.root / "host-machine-id",
        run=runner,
        **(context or {}),
    )
    try:
        assert verb_spec.handler is not None
        verb_spec.handler(ctx)
    except (helper.Refused, helper.Failed) as exc:
        status.write("failed", message=f"{verb} failed", error=str(exc))
    else:
        status.write("done", step=verb_spec.steps, message=f"{verb} complete")

    settled = json.loads(
        (directory / f"{request_id}.status.json").read_text(encoding="utf-8")
    )
    return HelperOutcome(
        state=settled["state"],
        error=settled["error"],
        step=settled["step"],
        of=settled["of"],
        commands=runner.commands,
    )


# --------------------------------------------------------------------------
# The hostile package corpus
# --------------------------------------------------------------------------


def _members(package: Path) -> list[tuple[tarfile.TarInfo, bytes | None]]:
    found: list[tuple[tarfile.TarInfo, bytes | None]] = []
    with tarfile.open(package, mode="r:") as archive:
        for info in archive.getmembers():
            handle = archive.extractfile(info)
            found.append((info, handle.read() if handle is not None else None))
    return found


def _repack(
    destination: Path, members: list[tuple[tarfile.TarInfo, bytes | None]]
) -> Path:
    with tarfile.open(destination, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for info, payload in members:
            if payload is None:
                tar.addfile(info)
            else:
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
    return destination


def tamper_with_a_member(package: Path, destination: Path) -> Path:
    """One payload byte changed. The manifest still says what it said."""
    members = _members(package)
    for index, (info, payload) in enumerate(members):
        if info.name.startswith("payload/") and payload:
            members[index] = (info, bytes([payload[0] ^ 0xFF]) + payload[1:])
            break
    else:  # pragma: no cover - every package built here carries a payload
        raise AssertionError(f"{package} carries no payload member to damage")
    return _repack(destination, members)


def add_an_unlisted_member(package: Path, destination: Path) -> Path:
    """A file the signed manifest does not name, appended to the payload."""
    members = _members(package)
    info = tarfile.TarInfo("payload/extra.py")
    info.mode = 0o644
    info.type = tarfile.REGTYPE
    members.append((info, b"# not in the manifest\n"))
    return _repack(destination, members)


def add_a_traversing_member(package: Path, destination: Path) -> Path:
    """A member whose path climbs out of ``payload/``."""
    members = _members(package)
    info = tarfile.TarInfo("payload/../../etc/cron.d/backdoor")
    info.mode = 0o644
    info.type = tarfile.REGTYPE
    members.append((info, b"* * * * * root id\n"))
    return _repack(destination, members)


def add_a_symlink_member(package: Path, destination: Path) -> Path:
    """A symlink aimed at the read-only root's trust anchors."""
    members = _members(package)
    info = tarfile.TarInfo("payload/anchors")
    info.type = tarfile.SYMTYPE
    info.linkname = "/usr/local/share/auditorium/trusted-keys"
    members.append((info, None))
    return _repack(destination, members)


def add_a_hardlink_member(package: Path, destination: Path) -> Path:
    members = _members(package)
    info = tarfile.TarInfo("payload/hardlink")
    info.type = tarfile.LNKTYPE
    info.linkname = "payload/VERSION"
    members.append((info, None))
    return _repack(destination, members)


def add_a_device_member(package: Path, destination: Path) -> Path:
    members = _members(package)
    info = tarfile.TarInfo("payload/console")
    info.type = tarfile.CHRTYPE
    info.devmajor = 5
    info.devminor = 1
    members.append((info, None))
    return _repack(destination, members)


def swap_the_declared_type(package: Path, destination: Path, new_type: str) -> Path:
    """Rewrite the manifest's ``type`` and leave the signature alone.

    The payload is untouched, so what this produces is a package whose signed
    document says one thing and whose bytes say another — and whose signature
    therefore no longer covers the document it carries.
    """
    members = _members(package)
    for index, (info, payload) in enumerate(members):
        if info.name == core_packages.MANIFEST_NAME and payload is not None:
            document = json.loads(payload)
            document["type"] = new_type
            members[index] = (info, core_packages.canonical_manifest_bytes(document))
            break
    return _repack(destination, members)


def strip_the_signature(package: Path, destination: Path) -> Path:
    """What continuous integration produces: a package nobody signed (§22.8)."""
    members = [
        (info, payload)
        for info, payload in _members(package)
        if info.name != core_packages.SIGNATURE_NAME
    ]
    return _repack(destination, members)


def corrupt_the_signature(package: Path, destination: Path) -> Path:
    members = _members(package)
    for index, (info, payload) in enumerate(members):
        if info.name == core_packages.SIGNATURE_NAME and payload is not None:
            members[index] = (info, bytes([payload[0] ^ 0x01]) + payload[1:])
            break
    return _repack(destination, members)


def append_data_after_the_archive(package: Path, destination: Path) -> Path:
    """A member appended past the tar's end, invisible to a stopping reader."""
    destination.write_bytes(package.read_bytes() + b"A" * (128 << 10))
    return destination


def put_the_manifest_second(package: Path, destination: Path) -> Path:
    """The signature first and the manifest second: a layout a streaming
    verifier could not check in order."""
    members = _members(package)
    members[0], members[1] = members[1], members[0]
    return _repack(destination, members)


def compress_the_container(package: Path, destination: Path) -> Path:
    """A gzipped package: a container that would have to be expanded to be read."""
    import gzip

    destination.write_bytes(gzip.compress(package.read_bytes()))
    return destination


#: Every way a package is made hostile, as ``(name, builder)``. Each damages a
#: genuine signed package in exactly one way, so a refusal names one cause.
HOSTILE_PACKAGES: tuple[tuple[str, Callable[[Path, Path], Path]], ...] = (
    ("a tampered payload member", tamper_with_a_member),
    ("a member the manifest does not list", add_an_unlisted_member),
    ("a member whose path escapes payload/", add_a_traversing_member),
    ("a symlink member", add_a_symlink_member),
    ("a hardlink member", add_a_hardlink_member),
    ("a device node member", add_a_device_member),
    ("no signature at all", strip_the_signature),
    ("a signature that does not verify", corrupt_the_signature),
    ("data appended after the archive", append_data_after_the_archive),
    ("the manifest and its signature transposed", put_the_manifest_second),
    ("a compressed container", compress_the_container),
)
