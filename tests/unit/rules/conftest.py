"""A small auditorium for the rule layer: banks, statuses, a house dimmer (spec §8.8).

The venue is built in an in-memory database the way the admin screens would
build it, the real lighting service runs over it with a fake lighting output
and a fake KNX subsystem, and the rules engine is started on top — so the
tests exercise the real fade engine, compositor and frame renderer, and see
frames and telegrams in the order they actually leave.

Rows, as §8.8 lays the room out::

    WHEN   knx 1/0/1  THEN  group "Row 1"     → 100% / 0%      STATUS 1/0/11
    WHEN   knx 1/0/2  THEN  group "Row 2"     → 100% / 0%      STATUS 1/0/12
    WHEN   knx 1/0/0  THEN  group "All Stage" → 100% / 0%      STATUS 1/0/10
    WHEN   knx 1/1/20 THEN  group "House"     → 100% / 0%      STATUS 1/1/21
                                                               STATUS 1/0/9 = external control
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from proskenion.config import Config, parse_config
from proskenion.core.bus import EventBus
from proskenion.core.events import KnxTelegramReceived
from proskenion.core.lighting import LightingService
from proskenion.core.state import StateStore
from proskenion.db.connection import MEMORY, Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import knx as knx_crud
from proskenion.db.crud import lighting as lighting_crud
from proskenion.db.crud import rules as rules_crud
from proskenion.db.crud import scenes as scenes_crud
from proskenion.db.crud.base import AUCKLAND
from proskenion.db.migrations import migrate
from proskenion.rules.engine import RulesEngine
from proskenion.rules.scheduler import ScheduleClock
from tests.unit.core.dmx.conftest import FakeDevices, wait_until

#: The controller's own individual address in these tests (``[knx] individual_address``).
OWN = "1.1.250"
#: A wall panel's.
PANEL = "1.1.4"
#: Where each single-channel fixture is patched.
PATCH = {"A": 1, "B": 2, "C": 3, "D": 4}


class FakeKnx:
    """The KNX subsystem: records every write, dimmer and status alike (§7.1)."""

    def __init__(self, own_address: str | None = OWN) -> None:
        self.writes: list[tuple[float, str, Any, int]] = []
        self.own_address = own_address
        self.fail: set[str] = set()

    async def write(self, group_address: str, value: Any, *, priority: int) -> None:
        if group_address in self.fail:
            raise OSError("knxd is not answering")
        self.writes.append((asyncio.get_running_loop().time(), group_address, value, priority))

    def to(self, group_address: str) -> list[Any]:
        return [v for _, ga, v, _ in self.writes if ga == group_address]

    def last(self, group_address: str) -> Any:
        return self.to(group_address)[-1]

    def times(self, group_address: str) -> list[float]:
        return [t for t, ga, _, _ in self.writes if ga == group_address]


@dataclass
class SceneCall:
    scene_id: int
    triggered_by: str
    trigger_value: object | None
    hirer_originated: bool = False
    at: float = field(default_factory=time.monotonic)
    """``time.monotonic()`` when the run was called."""


@dataclass
class FakeSceneOutcome:
    """The shape ``rules.engine.SceneOutcome`` needs — what
    :class:`~proskenion.scene.engine.SceneRunResult` satisfies for real."""

    result: str


class FakeSceneHandle:
    """The shape ``rules.engine.SceneHandle`` needs: a started run whose
    :meth:`result` is awaited separately from starting it — matching
    :class:`~proskenion.scene.engine.SceneRunHandle`'s real contract, where
    ``run()`` returns once the scene has *started*, not finished."""

    def __init__(self, scenes: FakeScenes, outcome: str) -> None:
        self._scenes = scenes
        self._outcome = outcome

    async def result(self) -> FakeSceneOutcome:
        if self._scenes.hold is not None:
            await self._scenes.hold.wait()
        return FakeSceneOutcome(self._outcome)


class FakeScenes:
    """The scene engine's entry point, faked (its own task builds the real one)."""

    def __init__(self) -> None:
        self.calls: list[SceneCall] = []
        #: Blocks the *handle's* result, not the start — a held scene is one
        #: still running, exactly the case §12.4's shutdown drain and
        #: rules.engine's CancelledError handling are for.
        self.hold: asyncio.Event | None = None
        self.raise_on: set[int] = set()

    async def run(
        self,
        scene_id: int,
        *,
        triggered_by: str,
        trigger_value: object | None = None,
        hirer_originated: bool = False,
    ) -> FakeSceneHandle:
        self.calls.append(SceneCall(scene_id, triggered_by, trigger_value, hirer_originated))
        if scene_id in self.raise_on:
            raise RuntimeError("projector unreachable")
        return FakeSceneHandle(self, "success")


class FakeDeviceKeys:
    """The device manager's ``state_key``: device row id → ``state.devices`` key."""

    def __init__(self, keys: dict[int, str] | None = None) -> None:
        self.keys = dict(keys or {})

    def state_key(self, device_id: int) -> str | None:
        return self.keys.get(device_id)


@dataclass
class Venue:
    output: int
    addresses: dict[str, int]
    channels: dict[str, int]
    groups: dict[str, int]
    rules: dict[str, int]
    statuses: dict[str, int]
    scenes: dict[str, int] = field(default_factory=dict)


async def build_venue(db: Database) -> Venue:
    output = await devices_crud.create(
        db, category="lighting_output", driver_key="artnet", name="eDMX8", config={}
    )
    addresses: dict[str, int] = {}
    for ga, name, dpt, direction in (
        ("1/0/0", "All Stage command", "1.001", "incoming"),
        ("1/0/1", "Bank 1 command", "1.001", "incoming"),
        ("1/0/2", "Bank 2 command", "1.001", "incoming"),
        ("1/0/9", "External control status", "1.001", "outgoing"),
        ("1/0/10", "All Stage status", "1.001", "outgoing"),
        ("1/0/11", "Bank 1 status", "1.001", "outgoing"),
        ("1/0/12", "Bank 2 status", "1.001", "outgoing"),
        ("0/5/0", "Alarm armed", "1.001", "incoming"),
        ("1/1/10", "House dimmer", "5.001", "outgoing"),
        ("1/1/20", "House command", "1.001", "incoming"),
        ("1/1/21", "House status", "1.001", "outgoing"),
        ("1/5/1", "Lux sensor", "9.004", "incoming"),
        ("1/5/2", "Scene selector", "5.010", "incoming"),
    ):
        row = await knx_crud.create_address(
            db, group_address=ga, name=name, dpt=dpt, direction=direction
        )
        addresses[ga] = row.id
    channels: dict[str, int] = {}
    for name, address in PATCH.items():
        row = await lighting_crud.create_channel(
            db,
            name=f"Fixture {name}",
            type="dmx",
            profile_id=1,
            device_id=output.id,
            address=address,
        )
        channels[name] = row.id
    house = await lighting_crud.create_channel(
        db, name="House centre", type="knx_dimmer", knx_command_address_id=addresses["1/1/10"]
    )
    channels["H"] = house.id
    groups: dict[str, int] = {}
    for name, members in (
        ("Row 1", ("A", "B")),
        ("Row 2", ("C", "D")),
        ("All Stage", ("A", "B", "C", "D")),
        ("House", ("H",)),
    ):
        group = await lighting_crud.create_group(db, name=name)
        await lighting_crud.set_group_members(db, group.id, [channels[m] for m in members])
        groups[name] = group.id
    rules: dict[str, int] = {}
    for name, ga, group in (
        ("Stage Bank 1", "1/0/1", "Row 1"),
        ("Stage Bank 2", "1/0/2", "Row 2"),
        ("All Stage", "1/0/0", "All Stage"),
        ("House", "1/1/20", "House"),
    ):
        rule = await rules_crud.create_rule(
            db,
            name=name,
            trigger_type="knx",
            knx_address_id=addresses[ga],
            match_type="any",
            action_type="lighting_group",
            lighting_group_id=groups[group],
            on_level=100.0,
            off_level=0.0,
            fade_ms=0,
        )
        rules[name] = rule.id
    statuses: dict[str, int] = {}
    for name, ga, source, group in (
        ("Bank 1 state", "1/0/11", "lighting_group_all_at", "Row 1"),
        ("Bank 2 state", "1/0/12", "lighting_group_all_at", "Row 2"),
        ("All Stage state", "1/0/10", "lighting_group_all_at", "All Stage"),
        ("House state", "1/1/21", "lighting_group_all_at", "House"),
        ("External control", "1/0/9", "external_control", None),
    ):
        status = await rules_crud.create_derived_status(
            db,
            name=name,
            knx_address_id=addresses[ga],
            source_type=source,
            lighting_group_id=None if group is None else groups[group],
            compare_level=None if group is None else 100.0,
        )
        statuses[ga] = status.id
    return Venue(output.id, addresses, channels, groups, rules, statuses)


async def add_scene(db: Database, venue: Venue, name: str, *, priority: str = "normal") -> int:
    scene = await scenes_crud.create_scene(db, name=name, priority=priority)
    venue.scenes[name] = scene.id
    return scene.id


class ScheduleTestClock:
    """The scheduler's wall and monotonic clocks, moved only by the test.

    The scheduler's sleep returns when the test moves the clock, or when a
    replan wakes it, whichever is first; the loop then re-reads the time, so
    an early wake is harmless. :meth:`advance` returns once the loop has
    handled the move and gone back to sleep.
    """

    def __init__(self, start: datetime) -> None:
        self.now = start.astimezone(UTC)
        self.mono = 5000.0
        self.naps: list[float] = []
        self.asleep = False
        self._moved = asyncio.Event()

    def wall(self) -> datetime:
        return self.now

    def local(self) -> datetime:
        return self.now.astimezone(AUCKLAND)

    def monotonic(self) -> float:
        return self.mono

    async def sleep(self, seconds: float, wake: asyncio.Event) -> None:
        self.naps.append(seconds)
        self._moved.clear()
        self.asleep = True
        moved = asyncio.ensure_future(self._moved.wait())
        woken = asyncio.ensure_future(wake.wait())
        try:
            await asyncio.wait({moved, woken}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self.asleep = False
            moved.cancel()
            woken.cancel()

    async def settled(self, naps_before: int | None = None) -> None:
        """Wait until the loop is asleep again (after ``naps_before`` naps, if given)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while not self.asleep or (naps_before is not None and len(self.naps) <= naps_before):
            if loop.time() > deadline:
                raise AssertionError("the scheduler never went back to sleep")
            await asyncio.sleep(0)

    async def advance(self, seconds: float, *, step: float = 0.0) -> None:
        """Time passes: both clocks move ``seconds``; the wall clock also steps ``step``."""
        await self.settled()
        before = len(self.naps)
        self.now += timedelta(seconds=seconds + step)
        self.mono += seconds
        self._moved.set()
        await self.settled(before)

    async def run_until(self, instant: str) -> None:
        """Let time pass until ``instant`` (ISO 8601 with offset), waking the loop
        exactly when its own sleep would have ended, as the real clock does."""
        target = datetime.fromisoformat(instant).astimezone(UTC)
        while self.now < target:
            await self.settled()
            remaining = (target - self.now).total_seconds()
            await self.advance(min(max(self.naps[-1], 0.001), remaining))


class Clock:
    """A controllable monotonic clock for debounce and rate limits."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class Rig:
    db: Database
    bus: EventBus
    state: StateStore
    venue: Venue
    lighting: LightingService
    devices: FakeDevices
    knx: FakeKnx
    scenes: FakeScenes
    keys: FakeDeviceKeys
    clock: Clock
    engine: RulesEngine

    async def telegram(
        self, group_address: str, value: object, *, source: str = PANEL, dpt: str = "1.001"
    ) -> None:
        """A telegram arriving from the bus, handled to completion."""
        await self.engine.handle_telegram(
            KnxTelegramReceived(group_address, dpt, value, b"\x00\x81", source)
        )

    def level(self, name: str) -> float:
        value = self.state.lighting.get_item("levels", self.venue.channels[name])
        return 0.0 if value is None else float(value)  # type: ignore[arg-type]

    def frame_slot(self, name: str) -> list[tuple[float, int]]:
        """``(time, DMX value)`` of every frame sent, for one fixture's slot."""
        address = PATCH[name]
        sent = self.devices.output(self.venue.output).sent
        return [(t, data[address - 1]) for t, _, data in sent]

    async def settle(self, predicate: Callable[[], bool], within: float = 2.0) -> None:
        await wait_until(predicate, within)

    async def reload(self) -> None:
        await self.engine.reload()


async def start_rig(
    db: Database,
    config: Config,
    *,
    knx: FakeKnx | None = None,
    restore: bool = False,
    schedule_clock: ScheduleClock | None = None,
) -> Rig:
    bus = EventBus()
    await bus.start()
    state = StateStore(config, bus)
    venue = await build_venue(db)
    devices = FakeDevices(venue.output)
    devices.connect(venue.output)
    fake_knx = knx or FakeKnx()
    lighting = LightingService(state, bus, db, devices, fake_knx, keepalive_s=1.0)
    await lighting.start()
    scenes = FakeScenes()
    keys = FakeDeviceKeys()
    clock = Clock()
    engine = RulesEngine(
        db,
        state,
        bus,
        lighting=lighting,
        scenes=scenes,
        knx=fake_knx,
        devices=keys,
        clock=clock,
        schedule_clock=schedule_clock,
        wall_clock=(
            None
            if schedule_clock is None
            else lambda: schedule_clock.wall().astimezone(AUCKLAND)
        ),
    )
    rig = Rig(db, bus, state, venue, lighting, devices, fake_knx, scenes, keys, clock, engine)
    if not restore:
        await engine.start()
        # The startup evaluation has gone out; wait for the first frame too.
        await wait_until(lambda: len(devices.output(venue.output).sent) >= 1)
    return rig


async def stop_rig(rig: Rig) -> None:
    await rig.engine.stop()
    await rig.lighting.stop()
    await rig.bus.stop()


@pytest.fixture
def dev_config() -> Config:
    return parse_config(
        {
            "database": {"path": "/data/db/auditorium.db"},
            "logging": {"path": "/data/logs"},
            "app": {"environment": "development"},
        }
    )


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    """An in-memory database with the shipped schema and seed applied."""
    database = Database()
    await database.open(MEMORY)
    try:
        await migrate(database)
        yield database
    finally:
        await database.close()


@pytest.fixture
async def rig(db: Database, dev_config: Config) -> AsyncIterator[Rig]:
    running = await start_rig(db, dev_config)
    try:
        yield running
    finally:
        await stop_rig(running)
