"""``POST /system/backup/restore`` (contracts §5, §13.2, §21.24; Q9, Q15).

The checks, the order and the refusals are proved in
``tests/unit/core/test_backup_restore.py``. What this file proves is the route:
that both shapes contracts §5 gives it are told apart, that a refusal arrives
as the §16.1 envelope naming the check that refused it, and that the answer
carries what §21.24 has to show — what was replaced, what was deliberately not,
and whether device passwords need re-entering.

The database here is **file-backed**, unlike every other API test's: a restore
replaces the database file and closes the connection, which an in-memory one
does not have and would not survive. For the same reason a restore is the last
request each of these tests makes.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from proskenion.api.app import API_PREFIX, create_app
from proskenion.config import Config
from proskenion.core import auth, setup
from proskenion.core.backup import BackupPaths
from proskenion.core.backup_archive import BuiltArchive, build_archive
from proskenion.core.backup_restore import RestoreResult
from proskenion.core.helper import HelperStatus
from proskenion.core.ratelimit import RateLimiter
from proskenion.db.connection import Database
from proskenion.db.crud import backup as backup_crud
from proskenion.db.crud import hirer as hirer_crud
from proskenion.db.crud import system_state
from proskenion.db.crud import users as users_crud
from proskenion.db.migrations import migrate
from tests.unit.api.conftest import (
    ADMIN_PASSWORD,
    HIRER_PIN,
    OPERATOR_PASSWORD,
    TEST_ROUNDS,
    make_client,
)

SYSTEM = f"{API_PREFIX}/system"
RESTORE = f"{SYSTEM}/backup/restore"
OCTET = {"content-type": "application/octet-stream"}


class FakeHelper:
    """Records the restart; see ``tests/unit/core/test_backup_restore.py``."""

    def __init__(self) -> None:
        self.restarts: list[int | None] = []

    async def submit(self, verb: str, **kwargs: Any) -> str:
        return "fake-request-id"

    async def run(self, verb: str, **kwargs: Any) -> HelperStatus:
        return HelperStatus(
            id="fake", state="done", step=1, of=1, message="done", error=None, finished_at="now"
        )

    async def restart_core(
        self, *, watchdog_window_s: int | None = None, settle: str = "running"
    ) -> HelperStatus:
        self.restarts.append(watchdog_window_s)
        return HelperStatus(
            id="fake", state="running", step=1, of=2, message="restarting", error=None,
            finished_at=None,
        )


@pytest.fixture
async def db(config: Config) -> AsyncIterator[Database]:
    """A database on disk, with the schema, seed and test staff passwords.

    Overrides the in-memory one in ``conftest``: this endpoint replaces the
    file the database lives in.
    """
    config.database.path.parent.mkdir(parents=True, exist_ok=True)
    database = Database()
    await database.open(config.database.path)
    try:
        await migrate(database)
        await users_crud.set_password_hash(
            database, "admin", auth.hash_secret(ADMIN_PASSWORD, rounds=TEST_ROUNDS)
        )
        await users_crud.set_password_hash(
            database, "operator", auth.hash_secret(OPERATOR_PASSWORD, rounds=TEST_ROUNDS)
        )
        await hirer_crud.set_pin_hash(
            database, auth.hash_secret(HIRER_PIN, rounds=TEST_ROUNDS), updated_by=None
        )
        await system_state.set(
            database, setup.SYSTEM_DOMAIN, setup.FIRST_RUN_COMPLETED_KEY, "true"
        )
        yield database
    finally:
        if database.is_open:
            await database.close()


@pytest.fixture
def backup_paths(tmp_path: Path, config: Config) -> BackupPaths:
    return BackupPaths(
        db_path=config.database.path,
        data_dir=config.app.data_dir,
        state_dir=config.app.state_dir,
        local_dir=tmp_path / "srv-local",
        usb_dir=tmp_path / "mnt-backup",
        staging_dir=tmp_path / "staging",
    )


@pytest.fixture
def helper() -> FakeHelper:
    return FakeHelper()


@pytest.fixture
def app(
    config: Config,
    db: Database,
    tokens: Any,
    limiter: RateLimiter,
    backup_paths: BackupPaths,
    helper: FakeHelper,
) -> FastAPI:
    application = create_app(
        config, db=db, tokens=tokens, limiter=limiter, backup_paths=backup_paths
    )
    application.state.helper = helper
    return application


@pytest.fixture
async def admin(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with make_client(app) as http:
        response = await http.post(f"{API_PREFIX}/auth/login", json={"password": ADMIN_PASSWORD})
        assert response.status_code == 200, response.text
        yield http


async def make_archive(config: Config, backup_paths: BackupPaths) -> BuiltArchive:
    """A real archive of this appliance, built the way the nightly job builds one."""
    return await build_archive(
        db_path=backup_paths.db_path,
        data_dir=backup_paths.data_dir,
        state_dir=backup_paths.state_dir,
        staging_dir=backup_paths.staging_dir or (backup_paths.data_dir / "staging"),
        schema_version=2,
        app_version="v1.2.0",
    )


# -- the streamed upload (Q9) ----------------------------------------------------------


async def test_an_uploaded_archive_is_restored_and_the_appliance_restarts(
    admin: AsyncClient, db: Database, config: Config, backup_paths: BackupPaths,
    helper: FakeHelper,
) -> None:
    await system_state.set(db, "venue", "name", "before")
    built = await make_archive(config, backup_paths)
    await system_state.set(db, "venue", "name", "after")

    response = await admin.post(
        f"{RESTORE}?sha256={built.sha256}", content=built.path.read_bytes(), headers=OCTET
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "upload"
    assert body["checksum_verified"] is True
    assert body["restarted"] is True
    assert "database" in body["replaced"]
    assert body["snapshot"].startswith("pre-restore-")
    assert helper.restarts == [60]

    restored = Database()
    await restored.open(config.database.path)
    try:
        assert await system_state.get_value(restored, "venue", "name") == "before"
    finally:
        await restored.close()


async def test_the_upload_is_removed_whether_it_was_restored_or_refused(
    admin: AsyncClient, config: Config, backup_paths: BackupPaths
) -> None:
    """/data/tmp is not where a rejected archive sits waiting to be found."""
    built = await make_archive(config, backup_paths)
    response = await admin.post(
        f"{RESTORE}?sha256={'0' * 64}", content=built.path.read_bytes(), headers=OCTET
    )
    assert response.status_code == 422, response.text
    assert list((config.app.data_dir / "tmp").glob("restore-*")) == []


async def test_a_refusal_names_the_check_that_refused_it(
    admin: AsyncClient, config: Config, backup_paths: BackupPaths, helper: FakeHelper
) -> None:
    """§16.1's envelope, with ``detail.rule`` so §21.24 says which check failed."""
    built = await make_archive(config, backup_paths)
    response = await admin.post(
        f"{RESTORE}?sha256={'0' * 64}", content=built.path.read_bytes(), headers=OCTET
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_failed"
    assert error["detail"]["rule"] == "checksum"
    assert "checksum does not match" in error["message"]
    assert helper.restarts == []


async def test_a_file_that_is_not_an_archive_is_refused(admin: AsyncClient) -> None:
    body = b"not an archive" * 100
    response = await admin.post(
        f"{RESTORE}?sha256={hashlib.sha256(body).hexdigest()}", content=body, headers=OCTET
    )
    assert response.status_code == 422
    assert response.json()["error"]["detail"]["rule"] == "archive"


async def test_an_empty_upload_is_refused(admin: AsyncClient) -> None:
    response = await admin.post(RESTORE, content=b"", headers=OCTET)
    assert response.status_code == 422
    assert response.json()["error"]["detail"]["rule"] == "upload_empty"


# -- the JSON shape: a stored archive, or a snapshot -------------------------------------


async def test_a_named_archive_is_restored_from_srv_local(
    admin: AsyncClient, db: Database, config: Config, backup_paths: BackupPaths
) -> None:
    import shutil

    from proskenion.core.backup_archive import archive_filename, checksum_filename

    built = await make_archive(config, backup_paths)
    backup_paths.local_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(built.path, backup_paths.local_dir / archive_filename(built.id))
    shutil.copyfile(built.checksum_path, backup_paths.local_dir / checksum_filename(built.id))
    await backup_crud.record_archive(
        db,
        archive_id=built.id,
        created_at=built.manifest.created_at,
        source="scheduled",
        size_bytes=built.size_bytes,
        sha256=built.sha256,
        schema_version=built.manifest.schema_version,
        app_version=built.manifest.app_version,
        local_present=True,
        usb_present=False,
        network_present=False,
    )

    response = await admin.post(RESTORE, json={"archive_id": built.id})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "local"
    assert body["archive_id"] == built.id


async def test_an_archive_that_is_not_recorded_is_not_found_anywhere(
    admin: AsyncClient,
) -> None:
    response = await admin.post(RESTORE, json={"archive_id": "auditorium-20200101-0300"})
    assert response.status_code == 503
    assert response.json()["error"]["detail"]["rule"] == "unreachable"


async def test_a_snapshot_that_is_not_held_is_not_found(admin: AsyncClient) -> None:
    response = await admin.post(RESTORE, json={"snapshot": "pre-restore-20260920-000000.db"})
    assert response.status_code == 404
    assert response.json()["error"]["detail"]["rule"] == "snapshot"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"archive_id": "a", "snapshot": "pre-restore-b.db"},
        {"snapshot": "pre-restore-b.db", "destination": "usb"},
        {"archive_id": "a", "unexpected": True},
    ],
)
async def test_a_request_naming_the_wrong_thing_is_refused(
    admin: AsyncClient, body: dict[str, Any]
) -> None:
    response = await admin.post(RESTORE, json=body)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_failed"


# -- what the restore leaves behind for the screen ---------------------------------------


async def test_the_answer_carries_what_was_not_applied(
    admin: AsyncClient, config: Config, backup_paths: BackupPaths
) -> None:
    """Q15: §21.24 has to be able to tell an operator what was left alone."""
    built = await make_archive(config, backup_paths)
    response = await admin.post(
        f"{RESTORE}?sha256={built.sha256}", content=built.path.read_bytes(), headers=OCTET
    )
    assert response.status_code == 200, response.text
    not_applied = " ".join(response.json()["not_applied"])
    assert "Network settings" in not_applied
    assert "Cloudflare" in not_applied
    assert "Application code" in not_applied


async def test_the_status_endpoint_carries_the_restore_this_appliance_came_back_from(
    admin: AsyncClient, db: Database
) -> None:
    """The contracts §6 banner vocabulary is closed and has no key for a
    restore, so the record persisted into the restored database is how
    "re-enter device passwords" reaches the screen after the restart."""
    import json

    result = RestoreResult(
        at="2026-09-20T19:30:00+12:00",
        source="usb",
        archive_id="auditorium-20260919-0300",
        created_at="2026-09-19T03:00:00+12:00",
        schema_version=2,
        app_version="v1.2.0",
        sha256="a" * 64,
        checksum_verified=True,
        snapshot="pre-restore-20260920-193000.db",
        baselines_snapshot=None,
        replaced=("database",),
        not_applied=("Network settings from the archive were not applied.",),
        migrations_pending=(),
        device_passwords_require_reentry=True,
        devices_needing_passwords=("Projector",),
        restarted=True,
    )
    await system_state.set(
        db, "backup", "restore", json.dumps(result.to_json()), source="backup_restore"
    )

    response = await admin.get(f"{SYSTEM}/backup/status")

    assert response.status_code == 200, response.text
    last = response.json()["last_restore"]
    assert last["archive_id"] == "auditorium-20260919-0300"
    assert last["device_passwords_require_reentry"] is True
    assert last["devices_needing_passwords"] == ["Projector"]


async def test_the_status_endpoint_says_nothing_when_nothing_was_restored(
    admin: AsyncClient,
) -> None:
    response = await admin.get(f"{SYSTEM}/backup/status")
    assert response.status_code == 200
    assert response.json()["last_restore"] is None
