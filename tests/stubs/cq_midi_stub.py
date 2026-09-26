"""A stub Allen & Heath CQ-20B MIDI port for tests (§7.3, §22.2).

A real TCP server on an OS-assigned loopback port, speaking the CQ's documented
MIDI protocol as ``docs/protocols/cq20b.md`` describes it. It is written from
that document and **does not import the driver**: its parser, its address
knowledge and its copy of the fader law are its own, so a mistake in the
driver's codec cannot hide behind a stub that shares it (compare
:mod:`tests.stubs.pjlink_stub`).

What it does, each from cq20b.md:

- **Holds a 14-bit value per NRPN address** (:attr:`CqMidiStub.state`) and
  applies every absolute set (§2: ``B0 63 MB B0 62 LB B0 06 VC B0 26 VF``).
- **Echoes every change back** to the client, as the desk does (§1, PDF p.3).
- **Answers a get** (§8: ``B0 63 MB B0 62 LB B0 60 7F``). The reply format is
  "Not stated in the PDF"; this stub assumes, as the driver does, that the reply
  is the same four-message absolute value for the address queried.
- **Applies a relative step** (§9): ±1 dB on a level address, and — as the
  desk does, and as the driver must never provoke — **toggles a mute** on
  either an increment or a decrement to a mute address (§2 and §5, PDF p.8).
  Every such toggle is recorded in :attr:`CqMidiStub.mute_toggles`.
- **Recalls a scene** (§7: ``B0 00 00 C0 PG``, scene = PG + 1) by loading the
  scene's preset values after :attr:`CqMidiStub.recall_delay`, and, when
  :attr:`CqMidiStub.announce_recall` is set, sending each changed value.
- **Pushes MixPad-style changes** with :meth:`CqMidiStub.push`.
- **Refuses a second client** two ways, either model of what §7.3 flagged as
  a bench question and cq20b.md §16 has since answered on the real desk:
  by default, while one client is connected the stub stops listening, so a
  second connection attempt is refused by the operating system (it listens
  again once that client leaves); with :attr:`CqMidiStub.reset_second_connection`
  set, it keeps listening and instead accepts a second connection and resets
  it at once, exactly as the real desk does — the incumbent is never touched
  either way.
- **Resets the connection** with :meth:`CqMidiStub.reset_connection`.
- **Stops admitting anyone** with :meth:`CqMidiStub.refuse_connections` — the
  client is dropped and every later attempt is refused, as when another MIDI
  client has taken the desk's one connection or the desk has gone away — until
  :meth:`CqMidiStub.accept_connections`.
- **Records every byte received** (:attr:`CqMidiStub.received`), with arrival
  times per chunk and per parsed message.

Outbound framing can be varied to prove the driver's parser:
:attr:`CqMidiStub.running_status` sends replies and echoes with running status,
and :attr:`CqMidiStub.chunk_size` splits every write into small TCP writes.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import struct
import sys
import time
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any

# -- the stub's own reading of cq20b.md ------------------------------------------

#: Mute addresses: MSB 00; Ip1-16 LSB 00-0F, ST1/ST2/USB/BT 18/1A/1C/1E, Main LR
#: 44, Out1-6 45-4A (cq20b.md §5).
MUTE_ADDRESSES: frozenset[tuple[int, int]] = frozenset(
    [(0x00, lsb) for lsb in range(0x10)]
    + [(0x00, lsb) for lsb in (0x18, 0x1A, 0x1C, 0x1E)]
    + [(0x00, lsb) for lsb in range(0x44, 0x4B)]
)
#: Level MSBs: 40 inputs to Main LR (cq20b.md §3.1), 4F Main and outputs (§3.2).
LEVEL_MSBS = frozenset((0x40, 0x4F))
#: Pan to Main LR, MSB 50 (cq20b.md §6).
PAN_MSB = 0x50
PAN_CENTRE = 0x40 << 7  # 40 00 (cq20b.md §6)

#: The p.15 table from cq20b.md §4, as (dB, 14-bit), written out by value here
#: rather than computed, so the stub does not share the driver's arithmetic.
LAW_POINTS: tuple[tuple[int, int], ...] = (
    (-89, 192), (-85, 256), (-80, 320), (-75, 448), (-70, 512), (-65, 640),
    (-60, 768), (-55, 896), (-50, 1024), (-45, 1536), (-40, 1984), (-38, 2368),
    (-36, 2752), (-35, 2944), (-34, 3200), (-33, 3392), (-32, 3584), (-31, 3776),
    (-30, 3968), (-29, 4160), (-28, 4352), (-27, 4544), (-26, 4736), (-25, 4928),
    (-24, 5184), (-23, 5376), (-22, 5568), (-21, 5760), (-20, 5952), (-19, 6144),
    (-18, 6336), (-17, 6528), (-16, 6720), (-15, 6912), (-14, 7168), (-13, 7360),
    (-12, 7552), (-11, 7744), (-10, 7936), (-9, 8384), (-8, 8768), (-7, 9216),
    (-6, 9600), (-5, 10048), (-4, 10560), (-3, 11072), (-2, 11520), (-1, 12032),
    (0, 12544), (1, 12992), (2, 13440), (3, 13888), (4, 14336), (5, 14784),
    (6, 15040), (7, 15360), (8, 15680), (9, 16000), (10, 16320),
)  # fmt: skip


def _stub_value_to_db(value: int) -> float | None:
    if value == 0:
        return None
    if value <= LAW_POINTS[0][1]:
        return float(LAW_POINTS[0][0])
    for (d0, v0), (d1, v1) in zip(LAW_POINTS, LAW_POINTS[1:], strict=False):
        if v0 <= value <= v1:
            return d0 + (value - v0) * (d1 - d0) / (v1 - v0)
    return float(LAW_POINTS[-1][0])


def _stub_db_to_value(db: float | None) -> int:
    if db is None:
        return 0
    if db <= LAW_POINTS[0][0]:
        return LAW_POINTS[0][1]
    for (d0, v0), (d1, v1) in zip(LAW_POINTS, LAW_POINTS[1:], strict=False):
        if d0 <= db <= d1:
            return round(v0 + (db - d0) * (v1 - v0) / (d1 - d0))
    return LAW_POINTS[-1][1]


def default_value(address: tuple[int, int]) -> int:
    """What an address holds before anything sets it: pan at centre, all else 0."""
    return PAN_CENTRE if address[0] == PAN_MSB else 0


def absolute_message(address: tuple[int, int], value: int, *, running: bool = False) -> bytes:
    """The four-message absolute value (cq20b.md §2); with ``running`` the status
    byte is sent once and the other three messages use running status."""
    msb, lsb = address
    parts = [(0x63, msb), (0x62, lsb), (0x06, value >> 7), (0x26, value & 0x7F)]
    out = bytearray()
    for index, (controller, data) in enumerate(parts):
        if index == 0 or not running:
            out.append(0xB0)
        out += bytes((controller, data))
    return bytes(out)


# -- messages the stub understood ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class StubMessage:
    """One complete message the stub parsed from the client."""

    kind: str  # "set" | "get" | "increment" | "decrement" | "recall"
    address: tuple[int, int] | None
    value: int | None  # the 14-bit value of a set; the scene number of a recall
    at: float  # time.monotonic() when its last byte arrived


class _StubParser:
    """The stub's own MIDI reader: channel 1 CC and program change, running
    status accepted, NRPN address bytes latched."""

    def __init__(self) -> None:
        self.status: int | None = None
        self.data: list[int] = []
        self.msb: int | None = None
        self.lsb: int | None = None
        self.coarse: int | None = None
        self.bank: int | None = None

    def feed(self, chunk: bytes, at: float) -> list[StubMessage]:
        out: list[StubMessage] = []
        for byte in chunk:
            if byte >= 0xF8:
                continue
            if byte >= 0x80:
                self.status, self.data = byte, []
                continue
            if self.status is None:
                continue
            self.data.append(byte)
            if self.status == 0xC0:
                out.append(StubMessage("recall", None, self.data[0] + 1, at))
                self.data = []
            elif self.status == 0xB0 and len(self.data) == 2:
                message = self._cc(self.data[0], self.data[1], at)
                self.data = []
                if message is not None:
                    out.append(message)
        return out

    def _cc(self, controller: int, value: int, at: float) -> StubMessage | None:
        address = (self.msb, self.lsb) if self.msb is not None and self.lsb is not None else None
        if controller == 0x63:
            self.msb, self.coarse = value, None
        elif controller == 0x62:
            self.lsb, self.coarse = value, None
        elif controller == 0x06:
            self.coarse = value
        elif controller == 0x26 and self.coarse is not None and address is not None:
            coarse, self.coarse = self.coarse, None
            return StubMessage("set", address, (coarse << 7) | value, at)
        elif controller == 0x60 and address is not None:
            return StubMessage("get" if value == 0x7F else "increment", address, None, at)
        elif controller == 0x61 and address is not None:
            return StubMessage("decrement", address, None, at)
        elif controller == 0x00:
            self.bank = value
        return None


@dataclass
class _Client:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    task: asyncio.Task[Any] | None = None
    parser: _StubParser = field(default_factory=_StubParser)


class CqMidiStub:
    """``async with CqMidiStub() as stub:`` starts and stops it."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        state: dict[tuple[int, int], int] | None = None,
        presets: dict[int, dict[tuple[int, int], int]] | None = None,
        recall_delay: float = 0.1,
        announce_recall: bool = True,
        answer_gets: bool = True,
        echo: bool = True,
    ) -> None:
        self._host = host
        self._port = port
        self.state: dict[tuple[int, int], int] = dict(state or {})
        #: Scene number (1-128) to the values it loads.
        self.presets: dict[int, dict[tuple[int, int], int]] = dict(presets or {})
        self.recall_delay = recall_delay
        self.announce_recall = announce_recall
        self.answer_gets = answer_gets
        self.echo = echo
        #: Send echoes and replies with running status.
        self.running_status = False
        #: Split every outbound write into writes of at most this many bytes.
        self.chunk_size: int | None = None
        #: What the real desk does with a second client, bench-observed
        #: 21 September 2026 (cq20b.md §1, §16): it is accepted at TCP level
        #: and reset immediately, and the incumbent is untouched. False by
        #: default, matching the OS-level refusal `_on_connect` otherwise
        #: models (below) — set this to switch a stub to the bench-accurate
        #: behaviour for a test that needs it.
        self.reset_second_connection = False

        #: Every byte received, from every client, in order.
        self.received = bytearray()
        self.chunks: list[tuple[float, bytes]] = []
        self.messages: list[StubMessage] = []
        #: Each mute address that received an increment or decrement.
        self.mute_toggles: list[tuple[int, int]] = []
        #: ``(time, scene)`` when each recall's preset was applied.
        self.recalls_applied: list[tuple[float, int]] = []
        self.connections = 0

        self._server: asyncio.Server | None = None
        self._client: _Client | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopping = False
        #: While set, the stub does not listen: see :meth:`refuse_connections`.
        self._refusing = False
        self.client_connected = asyncio.Event()
        self.client_gone = asyncio.Event()
        self.activity = asyncio.Event()

    # -- lifecycle -----------------------------------------------------------

    @property
    def port(self) -> int:
        return self._port

    async def start(self) -> None:
        await self._listen()

    async def _listen(self) -> None:
        self._server = await asyncio.start_server(self._on_connect, self._host, self._port)
        self._port = self._server.sockets[0].getsockname()[1]

    def _stop_listening(self) -> None:
        # close() stops accepting at once; wait_closed() would wait for the
        # connected client, which is exactly the one being kept.
        if self._server is not None:
            self._server.close()
            self._server = None

    async def stop(self) -> None:
        self._stopping = True
        self._stop_listening()
        if self._client is not None:
            self._client.writer.close()
            if self._client.task is not None:
                self._client.task.cancel()
                await asyncio.gather(self._client.task, return_exceptions=True)
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def __aenter__(self) -> CqMidiStub:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.stop()

    @property
    def listening(self) -> bool:
        return self._server is not None

    @property
    def connected(self) -> bool:
        return self._client is not None

    # -- connections -----------------------------------------------------------

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._client is not None:
            if self.reset_second_connection:
                # The real desk accepts a second MIDI/TCP connection and
                # resets it at once, leaving the incumbent connection
                # untouched (cq20b.md §1, §16; bench 21 September 2026) —
                # never assigned as self._client, so the incumbent is
                # genuinely unaffected.
                self._reset_writer(writer)
                return
            # A connection that reached the backlog before listening stopped.
            writer.transport.abort()
            return
        self.connections += 1
        client = _Client(reader, writer)
        self._client = client
        if not self.reset_second_connection:
            # One MIDI client at a time (cq20b.md §1): a second attempt is
            # refused at the OS level, by not listening for it. Bench
            # question 6 (§16) found the real desk behaves differently —
            # accept then reset, modelled above when reset_second_connection
            # is set — but this OS-level refusal is still a real failure
            # mode `CQ20BDriver.connect` must also handle on its own.
            self._stop_listening()
        self.client_gone.clear()
        self.client_connected.set()
        client.task = asyncio.current_task()
        try:
            await self._serve(client)
        finally:
            if self._client is client:
                self._client = None
                self.client_connected.clear()
                self.client_gone.set()
                with contextlib.suppress(Exception):
                    writer.close()
                if (
                    not self._stopping
                    and not self._refusing
                    and not self.reset_second_connection
                ):
                    with contextlib.suppress(OSError, RuntimeError):
                        await self._listen()

    async def _serve(self, client: _Client) -> None:
        while True:
            try:
                chunk = await client.reader.read(4096)
            except (ConnectionError, OSError):
                return
            if not chunk:
                return
            now = time.monotonic()
            self.received += chunk
            self.chunks.append((now, chunk))
            for message in client.parser.feed(chunk, now):
                self.messages.append(message)
                await self._handle(message)
            self.activity.set()

    @staticmethod
    def _reset_writer(writer: asyncio.StreamWriter) -> None:
        """Abort ``writer``'s connection with an RST rather than a clean FIN —
        what both a rebooting desk (:meth:`reset_connection`) and a desk
        refusing a second client (``reset_second_connection``) do."""
        sock = writer.get_extra_info("socket")
        if sock is not None:
            # l_onoff 1, l_linger 0: close with RST rather than FIN.
            linger = struct.pack("HH" if sys.platform == "win32" else "ii", 1, 0)
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
        writer.transport.abort()

    async def reset_connection(self) -> None:
        """Drop the client with a TCP reset, as a desk that reboots would."""
        client = self._client
        if client is None:
            return
        self._reset_writer(client.writer)
        await self.client_gone.wait()

    async def refuse_connections(self) -> None:
        """Drop the client and refuse every connection attempt from now on.

        From the application's side this is the operating system refusing the
        connection, which is how the stub models the desk's one MIDI client
        being someone else's (cq20b.md §1). Nothing listens, so an attempt is
        refused however quickly it comes: there is no window in which the
        application's own reconnect could win.
        """
        self._refusing = True
        self._stop_listening()
        if self._client is not None:
            await self.reset_connection()

    async def accept_connections(self) -> None:
        """Listen again, on the same port, after :meth:`refuse_connections`."""
        self._refusing = False
        if self._server is None and self._client is None:
            await self._listen()

    # -- the protocol -------------------------------------------------------------

    def value(self, address: tuple[int, int]) -> int:
        return self.state.get(address, default_value(address))

    async def _handle(self, message: StubMessage) -> None:
        address = message.address
        if message.kind == "set":
            assert address is not None and message.value is not None
            self.state[address] = message.value
            if self.echo:
                await self._send(
                    absolute_message(address, message.value, running=self.running_status)
                )
        elif message.kind == "get":
            assert address is not None
            if self.answer_gets:
                await self._send(
                    absolute_message(address, self.value(address), running=self.running_status)
                )
        elif message.kind in ("increment", "decrement"):
            assert address is not None
            await self._step(address, up=message.kind == "increment")
        elif message.kind == "recall":
            assert message.value is not None
            task = asyncio.create_task(self._recall(message.value))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _step(self, address: tuple[int, int], *, up: bool) -> None:
        if address in MUTE_ADDRESSES:
            # The desk toggles a mute on either message (cq20b.md §2, PDF p.8).
            self.mute_toggles.append(address)
            self.state[address] = 0 if self.value(address) else 1
        elif address[0] in LEVEL_MSBS:
            db = _stub_value_to_db(self.value(address))
            db = -89.0 if db is None and up else db
            if db is not None:
                db = min(10.0, db + 1) if up else db - 1
                self.state[address] = _stub_db_to_value(db) if db >= -89 else 0
        else:
            return
        await self._send(
            absolute_message(address, self.value(address), running=self.running_status)
        )

    async def _recall(self, scene: int) -> None:
        await asyncio.sleep(self.recall_delay)
        preset = self.presets.get(scene, {})
        changed = {a: v for a, v in preset.items() if self.value(a) != v}
        self.state.update(preset)
        self.recalls_applied.append((time.monotonic(), scene))
        if self.announce_recall:
            for address, value in changed.items():
                await self._send(absolute_message(address, value, running=self.running_status))

    async def push(self, address: tuple[int, int], value: int) -> None:
        """A change made in MixPad: the desk's state changes and it tells the
        MIDI client (cq20b.md §1, PDF p.3)."""
        self.state[address] = value
        await self._send(absolute_message(address, value, running=self.running_status))

    async def send_raw(self, data: bytes) -> None:
        """Arbitrary bytes to the client, for parser tests."""
        await self._send(data)

    async def _send(self, data: bytes) -> None:
        client = self._client
        if client is None:
            return
        size = self.chunk_size or len(data)
        try:
            for start in range(0, len(data), size):
                client.writer.write(data[start : start + size])
                await client.writer.drain()
        except (ConnectionError, OSError):
            return

    # -- inspection --------------------------------------------------------------

    def messages_of(self, kind: str) -> list[StubMessage]:
        return [m for m in self.messages if m.kind == kind]

    def clear_record(self) -> None:
        self.received.clear()
        self.chunks.clear()
        self.messages.clear()
