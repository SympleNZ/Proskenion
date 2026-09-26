"""A stub knxd for tests (§22.2 names this file).

A TCP server speaking knxd's local client protocol — see the source cited in
:mod:`proskenion.core.knx`'s module docstring — just enough to exercise
:class:`~proskenion.core.knx.KnxSubsystem` without a real knxd, gateway or
bus: it answers ``EIB_OPEN_GROUPCON``, records every ``EIB_GROUP_PACKET`` a
client sends (with the wall-clock time each finished arriving, so a test can
measure the §7.1 telegram budget), and can inject a group telegram to every
connected client on demand.

The wire framing is re-implemented here from the same protocol description
rather than imported from :mod:`proskenion.core.knx`, deliberately — a stub
that reused the module under test's own framing code could not tell a
framing bug in that module from a passing test.

TCP only, not the Unix socket knxd also offers: tests run on Windows, which
has no Unix socket to point a client at (see :mod:`proskenion.core.knx`).
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field

from proskenion.core import knx_dpt

EIB_OPEN_GROUPCON = 0x0026
EIB_GROUP_PACKET = 0x0027

APCI_READ = 0x00
APCI_RESPONSE = 0x40
APCI_WRITE = 0x80

_GROUP_ADDRESS_RE = re.compile(r"^(\d{1,2})/(\d)/(\d{1,3})$")
_INDIVIDUAL_ADDRESS_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{1,3})$")


def _pack_group(address: str) -> int:
    match = _GROUP_ADDRESS_RE.match(address)
    assert match is not None, f"not a group address: {address!r}"
    main, middle, sub = (int(part) for part in match.groups())
    return (main << 11) | (middle << 8) | sub


def _unpack_group(packed: int) -> str:
    return f"{(packed >> 11) & 0x1F}/{(packed >> 8) & 0x07}/{packed & 0xFF}"


def _pack_individual(address: str) -> int:
    match = _INDIVIDUAL_ADDRESS_RE.match(address)
    assert match is not None, f"not an individual address: {address!r}"
    area, line, device = (int(part) for part in match.groups())
    return (area << 12) | (line << 8) | device


def _encode_frame(type_: int, body: bytes) -> bytes:
    payload = type_.to_bytes(2, "big") + body
    return len(payload).to_bytes(2, "big") + payload


async def _read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    header = await reader.readexactly(2)
    length = int.from_bytes(header, "big")
    payload = await reader.readexactly(length)
    return int.from_bytes(payload[0:2], "big"), payload[2:]


@dataclass(frozen=True, slots=True)
class RecordedWrite:
    """One ``EIB_GROUP_PACKET`` a client sent, as the stub saw it."""

    group_address: str
    apci: int
    apdu: bytes
    received_at: float  # time.monotonic(), for measuring the §7.1 budget

    def value(self, dpt: str) -> object:
        """What the write carried, decoded as ``dpt`` — the counterpart of
        :meth:`KnxdStub.send_telegram`'s encoding, for asserting on values."""
        codec = knx_dpt.resolve(dpt)
        assert codec is not None, f"unsupported dpt in test: {dpt}"
        if codec.short_form:
            return codec.decode(bytes([self.apdu[1] & knx_dpt.SHORT_FORM_MASK]))
        return codec.decode(self.apdu[2:])


@dataclass
class _Client:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    task: asyncio.Task[None] = field(repr=False)


class KnxdStub:
    """A stub knxd. ``async with KnxdStub() as stub:`` starts and stops it."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._host = host
        self._requested_port = port
        self._server: asyncio.Server | None = None
        self._clients: dict[asyncio.StreamWriter, _Client] = {}
        self.writes: list[RecordedWrite] = []
        #: Set to False to make EIB_OPEN_GROUPCON go unanswered (probe timeout tests).
        self.answer_open: bool = True

    async def __aenter__(self) -> KnxdStub:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    @property
    def port(self) -> int:
        assert self._server is not None and self._server.sockets
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, self._host, self._requested_port
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            # Python 3.13 changed Server.wait_closed() to also wait for every
            # accepted connection to close, not only the listening socket, so
            # every open client connection must actually be torn down first,
            # not merely have its handler task cancelled.
            self._server.close_clients()
            await self._server.wait_closed()
            self._server = None
        tasks = [client.task for client in self._clients.values()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._clients.clear()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._clients[writer] = _Client(reader, writer, task)
        try:
            while True:
                try:
                    type_, body = await _read_frame(reader)
                except (asyncio.IncompleteReadError, ConnectionError):
                    return
                if type_ == EIB_OPEN_GROUPCON:
                    if self.answer_open:
                        writer.write(_encode_frame(EIB_OPEN_GROUPCON, b"\x00\x00"))
                        await writer.drain()
                elif type_ == EIB_GROUP_PACKET:
                    self._record_write(body)
                # anything else: a real knxd would handle it; the stub ignores it.
        finally:
            self._clients.pop(writer, None)
            writer.close()

    def _record_write(self, body: bytes) -> None:
        dst = int.from_bytes(body[0:2], "big")
        apdu = bytes(body[2:])
        self.writes.append(
            RecordedWrite(_unpack_group(dst), apdu[1] & 0xC0, apdu, time.monotonic())
        )

    async def emit(self, source_address: str, group_address: str, apdu: bytes) -> None:
        """Inject an incoming group telegram to every connected client."""
        body = (
            _pack_individual(source_address).to_bytes(2, "big")
            + _pack_group(group_address).to_bytes(2, "big")
            + apdu
        )
        frame = _encode_frame(EIB_GROUP_PACKET, body)
        for client in list(self._clients.values()):
            client.writer.write(frame)
            await client.writer.drain()

    async def send_telegram(
        self,
        group_address: str,
        dpt: str,
        value: object,
        *,
        source_address: str = "1.1.1",
        apci: int = APCI_WRITE,
    ) -> None:
        """Encode ``value`` as ``dpt`` and inject it as a group telegram — the
        test-author-friendly counterpart to :meth:`emit`."""
        codec = knx_dpt.resolve(dpt)
        assert codec is not None, f"unsupported dpt in test: {dpt}"
        raw = codec.encode(value)
        if codec.short_form:
            apdu = bytes([0x00, apci | (raw[0] & knx_dpt.SHORT_FORM_MASK)])
        else:
            apdu = bytes([0x00, apci]) + raw
        await self.emit(source_address, group_address, apdu)

    def connected_client_count(self) -> int:
        return len(self._clients)
