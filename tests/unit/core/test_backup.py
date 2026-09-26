"""The nightly job's escalation ladder, and the app-side watcher (§13.4, §4.5).

``BackupJob`` is exercised against fake destinations (:class:`FakeDestination`)
injected through its ``destinations_provider`` seam — real local/USB/SMB/SFTP
destinations are proved in ``tests/unit/core/test_backup_destinations.py``
and ``tests/integration/backup/`` (Docker); what matters here is the ladder
itself: retry-then-amber-then-red, that absent media never escalates it, and
that "both destinations unavailable" is its own, separate alarm.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from proskenion.core import retention
from proskenion.core.alerts import AlertKind, RecordingAlertSink
from proskenion.core.backup import (
    BACKUP_FAILED_AMBER_KEY,
    BACKUP_FAILED_RED_KEY,
    BACKUP_UNTRUSTED_KEY,
    NOT_CONFIGURED,
    RED_AFTER_NIGHTS,
    BackupJob,
    BackupPaths,
    BackupStatusWatcher,
    network_destination_status,
    read_status,
    run_monthly_verify,
)
from proskenion.core.backup_destinations import BackupDestination, DestinationError, DestinationName
from proskenion.core.bus import EventBus
from proskenion.core.secrets import DeviceSecret
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import backup as backup_crud
from proskenion.db.crud import security_events
from proskenion.db.crud.base import AUCKLAND

NOW = datetime(2026, 9, 20, 3, 0, 0, tzinfo=AUCKLAND)


class FakeDestination:
    """A :class:`~proskenion.core.backup_destinations.BackupDestination` a
    test can make absent or make fail, and can inspect afterwards."""

    def __init__(
        self, name: DestinationName, *, available: bool = True, fail_write: bool = False
    ) -> None:
        self.name = name
        self._available = available
        self._fail_write = fail_write
        self.written: list[str] = []
        self.deleted: list[str] = []
        self._store: dict[str, bytes] = {}

    async def available(self) -> bool:
        return self._available

    async def write(self, local_path: Path, filename: str) -> None:
        if self._fail_write:
            raise DestinationError(f"{self.name} refused to write {filename}")
        self._store[filename] = await asyncio.to_thread(local_path.read_bytes)
        self.written.append(filename)

    async def delete(self, filename: str) -> None:
        self._store.pop(filename, None)
        self.deleted.append(filename)

    async def list_names(self) -> list[str]:
        return list(self._store)

    async def read(self, filename: str, local_path: Path) -> None:
        await asyncio.to_thread(local_path.write_bytes, self._store[filename])


@pytest.fixture
async def bus() -> AsyncIterator[EventBus]:
    bus = EventBus()
    await bus.start()
    try:
        yield bus
    finally:
        await bus.stop()


@pytest.fixture
def state(dev_config: object, bus: EventBus) -> StateStore:
    return StateStore(dev_config, bus)  # type: ignore[arg-type]


@pytest.fixture
def paths(tmp_path: Path) -> BackupPaths:
    db_path = tmp_path / "db" / "auditorium.db"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()

    data_dir = tmp_path / "data"
    (data_dir / "config").mkdir(parents=True)
    (data_dir / "config" / "system.json").write_text("{}")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return BackupPaths(
        db_path=db_path,
        data_dir=data_dir,
        state_dir=state_dir,
        staging_dir=tmp_path / "staging",
    )


@pytest.fixture
def secret(tmp_path: Path) -> DeviceSecret:
    return DeviceSecret(os.urandom(32))


def _job(
    db: Database,
    paths: BackupPaths,
    secret: DeviceSecret,
    destinations: dict[DestinationName, BackupDestination],
    *,
    sleeps: list[float] | None = None,
    now: datetime = NOW,
) -> BackupJob:
    async def provider() -> dict[DestinationName, BackupDestination]:
        return destinations

    async def fake_sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    return BackupJob(
        db,
        paths,
        secret=secret,
        now=lambda: now,
        sleep=fake_sleep,
        destinations_provider=provider,
    )


# -- a normal, successful run ------------------------------------------------------------


async def test_a_successful_run_writes_every_destination_and_records_the_archive(
    db: Database, paths: BackupPaths, secret: DeviceSecret
) -> None:
    destinations: dict[DestinationName, BackupDestination] = {
        "local": FakeDestination("local"),
        "usb": FakeDestination("usb"),
        "network": FakeDestination("network"),
    }
    job = _job(db, paths, secret, destinations)

    status = await job.run(source="scheduled")

    assert status.job_result == "success"
    assert status.consecutive_failures == 0
    assert status.retried is False
    for name in ("local", "usb", "network"):
        outcome = status.destinations[name]
        assert outcome.attempted and outcome.ok

    row = await backup_crud.get_archive(db, status.archive_id or "")
    assert row is not None
    assert row.local_present and row.usb_present and row.network_present
    assert row.source == "scheduled"

    persisted = await read_status(db)
    assert persisted == status


# -- §4.5: absent media is skipped, never a failure --------------------------------------


async def test_absent_usb_is_skipped_and_never_fails_the_job(
    db: Database, paths: BackupPaths, secret: DeviceSecret
) -> None:
    destinations: dict[DestinationName, BackupDestination] = {
        "local": FakeDestination("local"),
        "usb": FakeDestination("usb", available=False),
        "network": FakeDestination("network"),
    }
    job = _job(db, paths, secret, destinations)

    status = await job.run(source="scheduled")

    assert status.job_result == "success"
    assert status.consecutive_failures == 0
    usb = status.destinations["usb"]
    assert usb.attempted is False
    assert usb.ok is None
    assert usb.reason == "media_absent"


async def test_no_network_destination_configured_is_skipped_not_failed(
    db: Database, paths: BackupPaths, secret: DeviceSecret
) -> None:
    destinations: dict[DestinationName, BackupDestination] = {
        "local": FakeDestination("local"),
        "usb": FakeDestination("usb"),
        # No "network" key at all: nothing is configured.
    }
    job = _job(db, paths, secret, destinations)
    status = await job.run(source="scheduled")
    assert status.job_result == "success"
    network = status.destinations["network"]
    assert network.attempted is False
    assert network.reason == NOT_CONFIGURED


# -- §13.4: retry once after ten minutes, then amber, then red after 3 nights ------------


async def test_a_failed_local_write_retries_once_ten_minutes_later(
    db: Database, paths: BackupPaths, secret: DeviceSecret
) -> None:
    destinations: dict[DestinationName, BackupDestination] = {
        "local": FakeDestination("local", fail_write=True),
        "usb": FakeDestination("usb"),
    }
    sleeps: list[float] = []
    job = _job(db, paths, secret, destinations, sleeps=sleeps)

    status = await job.run(source="scheduled")

    assert status.job_result == "failed"
    assert status.retried is True
    assert sleeps == [600.0]  # RETRY_DELAY_S
    assert status.consecutive_failures == 1
    # A hard local failure means nothing else was even attempted.
    assert status.destinations["usb"].attempted is False


async def test_a_manual_run_does_not_retry(
    db: Database, paths: BackupPaths, secret: DeviceSecret
) -> None:
    destinations: dict[DestinationName, BackupDestination] = {
        "local": FakeDestination("local", fail_write=True),
    }
    sleeps: list[float] = []
    job = _job(db, paths, secret, destinations, sleeps=sleeps)

    status = await job.run(source="manual")

    assert status.job_result == "failed"
    assert status.retried is False
    assert sleeps == []


async def test_consecutive_failures_accumulate_and_reset_on_success(
    db: Database, paths: BackupPaths, secret: DeviceSecret
) -> None:
    failing: dict[DestinationName, BackupDestination] = {
        "local": FakeDestination("local", fail_write=True)
    }
    succeeding: dict[DestinationName, BackupDestination] = {"local": FakeDestination("local")}

    for expected in (1, 2, 3):
        job = _job(db, paths, secret, failing)
        status = await job.run(source="scheduled")
        assert status.job_result == "failed"
        assert status.consecutive_failures == expected

    job = _job(db, paths, secret, succeeding)
    status = await job.run(source="scheduled")
    assert status.job_result == "success"
    assert status.consecutive_failures == 0


# -- §15.3: "pruned during the nightly backup job" ----------------------------------------


async def _security_event_timestamps(db: Database) -> list[str]:
    async with db.read() as conn:
        cursor = await conn.execute("SELECT timestamp FROM security_events ORDER BY timestamp")
        return [str(row[0]) for row in await cursor.fetchall()]


def _hold_snapshots(directory: Path, count: int) -> list[str]:
    directory.mkdir(parents=True, exist_ok=True)
    names = [f"pre-change-202609{n:02d}-000000.db" for n in range(1, count + 1)]
    for index, name in enumerate(names):
        (directory / name).write_bytes(b"x")
        os.utime(directory / name, (1_000 + index, 1_000 + index))
    return names


@pytest.mark.parametrize("fail_local", [False, True])
async def test_the_nightly_job_prunes_the_ninety_day_tables_and_the_snapshots(
    db: Database, paths: BackupPaths, secret: DeviceSecret, fail_local: bool
) -> None:
    """Every night, whatever tonight's archive did: retention is about old rows."""
    old = (NOW - timedelta(days=200)).isoformat(timespec="microseconds")
    recent = (NOW - timedelta(days=10)).isoformat(timespec="microseconds")
    await security_events.insert(db, "login_failed", timestamp=old)
    await security_events.insert(db, "login_failed", timestamp=recent)
    directory = paths.data_dir / "backups" / "snapshots"
    names = _hold_snapshots(directory, 12)
    destinations: dict[DestinationName, BackupDestination] = {
        "local": FakeDestination("local", fail_write=fail_local)
    }

    status = await _job(db, paths, secret, destinations).run(source="scheduled")

    assert status.job_result == ("failed" if fail_local else "success")
    assert await _security_event_timestamps(db) == [recent]
    assert sorted(p.name for p in directory.glob("pre-change-*.db")) == names[2:]


async def test_a_prune_that_fails_never_fails_the_backup(
    db: Database,
    paths: BackupPaths,
    secret: DeviceSecret,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def broken(*_: object, **__: object) -> None:
        raise RuntimeError("the snapshots directory is unreadable")

    monkeypatch.setattr(retention, "prune", broken)
    destinations: dict[DestinationName, BackupDestination] = {"local": FakeDestination("local")}

    with caplog.at_level("ERROR", logger="proskenion.core.backup"):
        status = await _job(db, paths, secret, destinations).run(source="scheduled")

    assert status.job_result == "success"
    assert await read_status(db) == status
    assert any("retention pruning failed" in r.getMessage() for r in caplog.records)


# -- the watcher: banners and one email per new failure -----------------------------------


async def test_watcher_raises_amber_then_red_and_clears_on_success(
    db: Database, paths: BackupPaths, secret: DeviceSecret, state: StateStore
) -> None:
    failing: dict[DestinationName, BackupDestination] = {
        "local": FakeDestination("local", fail_write=True)
    }
    sink = RecordingAlertSink()
    watcher = BackupStatusWatcher(db, state, sink)

    for night in range(1, RED_AFTER_NIGHTS + 1):
        job = _job(db, paths, secret, failing, now=NOW + timedelta(days=night))
        await job.run(source="scheduled")
        await watcher.poll()
        if night < RED_AFTER_NIGHTS:
            assert state.system.banner(BACKUP_FAILED_AMBER_KEY) is not None
            assert state.system.banner(BACKUP_FAILED_RED_KEY) is None
        else:
            assert state.system.banner(BACKUP_FAILED_RED_KEY) is not None
            assert state.system.banner(BACKUP_FAILED_AMBER_KEY) is None

    assert [a.kind for a in sink.sent] == [AlertKind.BACKUP_FAILED] * RED_AFTER_NIGHTS

    succeeding: dict[DestinationName, BackupDestination] = {"local": FakeDestination("local")}
    job = _job(db, paths, secret, succeeding)
    await job.run(source="scheduled")
    await watcher.poll()
    assert state.system.banner(BACKUP_FAILED_AMBER_KEY) is None
    assert state.system.banner(BACKUP_FAILED_RED_KEY) is None


async def test_watcher_polls_are_idempotent_for_an_unchanged_result(
    db: Database, paths: BackupPaths, secret: DeviceSecret, state: StateStore
) -> None:
    """A poll that finds the same ``attempted_at`` raises nothing new — the
    watcher must not re-email on every 30-second tick for one failed night."""
    failing: dict[DestinationName, BackupDestination] = {
        "local": FakeDestination("local", fail_write=True)
    }
    sink = RecordingAlertSink()
    watcher = BackupStatusWatcher(db, state, sink)
    job = _job(db, paths, secret, failing)
    await job.run(source="scheduled")

    await watcher.poll()
    await watcher.poll()
    await watcher.poll()

    assert len(sink.sent) == 1


# -- both destinations unavailable: red media alarm, independent of the ladder ------------


async def test_both_destinations_down_raises_media_failed_once(
    db: Database, paths: BackupPaths, secret: DeviceSecret, state: StateStore
) -> None:
    destinations: dict[DestinationName, BackupDestination] = {
        "local": FakeDestination("local"),  # local succeeds: this is not a job failure
        "usb": FakeDestination("usb", available=False),
        "network": FakeDestination("network", fail_write=True),
    }
    sink = RecordingAlertSink()
    watcher = BackupStatusWatcher(db, state, sink)

    job = _job(db, paths, secret, destinations)
    status = await job.run(source="scheduled")
    assert status.job_result == "success"  # local held; the job itself did not fail
    await watcher.poll()

    media_alerts = [a for a in sink.sent if a.kind == AlertKind.MEDIA_FAILED]
    assert len(media_alerts) == 1

    assert await network_destination_status(db) is False

    # A second identical night does not re-send the alert.
    job = _job(db, paths, secret, destinations, now=NOW + timedelta(days=1))
    await job.run(source="scheduled")
    await watcher.poll()
    assert len([a for a in sink.sent if a.kind == AlertKind.MEDIA_FAILED]) == 1


async def test_network_never_configured_is_not_treated_as_down(
    db: Database, paths: BackupPaths, secret: DeviceSecret, state: StateStore
) -> None:
    """A USB-only site must not be paged just because the stick is briefly out."""
    destinations: dict[DestinationName, BackupDestination] = {
        "local": FakeDestination("local"),
        "usb": FakeDestination("usb", available=False),
        # no "network" key: never configured.
    }
    sink = RecordingAlertSink()
    watcher = BackupStatusWatcher(db, state, sink)
    job = _job(db, paths, secret, destinations)
    await job.run(source="scheduled")
    await watcher.poll()

    assert [a for a in sink.sent if a.kind == AlertKind.MEDIA_FAILED] == []
    assert await network_destination_status(db) is None


# -- the monthly verification --------------------------------------------------------------


async def test_verify_marks_a_corrupt_archive_untrusted_and_the_watcher_alerts(
    db: Database, paths: BackupPaths, secret: DeviceSecret, state: StateStore
) -> None:
    destinations: dict[DestinationName, BackupDestination] = {"local": FakeDestination("local")}
    job = _job(db, paths, secret, destinations)
    status = await job.run(source="scheduled")
    assert status.archive_id is not None

    # Corrupt the recorded checksum so the monthly check catches it without
    # needing to hand-corrupt the archive bytes themselves.
    await backup_crud.mark_verified(
        db, status.archive_id, verified_at="x", untrusted=False, reason=None
    )
    async with db.write() as conn:
        await conn.execute(
            "UPDATE backup_archives SET sha256 = ? WHERE id = ?", ("0" * 64, status.archive_id)
        )

    sink = RecordingAlertSink()
    watcher = BackupStatusWatcher(db, state, sink)
    verify_status = await run_monthly_verify(db, paths)
    assert verify_status.ok is False
    await watcher.poll()

    assert state.system.banner(BACKUP_UNTRUSTED_KEY) is not None
    assert [a.kind for a in sink.sent] == [AlertKind.BACKUP_UNTRUSTED]

    row = await backup_crud.get_archive(db, status.archive_id)
    assert row is not None
    assert row.untrusted is True


async def test_verify_with_no_archives_is_a_clean_no_op(
    db: Database, paths: BackupPaths
) -> None:
    status = await run_monthly_verify(db, paths)
    assert status.ok is True
    assert status.archive_id is None
