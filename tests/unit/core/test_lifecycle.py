"""Boot sequence, healthy marker and graceful shutdown (spec §12.1, §12.4, §14.5).

The order of §12.1 is the contract, so these tests assert about the order and
about what boot does *not* wait for, not only that it finishes. The healthy
marker is timed on an injected clock: thirty seconds must be thirty seconds.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from pathlib import Path

import pytest

from proskenion.config import (
    AppSection,
    Config,
    DatabaseSection,
    Environment,
    LoggingSection,
)
from proskenion.core.broadcast import Broadcaster
from proskenion.core.bus import EventBus
from proskenion.core.devices import OWNER as DEVICE_OWNER
from proskenion.core.devices import DeviceManager
from proskenion.core.dmx.desk import DeskInput
from proskenion.core.drivers import load_shipped_drivers
from proskenion.core.events import DeviceStatusChanged, KnxTelegramReceived
from proskenion.core.health import BackupMediaMonitor, HealthPoller
from proskenion.core.lifecycle import (
    HEALTHY_AFTER_S,
    MIN_FREE_BYTES,
    Lifecycle,
    MountUnavailable,
    check_disk_space_sync,
    preflight,
    required_directories,
    verify_mounts_sync,
)
from proskenion.core.lighting import LightingService
from proskenion.core.persist import StatePersister
from proskenion.core.platform import BOOT_STATE_FILENAME, DevelopmentPlatform
from proskenion.core.state import DeviceStatusRecord, StateStore
from proskenion.core.timesync import DEGRADED_BANNER_KEY, TimeSyncMonitor
from proskenion.core.watchdog import WatchdogTask
from proskenion.db.connection import MEMORY, Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import knx as knx_crud
from proskenion.db.crud import lighting as lighting_crud
from proskenion.db.crud import system_state
from proskenion.db.migrations import MigrationFailed, migrate
from tests.unit.core.dmx.conftest import FakeDevices, FakeKnx, wait_until

LOOPBACK = {"transport": {"type": "loopback"}, "driver": {}}
NOT_SYNCED = "NTPSynchronized=no\nRTCTimeUSec=1757490131000000\n"


class FakeSchedule:
    def __init__(self) -> None:
        self.now = 1000.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.now += delay
        await asyncio.sleep(0)


class Notifier:
    """Records the sd_notify states the watchdog sends (§4.7)."""

    def __init__(self) -> None:
        self.states: list[str] = []

    def __call__(self, state: str) -> bool:
        self.states.append(state)
        return True


def unavailable_runner(argv: Sequence[str], timeout_s: float) -> None:
    """A machine with no ``timedatectl``: the boot wait must not take 30 s."""
    raise FileNotFoundError(argv[0])


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        database=DatabaseSection(path=tmp_path / "data" / "db" / "auditorium.db"),
        logging=LoggingSection(path=tmp_path / "data" / "logs"),
        app=AppSection(
            environment=Environment.DEVELOPMENT,
            state_dir=tmp_path / "srv" / "appliance",
            # The markers resolve their version from <data_dir>/app/current
            # (contracts §1), so the data directory has to be this tree and not
            # the production /data the default names.
            data_dir=tmp_path / "data",
        ),
    )


@pytest.fixture
async def bus() -> AsyncIterator[EventBus]:
    bus = EventBus()
    await bus.start()
    try:
        yield bus
    finally:
        await bus.stop()


@pytest.fixture
def state(config: Config, bus: EventBus) -> StateStore:
    return StateStore(config, bus)


@pytest.fixture
def schedule() -> FakeSchedule:
    return FakeSchedule()


@pytest.fixture
def drivers() -> Iterator[None]:
    load_shipped_drivers()
    yield


async def turns(count: int = 50) -> None:
    for _ in range(count):
        await asyncio.sleep(0)


async def settle(predicate: Callable[[], bool], *, timeout_s: float = 5.0) -> None:
    """Wait until ``predicate`` holds, or fail after ``timeout_s``.

    For work that crosses real sockets or the bus's delivery task: a fixed
    number of loop turns is a timing guess, and on a busy machine it loses.
    """
    for _ in range(int(timeout_s / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"condition not met within {timeout_s} s")


async def advance_until(
    predicate: object, schedule: FakeSchedule, *, limit: int = 5000
) -> None:
    """Give the loop turns until ``predicate()`` holds, or give up."""
    assert callable(predicate)
    for _ in range(limit):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"condition never held; clock reached {schedule.now}")


def build(
    config: Config,
    db: Database,
    state: StateStore,
    bus: EventBus,
    schedule: FakeSchedule,
    *,
    devices: DeviceManager | None = None,
    start_devices: bool = False,
    watchdog: WatchdogTask | None = None,
    persister: StatePersister | None = None,
    lighting: LightingService | None = None,
    desk_input: DeskInput | None = None,
    start_lighting: bool = True,
) -> Lifecycle:
    platform = DevelopmentPlatform()
    broadcaster = Broadcaster(state, bus)
    manager = devices or DeviceManager(db, state, bus, config)
    timesync = TimeSyncMonitor(
        state, bus, runner=unavailable_runner, sleep=schedule.sleep, clock=schedule.clock
    )
    backup = BackupMediaMonitor(state, probe=lambda: True, sleep=schedule.sleep)
    health = HealthPoller(platform, state, bus, db, backup=backup, sleep=schedule.sleep)
    return Lifecycle(
        config,
        db,
        state,
        bus,
        broadcaster,
        manager,
        platform=platform,
        timesync=timesync,
        health=health,
        backup=backup,
        watchdog=watchdog,
        persister=persister,
        lighting=lighting,
        desk_input=desk_input,
        start_devices=start_devices,
        start_lighting=start_lighting,
        sleep=schedule.sleep,
        clock=schedule.clock,
    )


async def lit_venue(db: Database) -> tuple[int, int]:
    """A lighting output device with one DMX dimmer patched to it.

    Returns ``(device_id, channel_id)``.
    """
    device = await devices_crud.create(
        db, category="lighting_output", driver_key="artnet", name="eDMX8", config={}
    )
    channel = await lighting_crud.create_channel(
        db, name="Front wash", type="dmx", profile_id=1, device_id=device.id, address=1
    )
    return device.id, channel.id


# -- §12.1 step 1: mounts -------------------------------------------------------


def test_required_directories_are_data_and_the_appliance_state(config: Config) -> None:
    directories = required_directories(config)
    assert Path(config.database.path).parent in directories
    assert Path(config.logging.path) in directories
    assert Path(config.app.state_dir) in directories


def test_verify_mounts_creates_and_probes(config: Config) -> None:
    verify_mounts_sync(config)
    for directory in required_directories(config):
        assert directory.is_dir()
        assert not list(directory.glob(".proskenion-write-probe-*"))  # cleaned up


def test_a_missing_mount_is_the_emergency_signal(config: Config, tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    broken = config.model_copy(
        update={"app": AppSection(state_dir=blocked / "appliance", data_dir=blocked / "data")}
    )
    with pytest.raises(MountUnavailable) as raised:
        verify_mounts_sync(broken)
    assert raised.value.reason == "data_unavailable"  # §16.7's closed set


def test_a_read_only_mount_is_the_emergency_signal(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Path.write_text

    def deny(self: Path, *args: object, **kwargs: object) -> int:
        if self.name.startswith(".proskenion-write-probe"):
            raise PermissionError("Read-only file system")
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", deny)
    with pytest.raises(MountUnavailable) as raised:
        verify_mounts_sync(config)
    assert raised.value.reason == "data_readonly"


def test_a_full_disk_is_the_emergency_signal(config: Config) -> None:
    """§14.5, Q12: below the floor, the application refuses to start."""
    with pytest.raises(MountUnavailable) as raised:
        check_disk_space_sync(config, free_bytes=lambda path: MIN_FREE_BYTES - 1)
    assert raised.value.reason == "disk_full"  # §16.7's closed set
    assert raised.value.path == Path(config.app.data_dir)


def test_free_space_exactly_at_the_floor_is_fine(config: Config) -> None:
    check_disk_space_sync(config, free_bytes=lambda path: MIN_FREE_BYTES)  # not below it


def test_verify_mounts_also_checks_disk_space(config: Config) -> None:
    """The mount check and the space check are one call for every real caller."""
    with pytest.raises(MountUnavailable) as raised:
        verify_mounts_sync(config, free_bytes=lambda path: 0)
    assert raised.value.reason == "disk_full"


def test_disk_space_is_checked_after_the_mounts_themselves(
    config: Config, tmp_path: Path
) -> None:
    """A missing mount is reported as such, not masked by a coincidentally low
    free-space reading for a path that was never going to be checked."""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    broken = config.model_copy(
        update={"app": AppSection(state_dir=blocked / "appliance", data_dir=blocked / "data")}
    )
    with pytest.raises(MountUnavailable) as raised:
        verify_mounts_sync(broken, free_bytes=lambda path: 0)
    assert raised.value.reason == "data_unavailable"


async def test_preflight_runs_migrations(config: Config) -> None:
    await preflight(config)
    db = Database()
    await db.open(config.database.path)
    try:
        async with db.read() as conn:
            cursor = await conn.execute("SELECT count(*) FROM schema_versions")
            row = await cursor.fetchone()
        assert row is not None and row[0] >= 1
    finally:
        await db.close()


async def test_preflight_propagates_a_migration_failure(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(db: Database, *args: object, **kwargs: object) -> list[str]:
        raise MigrationFailed("003_broken.sql", RuntimeError("no such column"))

    monkeypatch.setattr("proskenion.core.lifecycle.migrate", boom)
    with pytest.raises(MigrationFailed):
        await preflight(config)


# -- §12.1: the sequence --------------------------------------------------------


async def test_boot_detects_the_platform_restores_state_and_serves(
    config: Config, db: Database, state: StateStore, bus: EventBus, schedule: FakeSchedule
) -> None:
    await system_state.set(db, "lighting", "levels", json.dumps({"7": 82.5}))
    lifecycle = build(config, db, state, bus, schedule)

    await lifecycle.boot()
    try:
        assert lifecycle.platform.name() == "development"
        assert lifecycle.serving is True
        assert lifecycle.restored == {"lighting": ["levels"]}
        assert state.lighting.get_item("levels", "7") == 82.5
        assert lifecycle.persister is not None and lifecycle.persister.running
    finally:
        await lifecycle.shutdown()


async def test_boot_detects_the_platform_with_the_configured_dirs_when_none_is_injected(
    config: Config,
    db: Database,
    state: StateStore,
    bus: EventBus,
    schedule: FakeSchedule,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5.4, phase-1 milestone finding 5.

    ``build()`` above injects a platform so tests stay OS-independent; a real
    boot leaves it unset and calls :func:`detect_platform`, which must be
    given ``config.app.state_dir`` and ``config.app.data_dir`` — otherwise a
    development machine's :class:`DevelopmentPlatform` falls back to the real,
    hard-coded ``/srv/appliance`` and ``/data`` (``C:\\data`` on Windows).
    """
    calls: list[dict[str, object]] = []

    def spy(**kwargs: object) -> DevelopmentPlatform:
        calls.append(kwargs)
        return DevelopmentPlatform(
            appliance_dir=kwargs.get("appliance_dir"),  # type: ignore[arg-type]
            data_dir=kwargs.get("data_dir"),  # type: ignore[arg-type]
        )

    monkeypatch.setattr("proskenion.core.lifecycle.detect_platform", spy)

    lifecycle = build(config, db, state, bus, schedule)
    lifecycle._platform = None  # as a real boot leaves it, before detection
    await lifecycle.boot()
    try:
        assert calls == [
            {"appliance_dir": Path(config.app.state_dir), "data_dir": Path(config.app.data_dir)}
        ]
        assert lifecycle.platform.appliance_state_dir() == Path(config.app.state_dir)
        assert lifecycle.platform.data_dir() == Path(config.app.data_dir)
    finally:
        await lifecycle.shutdown()


async def test_boot_starts_degraded_when_the_clock_cannot_be_verified(
    config: Config, db: Database, state: StateStore, bus: EventBus, schedule: FakeSchedule
) -> None:
    """§4.9: it starts anyway, with the banner up and the retry running."""
    lifecycle = build(config, db, state, bus, schedule)
    await lifecycle.boot()
    try:
        assert lifecycle.timesync is not None
        assert lifecycle.timesync.degraded is True
        assert state.system.banner(DEGRADED_BANNER_KEY) is not None
        assert lifecycle.serving is True
    finally:
        await lifecycle.shutdown()


async def test_boot_writes_the_start_marker_but_not_yet_the_healthy_one(
    config: Config, db: Database, state: StateStore, bus: EventBus, schedule: FakeSchedule
) -> None:
    lifecycle = build(config, db, state, bus, schedule)
    await lifecycle.boot()
    try:
        marker = json.loads(
            (Path(config.app.state_dir) / BOOT_STATE_FILENAME).read_text(encoding="utf-8")
        )
        assert marker["started"]["at"]
        # No /data/app/current in a checkout, so the version resolves to null
        # rather than to a guess (contracts §1).
        assert marker["started"]["version"] is None
        assert marker["healthy"] is None
        assert lifecycle.healthy_marker_written is False
    finally:
        await lifecycle.shutdown()


async def test_boot_notifies_systemd_that_it_is_ready(
    config: Config, db: Database, state: StateStore, bus: EventBus, schedule: FakeSchedule
) -> None:
    notifier = Notifier()
    lifecycle = build(
        config, db, state, bus, schedule, watchdog=WatchdogTask(notify=notifier)
    )
    await lifecycle.boot()
    try:
        assert "READY=1" in notifier.states
        assert any(s.startswith("STATUS=") for s in notifier.states)
    finally:
        await lifecycle.shutdown()
    assert "STOPPING=1" in notifier.states


# -- §12.1: Phase 2 lighting ---------------------------------------------


async def test_boot_with_manual_external_control_restored_suspends_dmx_and_sends_no_frame(
    config: Config, db: Database, state: StateStore, bus: EventBus, schedule: FakeSchedule
) -> None:
    """§7.2.7, §12.3: only ``manual`` is restored, and it suspends the DMX pass."""
    device_id, channel_id = await lit_venue(db)
    await system_state.set(db, "lighting", "levels", json.dumps({str(channel_id): 55.0}))
    await system_state.set(db, "lighting", "external_control", json.dumps("manual"))
    devices = FakeDevices(device_id)
    devices.connect(device_id)  # connected from the start — still nothing must go out
    service = LightingService(state, bus, db, devices, FakeKnx())
    lifecycle = build(config, db, state, bus, schedule, lighting=service)

    await lifecycle.boot()
    try:
        assert service.external_active is True
        assert service.renderer.suspended is True
        await asyncio.sleep(0.1)
        assert devices.output(device_id).sent == []  # no frame while suspended
    finally:
        await lifecycle.shutdown()


async def test_boot_without_the_manual_flag_sends_one_frame_once_the_output_connects(
    config: Config, db: Database, state: StateStore, bus: EventBus, schedule: FakeSchedule
) -> None:
    """§12.1: composites the restored model — never a blackout — and waits for
    a lighting output device to report connected before sending anything."""
    device_id, channel_id = await lit_venue(db)
    await system_state.set(db, "lighting", "levels", json.dumps({str(channel_id): 77.0}))
    devices = FakeDevices(device_id)
    service = LightingService(state, bus, db, devices, FakeKnx())
    lifecycle = build(config, db, state, bus, schedule, lighting=service)

    await lifecycle.boot()
    try:
        assert service.external_active is False
        assert service.renderer.suspended is False
        # The restored level is composited straight away — nothing blacks out
        # at startup even before any output has connected (§12.1).
        assert service.composited_level(channel_id) == 77.0
        await asyncio.sleep(0.1)
        assert devices.output(device_id).sent == []  # nothing until connected

        devices.connect(device_id)
        bus.emit(DeviceStatusChanged("hdmi", "connected"))  # wakes the renderer
        await wait_until(lambda: devices.output(device_id).sent != [])
    finally:
        await lifecycle.shutdown()


async def test_boot_waits_for_booth_frames_before_the_renderers_first_frame(
    config: Config, db: Database, state: StateStore, bus: EventBus, schedule: FakeSchedule
) -> None:
    """§12.1's "wait up to 5 s for booth frames" (carry-forward 10,
    phase-7 plan). The output device is connected from the very start (the
    same premise ``test_boot_with_manual_external_control_restored_
    suspends_dmx_and_sends_no_frame`` uses above) — still nothing must go
    out, because the desk sends its first frame while the boot wait is in
    progress, before the renderer's task has even been created. Without the
    fix the renderer is armed immediately, before anything can be detected,
    and would have sent the restored model straight away — this is
    deterministic, not a timing guess, since the device being connected
    from the start means an unfixed renderer sends within its very first
    loop iteration, well before this test's 20 ms.

    ``desk`` keeps its own real clock rather than ``schedule``'s fake one
    (which ``lifecycle`` itself uses for other §12.1 waits, e.g. time-sync):
    ``desk_input.start()``'s always-on silence watch and ``wait_at_boot``'s
    own loop both read ``desk``'s clock, and a fake one that costs no real
    time lets the silence watch free-run once woken, racing itself far past
    five virtual seconds within the same event-loop turn.
    """
    device_id, channel_id = await lit_venue(db)
    await system_state.set(db, "lighting", "levels", json.dumps({str(channel_id): 60.0}))
    devices = FakeDevices(device_id)
    devices.connect(device_id)  # connected from the start — still nothing must go out
    service = LightingService(state, bus, db, devices, FakeKnx())
    desk = DeskInput(state, service.set_external_detected, lambda: service.config)
    lifecycle = build(config, db, state, bus, schedule, lighting=service, desk_input=desk)

    async def _send_the_desks_first_frame_shortly() -> None:
        await asyncio.sleep(0.02)
        desk.art_dmx(999, 0, bytes(512))

    sender = asyncio.create_task(_send_the_desks_first_frame_shortly())
    boot_started = asyncio.get_running_loop().time()
    await lifecycle.boot()
    boot_took = asyncio.get_running_loop().time() - boot_started
    await sender
    try:
        assert desk.detected is True
        assert service.external_active is True
        assert service.renderer.suspended is True
        assert boot_took < 1.0, "boot took the full 5 s instead of exiting once detected"
        assert devices.output(device_id).sent == [], (
            "the controller's restored model reached an already-connected output "
            "before the desk's own frame could be detected"
        )
    finally:
        await lifecycle.shutdown()


async def test_boot_does_not_wait_when_no_desk_input_is_configured(
    config: Config, db: Database, state: StateStore, bus: EventBus, schedule: FakeSchedule
) -> None:
    """No ``desk_input`` at all (booth input not wired) costs boot nothing —
    the same shape as every existing lifecycle test before this feature."""
    device_id, channel_id = await lit_venue(db)
    await system_state.set(db, "lighting", "levels", json.dumps({str(channel_id): 42.0}))
    devices = FakeDevices(device_id)
    service = LightingService(state, bus, db, devices, FakeKnx())
    lifecycle = build(config, db, state, bus, schedule, lighting=service, desk_input=None)

    boot_started = asyncio.get_running_loop().time()
    await lifecycle.boot()
    boot_took = asyncio.get_running_loop().time() - boot_started
    try:
        assert boot_took < 1.0  # no boot-time wait was even attempted
        assert service.renderer.suspended is False
    finally:
        await lifecycle.shutdown()


async def test_boot_does_not_wait_with_a_desk_input_but_no_lighting_output_device(
    config: Config, db: Database, state: StateStore, bus: EventBus, schedule: FakeSchedule
) -> None:
    """A ``desk_input`` exists (§7.2.7's feature is wired into the app), but
    no device row of category lighting_output has ever been configured — so
    nothing could ever call ``DeskInput.attach()`` to resolve ``configured``
    one way or the other, and ``wait_at_boot()`` alone would run its full
    real 5 s for nothing on every boot. Found by
    tests/unit/api/test_system.py's own boot-timing test, which has no such
    device either.
    """
    devices = FakeDevices()
    service = LightingService(state, bus, db, devices, FakeKnx())
    desk = DeskInput(state, service.set_external_detected, lambda: service.config)
    lifecycle = build(config, db, state, bus, schedule, lighting=service, desk_input=desk)

    boot_started = asyncio.get_running_loop().time()
    await lifecycle.boot()
    boot_took = asyncio.get_running_loop().time() - boot_started
    try:
        assert boot_took < 1.0, "boot waited on a desk input with no possible device to feed it"
    finally:
        await lifecycle.shutdown()


async def test_knx_dimmer_status_telegram_is_applied_to_its_channel(
    config: Config, db: Database, state: StateStore, bus: EventBus, schedule: FakeSchedule
) -> None:
    """§9.6: the boot sequence wires incoming status telegrams to the service."""
    command = await knx_crud.create_address(
        db, group_address="1/1/5", name="House command", dpt="5.001", direction="outgoing"
    )
    status = await knx_crud.create_address(
        db, group_address="1/1/6", name="House status", dpt="5.001", direction="incoming"
    )
    channel = await lighting_crud.create_channel(
        db,
        name="House centre",
        type="knx_dimmer",
        knx_command_address_id=command.id,
        knx_status_address_id=status.id,
    )
    service = LightingService(state, bus, db, FakeDevices(), FakeKnx())
    lifecycle = build(config, db, state, bus, schedule, lighting=service)

    await lifecycle.boot()
    try:
        bus.emit(
            KnxTelegramReceived(
                group_address="1/1/6", dpt="5.001", value=42.0, raw=b"", source_address="1.1.4"
            )
        )
        await settle(lambda: service.composited_level(channel.id) == 42.0)
    finally:
        await lifecycle.shutdown()


# -- §14.5: the healthy marker ---------------------------------------------------


def test_healthy_is_the_spec_definition(
    config: Config, db: Database, state: StateStore, bus: EventBus, schedule: FakeSchedule
) -> None:
    lifecycle = build(config, db, state, bus, schedule)
    state.register_owner("devices", "test")
    writer = state.devices.writer("test")

    assert lifecycle.healthy() is False  # not serving yet
    lifecycle._serving = True
    assert lifecycle.healthy() is True  # nothing configured is not unhealthy

    writer.set_record("hdmi", DeviceStatusRecord(status="connecting"))
    assert lifecycle.healthy() is False  # no outcome yet

    writer.set_record("mixer", DeviceStatusRecord(status="connected"))
    assert lifecycle.healthy() is True  # at least one device connected

    writer.remove("mixer")
    writer.set_record("hdmi", DeviceStatusRecord(status="error", kind="config"))
    assert lifecycle.healthy() is True  # a known, non-blocking device error

    writer.set_record("hdmi", DeviceStatusRecord(status="error", kind=None))
    assert lifecycle.healthy() is False  # an error we cannot name


async def test_healthy_marker_is_written_after_thirty_seconds_and_not_before(
    config: Config, db: Database, state: StateStore, bus: EventBus, schedule: FakeSchedule
) -> None:
    lifecycle = build(config, db, state, bus, schedule)
    await lifecycle.boot()
    try:
        start = schedule.now
        await advance_until(lambda: schedule.now - start >= HEALTHY_AFTER_S - 1, schedule)
        assert lifecycle.healthy_marker_written is False

        await advance_until(lambda: lifecycle.healthy_marker_written, schedule)
        assert schedule.now - start >= HEALTHY_AFTER_S
        marker = json.loads(
            (Path(config.app.state_dir) / BOOT_STATE_FILENAME).read_text(encoding="utf-8")
        )
        assert marker["healthy"]["at"]
        assert marker["healthy"]["version"] == marker["started"]["version"]
    finally:
        await lifecycle.shutdown()


async def test_the_thirty_seconds_restart_when_health_lapses(
    config: Config, db: Database, state: StateStore, bus: EventBus, schedule: FakeSchedule
) -> None:
    lifecycle = build(config, db, state, bus, schedule)
    state.register_owner("devices", "test")
    writer = state.devices.writer("test")
    writer.set_record("hdmi", DeviceStatusRecord(status="connecting"))
    await lifecycle.boot()
    try:
        start = schedule.now
        await advance_until(lambda: schedule.now - start >= 2 * HEALTHY_AFTER_S, schedule)
        assert lifecycle.healthy_marker_written is False  # still connecting

        writer.set_record("hdmi", DeviceStatusRecord(status="connected"))
        await advance_until(lambda: lifecycle.healthy_marker_written, schedule)
    finally:
        await lifecycle.shutdown()


# -- §12.4: shutdown -------------------------------------------------------------


async def test_shutdown_flushes_state_and_closes_devices_inside_the_budget(
    config: Config,
    db: Database,
    state: StateStore,
    bus: EventBus,
    schedule: FakeSchedule,
    drivers: None,
) -> None:
    await devices_crud.create(
        db,
        category="video_matrix",
        driver_key="stub",
        name="Auditorium matrix",
        config=LOOPBACK,
    )
    manager = DeviceManager(db, state, bus, config, stop_timeout=2.0)
    persister = StatePersister(state, db)
    lifecycle = build(
        config,
        db,
        state,
        bus,
        schedule,
        devices=manager,
        start_devices=True,
        persister=persister,
    )
    await lifecycle.boot()
    await settle(
        lambda: "hdmi" in state.devices.records()
        and state.devices.records()["hdmi"].status == "connected"
    )

    loop = asyncio.get_running_loop()
    started = loop.time()
    await lifecycle.shutdown()
    elapsed = loop.time() - started

    assert elapsed < 5.0  # §12.4's TimeoutStopSec=20, with room to spare
    # 3. flushed to SQLite — this is what §12.3 restores and the diagnostic
    #    record after an unexpected restart.
    rows = await system_state.get_domain(db, "devices")
    assert "status" in rows
    assert "connected" in rows["status"]
    # 5. every device connection closed
    assert state.owners("devices") == frozenset({DEVICE_OWNER})
    assert manager.state_key(1) is None


async def test_shutdown_stops_the_background_tasks(
    config: Config, db: Database, state: StateStore, bus: EventBus, schedule: FakeSchedule
) -> None:
    lifecycle = build(config, db, state, bus, schedule)
    await lifecycle.boot()
    tasks = list(lifecycle.tasks)
    assert tasks
    await lifecycle.shutdown()
    assert all(task.done() for task in tasks)
    assert lifecycle.serving is False


async def test_shutdown_hooks_are_named_and_ordered(
    config: Config, db: Database, state: StateStore, bus: EventBus, schedule: FakeSchedule
) -> None:
    """§12.4's Phase 2 steps are hooks, in the order the spec gives them."""
    called: list[str] = []
    lifecycle = build(config, db, state, bus, schedule)
    await lifecycle.boot()

    async def record(name: str) -> None:
        called.append(name)

    lifecycle._stop_scene_triggers = lambda: record("triggers")  # type: ignore[method-assign]
    lifecycle._wait_for_scenes = lambda: record("scenes")  # type: ignore[method-assign]
    lifecycle._dmx_blackout = lambda: record("blackout")  # type: ignore[method-assign]
    await lifecycle.shutdown()
    assert called == ["triggers", "scenes", "blackout"]


async def test_shutdown_zeroes_dmx_channels_through_the_level_store(
    config: Config, db: Database, state: StateStore, bus: EventBus, schedule: FakeSchedule
) -> None:
    """§12.4 step 4, §7.1: the model-level blackout, not a transport suspend."""
    device_id, channel_id = await lit_venue(db)
    service = LightingService(state, bus, db, FakeDevices(device_id), FakeKnx())
    lifecycle = build(config, db, state, bus, schedule, lighting=service)
    await lifecycle.boot()
    service.set_level(channel_id, 80.0)

    await lifecycle.shutdown()

    assert service.composited_level(channel_id) == 0.0


async def test_shutdown_skips_the_blackout_while_external_control_is_active(
    config: Config, db: Database, state: StateStore, bus: EventBus, schedule: FakeSchedule
) -> None:
    """§8.8, §7.2.7: a controller not driving the rig must not black it out."""
    device_id, channel_id = await lit_venue(db)
    service = LightingService(state, bus, db, FakeDevices(device_id), FakeKnx())
    lifecycle = build(config, db, state, bus, schedule, lighting=service)
    await lifecycle.boot()
    service.set_level(channel_id, 80.0)
    service.set_external_manual(True)

    await lifecycle.shutdown()

    assert service.composited_level(channel_id) == 80.0


async def test_a_second_database_is_not_opened_by_the_boot_sequence(
    config: Config, state: StateStore, bus: EventBus, schedule: FakeSchedule
) -> None:
    """The lifespan owns the connection; the boot sequence borrows it."""
    db = Database()
    await db.open(MEMORY)
    await migrate(db)
    try:
        lifecycle = build(config, db, state, bus, schedule)
        await lifecycle.boot()
        await lifecycle.shutdown()
        assert db.is_open
    finally:
        await db.close()
