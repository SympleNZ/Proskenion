"""``projector_power``, ``projector_input`` and ``hdmi_source`` (§8.12, §7.4,
§7.5, §13.5) — through the real :class:`~proskenion.core.projector.ProjectorService`
and :class:`~proskenion.core.video.VideoService`, against the PJLink and
LKV422 stubs, over the real drivers. The projector rig runs the real
:class:`~proskenion.core.devices.DeviceManager` over TCP, exactly as
``tests/unit/core/test_projector.py``; the HDMI rig wires the real
:class:`~proskenion.core.drivers.lkv422.LKV422Driver` directly over a
:class:`~proskenion.core.transport.loopback.LoopbackTransport`, exactly as
``tests/unit/core/test_video.py`` — the LKV422 driver is serial-only, so it
is never run through the device manager in tests (see that module's
docstring for why).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from proskenion.core.bus import EventBus
from proskenion.core.devices import DeviceManager
from proskenion.core.dmx.fade import SceneRun
from proskenion.core.drivers.base import Driver
from proskenion.core.drivers.capabilities import ProjectorCapabilities
from proskenion.core.drivers.lkv422 import LKV422Driver
from proskenion.core.drivers.pjlink import PJLinkDriver
from proskenion.core.projector import ProjectorService
from proskenion.core.state import StateStore
from proskenion.core.transport.loopback import LoopbackTransport
from proskenion.core.video import VideoService
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import video as video_crud
from proskenion.db.crud.scenes import Scene, SceneAction
from proskenion.scene.av_handlers import (
    HdmiSourceHandler,
    ProjectorInputHandler,
    ProjectorPowerHandler,
)
from proskenion.scene.domains import ActionContext
from proskenion.scene.engine import SceneEngine
from tests.stubs.echo_driver import RecordingSink
from tests.stubs.lkv422_stub import LKV422Stub
from tests.stubs.pjlink_stub import PJLinkStub
from tests.unit.scene.conftest import dev_config, make_scene

# `asyncio_mode = "auto"` (pyproject.toml) collects every `async def test_...`
# without a marker; no module-level `pytestmark` here because one plain,
# synchronous test lives alongside them (a marker on a sync function warns).


# -- shared test helpers -------------------------------------------------------------

_ACTION_DEFAULTS: dict[str, Any] = {
    "id": 1,
    "scene_id": 1,
    "sort_order": 0,
    "delay_ms": 0,
    "knx_address_id": None,
    "knx_value": None,
    "knx_source": "literal",
    "knx_scale": None,
    "dmx_snapshot": None,
    "dmx_fade_ms": None,
    "mixer_scene_id": None,
    "mixer_channel_id": None,
    "mixer_db": None,
    "mixer_muted": None,
    "projector_power": None,
    "projector_input": None,
    "hdmi_destination": None,
    "hdmi_input_id": None,
    "device_id": None,
    "created_at": "",
    "updated_at": "",
}

_SCENE = Scene(
    id=1,
    name="Test",
    description=None,
    enabled=True,
    icon=None,
    priority="normal",
    protected=False,
    visible_operator=True,
    sort_order=0,
    created_at="",
    updated_at="",
)


def make_action(domain: str, **overrides: Any) -> SceneAction:
    """A standalone :class:`SceneAction`, for calling a handler directly
    without a database row (matched to what :func:`proskenion.scene.validation.provisional_action`
    does for the save-time gate)."""
    return SceneAction(**{**_ACTION_DEFAULTS, "domain": domain, **overrides})


def ctx(*, discarded: bool = False) -> ActionContext:
    reason = "discarded — critical scene took over" if discarded else None
    return ActionContext(
        run=SceneRun(scene_id=1),
        scene=_SCENE,
        triggered_by="api:admin",
        discard_reason=lambda: reason,
    )


# -- the projector rig: the real DeviceManager and ProjectorService, over TCP --------


@asynccontextmanager
async def fast_projector_polling(interval: float = 0.02) -> AsyncIterator[None]:
    """Shrinks every one of :class:`PJLinkDriver`'s §7.4/§11.1 probe cadences
    — 30 s on, 5 min off, 5 s warming/cooling, 30 s error — to ``interval``,
    restored on exit. A class-level patch, not an instance override: the
    connected device's very first :meth:`~proskenion.core.drivers.base.Driver.maintain`
    sleep is computed from the state its initial probe just found (§7.4's
    "the moment the projector settles"), before a test ever gets a driver
    instance to touch, so only patching the class in time for ``connect()``
    lets a test wait out a real transition without a real five-minute
    ``OFF_INTERVAL`` wait in the middle of it."""
    saved = (
        PJLinkDriver.ON_INTERVAL,
        PJLinkDriver.OFF_INTERVAL,
        PJLinkDriver.TRANSITION_INTERVAL,
        PJLinkDriver.ERROR_INTERVAL,
    )
    PJLinkDriver.ON_INTERVAL = interval
    PJLinkDriver.OFF_INTERVAL = interval
    PJLinkDriver.TRANSITION_INTERVAL = interval
    PJLinkDriver.ERROR_INTERVAL = interval
    try:
        yield
    finally:
        (
            PJLinkDriver.ON_INTERVAL,
            PJLinkDriver.OFF_INTERVAL,
            PJLinkDriver.TRANSITION_INTERVAL,
            PJLinkDriver.ERROR_INTERVAL,
        ) = saved


@asynccontextmanager
async def projector_rig(
    db: Database, state: StateStore, bus: EventBus, stub: PJLinkStub
) -> AsyncIterator[tuple[DeviceManager, ProjectorService]]:
    device = await devices_crud.create(
        db,
        category="projector",
        driver_key="pjlink",
        name="Projector",
        config={
            "transport": {"type": "tcp", "host": "127.0.0.1", "port": stub.port},
            "driver": {"password": None},
        },
    )
    manager = DeviceManager(
        db, state, bus, dev_config(), connect_timeout=2.0, probe_timeout=1.0, stop_timeout=2.0
    )
    await manager.start()
    await manager.wait_for_connection(device.id)
    service = ProjectorService(state, bus, db, manager)
    await service.start()
    try:
        yield manager, service
    finally:
        await service.stop()
        await manager.stop()


def power_set_commands(stub: PJLinkStub) -> list[str]:
    """Every ``%1POWR`` command that actually set a value — excludes the
    ``%1POWR ?`` discovery/poll query, so "nothing sent" is unambiguous."""
    return [c.command for c in stub.received if c.command in ("%1POWR 0", "%1POWR 1")]


def input_set_commands(stub: PJLinkStub) -> list[str]:
    return [
        c.command
        for c in stub.received
        if c.command.startswith("%1INPT ") and c.command != "%1INPT ?"
    ]


# -- projector_power -----------------------------------------------------------------


async def test_power_confirms_and_sends_when_the_state_differs(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    async with PJLinkStub(initial_power="0") as stub:
        async with projector_rig(db, state, bus, stub) as (_manager, service):
            handler = ProjectorPowerHandler(service)
            outcome = await handler.execute(
                make_action("projector_power", projector_power="on"), ctx()
            )
            assert outcome.result == "confirmed"
            assert power_set_commands(stub) == ["%1POWR 1"]
            assert service.snapshot().state == "on"


async def test_power_already_in_the_requested_state_sends_nothing(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    async with PJLinkStub(initial_power="1") as stub:  # already on
        async with projector_rig(db, state, bus, stub) as (_manager, service):
            handler = ProjectorPowerHandler(service)
            outcome = await handler.execute(
                make_action("projector_power", projector_power="on"), ctx()
            )
            assert outcome.result == "confirmed"
            assert outcome.reason == "already on"
            assert power_set_commands(stub) == []  # nothing sent, on the stub's own record


async def test_power_during_warmup_fails_with_the_state_as_the_reason(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    async with PJLinkStub(initial_power="0", warm_seconds=5.0) as stub:
        async with projector_rig(db, state, bus, stub) as (_manager, service):
            await service.set_power(True)  # starts warming
            assert service.snapshot().state == "warming"
            handler = ProjectorPowerHandler(service)
            outcome = await handler.execute(
                make_action("projector_power", projector_power="off"), ctx()
            )
            assert outcome.result == "failed"
            assert outcome.reason == "the projector is warming"
            assert outcome.detail["state"] == "warming"


async def test_power_context_discarded_is_respected_before_the_service_call(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    async with PJLinkStub(initial_power="0") as stub:
        async with projector_rig(db, state, bus, stub) as (_manager, service):
            handler = ProjectorPowerHandler(service)
            outcome = await handler.execute(
                make_action("projector_power", projector_power="on"), ctx(discarded=True)
            )
            assert outcome.result == "skipped"
            assert power_set_commands(stub) == []


# -- projector_input ------------------------------------------------------------------


async def test_input_confirms_when_on(db: Database, state: StateStore, bus: EventBus) -> None:
    async with PJLinkStub(initial_power="1") as stub:  # already on
        async with projector_rig(db, state, bus, stub) as (_manager, service):
            handler = ProjectorInputHandler(service)
            outcome = await handler.execute(
                make_action("projector_input", projector_input="32"), ctx()
            )
            assert outcome.result == "confirmed"
            assert input_set_commands(stub) == ["%1INPT 32"]
            assert service.snapshot().input_ref == "32"


async def test_input_while_warming_fails_with_the_reason(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    async with PJLinkStub(initial_power="0", warm_seconds=5.0) as stub:
        async with projector_rig(db, state, bus, stub) as (_manager, service):
            await service.set_power(True)  # starts warming
            handler = ProjectorInputHandler(service)
            outcome = await handler.execute(
                make_action("projector_input", projector_input="31"), ctx()
            )
            assert outcome.result == "failed"
            assert outcome.reason == "the projector is warming"
            assert input_set_commands(stub) == []


async def test_input_fails_while_off(db: Database, state: StateStore, bus: EventBus) -> None:
    async with PJLinkStub(initial_power="0") as stub:
        async with projector_rig(db, state, bus, stub) as (_manager, service):
            handler = ProjectorInputHandler(service)
            outcome = await handler.execute(
                make_action("projector_input", projector_input="31"), ctx()
            )
            assert outcome.result == "failed"
            assert outcome.reason == "the projector is off"
            assert input_set_commands(stub) == []


def test_input_unsupported_gates_a_ref_the_projector_does_not_list() -> None:
    handler = ProjectorInputHandler(None)
    caps = ProjectorCapabilities(inputs=("31", "32"), supports_authentication=False)
    unknown = make_action("projector_input", projector_input="99")
    known = make_action("projector_input", projector_input="31")
    assert handler.unsupported(unknown, caps) is not None
    assert handler.unsupported(known, caps) is None


async def test_input_context_discarded_is_respected_before_the_service_call(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    async with PJLinkStub(initial_power="1") as stub:
        async with projector_rig(db, state, bus, stub) as (_manager, service):
            handler = ProjectorInputHandler(service)
            outcome = await handler.execute(
                make_action("projector_input", projector_input="32"), ctx(discarded=True)
            )
            assert outcome.result == "skipped"
            assert input_set_commands(stub) == []


# -- §8.13's worked example, end to end, and unsupported at run time -----------------


async def test_worked_example_power_then_input_end_to_end(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    """§8.13: power at t=0, input at the later delay once it has warmed — both
    ``✓``. The stub's warm-up, the driver's own transition poll and the
    scene's delay are all compressed."""
    async with fast_projector_polling():
        async with PJLinkStub(initial_power="0", warm_seconds=0.05) as stub:
            async with projector_rig(db, state, bus, stub) as (manager, service):
                engine = SceneEngine(db, state, devices=manager)
                engine.handlers.register("projector_power", ProjectorPowerHandler(service))
                engine.handlers.register("projector_input", ProjectorInputHandler(service))
                scene = await make_scene(
                    db,
                    {"domain": "projector_power", "delay_ms": 0, "projector_power": "on"},
                    {"domain": "projector_input", "delay_ms": 500, "projector_input": "31"},
                )
                try:
                    handle = await engine.run(scene.id, triggered_by="api:admin")
                    result = await handle.result()
                finally:
                    await engine.stop()

    assert result.result == "success"
    outcomes = {a.domain: a.result for a in result.actions}
    assert outcomes == {"projector_power": "confirmed", "projector_input": "confirmed"}


async def test_unsupported_input_ref_is_skipped_at_run_time(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    async with PJLinkStub(initial_power="1") as stub:  # already on
        async with projector_rig(db, state, bus, stub) as (manager, service):
            engine = SceneEngine(db, state, devices=manager)
            engine.handlers.register("projector_power", ProjectorPowerHandler(service))
            engine.handlers.register("projector_input", ProjectorInputHandler(service))
            scene = await make_scene(
                db, {"domain": "projector_input", "delay_ms": 0, "projector_input": "99"}
            )
            try:
                handle = await engine.run(scene.id, triggered_by="api:admin")
                result = await handle.result()
            finally:
                await engine.stop()

    # decision C (§8.15/§8.16): a run with nothing ✓ and nothing ✗ is partial.
    assert result.result == "partial"
    assert result.actions[0].result == "unsupported"
    assert input_set_commands(stub) == []


# -- the HDMI rig: the real LKV422Driver over a loopback transport -------------------


class FakeDriverSource:
    """The minimal :class:`~proskenion.core.video.DriverSource` a test wires
    up itself — the same shape ``tests/unit/core/test_video.py`` uses,
    reimplemented locally rather than imported (that module's own copy is
    not a shared fixture)."""

    def __init__(self) -> None:
        self._drivers: dict[int, Driver] = {}

    def attach(self, device_id: int, driver: Driver) -> None:
        self._drivers[device_id] = driver

    def running_driver(self, device_id: int) -> Driver | None:
        return self._drivers.get(device_id)

    def state_key(self, device_id: int) -> str | None:
        return "hdmi" if device_id in self._drivers else None


@asynccontextmanager
async def lkv422_pair(
    *, ack_switches: bool = False
) -> AsyncIterator[tuple[LKV422Driver, LKV422Stub]]:
    transport = LoopbackTransport()
    await transport.open()
    async with LKV422Stub(transport, ack_switches=ack_switches) as stub:
        driver = LKV422Driver(1, transport, {}, RecordingSink())
        driver.READ_TIMEOUT = 0.3
        driver.QUIET_PERIOD_S = 0.03
        driver.DRAIN_TIMEOUT_S = 0.01
        yield driver, stub


async def make_matrix_device(db: Database) -> devices_crud.Device:
    return await devices_crud.create(
        db, category="video_matrix", driver_key="lkv422", name="Matrix", config={}
    )


@dataclass
class DestinationRig:
    device_id: int
    destination_id: int
    inputs: list[video_crud.MatrixInput]


async def build_destination(db: Database, *, default_input: bool = False) -> DestinationRig:
    device = await make_matrix_device(db)
    inputs = [
        await video_crud.create_input(db, device_id=device.id, driver_ref=str(n), name=f"In {n}")
        for n in (1, 2)
    ]
    output = await video_crud.create_output(db, device_id=device.id, driver_ref="1", name="Out 1")
    destination = await video_crud.create_destination(
        db,
        device_id=device.id,
        name="The room",
        default_input_id=inputs[0].id if default_input else None,
    )
    await video_crud.set_destination_outputs(db, destination.id, [output.id])
    return DestinationRig(device.id, destination.id, inputs)


# -- hdmi_source -----------------------------------------------------------------------


async def test_hdmi_source_with_an_explicit_input_confirms(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    rig = await build_destination(db)
    devices = FakeDriverSource()
    video = VideoService(state, bus, db, devices)
    async with lkv422_pair() as (driver, _stub):
        devices.attach(rig.device_id, driver)
        await video.start()
        try:
            handler = HdmiSourceHandler(video, db)
            action = make_action(
                "hdmi_source", hdmi_destination=rig.destination_id, hdmi_input_id=rig.inputs[1].id
            )
            outcome = await handler.execute(action, ctx())
        finally:
            await video.stop()

    assert outcome.result == "confirmed"
    assert outcome.detail["input_id"] == rig.inputs[1].id


async def test_hdmi_source_with_no_input_id_routes_to_the_venue_default(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    rig = await build_destination(db, default_input=True)
    devices = FakeDriverSource()
    video = VideoService(state, bus, db, devices)
    async with lkv422_pair() as (driver, _stub):
        devices.attach(rig.device_id, driver)
        await video.start()
        try:
            handler = HdmiSourceHandler(video, db)
            action = make_action(
                "hdmi_source", hdmi_destination=rig.destination_id, hdmi_input_id=None
            )
            outcome = await handler.execute(action, ctx())
        finally:
            await video.stop()

    assert outcome.result == "confirmed"
    assert outcome.detail["input_id"] == rig.inputs[0].id
    assert outcome.detail["default"] is True


async def test_hdmi_source_with_no_default_fails(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    rig = await build_destination(db, default_input=False)
    video = VideoService(state, bus, db, FakeDriverSource())  # never connected — fails first
    handler = HdmiSourceHandler(video, db)
    action = make_action("hdmi_source", hdmi_destination=rig.destination_id, hdmi_input_id=None)
    outcome = await handler.execute(action, ctx())
    assert outcome.result == "failed"
    assert outcome.reason == "no default input"


async def test_hdmi_source_context_discarded_is_respected_before_the_service_call(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    rig = await build_destination(db)
    devices = FakeDriverSource()
    video = VideoService(state, bus, db, devices)
    async with lkv422_pair() as (driver, stub):
        devices.attach(rig.device_id, driver)
        await video.start()
        try:
            before = list(stub.received)
            handler = HdmiSourceHandler(video, db)
            action = make_action(
                "hdmi_source", hdmi_destination=rig.destination_id, hdmi_input_id=rig.inputs[1].id
            )
            outcome = await handler.execute(action, ctx(discarded=True))
            assert stub.received == before  # nothing further sent to the wire
        finally:
            await video.stop()

    assert outcome.result == "skipped"
