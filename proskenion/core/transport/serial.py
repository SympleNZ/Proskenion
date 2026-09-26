"""Serial transport — LKV422, RS-232 projectors (§5.5).

Serial is the only transport that can enumerate, and it enumerates the host,
not a device, so there is no chicken-and-egg. Ports are enumerated from
``/dev/serial/by-id/`` and that path is what gets stored; ``/dev/ttyUSB*`` is
offered only as a fallback for a cable with no serial number, flagged as
unstable because its numbering moves on replug (§4.12, B46).

Host access is behind :class:`SerialHost` so enumeration is testable with an
injected listing and metadata and never needs a real port.
"""

from __future__ import annotations

import asyncio
import glob
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Protocol

from proskenion.core.drivers.fields import Field
from proskenion.core.transport.base import (
    BaseTransport,
    ConfigurationError,
    PortOption,
    TransportClosed,
)

BY_ID_DIR = "/dev/serial/by-id"
_TTY_GLOBS = ("/dev/ttyUSB*", "/dev/ttyACM*")

SERIAL_SCHEMA: list[Field] = [
    Field(
        "device_path",
        type="device_path",
        label="Serial port",
        required=True,
        help="Stored as the /dev/serial/by-id path so it survives replugging",
    ),
    Field("baud", type="int", label="Baud rate", required=True, min=300, max=4_000_000),
    Field("bits", type="int", label="Data bits", default=8, min=5, max=8),
    Field(
        "parity",
        type="enum",
        label="Parity",
        default="none",
        options=[("none", "None"), ("even", "Even"), ("odd", "Odd")],
    ),
    Field("stop", type="enum", label="Stop bits", default="1", options=[("1", "1"), ("2", "2")]),
    Field(
        "flow",
        type="enum",
        label="Flow control",
        default="none",
        options=[("none", "None"), ("rtscts", "RTS/CTS"), ("xonxoff", "XON/XOFF")],
    ),
]


@dataclass(frozen=True)
class SerialPortInfo:
    """Per-port metadata, as ``serial.tools.list_ports`` reports it."""

    device: str  # e.g. "/dev/ttyUSB0"
    vendor_id: int | None = None
    product_id: int | None = None
    serial_number: str | None = None
    description: str | None = None


class SerialHost(Protocol):
    """What enumeration needs from the host — injectable for tests."""

    def by_id_entries(self) -> list[tuple[str, str]]:
        """``(by-id path, resolved device path)`` for each entry in ``BY_ID_DIR``."""
        ...

    def tty_devices(self) -> list[str]:
        """Every ``/dev/ttyUSB*`` and ``/dev/ttyACM*`` node present."""
        ...

    def port_info(self) -> list[SerialPortInfo]:
        """Metadata from pyserial's port listing."""
        ...

    async def held_by(self, device_path: str) -> str | None:
        """Name of the process holding the node open — a getty, say — or None."""
        ...


class SystemSerialHost:
    """The real host. Only the enumeration code talks to it."""

    def by_id_entries(self) -> list[tuple[str, str]]:
        base = Path(BY_ID_DIR)
        if not base.is_dir():
            return []
        return sorted((str(entry), os.path.realpath(entry)) for entry in base.iterdir())

    def tty_devices(self) -> list[str]:
        found: list[str] = []
        for pattern in _TTY_GLOBS:
            found.extend(glob.glob(pattern))
        return sorted(found)

    def port_info(self) -> list[SerialPortInfo]:
        from serial.tools import list_ports

        infos: list[SerialPortInfo] = []
        for port in list_ports.comports():
            infos.append(
                SerialPortInfo(
                    device=str(port.device),
                    vendor_id=port.vid if isinstance(port.vid, int) else None,
                    product_id=port.pid if isinstance(port.pid, int) else None,
                    serial_number=port.serial_number if port.serial_number else None,
                    description=port.description if port.description else None,
                )
            )
        return infos

    async def held_by(self, device_path: str) -> str | None:
        """Ask ``fuser`` who holds the node; None when nobody does or the tool
        is missing."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "fuser",
                device_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), 5.0)
        except (OSError, TimeoutError):
            return None
        pids = stdout.decode(errors="replace").split()
        if not pids:
            return None
        pid = pids[0].rstrip("cefFrm")
        try:
            proc = await asyncio.create_subprocess_exec(
                "ps",
                "-o",
                "comm=",
                "-p",
                pid,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), 5.0)
        except (OSError, TimeoutError):
            return f"pid {pid}"
        name = stdout.decode(errors="replace").strip()
        return f"{name} (pid {pid})" if name else f"pid {pid}"


_BY_ID_NAME = re.compile(
    r"^(?P<bus>[a-z]+)-(?P<body>.+?)(?:-if(?P<iface>\d+))?(?:-port(?P<port>\d+))?$"
)


def _parse_by_id_name(name: str) -> tuple[str, str | None]:
    """Split a by-id file name into ``(descriptive text, serial)``.

    udev builds the name as ``usb-<vendor>_<product>_<serial>-if00-port0`` with
    spaces replaced by underscores, so the serial is the last underscore-separated
    token of the body and the rest is the vendor and product text.
    """
    match = _BY_ID_NAME.match(name)
    body = match.group("body") if match else name
    if "_" not in body:
        return body, None
    text, serial = body.rsplit("_", 1)
    return text.replace("_", " "), serial or None


def _hex4(value: int | None) -> str | None:
    return f"{value:04x}" if value is not None else None


async def enumerate_serial_ports(
    host: SerialHost | None = None,
    held_paths: Mapping[str, str] | None = None,
) -> list[PortOption]:
    """Enumerate serial ports for the picker (§5.5 *Serial port enumeration*).

    ``held_paths`` maps a stored path (by-id or ``ttyUSB``) to a description of
    the configured device that holds it, so a port already assigned elsewhere
    is flagged before first connect rather than at it.
    """
    host = host or SystemSerialHost()
    held = dict(held_paths or {})
    info_by_device = {info.device: info for info in host.port_info()}

    def holder_of(paths: list[str]) -> str | None:
        for path in paths:
            if path in held:
                return held[path]
        return None

    options: list[PortOption] = []
    covered: set[str] = set()

    for by_id_path, device in host.by_id_entries():
        covered.add(device)
        text, serial_from_name = _parse_by_id_name(os.path.basename(by_id_path))
        info = info_by_device.get(device)
        serial = (info.serial_number if info and info.serial_number else None) or serial_from_name
        holder = holder_of([by_id_path, device]) or await host.held_by(device)
        label = f"{text} ({serial})" if serial else text
        options.append(
            PortOption(
                path=by_id_path,
                label=f"{label} — {device}",
                vendor_id=_hex4(info.vendor_id) if info else None,
                product_id=_hex4(info.product_id) if info else None,
                serial=serial,
                in_use=holder is not None,
                stable=True,
                in_use_by=holder,
            )
        )

    for device in host.tty_devices():
        if device in covered:
            continue
        info = info_by_device.get(device)
        text = (info.description if info and info.description else None) or os.path.basename(device)
        holder = holder_of([device]) or await host.held_by(device)
        options.append(
            PortOption(
                path=device,
                label=f"{text} — {device} (no stable path: this cable has no serial number)",
                vendor_id=_hex4(info.vendor_id) if info else None,
                product_id=_hex4(info.product_id) if info else None,
                serial=None,
                in_use=holder is not None,
                stable=False,
                in_use_by=holder,
            )
        )

    return options


class SerialTransport(BaseTransport):
    TYPE: ClassVar[str] = "serial"
    SCHEMA: ClassVar[list[Field]] = SERIAL_SCHEMA

    READ_SIZE: ClassVar[int] = 4096

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        host: SerialHost | None = None,
        held_paths: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(config)
        self._host = host
        self._held_paths = held_paths
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    @property
    def is_open(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    def _pyserial_options(self) -> dict[str, Any]:
        import serial

        parity = {"none": serial.PARITY_NONE, "even": serial.PARITY_EVEN, "odd": serial.PARITY_ODD}
        stop = {"1": serial.STOPBITS_ONE, "2": serial.STOPBITS_TWO}
        flow = str(self.config.get("flow", "none"))
        return {
            "baudrate": int(self.config["baud"]),
            "bytesize": int(self.config.get("bits", 8)),
            "parity": parity[str(self.config.get("parity", "none"))],
            "stopbits": stop[str(self.config.get("stop", "1"))],
            "rtscts": flow == "rtscts",
            "xonxoff": flow == "xonxoff",
        }

    async def open(self) -> None:
        if self.is_open:
            return
        import serial
        import serial_asyncio

        path = str(self.config["device_path"])
        try:
            self._reader, self._writer = await serial_asyncio.open_serial_connection(
                url=path, **self._pyserial_options()
            )
        except (serial.SerialException, OSError, ValueError) as exc:
            # Missing node, permission denied, busy, unsupported settings:
            # all things configuration or the host explains, not the device.
            raise ConfigurationError(f"cannot open {path}: {exc}") from exc

    async def close(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is None:
            return
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass

    async def send(self, data: bytes) -> None:
        if self._writer is None or self._writer.is_closing():
            raise TransportClosed("serial port is not open")
        try:
            self._writer.write(data)
            await self._writer.drain()
        except OSError as exc:
            raise TransportClosed(f"serial port lost: {exc}") from exc

    async def receive(self, timeout: float) -> bytes:
        if self._reader is None:
            raise TransportClosed("serial port is not open")
        try:
            data = await asyncio.wait_for(self._reader.read(self.READ_SIZE), timeout)
        except TimeoutError:
            # A quiet line, not a lost port: asyncio.wait_for's TimeoutError
            # is a subclass of OSError, so it must be let through here, ahead
            # of the OSError clause below, or every timed-out read is
            # reported as a dropped port (see the transport contract).
            raise
        except OSError as exc:
            raise TransportClosed(f"serial port lost: {exc}") from exc
        if not data:
            raise TransportClosed("serial port closed")
        return data

    async def enumerate(self) -> list[PortOption] | None:
        return await enumerate_serial_ports(self._host, self._held_paths)

    async def send_break(self, duration_s: float, *, mark_after_s: float = 0.0) -> None:
        """Assert the line break condition for ``duration_s``, clear it, then
        wait ``mark_after_s`` (the mark-after-break) before returning.

        Break is a line condition — the UART held low past a stop bit — not a
        byte on the wire, so it cannot travel through ``send(bytes)``; nothing
        in that contract can express holding the line rather than writing
        octets. DMX512's start-of-frame break is the one shipped use of this
        (the opendmx driver, §7.2.4), which is why it lives here rather than
        behind a private attribute some driver reaches for: the transport may
        know its own internals, so it is the transport's job to publish the
        one operation that needs them (§5.5 *No exceptions*).
        """
        handle = self._pyserial_handle()
        handle.break_condition = True
        try:
            await asyncio.sleep(duration_s)
        finally:
            handle.break_condition = False
        if mark_after_s > 0:
            await asyncio.sleep(mark_after_s)

    def _pyserial_handle(self) -> Any:
        """The pyserial ``Serial`` instance behind the open connection.

        ``serial_asyncio`` ships no type stubs (see the mypy override in
        pyproject.toml) and its transport's ``.serial`` attribute is not part
        of :class:`asyncio.WriteTransport`'s own shape, hence the ignore.
        """
        if self._writer is None or self._writer.is_closing():
            raise TransportClosed("serial port is not open")
        return self._writer.transport.serial  # type: ignore[attr-defined]
