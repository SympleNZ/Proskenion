"""Serial enumeration (§5.5, §22.2) with an injected host — no real ports —
and :meth:`SerialTransport.send_break` against a fake pyserial handle."""

from __future__ import annotations

import asyncio

import pytest

from proskenion.core.transport.base import ConfigurationError, TransportClosed
from proskenion.core.transport.serial import (
    SERIAL_SCHEMA,
    SerialPortInfo,
    SerialTransport,
    enumerate_serial_ports,
)

FTDI = "/dev/serial/by-id/usb-FTDI_USB-RS232_Cable_FTB6SPL2-if00-port0"
PROLIFIC = "/dev/serial/by-id/usb-Prolific_Technology_Inc._USB-Serial_Controller_A1B2C3-if00-port0"


class FakeHost:
    def __init__(self, getty_on: set[str] | None = None) -> None:
        self.getty_on = getty_on or set()
        self.asked: list[str] = []

    def by_id_entries(self) -> list[tuple[str, str]]:
        return [(FTDI, "/dev/ttyUSB0"), (PROLIFIC, "/dev/ttyUSB1")]

    def tty_devices(self) -> list[str]:
        return ["/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyUSB2"]

    def port_info(self) -> list[SerialPortInfo]:
        return [
            SerialPortInfo("/dev/ttyUSB0", 0x0403, 0x6001, "FTB6SPL2", "USB-RS232 Cable"),
            SerialPortInfo("/dev/ttyUSB1", 0x067B, 0x2303, "A1B2C3", "USB-Serial Controller"),
            SerialPortInfo("/dev/ttyUSB2", 0x1A86, 0x7523, None, "USB Serial"),  # CH340, no serial
        ]

    async def held_by(self, device_path: str) -> str | None:
        self.asked.append(device_path)
        return "agetty (pid 512)" if device_path in self.getty_on else None


async def test_by_id_paths_are_preferred_and_stored() -> None:
    options = await enumerate_serial_ports(FakeHost())
    assert [o.path for o in options] == [FTDI, PROLIFIC, "/dev/ttyUSB2"]
    ftdi = options[0]
    assert ftdi.stable and not ftdi.in_use
    assert (ftdi.vendor_id, ftdi.product_id, ftdi.serial) == ("0403", "6001", "FTB6SPL2")
    assert "FTDI USB-RS232 Cable" in ftdi.label and "/dev/ttyUSB0" in ftdi.label
    assert not any(o.path.startswith("/dev/ttyUSB") for o in options if o.stable)


async def test_serial_parsed_from_by_id_name_when_metadata_lacks_it() -> None:
    class NoMetadata(FakeHost):
        def port_info(self) -> list[SerialPortInfo]:
            return []

    options = await enumerate_serial_ports(NoMetadata())
    assert options[0].serial == "FTB6SPL2"
    assert options[1].serial == "A1B2C3"
    assert options[0].vendor_id is None


async def test_port_held_by_another_configured_device_is_flagged() -> None:
    options = await enumerate_serial_ports(FakeHost(), held_paths={FTDI: "HDMI matrix"})
    assert options[0].in_use and options[0].in_use_by == "HDMI matrix"
    assert not options[1].in_use


async def test_port_held_by_a_getty_is_flagged() -> None:
    host = FakeHost(getty_on={"/dev/ttyUSB1"})
    options = await enumerate_serial_ports(host)
    assert options[1].in_use and options[1].in_use_by == "agetty (pid 512)"
    assert "/dev/ttyUSB1" in host.asked


async def test_held_path_matches_resolved_device_node_too() -> None:
    options = await enumerate_serial_ports(FakeHost(), held_paths={"/dev/ttyUSB0": "old config"})
    assert options[0].in_use_by == "old config"


async def test_cable_without_serial_falls_back_to_tty_and_is_unstable() -> None:
    options = await enumerate_serial_ports(FakeHost())
    fallback = options[2]
    assert fallback.path == "/dev/ttyUSB2"
    assert fallback.stable is False
    assert fallback.serial is None
    assert (fallback.vendor_id, fallback.product_id) == ("1a86", "7523")
    assert "no stable path" in fallback.label


async def test_transport_enumerate_delegates_with_injected_host() -> None:
    transport = SerialTransport(
        {"device_path": FTDI, "baud": 9600}, host=FakeHost(), held_paths={PROLIFIC: "Projector"}
    )
    options = await transport.enumerate()
    assert options is not None
    assert [o.in_use_by for o in options] == [None, "Projector", None]


def test_serial_schema() -> None:
    keys = [f.key for f in SERIAL_SCHEMA]
    assert keys == ["device_path", "baud", "bits", "parity", "stop", "flow"]
    assert SERIAL_SCHEMA[0].type == "device_path"


async def test_missing_device_node_is_a_configuration_error() -> None:
    transport = SerialTransport(
        {"device_path": "/dev/serial/by-id/usb-nothing-here-if00-port0", "baud": 9600}
    )
    with pytest.raises(ConfigurationError, match="cannot open"):
        await transport.open()
    assert not transport.is_open


class _FakeReader:
    """A ``StreamReader`` stand-in whose ``read`` never returns — a quiet line."""

    async def read(self, n: int) -> bytes:  # noqa: ARG002 - the transport contract's own signature
        await asyncio.sleep(10)
        return b""  # pragma: no cover - the test's own timeout always wins first


async def test_receive_timeout_raises_timeout_error_and_leaves_the_transport_open() -> None:
    """Defect 1: a read timeout is a quiet line, not a lost port — the fix for
    `except OSError` swallowing `asyncio.wait_for`'s `TimeoutError` (it is a
    subclass of `OSError`) ahead of it."""
    transport = SerialTransport({"device_path": FTDI, "baud": 9600})
    transport._reader = _FakeReader()  # type: ignore[assignment]
    _open_with_fake_handle(transport)  # is_open needs a writer too

    with pytest.raises(TimeoutError):
        await transport.receive(0.05)

    assert transport.is_open


def test_pyserial_options_mapping() -> None:
    transport = SerialTransport(
        {
            "device_path": FTDI,
            "baud": 19200,
            "bits": 7,
            "parity": "even",
            "stop": "2",
            "flow": "rtscts",
        }
    )
    options = transport._pyserial_options()
    assert options["baudrate"] == 19200 and options["bytesize"] == 7
    assert options["parity"] == "E" and options["stopbits"] == 2
    assert options["rtscts"] is True and options["xonxoff"] is False


# -- send_break (§7.2.4): a fake pyserial handle, no real port -------------


class _FakeSerialHandle:
    """Stands in for the pyserial ``Serial`` instance behind ``_writer``."""

    def __init__(self) -> None:
        self.break_condition = False


class _FakeWriterTransport:
    def __init__(self, serial_handle: _FakeSerialHandle) -> None:
        self.serial = serial_handle


class _FakeWriter:
    """Just enough of ``asyncio.StreamWriter`` for ``_pyserial_handle`` to work."""

    def __init__(self, serial_handle: _FakeSerialHandle) -> None:
        self.transport = _FakeWriterTransport(serial_handle)
        self._closing = False

    def is_closing(self) -> bool:
        return self._closing


def _open_with_fake_handle(transport: SerialTransport) -> _FakeSerialHandle:
    handle = _FakeSerialHandle()
    transport._writer = _FakeWriter(handle)  # type: ignore[assignment]
    return handle


async def test_send_break_asserts_then_clears_then_waits_the_mark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = SerialTransport({"device_path": FTDI, "baud": 250_000})
    handle = _open_with_fake_handle(transport)

    observed: list[tuple[bool, float]] = []

    async def fake_sleep(delay: float) -> None:
        observed.append((handle.break_condition, delay))

    monkeypatch.setattr("proskenion.core.transport.serial.asyncio.sleep", fake_sleep)

    await transport.send_break(100e-6, mark_after_s=12e-6)

    # The break is asserted for the first sleep and cleared before the second.
    assert observed == [(True, 100e-6), (False, 12e-6)]
    assert handle.break_condition is False


async def test_send_break_without_a_mark_skips_the_second_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = SerialTransport({"device_path": FTDI, "baud": 250_000})
    _open_with_fake_handle(transport)

    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("proskenion.core.transport.serial.asyncio.sleep", fake_sleep)

    await transport.send_break(50e-6)

    assert delays == [50e-6]


async def test_send_break_clears_the_line_even_if_the_wait_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = SerialTransport({"device_path": FTDI, "baud": 250_000})
    handle = _open_with_fake_handle(transport)

    async def raising_sleep(delay: float) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr("proskenion.core.transport.serial.asyncio.sleep", raising_sleep)

    with pytest.raises(asyncio.CancelledError):
        await transport.send_break(100e-6)
    assert handle.break_condition is False


async def test_send_break_on_an_unopened_port_is_a_transport_closed_error() -> None:
    transport = SerialTransport({"device_path": FTDI, "baud": 250_000})
    with pytest.raises(TransportClosed):
        await transport.send_break(100e-6)


async def test_send_break_on_a_closing_writer_is_a_transport_closed_error() -> None:
    transport = SerialTransport({"device_path": FTDI, "baud": 250_000})
    handle = _open_with_fake_handle(transport)
    fake_writer = transport._writer
    assert fake_writer is not None
    fake_writer._closing = True  # type: ignore[attr-defined]
    with pytest.raises(TransportClosed):
        await transport.send_break(100e-6)
    assert handle.break_condition is False  # never touched
