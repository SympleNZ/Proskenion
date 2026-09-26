"""The HDMI matrix service (spec §7.5, §12.1-12.3): set_source, out-of-band
detection through the driver's own routing-poll listener (never a second
poll), and boot's query-not-route rule — against the LKV422 stub through the
real driver, and against the retained :class:`StubMatrixDriver`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import pytest

from proskenion.config import Config
from proskenion.core.broadcast import BROADCAST_DOMAINS, Broadcaster, Connection, Message
from proskenion.core.bus import EventBus
from proskenion.core.drivers.base import Driver
from proskenion.core.drivers.lkv422 import LKV422Driver
from proskenion.core.drivers.stub_matrix import StubMatrixDriver
from proskenion.core.events import VideoConfigChanged, VideoSourceChanged
from proskenion.core.state import StateStore
from proskenion.core.transport.loopback import LoopbackTransport
from proskenion.core.video import (
    MatrixOfflineError,
    RouteNotConfirmedError,
    UnknownDestinationError,
    UnknownInputError,
    VideoService,
)
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import video as video_crud
from tests.stubs.echo_driver import RecordingSink
from tests.stubs.lkv422_stub import LKV422Stub

pytestmark = pytest.mark.asyncio


# -- fixtures -----------------------------------------------------------------------


@pytest.fixture
async def bus() -> AsyncIterator[EventBus]:
    event_bus = EventBus()
    await event_bus.start()
    try:
        yield event_bus
    finally:
        await event_bus.stop()


@pytest.fixture
def state(dev_config: Config, bus: EventBus) -> StateStore:
    return StateStore(dev_config, bus)


class FakeDeviceSource:
    """A minimal :class:`~proskenion.core.video.DriverSource` — just enough
    of the device manager for the video service, backed by a driver the test
    wires up itself rather than a real serial or loopback-through-DeviceManager
    connection (the LKV422 driver's ``SUPPORTED_TRANSPORTS`` is serial-only)."""

    def __init__(self) -> None:
        self._drivers: dict[int, Driver] = {}

    def attach(self, device_id: int, driver: Driver) -> None:
        self._drivers[device_id] = driver

    def detach(self, device_id: int) -> None:
        self._drivers.pop(device_id, None)

    def running_driver(self, device_id: int) -> Driver | None:
        return self._drivers.get(device_id)

    def state_key(self, device_id: int) -> str | None:
        return "hdmi" if device_id in self._drivers else None


@asynccontextmanager
async def lkv422_pair(
    *, ack_switches: bool = False, settle_ms: int = 0
) -> AsyncIterator[tuple[LKV422Driver, LKV422Stub]]:
    """A real :class:`LKV422Driver` and :class:`LKV422Stub`, joined by a real
    loopback transport — the same shape ``tests/unit/core/drivers/test_lkv422.py``
    uses, kept local here since that module's own pair helper is private."""
    transport = LoopbackTransport()
    await transport.open()
    async with LKV422Stub(transport, ack_switches=ack_switches) as stub:
        driver = LKV422Driver(1, transport, {"settle_ms": settle_ms}, RecordingSink())
        driver.READ_TIMEOUT = 0.3
        driver.QUIET_PERIOD_S = 0.03
        driver.DRAIN_TIMEOUT_S = 0.01
        yield driver, stub


async def make_matrix_device(db: Database, *, driver_key: str = "lkv422") -> devices_crud.Device:
    return await devices_crud.create(
        db, category="video_matrix", driver_key=driver_key, name="Matrix", config={}
    )


def matrix_only_commands(stub: LKV422Stub) -> list[bytes]:
    """``stub.received`` minus the ``PAXXR`` routing reads — what's left is
    only ever a switch (§7.5: boot and the routing poll never route)."""
    return [c for c in stub.received if c != b"PAXXR"]


# -- set_source: one route call, encoded per §5.5/§7.5 -------------------------------


async def test_set_source_on_a_two_output_destination_sends_one_pa_command(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    device = await make_matrix_device(db)
    inputs = [
        await video_crud.create_input(db, device_id=device.id, driver_ref=str(n), name=f"In {n}")
        for n in range(1, 5)
    ]
    outputs = [
        await video_crud.create_output(db, device_id=device.id, driver_ref=str(n), name=f"Out {n}")
        for n in (1, 2)
    ]
    destination = await video_crud.create_destination(db, device_id=device.id, name="The room")
    await video_crud.set_destination_outputs(db, destination.id, [o.id for o in outputs])

    devices = FakeDeviceSource()
    service = VideoService(state, bus, db, devices)
    async with lkv422_pair() as (driver, stub):
        devices.attach(device.id, driver)
        await service.start()
        try:
            await service.set_source(destination.id, inputs[2].id)  # input "3"
        finally:
            await service.stop()

    sent = matrix_only_commands(stub)
    assert sent == [b"PA3R"]


async def test_set_source_on_a_one_output_destination_sends_a_ps_command(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    device = await make_matrix_device(db)
    inputs = [
        await video_crud.create_input(db, device_id=device.id, driver_ref=str(n), name=f"In {n}")
        for n in range(1, 5)
    ]
    outputs = [
        await video_crud.create_output(db, device_id=device.id, driver_ref=str(n), name=f"Out {n}")
        for n in (1, 2)
    ]
    destination = await video_crud.create_destination(db, device_id=device.id, name="Foyer")
    await video_crud.set_destination_outputs(db, destination.id, [outputs[0].id])  # output "1" only

    devices = FakeDeviceSource()
    service = VideoService(state, bus, db, devices)
    async with lkv422_pair() as (driver, stub):
        devices.attach(device.id, driver)
        await service.start()
        try:
            await service.set_source(destination.id, inputs[1].id)  # input "2"
        finally:
            await service.stop()

    sent = matrix_only_commands(stub)
    assert sent == [b"PS12R"]


async def test_a_failed_confirmation_answers_route_not_confirmed(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    device = await make_matrix_device(db)
    inputs = [
        await video_crud.create_input(db, device_id=device.id, driver_ref=str(n), name=f"In {n}")
        for n in range(1, 5)
    ]
    outputs = [
        await video_crud.create_output(db, device_id=device.id, driver_ref=str(n), name=f"Out {n}")
        for n in (1, 2)
    ]
    destination = await video_crud.create_destination(db, device_id=device.id, name="The room")
    await video_crud.set_destination_outputs(db, destination.id, [o.id for o in outputs])

    devices = FakeDeviceSource()
    service = VideoService(state, bus, db, devices)
    async with lkv422_pair() as (driver, stub):
        stub.accept_switches = False  # the switch never actually takes effect
        devices.attach(device.id, driver)
        await service.start()
        try:
            with pytest.raises(RouteNotConfirmedError):
                await service.set_source(destination.id, inputs[2].id)
        finally:
            await service.stop()


async def test_an_input_of_another_device_is_refused(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    device = await make_matrix_device(db)
    other_device = await make_matrix_device(db)
    outputs = [
        await video_crud.create_output(db, device_id=device.id, driver_ref=str(n), name=f"Out {n}")
        for n in (1, 2)
    ]
    destination = await video_crud.create_destination(db, device_id=device.id, name="The room")
    await video_crud.set_destination_outputs(db, destination.id, [o.id for o in outputs])
    foreign_input = await video_crud.create_input(
        db, device_id=other_device.id, driver_ref="1", name="Elsewhere"
    )

    devices = FakeDeviceSource()
    service = VideoService(state, bus, db, devices)
    async with lkv422_pair() as (driver, _stub):
        devices.attach(device.id, driver)
        await service.start()
        try:
            with pytest.raises(UnknownInputError):
                await service.set_source(destination.id, foreign_input.id)
        finally:
            await service.stop()


async def test_an_unknown_destination_is_refused(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    devices = FakeDeviceSource()
    service = VideoService(state, bus, db, devices)
    with pytest.raises(UnknownDestinationError):
        await service.set_source(999, 1)


async def test_set_source_while_the_matrix_is_offline_answers_matrix_offline(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    device = await make_matrix_device(db)
    input_row = await video_crud.create_input(db, device_id=device.id, driver_ref="1", name="In 1")
    output = await video_crud.create_output(db, device_id=device.id, driver_ref="1", name="Out 1")
    destination = await video_crud.create_destination(db, device_id=device.id, name="The room")
    await video_crud.set_destination_outputs(db, destination.id, [output.id])

    devices = FakeDeviceSource()  # nothing attached — the matrix is not connected
    service = VideoService(state, bus, db, devices)
    with pytest.raises(MatrixOfflineError):
        await service.set_source(destination.id, input_row.id)


# -- boot: query, never route (§12.2) ------------------------------------------------


async def test_boot_reads_the_routing_and_never_routes(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    device = await make_matrix_device(db)
    devices = FakeDeviceSource()
    service = VideoService(state, bus, db, devices)
    async with lkv422_pair() as (driver, stub):
        devices.attach(device.id, driver)
        await service.start()
        try:
            assert stub.received == [b"PAXXR"]
            assert matrix_only_commands(stub) == []
        finally:
            await service.stop()


async def test_the_service_also_works_with_stub_matrix(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    device = await make_matrix_device(db, driver_key="stub")
    input_row = await video_crud.create_input(db, device_id=device.id, driver_ref="2", name="In 2")
    output = await video_crud.create_output(db, device_id=device.id, driver_ref="1", name="Out 1")
    destination = await video_crud.create_destination(db, device_id=device.id, name="The room")
    await video_crud.set_destination_outputs(db, destination.id, [output.id])

    transport = LoopbackTransport()
    await transport.open()
    driver = StubMatrixDriver(device.id, transport, {}, RecordingSink())
    devices = FakeDeviceSource()
    devices.attach(device.id, driver)
    service = VideoService(state, bus, db, devices)
    await service.start()
    try:
        # Boot reads (never routes) — the stub starts with everything on input 1.
        assert state.hdmi.get_item("destinations", destination.id) == {
            "input_id": None,  # driver_ref "1" has no configured matrix_inputs row
            "diverged": False,
        }
        await service.set_source(destination.id, input_row.id)
        assert state.hdmi.get_item("destinations", destination.id) == {
            "input_id": input_row.id,
            "diverged": False,
        }
    finally:
        await service.stop()
        await transport.close()


# -- out-of-band: a front-panel change, without a second poll ------------------------


async def take(connection: Connection, count: int, within: float = 2.0) -> list[Message]:
    messages: list[Message] = []
    async with asyncio.timeout(within):
        async for message in connection.messages():
            messages.append(message)
            if len(messages) >= count:
                break
    return messages


async def test_a_front_panel_change_reaches_state_the_frame_and_the_event(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    device = await make_matrix_device(db)
    inputs = [
        await video_crud.create_input(db, device_id=device.id, driver_ref=str(n), name=f"In {n}")
        for n in range(1, 5)
    ]
    outputs = [
        await video_crud.create_output(db, device_id=device.id, driver_ref=str(n), name=f"Out {n}")
        for n in (1, 2)
    ]
    destination = await video_crud.create_destination(db, device_id=device.id, name="The room")
    await video_crud.set_destination_outputs(db, destination.id, [o.id for o in outputs])

    events: list[VideoSourceChanged] = []

    async def record_event(event: VideoSourceChanged) -> None:
        events.append(event)

    devices = FakeDeviceSource()
    service = VideoService(state, bus, db, devices)
    broadcaster = Broadcaster(state, bus)
    await broadcaster.start()
    try:
        async with lkv422_pair() as (driver, stub):
            driver.PROBE_INTERVAL = 0.02  # a short interval, not 30 s (§11.1)
            devices.attach(device.id, driver)
            await service.start()  # the boot read's own event fires here, before...
            # ... this subscription, so only the front-panel change is captured.
            bus.subscribe(
                VideoSourceChanged, record_event, name="test:video_source", cls="discrete"
            )
            connection = broadcaster.connect(tier="operator", domains=["hdmi"])
            probe_task = asyncio.create_task(driver.probe_periodically())
            try:
                await stub.front_panel("2", "3")  # out-of-band — no RS-232 traffic
                frames = await take(connection, 1)
            finally:
                probe_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await probe_task
                await service.stop()
        await broadcaster.stop()
    finally:
        if broadcaster.connection_count:  # pragma: no cover - defensive
            await broadcaster.stop()

    # Only output "2" moved; the destination's input_id is its *first* output's
    # input (§15.10), output "1", still on driver_ref "1" — and diverged is
    # true because the two outputs now disagree.
    assert frames == [
        {
            "type": "hdmi_source",
            "destination_id": destination.id,
            "input_id": inputs[0].id,  # output "1", driver_ref "1", unchanged
            "diverged": True,
        }
    ]
    assert len(events) == 1
    assert events[0] == VideoSourceChanged(
        destination_id=destination.id, input_id=inputs[0].id, diverged=True
    )
    assert state.hdmi.get_item("destinations", destination.id) == {
        "input_id": inputs[0].id,
        "diverged": True,
    }


async def test_selecting_a_source_reconverges_a_diverged_destination(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    device = await make_matrix_device(db)
    inputs = [
        await video_crud.create_input(db, device_id=device.id, driver_ref=str(n), name=f"In {n}")
        for n in range(1, 5)
    ]
    outputs = [
        await video_crud.create_output(db, device_id=device.id, driver_ref=str(n), name=f"Out {n}")
        for n in (1, 2)
    ]
    destination = await video_crud.create_destination(db, device_id=device.id, name="The room")
    await video_crud.set_destination_outputs(db, destination.id, [o.id for o in outputs])

    devices = FakeDeviceSource()
    service = VideoService(state, bus, db, devices)
    async with lkv422_pair() as (driver, stub):
        devices.attach(device.id, driver)
        await service.start()
        try:
            await stub.front_panel("2", "3")
            await driver.read_routing()  # stands in for the next probe
            assert state.hdmi.get_item("destinations", destination.id) == {
                "input_id": inputs[0].id,
                "diverged": True,
            }
            await service.set_source(destination.id, inputs[2].id)
            assert state.hdmi.get_item("destinations", destination.id) == {
                "input_id": inputs[2].id,
                "diverged": False,
            }
        finally:
            await service.stop()


# -- configuration changed: a destination or its outputs, after the matrix attached -


async def wait_until(predicate: Callable[[], bool], within: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def test_a_destination_created_after_the_matrix_attaches_appears_without_a_routing_change(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    """Smaller finding, docs/phase-3-milestone.md: ``_apply_routing`` otherwise
    runs at attach and on a routing change only, so a destination configured
    afterwards is missing from ``state.hdmi`` until the matrix next routes.
    ``VideoConfigChanged`` — emitted by the admin API once a destination or
    output write commits — makes the service recompute from the routing it
    already knows, without asking the matrix again.
    """
    device = await make_matrix_device(db)
    input_row = await video_crud.create_input(
        db, device_id=device.id, driver_ref="1", name="In 1"
    )

    devices = FakeDeviceSource()
    service = VideoService(state, bus, db, devices)
    async with lkv422_pair() as (driver, stub):
        devices.attach(device.id, driver)
        await service.start()  # attaches with no destinations configured yet
        try:
            assert state.hdmi.get("destinations") == {}

            # Configured only now, after the matrix attached: an output, then
            # a destination naming it — as the admin screen's two writes do.
            output = await video_crud.create_output(
                db, device_id=device.id, driver_ref="1", name="Out 1"
            )
            destination = await video_crud.create_destination(
                db, device_id=device.id, name="The room"
            )
            await video_crud.set_destination_outputs(db, destination.id, [output.id])
            bus.emit(VideoConfigChanged(reason="destination_created"))

            await wait_until(
                lambda: state.hdmi.get_item("destinations", destination.id) is not None
            )
            # The matrix's own routing, from its stub default — not re-read,
            # only recomputed against the now-current configuration.
            assert state.hdmi.get_item("destinations", destination.id) == {
                "input_id": input_row.id,
                "diverged": False,
            }
            assert matrix_only_commands(stub) == []
        finally:
            await service.stop()


async def test_broadcast_domains_still_names_hdmi() -> None:
    assert "hdmi" in BROADCAST_DOMAINS
