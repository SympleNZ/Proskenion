"""``/system/backup/*`` (contracts §5): status, run, verify, history,
download, destinations and the SFTP key. §22.4's round trip for each.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from proskenion.api.app import API_PREFIX, create_app
from proskenion.config import Config
from proskenion.core.backup import BackupJob, BackupPaths, BackupRunStatus, DestinationOutcome
from proskenion.core.backup_destinations import (
    BackupDestination,
    DestinationName,
    FilesystemDestination,
)
from proskenion.core.helper import HelperError, HelperStatus
from proskenion.core.ratelimit import RateLimiter
from proskenion.core.secrets import DeviceSecret
from proskenion.core.snapshots import snapshots_dir
from proskenion.db.connection import Database
from proskenion.db.crud import backup as backup_crud
from tests.unit.api.conftest import ADMIN_PASSWORD, make_client

SYSTEM = f"{API_PREFIX}/system"


class FakeHelper:
    """Stands in for :class:`~proskenion.core.helper.HelperClient` — "Back up
    now" only needs its ``run`` awaited, and saving a network destination
    only needs ``submit`` (fire-and-forget, the same as the device table's
    firewall mirror in ``proskenion.api.devices``); how either gets carried
    out is ``appliance/bin/auditorium-helper``'s and the systemd harness's
    concern.
    """

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls: list[str] = []
        self._raises = raises

    async def submit(self, verb: str, **kwargs: Any) -> str:
        self.calls.append(verb)
        return "fake-request-id"

    async def run(self, verb: str, **kwargs: Any) -> HelperStatus:
        self.calls.append(verb)
        if self._raises is not None:
            raise self._raises
        return HelperStatus(
            id="fake", state="done", step=4, of=4, message="done", error=None, finished_at="now"
        )


@pytest.fixture
def backup_paths(tmp_path: Path, config: Config) -> BackupPaths:
    """Local and USB point under ``tmp_path``, never at the real ``/srv/local``
    and ``/mnt/backup`` mounts (see ``get_backup_paths`` in ``proskenion.api.backup``)."""
    return BackupPaths(
        db_path=config.database.path,
        data_dir=config.app.data_dir,
        state_dir=config.app.state_dir,
        local_dir=tmp_path / "srv-local",
        usb_dir=tmp_path / "mnt-backup",
        staging_dir=tmp_path / "staging",
    )


def _local_only(
    paths: BackupPaths,
) -> Callable[[], Awaitable[dict[DestinationName, BackupDestination]]]:
    async def provider() -> dict[DestinationName, BackupDestination]:
        return {"local": FilesystemDestination("local", paths.local_dir)}

    return provider


@pytest.fixture
def app(
    config: Config, db: Database, tokens: Any, limiter: RateLimiter, backup_paths: BackupPaths
) -> FastAPI:
    application = create_app(
        config, db=db, tokens=tokens, limiter=limiter, backup_paths=backup_paths
    )
    application.state.helper = FakeHelper()
    return application


@pytest.fixture
async def admin(app: FastAPI) -> Any:
    async with make_client(app) as http:
        response = await http.post(f"{API_PREFIX}/auth/login", json={"password": ADMIN_PASSWORD})
        assert response.status_code == 200, response.text
        yield http


async def test_status_with_nothing_run_yet(admin: AsyncClient) -> None:
    response = await admin.get(f"{SYSTEM}/backup/status")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["last_run"] is None
    assert body["last_verify"] is None
    assert body["retention_days"] == {"local": 14, "usb": 7, "network": 30}
    assert isinstance(body["usb_present"], bool)


async def test_run_now_goes_through_the_helper_and_returns_the_persisted_status(
    admin: AsyncClient, app: FastAPI, db: Database
) -> None:
    status = BackupRunStatus(
        attempted_at="2026-09-20T03:00:00+12:00",
        source="manual",
        archive_id="auditorium-20260920-0300",
        job_result="success",
        job_detail=None,
        consecutive_failures=0,
        retried=False,
        destinations={
            "local": DestinationOutcome(True, True, None),
            "usb": DestinationOutcome(False, None, "media_absent"),
            "network": DestinationOutcome(False, None, "not_configured"),
        },
    )
    from proskenion.core.backup import _write_json  # test-only: seed what the helper "did"

    await _write_json(db, "status", status.to_json())

    response = await admin.post(f"{SYSTEM}/backup/run")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["archive_id"] == "auditorium-20260920-0300"
    assert body["result"] == "success"
    assert body["destinations"]["usb"] == {"attempted": False, "ok": None, "reason": "media_absent"}
    assert app.state.helper.calls == ["backup-now"]


async def test_run_now_reports_device_unavailable_when_the_helper_fails(
    admin: AsyncClient, app: FastAPI
) -> None:
    app.state.helper = FakeHelper(raises=HelperError("no helper is listening"))
    response = await admin.post(f"{SYSTEM}/backup/run")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "device_unavailable"


async def test_run_now_is_refused_to_a_non_admin(client: AsyncClient) -> None:
    response = await client.post(f"{SYSTEM}/backup/run")
    assert response.status_code == 401  # no session at all


async def test_verify_with_no_archives_is_a_clean_ok(admin: AsyncClient) -> None:
    response = await admin.post(f"{SYSTEM}/backup/verify")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["archive_id"] is None
    assert body["outcome"] == "none"


async def test_verify_of_an_unknown_archive_is_not_found(admin: AsyncClient) -> None:
    response = await admin.post(
        f"{SYSTEM}/backup/verify", params={"archive_id": "auditorium-20990101-0000"}
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_verify_of_a_named_archive_clears_an_earlier_untrusted_mark(
    admin: AsyncClient, backup_paths: BackupPaths, db: Database, config: Config
) -> None:
    """How the rig's false mark on 20260927-0301 is cleared on demand."""
    # The API's database is in memory; the archive is built from a small file.
    source_db = backup_paths.staging_dir.parent / "source.db"  # type: ignore[union-attr]
    conn = sqlite3.connect(source_db)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()
    job = BackupJob(
        db,
        dataclasses.replace(backup_paths, db_path=source_db),
        secret=DeviceSecret(os.urandom(32)),
        destinations_provider=_local_only(backup_paths),
    )
    run = await job.run(source="manual")
    assert run.archive_id is not None
    await backup_crud.mark_verified(
        db, run.archive_id, verified_at="x", untrusted=True, reason="is not present at ..."
    )

    response = await admin.post(
        f"{SYSTEM}/backup/verify", params={"archive_id": run.archive_id}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["ok"], body["outcome"], body["destination"]) == (True, "verified", "local")

    history = (await admin.get(f"{SYSTEM}/backup/history")).json()["archives"]
    (row,) = [a for a in history if a["id"] == run.archive_id]
    assert row["untrusted"] is False and row["untrusted_reason"] is None
    assert row["checked_destinations"] == ["local"]
    assert row["checked_at"] is not None


async def test_history_and_download(
    admin: AsyncClient, backup_paths: BackupPaths, db: Database
) -> None:
    paths = backup_paths
    paths.local_dir.mkdir(parents=True, exist_ok=True)
    (paths.local_dir / "auditorium-20260920-0300.tar.zst").write_bytes(b"archive-bytes")
    await backup_crud.record_archive(
        db,
        archive_id="auditorium-20260920-0300",
        created_at="2026-09-20T03:00:00+12:00",
        source="scheduled",
        size_bytes=13,
        sha256="a" * 64,
        schema_version=8,
        app_version="0.1.0",
        local_present=True,
        usb_present=False,
        network_present=False,
    )

    history = await admin.get(f"{SYSTEM}/backup/history")
    assert history.status_code == 200, history.text
    assert [a["id"] for a in history.json()["archives"]] == ["auditorium-20260920-0300"]
    (row,) = history.json()["archives"]
    assert row["checked_at"] is None  # recorded without an after-backup check
    assert row["checked_destinations"] == []

    download = await admin.get(f"{SYSTEM}/backup/auditorium-20260920-0300/download")
    assert download.status_code == 200, download.text
    assert download.content == b"archive-bytes"


async def test_download_of_an_unreachable_archive_is_device_unavailable(
    admin: AsyncClient, db: Database
) -> None:
    await backup_crud.record_archive(
        db,
        archive_id="gone",
        created_at="2026-09-20T03:00:00+12:00",
        source="scheduled",
        size_bytes=1,
        sha256="a" * 64,
        schema_version=8,
        app_version="0.1.0",
        local_present=False,
        usb_present=False,
        network_present=False,
    )
    response = await admin.get(f"{SYSTEM}/backup/gone/download")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "device_unavailable"


async def test_download_of_an_unknown_archive_is_not_found(admin: AsyncClient) -> None:
    response = await admin.get(f"{SYSTEM}/backup/no-such-archive/download")
    assert response.status_code == 404


# -- snapshots (§18 Phase 7) -----------------------------------------------------------


async def test_snapshots_is_empty_when_none_have_been_taken(admin: AsyncClient) -> None:
    response = await admin.get(f"{SYSTEM}/backup/snapshots")
    assert response.status_code == 200, response.text
    assert response.json()["snapshots"] == []


async def test_snapshots_lists_a_sidecar_and_a_bare_file_newest_first(
    admin: AsyncClient, config: Config
) -> None:
    """A snapshot with its sidecar (core/snapshots.py's own shape) shows
    when, why and by whom; an older one from before the sidecar existed —
    the pre-update-/pre-restore- files that predate it — still shows up,
    with what little the bare file can say — its name and size, nothing
    invented."""
    directory = snapshots_dir(config.app.data_dir)
    directory.mkdir(parents=True, exist_ok=True)

    with_sidecar = directory / "pre-change-20260925-090000.db"
    with_sidecar.write_bytes(b"sqlite-bytes")
    (directory / "pre-change-20260925-090000.json").write_text(
        json.dumps(
            {
                "snapshot": with_sidecar.name,
                "reason": "delete scene 3",
                "actor": "admin",
                "ip_address": "10.2.30.10",
                "taken_at": "2026-09-25T09:00:00+12:00",
                "app_version": "0.1.4",
                "size_bytes": len(b"sqlite-bytes"),
                "duration_ms": 42.5,
            }
        ),
        encoding="utf-8",
    )

    bare = directory / "pre-update-20260101-030000.db"
    bare.write_bytes(b"older-bytes")
    older = time.time() - 3600  # older mtime: sorts after the one above
    os.utime(bare, (older, older))

    response = await admin.get(f"{SYSTEM}/backup/snapshots")
    assert response.status_code == 200, response.text
    body = response.json()["snapshots"]
    assert [s["name"] for s in body] == [with_sidecar.name, bare.name]

    sidecar_entry = body[0]
    assert sidecar_entry["reason"] == "delete scene 3"
    assert sidecar_entry["actor"] == "admin"
    assert sidecar_entry["ip_address"] == "10.2.30.10"
    assert sidecar_entry["taken_at"] == "2026-09-25T09:00:00+12:00"
    assert sidecar_entry["app_version"] == "0.1.4"
    assert sidecar_entry["duration_ms"] == 42.5
    assert sidecar_entry["size_bytes"] == len(b"sqlite-bytes")

    bare_entry = body[1]
    assert bare_entry["reason"] is None
    assert bare_entry["actor"] is None
    assert bare_entry["ip_address"] is None
    assert bare_entry["taken_at"] is None
    assert bare_entry["app_version"] is None
    assert bare_entry["duration_ms"] is None
    assert bare_entry["size_bytes"] == len(b"older-bytes")


async def test_snapshots_is_admin_only(client: AsyncClient) -> None:
    response = await client.get(f"{SYSTEM}/backup/snapshots")
    assert response.status_code == 401


# -- destinations -------------------------------------------------------------------------


async def test_get_destinations_default_shape(
    admin: AsyncClient, backup_paths: BackupPaths
) -> None:
    response = await admin.get(f"{SYSTEM}/backup/destinations")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["local"] == {"path": str(backup_paths.local_dir), "retention_days": 14}
    assert body["network"]["protocol"] is None
    assert body["network"]["password_set"] is False
    assert body["usb"]["retention_days"] == 7


async def test_put_destinations_stores_smb_and_never_returns_the_password(
    admin: AsyncClient,
) -> None:
    response = await admin.put(
        f"{SYSTEM}/backup/destinations",
        json={
            "protocol": "smb",
            "host": "nas.school.nz",
            "port": 445,
            "path": "backups",
            "username": "proskenion",
            "password": "correct-horse-battery-staple",
            "enabled": True,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["network"]["protocol"] == "smb"
    assert body["network"]["password_set"] is True
    assert "password" not in body["network"]
    assert "correct-horse-battery-staple" not in response.text


async def test_put_destinations_sftp_generates_a_key_and_stores_no_password(
    admin: AsyncClient, config: Config
) -> None:
    response = await admin.put(
        f"{SYSTEM}/backup/destinations",
        json={
            "protocol": "sftp",
            "host": "nas.school.nz",
            "port": 22,
            "path": "/backups",
            "username": "proskenion",
            "enabled": True,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["network"]["protocol"] == "sftp"
    assert body["network"]["password_set"] is False
    private_key = config.app.state_dir / "backup-sftp-key"
    assert private_key.is_file()


async def test_put_destinations_missing_host_is_validation_failed(admin: AsyncClient) -> None:
    response = await admin.put(
        f"{SYSTEM}/backup/destinations",
        json={"protocol": "smb", "path": "backups", "username": "x", "password": "y"},
    )
    assert response.status_code == 422


async def test_sftp_key_endpoint_serves_a_public_key(admin: AsyncClient) -> None:
    response = await admin.get(f"{SYSTEM}/backup/sftp-key")
    assert response.status_code == 200, response.text
    assert response.text.startswith("ssh-ed25519 ")


async def test_sftp_key_is_admin_only(client: AsyncClient) -> None:
    response = await client.get(f"{SYSTEM}/backup/sftp-key")
    assert response.status_code == 401
