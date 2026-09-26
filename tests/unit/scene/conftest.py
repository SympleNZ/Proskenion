"""Fakes, a small rig and a running engine for the scene engine tests (spec §8.11–§8.16).

The lighting service is the real one, loaded from a real (in-memory)
database, so precedence, locks and external control are exercised through
the fade engine exactly as in production. KNX, the broadcaster and the
device manager's capability reports are fakes that record what they are
asked.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest

from proskenion.config import Config, parse_config
from proskenion.core.bus import EventBus
from proskenion.core.devices import CapabilityReport, DeviceUnavailable
from proskenion.core.drivers.capabilities import Capabilities, MixerCapabilities
from proskenion.core.knx import Priority, UnknownGroupAddress
from proskenion.core.lighting import LightingService
from proskenion.core.state import StateStore
from proskenion.db.connection import MEMORY, Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import knx as knx_crud
from proskenion.db.crud import lighting as lighting_crud
from proskenion.db.crud import scenes as scenes_crud
from proskenion.db.crud.scenes import Scene, SceneAction
from proskenion.db.migrations import migrate
from proskenion.scene.domains import ActionContext, ActionOutcome
from proskenion.scene.engine import SceneEngine
from tests.unit.core.dmx.conftest import FakeDevices, FakeKnx


def dev_config() -> Config:
    return parse_config(
        {
            "database": {"path": "/data/db/auditorium.db"},
            "logging": {"path": "/data/logs"},
            "app": {"environment": "development"},
        }
    )


# -- fakes -------------------------------------------------------------------------


class FakeKnxWriter:
    """The KNX subsystem's ``write``: records every call; can refuse an address."""

    def __init__(self) -> None:
        self.writes: list[tuple[float, str, Any, int]] = []
        self.unknown: set[str] = set()

    async def write(self, group_address: str, value: Any, *, priority: Priority) -> None:
        if group_address in self.unknown:
            raise UnknownGroupAddress(group_address)
        self.writes.append((asyncio.get_running_loop().time(), group_address, value, priority))


class FakeBroadcaster:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def publish(self, message: dict[str, Any]) -> int:
        self.messages.append(message)
        return 1

    def types(self) -> list[str]:
        return [str(m["type"]) for m in self.messages]


class FakeCapabilities:
    """The device manager's ``capabilities()``: a fixed report per device id."""

    def __init__(self, reports: Mapping[int, Capabilities] | None = None) -> None:
        self.reports = dict(reports or {})
        self.asked: list[int] = []

    async def capabilities(self, device_id: int) -> CapabilityReport:
        self.asked.append(device_id)
        if device_id not in self.reports:
            raise DeviceUnavailable(f"device {device_id} is not configured")
        return CapabilityReport(self.reports[device_id], as_connected=True)


@dataclass
class RecordingHandler:
    """A domain handler for the six domains Phase 2 does not implement."""

    outcome: ActionOutcome = field(default_factory=ActionOutcome.sent)
    gate: Callable[[SceneAction, Capabilities], str | None] = lambda action, caps: None
    raises: Exception | None = None
    calls: list[tuple[float, int, ActionContext]] = field(default_factory=list)
    gated: list[int] = field(default_factory=list)

    def unsupported(self, action: SceneAction, capabilities: Capabilities) -> str | None:
        self.gated.append(action.id)
        return self.gate(action, capabilities)

    async def execute(self, action: SceneAction, context: ActionContext) -> ActionOutcome:
        self.calls.append((asyncio.get_running_loop().time(), action.id, context))
        if self.raises is not None:
            raise self.raises
        return self.outcome

    def called(self) -> list[int]:
        return [action_id for _, action_id, _ in self.calls]


def mixer_capabilities(
    *, scene_recall: bool = True, supports_mute: bool = True
) -> MixerCapabilities:
    return MixerCapabilities(
        input_count=8,
        output_count=2,
        supports_scene_recall=scene_recall,
        supports_pan=False,
        supports_mute=supports_mute,
        supports_metering=False,
        meter_min_db=None,
        meter_max_db=None,
        meter_point=None,
        supports_gain=False,
        supports_dca=False,
        min_db=-90.0,
        max_db=10.0,
    )


# -- the rig -----------------------------------------------------------------------


@dataclass
class Rig:
    """Ids of what :func:`build_rig` put in the database."""

    output: int
    front: int
    mid: int
    back: int
    house: int
    lamp: int  # 1.001 outgoing
    level: int  # 5.001 outgoing
    byte: int  # 5.010 outgoing
    armed: int  # 1.001 incoming — the alarm's armed state


async def build_rig(db: Database) -> Rig:
    output = await devices_crud.create(
        db, category="lighting_output", driver_key="artnet", name="eDMX8", config={}
    )
    house_ga = await knx_crud.create_address(
        db, group_address="1/1/10", name="House centre", dpt="5.001", direction="outgoing"
    )
    lamp = await knx_crud.create_address(
        db, group_address="1/0/9", name="Stage lamp", dpt="1.001", direction="outgoing"
    )
    level = await knx_crud.create_address(
        db, group_address="1/0/5", name="Stage level", dpt="5.001", direction="outgoing"
    )
    byte = await knx_crud.create_address(
        db, group_address="1/0/20", name="Raw byte", dpt="5.010", direction="both"
    )
    armed = await knx_crud.create_address(
        db, group_address="1/0/1", name="Alarm armed", dpt="1.001", direction="incoming"
    )
    channels = []
    for name, address in (("Front wash", 1), ("Mid wash", 2), ("Back wash", 3)):
        channel = await lighting_crud.create_channel(
            db, name=name, type="dmx", profile_id=1, device_id=output.id, address=address
        )
        channels.append(channel.id)
    house = await lighting_crud.create_channel(
        db, name="House centre", type="knx_dimmer", knx_command_address_id=house_ga.id
    )
    return Rig(output.id, *channels, house.id, lamp.id, level.id, byte.id, armed.id)


async def make_scene(
    db: Database,
    *actions: dict[str, Any],
    name: str = "Performance Start",
    priority: str = "normal",
    enabled: bool = True,
    protected: bool = False,
    visible_operator: bool = True,
) -> Scene:
    scene = await scenes_crud.create_scene(
        db,
        name=name,
        priority=priority,
        enabled=enabled,
        protected=protected,
        visible_operator=visible_operator,
    )
    for index, action in enumerate(actions):
        values = {"sort_order": index, **action}
        await scenes_crud.create_action(db, scene_id=scene.id, **values)
    return scene


def dmx(snapshot: Mapping[int, float], *, fade_ms: int = 0, delay_ms: int = 0) -> dict[str, Any]:
    return {
        "domain": "dmx",
        "delay_ms": delay_ms,
        "dmx_snapshot": {str(k): {"level": v} for k, v in snapshot.items()},
        "dmx_fade_ms": fade_ms,
    }


def knx(
    address_id: int, value: str | None = None, *, delay_ms: int = 0, **extra: Any
) -> dict[str, Any]:
    return {
        "domain": "knx",
        "delay_ms": delay_ms,
        "knx_address_id": address_id,
        "knx_value": value,
        **extra,
    }


# -- fixtures ----------------------------------------------------------------------


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    database = Database()
    await database.open(MEMORY)
    try:
        await migrate(database)
        yield database
    finally:
        await database.close()


@pytest.fixture
async def bus() -> AsyncIterator[EventBus]:
    events = EventBus()
    await events.start()
    try:
        yield events
    finally:
        await events.stop()


@pytest.fixture
def state(bus: EventBus) -> StateStore:
    return StateStore(dev_config(), bus)


@pytest.fixture
async def rig(db: Database) -> Rig:
    return await build_rig(db)


@pytest.fixture
async def lighting(
    db: Database, bus: EventBus, state: StateStore, rig: Rig
) -> AsyncIterator[LightingService]:
    service = LightingService(state, bus, db, FakeDevices(rig.output), FakeKnx())
    await service.start()
    try:
        yield service
    finally:
        await service.stop()


@dataclass
class Harness:
    db: Database
    state: StateStore
    lighting: LightingService
    knx: FakeKnxWriter
    broadcaster: FakeBroadcaster
    devices: FakeCapabilities
    engine: SceneEngine
    rig: Rig

    def level(self, channel_id: int) -> float:
        value = self.state.lighting.get_item("levels", channel_id)
        return 0.0 if value is None else float(str(value))


@pytest.fixture
async def harness(
    db: Database, state: StateStore, lighting: LightingService, rig: Rig
) -> AsyncIterator[Harness]:
    knx_writer = FakeKnxWriter()
    broadcaster = FakeBroadcaster()
    devices = FakeCapabilities()
    engine = SceneEngine(
        db,
        state,
        lighting=lighting,
        knx=knx_writer,
        devices=devices,
        broadcaster=broadcaster,
    )
    try:
        yield Harness(db, state, lighting, knx_writer, broadcaster, devices, engine, rig)
    finally:
        await engine.stop()


async def wait_for(condition: Callable[[], bool], within_s: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within_s
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)
