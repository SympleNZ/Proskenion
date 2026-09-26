"""A backup restore, followed across the restart it ends with (§13.2, §21.24, Q15).

Every test here drives the real sequence, in the order the appliance runs it,
because each defect it covers sat between two halves that were each correct:

1. an appliance *before* a rebuild — a database with the email relay, a
   network backup destination and a projector whose password is encrypted
   with that machine's device secret — backed up by the real
   :func:`~proskenion.core.backup_archive.build_archive`;
2. the rebuilt appliance, started for real (the application's lifespan), with
   the ``system.json`` the image seeds and none of those settings;
3. the restore, through the real :class:`~proskenion.core.backup_restore.RestoreService`,
   uploaded as the rehearsal on 25 September 2026 uploaded it;
4. the start that follows — the lifespan again, on the restored database —
   and what ``/system/backup/status`` then says.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from proskenion.api.app import API_PREFIX, create_app
from proskenion.config import Config
from proskenion.core import auth, setup, system_config
from proskenion.core.backup import BackupPaths
from proskenion.core.backup_archive import build_archive
from proskenion.core.backup_restore import (
    RestorePaths,
    RestoreResult,
    RestoreService,
    UploadSource,
    read_restore_record,
    stage_archive_upload,
)
from proskenion.core.helper import HelperStatus
from proskenion.core.secrets import DEFAULT_SECRET_PATH, DeviceSecret, generate_secret_if_missing
from proskenion.db.connection import Database
from proskenion.db.crud import backup as backup_crud
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import email as email_crud
from proskenion.db.crud import system_state
from proskenion.db.crud import users as users_crud
from proskenion.db.crud.base import AUCKLAND
from proskenion.db.migrations import migrate
from tests.unit.api.conftest import ADMIN_PASSWORD, TEST_ROUNDS, make_client

RELAY = {"host": "relay.n4l.co.nz", "port": 25}
NAS = {"address": "10.2.30.20", "protocol": "smb"}
ARCHIVE_ID = "auditorium-20260925-1135"


class RestartingHelper:
    """The restore's restart request, recorded. Carrying it out is
    ``appliance/bin/auditorium-helper``'s job; here the next lifespan is the
    restart."""

    def __init__(self) -> None:
        self.restarts = 0

    async def restart_core(self, **_: Any) -> HelperStatus:
        self.restarts += 1
        return HelperStatus(
            id="restart", state="running", step=1, of=2, message="restarting", error=None,
            finished_at=None,
        )


async def _chunks(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def _the_appliance_before_the_rebuild(root: Path) -> Path:
    """A commissioned appliance's database and ``system.json``, backed up."""
    data = root / "data"
    state = root / "appliance"
    (data / "config").mkdir(parents=True)
    state.mkdir()
    system_config.system_config_path(data).write_text(
        json.dumps({"hostname": "auditorium", "network": {"smtp_relay": RELAY}}),
        encoding="utf-8",
    )
    old_secret = DeviceSecret(b"\x01" * 32)
    db = Database()
    await db.open(root / "auditorium.db")
    try:
        await migrate(db)
        await users_crud.set_password_hash(
            db, "admin", auth.hash_secret(ADMIN_PASSWORD, rounds=TEST_ROUNDS)
        )
        await system_state.set(db, setup.SYSTEM_DOMAIN, setup.FIRST_RUN_COMPLETED_KEY, "true")
        await email_crud.upsert(
            db,
            host=str(RELAY["host"]),
            port=int(RELAY["port"]),
            tls_mode="none",
            username=None,
            password=None,
            sender="auditorium@obhs.school.nz",
            recipient="ict@obhs.school.nz",
            updated_by=None,
        )
        await backup_crud.upsert_destination(
            db,
            protocol="smb",
            host=NAS["address"],
            port=None,
            path="/backups/auditorium",
            username="auditorium",
            password=old_secret.encrypt_value("backup_destination_password", "nas-password"),
            enabled=True,
            updated_by=None,
        )
        await devices_crud.create(
            db,
            category="projector",
            driver_key="pjlink",
            name="Projector",
            enabled=False,
            config={
                "host": "10.2.30.60",
                "password": old_secret.encrypt_value("password", "pjlink-password"),
            },
        )
    finally:
        await db.close()
    built = await build_archive(
        db_path=root / "auditorium.db",
        data_dir=data,
        state_dir=state,
        staging_dir=root / "staging",
        schema_version=1,
        app_version="v0.1.2",
        now=datetime(2026, 9, 25, 11, 35, tzinfo=AUCKLAND),
    )
    return built.path


def _rebuilt(config: Config) -> None:
    """``system.json`` as the image seeds it: addressing, no relay, no NAS."""
    data = config.app.data_dir
    (data / "config").mkdir(parents=True)
    system_config.system_config_path(data).write_text(
        json.dumps({"hostname": "auditorium", "network": {"address": "10.2.30.251/24"}}),
        encoding="utf-8",
    )


def _backup_paths(config: Config, tmp_path: Path) -> BackupPaths:
    return BackupPaths(
        db_path=config.database.path,
        data_dir=config.app.data_dir,
        state_dir=config.app.state_dir,
        local_dir=tmp_path / "srv-local",
        usb_dir=tmp_path / "mnt-backup",
        staging_dir=tmp_path / "staging-live",
    )


async def _restore_on_the_rebuilt_appliance(
    config: Config, tmp_path: Path, archive: Path
) -> RestoreResult:
    """Start the rebuilt appliance and restore the uploaded archive on it."""
    upload = await asyncio.to_thread(archive.read_bytes)
    app = create_app(config, backup_paths=_backup_paths(config, tmp_path))
    async with app.router.lifespan_context(app):
        live: Database = app.state.db
        data = config.app.data_dir
        staged = await stage_archive_upload(_chunks(upload), data / "tmp")
        secret_path = config.app.state_dir / DEFAULT_SECRET_PATH.name
        generate_secret_if_missing(secret_path)
        helper = RestartingHelper()
        service = RestoreService(
            live,
            RestorePaths.for_appliance(
                database=config.database.path,
                data_dir=data,
                state_dir=config.app.state_dir,
                local_dir=tmp_path / "srv-local",
                usb_dir=tmp_path / "mnt-backup",
            ),
            secret=DeviceSecret.load(secret_path),
            helper=helper,  # type: ignore[arg-type]
        )
        result = await service.restore(UploadSource(staged=staged))
        assert helper.restarts == 1, "the restore did not ask for its restart"
    return result


def _helper_requests(config: Config) -> list[str]:
    directory = config.app.data_dir / "run" / "helper"
    verbs: list[str] = []
    for path in sorted(directory.glob("*.json")):
        if path.name.endswith(".status.json"):
            continue
        verbs.append(json.loads(path.read_text(encoding="utf-8"))["verb"])
    return verbs


# -- the firewall's inputs come back with the database (defect 3) ---------------------


async def test_the_start_after_a_restore_derives_the_relay_and_the_nas_from_it(
    config: Config, tmp_path: Path
) -> None:
    archive = await _the_appliance_before_the_rebuild(tmp_path / "before")
    _rebuilt(config)
    await _restore_on_the_rebuilt_appliance(config, tmp_path, archive)

    network = system_config.read(config.app.data_dir)["network"]
    assert "smtp_relay" not in network, "the restore applied system.json itself (Q15)"
    for path in (config.app.data_dir / "run" / "helper").glob("*.json"):
        path.unlink()

    # The restart the restore asked for.
    app = create_app(config, backup_paths=_backup_paths(config, tmp_path))
    async with app.router.lifespan_context(app):
        pass

    network = system_config.read(config.app.data_dir)["network"]
    assert network.get("smtp_relay") == RELAY, "mail's outbound rule would be missing"
    assert network.get("backup_destination") == NAS, "the NAS's rule would be missing"
    assert network["address"] == "10.2.30.251/24", "the appliance's own addressing moved"
    assert "apply-network" in _helper_requests(config), "the firewall was never re-rendered"


async def test_the_restore_does_not_list_what_it_derives_as_settings_not_applied(
    config: Config, tmp_path: Path
) -> None:
    """``network.smtp_relay`` sat under "Network settings the backup disagrees
    with (not applied)" — telling the admin to re-enter what the next start
    derives from the restored database anyway."""
    archive = await _the_appliance_before_the_rebuild(tmp_path / "before")
    _rebuilt(config)
    result = await _restore_on_the_rebuilt_appliance(config, tmp_path, archive)

    keys = [difference.key for difference in result.network_differences]
    assert not [key for key in keys if key.startswith("network.smtp_relay")], keys
    assert not [key for key in keys if key.startswith("network.backup_destination")], keys
    assert not [key for key in keys if key.startswith("devices")], keys
    # A real network setting the archive disagrees with is still reported.
    assert "network.address" in keys


# -- what the Backup screen shows after the restart (defect 5) ------------------------


async def test_the_restore_record_survives_the_restart_and_says_what_needs_attention(
    config: Config, tmp_path: Path
) -> None:
    archive = await _the_appliance_before_the_rebuild(tmp_path / "before")
    _rebuilt(config)
    result = await _restore_on_the_rebuilt_appliance(config, tmp_path, archive)
    assert result.restarted_at is None, "the restore cannot know when the restart happened"

    app = create_app(config, backup_paths=_backup_paths(config, tmp_path))
    async with app.router.lifespan_context(app):
        async with make_client(app) as http:
            login = await http.post(f"{API_PREFIX}/auth/login", json={"password": ADMIN_PASSWORD})
            assert login.status_code == 200, login.text
            status = await http.get(f"{API_PREFIX}/system/backup/status")
            assert status.status_code == 200, status.text
            last = status.json()["last_restore"]

            assert last is not None, "nothing records that the restore happened"
            assert last["at"] == result.at
            assert last["source"] == "upload"
            assert last["archive_id"] == ARCHIVE_ID
            assert last["restarted"] is True
            assert last["restarted_at"] is not None
            assert last["restarted_at"] >= last["at"]
            assert last["acknowledged_at"] is None
            assert last["devices_still_needing_passwords"] == ["Projector"]
            assert last["settings_still_needing_passwords"] == [
                "Network backup destination (Admin → Backup)"
            ]
            restarted_at = last["restarted_at"]

            acknowledged = await http.post(f"{API_PREFIX}/system/backup/restore/acknowledge")
            assert acknowledged.status_code == 200, acknowledged.text
            assert acknowledged.json()["acknowledged_at"] is not None

    # A later start is not the restart: the stamp stays the first one.
    app = create_app(config, backup_paths=_backup_paths(config, tmp_path))
    async with app.router.lifespan_context(app):
        record = await read_restore_record(app.state.db)
    assert record is not None
    assert record.restarted_at == restarted_at
    assert record.acknowledged_at is not None


async def test_a_password_re_entered_leaves_the_attention_list(
    config: Config, tmp_path: Path
) -> None:
    archive = await _the_appliance_before_the_rebuild(tmp_path / "before")
    _rebuilt(config)
    await _restore_on_the_rebuilt_appliance(config, tmp_path, archive)

    app = create_app(config, backup_paths=_backup_paths(config, tmp_path))
    async with app.router.lifespan_context(app):
        db: Database = app.state.db
        secret = DeviceSecret.load(config.app.state_dir / DEFAULT_SECRET_PATH.name)
        projector = next(d for d in await devices_crud.list_all(db) if d.name == "Projector")
        await devices_crud.update(
            db,
            projector.id,
            projector.updated_at,
            config={**projector.config, "password": secret.encrypt_value("password", "new")},
        )
        async with make_client(app) as http:
            await http.post(f"{API_PREFIX}/auth/login", json={"password": ADMIN_PASSWORD})
            last = (await http.get(f"{API_PREFIX}/system/backup/status")).json()["last_restore"]
    assert last["devices_needing_passwords"] == ["Projector"], "what the restore found is kept"
    assert last["devices_still_needing_passwords"] == []


@pytest.fixture(autouse=True)
def cheap_bcrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "BCRYPT_ROUNDS", 4)
