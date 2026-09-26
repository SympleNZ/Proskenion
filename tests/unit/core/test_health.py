"""Health polling, backup media and the vitals payload (spec §11.1–§11.3, §4.5).

Two things are being defended here. First, that every colour in the payload
comes from :func:`proskenion.core.vitals.classify` — the tests feed §11.2's
boundary values in and compare the payload's level with ``classify``'s answer
rather than with a colour written out by hand, so a threshold cannot drift
apart from the one the email alerts use. Second, that an unavailable metric is
``None`` with level ``unknown``: the ``DevelopmentPlatform``, where almost
nothing can be read, is the worked example.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from proskenion.config import Config
from proskenion.core import health as health_module
from proskenion.core.alerts import AlertKind, RecordingAlertSink
from proskenion.core.broadcast import Broadcaster
from proskenion.core.bus import EventBus
from proskenion.core.devices import SLOT_NAMES
from proskenion.core.health import (
    BACKUP_ABSENT_BANNER_AFTER_S,
    BACKUP_BANNER_KEY,
    DISK_BANNER_KEY,
    BackupMediaMonitor,
    HealthPoller,
)
from proskenion.core.platform import (
    DevelopmentPlatform,
    PartitionUsage,
    PowerSource,
    Slot,
    StorageHealth,
)
from proskenion.core.state import DeviceStatusRecord, StateStore
from proskenion.core.vitals import classify
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import security_events


class FakePlatform:
    """A platform whose every reading is set by the test (§5.4)."""

    def __init__(
        self,
        *,
        cpu: float | None = 42.0,
        memory: tuple[int, int] | None = (812_000_000, 8_000_000_000),
        storage: StorageHealth | None = None,
        partitions: list[PartitionUsage] | None = None,
        uptime: float | None = 123456.7,
        data_dir: Path = Path("/data"),
    ) -> None:
        self.cpu = cpu
        self.mem = memory
        self.storage = storage or StorageHealth(
            model="Kingston NV2 256GB",
            health="healthy",
            life_used_percent=0.0,
            temperature_c=42.0,
            media_errors=0,
            partial=False,
            detail=None,
        )
        self.partitions = (
            partitions
            if partitions is not None
            else [PartitionUsage("/data", 64_000_000_000, 6_400_000_000)]
        )
        self.uptime = uptime
        self._data_dir = data_dir

    def name(self) -> str:
        return "Raspberry Pi Compute Module 5"

    async def cpu_temperature(self) -> float | None:
        return self.cpu

    async def storage_health(self) -> StorageHealth:
        return self.storage

    async def power_source(self) -> PowerSource:
        return "poe"

    def watchdog_device(self) -> Path | None:
        return None

    def boot_config_path(self) -> Path | None:
        return None

    async def active_root_slot(self) -> Slot:
        return "a"

    async def stage_root_slot(self, slot: Slot) -> None:
        return None

    async def confirm_root_slot(self) -> None:
        return None

    async def partition_usage(self) -> list[PartitionUsage]:
        return self.partitions

    async def memory(self) -> tuple[int, int] | None:
        return self.mem

    async def uptime_seconds(self) -> float | None:
        return self.uptime

    def appliance_state_dir(self) -> Path:
        return Path("/srv/appliance")

    def data_dir(self) -> Path:
        return self._data_dir


class FakeSchedule:
    def __init__(self) -> None:
        self.now = 1000.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.now += delay
        await asyncio.sleep(0)


@pytest.fixture
async def bus() -> AsyncIterator[EventBus]:
    bus = EventBus()
    await bus.start()
    try:
        yield bus
    finally:
        await bus.stop()


@pytest.fixture
def state(dev_config: Config, bus: EventBus) -> StateStore:
    return StateStore(dev_config, bus)


@pytest.fixture
def schedule() -> FakeSchedule:
    return FakeSchedule()


def poller(
    platform: FakePlatform | DevelopmentPlatform,
    state: StateStore,
    bus: EventBus,
    db: Database,
    schedule: FakeSchedule,
    **kwargs: object,
) -> HealthPoller:
    return HealthPoller(
        platform,
        state,
        bus,
        db,
        sleep=schedule.sleep,
        clock=schedule.clock,
        **kwargs,  # type: ignore[arg-type]
    )


# -- backup media (§4.5) --------------------------------------------------------


def monitor(state: StateStore, schedule: FakeSchedule, present: list[bool]) -> BackupMediaMonitor:
    return BackupMediaMonitor(
        state,
        probe=lambda: present[0],
        sleep=schedule.sleep,
        clock=schedule.clock,
    )


async def test_absent_backup_media_is_amber_never_red(
    state: StateStore, schedule: FakeSchedule
) -> None:
    present = [False]
    media = monitor(state, schedule, present)
    await media.poll()
    reading = media.health()
    assert reading.present is False
    assert reading.level == "amber"
    assert reading.absent_since is not None


async def test_backup_media_is_not_in_the_operator_status_bar(
    state: StateStore, schedule: FakeSchedule
) -> None:
    """§4.5: it surfaces on the health screen, never in the status bar."""
    present = [False]
    await monitor(state, schedule, present).poll()
    # The status bar renders one slot per ``state.devices`` record (§21.7); the
    # backup media never becomes one, and it is not a device category either.
    assert BACKUP_BANNER_KEY not in state.devices.records()
    assert BACKUP_BANNER_KEY not in set(SLOT_NAMES.values())


async def test_the_media_failed_alert_is_single_sourced_with_the_backup_watcher(
    state: StateStore, schedule: FakeSchedule, db: Database
) -> None:
    """Carry-forward 6 (phase-7 plan): this monitor and
    ``backup.BackupStatusWatcher`` can each independently decide "both
    backup destinations are unavailable" — one from 48 hours of live USB
    absence plus a known-down network destination, the other from last
    night's job. Before this fix each kept its own in-memory "already sent"
    flag, so both could email about the very same outage. Persisted and
    shared, only the first to notice sends it.
    """
    from proskenion.core.backup import mark_media_alert_sent

    # Stand in for BackupStatusWatcher already having sent the alert for
    # this same outage (e.g. from last night's job).
    await mark_media_alert_sent(db)

    present = [False]
    sink = RecordingAlertSink()
    media = BackupMediaMonitor(
        state,
        probe=lambda: present[0],
        sleep=schedule.sleep,
        clock=schedule.clock,
        network_status=lambda: _async_false(),
        alert_sink=sink,
        db=db,
    )
    await media.poll()
    schedule.now += BACKUP_ABSENT_BANNER_AFTER_S
    await media.poll()

    reading = media.health()
    assert reading.level == "red", "the banner itself still escalates"
    assert sink.sent == [], "a monitor that finds the alert already sent must not resend it"


async def test_a_restart_mid_outage_does_not_resend_the_media_failed_alert(
    state: StateStore, schedule: FakeSchedule, db: Database
) -> None:
    """The persisted flag also survives this monitor's own restart, not
    only a hand-off to the other monitor."""
    present = [False]
    sink = RecordingAlertSink()
    media = BackupMediaMonitor(
        state,
        probe=lambda: present[0],
        sleep=schedule.sleep,
        clock=schedule.clock,
        network_status=lambda: _async_false(),
        alert_sink=sink,
        db=db,
    )
    await media.poll()
    schedule.now += BACKUP_ABSENT_BANNER_AFTER_S
    await media.poll()
    assert len(sink.sent) == 1

    # A fresh instance — standing in for a process restart — still finds the
    # media absent and the network still down and must not send again.
    sink2 = RecordingAlertSink()
    restarted = BackupMediaMonitor(
        state,
        probe=lambda: present[0],
        sleep=schedule.sleep,
        clock=schedule.clock,
        network_status=lambda: _async_false(),
        alert_sink=sink2,
        db=db,
    )
    await restarted.poll()
    schedule.now += BACKUP_ABSENT_BANNER_AFTER_S
    await restarted.poll()
    assert sink2.sent == [], "a restarted monitor resent an alert already sent before it started"


async def _async_false() -> bool:
    return False


async def test_banner_appears_only_after_forty_eight_hours(
    state: StateStore, schedule: FakeSchedule
) -> None:
    present = [False]
    media = monitor(state, schedule, present)
    await media.poll()
    schedule.now += BACKUP_ABSENT_BANNER_AFTER_S - 60
    await media.poll()
    assert state.system.banner(BACKUP_BANNER_KEY) is None

    schedule.now += 60
    await media.poll()
    banner = state.system.banner(BACKUP_BANNER_KEY)
    assert banner is not None
    assert banner.level == "amber"


async def test_hot_insert_clears_the_banner_without_a_restart(
    state: StateStore, schedule: FakeSchedule
) -> None:
    present = [False]
    media = monitor(state, schedule, present)
    await media.poll()
    schedule.now += BACKUP_ABSENT_BANNER_AFTER_S
    await media.poll()
    assert state.system.banner(BACKUP_BANNER_KEY) is not None

    present[0] = True  # a udev rule remounts it
    await media.poll()
    assert state.system.banner(BACKUP_BANNER_KEY) is None
    assert media.health().level == "green"
    assert media.absent_since is None


async def test_losing_the_media_never_touches_control_state(
    state: StateStore, schedule: FakeSchedule
) -> None:
    state.register_owner("lighting", "test", allow_multiple=True)
    state.lighting.writer("test").set_item("levels", 7, 82.5)
    present = [False]
    media = monitor(state, schedule, present)
    await media.poll()
    schedule.now += BACKUP_ABSENT_BANNER_AFTER_S
    await media.poll()
    assert state.lighting.get_item("levels", 7) == 82.5


# -- the payload (§16.7) ---------------------------------------------------------


async def test_payload_shape_matches_the_spec(
    state: StateStore, bus: EventBus, db: Database, schedule: FakeSchedule
) -> None:
    device = await devices_crud.create(
        db,
        category="video_matrix",
        driver_key="stub",
        name="Stub video matrix",
        config={"transport": {"type": "loopback"}, "driver": {}},
    )
    state.register_owner("devices", "test")
    state.devices.writer("test").set_record(
        "hdmi", DeviceStatusRecord(status="connected", last_seen=None)
    )
    broadcaster = Broadcaster(state, bus)
    media = monitor(state, schedule, [True])
    await media.poll()
    snapshot = await poller(
        FakePlatform(), state, bus, db, schedule, broadcaster=broadcaster, backup=media
    ).poll()
    payload = snapshot.as_dict()

    assert set(payload) == {
        "platform",
        "version",
        "uptime_seconds",
        "cpu",
        "memory",
        "storage",
        "partitions",
        "backup_media",
        "application",
        "devices",
        "time",
        "overall",
    }
    assert payload["platform"] == "Raspberry Pi Compute Module 5"
    assert payload["cpu"] == {"temperature_c": 42.0, "level": "green"}
    assert payload["memory"] == {
        "used_bytes": 812_000_000,
        "total_bytes": 8_000_000_000,
        "percent": 10.2,
        "level": "green",
    }
    assert payload["storage"] == {
        "model": "Kingston NV2 256GB",
        "health": "healthy",
        "life_used_percent": 0.0,
        "temperature_c": 42.0,
        "media_errors": 0,
        "partial": False,
        "level": "green",
    }
    assert payload["backup_media"] == {"present": True, "absent_since": None, "level": "green"}
    assert payload["application"]["clients"] == 0
    assert payload["application"]["bus"] == {
        "drop_count_window": 0,
        "drop_consecutive_windows": 0,
        "unsubscribed": [],
        "level": "green",
    }
    assert payload["devices"] == [
        {
            "key": "hdmi",
            "name": "Stub video matrix",
            "category": "video_matrix",
            "status": "connected",
            "kind": None,
            "detail": None,
            "last_seen": None,
            "host": None,
            "port": None,
            "protocol": None,
            "latency_ms": None,
            "reconnects": 0,
            "last_error": None,
            "level": "green",
        }
    ]
    assert device.id > 0
    assert payload["time"]["server_time"].endswith(("+12:00", "+13:00"))
    assert payload["overall"] == "green"


async def test_unavailable_metrics_are_null_and_unknown(
    state: StateStore, bus: EventBus, db: Database, schedule: FakeSchedule
) -> None:
    """The development platform: almost nothing is readable, nothing is invented."""
    snapshot = await poller(DevelopmentPlatform(), state, bus, db, schedule).poll()
    payload = snapshot.as_dict()
    assert payload["platform"] == "development"
    assert payload["uptime_seconds"] is None
    assert payload["cpu"] == {"temperature_c": None, "level": "unknown"}
    assert payload["memory"] == {
        "used_bytes": None,
        "total_bytes": None,
        "percent": None,
        "level": "unknown",
    }
    assert payload["storage"]["life_used_percent"] is None
    assert payload["storage"]["media_errors"] is None
    assert payload["storage"]["level"] == "unknown"
    assert payload["partitions"] == []
    assert payload["application"]["loop_lag_p50_ms"] is None
    assert payload["application"]["loop_lag_p99_ms"] is None
    # An unknown never outranks a real reading, so the appliance is not amber
    # merely because a laptop cannot read its own SSD.
    assert payload["overall"] == "green"


# -- §11.2 boundaries flow through classify --------------------------------------


@pytest.mark.parametrize(
    ("cpu", "expected"),
    [(70.0, "green"), (70.1, "amber"), (80.0, "amber"), (80.1, "red")],
)
async def test_cpu_boundaries_come_from_classify(
    state: StateStore,
    bus: EventBus,
    db: Database,
    schedule: FakeSchedule,
    cpu: float,
    expected: str,
) -> None:
    snapshot = await poller(FakePlatform(cpu=cpu), state, bus, db, schedule).poll()
    assert snapshot.cpu.level == classify("cpu_temperature_c", cpu) == expected


async def test_memory_boundary_comes_from_classify(
    state: StateStore, bus: EventBus, db: Database, schedule: FakeSchedule
) -> None:
    platform = FakePlatform(memory=(9_010_000_000, 10_000_000_000))  # 90.1 %
    snapshot = await poller(platform, state, bus, db, schedule).poll()
    assert snapshot.memory.percent == 90.1
    assert snapshot.memory.level == classify("memory_used_percent", 90.1) == "red"


async def test_the_two_data_rows_are_not_competing_thresholds(
    state: StateStore, bus: EventBus, db: Database, schedule: FakeSchedule
) -> None:
    """§11.2: absolute free space is the operational trigger, percentage the colour."""
    total = 64_000_000_000
    # 85 % used, 9.6 GB free: amber by percentage, green by absolute free space.
    platform = FakePlatform(partitions=[PartitionUsage("/data", total, int(total * 0.85))])
    row = (await poller(platform, state, bus, db, schedule).poll()).partitions[0]
    assert row.percent == 85.0
    assert classify("data_free_mb", row.free_bytes / (1024 * 1024)) == "green"
    assert row.level == classify("data_used_percent", 85.0) == "amber"


async def test_low_absolute_free_space_colours_the_data_partition(
    state: StateStore, bus: EventBus, db: Database, schedule: FakeSchedule
) -> None:
    total = 64_000_000_000
    free = 400 * 1024 * 1024  # under §11.2's 500 MB
    platform = FakePlatform(partitions=[PartitionUsage("/data", total, total - free)])
    row = (await poller(platform, state, bus, db, schedule).poll()).partitions[0]
    assert classify("data_free_mb", 400.0) == "amber"
    assert row.level == "red"  # the percentage row is red at 99 % used
    assert row.free_bytes == free


async def test_a_failing_drive_is_red_even_when_its_numbers_are_inside_the_thresholds(
    state: StateStore, bus: EventBus, db: Database, schedule: FakeSchedule
) -> None:
    platform = FakePlatform(
        storage=StorageHealth(
            model="Kingston NV2 256GB",
            health="failing",
            life_used_percent=1.0,
            temperature_c=40.0,
            media_errors=0,
            partial=True,
            detail="smartctl says the drive expects to fail",
        )
    )
    snapshot = await poller(platform, state, bus, db, schedule).poll()
    assert snapshot.storage.level == "red"
    assert snapshot.storage.partial is True
    assert snapshot.overall == "red"


async def test_media_errors_are_amber_from_the_first_one(
    state: StateStore, bus: EventBus, db: Database, schedule: FakeSchedule
) -> None:
    platform = FakePlatform(
        storage=StorageHealth(
            model="Kingston NV2 256GB",
            health="healthy",
            life_used_percent=1.0,
            temperature_c=40.0,
            media_errors=1,
            partial=False,
            detail=None,
        )
    )
    snapshot = await poller(platform, state, bus, db, schedule).poll()
    assert snapshot.storage.level == classify("ssd_media_errors", 1.0) == "amber"


# -- publishing and disk pressure (§11.3) -----------------------------------------


async def test_free_space_is_published_to_the_state_store(
    state: StateStore, bus: EventBus, db: Database, schedule: FakeSchedule
) -> None:
    total = 64_000_000_000
    free = 2_000 * 1024 * 1024
    platform = FakePlatform(partitions=[PartitionUsage("/data", total, total - free)])
    await poller(platform, state, bus, db, schedule).poll()
    assert state.system.disk_free == pytest.approx(2000.0)


async def test_disk_pressure_prunes_and_raises_the_red_banner(
    state: StateStore, bus: EventBus, db: Database, schedule: FakeSchedule
) -> None:
    old = "2020-01-01T00:00:00.000000+13:00"
    for _ in range(3):
        await security_events.insert(db, "login_failed", timestamp=old)
    total = 64_000_000_000
    free = 40 * 1024 * 1024  # below §11.3's 100 MB
    platform = FakePlatform(partitions=[PartitionUsage("/data", total, total - free)])
    monitor_ = poller(platform, state, bus, db, schedule)

    await monitor_.poll()

    async with db.read() as conn:
        cursor = await conn.execute("SELECT count(*) FROM security_events")
        row = await cursor.fetchone()
    assert row is not None and row[0] == 0  # pruned under §11.3's thresholds
    banner = state.system.banner(DISK_BANNER_KEY)
    assert banner is not None
    assert banner.level == "red"


async def test_disk_pressure_prunes_the_snapshots_to_five(
    state: StateStore, bus: EventBus, db: Database, schedule: FakeSchedule, tmp_path: Path
) -> None:
    """§11.3: "pre-change snapshots beyond the most recent 5, against the normal 10"."""
    data_dir = tmp_path / "data"
    directory = data_dir / "backups" / "snapshots"
    directory.mkdir(parents=True)
    names = [f"pre-change-202609{n:02d}-000000.db" for n in range(1, 13)]
    for index, name in enumerate(names):
        (directory / name).write_bytes(b"x")
        os.utime(directory / name, (1_000 + index, 1_000 + index))
    total = 64_000_000_000
    free = 300 * 1024 * 1024  # below §11.3's 500 MB, above its 100 MB
    platform = FakePlatform(
        partitions=[PartitionUsage(data_dir.as_posix(), total, total - free)],
        data_dir=data_dir,
    )

    await poller(platform, state, bus, db, schedule).poll()

    assert sorted(p.name for p in directory.glob("*.db")) == names[7:]


async def test_disk_pressure_raises_the_disk_critical_alert_exactly_once(
    state: StateStore, bus: EventBus, db: Database, schedule: FakeSchedule
) -> None:
    """§11.4: "free space on /data is critically low" — once per new red
    banner, not once per poll while it stays red."""
    sink = RecordingAlertSink()
    total = 64_000_000_000
    platform = FakePlatform(partitions=[PartitionUsage("/data", total, total - 40 * 1024 * 1024)])
    monitor_ = poller(platform, state, bus, db, schedule, alert_sink=sink)

    await monitor_.poll()
    await asyncio.sleep(0)  # the alert is fired with create_task, not awaited inline
    schedule.now += health_module.POLL_INTERVAL_S
    await monitor_.poll()  # still critical: the banner was already raised
    await asyncio.sleep(0)

    assert len(sink.sent) == 1
    assert sink.sent[0].kind == AlertKind.DISK_CRITICAL


async def test_the_red_banner_clears_once_space_returns(
    state: StateStore, bus: EventBus, db: Database, schedule: FakeSchedule
) -> None:
    total = 64_000_000_000
    platform = FakePlatform(partitions=[PartitionUsage("/data", total, total - 40 * 1024 * 1024)])
    monitor_ = poller(platform, state, bus, db, schedule)
    await monitor_.poll()
    assert state.system.banner(DISK_BANNER_KEY) is not None

    platform.partitions = [PartitionUsage("/data", total, total - 8_000_000_000)]
    schedule.now += health_module.POLL_INTERVAL_S
    await monitor_.poll()
    assert state.system.banner(DISK_BANNER_KEY) is None


async def test_current_serves_the_cached_snapshot_within_the_interval(
    state: StateStore, bus: EventBus, db: Database, schedule: FakeSchedule
) -> None:
    poll = poller(FakePlatform(), state, bus, db, schedule)
    first = await poll.poll()
    assert await poll.current() is first  # a screenful of clients is one reading

    schedule.now += health_module.POLL_INTERVAL_S
    assert await poll.current() is not first
