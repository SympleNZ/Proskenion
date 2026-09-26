"""CQ-20B native metering client (spec §7.3 *Metering — two connections*).

MIDI over TCP (the CQ's documented protocol, driven by the CQ-20B driver) carries
no metering. MixPad — Allen & Heath's own control app — does meter, over a
second, undocumented connection on port 51326: "the CQ's proprietary control
protocol rather than the documented MIDI one" (§7.3). This module is that
second connection, and nothing else. It is **read-mostly**: the handshake, a
client-init, and periodic keep-alives — "nothing that changes mixer state"
(§7.3). Every write the real protocol supports (faders, mutes, scene recall)
stays on MIDI, in the CQ-20B driver, for the three reasons §7.3 and
``docs/protocols/cq20b-native.md`` §8 give: MIDI is A&H's published contract,
so a firmware change degrades a display rather than the room; writing to a
reverse-engineered protocol on a live console mid-performance is a different
risk class from reading it; and scene recall — central to how the venue
operates — has not been reverse-engineered at all, so MIDI is needed
regardless.

Full protocol detail — framing, the connection sequence, the meter record
maps and the bench findings this implementation relies on — is
``docs/protocols/cq20b-native.md``; every constant below cites the section it
came from. Nothing here should be taken as correct beyond what that document
records: it is reverse-engineered and marked as such throughout.

Attribution and changes (Apache License 2.0)
---------------------------------------------
The framing, connection sequence and meter layout implemented below derive
from **DigiMixer** by Jon Skeet, <https://github.com/jskeet/DemoCode>,
specifically the ``DigiMixer.CqSeries``, ``DigiMixer.CqSeries.Core`` and
``DigiMixer.AllenAndHeath.Core`` projects, licensed under the **Apache
License, Version 2.0** <https://www.apache.org/licenses/LICENSE-2.0>. A copy
of the licence is available at that URL; this notice and the one in
``docs/protocols/cq20b-native.md`` together satisfy the licence's attribution
requirement.

This module is an independent Python implementation, not a translation of
DigiMixer's C#, and the Apache licence's "state changes" obligation applies
to at least these differences, each bench-confirmed against a live CQ-20B on
2026-09-11 (``docs/protocols/cq20b-native.md`` §4, §5, §9):

- **Read-only.** DigiMixer implements writing faders and mutes over this
  protocol; this module never does (see above).
- **Twice DigiMixer's input channel count.** DigiMixer reads 16 input meter
  records; the CQ-20B's input-meter body is 640 bytes, which is 32 records at
  the same 20-byte stride and offset DigiMixer already uses — the CQ-20B has
  32 metered inputs (16 mono, four stereo pairs, four stereo FX returns), not
  16.
- **A corrected output record count.** ``docs/protocols/cq20b-native.md`` §5,
  in its prose, describes the output body's eight channel records as
  occupying "the first 64 bytes"; eight records at the documented 16-byte
  stride occupy the first **128** bytes, and re-deriving Main L/R (records 6
  and 7, byte offsets 110 and 126) against ``tools/session.log`` confirms 128
  is the figure that matches the capture, not 64. This module uses the
  128-byte figure (as a record count times a stride, never a raw byte
  constant) and the mismatch is flagged in the document's implementation
  notes for the bench to square away.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Final

from proskenion.core.transport.base import ConfigurationError, TransportClosed
from proskenion.core.transport.tcp import TcpTransport

log = logging.getLogger(__name__)

# -- wire framing (cq20b-native.md §2 *Framing*) -------------------------------------

_VARIABLE_PREFIX: Final = 0x7F
_FIXED_PREFIX: Final = 0xF7

_MSG_UDP_HANDSHAKE: Final = 0
_MSG_KEEPALIVE: Final = 5
_MSG_REGULAR: Final = 7
_MSG_INPUT_METERS: Final = 8
_MSG_OUTPUT_METERS: Final = 9
_MSG_CLIENT_INIT_REQUEST: Final = 12
_MSG_CLIENT_INIT_RESPONSE: Final = 13

#: §2's ``ClientInitRequest`` body, verbatim.
_CLIENT_INIT_BODY: Final = bytes((0x02, 0x00))

#: The client's own default local UDP port for receiving meters — see the
#: class docstring's "Fixed local port (firewall)" section for why this is
#: fixed rather than ephemeral. One above the native control port (51326),
#: which is one above MIDI (51325, cq20b.md): continuing the CQ ports' own
#: numbering for a port that belongs to us, not the desk, so a packet
#: capture reads unambiguously. ``appliance/bin/auditorium-config-apply``'s
#: ``DEFAULT_DEVICES`` mixer entry opens this exact port inbound for the
#: mixer's address and must be kept in step with it by hand — that script is
#: stand-alone, stdlib-only, and does not import this module.
DEFAULT_LOCAL_UDP_PORT: Final = 51327


@dataclass(frozen=True, slots=True)
class _Frame:
    """One decoded message: a type and its body, framing prefix stripped."""

    type: int
    body: bytes


def _encode(msg_type: int, body: bytes = b"") -> bytes:
    """The variable-length form (cq20b-native.md §2): ``0x7F | type | len32le | body``.

    Every message this client *sends* uses this form — the handshake, the
    client-init request and the keep-alive are all variable-length in the
    document's own connection sequence.
    """
    return bytes((_VARIABLE_PREFIX, msg_type)) + struct.pack("<i", len(body)) + body


def _take_frame(buf: bytearray) -> _Frame | None:
    """Extract and remove one complete frame from the front of ``buf``.

    Returns ``None`` only when the buffer holds no complete frame yet — the
    caller should append more bytes and try again. A leading byte that
    matches neither framing prefix is discarded and the search continues
    (cq20b-native.md §2's two prefixes are the whole of the framing grammar,
    so anything else is either garbage or a resynchronisation point); this is
    what keeps a corrupted or unexpected byte from wedging the parser
    forever, matching ``tools/cq_probe.py``'s ``parse_stream``, which this
    function was checked against.

    The fixed-length form's 8- versus 9-byte discriminator is §2's own:
    ``buf[1] == 0x12 and buf[3] == 0x23``, or ``buf[1] == 0x13 and buf[3] ==
    0x16``, is the 9-byte variant; everything else fixed-length is 8 bytes.
    This client never sends the fixed form and does not otherwise interpret
    it — the mixer's unsolicited type-7 ``Regular`` pushes (bench §9) arrive
    this way and are simply counted past, never decoded.
    """
    while buf:
        if buf[0] == _VARIABLE_PREFIX:
            if len(buf) < 6:
                return None
            length = struct.unpack_from("<i", buf, 2)[0]
            if length < 0 or len(buf) < 6 + length:
                return None
            frame = _Frame(buf[1], bytes(buf[6 : 6 + length]))
            del buf[: 6 + length]
            return frame
        if buf[0] == _FIXED_PREFIX:
            if len(buf) < 8:
                return None
            nine_byte = (buf[1] == 0x12 and buf[3] == 0x23) or (buf[1] == 0x13 and buf[3] == 0x16)
            total = 9 if nine_byte else 8
            if len(buf) < total:
                return None
            frame = _Frame(_MSG_REGULAR, bytes(buf[1:total]))
            del buf[:total]
            return frame
        del buf[0]  # not a recognised prefix — resynchronise and keep looking
    return None


def _all_frames(buf: bytearray) -> list[_Frame]:
    """Every complete frame ``buf`` holds, in order. Used for one UDP
    datagram, which — unlike the TCP stream — is already a self-contained
    chunk of bytes with no continuation to wait for."""
    frames: list[_Frame] = []
    while (frame := _take_frame(buf)) is not None:
        frames.append(frame)
    return frames


# -- meter record maps (cq20b-native.md §4, §5, §7) -----------------------------------

#: Input-meter body: 32 records of 20 bytes, level at byte offset 14 within
#: each record (§4, confirmed by capture — DigiMixer's stride and offset are
#: right; its record count of 16 is not).
INPUT_STRIDE: Final = 20
#: Output-meter body: 8 records of 16 bytes (§5; see the module docstring's
#: "corrected output record count" for why this is 8×16, never a raw 64 or
#: 128 constant, and never derived by dividing the body length — §5's own
#: warning is that doing so gives 50, of which 42 are not channels).
OUTPUT_STRIDE: Final = 16
#: Both record shapes carry their level at the same byte offset (§4, §5).
_LEVEL_OFFSET: Final = 14

#: The 32 input records, in order, mapped from cq20b-native.md §4's table to
#: driver-reference-shaped identifiers. Stereo pairs and the four FX returns
#: are kept as separate left/right entries rather than combined into one
#: value per pair: "a stereo channel maps to several driver_refs … and each
#: gets its own meter — a summed meter would hide one side of a stereo
#: source failing, which is the fault this feature exists to find" (spec
#: §7.3, *Ganged channels carry a list*). Composing code gangs the ``…l``/
#: ``…r`` pair for one virtual channel exactly as that subsection describes;
#: this module does not do the ganging itself, because it does not know the
#: admin's channel configuration (§15.6) — only the CQ-20B driver does.
INPUT_REFS: Final[tuple[str, ...]] = (
    *(f"ip{n}" for n in range(1, 17)),  # records 0-15
    "st1l", "st1r",  # records 16-17
    "st2l", "st2r",  # records 18-19
    "usbl", "usbr",  # records 20-21
    "btl", "btr",  # records 22-23
    "fx1l", "fx1r", "fx2l", "fx2r", "fx3l", "fx3r", "fx4l", "fx4r",  # records 24-31
)

#: The 8 output records, in order (§5's table). ``out1``…``out6`` are the
#: mix outputs; ``mainl``/``mainr`` are Main L/R, kept separate for the same
#: reason as the stereo inputs above.
OUTPUT_REFS: Final[tuple[str, ...]] = (
    "out1", "out2", "out3", "out4", "out5", "out6", "mainl", "mainr",
)

#: Every identifier this client can ever deliver through ``on_meters`` — the
#: mapping the CQ-20B driver composes against (see the module docstring and the class
#: docstring below).
ALL_REFS: Final[frozenset[str]] = frozenset(INPUT_REFS) | frozenset(OUTPUT_REFS)

#: §7's conversion, inherited from the Qu-SB via DigiMixer. "Not proven
#: without injecting a reference tone… relative movement is what this system
#: needs, so the absolute calibration is not load-bearing" — cq20b-native.md
#: §7. The unit is dB, not dBFS: there is no declared 0 dBFS reference point
#: for this desk, only this offset formula's own zero.
_DB_ZERO_RAW: Final = 0x8000
_DB_SCALE: Final = 256.0
_DB_SHIFT: Final = 18.0


def _raw_to_db(raw: int) -> float:
    """cq20b-native.md §7's conversion, verbatim."""
    return (raw - _DB_ZERO_RAW) / _DB_SCALE - _DB_SHIFT


def _parse_records(body: bytes, refs: tuple[str, ...], stride: int) -> dict[str, float]:
    """Decode every record ``refs`` names out of ``body``.

    Stops at the first record that would read past the end of ``body``
    rather than raising: a short or truncated frame (a garbled UDP datagram,
    say) yields whatever prefix of records was actually present instead of
    discarding the frame entirely. Never reads past ``len(refs)`` records —
    the output body's trailing bytes beyond the eight channel records are
    "not channel records" and of unknown meaning (§5) and are never touched.
    """
    levels: dict[str, float] = {}
    for index, ref in enumerate(refs):
        offset = index * stride + _LEVEL_OFFSET
        if offset + 2 > len(body):
            break
        raw = struct.unpack_from("<H", body, offset)[0]
        levels[ref] = _raw_to_db(raw)
    return levels


# -- callbacks -------------------------------------------------------------------------

#: Called with every reference a just-parsed meter frame carried (a partial
#: update — one call per input-meter frame, one per output-meter frame, never
#: a merged view). Values are dB (see ``_raw_to_db`` above); ``None`` is
#: never produced by this client today, but the parameter keeps the same
#: shape as :class:`proskenion.core.drivers.capabilities.MeterFrame.levels`
#: for whoever composes it into one. **Display only — see the class
#: docstring's "Never a control input" section.**
MeterCallback = Callable[[Mapping[str, float | None]], Awaitable[None]]

#: Called on every change of availability, never on a repeated report of the
#: same state (mirrors :class:`proskenion.core.drivers.pjlink.StateListener`'s
#: "never on a repeat read" rule). ``reason`` is ``None`` exactly when
#: ``available`` is ``True``.
AvailabilityCallback = Callable[[bool, str | None], Awaitable[None]]


class _UnexpectedReply(Exception):
    """The mixer's reply to the handshake or the client-init was not what
    cq20b-native.md §2 describes — treated the same as a refused connection
    (see :meth:`NativeMeterClient._session`)."""


class _MeterDatagramProtocol(asyncio.DatagramProtocol):
    """Feeds every inbound UDP datagram to ``queue`` whole.

    Overflow drops the oldest datagram, exactly as
    :class:`proskenion.core.transport.udp.UdpTransport` does for the same
    reason (§5.6's spirit): meters are continuous data, and the newest frame
    is the only one worth keeping. This client does not use
    :class:`~proskenion.core.transport.udp.UdpTransport` itself because that
    transport's remote address is fixed at ``open()``, while the CQ's meter
    port is only known after the TCP handshake reply (cq20b-native.md §2)
    names it — the same problem ``tools/cq_probe.py`` solves the same way,
    with its own bare :class:`asyncio.DatagramProtocol`.
    """

    #: Datagrams buffered before the oldest is dropped — generous, since a
    #: dropped meter datagram just means one stale frame less, never lost
    #: state (§5.6, B58).
    QUEUE_SIZE: ClassVar[int] = 64

    def __init__(self, queue: asyncio.Queue[bytes]) -> None:
        self._queue = queue

    def datagram_received(self, data: bytes, addr: Any) -> None:
        if self._queue.full():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - race with the reader
                pass
        self._queue.put_nowait(data)

    def error_received(self, exc: Exception) -> None:
        # ICMP unreachable and friends surface here; nothing to do with one —
        # the session's own liveness comes from the TCP control connection.
        return None


#: The clear, specific status bench §9 asks for: "the refusal should name its
#: likely cause… adding 'both MixPad slots may be in use' turns it into an
#: instruction the operator can act on." Fixed wording — every refusal this
#: client can currently distinguish (a refused TCP connect, a connection
#: dropped mid-handshake, or an unrecognised reply) has the one explanation
#: bench §9 found: the CQ's two MixPad slots are both already in use. The
#: underlying exception is logged, not appended here, so the operator-facing
#: text stays stable and testable rather than growing a different message
#: for every network errno.
MSG_REFUSED: Final = (
    "Metering unavailable — the native connection was refused. Both MixPad "
    "slots may be in use; metering returns once one frees."
)


def _lost_message(detail: str) -> str:
    """The status for a session that connected and then lost its transport —
    distinct from :data:`MSG_REFUSED`, which is for a connection that never
    got in at all (§9's two bench scenarios are different failures with
    different likely causes)."""
    return f"Metering unavailable — the native connection was lost: {detail}"


class NativeMeterClient:
    """The CQ-20B's read-mostly native connection, port 51326 (§7.3).

    Owns the handshake, the client-init and the ~3-second keep-alive
    (cq20b-native.md §2, §9) over its own TCP control connection plus a UDP
    socket for meters — **and nothing else**. It never sends a fader, mute,
    pan or scene-recall message; §8 of the document explains why control
    stays on MIDI, and the structural test in
    ``tests/unit/core/mixer/test_native.py`` proves this module contains no
    code that could.

    **Never a control input.** Every level this client produces is a display
    value passed straight to ``on_meters`` and nowhere else. It holds no
    cache a caller could read back, keeps no history and computes nothing
    from a meter reading — there is nothing here *to* feed into a control
    decision even by accident (spec §7.3 *Meters never enter the control
    path*, B58).

    **Its own connection and retry state.** A separate instance from
    whatever the CQ-20B's MIDI driver runs, with its own backoff — never the
    general driver's 300 s cap (§5.3, B38). Bench §9 recommends a shorter one
    for this specific connection: "retrying it costs the mixer nothing and
    the wait is visible to the operator" as "metering unavailable" rather
    than a room going dark, so :attr:`MAX_RETRY_DELAY` is 60 s here. That
    number is this module's own engineering judgement from §9's reasoning,
    not a bench-measured or specification-mandated value — see
    ``docs/protocols/cq20b-native.md``'s implementation notes.

    **Fixed local port (firewall).** The appliance's outbound firewall is
    default-drop except a fixed table of device ports (spec §3.4), and its
    inbound firewall is default-drop except a fixed table of listen ports
    plus ``ct state established,related`` (§3.3) — see
    ``appliance/bin/auditorium-config-apply``. An *ephemeral* local UDP port
    (``local_addr=("0.0.0.0", 0)``, this module's first cut) cannot appear in
    either fixed table, because it is a different number on every
    connection attempt, so it depends entirely on conntrack: an inbound meter
    datagram is admitted only once *this client's own* outbound packet to the
    mixer's meter port has already passed through, forming the conntrack
    entry that ``established,related`` then matches. That fails twice over —
    the CQ's meter port is itself negotiated per connection (observed 51324
    on the bench, cq20b-native.md §2, §9, not a fixed number to allow
    outbound to in the first place), and even granting outbound, "meters
    begin arriving on our UDP port" is §2 step 7, one step *before* the
    keep-alive of step 8, so the first datagrams would arrive before any
    conntrack entry existed to admit them.

    Binding a **fixed, configurable** local port instead — :data:`DEFAULT_LOCAL_UDP_PORT`
    unless the constructor's ``local_udp_port`` says otherwise — lets the
    appliance firewall carry one static inbound rule for "the mixer's
    address, this exact port", independent of conntrack timing entirely; and
    :meth:`_keepalive` now sends its first packet immediately once a session
    starts, rather than waiting out a whole :attr:`KEEPALIVE_INTERVAL` first,
    which keeps this client's *own* half of the exchange as prompt as §2's
    steps 7 and 8 describe rather than trailing it by design. See
    ``docs/protocols/cq20b-native.md``'s implementation notes for the
    firewall rules themselves.

    Construction
    ------------
    ``transport`` is a :class:`~proskenion.core.transport.tcp.TcpTransport`
    already configured for the mixer's host and port 51326 — built by the
    caller exactly as any other driver's transport is (§5.5: the transport
    owns addressing, this class never constructs one). ``TcpTransport``
    specifically, not the general :class:`~proskenion.core.transport.base.Transport`
    protocol, because this client reads ``transport.config["host"]`` once, to
    address the UDP socket the handshake negotiates — the one piece of
    addressing this protocol's own design (a TCP-negotiated UDP peer) does
    not let the transport abstraction carry on its own; see
    :class:`_MeterDatagramProtocol`'s docstring.

    ``local_udp_port`` defaults to :data:`DEFAULT_LOCAL_UDP_PORT` and is a
    plain constructor parameter — not read from ``transport.config`` — so
    the CQ-20B driver can take it straight from the device's own configuration
    if an installation ever needs a different one (the appliance firewall
    rule would need updating to match; see above).

    ``on_meters`` and ``on_availability`` are required keyword-only callbacks
    — see their type aliases above for the exact contract. Both are awaited
    directly; an exception from either is logged and does not stop the
    client (mirrors :meth:`proskenion.core.drivers.pjlink.PJLinkDriver._update_state`'s
    listener handling).

    Composing this from the CQ-20B driver
    --------------------------------------
    ::

        transport = TcpTransport({"host": host, "port": 51326})
        meters = NativeMeterClient(
            transport,
            on_meters=publish_meters,   # e.g. domain.writer(...).set_item("meters", ref, db)
            on_availability=publish_availability,
            # local_udp_port=DEFAULT_LOCAL_UDP_PORT unless configured otherwise
        )
        await meters.start()
        ...
        await meters.stop()

    ``on_meters`` receives a partial mapping — the references one just-parsed
    frame carried, keyed as :data:`INPUT_REFS` and :data:`OUTPUT_REFS` list
    them (``"ip1"``…``"ip16"``, ``"st1l"``/``"st1r"``, ``"st2l"``/``"st2r"``,
    ``"usbl"``/``"usbr"``, ``"btl"``/``"btr"``, ``"fx1l"``/``"fx1r"`` through
    ``"fx4l"``/``"fx4r"``, ``"out1"``…``"out6"``, ``"mainl"``/``"mainr"``) —
    never a merged snapshot. §7.3's driver-reference vocabulary
    (``ip1``…``ip16``, ``st1``, ``st2``, ``usb``, ``bt``, the FX returns,
    ``out1``…``out6``, Main L/R) names the *channels* MIDI addresses as
    single faders; this client's identifiers are one level finer, one per
    physical meter, because spec §7.3 *Ganged channels carry a list*
    requires exactly that granularity for metering ("each gets its own
    meter"). The composing driver gangs the ``…l``/``…r`` pair for a given
    §15.6 ``mixer_channel_refs`` row into that channel's ordered meter list;
    this client supplies the two halves, never the combination.
    """

    #: cq20b-native.md §9: "a 3-second keep-alive is sufficient… whether a
    #: longer interval would also hold is unknown; 3 s is safe."
    KEEPALIVE_INTERVAL: ClassVar[float] = 3.0

    #: Seconds allowed for each half of the handshake/client-init exchange
    #: (§2 steps 2-3 and 5-6) before treating it as a refusal.
    HANDSHAKE_TIMEOUT: ClassVar[float] = 5.0

    #: How often the TCP watch loop re-checks for a frame while otherwise
    #: idle. Not a protocol value — see :meth:`_watch_tcp`.
    TCP_POLL_INTERVAL: ClassVar[float] = 10.0

    #: This connection's own backoff — see the class docstring.
    INITIAL_RETRY_DELAY: ClassVar[float] = 5.0
    MAX_RETRY_DELAY: ClassVar[float] = 60.0

    def __init__(
        self,
        transport: TcpTransport,
        *,
        on_meters: MeterCallback,
        on_availability: AvailabilityCallback,
        local_udp_port: int = DEFAULT_LOCAL_UDP_PORT,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.transport = transport
        self._on_meters = on_meters
        self._on_availability = on_availability
        #: Fixed, not ephemeral — see the class docstring's "Fixed local
        #: port (firewall)" section for why.
        self._local_udp_port = local_udp_port
        #: Replaced in tests so backoff and the keep-alive cadence do not
        #: really wait (mirrors :class:`proskenion.core.drivers.base.Driver`).
        self._sleep = sleep
        self._host: str = str(transport.config["host"])
        self._retry_delay = self.INITIAL_RETRY_DELAY
        #: ``None`` until the first connection attempt reports something —
        #: see :meth:`_report`: the very first failure is still a transition,
        #: from "unknown" to "unavailable".
        self._available: bool | None = None
        #: The reason behind :attr:`_available`, kept alongside it so a
        #: caller that registers *after* the first report — see
        #: :attr:`reason`'s own doc — can still read it. ``None``
        #: exactly when :attr:`_available` is not ``False``, mirroring
        #: :data:`AvailabilityCallback`'s own contract.
        self._reason: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._tcp_buffer = bytearray()
        self._udp_transport: asyncio.DatagramTransport | None = None
        self._udp_queue: asyncio.Queue[bytes] = asyncio.Queue(_MeterDatagramProtocol.QUEUE_SIZE)

    @property
    def available(self) -> bool:
        """The last availability reported to ``on_availability`` — no I/O,
        just this client's own memory of it."""
        return self._available is True

    @property
    def reason(self) -> str | None:
        """The reason behind the last report, mirroring :attr:`available` —
        no I/O, just this client's own memory of it. ``None`` while
        available or before anything has been reported.

        Closes a startup-ordering gap: :meth:`_report` calls
        ``on_availability`` only *on a change*, so a caller that registers a
        listener after the first (and, for a connection refused outright,
        possibly only) report would otherwise never learn it. A caller reads
        this once, right after registering, exactly as
        :meth:`~proskenion.core.drivers.cq20b.CQ20BDriver.known_state` lets a
        late-registering caller catch up on channel values with no further
        command (see that method's own docstring, and
        :class:`~proskenion.core.mixer.service.MixerService`'s own "Startup
        ordering" section).
        """
        return self._reason

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Start the connect/retry loop in the background. Idempotent."""
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.get_running_loop().create_task(
            self._run(), name="cq-native-meters"
        )

    async def stop(self) -> None:
        """Stop the loop and close both the TCP and UDP sockets. Idempotent."""
        self._stopping = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._close_udp_socket()
        await self.transport.close()

    # -- the connect/retry loop (its own, separate from MIDI's — see the
    #    class docstring) -----------------------------------------------------

    async def _run(self) -> None:
        while not self._stopping:
            await self._session()
            if self._stopping:
                return
            await self._sleep(self._retry_delay)
            self._retry_delay = min(self._retry_delay * 2, self.MAX_RETRY_DELAY)

    async def _session(self) -> None:
        """One connection attempt, start to finish.

        Never raises: every failure this method can distinguish is reported
        through ``on_availability`` and then returns, leaving :meth:`_run` to
        manage only the backoff between attempts. A refusal before the
        client-init completes (refused TCP connect, connection dropped
        mid-handshake, or an unrecognised reply — bench §9 found no way to
        tell these apart on the wire, only that the cause is the same either
        way) reports :data:`MSG_REFUSED`; a failure after a session was
        already up reports :func:`_lost_message`.
        """
        try:
            await self.transport.open()
        except ConfigurationError as exc:
            log.info("cq native: connect failed: %s", exc)
            await self._report(False, MSG_REFUSED)
            return
        try:
            try:
                await self._open_udp_socket()
                mixer_udp_port = await self._handshake_and_init()
            except (TransportClosed, OSError, TimeoutError, _UnexpectedReply) as exc:
                # OSError here also covers the fixed local UDP port
                # (:data:`DEFAULT_LOCAL_UDP_PORT` by default) being unable to
                # bind — another process already holding it, say. That is
                # exactly as much a refusal as the mixer's own would be: this
                # session cannot start, for a reason the operator can look
                # into, and the retry loop below is what gives it another go.
                log.info("cq native: handshake failed: %s", exc)
                await self._report(False, MSG_REFUSED)
                return
            # Reset only after the client-init actually completed — the same
            # rule as the general driver's backoff (§5.3, B38), applied to
            # this connection's own retry state.
            self._retry_delay = self.INITIAL_RETRY_DELAY
            await self._report(True, None)
            try:
                await self._serve(mixer_udp_port)
            except* (TransportClosed, OSError, TimeoutError) as group:
                detail = str(group.exceptions[0]) or type(group.exceptions[0]).__name__
                log.info("cq native: session lost: %s", detail)
                await self._report(False, _lost_message(detail))
        finally:
            await self._close_udp_socket()
            await self.transport.close()

    async def _report(self, available: bool, reason: str | None) -> None:
        """Call ``on_availability`` exactly on a change, never a repeat
        (§7.3 *Failure is degradation, not loss*: the interface says
        "metering unavailable" once, not on every failed retry)."""
        if self._available is available:
            return
        self._available = available
        self._reason = reason
        try:
            await self._on_availability(available, reason)
        except Exception:  # a callback's bug must not break reconnection
            log.exception("cq native: on_availability callback raised")

    # -- connection sequence (cq20b-native.md §2) -----------------------------

    async def _open_udp_socket(self) -> None:
        """Bind the fixed local UDP port (see the class docstring's "Fixed
        local port (firewall)" section) — never an ephemeral one, so the
        appliance firewall can carry one static inbound rule for it."""
        loop = asyncio.get_running_loop()
        self._udp_queue = asyncio.Queue(_MeterDatagramProtocol.QUEUE_SIZE)
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _MeterDatagramProtocol(self._udp_queue),
            local_addr=("0.0.0.0", self._local_udp_port),
        )
        self._udp_transport = transport

    async def _close_udp_socket(self) -> None:
        transport, self._udp_transport = self._udp_transport, None
        if transport is not None:
            transport.close()

    async def _handshake_and_init(self) -> int:
        """§2 steps 2, 3, 5 and 6. Step 4, ``VersionRequest``, is marked
        optional in the document's own sequence and is skipped: nothing in
        this client reads a version response, so the extra round trip buys
        nothing. Returns the mixer's UDP port from the handshake reply.
        """
        await self.transport.send(
            _encode(_MSG_UDP_HANDSHAKE, struct.pack("<H", self._local_udp_port))
        )
        handshake_reply = await self._read_tcp_frame(self.HANDSHAKE_TIMEOUT)
        if handshake_reply.type != _MSG_UDP_HANDSHAKE or len(handshake_reply.body) < 2:
            raise _UnexpectedReply(f"unexpected reply to the UDP handshake: {handshake_reply!r}")
        mixer_udp_port = int(struct.unpack_from("<H", handshake_reply.body, 0)[0])

        await self.transport.send(_encode(_MSG_CLIENT_INIT_REQUEST, _CLIENT_INIT_BODY))
        init_reply = await self._read_tcp_frame(self.HANDSHAKE_TIMEOUT)
        if init_reply.type != _MSG_CLIENT_INIT_RESPONSE:
            raise _UnexpectedReply(f"unexpected reply to client-init: {init_reply!r}")
        return mixer_udp_port

    async def _read_tcp_frame(self, budget: float) -> _Frame:
        """Read one complete frame from the TCP control connection within
        ``budget`` seconds, buffering partial reads across calls (§2's
        framing is stream-oriented; a single ``receive`` is not guaranteed
        to land on a frame boundary).

        Named ``budget`` rather than ``timeout`` only to keep this internal
        method out of ruff's ASYNC109 (which wants ``timeout``-named
        parameters replaced by an outer ``asyncio.timeout()``); the shape is
        exactly ``proskenion.core.transport.base``'s
        ``receive(timeout: float)`` contract, called here in a loop rather
        than once, because one frame is not guaranteed to arrive in one
        ``receive``.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget
        while True:
            frame = _take_frame(self._tcp_buffer)
            if frame is not None:
                return frame
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("no frame from the mixer before the timeout")
            self._tcp_buffer.extend(await self.transport.receive(remaining))

    # -- serving one connected session ----------------------------------------

    async def _serve(self, mixer_udp_port: int) -> None:
        """Run for the life of one connected session: the TCP watch, the UDP
        meter reader and the keep-alive sender, all concurrently. Returns
        only if the task group is cancelled from outside (:meth:`stop`);
        otherwise the first of the three to fail cancels the other two and
        the group raises, which :meth:`_session` classifies.

        The TCP watch runs as the group's body, in this task, rather than as
        a third child beside a parent that would only wait: a body that
        raises is grouped and cancels its siblings exactly as a failing child
        does, and a failing child cancels the body. One task fewer (§23.3).
        """
        async with asyncio.TaskGroup() as group:
            group.create_task(self._read_udp(), name="cq-native-udp-read")
            group.create_task(self._keepalive(mixer_udp_port), name="cq-native-keepalive")
            await self._watch_tcp()

    async def _watch_tcp(self) -> None:
        """Drain the TCP control connection for the life of the session.

        Nothing more is sent over TCP after the handshake, but the mixer
        pushes unsolicited type-7 ``Regular`` frames unprompted (bench §9,
        confirmed by capture) that this system does not use; they are
        logged at debug level and discarded. A quiet connection is not a
        failure — :meth:`_read_tcp_frame` raising :class:`TimeoutError` here
        means only that nothing arrived within one poll window, exactly the
        distinction ``proskenion.core.transport.base``'s module docstring
        draws between a quiet line and a lost one — so this loop simply
        polls again. Only the transport itself raising (closed, reset, an
        ``OSError``) ends the loop, which is what tells :meth:`_serve` the
        control connection is gone.
        """
        while True:
            try:
                frame = await self._read_tcp_frame(self.TCP_POLL_INTERVAL)
            except TimeoutError:
                continue
            if frame.type != _MSG_REGULAR:
                log.debug("cq native: unexpected TCP frame type %d ignored", frame.type)

    async def _read_udp(self) -> None:
        """Parse every UDP datagram into meter frames and hand each one's
        levels to ``on_meters`` as soon as it arrives.

        Blocks on the queue with no timeout: cq20b-native.md §9 records the
        meter frame rate itself as unknown, so this client draws no
        conclusion from a quiet UDP socket — only :meth:`_watch_tcp` and
        :meth:`_keepalive` can end a session. A future watchdog on meter
        staleness is one of the things ``docs/protocols/cq20b-native.md``
        now lists as still needing the bench, once §9's rate question is
        answered well enough to choose a threshold that will not cause
        false reconnects.
        """
        while True:
            datagram = await self._udp_queue.get()
            await self._handle_datagram(bytearray(datagram))

    async def _handle_datagram(self, buf: bytearray) -> None:
        for frame in _all_frames(buf):
            levels: dict[str, float]
            if frame.type == _MSG_INPUT_METERS:
                levels = _parse_records(frame.body, INPUT_REFS, INPUT_STRIDE)
            elif frame.type == _MSG_OUTPUT_METERS:
                levels = _parse_records(frame.body, OUTPUT_REFS, OUTPUT_STRIDE)
            else:
                # Type 5 (the mixer's own keep-alive), 10, 23 and 24: not
                # meters this system uses (cq20b-native.md §6, §9) — counted
                # past, never decoded.
                continue
            if not levels:
                continue
            try:
                await self._on_meters(levels)
            except Exception:  # a callback's bug must not break metering
                log.exception("cq native: on_meters callback raised")

    async def _keepalive(self, mixer_udp_port: int) -> None:
        """§2 step 8: a ``KeepAlive`` datagram every :attr:`KEEPALIVE_INTERVAL`
        seconds, sent to the mixer's own UDP port as learned from the
        handshake reply — the **first one sent immediately**, not after the
        first interval. Meters begin arriving at §2 step 7, one step before
        this one, so waiting out a whole interval before this client's own
        first datagram would mean the CQ's meters started before either side
        had exchanged anything since the handshake; sending straight away
        keeps this client's own half of the exchange as prompt as the
        document's own step numbering already implies (see the class
        docstring's "Fixed local port (firewall)" section).
        """
        packet = _encode(_MSG_KEEPALIVE)
        assert self._udp_transport is not None
        peer = (self._host, mixer_udp_port)
        self._udp_transport.sendto(packet, peer)
        while True:
            await self._sleep(self.KEEPALIVE_INTERVAL)
            self._udp_transport.sendto(packet, peer)
