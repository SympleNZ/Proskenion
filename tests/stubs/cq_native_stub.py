"""A stub CQ-20B native metering server for tests (§7.3, §22.2).

A real TCP-and-UDP server on an OS-assigned loopback port, speaking the
port-51326 protocol independently of :mod:`proskenion.core.mixer.native` —
reimplemented from ``docs/protocols/cq20b-native.md`` rather than importing
the client's own framing, so a framing bug in the client cannot hide behind a
stub that shares it (compare ``tests/stubs/pjlink_stub.py``'s module
docstring, which explains the same choice).

The two meter bodies below are sampled, not invented, from
``tools/session.log`` — the untracked 2026-09-11 bench capture
``docs/protocols/cq20b-native.md`` §9 cites. A stub built from made-up
numbers could not prove the client decodes the real record layout; these
bytes are exactly what the CQ-20B put on the wire. See
:data:`SAMPLE_INPUT_BODY` and :data:`SAMPLE_OUTPUT_BODY` for which values
they carry.

Three independent failure injections, mirroring ``pjlink_stub.py``'s:

- :attr:`CqNativeStub.max_connections` — the (n+1)-th simultaneous TCP
  connection is accepted and then closed immediately, before any reply,
  modelling the CQ's shared MixPad-slot pool (bench §9: two slots, one
  native connection each).
- :attr:`CqNativeStub.drop_after_handshake` — complete the UDP handshake,
  then close before the client-init reply arrives.
- :attr:`CqNativeStub.send_garbage` — reply to the handshake with bytes that
  match neither framing prefix, instead of a well-formed frame.

:meth:`CqNativeStub.send_udp_garbage` additionally pushes one malformed UDP
datagram to every connected peer on demand, for testing that a garbled meter
datagram does not crash the client mid-session.
"""

from __future__ import annotations

import asyncio
import struct
import time
from typing import Any

_VARIABLE_PREFIX = 0x7F

MSG_UDP_HANDSHAKE = 0
MSG_KEEPALIVE = 5
MSG_INPUT_METERS = 8
MSG_OUTPUT_METERS = 9
MSG_CLIENT_INIT_REQUEST = 12
MSG_CLIENT_INIT_RESPONSE = 13

#: Sampled from ``tools/session.log``'s first UDP type-8 line: 32 records of
#: 20 bytes (cq20b-native.md §4). Record 1 (Ip2) carries an active
#: microphone at raw ``21082`` (~-63.6 dB); ST1 R and ST2 L/R (records 17-19)
#: are active from an AUX/HDMI feed at raw ``7377``/``15825``
#: (~-117.2/-84.2 dB); every other input reads the capture's silent baseline
#: of raw ``4608`` (~-128.0 dB), except the four FX returns (records 24-31),
#: which read raw ``0`` (~-146.0 dB) — unpatched in this capture.
SAMPLE_INPUT_BODY = bytes.fromhex(
    "001200120012176c001200120d8000120000008011545a52bd53387ebd535a520d805a520000"
    "0080001200120012176c001200120d80001200000080001200120012176c001200120d800012"
    "00000080001200120012176c001200120d80001200000080001200120012176c001200120d80"
    "001200000080001200120012176c001200120d80001200000080001200120012176c00120012"
    "0d80001200000080001200120012176c001200120d80001200000080001200120012176c0012"
    "00120d80001200000080001200120012176c001200120d80001200000080001200120012176c"
    "001200120d80001200000080001200120012176c001200120d80001200000080001200120012"
    "176c001200120d80001200000080001200120012176c001200120d8000120000008000120012"
    "0012176c001200120d80001200000080001200120012176c001200120d80001200000080d11c"
    "d11cd11c176cd11cd11c0d80d11c00000080d13dd13dd13d176cd13dd13d0d80d13d00000080"
    "d13dd13dd13d176cd13dd13d0d80d13d00000080001200120012176c001200120d8000120000"
    "0080001200120012176c001200120d80001200000080001200120012176c001200120d800012"
    "00000080001200120012176c001200120d800012000000800000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
)

#: Sampled from ``tools/session.log``'s first UDP type-9 line: the eight
#: 16-byte output records (cq20b-native.md §5). Main L/R (records 6-7) are
#: active at raw ``20151``/``20190`` (~-67.3/-67.1 dB); Out 1-6 read the same
#: raw ``4608`` silent baseline as the quiet inputs above. The remaining 680
#: bytes are the "not channel records" §5 warns not to interpret, carried
#: verbatim so a client that (wrongly) tried to read past record 7 would be
#: exercised against real, not zeroed, trailing bytes.
SAMPLE_OUTPUT_BODY = bytes.fromhex(
    "001200120d800012001200120e800012001200120d800012001200120e800012001200120d80"
    "0012001200120e800012001200120d800012001200120e800012001200120d80001200120012"
    "0e800012001200120d800012001200120e800012394e394e0d80394eb74e394e0e80b74e704e"
    "704e0d80704ede4e704e0e80de4e001200120d800012001200120e800012001200120d800012"
    "001200120e800012001200120d800012001200120e800012001200120d800012001200120e80"
    "0012001200120d800012001200120e800012001200120d800012001200120e80001200120012"
    "0d800012001200120e800012001200120d800012001200120e80001200000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000012001200120012"
    "0012001200120012001200120012001200120012001200120012001200120012001200120012"
    "001200120012001200120012001200120012ffffff0000120012001200120012001200120012"
    "0012001200120012001200120012001200120012001200120012001200120012001200120012"
    "00120012001200120012ffffff00000000000000000000120012001200120000000000000000"
    "0012001200120012000000000000000000120012001200120000000000000000001200120012"
    "0012000000000000000000120012001200120000000000000000001200120012001200000000"
    "0000000000120012001200120000000000000000001200120012001200120012001278550012"
    "0012001200120012001200120012001200120012001200120012001200120012001200120012"
    "0012001200120012001200120012001200120012001200120012d13d0012d13d001200120012"
    "00120012cf4f0012cf4f"
)


#: Where a record carries its level, and each body's record stride:
#: ``uint16_le(body, record * stride + 14)`` (cq20b-native.md §4, §5).
LEVEL_OFFSET = 14
INPUT_STRIDE = 20
OUTPUT_STRIDE = 16


def _encode(msg_type: int, body: bytes = b"") -> bytes:
    return bytes((_VARIABLE_PREFIX, msg_type)) + struct.pack("<i", len(body)) + body


def _take_variable_frames(buf: bytearray) -> list[tuple[int, bytes]]:
    """Consume every complete variable-length frame from the front of
    ``buf``. The stub only has to understand what a well-behaved client
    sends — the handshake and client-init requests, and its keep-alives,
    all variable-length — so the fixed-length form is not implemented here.
    A byte matching neither the variable prefix nor a recognised frame is
    dropped and the search continues, the same resynchronisation the real
    client implements.
    """
    frames: list[tuple[int, bytes]] = []
    while buf:
        if buf[0] != _VARIABLE_PREFIX:
            del buf[0]
            continue
        if len(buf) < 6:
            break
        length = struct.unpack_from("<i", buf, 2)[0]
        if length < 0 or len(buf) < 6 + length:
            break
        frames.append((buf[1], bytes(buf[6 : 6 + length])))
        del buf[: 6 + length]
    return frames


class _KeepaliveProtocol(asyncio.DatagramProtocol):
    """Records every UDP datagram a client sends the stub — in practice,
    only its keep-alives (§2 step 8)."""

    def __init__(self, stub: CqNativeStub) -> None:
        self._stub = stub

    def datagram_received(self, data: bytes, addr: Any) -> None:
        buf = bytearray(data)
        for msg_type, _body in _take_variable_frames(buf):
            if msg_type == MSG_KEEPALIVE:
                self._stub.keepalives_received.append(time.monotonic())

    def error_received(self, exc: Exception) -> None:
        return None


class CqNativeStub:
    """``async with CqNativeStub(...) as stub:`` starts and stops it."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        max_connections: int = 1,
        meter_interval: float = 0.05,
    ) -> None:
        self._host = host
        self._requested_port = port
        #: How many simultaneous native connections the stub allows before
        #: refusing — see the module docstring.
        self.max_connections = max_connections
        self.meter_interval = meter_interval
        #: What every meter datagram carries: the sampled bodies, until a
        #: test moves a level with :meth:`set_input_level` or
        #: :meth:`set_output_level`. A desk sends the same bytes while
        #: nothing changes, so only a moved level is news to a client.
        self.input_body = bytearray(SAMPLE_INPUT_BODY)
        self.output_body = bytearray(SAMPLE_OUTPUT_BODY)

        #: Failure injection — see the module docstring.
        self.drop_after_handshake = False
        self.send_garbage = False

        #: Counters and records a test asserts against.
        self.handshakes = 0
        self.client_inits = 0
        self.keepalives_received: list[float] = []
        self.overlapping_connections = 0

        self._server: asyncio.Server | None = None
        self._tcp_clients: dict[asyncio.StreamWriter, asyncio.Task[None]] = {}
        self._active = 0
        self._udp_transport: asyncio.DatagramTransport | None = None
        #: One UDP peer address per TCP connection that has completed
        #: client-init — meters are pushed to everyone in here.
        self._peers: dict[asyncio.StreamWriter, tuple[str, int]] = {}
        self._meter_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> CqNativeStub:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    @property
    def port(self) -> int:
        assert self._server is not None and self._server.sockets
        return int(self._server.sockets[0].getsockname()[1])

    @property
    def connected_client_count(self) -> int:
        return len(self._tcp_clients)

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._udp_transport, _ = await loop.create_datagram_endpoint(
            lambda: _KeepaliveProtocol(self), local_addr=(self._host, 0)
        )
        self._server = await asyncio.start_server(
            self._handle_client, self._host, self._requested_port
        )
        self._meter_task = asyncio.create_task(self._send_meters_forever())

    async def stop(self) -> None:
        if self._meter_task is not None:
            self._meter_task.cancel()
            try:
                await self._meter_task
            except asyncio.CancelledError:
                pass
            self._meter_task = None
        if self._udp_transport is not None:
            self._udp_transport.close()
            self._udp_transport = None
        if self._server is not None:
            self._server.close()
            # Python 3.13's Server.wait_closed() also waits for every
            # accepted connection to close (see tests/stubs/knxd_stub.py).
            self._server.close_clients()
            await self._server.wait_closed()
            self._server = None
        tasks = list(self._tcp_clients.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tcp_clients.clear()
        self._peers.clear()

    # -- protocol --------------------------------------------------------

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._tcp_clients[writer] = task
        if self._active >= self.max_connections:
            # Bench §9: the CQ's two MixPad slots are shared; a connection
            # past the limit is refused, not queued.
            self.overlapping_connections += 1
            self._tcp_clients.pop(writer, None)
            writer.close()
            return
        self._active += 1
        peername = writer.get_extra_info("peername")
        peer_host = str(peername[0]) if peername else self._host
        client_udp_port: int | None = None
        buf = bytearray()
        try:
            while True:
                try:
                    chunk = await reader.read(4096)
                except (ConnectionError, OSError):
                    return
                if not chunk:
                    return
                buf.extend(chunk)
                for msg_type, body in _take_variable_frames(buf):
                    if msg_type == MSG_UDP_HANDSHAKE and len(body) >= 2:
                        self.handshakes += 1
                        client_udp_port = struct.unpack_from("<H", body, 0)[0]
                        if self.send_garbage:
                            writer.write(b"\x99\x42not-a-recognised-frame")
                            await writer.drain()
                            continue
                        writer.write(
                            _encode(MSG_UDP_HANDSHAKE, struct.pack("<H", self._udp_local_port))
                        )
                        await writer.drain()
                        if self.drop_after_handshake:
                            return
                    elif msg_type == MSG_CLIENT_INIT_REQUEST:
                        self.client_inits += 1
                        writer.write(_encode(MSG_CLIENT_INIT_RESPONSE))
                        await writer.drain()
                        if client_udp_port is not None:
                            self._peers[writer] = (peer_host, client_udp_port)
        finally:
            self._active -= 1
            self._peers.pop(writer, None)
            self._tcp_clients.pop(writer, None)
            writer.close()

    @property
    def _udp_local_port(self) -> int:
        assert self._udp_transport is not None
        sockname = self._udp_transport.get_extra_info("sockname")
        return int(sockname[1])

    async def _send_meters_forever(self) -> None:
        while True:
            await asyncio.sleep(self.meter_interval)
            if self._udp_transport is None:
                continue
            for peer in list(self._peers.values()):
                self._udp_transport.sendto(_encode(MSG_INPUT_METERS, bytes(self.input_body)), peer)
                self._udp_transport.sendto(
                    _encode(MSG_OUTPUT_METERS, bytes(self.output_body)), peer
                )

    # -- test helpers ------------------------------------------------------

    def set_input_level(self, record: int, raw: int) -> None:
        """Input record ``record`` (0-31, cq20b-native.md §4's map) reads ``raw``."""
        struct.pack_into("<H", self.input_body, record * INPUT_STRIDE + LEVEL_OFFSET, raw)

    def set_output_level(self, record: int, raw: int) -> None:
        """Output record ``record`` (0-7, cq20b-native.md §5's map) reads ``raw``."""
        struct.pack_into("<H", self.output_body, record * OUTPUT_STRIDE + LEVEL_OFFSET, raw)

    def send_udp_garbage(self) -> None:
        """Push one malformed datagram to every connected peer — proves a
        garbled meter datagram is discarded rather than crashing the
        client."""
        assert self._udp_transport is not None
        for peer in list(self._peers.values()):
            self._udp_transport.sendto(b"\x01\x02\x03not-a-frame-at-all", peer)

    async def drop_all_connections(self) -> None:
        """Close every connected TCP session immediately — a lost native
        connection, once a session was already established."""
        for writer in list(self._tcp_clients):
            writer.close()
