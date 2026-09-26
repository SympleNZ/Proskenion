"""The KNX subsystem: a knxd local-client connection (spec §7.1).

KNX is a subsystem, not a driver category (§5.5, Appendix B42)
------------------------------------------------------------------
Nothing here subclasses ``proskenion.core.drivers.base.Driver``, nothing
registers with the driver registry, and there is no KNX
``proskenion.core.drivers.categories.Category``. Group addresses are already
an abstract addressing model and DPTs are already a datatype system — a
generic building-control interface over KNX would be a rename, not a
generalisation (§5.5). This module must never import anything from
``proskenion.core.drivers``, and nothing under that package may import this
module; ``tests/unit/core/test_knx.py`` asserts both directions by scanning
the source.

Architecture (§7.1)
--------------------
``Application <-> knxd socket <-> knxd <-> KNXnet/IP <-> KNX gateway <-> KNX bus``

knxd owns the KNXnet/IP tunnel, keep-alive and gateway reconnection; this
module speaks only knxd's local client protocol over its Unix socket
(``/run/knx`` by default) or its TCP port (6720) — the TCP path exists
because development happens on Windows, which has no Unix socket to point
at, and because it lets :mod:`tests.stubs.knxd_stub` stand in for knxd in
tests. The application never handles KNXnet/IP directly.

Protocol source
----------------
Implemented from knxd's own client protocol, documented as ``eibclient.h`` /
``EIBConnection`` in the knxd repository (https://github.com/knxd/knxd):

* The ``EIB_*`` packet type codes and the wire framing (a 2-byte big-endian
  length prefix, then a 2-byte big-endian type code, then the body) come
  from ``src/include/eibtypes.h`` and ``src/client/c/io.c``.
* The group-socket exchange — ``EIB_OPEN_GROUPCON`` (0x0026) to open a group
  connection, and ``EIB_GROUP_PACKET`` (0x0027) to send or receive a group
  telegram, with an incoming packet's body laid out as
  ``source(2) + destination(2) + APDU(...)`` — comes from the code-generator
  templates that knxd builds its own per-language clients from:
  ``src/client/def/opengroupsocket.inc``, ``sendgroup.inc`` and
  ``getgroupsrc.inc``.
* The exact byte layout of the ``EIB_OPEN_GROUPCON`` request body (3 bytes:
  reserved, a write-only flag, reserved) and of a Group Value APDU's
  TPCI/APCI framing was cross-checked against ``knxdclient``
  (https://github.com/mhthies/knxdclient), an independent, actively
  maintained Python implementation of the same protocol, because the
  generator templates above are themselves a domain-specific language for
  knxd's build, not directly-readable wire code. Where the two agreed, it is
  implemented as read. They appeared to disagree on one point — the byte
  position of the write-only flag within ``EIB_OPEN_GROUPCON``'s 3-byte
  body — and ``knxdclient`` is followed there. It cannot matter today: this
  subsystem always opens read-write, so the body is all zeros.

Group addresses are the three-level string form (``"1/0/1"``); see
:func:`parse_group_address` and :func:`format_group_address` for the packed
16-bit encoding. DPT codecs live in :mod:`proskenion.core.knx_dpt` — adding a
supported type means adding a codec there, per §7.1.

What is deliberately not here
------------------------------
* **Debounce** belongs to the rule layer (§8.4): this module reports every
  telegram it decodes, once, with no opinion about repeats.
* **Scene and rule matching** are not this module's business either — it
  knows nothing about lighting, banks or scenes. Interested code subscribes
  to :class:`~proskenion.core.events.KnxTelegramReceived`.
* **Wiring into the application lifespan** — constructing this class,
  starting its :meth:`KnxSubsystem.run`, and the boot-time "wait up to 30 s,
  then carry on regardless" behaviour of §12.1 — is deliberately left to the
  task that owns the lifespan. This module only guarantees that
  :meth:`run` never blocks its caller: it is meant to be scheduled as its
  own task, the same as every other supervised client (§5.3).
* **The group address library** (which DPT and direction a group address
  has) is injected as an :class:`AddressRegistry`, not read from the
  database here — the schema for it is other work happening in parallel.
  :class:`InMemoryAddressRegistry` is what tests, and any caller without a
  database-backed registry yet, use.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Any, Literal, Protocol

from proskenion.config import KnxSection
from proskenion.core import knx_dpt
from proskenion.core.bus import EventBus
from proskenion.core.events import DeviceStatus as OperatorStatus
from proskenion.core.events import KnxTelegramReceived
from proskenion.db.crud.base import now_iso

log = logging.getLogger(__name__)

# -- knxd wire protocol constants (see the module docstring for the source) --

EIB_OPEN_GROUPCON = 0x0026
EIB_GROUP_PACKET = 0x0027

#: KNX Application Layer PCI, KNX specification §3.3.7.2 — the top bits of a
#: group-value APDU's second byte.
APCI_READ = 0x00
APCI_RESPONSE = 0x40
APCI_WRITE = 0x80

#: The APDU "short form" packs a payload into the low 6 bits of the APCI
#: byte; see :mod:`proskenion.core.knx_dpt`.
_SHORT_FORM_MASK = knx_dpt.SHORT_FORM_MASK

#: §7.1 *Outgoing writes*: the global telegram budget.
RATE_LIMIT_PER_SECOND = 15
RATE_LIMIT_WINDOW_S = 1.0
#: Admission is counted when a telegram is released, but the budget is about
#: what reaches the bus, and scheduling or network jitter can bunch releases up
#: on the way. Admitting 15 per window plus this margin keeps any one second of
#: delivery within 15 for up to this much jitter, at the cost of a sustained
#: ceiling of about 13.6 per second.
RATE_LIMIT_MARGIN_S = 0.1

#: Backoff, exactly the shape every other client in the system uses (§5.3):
#: starts here, doubles, caps below, resets only after a successful probe.
INITIAL_RETRY_DELAY_S = 5.0
MAX_RETRY_DELAY_S = 300.0

#: KNXnet/IP's standard UDP port, on which knxd's ``ipt:`` tunnel reaches the
#: gateway (§3.1, §4.11; ``appliance/etc/knxd.conf.default`` names no port, so
#: knxd uses this one). This module talks only to knxd, never to the gateway;
#: the constant names the one port the appliance's firewall must open for the
#: KNX traffic this subsystem causes.
KNXNET_IP_PORT = 3671

_GROUP_ADDRESS_RE = re.compile(r"^(\d{1,2})/(\d)/(\d{1,3})$")

StatusReporter = Callable[..., Awaitable[None]]
"""What :meth:`proskenion.core.devices.DeviceManager.report_subsystem_status`
looks like — injected so this module never touches the state store."""


# -- addressing (§7.1) --------------------------------------------------------


def parse_group_address(address: str) -> int:
    """The packed 16-bit form of a three-level group address, e.g. ``"1/0/1"``."""
    match = _GROUP_ADDRESS_RE.match(address.strip())
    if match is None:
        raise ValueError(f"{address!r} is not a three-level group address (e.g. '1/0/1')")
    main, middle, sub = (int(part) for part in match.groups())
    if main > 31 or middle > 7 or sub > 255:
        raise ValueError(f"{address!r} is out of range for a group address")
    return (main << 11) | (middle << 8) | sub


def format_group_address(packed: int) -> str:
    """The three-level string form of a packed 16-bit group address."""
    return f"{(packed >> 11) & 0x1F}/{(packed >> 8) & 0x07}/{packed & 0xFF}"


def format_individual_address(packed: int) -> str:
    """The ``area.line.device`` string form of a packed 16-bit physical address."""
    return f"{(packed >> 12) & 0x0F}.{(packed >> 8) & 0x0F}.{packed & 0xFF}"


class AddressDirection(StrEnum):
    """§7.1's ``KNXGroupAddress.direction``."""

    INCOMING = "incoming"
    OUTGOING = "outgoing"
    BOTH = "both"


@dataclass(frozen=True, slots=True)
class AddressEntry:
    """What this module needs from one row of the §7.1 group address library."""

    dpt: str
    direction: AddressDirection


class AddressRegistry(Protocol):
    """What DPT and direction a group address has — injected, not read from
    the database here (see the module docstring)."""

    def lookup(self, group_address: str) -> AddressEntry | None: ...


class InMemoryAddressRegistry:
    """A plain dict-backed :class:`AddressRegistry`, for tests and any caller
    that has not wired up the database-backed one yet."""

    def __init__(self, entries: Mapping[str, AddressEntry] | None = None) -> None:
        self._entries: dict[str, AddressEntry] = dict(entries or {})

    def lookup(self, group_address: str) -> AddressEntry | None:
        return self._entries.get(group_address)

    def register(self, group_address: str, dpt: str, direction: AddressDirection) -> None:
        self._entries[group_address] = AddressEntry(dpt, direction)


# -- errors --------------------------------------------------------------------


class KnxError(Exception):
    """Base of every error this module raises."""


class UnknownGroupAddress(KnxError):
    def __init__(self, group_address: str) -> None:
        super().__init__(f"{group_address} is not in the group address library")
        self.group_address = group_address


class IncomingOnlyAddress(KnxError):
    def __init__(self, group_address: str) -> None:
        super().__init__(f"{group_address} is registered incoming-only; refusing to write")
        self.group_address = group_address


class UnsupportedDpt(KnxError):
    def __init__(self, dpt: str) -> None:
        super().__init__(f"DPT {dpt} is not supported (§7.1)")
        self.dpt = dpt


class KnxProtocolError(KnxError):
    """knxd responded in a way its client protocol does not allow for."""


# -- outgoing priority and the telegram budget (§7.1) --------------------------


class Priority(IntEnum):
    """§7.1's outgoing-write priority table. Lower sends first."""

    ALARM = 1
    SCENE_STATUS = 2
    FADE_STEP = 3


@dataclass(frozen=True, slots=True)
class _PendingWrite:
    group_address: str
    dpt: str | None
    value: Any
    priority: Priority
    wire_body: bytes  # group address (2 bytes) + APDU, ready to send as EIB_GROUP_PACKET
    apdu: bytes  # the APDU alone, for logging and the monitor


class _Budget:
    """The §7.1 priority queue: alarm, then scene feedback, then fade steps.

    Coalescing only priority 3 (fade steps) — a newer value queued for an
    address replaces an older one still waiting there — is an implementation
    choice consistent with, but not stated by, §7.1: a stale intermediate
    fade value queued behind a backlog is pure waste and would stretch the
    fade once it finally sent. Priorities 1 and 2 are never coalesced: each
    alarm or scene-status write carries its own meaning and every one of
    them is sent. A plain ``dict`` gives priority 3 exactly this behaviour
    for free — re-assigning an existing key keeps its original position in
    iteration order, so a coalesced write is still sent in its original
    turn, just with the newer value.
    """

    def __init__(self) -> None:
        self._alarm: deque[_PendingWrite] = deque()
        self._status: deque[_PendingWrite] = deque()
        self._fade: dict[str, _PendingWrite] = {}
        self._has_pending = asyncio.Event()

    def enqueue(self, item: _PendingWrite) -> None:
        if item.priority is Priority.ALARM:
            self._alarm.append(item)
        elif item.priority is Priority.SCENE_STATUS:
            self._status.append(item)
        else:
            self._fade[item.group_address] = item
        self._has_pending.set()

    async def next(self) -> _PendingWrite:
        """The next item to send, in priority order; waits when empty."""
        while True:
            if self._alarm:
                return self._alarm.popleft()
            if self._status:
                return self._status.popleft()
            if self._fade:
                address = next(iter(self._fade))
                return self._fade.pop(address)
            self._has_pending.clear()
            await self._has_pending.wait()

    def depth(self) -> dict[str, int]:
        return {
            "alarm": len(self._alarm),
            "scene_status": len(self._status),
            "fade_step": len(self._fade),
        }


class _RateLimiter:
    """A rolling-window admission gate for §7.1's 15 telegrams/second budget.

    Tracks send timestamps in a rolling window (not a fixed calendar
    bucket), so "no more than 15 telegrams in any one-second window" holds
    for every window, not only ones aligned to a clock tick.
    """

    def __init__(
        self,
        rate: int = RATE_LIMIT_PER_SECOND,
        window_s: float = RATE_LIMIT_WINDOW_S + RATE_LIMIT_MARGIN_S,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._rate = rate
        self._window = window_s
        self._clock = clock
        self._sleep = sleep
        self._sent: deque[float] = deque()

    async def acquire(self) -> None:
        while True:
            now = self._clock()
            while self._sent and now - self._sent[0] >= self._window:
                self._sent.popleft()
            if len(self._sent) < self._rate:
                self._sent.append(now)
                return
            await self._sleep(max(self._window - (now - self._sent[0]), 0.0))


class _Backoff:
    """Doubling backoff capped at ``max_delay``, reset only after a success."""

    def __init__(self, initial: float, maximum: float) -> None:
        self._initial = initial
        self._max = maximum
        self._delay = initial

    def reset(self) -> None:
        self._delay = self._initial

    def next(self) -> float:
        delay = self._delay
        self._delay = min(self._delay * 2, self._max)
        return delay


# -- the live telegram monitor (§7.1, §21.19) -----------------------------------

Direction = Literal["incoming", "outgoing"]


@dataclass(frozen=True, slots=True)
class MonitorEntry:
    """One telegram, either direction — what §21.19's live monitor screen shows."""

    timestamp: str  # ISO 8601 with offset (§4.9)
    direction: Direction
    group_address: str
    dpt: str | None
    value: Any
    raw: bytes
    source_address: str | None = None  # only meaningful for "incoming"


class TelegramMonitor:
    """Every telegram, both directions, with a bounded replay buffer.

    §21.19 serves this as server-sent events; that HTTP wiring belongs to
    the API task. This class is transport-agnostic — :meth:`stream` is a
    plain async iterator any consumer can drive, and a monitor opened
    mid-stream first replays the recent buffer so it is not looking at an
    empty screen.
    """

    def __init__(self, buffer_size: int = 200, subscriber_queue_size: int = 256) -> None:
        self._buffer: deque[MonitorEntry] = deque(maxlen=buffer_size)
        self._subscriber_queue_size = subscriber_queue_size
        self._subscribers: set[asyncio.Queue[MonitorEntry]] = set()

    def record(self, entry: MonitorEntry) -> None:
        self._buffer.append(entry)
        for queue in list(self._subscribers):
            if queue.full():
                queue.get_nowait()  # a slow reader sees the newest, not a growing backlog
            queue.put_nowait(entry)

    def recent(self) -> list[MonitorEntry]:
        return list(self._buffer)

    async def stream(self) -> AsyncIterator[MonitorEntry]:
        queue: asyncio.Queue[MonitorEntry] = asyncio.Queue(maxsize=self._subscriber_queue_size)
        for entry in self.recent():
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(entry)
        self._subscribers.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)


@dataclass(frozen=True, slots=True)
class UnsupportedTelegram:
    """The most recent telegram on an address whose DPT is not supported (§7.1)."""

    dpt: str
    raw: bytes
    timestamp: str


# -- wire framing (see the module docstring for the source) --------------------


def _encode_frame(type_: int, body: bytes) -> bytes:
    payload = type_.to_bytes(2, "big") + body
    return len(payload).to_bytes(2, "big") + payload


async def _read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    header = await reader.readexactly(2)
    length = int.from_bytes(header, "big")
    payload = await reader.readexactly(length)
    return int.from_bytes(payload[0:2], "big"), payload[2:]


def _build_apdu(codec: knx_dpt.DptCodec, apci: int, raw: bytes) -> bytes:
    if codec.short_form:
        return bytes([0x00, apci | (raw[0] & _SHORT_FORM_MASK)])
    return bytes([0x00, apci]) + raw


def _decode_apdu(codec: knx_dpt.DptCodec, apdu: bytes) -> Any:
    if codec.short_form:
        return codec.decode(bytes([apdu[1] & _SHORT_FORM_MASK]))
    return codec.decode(apdu[2:])


# -- the subsystem ---------------------------------------------------------------


class KnxSubsystem:
    """The knxd connection: a persistent, supervised task (§5.3-style).

    ``run()`` connects, confirms (opens the group socket), maintains the
    connection — reading incoming telegrams, sending queued writes within
    budget, and running the optional heartbeat — and recovers with capped
    backoff, forever. It never returns and never lets an exception escape,
    so a caller schedules it with ``asyncio.create_task`` and never awaits
    it directly (see the module docstring: wiring it into the lifespan is
    another task's job).

    Status is reported through ``report_status`` — see
    :meth:`proskenion.core.devices.DeviceManager.report_subsystem_status` —
    under ``status_key`` (``"knx"``), because KNX has no ``devices`` table
    row of its own.
    """

    def __init__(
        self,
        cfg: KnxSection,
        registry: AddressRegistry,
        bus: EventBus,
        report_status: StatusReporter,
        *,
        status_key: str = "knx",
        rate_per_second: int = RATE_LIMIT_PER_SECOND,
        open_timeout_s: float = 5.0,
        heartbeat_timeout_s: float = 5.0,
        monitor_buffer: int = 200,
        backoff_initial_s: float = INITIAL_RETRY_DELAY_S,
        backoff_max_s: float = MAX_RETRY_DELAY_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._cfg = cfg
        self._registry = registry
        self._bus = bus
        self._report_status = report_status
        self._status_key = status_key
        self._open_timeout = open_timeout_s
        self._heartbeat_timeout = heartbeat_timeout_s
        self._clock = clock
        self._sleep = sleep

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._budget = _Budget()
        self._rate_limiter = _RateLimiter(rate_per_second, clock=clock, sleep=sleep)
        self._backoff = _Backoff(backoff_initial_s, backoff_max_s)
        self._monitor = TelegramMonitor(monitor_buffer)
        self._unsupported: dict[str, UnsupportedTelegram] = {}

        self._heartbeat_echo = asyncio.Event()
        self._heartbeat_last_apdu: bytes | None = None
        self._heartbeat_ok: bool | None = None
        self._heartbeat_failures = 0
        self._learned_address: str | None = None

    # -- public queries --------------------------------------------------------

    @property
    def monitor(self) -> TelegramMonitor:
        return self._monitor

    @property
    def own_address(self) -> str | None:
        """The individual address the controller's own telegrams carry, if known.

        knxd echoes the controller's writes back as incoming telegrams — the
        heartbeat depends on it — so a status write to ``1/0/11`` returns as a
        :class:`~proskenion.core.events.KnxTelegramReceived` from this address
        moments later. The rule layer compares against it so its own writes
        never trigger a rule (§8.7). ``[knx] individual_address`` wins when
        configured; otherwise the address is learned from the heartbeat's
        echo, whose source is by construction the controller's own. ``None``
        until one or the other is available.
        """
        return self._cfg.individual_address or self._learned_address

    def is_own_source(self, source_address: str) -> bool:
        """Whether an incoming telegram's source is the controller itself."""
        own = self.own_address
        return own is not None and source_address == own

    def unsupported(self) -> dict[str, UnsupportedTelegram]:
        """Addresses whose most recent telegram had an unsupported DPT (§7.1)."""
        return dict(self._unsupported)

    @property
    def heartbeat_ok(self) -> bool | None:
        """``None`` before the first heartbeat cycle; then whether it echoed."""
        return self._heartbeat_ok

    # -- §5.3-style connect/probe/disconnect ------------------------------------

    async def connect(self) -> None:
        """Open the transport to knxd. May prove nothing about knxd itself.

        ``asyncio.open_unix_connection`` is not merely unstubbed for Windows
        in typeshed (mypy runs on the Windows development machine) — it is
        genuinely absent from ``asyncio`` there at runtime, since
        :mod:`asyncio.unix_events` (which defines it) is only imported on
        POSIX. Referencing the attribute itself, not just calling it, then
        raises ``AttributeError`` rather than the ``NotImplementedError`` this
        method originally assumed, which :meth:`run`'s ``except (OSError,
        NotImplementedError)`` does not catch — breaking "never raises" the
        very first time the default (unix-socket) configuration ran through a
        full lifespan on a Windows box instead of always being pointed at the
        TCP stub in tests. Looking the attribute up defensively and raising a
        plain ``OSError`` keeps the platform gap inside the vocabulary
        :meth:`run` already handles, rather than teaching it a third
        exception type for what is really the same "could not open the
        transport" outcome.
        """
        if self._cfg.host:
            self._reader, self._writer = await asyncio.open_connection(
                self._cfg.host, self._cfg.port
            )
        else:
            open_unix_connection = getattr(asyncio, "open_unix_connection", None)
            if open_unix_connection is None:
                raise OSError(
                    "asyncio.open_unix_connection is not available on this platform "
                    "(no [knx] host/port configured for the TCP alternative)"
                )
            self._reader, self._writer = await open_unix_connection(str(self._cfg.socket))

    async def probe(self) -> None:
        """Confirm knxd's protocol responds: open the group socket. Authoritative."""
        writer = self._require_writer()
        reader = self._require_reader()
        # write_only=False (the reserved+flag+reserved body from the module
        # docstring): this subsystem always wants to receive as well as send.
        writer.write(_encode_frame(EIB_OPEN_GROUPCON, bytes([0x00, 0x00, 0x00])))
        await writer.drain()
        packet_type, _ = await asyncio.wait_for(_read_frame(reader), self._open_timeout)
        if packet_type != EIB_OPEN_GROUPCON:
            raise KnxProtocolError(
                f"unexpected response 0x{packet_type:04x} to EIB_OPEN_GROUPCON"
            )

    async def disconnect(self) -> None:
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    def _require_writer(self) -> asyncio.StreamWriter:
        if self._writer is None:
            raise RuntimeError("knx: not connected")
        return self._writer

    def _require_reader(self) -> asyncio.StreamReader:
        if self._reader is None:
            raise RuntimeError("knx: not connected")
        return self._reader

    # -- the supervised run loop -------------------------------------------------

    async def run(self) -> None:
        """Connect, confirm, maintain, recover. Never returns; never raises."""
        try:
            while True:
                await self._report_status(self._status_key, "connecting")
                try:
                    await self.connect()
                except (OSError, NotImplementedError) as exc:
                    await self._report_status(
                        self._status_key, "error", kind="config", detail=str(exc) or repr(exc)
                    )
                    await self._sleep(self._backoff.next())
                    continue
                try:
                    await self.probe()
                except (OSError, EOFError, KnxProtocolError, TimeoutError) as exc:
                    await self._report_status(
                        self._status_key, "error", kind="device", detail=str(exc) or repr(exc)
                    )
                    await self.disconnect()
                    await self._sleep(self._backoff.next())
                    continue

                self._backoff.reset()
                await self._report_status(self._status_key, "connected")
                await self._maintain()
                await self.disconnect()
                await self._report_status(
                    self._status_key, "error", kind="device", detail="connection to knxd was lost"
                )
                await self._sleep(self._backoff.next())
        finally:
            await self.disconnect()

    async def _maintain(self) -> None:
        """Run the reader, sender and (if enabled) heartbeat until one fails."""
        tasks = [
            asyncio.create_task(self._read_loop(), name="knx-reader"),
            asyncio.create_task(self._send_loop(), name="knx-sender"),
        ]
        if self._heartbeat_enabled():
            tasks.append(asyncio.create_task(self._heartbeat_loop(), name="knx-heartbeat"))
        try:
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                exc = None if task.cancelled() else task.exception()
                if exc is None:
                    continue
                if isinstance(exc, OSError | asyncio.IncompleteReadError | KnxProtocolError):
                    # The connection went: expected, and run() reconnects.
                    log.info("knx: %s ended: %s", task.get_name(), exc)
                else:
                    # Anything else is a bug in this module, not the network.
                    log.error("knx: %s failed", task.get_name(), exc_info=exc)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    # -- receiving -----------------------------------------------------------

    async def _read_loop(self) -> None:
        reader = self._require_reader()
        while True:
            packet_type, body = await _read_frame(reader)
            if packet_type == EIB_GROUP_PACKET:
                self._handle_incoming(body)
            else:
                log.debug("knx: unexpected packet type 0x%04x ignored", packet_type)

    def _handle_incoming(self, body: bytes) -> None:
        if len(body) < 6:
            log.warning("knx: short EIB_GROUP_PACKET body ignored (%d bytes)", len(body))
            return
        src = int.from_bytes(body[0:2], "big")
        dst = int.from_bytes(body[2:4], "big")
        apdu = bytes(body[4:])
        if len(apdu) < 2:
            log.warning("knx: short APDU ignored")
            return

        group_address = format_group_address(dst)
        source_address = format_individual_address(src)
        timestamp = now_iso()

        self._maybe_heartbeat_echo(group_address, apdu, source_address)

        entry = self._registry.lookup(group_address)
        if entry is None:
            log.debug("knx: telegram on unregistered address %s ignored", group_address)
            return

        codec = knx_dpt.resolve(entry.dpt)
        if codec is None:
            self._unsupported[group_address] = UnsupportedTelegram(entry.dpt, apdu, timestamp)
            log.warning(
                "knx: unsupported DPT %s on %s; raw payload %s",
                entry.dpt,
                group_address,
                apdu.hex(),
            )
            self._monitor.record(
                MonitorEntry(
                    timestamp, "incoming", group_address, entry.dpt, None, apdu, source_address
                )
            )
            return

        try:
            value = _decode_apdu(codec, apdu)
        except knx_dpt.DptError as exc:
            log.warning("knx: could not decode %s on %s: %s", entry.dpt, group_address, exc)
            return

        self._monitor.record(
            MonitorEntry(
                timestamp, "incoming", group_address, entry.dpt, value, apdu, source_address
            )
        )
        self._bus.emit(
            KnxTelegramReceived(group_address, entry.dpt, value, apdu, source_address)
        )

    # -- sending, priority and the budget --------------------------------------

    async def _send_loop(self) -> None:
        while True:
            item = await self._budget.next()
            await self._rate_limiter.acquire()
            writer = self._require_writer()
            writer.write(_encode_frame(EIB_GROUP_PACKET, item.wire_body))
            await writer.drain()
            log.info(
                "knx: wrote %s = %r (dpt=%s, priority=%s)",
                item.group_address,
                item.value,
                item.dpt,
                item.priority.name,
            )
            self._monitor.record(
                MonitorEntry(
                    now_iso(), "outgoing", item.group_address, item.dpt, item.value, item.apdu
                )
            )

    async def write(
        self, group_address: str, value: Any, *, priority: Priority | int
    ) -> None:
        """Queue a Group Value Write for ``group_address`` at ``priority`` (§7.1).

        Raises :class:`UnknownGroupAddress` when the address is not in the
        registry, :class:`IncomingOnlyAddress` when it is registered
        incoming-only (§7.1 *Outgoing writes* — "the application never
        writes to an address marked incoming-only"), and
        :class:`UnsupportedDpt` when its DPT has no codec. ``priority`` may be
        given as its number (the KNX dimmer pass passes ``3``); it is
        normalised here, because the queue and the send log both rely on it
        being a :class:`Priority`. The write is
        logged once it actually leaves, in :meth:`_send_loop` — "every write
        is logged" applies to what reaches the bus, not to what is queued.
        """
        priority = Priority(priority)
        entry = self._registry.lookup(group_address)
        if entry is None:
            raise UnknownGroupAddress(group_address)
        if entry.direction is AddressDirection.INCOMING:
            log.warning("knx: refused write to incoming-only address %s", group_address)
            raise IncomingOnlyAddress(group_address)
        codec = knx_dpt.resolve(entry.dpt)
        if codec is None:
            raise UnsupportedDpt(entry.dpt)
        raw = codec.encode(value)
        apdu = _build_apdu(codec, APCI_WRITE, raw)
        ga_bytes = parse_group_address(group_address).to_bytes(2, "big")
        self._budget.enqueue(
            _PendingWrite(group_address, entry.dpt, value, priority, ga_bytes + apdu, apdu)
        )

    async def read(self, group_address: str) -> None:
        """Send a Group Value Read for ``group_address``.

        §7.1's three priorities are specified for writes; a read carries no
        value that can go stale, so it is queued at the scene-status tier —
        an implementation choice, not stated by §7.1.
        """
        ga_bytes = parse_group_address(group_address).to_bytes(2, "big")
        apdu = bytes([0x00, APCI_READ])
        self._budget.enqueue(
            _PendingWrite(group_address, None, None, Priority.SCENE_STATUS, ga_bytes + apdu, apdu)
        )

    # -- the optional heartbeat (§7.1 *Health*) ----------------------------------

    def _heartbeat_enabled(self) -> bool:
        return (
            self._cfg.heartbeat_address is not None
            and self._cfg.heartbeat_interval_s is not None
        )

    async def _heartbeat_loop(self) -> None:
        """Write to the reserved address on an interval; confirm it echoes.

        Bypasses the address registry and DPT codecs entirely: the reserved
        address is, by §7.1's own description, "left unassigned" — nothing
        on the bus is meant to interpret it as a real datapoint — so the
        heartbeat sends a plain single-bit toggle and confirms the round
        trip by matching the raw APDU bytes it receives back, not by
        decoding a value against a registered DPT. This is an
        implementation choice consistent with, but not spelled out by,
        §7.1's "write to a reserved group address and confirm it echoes
        back".
        """
        address = self._cfg.heartbeat_address
        interval = self._cfg.heartbeat_interval_s
        assert address is not None and interval is not None
        ga_bytes = parse_group_address(address).to_bytes(2, "big")
        toggle = False
        while True:
            await self._sleep(interval)
            toggle = not toggle
            apdu = bytes([0x00, APCI_WRITE | (1 if toggle else 0)])
            self._heartbeat_echo.clear()
            self._heartbeat_last_apdu = apdu
            self._budget.enqueue(
                _PendingWrite(address, None, toggle, Priority.ALARM, ga_bytes + apdu, apdu)
            )
            try:
                await asyncio.wait_for(self._heartbeat_echo.wait(), self._heartbeat_timeout)
            except TimeoutError:
                self._heartbeat_ok = False
                self._heartbeat_failures += 1
                log.warning(
                    "knx: heartbeat on %s did not echo within %.1fs",
                    address,
                    self._heartbeat_timeout,
                )
            else:
                self._heartbeat_ok = True
                self._heartbeat_failures = 0
            await self._report_heartbeat_status()

    def _maybe_heartbeat_echo(
        self, group_address: str, apdu: bytes, source_address: str | None = None
    ) -> None:
        if (
            self._cfg.heartbeat_address == group_address
            and self._heartbeat_last_apdu is not None
            and apdu == self._heartbeat_last_apdu
        ):
            self._heartbeat_echo.set()
            # The echo of our own write carries our own source address: learn
            # it, unless configured, so the rule layer can recognise every
            # other echo of ours (§8.7). See :attr:`own_address`.
            if (
                source_address is not None
                and self._cfg.individual_address is None
                and source_address != self._learned_address
            ):
                log.info("knx: controller's own individual address is %s", source_address)
                self._learned_address = source_address

    async def _report_heartbeat_status(self) -> None:
        if self._heartbeat_ok is False:
            # One missed echo is amber and still shows as connected-but-degraded;
            # a second consecutive miss is red — the same shape §7.5 gives
            # every other client's missed-reply handling.
            status: OperatorStatus = "degraded" if self._heartbeat_failures <= 1 else "error"
            await self._report_status(
                self._status_key, status, kind="device", detail="heartbeat did not echo"
            )
        else:
            await self._report_status(self._status_key, "connected")


__all__ = [
    "AddressDirection",
    "AddressEntry",
    "AddressRegistry",
    "InMemoryAddressRegistry",
    "IncomingOnlyAddress",
    "KnxError",
    "KnxProtocolError",
    "KnxSubsystem",
    "MonitorEntry",
    "Priority",
    "TelegramMonitor",
    "UnknownGroupAddress",
    "UnsupportedDpt",
    "UnsupportedTelegram",
    "format_group_address",
    "format_individual_address",
    "parse_group_address",
]
