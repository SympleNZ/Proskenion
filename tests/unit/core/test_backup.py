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
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from proskenion.core import backup as backup_module
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
    reconcile_presence,
    run_monthly_verify,
)
from proskenion.core.backup_archive import VerifyResult
from proskenion.core.backup_destinations import (
    BackupDestination,
    DestinationError,
    DestinationName,
    FilesystemDestination,
)
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
        self,
        name: DestinationName,
        *,
        available: bool = True,
        fail_write: bool = False,
        corrupt_read: bool = False,
    ) -> None:
        self.name = name
        self._available = available
        self._fail_write = fail_write
        # Hands back different bytes from what was written: a copy that
        # does not read back as written (the verify-after-write check).
        self._corrupt_read = corrupt_read
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
        if filename not in self._store:
            raise DestinationError(f"{filename} is not held by {self.name}")
        data = self._store[filename]
        if self._corrupt_read:
            data = data + b"!"
        await asyncio.to_thread(local_path.write_bytes, data)


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


def _fs_destinations(tmp_path: Path) -> dict[DestinationName, BackupDestination]:
    """Real directory destinations under ``tmp_path`` — "local" and a "usb"
    that is present exactly while its directory exists (the real
    :class:`~proskenion.core.backup_destinations.UsbDestination` asks for a
    mount point, which a temporary directory never is)."""
    (tmp_path / "local").mkdir(exist_ok=True)
    (tmp_path / "usb").mkdir(exist_ok=True)
    return {
        "local": FilesystemDestination("local", tmp_path / "local"),
        "usb": FilesystemDestination("usb", tmp_path / "usb"),
    }


def _provider(
    destinations: dict[DestinationName, BackupDestination],
) -> Callable[[], Awaitable[dict[DestinationName, BackupDestination]]]:
    async def provider() -> dict[DestinationName, BackupDestination]:
        return destinations

    return provider


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
    db: Database, paths: BackupPaths, secret: DeviceSecret, state: StateStore, tmp_path: Path
) -> None:
    """A copy that is actually read and fails its checksum is corrupt: that,
    and only that, marks the archive untrusted (§13.4)."""
    destinations = _fs_destinations(tmp_path)
    job = _job(db, paths, secret, destinations)
    status = await job.run(source="scheduled")
    assert status.archive_id is not None

    # Flip bytes in both copies: each is present, each is read, each is bad.
    for name in ("local", "usb"):
        copy = tmp_path / name / f"{status.archive_id}.tar.zst"
        data = bytearray(copy.read_bytes())
        data[len(data) // 2] ^= 0xFF
        copy.write_bytes(bytes(data))

    sink = RecordingAlertSink()
    watcher = BackupStatusWatcher(db, state, sink)
    verify_status = await run_monthly_verify(
        db, paths, destinations_provider=_provider(destinations)
    )
    assert verify_status.ok is False
    assert verify_status.outcome == "untrusted"
    assert verify_status.destination == "local"
    assert "checksum mismatch" in verify_status.detail
    await watcher.poll()

    assert state.system.banner(BACKUP_UNTRUSTED_KEY) is not None
    assert [a.kind for a in sink.sent] == [AlertKind.BACKUP_UNTRUSTED]

    row = await backup_crud.get_archive(db, status.archive_id)
    assert row is not None
    assert row.untrusted is True
    assert row.local_present and row.usb_present  # present, just bad


async def test_verify_with_no_archives_is_a_clean_no_op(
    db: Database, paths: BackupPaths
) -> None:
    status = await run_monthly_verify(db, paths)
    assert status.ok is True
    assert status.archive_id is None


# -- the CM5, 1 October 2026: a stale index is not a corrupt archive -----------------------


async def _two_nights(
    db: Database,
    paths: BackupPaths,
    secret: DeviceSecret,
    destinations: dict[DestinationName, BackupDestination],
) -> tuple[str, str]:
    """Two archives, a night apart, each written to every destination given."""
    ids: list[str] = []
    for night in (0, 1):
        job = _job(db, paths, secret, destinations, now=NOW + timedelta(days=night))
        status = await job.run(source="scheduled")
        assert status.job_result == "success" and status.archive_id is not None
        ids.append(status.archive_id)
    return ids[0], ids[1]


def _reimage_local(tmp_path: Path) -> None:
    """28 September 2026: /srv/local recreated empty; /data (the index) kept."""
    for child in (tmp_path / "local").iterdir():
        child.unlink()


def _pick(the_id: str) -> Callable[[list[backup_crud.ArchiveRow]], backup_crud.ArchiveRow]:
    def choose(rows: list[backup_crud.ArchiveRow]) -> backup_crud.ArchiveRow:
        return next(r for r in rows if r.id == the_id)

    return choose


async def _no_reconcile(*args: object, **kwargs: object) -> None:
    raise RuntimeError("reconcile disabled for this test")


async def test_the_rigs_scenario_verifies_from_the_usb_and_clears_the_stale_local_flag(
    db: Database, paths: BackupPaths, secret: DeviceSecret, state: StateStore, tmp_path: Path
) -> None:
    destinations = _fs_destinations(tmp_path)
    older, _ = await _two_nights(db, paths, secret, destinations)
    _reimage_local(tmp_path)

    sink = RecordingAlertSink()
    watcher = BackupStatusWatcher(db, state, sink)
    status = await run_monthly_verify(
        db, paths, random_choice=_pick(older), destinations_provider=_provider(destinations)
    )

    assert status.ok is True
    assert status.outcome == "verified"
    assert status.destination == "usb"
    row = await backup_crud.get_archive(db, older)
    assert row is not None
    assert row.local_present is False and row.usb_present is True
    assert row.untrusted is False and row.verified_at is not None
    await watcher.poll()
    assert sink.sent == []
    assert state.system.banner(BACKUP_UNTRUSTED_KEY) is None


async def test_fetch_falls_back_to_the_usb_when_local_is_flagged_but_empty(
    db: Database,
    paths: BackupPaths,
    secret: DeviceSecret,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback holds on its own, without the reconcile having run first."""
    destinations = _fs_destinations(tmp_path)
    older, _ = await _two_nights(db, paths, secret, destinations)
    _reimage_local(tmp_path)
    monkeypatch.setattr(backup_module, "reconcile_presence", _no_reconcile)

    status = await run_monthly_verify(
        db, paths, archive_id=older, destinations_provider=_provider(destinations)
    )

    assert (status.ok, status.outcome, status.destination) == (True, "verified", "usb")
    row = await backup_crud.get_archive(db, older)
    assert row is not None and row.local_present is False and row.untrusted is False


async def test_a_successful_verify_clears_an_earlier_false_untrusted_mark_and_its_banner(
    db: Database, paths: BackupPaths, secret: DeviceSecret, state: StateStore, tmp_path: Path
) -> None:
    """What the rig has now: 20260927-0301 marked untrusted because its local
    file was gone, and the banner up. Checking that archive again clears both."""
    destinations = _fs_destinations(tmp_path)
    older, _ = await _two_nights(db, paths, secret, destinations)
    _reimage_local(tmp_path)
    old_reason = f"{older}.tar.zst is not present at /srv/local/backups"
    await backup_crud.mark_verified(
        db, older, verified_at="2026-10-01T04:00:00+13:00", untrusted=True, reason=old_reason
    )
    await backup_module._write_json(
        db,
        backup_module.KEY_VERIFY,
        {"verified_at": "2026-10-01T04:00:00+13:00", "archive_id": older, "ok": False,
         "detail": old_reason},
    )
    sink = RecordingAlertSink()
    watcher = BackupStatusWatcher(db, state, sink)
    await watcher.poll()
    assert state.system.banner(BACKUP_UNTRUSTED_KEY) is not None  # the rig, today

    status = await run_monthly_verify(
        db,
        paths,
        archive_id=older,
        now=lambda: NOW + timedelta(days=12),
        destinations_provider=_provider(destinations),
    )
    await watcher.poll()

    assert status.ok is True
    row = await backup_crud.get_archive(db, older)
    assert row is not None and row.untrusted is False and row.untrusted_reason is None
    assert state.system.banner(BACKUP_UNTRUSTED_KEY) is None


async def test_an_archive_missing_everywhere_is_its_own_outcome_never_untrusted(
    db: Database,
    paths: BackupPaths,
    secret: DeviceSecret,
    state: StateStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destinations = _fs_destinations(tmp_path)
    older, newer = await _two_nights(db, paths, secret, destinations)
    for name in ("local", "usb"):
        (tmp_path / name / f"{older}.tar.zst").unlink()
    monkeypatch.setattr(backup_module, "reconcile_presence", _no_reconcile)

    sink = RecordingAlertSink()
    watcher = BackupStatusWatcher(db, state, sink)
    status = await run_monthly_verify(
        db, paths, archive_id=older, destinations_provider=_provider(destinations)
    )
    await watcher.poll()

    assert status.ok is False
    assert status.outcome == "missing"
    assert "not present at any destination" in status.detail
    assert await backup_crud.get_archive(db, older) is None  # present nowhere: dropped
    assert await backup_crud.get_archive(db, newer) is not None
    assert [a.kind for a in sink.sent] == [AlertKind.BACKUP_MISSING]
    assert sink.sent[0].subject == "A backup archive is missing from every destination"
    assert "Nothing was found to be corrupt" in sink.sent[0].body
    assert state.system.banner(BACKUP_UNTRUSTED_KEY) is None


async def test_an_unreachable_usb_keeps_its_flag_and_marks_nothing(
    db: Database,
    paths: BackupPaths,
    secret: DeviceSecret,
    state: StateStore,
    tmp_path: Path,
) -> None:
    destinations = _fs_destinations(tmp_path)
    older, _ = await _two_nights(db, paths, secret, destinations)
    _reimage_local(tmp_path)
    (tmp_path / "usb").rename(tmp_path / "usb-unplugged")  # the stick is out

    sink = RecordingAlertSink()
    watcher = BackupStatusWatcher(db, state, sink)
    status = await run_monthly_verify(
        db, paths, archive_id=older, destinations_provider=_provider(destinations)
    )
    await watcher.poll()

    assert status.ok is False
    assert status.outcome == "unreachable"
    row = await backup_crud.get_archive(db, older)
    assert row is not None
    assert row.local_present is False  # reachable, listed, not there: cleared
    assert row.usb_present is True  # unreachable is not missing: kept
    assert row.untrusted is False and row.verified_at is None
    assert sink.sent == []
    assert state.system.banner(BACKUP_UNTRUSTED_KEY) is None


async def test_verify_prefers_an_archive_a_reachable_destination_was_just_seen_holding(
    db: Database, paths: BackupPaths, secret: DeviceSecret, tmp_path: Path
) -> None:
    destinations = _fs_destinations(tmp_path)
    older, newer = await _two_nights(db, paths, secret, destinations)
    # older is only on the USB, which is now out; newer is still local.
    (tmp_path / "local" / f"{older}.tar.zst").unlink()
    (tmp_path / "local" / f"{older}.tar.zst.sha256").unlink()
    (tmp_path / "usb").rename(tmp_path / "usb-unplugged")
    offered: list[list[str]] = []

    def record(rows: list[backup_crud.ArchiveRow]) -> backup_crud.ArchiveRow:
        offered.append([r.id for r in rows])
        return rows[0]

    status = await run_monthly_verify(
        db, paths, random_choice=record, destinations_provider=_provider(destinations)
    )

    assert offered == [[newer]]
    assert status.ok is True and status.archive_id == newer


async def test_verify_of_an_unknown_archive_id_raises_lookup_error(
    db: Database, paths: BackupPaths, tmp_path: Path
) -> None:
    with pytest.raises(LookupError):
        await run_monthly_verify(
            db,
            paths,
            archive_id="auditorium-20990101-0000",
            destinations_provider=_provider(_fs_destinations(tmp_path)),
        )


# -- reconciling the index with the destinations -----------------------------------------


async def test_reconcile_clears_and_sets_flags_only_at_reachable_destinations(
    db: Database, paths: BackupPaths, secret: DeviceSecret, tmp_path: Path
) -> None:
    destinations = _fs_destinations(tmp_path)
    older, newer = await _two_nights(db, paths, secret, destinations)
    _reimage_local(tmp_path)
    # newer's USB flag lies the other way: the file is there, the flag says not.
    await backup_crud.set_presence(db, newer, "usb", False)

    result = await reconcile_presence(db, destinations, scratch_dir=tmp_path / "scratch")

    assert result.reachable == {"local", "usb"}
    assert set(result.cleared) == {(older, "local"), (newer, "local")}
    assert result.set == ((newer, "usb"),)
    for the_id in (older, newer):
        row = await backup_crud.get_archive(db, the_id)
        assert row is not None and row.local_present is False and row.usb_present is True

    # With the stick out, its flags are left exactly as they are.
    (tmp_path / "usb").rename(tmp_path / "usb-unplugged")
    await backup_crud.set_presence(db, older, "usb", True)
    result = await reconcile_presence(db, destinations, scratch_dir=tmp_path / "scratch")
    assert result.reachable == {"local"}
    assert result.cleared == () and result.set == ()
    row = await backup_crud.get_archive(db, older)
    assert row is not None and row.usb_present is True


async def test_reconcile_adopts_an_orphan_its_sidecar_vouches_for(
    db: Database, paths: BackupPaths, secret: DeviceSecret, tmp_path: Path
) -> None:
    """20260928-0304 on the rig: on the USB with a good sidecar, no row at all."""
    destinations = _fs_destinations(tmp_path)
    older, newer = await _two_nights(db, paths, secret, destinations)
    original = await backup_crud.get_archive(db, newer)
    assert original is not None
    async with db.write() as conn:
        await conn.execute("DELETE FROM backup_archives WHERE id = ?", (newer,))
    for suffix in (".tar.zst", ".tar.zst.sha256"):
        (tmp_path / "local" / f"{newer}{suffix}").unlink()  # only the USB has it

    result = await reconcile_presence(db, destinations, scratch_dir=tmp_path / "scratch")

    assert result.adopted == (newer,)
    adopted = await backup_crud.get_archive(db, newer)
    assert adopted is not None
    assert adopted.sha256 == original.sha256
    assert adopted.size_bytes == original.size_bytes
    assert adopted.created_at == original.created_at  # the manifest's own
    assert adopted.schema_version == original.schema_version  # read from the manifest
    assert adopted.app_version == original.app_version
    assert adopted.source == "scheduled"
    assert (adopted.local_present, adopted.usb_present, adopted.network_present) == (
        False,
        True,
        False,
    )
    assert adopted.untrusted is False and adopted.checked_at is None
    assert (await backup_crud.get_archive(db, older)) is not None


async def test_reconcile_refuses_an_orphan_without_a_sidecar_or_with_a_wrong_one(
    db: Database, paths: BackupPaths, secret: DeviceSecret, tmp_path: Path
) -> None:
    destinations = _fs_destinations(tmp_path)
    older, newer = await _two_nights(db, paths, secret, destinations)
    async with db.write() as conn:
        await conn.execute("DELETE FROM backup_archives")
    _reimage_local(tmp_path)
    usb = tmp_path / "usb"
    (usb / f"{older}.tar.zst.sha256").unlink()  # no sidecar
    (usb / f"{newer}.tar.zst.sha256").write_text(f"{'0' * 64}  {newer}.tar.zst\n")  # wrong

    result = await reconcile_presence(db, destinations, scratch_dir=tmp_path / "scratch")

    assert result.adopted == ()
    reasons = dict(result.refused)
    assert "sidecar" in reasons[older]
    assert "does not match its sidecar" in reasons[newer]
    assert await backup_crud.all_archives(db) == []
    # Nothing is deleted on a guess.
    assert (usb / f"{older}.tar.zst").is_file() and (usb / f"{newer}.tar.zst").is_file()


async def test_reconcile_drops_a_row_it_finds_held_nowhere(
    db: Database, paths: BackupPaths, secret: DeviceSecret, tmp_path: Path
) -> None:
    """20260925-0812 on the rig: local gone, never on the USB."""
    destinations = _fs_destinations(tmp_path)
    older, newer = await _two_nights(db, paths, secret, destinations)
    await backup_crud.set_presence(db, older, "usb", False)
    for suffix in (".tar.zst", ".tar.zst.sha256"):
        (tmp_path / "usb" / f"{older}{suffix}").unlink()
        (tmp_path / "local" / f"{older}{suffix}").unlink()

    result = await reconcile_presence(db, destinations, scratch_dir=tmp_path / "scratch")

    assert result.removed == (older,)
    assert await backup_crud.get_archive(db, older) is None
    assert await backup_crud.get_archive(db, newer) is not None


async def test_retention_prunes_from_the_reconciled_flags_including_an_adopted_orphan(
    db: Database, paths: BackupPaths, secret: DeviceSecret, tmp_path: Path
) -> None:
    """The nightly job reconciles before it prunes: an orphan past the USB's
    7 days is adopted, then aged out like any other archive."""
    destinations = _fs_destinations(tmp_path)
    old_job = _job(db, paths, secret, destinations, now=NOW - timedelta(days=10))
    old = await old_job.run(source="scheduled")
    assert old.archive_id is not None
    async with db.write() as conn:
        await conn.execute("DELETE FROM backup_archives WHERE id = ?", (old.archive_id,))
    for suffix in (".tar.zst", ".tar.zst.sha256"):
        (tmp_path / "local" / f"{old.archive_id}{suffix}").unlink()

    tonight = await _job(db, paths, secret, destinations).run(source="scheduled")

    assert tonight.job_result == "success"
    assert not (tmp_path / "usb" / f"{old.archive_id}.tar.zst").exists()  # pruned (7 days)
    assert await backup_crud.get_archive(db, old.archive_id) is None
    assert tonight.archive_id is not None
    row = await backup_crud.get_archive(db, tonight.archive_id)
    assert row is not None and row.local_present and row.usb_present


# -- verify-after-write: every run checks its own copies ----------------------------------


async def test_every_copy_is_read_back_and_the_check_is_recorded(
    db: Database, paths: BackupPaths, secret: DeviceSecret
) -> None:
    destinations: dict[DestinationName, BackupDestination] = {
        "local": FakeDestination("local"),
        "usb": FakeDestination("usb"),
        "network": FakeDestination("network"),
    }
    status = await _job(db, paths, secret, destinations).run(source="manual")

    assert status.job_result == "success"
    row = await backup_crud.get_archive(db, status.archive_id or "")
    assert row is not None
    assert row.checked_at == NOW.isoformat(timespec="seconds")
    assert row.checked_destinations == ("local", "usb", "network")
    assert row.verified_at is None  # the monthly check is a separate record


async def test_a_copy_that_does_not_read_back_is_that_destinations_failure(
    db: Database, paths: BackupPaths, secret: DeviceSecret
) -> None:
    usb = FakeDestination("usb", corrupt_read=True)
    destinations: dict[DestinationName, BackupDestination] = {
        "local": FakeDestination("local"),
        "usb": usb,
        "network": FakeDestination("network"),
    }
    status = await _job(db, paths, secret, destinations).run(source="scheduled")

    assert status.job_result == "success"  # local held
    assert status.destinations["usb"].ok is False
    assert "does not read back as written" in (status.destinations["usb"].reason or "")
    assert status.destinations["network"].ok is True
    row = await backup_crud.get_archive(db, status.archive_id or "")
    assert row is not None
    assert row.usb_present is False and row.local_present and row.network_present
    assert row.checked_destinations == ("local", "network")
    assert await usb.list_names() == []  # the bad copy was taken away again


async def test_a_local_copy_that_does_not_read_back_fails_the_backup(
    db: Database, paths: BackupPaths, secret: DeviceSecret
) -> None:
    destinations: dict[DestinationName, BackupDestination] = {
        "local": FakeDestination("local", corrupt_read=True)
    }
    sleeps: list[float] = []
    status = await _job(db, paths, secret, destinations, sleeps=sleeps).run(source="scheduled")

    assert status.job_result == "failed"
    assert status.retried is True and sleeps == [600.0]
    assert "does not read back as written" in (status.job_detail or "")
    assert await backup_crud.all_archives(db) == []


async def test_an_archive_that_fails_its_own_integrity_check_fails_the_backup(
    db: Database,
    paths: BackupPaths,
    secret: DeviceSecret,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def bad(*args: object, **kwargs: object) -> VerifyResult:
        return VerifyResult(
            ok=False,
            checksum_ok=True,
            integrity_ok=False,
            detail="database disk image is malformed",
        )

    monkeypatch.setattr(backup_module, "verify_archive", bad)
    local = FakeDestination("local")
    status = await _job(db, paths, secret, {"local": local}).run(source="manual")

    assert status.job_result == "failed"
    assert "failed its own check" in (status.job_detail or "")
    assert "malformed" in (status.job_detail or "")
    assert local.written == []  # nothing distributed
    assert await backup_crud.all_archives(db) == []
