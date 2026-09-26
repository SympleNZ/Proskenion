"""Restoring from a backup archive (§13.2, §13.7, §21.24; Q9, Q15; B19, B33).

An archive is unsigned input, so most of this file is about refusals: each
check has its own, and each is proved separately, because §21.24 shows *which*
one failed and a test that only asserts "it was refused" would not notice the
day they all collapse into one message.

The round trip is the other half — back up, change everything, restore, and
find the configuration back — together with Q15's exclusions asserted one at a
time: an archive carries ``system.json`` and application code, and a restore
must leave both exactly where they were.

Archives come from two places here. The round trip uses a real one, built by
:func:`~proskenion.core.backup_archive.build_archive` from a live appliance,
because that is the only way to prove the two halves agree. The refusals use
:func:`pack_archive`, which writes a tar by hand — a hostile member path or a
database that does not match its own manifest is not something the builder can
be asked for.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tarfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import zstandard

from proskenion.core import backup_destinations
from proskenion.core.backup_archive import (
    DB_MEMBER,
    MANIFEST_MEMBER,
    archive_filename,
    build_archive,
    checksum_filename,
)
from proskenion.core.backup_restore import (
    ARCHIVE_STEPS,
    RESTORE_OPERATION,
    Actor,
    ArchiveUnreachable,
    ArchiveUnreadable,
    ArchiveUntrusted,
    ChecksumMismatch,
    ChecksumMissing,
    DatabaseCorrupt,
    DatabaseDigestMismatch,
    MigrationWouldFail,
    NamedSource,
    NoSuchSnapshot,
    RestorePaths,
    RestoreRefused,
    RestoreResult,
    RestoreService,
    SchemaAheadInArchive,
    SnapshotSource,
    StagedArchive,
    UnsafeArchiveMember,
    UploadEmpty,
    UploadSource,
    UploadTooLarge,
    check_archive_member_path,
    network_differences,
    read_restore_record,
    stage_archive_upload,
)
from proskenion.core.certs import generate_self_signed, write_certificate_pair
from proskenion.core.helper import HelperError, HelperStatus
from proskenion.core.secrets import DeviceSecret
from proskenion.core.snapshots import read_sidecar
from proskenion.db.connection import Database
from proskenion.db.crud import backup as backup_crud
from proskenion.db.crud import system_state
from proskenion.db.crud.base import AUCKLAND
from proskenion.db.migrations import migrate

NOW = datetime(2026, 9, 20, 19, 30, 0, tzinfo=AUCKLAND)
HOSTNAME = "av.school.nz"

CERT_BEFORE = b"-----BEGIN CERTIFICATE-----\nbefore\n-----END CERTIFICATE-----\n"
KEY_BEFORE = b"-----BEGIN PRIVATE KEY-----\nbefore\n-----END PRIVATE KEY-----\n"
CERT_AFTER = b"-----BEGIN CERTIFICATE-----\nafter\n-----END CERTIFICATE-----\n"
KEY_AFTER = b"-----BEGIN PRIVATE KEY-----\nafter\n-----END PRIVATE KEY-----\n"

TOKEN_BYTES = b'{"enc": "the-cloudflare-token-this-machine-holds"}'

SYSTEM_JSON_BEFORE: dict[str, Any] = {
    "hostname": "auditorium",
    "network": {
        "address": "10.2.30.45/24",
        "gateway": "10.2.30.1",
        "dns": ["10.2.30.1"],
        "smtp_relay": {"host": "relay.n4l.co.nz", "port": 25},
    },
}
SYSTEM_JSON_AFTER: dict[str, Any] = {
    "hostname": "auditorium",
    "network": {
        "address": "10.9.9.9/24",
        "gateway": "10.9.9.1",
        "dns": ["10.2.30.1"],
        "smtp_relay": {"host": "relay.n4l.co.nz", "port": 25},
    },
}


# -- a fake helper: the restart is the appliance's, not this test's ---------------------


class FakeHelper:
    """Records the restart a restore ends with (contracts §2).

    Only ``restart_core`` is needed: how a restart is actually carried out is
    ``appliance/bin/auditorium-helper``'s business and the systemd harness's.
    """

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.restarts: list[int | None] = []
        self._raises = raises

    async def restart_core(
        self, *, watchdog_window_s: int | None = None, settle: str = "running"
    ) -> HelperStatus:
        self.restarts.append(watchdog_window_s)
        if self._raises is not None:
            raise self._raises
        return HelperStatus(
            id="fake", state="running", step=1, of=2, message="restarting", error=None,
            finished_at=None,
        )


class FakeDestination:
    """A destination holding files in memory, so the network path is proved
    without a NAS. The real SMB and SFTP clients are proved against real
    servers in ``tests/integration/backup/test_destinations.py``."""

    name = "network"

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.reads: list[str] = []

    async def available(self) -> bool:
        return True

    async def write(self, local_path: Path, filename: str) -> None:
        self.store[filename] = await asyncio.to_thread(local_path.read_bytes)

    async def delete(self, filename: str) -> None:
        self.store.pop(filename, None)

    async def list_names(self) -> list[str]:
        return list(self.store)

    async def read(self, filename: str, local_path: Path) -> None:
        if filename not in self.store:
            raise backup_destinations.DestinationError(f"{filename} is not here")
        self.reads.append(filename)
        await asyncio.to_thread(local_path.write_bytes, self.store[filename])


# -- a live appliance on disk ----------------------------------------------------------


@dataclass
class Appliance:
    """A file-backed appliance: a real database, and the directories a restore
    replaces or refuses to replace."""

    db: Database
    paths: RestorePaths
    secret: DeviceSecret
    helper: FakeHelper
    staging: Path
    progress: list[tuple[str, int, int, str]] = field(default_factory=list)

    @property
    def data_dir(self) -> Path:
        return self.paths.data_dir

    @property
    def baselines(self) -> Path:
        return self.data_dir / "config" / "baselines"

    @property
    def system_json(self) -> Path:
        return self.data_dir / "config" / "system.json"

    @property
    def token(self) -> Path:
        return self.data_dir / "certs" / "cloudflare-token.enc"

    @property
    def app_marker(self) -> Path:
        return self.data_dir / "app" / "v1.2.0" / "marker.txt"

    def live_cert(self) -> bytes:
        return (self.data_dir / "certs" / "live" / HOSTNAME / "fullchain.pem").read_bytes()

    def live_key(self) -> bytes:
        return (self.data_dir / "certs" / "live" / HOSTNAME / "privkey.pem").read_bytes()

    def service(self, **kwargs: Any) -> RestoreService:
        def progress(operation: str, step: int, of: int, message: str) -> None:
            self.progress.append((operation, step, of, message))

        kwargs.setdefault("helper", self.helper)
        return RestoreService(
            self.db,
            self.paths,
            secret=self.secret,
            progress=progress,
            now=lambda: NOW,
            **kwargs,
        )


@pytest.fixture
async def appliance(tmp_path: Path) -> AsyncIterator[Appliance]:
    data_dir = tmp_path / "data"
    state_dir = tmp_path / "appliance"
    db_path = data_dir / "db" / "auditorium.db"
    db_path.parent.mkdir(parents=True)
    state_dir.mkdir()

    (data_dir / "config").mkdir(parents=True)
    (data_dir / "config" / "system.json").write_text(
        json.dumps(SYSTEM_JSON_BEFORE), encoding="utf-8"
    )
    (data_dir / "config" / "baselines").mkdir(parents=True)
    (data_dir / "config" / "baselines" / "current.sqlite").write_bytes(b"BASELINE-BEFORE")
    write_certificate_pair(data_dir, HOSTNAME, KEY_BEFORE, CERT_BEFORE)
    (data_dir / "certs" / "cloudflare-token.enc").write_bytes(TOKEN_BYTES)
    (data_dir / "app" / "v1.2.0").mkdir(parents=True)
    (data_dir / "app" / "v1.2.0" / "marker.txt").write_text("running code", encoding="utf-8")
    (state_dir / "smtp-fallback.toml").write_text("host = 'relay'\n", encoding="utf-8")

    db = Database()
    await db.open(db_path)
    await migrate(db)
    try:
        yield Appliance(
            db=db,
            paths=RestorePaths.for_appliance(
                database=db_path,
                data_dir=data_dir,
                state_dir=state_dir,
                local_dir=tmp_path / "srv-local",
                usb_dir=tmp_path / "mnt-backup",
            ),
            secret=DeviceSecret(b"\x11" * 32),
            helper=FakeHelper(),
            staging=tmp_path / "staging",
        )
    finally:
        with contextlib.suppress(Exception):
            await db.close()


async def build_live_archive(appliance: Appliance, **overrides: Any) -> Any:
    """A real archive of the appliance as it is right now."""
    kwargs: dict[str, Any] = {
        "db_path": appliance.paths.database,
        "data_dir": appliance.data_dir,
        "state_dir": appliance.paths.state_dir,
        "staging_dir": appliance.staging,
        "schema_version": 2,
        "app_version": "v1.2.0",
        "now": NOW,
    }
    kwargs.update(overrides)
    return await build_archive(**kwargs)


def publish(appliance: Appliance, built: Any, *, destination: str = "local") -> Path:
    """Put a built archive and its sidecar where a named restore will find it."""
    root = {
        "local": appliance.paths.local_dir,
        "usb": appliance.paths.usb_dir,
    }[destination]
    root.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(built.path, root / archive_filename(built.id))
    shutil.copyfile(built.checksum_path, root / checksum_filename(built.id))
    return root / archive_filename(built.id)


async def record(appliance: Appliance, built: Any, **presence: bool) -> None:
    await backup_crud.record_archive(
        appliance.db,
        archive_id=built.id,
        created_at=built.manifest.created_at,
        source="scheduled",
        size_bytes=built.size_bytes,
        sha256=built.sha256,
        schema_version=built.manifest.schema_version,
        app_version=built.manifest.app_version,
        local_present=presence.get("local", False),
        usb_present=presence.get("usb", False),
        network_present=presence.get("network", False),
    )


# -- packing an archive by hand, for the refusals ---------------------------------------


def _info(name: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = 0o640
    info.type = tarfile.REGTYPE
    return info


def pack_archive(
    path: Path,
    *,
    files: list[tuple[str, bytes]],
    manifest: dict[str, Any] | None = None,
    contents: list[str] | None = None,
    specials: list[tarfile.TarInfo] | None = None,
    manifest_first: bool = True,
) -> str:
    """Write a ``.tar.zst`` with exactly these members. Returns its SHA-256.

    ``files`` is written in order; ``contents`` overrides what the manifest
    claims to carry, and ``specials`` adds header-only members — a symlink,
    a directory — that no builder would ever produce.
    """
    db_bytes = next((data for name, data in files if name == DB_MEMBER), b"")
    document = {
        "created_at": NOW.isoformat(timespec="seconds"),
        "schema_version": 2,
        "app_version": "v1.2.0",
        "sha256": hashlib.sha256(db_bytes).hexdigest(),
        "contents": contents if contents is not None else [name for name, _ in files],
    }
    if manifest is not None:
        document.update(manifest)
    payload = json.dumps(document, indent=2, sort_keys=True).encode("utf-8")

    compressor = zstandard.ZstdCompressor(level=1)
    with open(path, "wb") as raw:
        with compressor.stream_writer(raw, closefd=False) as compressed:
            with tarfile.open(fileobj=compressed, mode="w|") as tar:
                if manifest_first:
                    tar.addfile(_info(MANIFEST_MEMBER, len(payload)), io.BytesIO(payload))
                for name, data in files:
                    tar.addfile(_info(name, len(data)), io.BytesIO(data))
                for special in specials or []:
                    tar.addfile(special)
                if not manifest_first:
                    tar.addfile(_info(MANIFEST_MEMBER, len(payload)), io.BytesIO(payload))
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def migrated_db_bytes(tmp_path: Path, *, mutate: Any = None) -> bytes:
    """A real, migrated database as bytes, optionally damaged on the way out."""
    source = tmp_path / f"source-{id(mutate)}.db"
    db = Database()
    await db.open(source)
    await migrate(db)
    await db.close()
    clean = tmp_path / f"clean-{id(mutate)}.db"
    clean.unlink(missing_ok=True)
    with contextlib.closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as live:
        with contextlib.closing(sqlite3.connect(clean)) as copy:
            live.backup(copy)
    if mutate is not None:
        with contextlib.closing(sqlite3.connect(clean)) as conn:
            mutate(conn)
            conn.commit()
    return clean.read_bytes()


def upload_of(path: Path, sha256: str | None) -> UploadSource:
    return UploadSource(
        staged=StagedArchive(
            path=path, sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            size=path.stat().st_size,
        ),
        declared_sha256=sha256,
    )


# ======================================================================================
# The round trip (§22.4)
# ======================================================================================


async def test_round_trip_restores_the_configuration_and_restarts(
    appliance: Appliance, tmp_path: Path
) -> None:
    """Back up, change everything, restore: the configuration is back and the
    appliance restarts behind a bounded watchdog window."""
    await system_state.set(appliance.db, "venue", "name", "before")
    built = await build_live_archive(appliance)

    # Change everything a restore is allowed to touch.
    await system_state.set(appliance.db, "venue", "name", "after")
    (appliance.baselines / "current.sqlite").write_bytes(b"BASELINE-AFTER")
    write_certificate_pair(appliance.data_dir, HOSTNAME, KEY_AFTER, CERT_AFTER)
    assert appliance.live_cert() == CERT_AFTER

    result = await appliance.service().restore(
        upload_of(built.path, built.sha256), actor=Actor(ident="admin", ip_address="10.2.30.9")
    )

    assert result.checksum_verified is True
    assert result.restarted is True
    assert appliance.helper.restarts == [60]
    assert "database" in result.replaced
    assert "baselines" in result.replaced
    assert f"certificate: {HOSTNAME}" in result.replaced
    assert result.archive_id == built.id

    # The database is back — read the file the restore put in place.
    restored = Database()
    await restored.open(appliance.paths.database)
    try:
        assert await system_state.get_value(restored, "venue", "name") == "before"
    finally:
        await restored.close()

    assert (appliance.baselines / "current.sqlite").read_bytes() == b"BASELINE-BEFORE"
    assert appliance.live_cert() == CERT_BEFORE
    assert appliance.live_key() == KEY_BEFORE


async def test_a_restore_says_when_the_certificate_served_changes_and_what_it_names(
    appliance: Appliance, tmp_path: Path
) -> None:
    """The browser that asked for the restore accepted the certificate being
    served; the restored one is refused until the page is reloaded — and on an
    address it does not name, for good (the rebuild rehearsal, 25 September
    2026: the real certificate came back while the admin was on the bare IP)."""
    real = generate_self_signed(HOSTNAME, directory=tmp_path / "real", addresses=[])
    write_certificate_pair(
        appliance.data_dir, HOSTNAME, real.key.read_bytes(), real.certificate.read_bytes()
    )
    built = await build_live_archive(appliance)

    # The rebuilt appliance serves a fallback of its own, valid for the IP too.
    fallback = generate_self_signed(
        HOSTNAME, directory=tmp_path / "fallback", addresses=["10.2.30.251"]
    )
    write_certificate_pair(
        appliance.data_dir, HOSTNAME, fallback.key.read_bytes(), fallback.certificate.read_bytes()
    )
    result = await appliance.service().restore(upload_of(built.path, built.sha256))
    assert result.certificate_replaced is True
    assert result.certificate_names == (HOSTNAME,), "the restored one does not name the IP"
    assert RestoreResult.from_json(result.to_json()).certificate_names == (HOSTNAME,)

    # Restoring the same archive again changes nothing the browser sees.
    again = await appliance.service().restore(upload_of(built.path, built.sha256))
    assert again.certificate_replaced is False


async def test_the_restore_writes_the_audit_row_into_the_database_it_restored(
    appliance: Appliance,
) -> None:
    """§6.14's ``backup_restored``. It goes into the **new** database: a row in
    the old one would be in a file nothing will ever open again."""
    built = await build_live_archive(appliance)
    await appliance.service().restore(
        upload_of(built.path, built.sha256), actor=Actor(ident="admin", ip_address="10.2.30.9")
    )

    restored = Database()
    await restored.open(appliance.paths.database)
    try:
        async with restored.read() as conn:
            cursor = await conn.execute(
                "SELECT event_type, user_ident, ip_address, detail FROM security_events"
            )
            rows = [dict(r) for r in await cursor.fetchall()]
        record_ = await read_restore_record(restored)
    finally:
        await restored.close()

    assert [r["event_type"] for r in rows] == ["backup_restored"]
    assert rows[0]["user_ident"] == "admin"
    assert rows[0]["ip_address"] == "10.2.30.9"
    detail = json.loads(rows[0]["detail"])
    assert detail["archive_id"] == built.id
    assert detail["source"] == "upload"

    assert record_ is not None
    assert record_.archive_id == built.id
    assert record_.snapshot.startswith("pre-restore-")


async def test_progress_reports_every_step_under_the_contracted_operation(
    appliance: Appliance,
) -> None:
    built = await build_live_archive(appliance)
    await appliance.service().restore(upload_of(built.path, built.sha256))

    assert [p[0] for p in appliance.progress] == [RESTORE_OPERATION] * len(ARCHIVE_STEPS)
    assert [p[1] for p in appliance.progress] == list(range(1, len(ARCHIVE_STEPS) + 1))
    assert {p[2] for p in appliance.progress} == {len(ARCHIVE_STEPS)}
    assert appliance.progress[-1][3] == "Restarting the appliance"


# ======================================================================================
# Q15: exactly three things are replaced, and each exclusion separately
# ======================================================================================


async def test_network_settings_are_shown_and_not_applied(appliance: Appliance) -> None:
    """Q15: an archive carries ``system.json``; a restore never applies it."""
    built = await build_live_archive(appliance)
    appliance.system_json.write_text(json.dumps(SYSTEM_JSON_AFTER), encoding="utf-8")

    result = await appliance.service().restore(upload_of(built.path, built.sha256))

    assert json.loads(appliance.system_json.read_text()) == SYSTEM_JSON_AFTER
    differences = {d.key: (d.current, d.archived) for d in result.network_differences}
    assert differences["network.address"] == ("10.9.9.9/24", "10.2.30.45/24")
    assert differences["network.gateway"] == ("10.9.9.1", "10.2.30.1")
    assert "network.dns" not in differences  # unchanged, so not reported
    assert any("Network settings" in sentence for sentence in result.not_applied)


async def test_application_code_is_untouched(appliance: Appliance) -> None:
    """§13.2 puts application code in the archive; Q15 keeps it out of an
    in-app restore — that is the recovery USB's job (§13.7)."""
    built = await build_live_archive(appliance)
    appliance.app_marker.write_text("newer code", encoding="utf-8")

    result = await appliance.service().restore(upload_of(built.path, built.sha256))

    assert appliance.app_marker.read_text() == "newer code"
    assert sorted(p.name for p in (appliance.data_dir / "app").iterdir()) == ["v1.2.0"]
    assert any("Application code" in sentence for sentence in result.not_applied)


async def test_the_cloudflare_token_this_machine_holds_is_kept(appliance: Appliance) -> None:
    """§13.2 excludes the token from the archive; Q15 keeps the one already here."""
    built = await build_live_archive(appliance)
    result = await appliance.service().restore(upload_of(built.path, built.sha256))

    assert appliance.token.read_bytes() == TOKEN_BYTES
    assert any("Cloudflare" in sentence for sentence in result.not_applied)
    # And it was never in the archive to begin with (§13.2, B19).
    assert not any("cloudflare" in name.lower() for name in built.manifest.contents)


async def test_the_smtp_fallback_file_is_not_replaced(appliance: Appliance) -> None:
    """The archive carries it (contracts §8); Q15's list does not."""
    built = await build_live_archive(appliance)
    fallback = appliance.paths.state_dir / "smtp-fallback.toml"
    fallback.write_text("host = 'changed'\n", encoding="utf-8")

    await appliance.service().restore(upload_of(built.path, built.sha256))

    assert fallback.read_text() == "host = 'changed'\n"


def test_network_differences_ignores_the_mirrored_device_table() -> None:
    """``system.json``'s ``devices`` is mirrored from the rows a restore does
    replace (contracts §4), so reporting it would report the same change twice."""
    here = {"devices": [{"name": "mixer", "address": "10.2.30.71"}], "hostname": "a"}
    there = {"devices": [], "hostname": "b"}
    assert [d.key for d in network_differences(here, there)] == ["hostname"]


# ======================================================================================
# The refusals, one at a time
# ======================================================================================


async def test_a_bad_checksum_is_refused_by_name(appliance: Appliance) -> None:
    built = await build_live_archive(appliance)
    with pytest.raises(ChecksumMismatch) as caught:
        await appliance.service().restore(upload_of(built.path, "0" * 64))
    assert caught.value.rule == "checksum"
    assert appliance.helper.restarts == []


async def test_a_checksum_that_is_not_a_digest_is_refused(appliance: Appliance) -> None:
    built = await build_live_archive(appliance)
    with pytest.raises(ChecksumMissing):
        await appliance.service().restore(upload_of(built.path, "not-a-digest"))


async def test_a_corrupt_database_inside_a_valid_container_is_refused(
    appliance: Appliance, tmp_path: Path
) -> None:
    """The container's checksum only proves it arrived intact. This one has a
    manifest that agrees with its own contents and a database SQLite will not
    open — which is exactly the silent corruption §13.4 exists to catch."""
    archive = tmp_path / "corrupt.tar.zst"
    digest = pack_archive(archive, files=[(DB_MEMBER, b"this is not a database at all")])

    with pytest.raises(DatabaseCorrupt) as caught:
        await appliance.service().restore(upload_of(archive, digest))
    assert caught.value.rule == "database_integrity"


async def test_a_database_that_does_not_match_its_manifest_is_refused(
    appliance: Appliance, tmp_path: Path
) -> None:
    archive = tmp_path / "mismatched.tar.zst"
    good = await migrated_db_bytes(tmp_path)
    digest = pack_archive(
        archive, files=[(DB_MEMBER, good)], manifest={"sha256": "a" * 64}
    )
    with pytest.raises(DatabaseDigestMismatch):
        await appliance.service().restore(upload_of(archive, digest))


async def test_a_schema_ahead_of_this_build_is_refused_with_update_first(
    appliance: Appliance, tmp_path: Path
) -> None:
    """Q15: a newer archive is refused, and the sentence says what to do."""
    archive = tmp_path / "future.tar.zst"
    good = await migrated_db_bytes(tmp_path)
    digest = pack_archive(
        archive, files=[(DB_MEMBER, good)], manifest={"schema_version": 999}
    )
    with pytest.raises(SchemaAheadInArchive) as caught:
        await appliance.service().restore(upload_of(archive, digest))
    assert "Update the application first" in caught.value.summary
    assert appliance.paths.database.exists()


async def test_a_migration_that_would_fail_stops_before_anything_changes(
    appliance: Appliance, tmp_path: Path
) -> None:
    """The dry run is on a copy (§14.2, B33). This database claims never to
    have had the schema migration applied, so running it forward tries to
    create tables that are already there."""

    def forget_the_schema_migration(conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM schema_versions WHERE migration LIKE '001%'")

    archive = tmp_path / "unmigratable.tar.zst"
    damaged = await migrated_db_bytes(tmp_path, mutate=forget_the_schema_migration)
    digest = pack_archive(archive, files=[(DB_MEMBER, damaged)])

    before = appliance.paths.database.read_bytes()
    with pytest.raises(MigrationWouldFail) as caught:
        await appliance.service().restore(upload_of(archive, digest))
    assert caught.value.rule == "migration"
    assert appliance.paths.database.read_bytes() == before


@pytest.mark.parametrize(
    "hostile",
    [
        "../../../etc/passwd",
        "/etc/passwd",
        "db/../../escape.db",
        "db/./proskenion.db",
        "app/code.py",
        "db",
    ],
)
def test_a_hostile_member_path_is_refused(hostile: str) -> None:
    """An archive's member paths are treated exactly as a package's are
    (:func:`proskenion.core.packages.check_member_path`): relative,
    normalised, no traversal, and inside the four directories contracts
    §8 gives an archive."""
    with pytest.raises(UnsafeArchiveMember):
        check_archive_member_path(hostile)


async def test_an_archive_with_a_traversing_member_is_refused_unpacked(
    appliance: Appliance, tmp_path: Path
) -> None:
    archive = tmp_path / "hostile.tar.zst"
    good = await migrated_db_bytes(tmp_path)
    digest = pack_archive(
        archive,
        files=[(DB_MEMBER, good), ("../../escape.txt", b"owned")],
        contents=[DB_MEMBER, "../../escape.txt"],
    )
    with pytest.raises(UnsafeArchiveMember):
        await appliance.service().restore(upload_of(archive, digest))
    assert not (tmp_path.parent / "escape.txt").exists()


async def test_a_symlink_member_is_refused(appliance: Appliance, tmp_path: Path) -> None:
    """The one that matters most: a member named ``certs/x/fullchain.pem``
    pointing at ``/srv/appliance`` would turn "write the pair" into "write
    wherever the archive chose"."""
    link = tarfile.TarInfo("certs/av.school.nz/fullchain.pem")
    link.type = tarfile.SYMTYPE
    link.linkname = "/srv/appliance/device-secret"

    archive = tmp_path / "symlink.tar.zst"
    good = await migrated_db_bytes(tmp_path)
    digest = pack_archive(
        archive,
        files=[(DB_MEMBER, good)],
        specials=[link],
        contents=[DB_MEMBER, "certs/av.school.nz/fullchain.pem"],
    )
    with pytest.raises(UnsafeArchiveMember) as caught:
        await appliance.service().restore(upload_of(archive, digest))
    assert "symlink" in str(caught.value)


async def test_a_member_the_manifest_does_not_list_is_refused(
    appliance: Appliance, tmp_path: Path
) -> None:
    """Contracts §3's rule 3, applied to an archive: nothing outside the manifest."""
    archive = tmp_path / "extra.tar.zst"
    good = await migrated_db_bytes(tmp_path)
    digest = pack_archive(
        archive,
        files=[(DB_MEMBER, good), ("config/extra.json", b"{}")],
        contents=[DB_MEMBER],
    )
    with pytest.raises(UnsafeArchiveMember) as caught:
        await appliance.service().restore(upload_of(archive, digest))
    assert "not listed" in str(caught.value)


async def test_a_manifest_the_archive_does_not_honour_is_refused(
    appliance: Appliance, tmp_path: Path
) -> None:
    archive = tmp_path / "missing.tar.zst"
    good = await migrated_db_bytes(tmp_path)
    digest = pack_archive(
        archive,
        files=[(DB_MEMBER, good)],
        contents=[DB_MEMBER, "baselines/current.sqlite"],
    )
    with pytest.raises(ArchiveUnreadable) as caught:
        await appliance.service().restore(upload_of(archive, digest))
    assert "does not carry" in str(caught.value)


async def test_a_file_that_is_not_an_archive_is_refused(
    appliance: Appliance, tmp_path: Path
) -> None:
    not_an_archive = tmp_path / "photo.tar.zst"
    not_an_archive.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 512)
    digest = hashlib.sha256(not_an_archive.read_bytes()).hexdigest()
    with pytest.raises(ArchiveUnreadable):
        await appliance.service().restore(upload_of(not_an_archive, digest))


async def test_a_manifest_that_is_not_first_is_refused(
    appliance: Appliance, tmp_path: Path
) -> None:
    """The manifest has to be read before anything else is looked at, so it
    has to arrive before anything else in a forward-only stream."""
    archive = tmp_path / "reordered.tar.zst"
    good = await migrated_db_bytes(tmp_path)
    digest = pack_archive(archive, files=[(DB_MEMBER, good)], manifest_first=False)
    with pytest.raises(ArchiveUnreadable) as caught:
        await appliance.service().restore(upload_of(archive, digest))
    assert MANIFEST_MEMBER in str(caught.value)


async def test_nothing_is_replaced_by_any_refusal(appliance: Appliance, tmp_path: Path) -> None:
    """Every refusal above happens before the pre-restore snapshot, so a
    refused restore leaves no trace at all."""
    before = appliance.paths.database.read_bytes()
    archive = tmp_path / "corrupt.tar.zst"
    digest = pack_archive(archive, files=[(DB_MEMBER, b"rubbish")])
    with pytest.raises(RestoreRefused):
        await appliance.service().restore(upload_of(archive, digest))

    assert appliance.paths.database.read_bytes() == before
    assert (appliance.baselines / "current.sqlite").read_bytes() == b"BASELINE-BEFORE"
    assert not appliance.paths.snapshots_dir.exists()
    assert appliance.helper.restarts == []


# ======================================================================================
# The device secret (§6.10, §13.2)
# ======================================================================================


async def test_a_different_device_secret_asks_for_the_passwords_rather_than_failing(
    appliance: Appliance,
) -> None:
    """§13.2: the device secret is excluded on purpose, so restoring onto
    another machine cannot decrypt what the archive holds. That surfaces as
    "re-enter device passwords", with the devices named."""
    other_machine = DeviceSecret(b"\x22" * 32)
    async with appliance.db.write() as conn:
        await conn.execute(
            "INSERT INTO devices (id, category, driver_key, name, enabled, config,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
            (
                1,
                "projector",
                "pjlink",
                "Projector",
                json.dumps(
                    {
                        "password": other_machine.encrypt_value("password", "hunter2"),
                        "transport": {"type": "tcp", "host": "10.2.30.80", "port": 4352},
                    }
                ),
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
    built = await build_live_archive(appliance)

    result = await appliance.service().restore(upload_of(built.path, built.sha256))

    assert result.device_passwords_require_reentry is True
    assert result.devices_needing_passwords == ("Projector",)
    assert result.restarted is True  # surfaced, never a refusal


async def test_the_same_device_secret_asks_for_nothing(appliance: Appliance) -> None:
    async with appliance.db.write() as conn:
        await conn.execute(
            "INSERT INTO devices (id, category, driver_key, name, enabled, config,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
            (
                1,
                "projector",
                "pjlink",
                "Projector",
                json.dumps({"password": appliance.secret.encrypt_value("password", "hunter2")}),
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
    built = await build_live_archive(appliance)
    result = await appliance.service().restore(upload_of(built.path, built.sha256))

    assert result.device_passwords_require_reentry is False
    assert result.devices_needing_passwords == ()


# ======================================================================================
# Sources: an upload, /srv/local, the USB stick, the network destination
# ======================================================================================


async def test_restoring_a_named_archive_from_srv_local(appliance: Appliance) -> None:
    built = await build_live_archive(appliance)
    publish(appliance, built, destination="local")
    await record(appliance, built, local=True)

    result = await appliance.service().restore(NamedSource(archive_id=built.id))

    assert result.source == "local"
    assert result.checksum_verified is True
    assert result.archive_id == built.id


async def test_restoring_from_the_backup_usb(
    appliance: Appliance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The USB destination asks whether it is mounted before it reads (§4.5);
    a directory in a test is not a mount point, so that answer is stubbed and
    everything else is the real filesystem destination."""
    built = await build_live_archive(appliance)
    publish(appliance, built, destination="usb")
    await record(appliance, built, usb=True)
    monkeypatch.setattr(backup_destinations, "is_mounted", lambda path: True)

    result = await appliance.service().restore(
        NamedSource(archive_id=built.id, destination="usb")
    )

    assert result.source == "usb"
    assert result.checksum_verified is True


async def test_restoring_from_the_network_destination(appliance: Appliance) -> None:
    built = await build_live_archive(appliance)
    network = FakeDestination()
    await network.write(built.path, archive_filename(built.id))
    await network.write(built.checksum_path, checksum_filename(built.id))
    await record(appliance, built, network=True)

    async def provider() -> FakeDestination:
        return network

    result = await appliance.service(network_destination=provider).restore(
        NamedSource(archive_id=built.id, destination="network")
    )

    assert result.source == "network"
    assert checksum_filename(built.id) in network.reads


async def test_a_named_archive_that_is_nowhere_says_so(appliance: Appliance) -> None:
    built = await build_live_archive(appliance)
    await record(appliance, built, local=True)  # recorded, but never written out
    with pytest.raises(ArchiveUnreachable):
        await appliance.service().restore(NamedSource(archive_id=built.id))


async def test_an_archive_the_monthly_check_failed_is_never_offered(
    appliance: Appliance,
) -> None:
    """§13.4: a failed verification flags the archive untrusted, and a restore
    refuses it rather than putting a known-corrupt database in place."""
    built = await build_live_archive(appliance)
    publish(appliance, built, destination="local")
    await record(appliance, built, local=True)
    await backup_crud.mark_verified(
        appliance.db, built.id, verified_at=NOW.isoformat(), untrusted=True, reason="bad checksum"
    )
    with pytest.raises(ArchiveUntrusted):
        await appliance.service().restore(NamedSource(archive_id=built.id))


async def test_a_stored_archive_with_no_sidecar_falls_back_to_the_recorded_hash(
    appliance: Appliance,
) -> None:
    """The sidecar is the first answer; the row this appliance wrote when it
    built the archive is the second."""
    built = await build_live_archive(appliance)
    publish(appliance, built, destination="local")
    (appliance.paths.local_dir / checksum_filename(built.id)).unlink()
    await record(appliance, built, local=True)

    result = await appliance.service().restore(NamedSource(archive_id=built.id))
    assert result.checksum_verified is True


async def test_an_upload_without_a_sidecar_is_allowed_and_says_so(
    appliance: Appliance,
) -> None:
    """A checksum an operator supplies beside a file they also supplied proves
    nothing against substitution; what it does prove, the manifest's own
    digest of the database proves for the member that matters."""
    built = await build_live_archive(appliance)
    result = await appliance.service().restore(upload_of(built.path, None))
    assert result.checksum_verified is False
    assert result.restarted is True


# ======================================================================================
# The streamed upload (Q9)
# ======================================================================================


async def _chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


async def test_an_upload_is_streamed_to_disk_and_hashed_on_the_way(tmp_path: Path) -> None:
    body = b"an archive's bytes" * 1000
    staged = await stage_archive_upload(_chunks(body[:500], body[500:]), tmp_path / "tmp")

    assert staged.path.read_bytes() == body
    assert staged.sha256 == hashlib.sha256(body).hexdigest()
    assert staged.size == len(body)
    assert staged.path.parent == tmp_path / "tmp"


async def test_an_upload_past_the_limit_is_abandoned_with_its_file(tmp_path: Path) -> None:
    with pytest.raises(UploadTooLarge):
        await stage_archive_upload(_chunks(b"x" * 100, b"y" * 100), tmp_path, max_bytes=150)
    assert await asyncio.to_thread(lambda: list(tmp_path.glob("restore-*"))) == []


async def test_an_empty_upload_is_refused(tmp_path: Path) -> None:
    with pytest.raises(UploadEmpty):
        await stage_archive_upload(_chunks(), tmp_path)
    assert await asyncio.to_thread(lambda: list(tmp_path.glob("restore-*"))) == []


# ======================================================================================
# The pre-restore snapshot (§21.24: "so it is itself reversible")
# ======================================================================================


async def test_the_pre_restore_snapshot_puts_the_appliance_back(appliance: Appliance) -> None:
    """Restore an old archive, then undo it: the configuration that was
    running before the restore comes back, database and baselines alike."""
    await system_state.set(appliance.db, "venue", "name", "archived")
    built = await build_live_archive(appliance)

    await system_state.set(appliance.db, "venue", "name", "running")
    (appliance.baselines / "current.sqlite").write_bytes(b"BASELINE-RUNNING")

    restored = await appliance.service().restore(upload_of(built.path, built.sha256))
    assert (appliance.baselines / "current.sqlite").read_bytes() == b"BASELINE-BEFORE"

    # The application would have restarted here; a new process opens the file.
    appliance.db = Database()
    await appliance.db.open(appliance.paths.database)
    assert await system_state.get_value(appliance.db, "venue", "name") == "archived"

    undone = await appliance.service().restore(SnapshotSource(name=restored.snapshot))

    assert undone.source == "snapshot"
    assert "baselines" in undone.replaced
    back = Database()
    await back.open(appliance.paths.database)
    try:
        assert await system_state.get_value(back, "venue", "name") == "running"
    finally:
        await back.close()
    assert (appliance.baselines / "current.sqlite").read_bytes() == b"BASELINE-RUNNING"


async def test_undoing_takes_its_own_snapshot_first(appliance: Appliance) -> None:
    """Going back is a restore like any other, so it is reversible too."""
    built = await build_live_archive(appliance)
    first = await appliance.service().restore(upload_of(built.path, built.sha256))

    appliance.db = Database()
    await appliance.db.open(appliance.paths.database)
    undone = await appliance.service().restore(SnapshotSource(name=first.snapshot))

    assert undone.snapshot != first.snapshot
    assert (appliance.paths.snapshots_dir / undone.snapshot).is_file()


async def test_a_snapshot_name_that_is_not_one_is_refused(appliance: Appliance) -> None:
    for name in ("../../auditorium.db", "pre-update-v1.2.0.db", "nothing.db"):
        with pytest.raises(NoSuchSnapshot):
            await appliance.service().restore(SnapshotSource(name=name))


# ======================================================================================
# The restart
# ======================================================================================


async def test_a_refused_restart_says_the_backup_is_in_place(appliance: Appliance) -> None:
    """The data is already restored by the time the restart is asked for, so
    the failure has to say so rather than imply nothing happened."""
    built = await build_live_archive(appliance)
    appliance.helper = FakeHelper(raises=HelperError("the helper is not running"))

    from proskenion.core.backup_restore import RestartRefused

    with pytest.raises(RestartRefused) as caught:
        await appliance.service().restore(upload_of(built.path, built.sha256))
    assert "could not restart itself" in caught.value.summary


async def test_without_a_helper_the_restore_completes_unrestarted(
    appliance: Appliance,
) -> None:
    """A bench or a test with no privileged helper still restores; it just
    does not claim to have restarted."""
    built = await build_live_archive(appliance)
    result = await appliance.service(helper=None).restore(upload_of(built.path, built.sha256))
    assert result.restarted is False


# ======================================================================================
# The scene engine's exclusive lock (§13.5's guard, reused)
# ======================================================================================


class RecordingLock:
    def __init__(self) -> None:
        self.held = 0
        self.entered = False

    @contextlib.asynccontextmanager
    async def exclusive(self) -> AsyncIterator[None]:
        self.entered = True
        self.held += 1
        try:
            yield
        finally:
            self.held -= 1


async def test_the_scene_lock_is_held_across_the_commit(appliance: Appliance) -> None:
    lock = RecordingLock()
    built = await build_live_archive(appliance)
    await appliance.service(scenes=lock).restore(upload_of(built.path, built.sha256))
    assert lock.entered is True
    assert lock.held == 0


# ======================================================================================
# Snapshot retention (§15.3)
# ======================================================================================


def test_old_pre_restore_snapshots_are_pruned_with_their_baselines(tmp_path: Path) -> None:
    from proskenion.core.backup_restore import PRE_RESTORE_PREFIX, list_snapshots
    from proskenion.core.snapshots import prune_sync

    for index in range(12):
        stamp = f"2026092{index:02d}-000000"
        path = tmp_path / f"{PRE_RESTORE_PREFIX}{stamp}.db"
        path.write_bytes(b"x")
        os.utime(path, (index, index))
        (tmp_path / f"{PRE_RESTORE_PREFIX}{stamp}-baselines").mkdir()
    prune_sync(tmp_path, keep=10)

    assert len(list_snapshots(tmp_path)) == 10
    assert len(list(tmp_path.glob(f"{PRE_RESTORE_PREFIX}*-baselines"))) == 10


async def test_a_restore_prunes_no_snapshot_itself(appliance: Appliance) -> None:
    """§15.3 prunes in the nightly job and under pressure; a restore that
    pruned would be able to delete the snapshot another restore relies on."""
    appliance.paths.snapshots_dir.mkdir(parents=True)
    for index in range(12):
        name = f"pre-change-202609{index + 1:02d}-000000.db"
        (appliance.paths.snapshots_dir / name).write_bytes(b"x")
    built = await build_live_archive(appliance)
    result = await appliance.service().restore(upload_of(built.path, built.sha256))

    held = sorted(p.name for p in appliance.paths.snapshots_dir.glob("pre-*.db"))
    assert len(held) == 13
    assert result.snapshot in held
    sidecar = read_sidecar(appliance.paths.snapshots_dir / result.snapshot)
    assert sidecar is not None and sidecar["reason"].startswith("backup restore from")
