"""The projector service against the PJLink stub, through the real driver (spec §7.4, §5.6).

Every test runs the real :class:`~proskenion.core.drivers.pjlink.PJLinkDriver`
over the TCP transport against :class:`~tests.stubs.pjlink_stub.PJLinkStub`,
supervised by the real :class:`~proskenion.core.devices.DeviceManager` — the
same stack the API and the rules engine sit on top of.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from proskenion.config import Config
from proskenion.core.broadcast import Message
from proskenion.core.bus import EventBus
from proskenion.core.devices import DeviceManager
from proskenion.core.drivers.capabilities import ProjectorState
from proskenion.core.events import ProjectorStateChanged
from proskenion.core.projector import (
    NoProjectorConfigured,
    ProjectorService,
    ProjectorUnavailable,
    UnknownProjectorInput,
)
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from tests.stubs.pjlink_stub import PJLinkStub


class ManualClock:
    """The service's clock and sleep for the minimum warm-up hold (§7.4):
    time moves only when a test calls :meth:`advance`."""

    def __init__(self) -> None:
        self.now = 1000.0
        self._wakers: list[asyncio.Event] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        until = self.now + delay
        while self.now < until:
            waker = asyncio.Event()
            self._wakers.append(waker)
            await waker.wait()

    async def advance(self, seconds: float) -> None:
        self.now += seconds
        wakers, self._wakers = self._wakers, []
        for waker in wakers:
            waker.set()
        for _ in range(5):  # let a woken hold task run up to its first real I/O
            await asyncio.sleep(0)


class FakePublisher:
    """Stands in for the broadcaster: records every ``projector_state`` frame."""

    def __init__(self) -> None:
        self.messages: list[Message] = []

    def publish(self, message: Message) -> int:
        self.messages.append(message)
        return 1


@dataclass
class Rig:
    bus: EventBus
    state: StateStore
    devices: DeviceManager
    service: ProjectorService
    publisher: FakePublisher
    events: list[ProjectorStateChanged]
    device_id: int | None


async def _build(
    db: Database,
    dev_config: Config,
    stub: PJLinkStub | None,
    *,
    password: str | None = None,
    min_warmup_s: int | None = None,
    clock: ManualClock | None = None,
) -> Rig:
    bus = EventBus()
    await bus.start()
    state = StateStore(dev_config, bus)
    device_id: int | None = None
    if stub is not None:
        driver: dict[str, Any] = {"password": password}
        if min_warmup_s is not None:  # None: the driver's own default (60 s)
            driver["min_warmup_s"] = min_warmup_s
        device = await devices_crud.create(
            db,
            category="projector",
            driver_key="pjlink",
            name="Projector",
            config={
                "transport": {"type": "tcp", "host": "127.0.0.1", "port": stub.port},
                "driver": driver,
            },
        )
        device_id = device.id
    manager = DeviceManager(
        db, state, bus, dev_config, connect_timeout=2.0, probe_timeout=1.0, stop_timeout=2.0
    )
    await manager.start()
    if device_id is not None:
        await manager.wait_for_connection(device_id)
    events: list[ProjectorStateChanged] = []

    async def collect(event: ProjectorStateChanged) -> None:
        events.append(event)

    bus.subscribe(ProjectorStateChanged, collect, name="test:projector-events")
    publisher = FakePublisher()
    if clock is None:
        service = ProjectorService(state, bus, db, manager, broadcaster=publisher)
    else:
        service = ProjectorService(
            state, bus, db, manager, broadcaster=publisher, clock=clock.monotonic, sleep=clock.sleep
        )
    await service.start()
    return Rig(bus, state, manager, service, publisher, events, device_id)


async def _teardown(rig: Rig) -> None:
    await rig.service.stop()
    await rig.devices.stop()
    await rig.bus.stop()


async def _wait_for(predicate: Callable[[], bool], within: float = 2.0) -> None:
    """The bus delivers ``ProjectorStateChanged`` on its own consumer task
    (§5.6): a write to ``state.projector`` is synchronous and immediately
    visible, but ``rig.events`` needs at least one event-loop turn to catch
    up, so every assertion on it polls rather than reading it cold."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


# -- boot: discovery, never a power command (§7.4) ---------------------------------


async def test_boot_discovers_state_and_sends_no_power_command(
    db: Database, dev_config: Config
) -> None:
    async with PJLinkStub() as stub:
        stub.set_power_immediately("1")
        rig = await _build(db, dev_config, stub)
        try:
            assert rig.state.projector.get("state") == "on"
            commands = [r.command for r in stub.received]
            assert not any(
                c.startswith("%1POWR 0") or c.startswith("%1POWR 1") for c in commands
            )
        finally:
            await _teardown(rig)


async def test_absent_projector_sends_nothing_and_leaves_state_empty(
    db: Database, dev_config: Config
) -> None:
    rig = await _build(db, dev_config, None)
    try:
        assert rig.service.device_id is None
        snapshot = rig.service.snapshot()
        assert snapshot.device_id is None
        assert snapshot.state is None
        assert snapshot.input_ref is None
        with pytest.raises(NoProjectorConfigured):
            await rig.service.set_power(True)
        with pytest.raises(NoProjectorConfigured):
            await rig.service.set_input("31")
    finally:
        await _teardown(rig)


# -- one write, one event, one frame per change (§5.6, §16.8) -----------------------


async def test_a_state_change_reaches_the_store_the_event_and_the_frame_once(
    db: Database, dev_config: Config
) -> None:
    async with PJLinkStub() as stub:
        stub.set_power_immediately("0")
        rig = await _build(db, dev_config, stub)
        try:
            assert rig.state.projector.get("state") == "off"
            await _wait_for(lambda: len(rig.events) == 1)
            assert (rig.events[0].state, rig.events[0].previous) == ("off", "unreachable")
            assert rig.publisher.messages == [
                {"type": "projector_state", "state": "off", "input_ref": None}
            ]

            # A real transition: the stub changes (a probe would find this at
            # its next tick); calling probe() directly is the same code path
            # a scheduled probe takes, without waiting out the interval.
            stub.set_power_immediately("1")
            driver = rig.devices.running_driver(rig.device_id)
            assert driver is not None
            await driver.probe()

            assert rig.state.projector.get("state") == "on"
            assert rig.state.projector.get("input_ref") == stub.current_input
            await _wait_for(lambda: len(rig.events) == 2)
            assert (rig.events[1].state, rig.events[1].previous) == ("on", "off")
            assert rig.publisher.messages[-1] == {
                "type": "projector_state",
                "state": "on",
                "input_ref": stub.current_input,
            }

            # No change: probing again must not repeat the event or the frame.
            await driver.probe()
            await asyncio.sleep(0.05)  # give a spurious repeat time to arrive
            assert len(rig.events) == 2
            assert len(rig.publisher.messages) == 2
        finally:
            await _teardown(rig)


async def test_late_listener_attachment_still_reports_the_discovered_state(
    db: Database, dev_config: Config
) -> None:
    """Boot's own ordering (device manager connects before the service can
    listen) — reproduced directly: the driver discovers "warming" before
    ``add_state_listener`` is ever called, and the service must still pick
    it up rather than staying silent until the next real transition."""
    async with PJLinkStub(warm_seconds=5.0) as stub:
        stub.set_power_immediately("3")
        rig = await _build(db, dev_config, stub)
        try:
            assert rig.state.projector.get("state") == "warming"
            await _wait_for(lambda: len(rig.events) == 1)
            assert rig.events[0].state == "warming"
        finally:
            await _teardown(rig)


# -- B52: rejected outright, nothing sent to the stub (§7.4, §16.5) -----------------


async def test_power_and_input_are_refused_while_warming_with_nothing_sent(
    db: Database, dev_config: Config
) -> None:
    async with PJLinkStub(warm_seconds=5.0) as stub:
        stub.set_power_immediately("3")
        rig = await _build(db, dev_config, stub)
        try:
            received_before = len(stub.received)

            with pytest.raises(ProjectorUnavailable) as power_exc:
                await rig.service.set_power(True)
            assert power_exc.value.state == "warming"
            assert power_exc.value.reason == "transitioning"

            with pytest.raises(ProjectorUnavailable) as input_exc:
                await rig.service.set_input("31")
            assert input_exc.value.state == "warming"
            assert input_exc.value.reason == "transitioning"

            assert len(stub.received) == received_before  # the stub saw neither command
        finally:
            await _teardown(rig)


async def test_power_and_input_are_refused_while_cooling_with_nothing_sent(
    db: Database, dev_config: Config
) -> None:
    async with PJLinkStub(cool_seconds=5.0) as stub:
        stub.set_power_immediately("2")
        rig = await _build(db, dev_config, stub)
        try:
            received_before = len(stub.received)

            with pytest.raises(ProjectorUnavailable) as power_exc:
                await rig.service.set_power(False)
            assert power_exc.value.state == "cooling"
            assert power_exc.value.reason == "transitioning"

            assert len(stub.received) == received_before
        finally:
            await _teardown(rig)


# -- commands: success re-reads, unreachable, unknown input -------------------------


async def test_set_power_on_reads_back_state_and_input(
    db: Database, dev_config: Config
) -> None:
    """With the minimum warm-up disabled (0), the pre-2026-09-30 behaviour:
    the projector's own report is shown at once (§7.4)."""
    async with PJLinkStub(inputs=("11", "31")) as stub:
        stub.set_power_immediately("0")
        rig = await _build(db, dev_config, stub, min_warmup_s=0)
        try:
            await rig.service.set_power(True)
            assert rig.state.projector.get("state") == "on"
            assert rig.state.projector.get("input_ref") == stub.current_input
        finally:
            await _teardown(rig)


async def test_set_input_rejects_a_ref_the_projector_does_not_list(
    db: Database, dev_config: Config
) -> None:
    async with PJLinkStub(inputs=("11", "31")) as stub:
        stub.set_power_immediately("1")
        rig = await _build(db, dev_config, stub)
        try:
            with pytest.raises(UnknownProjectorInput) as excinfo:
                await rig.service.set_input("99")
            assert excinfo.value.input_ref == "99"
        finally:
            await _teardown(rig)


async def test_set_input_accepted_updates_the_store_and_publishes(
    db: Database, dev_config: Config
) -> None:
    async with PJLinkStub(inputs=("11", "31")) as stub:
        stub.set_power_immediately("1")
        stub.current_input = "11"
        rig = await _build(db, dev_config, stub)
        try:
            before = len(rig.publisher.messages)

            await rig.service.set_input("31")

            assert rig.state.projector.get("input_ref") == "31"
            assert stub.current_input == "31"
            assert len(rig.publisher.messages) == before + 1
            assert rig.publisher.messages[-1]["input_ref"] == "31"
        finally:
            await _teardown(rig)


async def test_commands_answer_unreachable_when_the_device_is_not_connected(
    db: Database, dev_config: Config
) -> None:
    # A device row that points nowhere real: never connects, so
    # ``running_driver`` stays ``None`` throughout.
    bus = EventBus()
    await bus.start()
    state = StateStore(dev_config, bus)
    await devices_crud.create(
        db,
        category="projector",
        driver_key="pjlink",
        name="Projector",
        config={"transport": {"type": "tcp", "host": "127.0.0.1", "port": 1}, "driver": {}},
    )
    manager = DeviceManager(
        db, state, bus, dev_config, connect_timeout=0.2, probe_timeout=0.2, stop_timeout=1.0
    )
    await manager.start()
    service = ProjectorService(state, bus, db, manager)
    await service.start()
    try:
        with pytest.raises(ProjectorUnavailable) as excinfo:
            await service.set_power(True)
        assert excinfo.value.state == "unreachable"
        assert excinfo.value.reason is None
    finally:
        await service.stop()
        await manager.stop()
        await bus.stop()


# -- the minimum warm-up hold (§7.4, B52; owner decision 2026-09-30) ----------------
#
# On the rig the projector reported "warming" for 13.4 s, then "on" while its
# lamp was still visibly warming, and an off at +20 s went through. From our
# own accepted power-on the service now shows "warming" for ``min_warmup_s``
# (default 60 s) even once PJLink says "on". The stub here warms instantly —
# reports "on" at once — the worst case of that finding. A restart during a
# hold needs no test of its own: the service keeps no hold across a restart,
# which is exactly test_boot_discovers_state_and_sends_no_power_command.


async def _probe(rig: Rig) -> None:
    """``probe()`` is the same code path a scheduled probe takes."""
    driver = rig.devices.running_driver(rig.device_id) if rig.device_id else None
    assert driver is not None
    await driver.probe()


async def _projector_reports_on(rig: Rig, stub: PJLinkStub) -> None:
    """Let the stub finish its (instant) warm-up and have the driver see it."""
    await _wait_for(lambda: stub.power == "1")
    await _probe(rig)


def _power_commands(stub: PJLinkStub) -> list[str]:
    return [r.command for r in stub.received if r.command in ("%1POWR 0", "%1POWR 1")]


async def test_hold_shows_warming_until_min_warmup_even_once_the_projector_says_on(
    db: Database, dev_config: Config
) -> None:
    clock = ManualClock()
    async with PJLinkStub(inputs=("11", "31")) as stub:
        stub.set_power_immediately("0")
        rig = await _build(db, dev_config, stub, clock=clock)
        try:
            await rig.service.set_power(True)
            await _projector_reports_on(rig, stub)

            assert rig.state.projector.get("state") == "warming"
            assert rig.service.snapshot().state == "warming"
            assert rig.service.warmup_remaining_s() == 60.0
            assert rig.publisher.messages[-1]["state"] == "warming"

            await clock.advance(59.5)
            assert rig.state.projector.get("state") == "warming"
            assert rig.service.warmup_remaining_s() == 1.0  # whole seconds, rounded up

            await clock.advance(0.5)
            # The frame follows the input read that the `on` transition makes.
            await _wait_for(lambda: rig.publisher.messages[-1]["state"] == "on")
            assert rig.state.projector.get("state") == "on"
            assert rig.service.warmup_remaining_s() is None
            assert rig.state.projector.get("input_ref") == stub.current_input
            assert rig.publisher.messages[-1] == {
                "type": "projector_state",
                "state": "on",
                "input_ref": stub.current_input,
            }
            await _wait_for(lambda: len(rig.events) == 3)
            assert [(e.state, e.previous) for e in rig.events] == [
                ("off", "unreachable"),
                ("warming", "off"),
                ("on", "warming"),
            ]

            # The `on` transition fired once: a later probe adds nothing.
            await _probe(rig)
            await asyncio.sleep(0.05)
            assert [e.state for e in rig.events].count("on") == 1
        finally:
            await _teardown(rig)


async def test_off_refused_at_20_s_and_accepted_at_61_s(
    db: Database, dev_config: Config
) -> None:
    clock = ManualClock()
    async with PJLinkStub() as stub:
        stub.set_power_immediately("0")
        rig = await _build(db, dev_config, stub, clock=clock)
        try:
            await rig.service.set_power(True)
            await _projector_reports_on(rig, stub)

            await clock.advance(20)
            with pytest.raises(ProjectorUnavailable) as refused:
                await rig.service.set_power(False)
            assert (refused.value.state, refused.value.reason) == ("warming", "transitioning")
            assert _power_commands(stub) == ["%1POWR 1"]  # the off never reached the wire

            await clock.advance(41)
            await _wait_for(lambda: rig.state.projector.get("state") == "on")
            await rig.service.set_power(False)
            assert _power_commands(stub) == ["%1POWR 1", "%1POWR 0"]
        finally:
            await _teardown(rig)


async def test_input_is_refused_during_the_hold_with_nothing_sent(
    db: Database, dev_config: Config
) -> None:
    clock = ManualClock()
    async with PJLinkStub(inputs=("11", "31")) as stub:
        stub.set_power_immediately("0")
        stub.current_input = "11"
        rig = await _build(db, dev_config, stub, clock=clock)
        try:
            await rig.service.set_power(True)
            await _projector_reports_on(rig, stub)
            await clock.advance(30)

            with pytest.raises(ProjectorUnavailable) as refused:
                await rig.service.set_input("31")
            assert (refused.value.state, refused.value.reason) == ("warming", "transitioning")
            assert not any(r.command == "%1INPT 31" for r in stub.received)
            assert stub.current_input == "11"
        finally:
            await _teardown(rig)


async def test_a_projector_reporting_off_during_the_hold_is_shown_off_at_once(
    db: Database, dev_config: Config
) -> None:
    """A failed start wins immediately: the hold never keeps showing warming
    for a projector that says it is off."""
    clock = ManualClock()
    async with PJLinkStub() as stub:
        stub.set_power_immediately("0")
        rig = await _build(db, dev_config, stub, clock=clock)
        try:
            await rig.service.set_power(True)
            await _projector_reports_on(rig, stub)
            await clock.advance(10)
            assert rig.state.projector.get("state") == "warming"

            stub.set_power_immediately("0")
            await _probe(rig)

            assert rig.state.projector.get("state") == "off"
            assert rig.service.warmup_remaining_s() is None
            assert rig.publisher.messages[-1]["state"] == "off"
            # And with the projector off, power-on is not refused.
            await rig.service.set_power(True)
            assert _power_commands(stub) == ["%1POWR 1", "%1POWR 1"]
        finally:
            await _teardown(rig)


@pytest.mark.parametrize("fault", [ProjectorState.ERROR, ProjectorState.UNREACHABLE])
async def test_a_fault_during_the_hold_is_never_masked(
    db: Database, dev_config: Config, fault: ProjectorState
) -> None:
    """``error`` and ``unreachable`` reports arrive through the same driver
    listener a probe calls (§7.4); both are shown as reported, mid-hold."""
    clock = ManualClock()
    async with PJLinkStub() as stub:
        stub.set_power_immediately("0")
        rig = await _build(db, dev_config, stub, clock=clock)
        try:
            await rig.service.set_power(True)
            await _projector_reports_on(rig, stub)
            await clock.advance(5)

            await rig.service._on_driver_state_changed(fault, ProjectorState.ON)

            assert rig.state.projector.get("state") == fault.value
            assert rig.service.warmup_remaining_s() is None
            # The hold's end changes nothing further.
            await clock.advance(60)
            await asyncio.sleep(0.05)
            assert rig.state.projector.get("state") == fault.value
        finally:
            await _teardown(rig)


async def test_min_warmup_zero_shows_on_as_soon_as_the_projector_reports_it(
    db: Database, dev_config: Config
) -> None:
    clock = ManualClock()
    async with PJLinkStub() as stub:
        stub.set_power_immediately("0")
        rig = await _build(db, dev_config, stub, min_warmup_s=0, clock=clock)
        try:
            await rig.service.set_power(True)
            await _projector_reports_on(rig, stub)
            assert rig.state.projector.get("state") == "on"
            assert rig.service.warmup_remaining_s() is None
            await rig.service.set_power(False)  # no hold: accepted at once
            assert _power_commands(stub) == ["%1POWR 1", "%1POWR 0"]
        finally:
            await _teardown(rig)


async def test_a_custom_min_warmup_is_honoured(db: Database, dev_config: Config) -> None:
    clock = ManualClock()
    async with PJLinkStub() as stub:
        stub.set_power_immediately("0")
        rig = await _build(db, dev_config, stub, min_warmup_s=90, clock=clock)
        try:
            await rig.service.set_power(True)
            await _projector_reports_on(rig, stub)
            assert rig.service.warmup_remaining_s() == 90.0
            await clock.advance(61)
            assert rig.state.projector.get("state") == "warming"
            await clock.advance(29)
            await _wait_for(lambda: rig.state.projector.get("state") == "on")
        finally:
            await _teardown(rig)


async def test_a_real_warm_up_longer_than_the_hold_still_shows_warming(
    db: Database, dev_config: Config
) -> None:
    """The hold is a minimum: when it ends with the projector still
    reporting warming, the state stays warming until the projector says on."""
    clock = ManualClock()
    async with PJLinkStub(warm_seconds=30.0) as stub:
        stub.set_power_immediately("0")
        rig = await _build(db, dev_config, stub, clock=clock)
        try:
            await rig.service.set_power(True)
            assert stub.power == "3"
            await clock.advance(60)
            await asyncio.sleep(0.05)
            assert rig.state.projector.get("state") == "warming"
            assert rig.service.warmup_remaining_s() is None  # nothing left to count

            stub.set_power_immediately("1")
            await _probe(rig)
            assert rig.state.projector.get("state") == "on"
        finally:
            await _teardown(rig)


async def test_power_on_to_a_projector_already_on_starts_no_hold(
    db: Database, dev_config: Config
) -> None:
    clock = ManualClock()
    async with PJLinkStub() as stub:
        stub.set_power_immediately("1")
        rig = await _build(db, dev_config, stub, clock=clock)
        try:
            await rig.service.set_power(True)
            assert rig.state.projector.get("state") == "on"
            assert rig.service.warmup_remaining_s() is None
        finally:
            await _teardown(rig)
