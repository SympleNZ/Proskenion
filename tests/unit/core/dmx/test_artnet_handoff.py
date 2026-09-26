"""Art-Net hand-offs over real sockets (spec §7.2.5, §7.2.7, §7.2.8).

The defects these guard against lived *between* parts that each passed their
own tests: the driver polled from an ephemeral port while the node answered
to 6454, and nothing carried the booth's ArtDmx, with its sender, from a
socket to desk detection. So nothing here is mocked below the Art-Net
socket: the controller's side is the real ``artnet`` driver, the real
:class:`~proskenion.core.dmx.endpoint.ArtNetEndpoint` and, for detection,
the real device manager, desk input and lighting service; the far side is
:class:`~tests.stubs.artnet_stub.ArtNetStub` on its own UDP socket.

Distinct source addresses on one machine
----------------------------------------
Source filtering can only be tested with senders at different addresses.
Every address in 127.0.0.0/8 is loopback on Linux and Windows, so the node
stub binds **127.0.0.2** and the "other Art-Net device" (the previous node,
or the legacy controller) binds **127.0.0.3**; the operating system stamps
each datagram with the address its socket is bound to, exactly as the
eDMX8 MAX's and the eDMX4's datagrams arrive on the VLAN. The controller's
own address on that route is 127.0.0.1. Where the platform will not bind
those addresses (macOS, by default) the tests skip rather than fake it.

The Art-Net port
----------------
Production binds 6454. Here :attr:`ArtNetEndpoint.default_port` is set to a
free port ``P`` for the test, standing in for 6454, and the node stub sends
its ArtPollReply to ``127.0.0.1:P`` — a fixed destination, the way the real
node broadcasts to ``<broadcast>:6454`` — never back to whatever port the
poll came from. A driver that polls from any other port never hears it.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest

from proskenion.config import Config
from proskenion.core.bus import EventBus
from proskenion.core.devices import DeviceManager
from proskenion.core.dmx import artnet
from proskenion.core.dmx.desk import DeskInput
from proskenion.core.dmx.drivers import ArtnetDriver
from proskenion.core.dmx.endpoint import ArtNetEndpoint
from proskenion.core.drivers.categories import Category
from proskenion.core.lighting import LightingService
from proskenion.core.state import StateStore
from proskenion.core.transport.udp import UdpTransport
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from tests.stubs.artnet_stub import ArtNetStub
from tests.stubs.echo_driver import RecordingSink
from tests.unit.core.dmx.conftest import FakeDevices, FakeKnx, config, dmx, wait_until

NODE = "127.0.0.2"
OTHER = "127.0.0.3"
INPUT_UNIVERSE = 0


def _bindable(address: str) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.bind((address, 0))
        except OSError:
            return False
    return True


pytestmark = pytest.mark.skipif(
    not (_bindable(NODE) and _bindable(OTHER)),
    reason="this platform does not bind 127.0.0.2/127.0.0.3 on loopback",
)


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("0.0.0.0", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def artnet_port(monkeypatch: pytest.MonkeyPatch) -> int:
    """The port standing in for 6454 in this test."""
    port = _free_udp_port()
    monkeypatch.setattr(ArtNetEndpoint, "default_port", port)
    return port


@pytest.fixture
async def node(artnet_port: int) -> AsyncIterator[ArtNetStub]:
    """The eDMX8 MAX: answers every ArtPoll to the Art-Net port, not the poll's port."""
    stub = ArtNetStub(
        short_name="eDMX8 MAX",
        firmware_version=(2, 3),
        ports=(
            artnet.ArtNetPort(port_type=0x40, good_input=0x80, good_output=0, sw_in=0, sw_out=0),
            artnet.ArtNetPort(port_type=0x40, good_input=0x00, good_output=0, sw_in=1, sw_out=0),
        ),
        reply_to=("127.0.0.1", artnet_port),
    )
    await stub.start(NODE)
    try:
        yield stub
    finally:
        await stub.stop()


@pytest.fixture
async def other(artnet_port: int) -> AsyncIterator[ArtNetStub]:
    """Another Art-Net device on the VLAN — the previous node, still in service."""
    stub = ArtNetStub(short_name="eDMX4", reply_to=("127.0.0.1", artnet_port))
    await stub.start(OTHER)
    try:
        yield stub
    finally:
        await stub.stop()


def _driver(node: ArtNetStub, **config: Any) -> ArtnetDriver:
    return ArtnetDriver(
        1, UdpTransport({"host": NODE, "port": node.port}), dict(config), RecordingSink()
    )


# -- health: ArtPollReply to the Art-Net port (§7.2.8) --------------------------


async def test_a_reply_sent_to_the_art_net_port_makes_the_node_healthy(
    node: ArtNetStub, artnet_port: int
) -> None:
    driver = _driver(node)
    await driver.connect()
    try:
        result = await driver.probe()
    finally:
        await driver.disconnect()
    assert result.alive is True, result.detail
    assert node.polls == [("127.0.0.1", artnet_port)]  # the poll left from the Art-Net port
    assert driver.last_reply is not None
    assert driver.last_reply.short_name == "eDMX8 MAX"
    # GoodInput is on the health detail as a commissioning check (§7.2.8).
    assert result.detail == "eDMX8 MAX firmware 2.3; input 0 receiving, input 1 no data"


async def test_a_reply_from_another_address_does_not_make_the_node_healthy(
    node: ArtNetStub, other: ArtNetStub, artnet_port: int
) -> None:
    node.reply_to_poll = False  # the eDMX8 is down
    driver = _driver(node)
    driver.POLL_TIMEOUT = 0.5
    await driver.connect()

    async def other_node_keeps_answering() -> None:
        # The eDMX4 answering the legacy controller's polls, broadcast to 6454.
        while True:
            other.send_poll_reply(("127.0.0.1", artnet_port))
            await asyncio.sleep(0.05)

    chatter = asyncio.create_task(other_node_keeps_answering())
    try:
        result = await driver.probe()
    finally:
        chatter.cancel()
        await driver.disconnect()
    assert result.alive is False
    assert driver.last_reply is None


async def test_a_reply_the_node_sent_for_another_controller_still_counts(
    node: ArtNetStub, artnet_port: int
) -> None:
    node.reply_to_poll = False
    driver = _driver(node)
    driver.POLL_TIMEOUT = 2.0
    await driver.connect()
    try:
        probe = asyncio.create_task(driver.probe())
        await asyncio.sleep(0.05)
        node.send_poll_reply(("127.0.0.1", artnet_port))  # the legacy controller polled it
        result = await probe
    finally:
        await driver.disconnect()
    assert result.alive is True


async def test_the_controller_never_answers_an_art_poll(node: ArtNetStub, artnet_port: int) -> None:
    driver = _driver(node, input_universes="0")
    await driver.connect()
    loop = asyncio.get_running_loop()
    heard: list[bytes] = []

    class Poller(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
            heard.append(data)

    transport, _ = await loop.create_datagram_endpoint(Poller, local_addr=(OTHER, 0))
    try:
        transport.sendto(artnet.encode_art_poll(), ("127.0.0.1", artnet_port))
        await asyncio.sleep(0.2)
    finally:
        transport.close()
        await driver.disconnect()
    assert heard == []


# -- desk detection: ArtDmx from the node (§7.2.7) -------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class Room:
    manager: DeviceManager
    state: StateStore
    lighting: LightingService
    desk: DeskInput
    clock: FakeClock
    device_id: int

    @property
    def external(self) -> object:
        return self.state.lighting.get("external_control")


@pytest.fixture
async def room(
    db: Database, dev_config: Config, bus: EventBus, node: ArtNetStub
) -> AsyncIterator[Room]:
    """The device manager running the artnet driver for the node, wired to the
    desk input and a lighting service exactly as the application wires them."""
    state = StateStore(dev_config, bus)
    device = await devices_crud.create(
        db,
        category="lighting_output",
        driver_key="artnet",
        name="eDMX8",
        config={
            "transport": {"type": "udp", "host": NODE, "port": node.port},
            "driver": {"input_universes": str(INPUT_UNIVERSE)},
        },
    )
    lighting = LightingService(state, bus, None, FakeDevices(device.id), FakeKnx())
    await lighting.start(
        config(
            dmx(1, 1, device=device.id, universe=INPUT_UNIVERSE),
            dmx(2, 5, device=device.id, universe=3),
        )
    )
    clock = FakeClock()
    desk = DeskInput(state, lighting.set_external_detected, lambda: lighting.config, clock=clock)
    manager = DeviceManager(db, state, bus, dev_config, connect_timeout=3.0, stop_timeout=3.0)
    manager.set_lighting_input(desk)
    await manager.start()
    try:
        record = await manager.wait_for_connection(device.id, 3.0)
        assert record is not None and record.status == "connected", record
        yield Room(manager, state, lighting, desk, clock, device.id)
    finally:
        await manager.stop()
        await lighting.stop()


async def _emit(stub: ArtNetStub, artnet_port: int, data: bytes) -> None:
    await stub.emit_art_dmx(INPUT_UNIVERSE, data, to=("127.0.0.1", artnet_port))


async def test_art_dmx_from_the_node_is_a_desk_and_five_seconds_of_silence_clears_it(
    room: Room, node: ArtNetStub, artnet_port: int
) -> None:
    assert room.external == "off"
    await _emit(node, artnet_port, bytes(512))  # all zeros still counts (§7.2.7)
    await wait_until(lambda: room.external == "detected")

    room.clock.now += 4.9
    room.desk.check()
    assert room.external == "detected"  # leave only after 5 s of silence

    room.clock.now += 0.1
    room.desk.check()
    assert room.external == "off"


async def test_the_desks_levels_are_observed_for_patched_channels_only(
    room: Room, node: ArtNetStub, artnet_port: int
) -> None:
    frame = bytearray(512)
    frame[0] = 255  # channel 1: patched on the input universe
    frame[4] = 128  # an address patched only on universe 3, not this one
    await _emit(node, artnet_port, bytes(frame))
    await wait_until(lambda: room.external == "detected")
    room.desk.check()
    assert room.state.lighting.get("observed") == {"1": 100.0}


async def test_art_dmx_from_another_address_is_not_a_desk(
    room: Room, node: ArtNetStub, other: ArtNetStub, artnet_port: int
) -> None:
    # The previous node broadcasting its own input on the same universe.
    for _ in range(5):
        await _emit(other, artnet_port, bytes([255]) * 512)
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.1)
    room.desk.check()
    assert room.external == "off"
    assert room.state.lighting.get("observed") == {}

    await _emit(node, artnet_port, bytes(512))  # the path was live all along
    await wait_until(lambda: room.external == "detected")


async def test_the_device_status_carries_good_input_as_a_commissioning_check(room: Room) -> None:
    def detail() -> str:
        record = room.manager.status(room.device_id)
        return "" if record is None or record.detail is None else record.detail

    await wait_until(lambda: "input 0 receiving" in detail())
    assert detail() == "eDMX8 MAX firmware 2.3; input 0 receiving, input 1 no data"
    assert room.external == "off"  # GoodInput gates nothing (§7.2.7)


# -- restarts: the socket is released and rebound (§5.3) --------------------------


async def test_restarting_the_driver_twice_in_a_row_rebinds_the_art_net_port(
    room: Room, node: ArtNetStub, artnet_port: int
) -> None:
    for _ in range(2):
        await room.manager.reload(room.device_id)
        record = await room.manager.wait_for_connection(room.device_id, 3.0)
        assert record is not None and record.status == "connected", record
    [endpoint] = ArtNetEndpoint.open_endpoints().values()
    assert endpoint.local_port == artnet_port
    assert endpoint.lease_count == 1
    # And the rebuilt driver still carries the booth input.
    await _emit(node, artnet_port, bytes(512))
    await wait_until(lambda: room.external == "detected")


async def test_the_test_button_shares_the_running_socket(
    room: Room, node: ArtNetStub, artnet_port: int
) -> None:
    stored: dict[str, Any] = {
        "transport": {"type": "udp", "host": NODE, "port": node.port},
        "driver": {},
    }
    report = await room.manager.test(Category.LIGHTING_OUTPUT, "artnet", stored)
    assert report.ok, report
    [endpoint] = ArtNetEndpoint.open_endpoints().values()
    assert endpoint.lease_count == 1  # the throwaway's lease is gone; the running one stays
