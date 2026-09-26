"""The update engine: staging, verification, the ordered apply, and rollback (§14.2–§14.5).

The property under test throughout is **ordering**. An update runs unattended
in a school hall, so what matters is not that the happy path works but that
every point at which the process can stop leaves an appliance that still
starts — on the old version or the new one, never on neither.

The packages here are real: built and signed with ``tools/package.py``,
verified with the appliance's own verifier against a real Ed25519 anchor. A
stub package would only prove that the update process works on stub packages.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import sys
from collections.abc import AsyncIterator, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from proskenion.core import update as update_module
from proskenion.core.packages import (
    DowngradeRefused,
    MemberMismatch,
    PackageTypeMismatch,
    SignatureInvalid,
)
from proskenion.core.snapshots import read_sidecar
from proskenion.core.update import (
    APPLY_STEPS,
    MigrationDryRunFailed,
    NothingToRollBackTo,
    PendingUpdate,
    UpdatePaths,
    UpdateRunner,
    UploadEmpty,
    UploadTooLarge,
    WheelsMissing,
    clear_pending,
    discard_stale_uploads,
    evaluate_quiet,
    installed_version,
    installed_versions,
    outside_nightly_window,
    previous_versions,
    prune_versions,
    read_pending,
    restore_snapshot,
    snapshot_database,
    stage_upload,
    swap_current,
    take_pre_update_snapshot,
    venv_name,
    verify_upload,
    write_pending,
)
from tests.package_factory import Signing, build_package, make_signing, make_source

AUCKLAND = ZoneInfo("Pacific/Auckland")

#: A ``proskenion.db.migrations`` the new version can be asked to run. The
#: seam under test is that the **new** version's runner, in the **new**
#: environment, is what answers — not what this repository happens to ship.
MIGRATION_OK = """
import sys
if "--check" not in sys.argv:
    raise SystemExit(64)
print('{"ok": true}')
raise SystemExit(0)
"""

MIGRATION_FAILS = """
import sys
print("003_add_column.sql failed: no such table: scenes", file=sys.stderr)
raise SystemExit(2)
"""


def _tree(version: str, migration: str = MIGRATION_OK) -> dict[str, str]:
    return {
        "proskenion/db/__init__.py": "",
        "proskenion/db/migrations.py": migration,
    }


def _link_target(link: Path) -> Path:
    """Where a symlink points, without the extended-path prefix Windows adds."""
    return Path(os.readlink(link).removeprefix("\\\\?\\"))


class Appliance:
    """A /data and /srv/appliance laid out the way the image builder leaves them."""

    def __init__(self, root: Path, signing: Signing) -> None:
        self.root = root
        self.signing = signing
        self.data = root / "data"
        self.state = root / "srv-appliance"
        (self.data / "app").mkdir(parents=True)
        (self.data / "tmp").mkdir(parents=True)
        self.state.mkdir(parents=True)
        self.paths = UpdatePaths.for_appliance(
            self.data, self.state, self.data / "auditorium.db"
        )
        self.install("v1.2.0")
        self.write_database({"assembly", "concert"})

    # -- set-up helpers ----------------------------------------------------

    def install(self, version: str, *, make_current: bool = True) -> Path:
        directory = self.data / "app" / version
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "VERSION").write_text(version + "\n", encoding="utf-8")
        if make_current:
            swap_current(self.data / "app", version)
        return directory

    def write_database(self, scenes: set[str]) -> None:
        connection = sqlite3.connect(self.paths.database)
        try:
            connection.execute("CREATE TABLE IF NOT EXISTS scenes (name TEXT)")
            connection.execute("DELETE FROM scenes")
            connection.executemany(
                "INSERT INTO scenes (name) VALUES (?)", [(name,) for name in sorted(scenes)]
            )
            connection.commit()
        finally:
            connection.close()

    def scenes(self) -> set[str]:
        connection = sqlite3.connect(self.paths.database)
        try:
            return {row[0] for row in connection.execute("SELECT name FROM scenes")}
        finally:
            connection.close()

    # -- packages ----------------------------------------------------------

    def package(self, version: str, **kwargs: Any) -> Path:
        source = kwargs.pop("source", None)
        if source is None:
            source = make_source(self.root, version, extra=_tree(version))
        return build_package(self.root, version, self.signing, source=source, **kwargs)

    def pending_for(self, package: Path, version: str) -> PendingUpdate:
        return PendingUpdate(
            package=package,
            version=version,
            sha256="",
            size=package.stat().st_size,
            received_at="2026-09-20T19:00:00+12:00",
            manifest={},
        )

    def runner(self, **kwargs: Any) -> UpdateRunner:
        kwargs.setdefault("anchors_dir", self.signing.anchors)
        return UpdateRunner(self.paths, **kwargs)


class RecordingHelper:
    """Stands in for the privileged helper: records what it was asked for."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def restart_core(
        self, *, watchdog_window_s: int | None = None, settle: str = "running"
    ) -> None:
        self.calls.append(
            ("restart-core", {"watchdog_window_s": watchdog_window_s, "settle": settle})
        )

    async def run(self, verb: str, **kwargs: Any) -> None:
        self.calls.append((verb, kwargs))


@pytest.fixture
def signing(tmp_path: Path) -> Signing:
    return make_signing(tmp_path)


@pytest.fixture
def appliance(tmp_path: Path, signing: Signing) -> Appliance:
    return Appliance(tmp_path, signing)


@pytest.fixture
def fast_venv(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Skip the real ``python -m venv`` where the environment is not the subject.

    The real build is exercised by
    :func:`test_prepare_builds_a_real_environment_from_the_wheels` and by the
    systemd harness. Everywhere else it is seconds spent re-proving the same
    thing, so the directory is made and the interpreter it would contain is
    this one.
    """

    def build(self: UpdateRunner, destination: Path) -> Path:
        wheels = destination / "wheels"
        if not wheels.is_dir() or not any(wheels.glob("*.whl")):
            raise WheelsMissing(f"{wheels} holds no wheels")
        target = destination / venv_name()
        target.mkdir(parents=True, exist_ok=True)
        return target

    monkeypatch.setattr(UpdateRunner, "_build_venv", build)
    monkeypatch.setattr(update_module, "_venv_python", lambda venv: Path(sys.executable))
    yield


async def _chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


# -- the upload (Q9) -------------------------------------------------------------------


class TestStreamedUpload:
    """The upload is written as it arrives, never assembled in memory.

    The appliance has 4 GB of RAM and a package may be 2 GB, so this is not a
    tidiness question. The assertion is on the spool path and on the file
    growing mid-stream, because allocating two gigabytes to prove it would be
    the very thing under test.
    """

    async def test_it_spools_to_data_tmp_and_hashes_on_the_way(
        self, appliance: Appliance
    ) -> None:
        body = b"abcdefgh" * 1000
        staged = await stage_upload(_chunks(body[:4000], body[4000:]), appliance.paths)
        assert staged.path.parent == appliance.paths.tmp_dir
        assert staged.path.read_bytes() == body
        assert staged.size == len(body)
        import hashlib

        assert staged.sha256 == hashlib.sha256(body).hexdigest()

    async def test_each_chunk_is_on_disk_before_the_next_is_asked_for(
        self, appliance: Appliance
    ) -> None:
        """Nothing accumulates: the file is already growing while the body arrives."""
        sizes: list[int] = []

        async def observed() -> AsyncIterator[bytes]:
            for _ in range(4):
                yield b"z" * 1024
                spooled = list(appliance.paths.tmp_dir.glob("upload-*.tar"))
                assert len(spooled) == 1
                sizes.append(spooled[0].stat().st_size)

        await stage_upload(observed(), appliance.paths)
        assert sizes == [1024, 2048, 3072, 4096]

    async def test_an_upload_past_the_cap_is_refused_and_leaves_nothing(
        self, appliance: Appliance
    ) -> None:
        with pytest.raises(UploadTooLarge):
            await stage_upload(_chunks(b"x" * 600, b"x" * 600), appliance.paths, max_bytes=1000)
        assert list(appliance.paths.tmp_dir.glob("upload-*.tar")) == []

    async def test_an_empty_upload_is_refused(self, appliance: Appliance) -> None:
        with pytest.raises(UploadEmpty):
            await stage_upload(_chunks(b"", b""), appliance.paths)
        assert list(appliance.paths.tmp_dir.glob("upload-*.tar")) == []

    def test_the_cap_is_two_gigabytes(self) -> None:
        assert update_module.MAX_UPLOAD_BYTES == 2 * 1024 * 1024 * 1024


# -- verification ----------------------------------------------------------------------


class TestVerification:
    async def test_a_signed_package_is_admitted_with_its_manifest(
        self, appliance: Appliance
    ) -> None:
        package = appliance.package(
            "v1.3.0", changes=["Fix: WebSocket reconnection"], min_app_version="v1.1.0"
        )
        staged = await stage_upload(_chunks(package.read_bytes()), appliance.paths)
        manifest = await verify_upload(
            staged, appliance.paths, anchors_dir=appliance.signing.anchors
        )
        assert manifest.version == "v1.3.0"
        assert manifest.changes == ("Fix: WebSocket reconnection",)
        assert manifest.key_id

    async def test_a_tampered_package_is_refused_before_anything_is_extracted(
        self, appliance: Appliance
    ) -> None:
        package = appliance.package("v1.3.0")
        raw = bytearray(package.read_bytes())
        # One byte of manifest.json, which the signature covers. The signature
        # is checked before the manifest is even parsed, so this is refused as
        # a signature failure rather than as malformed JSON.
        index = raw.index(b'"created_at"') + 20
        raw[index] ^= 0x20
        staged = await stage_upload(_chunks(bytes(raw)), appliance.paths)
        with pytest.raises(SignatureInvalid):
            await verify_upload(staged, appliance.paths, anchors_dir=appliance.signing.anchors)
        assert not (appliance.data / "app" / "v1.3.0").exists()

    async def test_a_tampered_payload_is_refused_by_its_digest(
        self, appliance: Appliance
    ) -> None:
        """The signature covers the manifest; the manifest covers every member."""
        package = appliance.package("v1.3.0")
        raw = bytearray(package.read_bytes())
        index = raw.index(b"__version__")  # a payload file's own bytes
        raw[index + 3] ^= 0x01
        staged = await stage_upload(_chunks(bytes(raw)), appliance.paths)
        with pytest.raises(MemberMismatch):
            await verify_upload(staged, appliance.paths, anchors_dir=appliance.signing.anchors)
        assert not (appliance.data / "app" / "v1.3.0").exists()

    async def test_a_downgrade_is_refused(self, appliance: Appliance) -> None:
        package = appliance.package("v1.1.0")
        staged = await stage_upload(_chunks(package.read_bytes()), appliance.paths)
        with pytest.raises(DowngradeRefused):
            await verify_upload(staged, appliance.paths, anchors_dir=appliance.signing.anchors)

    async def test_an_os_package_is_refused_by_the_application_endpoint(
        self, appliance: Appliance, tmp_path: Path
    ) -> None:
        from tests.package_factory import tool

        source = make_source(tmp_path, "v1.3.0")
        output = tmp_path / "os.tar"
        assert (
            tool.main(
                [
                    "build",
                    "--type",
                    "os",
                    "--version",
                    "v1.3.0",
                    "--source",
                    str(source),
                    "--output",
                    str(output),
                    "--force",
                ]
            )
            == 0
        )
        assert (
            tool.main(
                [
                    "sign",
                    "--package",
                    str(output),
                    "--key",
                    str(appliance.signing.keys / "test-release.key"),
                    "--no-passphrase",
                ]
            )
            == 0
        )
        staged = await stage_upload(_chunks(output.read_bytes()), appliance.paths)
        with pytest.raises(PackageTypeMismatch):
            await verify_upload(staged, appliance.paths, anchors_dir=appliance.signing.anchors)


# -- what is waiting -------------------------------------------------------------------


class TestPendingRecord:
    async def test_a_new_pending_package_replaces_and_deletes_the_old_one(
        self, appliance: Appliance
    ) -> None:
        first = await stage_upload(_chunks(b"one"), appliance.paths)
        write_pending(appliance.paths, appliance.pending_for(first.path, "v1.3.0"))
        second = await stage_upload(_chunks(b"two"), appliance.paths)
        write_pending(appliance.paths, appliance.pending_for(second.path, "v1.4.0"))
        assert not first.path.exists()
        pending = read_pending(appliance.paths)
        assert pending is not None
        assert pending.version == "v1.4.0"

    async def test_a_pending_record_whose_package_has_gone_reads_as_nothing(
        self, appliance: Appliance
    ) -> None:
        staged = await stage_upload(_chunks(b"one"), appliance.paths)
        write_pending(appliance.paths, appliance.pending_for(staged.path, "v1.3.0"))
        staged.path.unlink()
        assert read_pending(appliance.paths) is None

    async def test_stale_uploads_are_discarded_but_the_pending_one_is_kept(
        self, appliance: Appliance
    ) -> None:
        keep = await stage_upload(_chunks(b"keep"), appliance.paths)
        await stage_upload(_chunks(b"orphan"), appliance.paths)
        await stage_upload(_chunks(b"orphan"), appliance.paths)
        write_pending(appliance.paths, appliance.pending_for(keep.path, "v1.3.0"))
        assert discard_stale_uploads(appliance.paths) == 2
        assert keep.path.exists()

    async def test_clearing_takes_the_package_with_it(self, appliance: Appliance) -> None:
        staged = await stage_upload(_chunks(b"one"), appliance.paths)
        write_pending(appliance.paths, appliance.pending_for(staged.path, "v1.3.0"))
        clear_pending(appliance.paths)
        assert not staged.path.exists()
        assert read_pending(appliance.paths) is None


# -- preparation: extract, environment, dry run ----------------------------------------


class TestPrepare:
    async def test_it_extracts_beside_the_current_version_and_never_over_it(
        self, appliance: Appliance, fast_venv: None
    ) -> None:
        package = appliance.package("v1.3.0")
        prepared = await appliance.runner().prepare(
            appliance.pending_for(package, "v1.3.0")
        )
        assert prepared.directory == appliance.data / "app" / "v1.3.0"
        assert (prepared.directory / "VERSION").read_text(encoding="utf-8") == "v1.3.0\n"
        # The version that is running has not been touched, which is what
        # makes rollback a symlink change rather than a restore (§14.2).
        assert installed_version(appliance.paths) == "v1.2.0"
        assert (appliance.data / "app" / "v1.2.0" / "VERSION").exists()


    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes; the appliance is Debian")
    async def test_nginx_can_read_the_web_interface_it_installs(
        self, appliance: Appliance, fast_venv: None
    ) -> None:
        """nginx's workers run as www-data and serve web/ from the version directory.

        This process runs under auditorium-core's UMask=0027, and an image
        built before this was fixed left /data/app 0750: at either, every page
        was a 500 (the CM5, 24 September 2026). Others may traverse and read —
        nothing in a version is secret; config.toml is a link into
        /data/config, which stays closed.
        """
        (appliance.data / "app").chmod(0o750)
        extra = {**_tree("v1.3.0"), "web/index.html": "<title>Proskenion</title>\n"}
        source = make_source(appliance.root, "v1.3.0", extra=extra)
        package = appliance.package("v1.3.0", source=source)
        previous = os.umask(0o027)
        try:
            prepared = await appliance.runner().prepare(
                appliance.pending_for(package, "v1.3.0")
            )
        finally:
            os.umask(previous)
        for directory in (appliance.data / "app", prepared.directory, prepared.directory / "web"):
            assert stat.S_IMODE(directory.stat().st_mode) == 0o755, directory
        index = prepared.directory / "web" / "index.html"
        assert stat.S_IMODE(index.stat().st_mode) == 0o644

    async def test_the_new_version_reads_this_machines_bootstrap_config(
        self, appliance: Appliance, fast_venv: None
    ) -> None:
        """§4.14: ``/opt/auditorium/config.toml`` must resolve after the swap.

        ``ExecStart`` passes that path, and ``/opt/auditorium`` is
        ``/data/app/current``. A package cannot carry the symlink — the
        verifier refuses links — so without this every update would start an
        application that cannot find its configuration.
        """
        package = appliance.package("v1.3.0")
        prepared = await appliance.runner().prepare(
            appliance.pending_for(package, "v1.3.0")
        )
        link = prepared.directory / "config.toml"
        assert link.is_symlink()
        assert _link_target(link) == appliance.data / "config" / "auditorium.toml"

    async def test_a_config_toml_in_the_package_does_not_replace_the_machines(
        self, appliance: Appliance, fast_venv: None
    ) -> None:
        extra = {**_tree("v1.3.0"), "config.toml": '[database]\npath = "/elsewhere.db"\n'}
        source = make_source(appliance.root, "v1.3.0", extra=extra)
        package = appliance.package("v1.3.0", source=source)
        prepared = await appliance.runner().prepare(
            appliance.pending_for(package, "v1.3.0")
        )
        link = prepared.directory / "config.toml"
        assert link.is_symlink()
        assert _link_target(link) == appliance.data / "config" / "auditorium.toml"

    async def test_prepare_builds_a_real_environment_from_the_wheels(
        self, appliance: Appliance
    ) -> None:
        """The real ``python -m venv`` and a real offline ``pip install``.

        ``--no-index`` is the point: the appliance sits on a school VLAN with
        restricted internet access, so every wheel has to travel inside the
        package.
        """
        package = appliance.package("v1.3.0")
        prepared = await appliance.runner().prepare(
            appliance.pending_for(package, "v1.3.0")
        )
        assert prepared.venv.name == venv_name()
        assert prepared.venv.name.startswith("venv-cp")
        link = prepared.directory / "venv"
        assert link.is_symlink()
        assert os.readlink(link).rstrip("/\\").endswith(venv_name())
        installed = list(prepared.venv.rglob("proskenion_stub/__init__.py"))
        assert installed, "the vendored wheel was not installed into the environment"

    async def test_a_package_with_no_wheels_is_refused_and_leaves_nothing(
        self, appliance: Appliance, fast_venv: None
    ) -> None:
        source = make_source(
            appliance.root, "v1.3.0", with_wheels=False, extra=_tree("v1.3.0")
        )
        package = appliance.package("v1.3.0", source=source)
        with pytest.raises(WheelsMissing):
            await appliance.runner().prepare(appliance.pending_for(package, "v1.3.0"))
        assert not (appliance.data / "app" / "v1.3.0").exists()

    async def test_a_failing_migration_changes_nothing_at_all(
        self, appliance: Appliance, fast_venv: None
    ) -> None:
        """§14.2: the dry run aborts cleanly, having changed nothing."""
        source = make_source(
            appliance.root, "v1.3.0", extra=_tree("v1.3.0", MIGRATION_FAILS)
        )
        package = appliance.package("v1.3.0", source=source)
        helper = RecordingHelper()
        with pytest.raises(MigrationDryRunFailed, match="003_add_column"):
            await appliance.runner(helper=helper).prepare(
                appliance.pending_for(package, "v1.3.0")
            )
        assert not (appliance.data / "app" / "v1.3.0").exists()
        assert installed_version(appliance.paths) == "v1.2.0"
        assert list(appliance.paths.snapshots_dir.glob("*.db")) == []
        assert (await appliance.paths.store().read()).update is None
        assert helper.calls == []
        assert appliance.scenes() == {"assembly", "concert"}

    async def test_the_dry_run_never_touches_the_live_database(
        self, appliance: Appliance, fast_venv: None
    ) -> None:
        """It runs against a copy, so a migration that writes cannot reach production."""
        writes = """
import sqlite3, sys
connection = sqlite3.connect(sys.argv[-1])
connection.execute("DELETE FROM scenes")
connection.commit()
raise SystemExit(0)
"""
        source = make_source(appliance.root, "v1.3.0", extra=_tree("v1.3.0", writes))
        package = appliance.package("v1.3.0", source=source)
        await appliance.runner().prepare(appliance.pending_for(package, "v1.3.0"))
        assert appliance.scenes() == {"assembly", "concert"}
        assert list(appliance.paths.tmp_dir.glob("migration-check-*")) == []

    async def test_an_interrupted_earlier_attempt_is_replaced(
        self, appliance: Appliance, fast_venv: None
    ) -> None:
        half = appliance.data / "app" / "v1.3.0"
        half.mkdir()
        (half / "leftover").write_text("junk", encoding="utf-8")
        package = appliance.package("v1.3.0")
        prepared = await appliance.runner().prepare(
            appliance.pending_for(package, "v1.3.0")
        )
        assert not (prepared.directory / "leftover").exists()


# -- the commit: ordering is the correctness -------------------------------------------


class Steps:
    """Records the sequence of step events, and what was true when each ended."""

    def __init__(self, appliance: Appliance) -> None:
        self.appliance = appliance
        self.events: list[str] = []
        self.current_at: dict[str, str | None] = {}
        self.update_at: dict[str, Any] = {}

    def __call__(self, key: str, phase: str) -> None:
        self.events.append(f"{phase}:{key}")
        if phase == "done":
            self.current_at[key] = installed_version(self.appliance.paths)
            self.update_at[key] = self.appliance.paths.store().read_sync().update


class TestCommitOrder:
    async def test_the_seven_steps_run_in_the_order_the_specification_gives(
        self, appliance: Appliance, fast_venv: None
    ) -> None:
        package = appliance.package("v1.3.0")
        steps = Steps(appliance)
        helper = RecordingHelper()
        runner = appliance.runner(helper=helper, observer=steps)
        prepared = await runner.prepare(appliance.pending_for(package, "v1.3.0"))
        await runner.commit(prepared)
        assert steps.events == [
            "start:extract",
            "done:extract",
            "start:venv",
            "done:venv",
            "start:dry_run",
            "done:dry_run",
            "start:snapshot",
            "done:snapshot",
            "start:record",
            "done:record",
            "start:swap",
            "done:swap",
            "start:restart",
            "done:restart",
        ]
        assert [key for key, _ in APPLY_STEPS] == [
            "extract",
            "venv",
            "dry_run",
            "snapshot",
            "record",
            "swap",
            "restart",
        ]

    async def test_the_record_is_written_before_the_swap_and_the_snapshot_before_both(
        self, appliance: Appliance, fast_venv: None
    ) -> None:
        """A machine that dies between the record and the swap is recoverable.

        The other way round — a new version live with nothing recording what
        it replaced — leaves the unattended path in §14.5 with no rollback
        target and no snapshot to restore.
        """
        package = appliance.package("v1.3.0")
        steps = Steps(appliance)
        runner = appliance.runner(helper=RecordingHelper(), observer=steps)
        prepared = await runner.prepare(appliance.pending_for(package, "v1.3.0"))
        applied = await runner.commit(prepared)

        assert appliance.paths.snapshot_path("v1.3.0").is_file()
        assert steps.update_at["snapshot"] is None  # nothing recorded yet
        assert steps.update_at["record"]["to"] == "v1.3.0"
        assert steps.update_at["record"]["from"] == "v1.2.0"
        assert steps.current_at["record"] == "v1.2.0"  # still the old code
        assert steps.current_at["swap"] == "v1.3.0"
        assert applied.from_version == "v1.2.0"
        assert applied.to_version == "v1.3.0"
        assert applied.snapshot == str(appliance.paths.snapshot_path("v1.3.0"))

    async def test_the_restart_goes_through_the_helper_without_waiting_for_it(
        self, appliance: Appliance, fast_venv: None
    ) -> None:
        package = appliance.package("v1.3.0")
        helper = RecordingHelper()
        runner = appliance.runner(helper=helper)
        prepared = await runner.prepare(appliance.pending_for(package, "v1.3.0"))
        await runner.commit(prepared)
        # §4.7: a version that has never run on this machine before gets the
        # widened watchdog window for its first start (§14.5). The helper
        # bounds it; this only asks.
        assert helper.calls == [
            ("restart-core", {"watchdog_window_s": 60, "settle": "running"})
        ]

    async def test_the_snapshot_carries_the_data_as_it_was_at_the_swap(
        self, appliance: Appliance, fast_venv: None
    ) -> None:
        package = appliance.package("v1.3.0")
        runner = appliance.runner(helper=RecordingHelper())
        prepared = await runner.prepare(appliance.pending_for(package, "v1.3.0"))
        appliance.write_database({"assembly", "concert", "prizegiving"})
        await runner.commit(prepared)
        restored = appliance.data / "restored.db"
        snapshot_database(appliance.paths.snapshot_path("v1.3.0"), restored)
        connection = sqlite3.connect(restored)
        try:
            names = {row[0] for row in connection.execute("SELECT name FROM scenes")}
        finally:
            connection.close()
        assert names == {"assembly", "concert", "prizegiving"}


class TestKilledAtEveryStep:
    """Stop the process at each step and show the appliance still starts.

    The real kill — ``SIGKILL`` under a real systemd, with the service
    restarted afterwards — is in ``appliance/tests/systemd-cases.sh``. This is
    the same question asked at the Python level, where every intermediate
    state can be inspected: after every step, ``current`` resolves, and it
    resolves to a version directory that exists.
    """

    class Killed(BaseException):
        """Not an ``Exception``: nothing in the runner may catch and tidy it."""

    @pytest.mark.parametrize("after", [key for key, _ in APPLY_STEPS])
    async def test_current_always_resolves_to_an_installed_version(
        self, appliance: Appliance, fast_venv: None, after: str
    ) -> None:
        package = appliance.package("v1.3.0")

        def kill(key: str, phase: str) -> None:
            if key == after and phase == "done":
                raise TestKilledAtEveryStep.Killed(key)

        runner = appliance.runner(helper=RecordingHelper(), observer=kill)
        pending = appliance.pending_for(package, "v1.3.0")
        with pytest.raises(TestKilledAtEveryStep.Killed):
            prepared = await runner.prepare(pending)
            await runner.commit(prepared)

        version = installed_version(appliance.paths)
        assert version in ("v1.2.0", "v1.3.0")
        assert version is not None
        assert (appliance.data / "app" / version).is_dir()
        assert (appliance.data / "app" / version / "VERSION").is_file()
        # Before the swap it must still be the old one; from the swap on, the
        # new one — there is no step at which it is neither.
        expected = "v1.3.0" if after in ("swap", "restart") else "v1.2.0"
        assert version == expected

    @pytest.mark.parametrize("after", ["snapshot", "record", "swap", "restart"])
    async def test_whatever_it_comes_back_on_there_is_a_way_out_of_it(
        self, appliance: Appliance, fast_venv: None, after: str
    ) -> None:
        """Killed before the swap: still the old version, and it is intact.

        Killed at or after the swap: the new version, with the previous one
        and its snapshot both still there for §14.3 to use.
        """
        package = appliance.package("v1.3.0")

        def kill(key: str, phase: str) -> None:
            if key == after and phase == "done":
                raise TestKilledAtEveryStep.Killed(key)

        runner = appliance.runner(helper=RecordingHelper(), observer=kill)
        with pytest.raises(TestKilledAtEveryStep.Killed):
            prepared = await runner.prepare(appliance.pending_for(package, "v1.3.0"))
            await runner.commit(prepared)

        assert (appliance.data / "app" / "v1.2.0" / "VERSION").is_file()
        assert appliance.paths.snapshot_path("v1.3.0").is_file()
        if after in ("swap", "restart"):
            rolled = await appliance.runner(helper=RecordingHelper()).roll_back()
            assert rolled.to_version == "v1.2.0"
            assert installed_version(appliance.paths) == "v1.2.0"
        else:
            # Still on the version that has been running all along: there is
            # nothing to undo, and an unused v1.3.0 beside it is not a
            # rollback target.
            assert installed_version(appliance.paths) == "v1.2.0"
            with pytest.raises(NothingToRollBackTo):
                await appliance.runner(helper=RecordingHelper()).roll_back()

    async def test_an_unfinished_newer_version_is_never_a_rollback_target(
        self, appliance: Appliance
    ) -> None:
        """Roll back means back. A half-installed v1.3.0 is not somewhere to go."""
        appliance.install("v1.3.0", make_current=False)
        with pytest.raises(NothingToRollBackTo):
            await appliance.runner(helper=RecordingHelper()).roll_back()
        assert installed_version(appliance.paths) == "v1.2.0"


# -- retention -------------------------------------------------------------------------


class TestRetention:
    def test_the_current_symlink_is_never_mistaken_for_a_version(
        self, appliance: Appliance
    ) -> None:
        assert installed_versions(appliance.data / "app") == ["v1.2.0"]
        assert previous_versions(appliance.paths) == []

    def test_two_previous_versions_are_kept_and_older_ones_pruned(
        self, appliance: Appliance
    ) -> None:
        for version in ("v1.0.0", "v1.1.0", "v1.2.0", "v1.3.0"):
            appliance.install(version, make_current=version == "v1.3.0")
        removed = prune_versions(appliance.paths)
        assert set(removed) == {"v1.0.0"}
        assert set(installed_versions(appliance.data / "app")) == {
            "v1.1.0",
            "v1.2.0",
            "v1.3.0",
        }

    def test_the_recorded_rollback_target_is_protected_whatever_its_age(
        self, appliance: Appliance
    ) -> None:
        for version in ("v1.0.0", "v1.1.0", "v1.2.0", "v1.3.0"):
            appliance.install(version, make_current=version == "v1.3.0")
        os.utime(appliance.data / "app" / "v1.0.0", (0, 0))
        removed = prune_versions(appliance.paths, keep_previous=1, protect=["v1.0.0"])
        assert "v1.0.0" not in removed
        assert (appliance.data / "app" / "v1.0.0").is_dir()

    def test_the_pre_update_snapshot_carries_a_sidecar_and_prunes_nothing(
        self, appliance: Appliance
    ) -> None:
        """§15.3 prunes in the nightly job and under pressure, across every kind
        of snapshot, never the one a rollback relies on; the update itself
        only takes one, through the same helper as every other snapshot."""
        appliance.paths.snapshots_dir.mkdir(parents=True)
        for index in range(13):
            path = appliance.paths.snapshot_path(f"v9.{index}.0")
            path.write_bytes(b"x")
            os.utime(path, (index, index))
        target = appliance.paths.snapshot_path("v1.3.0")
        assert take_pre_update_snapshot(
            appliance.paths.database, target, from_version="v1.2.0"
        ) is True
        assert len(list(appliance.paths.snapshots_dir.glob("pre-update-*.db"))) == 14
        sidecar = read_sidecar(target)
        assert sidecar is not None
        assert sidecar["reason"] == "update to v1.3.0"
        assert sidecar["app_version"] == "v1.2.0"
        restored = appliance.paths.tmp_dir / "restored.db"
        restored.parent.mkdir(parents=True, exist_ok=True)
        restore_snapshot(target, restored)
        assert sqlite3.connect(restored).execute("PRAGMA integrity_check").fetchone()[0] == "ok"


# -- rollback (§14.3) ------------------------------------------------------------------


class TestRollback:
    async def test_the_symlink_moves_before_the_snapshot_is_restored(
        self, appliance: Appliance, fast_venv: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """B33: the intuitive order silently undoes the rollback at the next start.

        New code against an old schema migrates forward and the rollback
        disappears. Old code against a new schema is refused by §15.2's
        schema-ahead guard, which is loud and recoverable.
        """
        package = appliance.package("v1.3.0")
        runner = appliance.runner(helper=RecordingHelper())
        prepared = await runner.prepare(appliance.pending_for(package, "v1.3.0"))
        await runner.commit(prepared)

        seen: list[str | None] = []
        real = update_module.restore_snapshot

        def observing(snapshot: Path, database: Path) -> None:
            seen.append(installed_version(appliance.paths))
            real(snapshot, database)

        monkeypatch.setattr(update_module, "restore_snapshot", observing)
        rolled = await appliance.runner(helper=RecordingHelper()).roll_back()
        assert seen == ["v1.2.0"], "the snapshot was restored before the symlink moved"
        assert rolled.from_version == "v1.3.0"
        assert rolled.to_version == "v1.2.0"

    async def test_it_restores_the_data_as_it_was_before_the_update(
        self, appliance: Appliance, fast_venv: None
    ) -> None:
        package = appliance.package("v1.3.0")
        runner = appliance.runner(helper=RecordingHelper())
        prepared = await runner.prepare(appliance.pending_for(package, "v1.3.0"))
        await runner.commit(prepared)
        appliance.write_database({"assembly", "concert", "written-after-the-update"})
        await appliance.runner(helper=RecordingHelper()).roll_back()
        assert appliance.scenes() == {"assembly", "concert"}

    async def test_it_clears_the_update_record_and_restarts(
        self, appliance: Appliance, fast_venv: None
    ) -> None:
        package = appliance.package("v1.3.0")
        runner = appliance.runner(helper=RecordingHelper())
        prepared = await runner.prepare(appliance.pending_for(package, "v1.3.0"))
        await runner.commit(prepared)
        helper = RecordingHelper()
        await appliance.runner(helper=helper).roll_back()
        assert (await appliance.paths.store().read()).update is None
        # No widened window on the way back: the version being restored has
        # already run here, so extra slack would only delay the watchdog.
        assert helper.calls == [
            ("restart-core", {"watchdog_window_s": None, "settle": "running"})
        ]

    async def test_with_no_previous_version_it_refuses_rather_than_guessing(
        self, appliance: Appliance
    ) -> None:
        with pytest.raises(NothingToRollBackTo):
            await appliance.runner(helper=RecordingHelper()).roll_back()
        assert installed_version(appliance.paths) == "v1.2.0"

    async def test_it_will_not_point_current_at_itself(self, appliance: Appliance) -> None:
        """``is_dir()`` follows symlinks, so ``current`` looks like a version directory."""
        with pytest.raises(NothingToRollBackTo):
            await appliance.runner(helper=RecordingHelper()).roll_back(to="current")
        assert installed_version(appliance.paths) == "v1.2.0"

    async def test_the_code_rolls_back_even_with_no_snapshot(
        self, appliance: Appliance
    ) -> None:
        appliance.install("v1.3.0")
        rolled = await appliance.runner(helper=RecordingHelper()).roll_back()
        assert rolled.to_version == "v1.2.0"
        assert rolled.snapshot is None
        assert installed_version(appliance.paths) == "v1.2.0"


# -- snapshots -------------------------------------------------------------------------


class TestSnapshots:
    def test_a_snapshot_round_trips(self, appliance: Appliance, tmp_path: Path) -> None:
        target = tmp_path / "snap.db"
        assert snapshot_database(appliance.paths.database, target) is True
        appliance.write_database({"changed"})
        restore_snapshot(target, appliance.paths.database)
        assert appliance.scenes() == {"assembly", "concert"}

    def test_a_first_install_with_no_database_is_not_a_failure(
        self, tmp_path: Path
    ) -> None:
        assert snapshot_database(tmp_path / "absent.db", tmp_path / "snap.db") is False

    def test_restoring_removes_the_write_ahead_log(
        self, appliance: Appliance, tmp_path: Path
    ) -> None:
        target = tmp_path / "snap.db"
        snapshot_database(appliance.paths.database, target)
        wal = appliance.paths.database.with_name(appliance.paths.database.name + "-wal")
        wal.write_bytes(b"stale")
        restore_snapshot(target, appliance.paths.database)
        assert not wal.exists()


# -- "the next quiet moment" (Q17) -----------------------------------------------------


class TestQuiet:
    """Each of the four conditions on its own, then all of them together."""

    base = dict(
        hirer_enabled=False,
        scenes_running=0,
        seconds_since_connection=3600.0,
        when=datetime(2026, 9, 20, 23, 15, tzinfo=AUCKLAND),
    )

    def test_all_four_together_is_quiet(self) -> None:
        report = evaluate_quiet(**self.base)  # type: ignore[arg-type]
        assert report.quiet
        assert report.blocking() == []

    def test_hirer_access_enabled_is_not_quiet(self) -> None:
        report = evaluate_quiet(**{**self.base, "hirer_enabled": True})  # type: ignore[arg-type]
        assert not report.quiet
        assert report.blocking() == ["hirer_access_disabled"]

    def test_a_running_scene_is_not_quiet(self) -> None:
        report = evaluate_quiet(**{**self.base, "scenes_running": 1})  # type: ignore[arg-type]
        assert report.blocking() == ["no_scene_running"]

    def test_a_socket_inside_ten_minutes_is_not_quiet(self) -> None:
        report = evaluate_quiet(
            **{**self.base, "seconds_since_connection": 599.0}  # type: ignore[arg-type]
        )
        assert report.blocking() == ["no_recent_connection"]
        assert evaluate_quiet(
            **{**self.base, "seconds_since_connection": 600.0}  # type: ignore[arg-type]
        ).quiet

    @pytest.mark.parametrize(
        ("hour", "minute", "outside"),
        [
            (2, 29, True),
            (2, 30, False),
            (3, 0, False),
            (3, 29, False),
            (3, 30, True),
            (14, 0, True),
        ],
    )
    def test_the_nightly_window_is_closed_at_its_start_and_open_at_its_end(
        self, hour: int, minute: int, outside: bool
    ) -> None:
        when = datetime(2026, 9, 20, hour, minute, tzinfo=AUCKLAND)
        assert outside_nightly_window(when) is outside
        assert (
            evaluate_quiet(**{**self.base, "when": when}).quiet is outside  # type: ignore[arg-type]
        )

    def test_every_condition_that_blocks_is_named(self) -> None:
        report = evaluate_quiet(
            hirer_enabled=True,
            scenes_running=2,
            seconds_since_connection=0.0,
            when=datetime(2026, 9, 20, 3, 0, tzinfo=AUCKLAND),
        )
        assert report.blocking() == [
            "hirer_access_disabled",
            "no_scene_running",
            "no_recent_connection",
            "outside_nightly_window",
        ]
        assert report.to_json()["quiet"] is False
