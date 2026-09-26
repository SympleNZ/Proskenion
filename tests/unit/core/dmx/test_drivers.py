"""The three ``lighting_output`` drivers: registry checks, the artnet driver
against the Art-Net stub, the sacn driver's injectable ping probe, and the
unverified opendmx driver's break/MAB/frame send sequence."""

from __future__ import annotations

import asyncio
import inspect
import socket
from collections.abc import Callable

import pytest

from proskenion.core.dmx import artnet
from proskenion.core.dmx.drivers import (
    DEFAULT_PRIORITY,
    DEFAULT_SOURCE_NAME,
    ArtnetDriver,
    OpenDmxDriver,
    SacnDriver,
    parse_universes,
)
from proskenion.core.drivers import registry
from proskenion.core.drivers.base import DeviceStatus
from proskenion.core.drivers.capabilities import LightingCapabilities
from proskenion.core.drivers.categories import Category
from proskenion.core.transport.base import ConfigurationError, TransportClosed
from proskenion.core.transport.loopback import LoopbackTransport
from proskenion.core.transport.serial import SerialTransport
from proskenion.core.transport.udp import UdpTransport
from tests.stubs.artnet_stub import ArtNetStub
from tests.stubs.echo_driver import RecordingSink

_DMX_DRIVERS = (ArtnetDriver, SacnDriver, OpenDmxDriver)

# -- registry: shared checks for all three drivers -----------------------


def test_all_three_drivers_are_registered_under_lighting_output() -> None:
    assert registry.DRIVERS[(Category.LIGHTING_OUTPUT, "artnet")] is ArtnetDriver
    assert registry.DRIVERS[(Category.LIGHTING_OUTPUT, "sacn")] is SacnDriver
    assert registry.DRIVERS[(Category.LIGHTING_OUTPUT, "opendmx")] is OpenDmxDriver


def test_no_dmx_driver_schema_carries_addressing() -> None:
    for driver_cls in _DMX_DRIVERS:
        for f in driver_cls.CONFIG_SCHEMA:
            assert f.type not in ("host", "device_path"), (driver_cls.key, f.key)
            assert f.key not in ("host", "device_path"), (driver_cls.key, f.key)


def test_capabilities_is_a_method_for_every_dmx_driver() -> None:
    for driver_cls in _DMX_DRIVERS:
        assert inspect.isfunction(inspect.getattr_static(driver_cls, "capabilities"))


def test_available_lighting_output_drivers_report_declared_capabilities() -> None:
    infos = {info.key: info for info in registry.available(Category.LIGHTING_OUTPUT)}
    assert {"artnet", "sacn", "opendmx"} <= set(infos)
    for info in infos.values():
        assert isinstance(info.capabilities, LightingCapabilities)
    assert infos["artnet"].capabilities.supports_poll is True
    assert infos["artnet"].capabilities.supports_receive is True
    assert infos["sacn"].capabilities.supports_poll is False
    assert infos["opendmx"].capabilities.supports_poll is False
    assert infos["opendmx"].capabilities.universe_count == 1


async def test_registry_build_produces_a_ready_artnet_driver() -> None:
    driver = await registry.build(
        5,
        Category.LIGHTING_OUTPUT,
        "artnet",
        {"transport": {"type": "udp", "host": "10.2.30.60"}, "driver": {}},
        RecordingSink(),
    )
    assert isinstance(driver, ArtnetDriver)
    assert driver.transport.config["port"] == artnet.ARTNET_PORT


async def test_registry_build_produces_a_ready_sacn_driver_with_defaults() -> None:
    driver = await registry.build(
        6,
        Category.LIGHTING_OUTPUT,
        "sacn",
        {"transport": {"type": "udp", "host": "10.2.30.60"}, "driver": {}},
        RecordingSink(),
    )
    assert isinstance(driver, SacnDriver)
    assert driver.transport.config["port"] == artnet.SACN_PORT
    assert driver.config == {"priority": DEFAULT_PRIORITY, "source_name": DEFAULT_SOURCE_NAME}


async def test_registry_build_produces_a_ready_opendmx_driver() -> None:
    driver = await registry.build(
        7,
        Category.LIGHTING_OUTPUT,
        "opendmx",
        {
            "transport": {"type": "serial", "device_path": "/dev/serial/by-id/enttec-open-dmx"},
            "driver": {},
        },
        RecordingSink(),
    )
    assert isinstance(driver, OpenDmxDriver)
    assert driver.transport.config["baud"] == 250_000
    assert driver.transport.config["stop"] == "2"


def test_opendmx_name_says_unverified() -> None:
    assert "unverified" in OpenDmxDriver.name.lower()


def test_opendmx_module_docstring_explains_why_it_exists() -> None:
    import proskenion.core.dmx.drivers as module

    assert module.__doc__ is not None
    assert "never" in module.__doc__.lower()


# -- artnet: against the stub ---------------------------------------------


async def test_artnet_probe_succeeds_against_a_replying_stub() -> None:
    stub = ArtNetStub(short_name="eDMX8", firmware_version=(2, 1))
    await stub.start()
    try:
        transport = UdpTransport({"host": "127.0.0.1", "port": stub.port})
        driver = ArtnetDriver(1, transport, {}, RecordingSink())
        await driver.connect()
        try:
            result = await driver.probe()
        finally:
            await driver.disconnect()
        assert result.alive is True
        assert "eDMX8" in (result.detail or "")
        assert driver.last_reply is not None
        assert driver.last_reply.firmware_version == (2, 1)
    finally:
        await stub.stop()


async def test_artnet_probe_reports_device_kind_when_the_stub_does_not_reply() -> None:
    stub = ArtNetStub(reply_to_poll=False)
    await stub.start()
    try:
        transport = UdpTransport({"host": "127.0.0.1", "port": stub.port})
        driver = ArtnetDriver(1, transport, {}, RecordingSink())
        driver.POLL_TIMEOUT = 0.2
        await driver.connect()
        try:
            result = await driver.probe()
        finally:
            await driver.disconnect()
        assert result.alive is False
    finally:
        await stub.stop()


async def test_artnet_connect_reports_a_configuration_error_on_bind_conflict() -> None:
    # The connect-versus-probe split (§5.3) every driver inherits: an Art-Net
    # port another program already holds is a configuration error, and
    # probe() is never reached.
    blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    blocker.bind(("0.0.0.0", 0))
    try:
        transport = UdpTransport({"host": "127.0.0.1", "port": 1})
        driver = ArtnetDriver(
            1, transport, {}, RecordingSink(), endpoint_port=blocker.getsockname()[1]
        )
        with pytest.raises(ConfigurationError):
            await driver.connect()
    finally:
        blocker.close()


async def test_artnet_send_universe_delivers_exactly_one_frame_per_call() -> None:
    stub = ArtNetStub()
    await stub.start()
    try:
        transport = UdpTransport({"host": "127.0.0.1", "port": stub.port})
        driver = ArtnetDriver(1, transport, {}, RecordingSink())
        await driver.connect()
        try:
            data = (bytes(range(256)) * 2)[:512]
            await driver.send_universe(3, data)
            await asyncio.sleep(0.05)
            assert len(stub.received) == 1
            assert stub.received[0].universe == 3
            assert stub.received[0].data == data
            assert len(stub.received[0].data) == 512

            await driver.send_universe(3, data)
            await asyncio.sleep(0.05)
            assert len(stub.received) == 2  # exactly one more — not zero, not several
        finally:
            await driver.disconnect()
    finally:
        await stub.stop()


async def test_artnet_blackout_sends_zeros_to_every_sent_universe() -> None:
    stub = ArtNetStub()
    await stub.start()
    try:
        transport = UdpTransport({"host": "127.0.0.1", "port": stub.port})
        driver = ArtnetDriver(1, transport, {}, RecordingSink())
        await driver.connect()
        try:
            await driver.send_universe(1, bytes([255]) * 512)
            await driver.send_universe(2, bytes([128]) * 512)
            await asyncio.sleep(0.05)
            stub.received.clear()

            await driver.blackout()
            await asyncio.sleep(0.05)
            assert len(stub.received) == 2
            assert {r.universe for r in stub.received} == {1, 2}
            assert all(r.data == bytes(512) for r in stub.received)
        finally:
            await driver.disconnect()
    finally:
        await stub.stop()


async def test_artnet_sequence_counter_increments_and_never_emits_zero() -> None:
    stub = ArtNetStub()
    await stub.start()
    try:
        driver = ArtnetDriver(
            1, UdpTransport({"host": "127.0.0.1", "port": stub.port}), {}, RecordingSink()
        )
        await driver.connect()
        try:
            for _ in range(3):
                await driver.send_universe(1, bytes(512))
            await asyncio.sleep(0.05)
        finally:
            await driver.disconnect()
        assert [r.sequence for r in stub.received] == [1, 2, 3]
    finally:
        await stub.stop()


async def test_artnet_sends_nothing_before_connect() -> None:
    driver = ArtnetDriver(1, UdpTransport({"host": "127.0.0.1", "port": 1}), {}, RecordingSink())
    with pytest.raises(TransportClosed):
        await driver.send_universe(1, bytes(512))


# -- artnet: the booth input universe (§7.2.7) --------------------------------


async def test_an_existing_artnet_row_without_an_input_universe_loads_unchanged() -> None:
    driver = await registry.build(
        5,
        Category.LIGHTING_OUTPUT,
        "artnet",
        {"transport": {"type": "udp", "host": "10.2.30.245", "port": 6454}, "driver": {}},
        RecordingSink(),
    )
    assert isinstance(driver, ArtnetDriver)
    assert driver.config == {}
    assert driver.input_universes == frozenset()  # detection off, never "every universe"


async def test_the_input_universe_is_parsed_from_the_driver_config() -> None:
    driver = await registry.build(
        5,
        Category.LIGHTING_OUTPUT,
        "artnet",
        {
            "transport": {"type": "udp", "host": "10.2.30.245"},
            "driver": {"input_universes": " 0, 1 "},
        },
        RecordingSink(),
    )
    assert isinstance(driver, ArtnetDriver)
    assert driver.input_universes == frozenset({0, 1})


@pytest.mark.parametrize("value", ["abc", "0;1", "-1", "0, 40000"])
async def test_an_invalid_input_universe_is_refused_on_save(value: str) -> None:
    with pytest.raises(registry.ConfigValidationError) as caught:
        await registry.build(
            5,
            Category.LIGHTING_OUTPUT,
            "artnet",
            {
                "transport": {"type": "udp", "host": "10.2.30.245"},
                "driver": {"input_universes": value},
            },
            RecordingSink(),
        )
    assert "driver.input_universes" in caught.value.detail


def test_the_input_universe_is_one_generic_schema_field() -> None:
    [field] = ArtnetDriver.CONFIG_SCHEMA
    assert (field.key, field.type, field.required, field.default) == (
        "input_universes",
        "string",
        False,
        None,
    )


def test_an_input_universe_that_is_also_an_output_is_allowed() -> None:
    # §7.2.7 *Universe configuration*: the node's merge needs exactly that.
    assert parse_universes("2") == frozenset({2})
    assert parse_universes("") == frozenset()
    assert parse_universes(None) == frozenset()


# -- artnet: amber, then red (§7.2.8) --------------------------------------------


async def _no_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


async def test_one_missed_poll_is_reported_then_a_reply_recovers() -> None:
    stub = ArtNetStub(reply_to_poll=False)
    await stub.start()
    sink = RecordingSink()
    driver = ArtnetDriver(1, UdpTransport({"host": "127.0.0.1", "port": stub.port}), {}, sink)
    driver.POLL_TIMEOUT = 0.1
    driver._sleep = _no_sleep
    await driver.connect()
    maintain = asyncio.create_task(driver.maintain())
    try:
        await wait_for_reports(sink, 3)
        # Every miss is reported; the device manager shows the first amber
        # and the second consecutive one red. The driver stays up.
        assert sink.statuses[:3] == [
            (DeviceStatus.CONNECTED, None),  # the connecting probe's detail
            (DeviceStatus.ERROR, "device"),
            (DeviceStatus.ERROR, "device"),
        ]
        assert not maintain.done()
        stub.reply_to_poll = True
        await wait_for(lambda: sink.statuses.count((DeviceStatus.CONNECTED, None)) == 2)
    finally:
        maintain.cancel()
        await driver.disconnect()
        await stub.stop()


async def wait_for_reports(sink: RecordingSink, count: int) -> None:
    await wait_for(lambda: len(sink.reports) >= count)


async def wait_for(predicate: Callable[[], bool], within: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


# -- sacn -------------------------------------------------------------------


async def test_sacn_probe_uses_the_injected_ping_runner() -> None:
    calls: list[str] = []

    async def fake_ping(host: str) -> bool:
        calls.append(host)
        return host == "10.2.30.71"

    transport = UdpTransport({"host": "10.2.30.71", "port": artnet.SACN_PORT})
    driver = SacnDriver(1, transport, {}, RecordingSink(), ping=fake_ping)
    result = await driver.probe()
    assert result.alive is True
    assert calls == ["10.2.30.71"]


async def test_sacn_probe_reports_unreachable() -> None:
    async def fake_ping(host: str) -> bool:
        return False

    transport = UdpTransport({"host": "10.2.30.71", "port": artnet.SACN_PORT})
    driver = SacnDriver(1, transport, {}, RecordingSink(), ping=fake_ping)
    result = await driver.probe()
    assert result.alive is False
    assert "10.2.30.71" in (result.detail or "")


async def test_sacn_probe_never_shells_out_by_default_in_tests() -> None:
    # The injected runner replaces the real `ping` entirely — no subprocess spawned.
    async def fake_ping(host: str) -> bool:
        raise AssertionError("should not be reached")

    transport = UdpTransport({"host": "", "port": artnet.SACN_PORT})
    driver = SacnDriver(1, transport, {}, RecordingSink(), ping=fake_ping)
    result = await driver.probe()
    assert result.alive is False
    assert result.detail == "no host configured"


async def test_sacn_send_universe_encodes_priority_and_source_name() -> None:
    transport = LoopbackTransport()
    await transport.open()
    config = {"priority": 150, "source_name": "Proskenion booth"}
    driver = SacnDriver(1, transport, config, RecordingSink())
    data = (bytes(range(256)) * 2)[:512]
    await driver.send_universe(7, data)
    assert len(transport.sent) == 1
    packet = transport.sent[0]
    assert packet[108] == 150  # priority offset — see test_sacn.py
    name = packet[44:108].split(b"\x00", 1)[0].decode()
    assert name == "Proskenion booth"
    assert packet[-512:] == data


async def test_sacn_blackout_sends_zeros() -> None:
    transport = LoopbackTransport()
    await transport.open()
    driver = SacnDriver(1, transport, {}, RecordingSink())
    await driver.send_universe(4, bytes([9]) * 512)
    await driver.blackout()
    assert len(transport.sent) == 2
    assert transport.sent[-1][-512:] == bytes(512)


# -- opendmx: unverified, but its send sequence is testable ---------------


class _RecordingSerialTransport(SerialTransport):
    """A real ``SerialTransport`` (so ``isinstance`` in the driver holds)
    whose I/O-touching methods are replaced with recorders. This is the
    ``send_break``/``send`` boundary the driver actually relies on now —
    see :meth:`~proskenion.core.transport.serial.SerialTransport.send_break`."""

    def __init__(self) -> None:
        super().__init__({"device_path": "/dev/serial/by-id/fake-enttec", "baud": 250_000})
        self.calls: list[tuple[str, object]] = []

    async def send_break(self, duration_s: float, *, mark_after_s: float = 0.0) -> None:
        self.calls.append(("send_break", (duration_s, mark_after_s)))

    async def send(self, data: bytes) -> None:
        self.calls.append(("send", data))


async def test_opendmx_send_sequence_is_break_then_start_code_and_payload() -> None:
    transport = _RecordingSerialTransport()
    driver = OpenDmxDriver(1, transport, {}, RecordingSink())

    data = (bytes(range(256)) * 2)[:512]
    await driver.send_universe(7, data)

    assert [kind for kind, _ in transport.calls] == ["send_break", "send"]
    duration_s, mark_after_s = transport.calls[0][1]  # type: ignore[misc]
    assert duration_s >= 88e-6
    assert mark_after_s >= 8e-6
    assert duration_s == OpenDmxDriver.BREAK_SECONDS
    assert mark_after_s == OpenDmxDriver.MARK_AFTER_BREAK_SECONDS

    payload = transport.calls[1][1]
    assert isinstance(payload, bytes)
    assert payload[0] == 0x00  # DMX start code
    assert payload[1:] == data
    assert len(payload) == 513


async def test_opendmx_blackout_sends_zeros_for_every_sent_universe() -> None:
    transport = _RecordingSerialTransport()
    driver = OpenDmxDriver(1, transport, {}, RecordingSink())
    await driver.send_universe(1, bytes(range(256)) * 2)
    transport.calls.clear()

    await driver.blackout()

    assert [kind for kind, _ in transport.calls] == ["send_break", "send"]
    payload = transport.calls[1][1]
    assert payload == bytes(513)


async def test_opendmx_rejects_a_short_frame() -> None:
    driver = OpenDmxDriver(1, _RecordingSerialTransport(), {}, RecordingSink())
    with pytest.raises(ValueError, match="512"):
        await driver.send_universe(1, bytes(10))


async def test_opendmx_rejects_a_non_serial_transport() -> None:
    # SUPPORTED_TRANSPORTS declares "serial" only; a driver built directly
    # (bypassing the registry) with anything else must fail loudly rather
    # than silently skip the break it cannot issue.
    driver = OpenDmxDriver(1, LoopbackTransport(), {}, RecordingSink())
    with pytest.raises(TypeError, match="SerialTransport"):
        await driver.send_universe(1, bytes(512))


async def test_opendmx_probe_is_the_device_nodes_presence() -> None:
    transport = LoopbackTransport()
    driver = OpenDmxDriver(1, transport, {}, RecordingSink())
    result = await driver.probe()
    assert result.alive is False  # not open yet

    await transport.open()
    result = await driver.probe()
    assert result.alive is True
