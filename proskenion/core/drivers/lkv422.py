"""Lenkeng LKV422 HDMI matrix driver over RS-232 (spec §7.5).

The LKV422 is a 4-in/2-out HDMI matrix switched from three places — RS-232,
its front-panel buttons, and a bundled IR remote — of which this driver
speaks only one. Everything here follows §7.5's driver sketch: the
terminator-agnostic reply parser, confirmed (send-settle-verify) switching,
and the connection model that treats a valid ``PAXXR`` reply, not opening the
serial port, as the connection event.

**Manual cross-check.** The Lenkeng LKV422 user manual (from Lenkeng; not
redistributed) was read for this task. Its RS-232 pages (§3 "RS-232 control") match §7.5's quotes
exactly: ``PA{n}R`` for a ganged route, ``PS{o}{i}R`` for one output,
``PAXXR`` returning ``OKP{a}P{b}`` or ``ERR``, 9600 8N1, and the ``PAPXPX``
combined syntax the application does not use. The manual is equally silent
on line termination and on whether a switch command acknowledges — it states
no more than §7.5 already quotes — so both remain genuinely open until the
bench session; nothing here is a guess dressed up as a fact from the manual.

**Serial settings.** 9600 8N1 on ``/dev/hdmi-matrix`` by default
(:data:`TRANSPORT_DEFAULTS`); the path is only a default; the field is a
normal ``device_path`` on the serial transport, so an admin can point it at
``COM3`` for development on Windows without any code change (§5.5 — a driver
never mentions a device path, only its transport's schema does).

**Why the settle time is a config field, not a constant.** §7.5's sketch
hard-codes 250 ms. The behaviour it exists to accommodate — some outputs
still showing the previous source while the switch takes effect — is a
property of the hardware nobody has bench-tested yet, so the number is kept
changeable without a code change (and dropping it to zero is how the test
suite avoids a real sleep on every route).

**Why every exchange is serialised.** The base driver's periodic health
probe and a service-layer ``route()`` call are independent tasks with
nothing else ordering them. A serial line has no request/reply correlation
— a ``PAXXR`` query from one exchange cannot be told apart from another's —
so an interleaved probe can consume a switch's own confirmation reply, or
be consumed by it. One :class:`asyncio.Lock` per driver instance, held for
a switch's whole send-settle-verify sequence and for every standalone
routing read, keeps exactly one exchange on the wire at a time. See
:meth:`LKV422Driver.route` and :meth:`LKV422Driver.read_routing`.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from proskenion.core.drivers.base import Driver, ProbeResult, StatusSink
from proskenion.core.drivers.capabilities import ChannelRef, MatrixCapabilities, MatrixRefs
from proskenion.core.drivers.categories import Category
from proskenion.core.drivers.fields import Field
from proskenion.core.drivers.registry import register
from proskenion.core.transport.base import ConfigurationError, Transport

log = logging.getLogger(__name__)

#: {output_ref: input_ref} — a full routing snapshot, as ``PAXXR`` reports it.
Routing = dict[str, str]

#: Called whenever the driver's known routing changes, with (new, previous).
#: See :meth:`LKV422Driver.add_routing_listener` for why a listener does not
#: need to tell its own route apart from a front-panel one.
RoutingListener = Callable[[Routing, Routing], Awaitable[None]]

DEFAULT_DEVICE_PATH = "/dev/hdmi-matrix"
DEFAULT_SETTLE_MS = 250


class MatrixError(Exception):
    """The LKV422 responded, but not usefully: ``ERR``, a garbled reply, or a
    switch whose confirmation query shows it did not take effect."""


@register
class LKV422Driver(Driver):
    """Lenkeng LKV422 4x2 HDMI matrix over RS-232, 9600 8N1 (§7.5)."""

    key: ClassVar[str] = "lkv422"
    category: ClassVar[Category] = Category.VIDEO_MATRIX
    name: ClassVar[str] = "Lenkeng LKV422 (RS-232)"

    SUPPORTED_TRANSPORTS: ClassVar[list[str]] = ["serial"]
    TRANSPORT_DEFAULTS: ClassVar[dict[str, dict[str, Any]]] = {
        "serial": {
            "device_path": DEFAULT_DEVICE_PATH,
            "baud": 9600,
            "bits": 8,
            "parity": "none",
            "stop": "1",
        }
    }
    CONFIG_SCHEMA: ClassVar[list[Field]] = [
        Field(
            "settle_ms",
            type="int",
            label="Settle time (ms)",
            default=DEFAULT_SETTLE_MS,
            min=0,
            max=5_000,
            help="How long to wait after sending a switch before confirming it with PAXXR",
        ),
    ]

    #: Fixed by the hardware — a 4x2 matrix, never configurable (§7.5).
    INPUT_REFS: ClassVar[tuple[str, ...]] = ("1", "2", "3", "4")
    OUTPUT_REFS: ClassVar[tuple[str, ...]] = ("1", "2")

    #: Matches a valid status reply or a rejection; terminator-agnostic — it
    #: never anchors to the start or end of the buffer (§7.5 *Undetermined
    #: until bench testing*).
    RESPONSE: ClassVar[re.Pattern[bytes]] = re.compile(rb"OKP(\d)P(\d)|ERR")

    #: How often the routing poll runs — §11.1 gives 30 s, tightened here to
    #: keep §18's 30-second bound on a front-panel change reaching the
    #: interface. The base :class:`~proskenion.core.drivers.base.Driver`
    #: loop sleeps *after* each probe rather than before it, so at a literal
    #: 30 s the worst case (one interval, plus the poll's own PAXXR round
    #: trip, plus a switch that happens to be settling) lands a little past
    #: the 30-second bound rather than under it. 25 s restores the margin
    #: without a code change beyond this one constant.
    PROBE_INTERVAL: ClassVar[float] = 25.0

    #: How long ``PAXXR`` is given to reply — "wait up to 1 second" (§7.5).
    READ_TIMEOUT: ClassVar[float] = 1.0
    #: A gap this long with nothing further arriving is taken as the end of a
    #: reply, whatever the terminator turns out to be. Deliberately much
    #: shorter than READ_TIMEOUT: it only needs to be longer than the byte
    #: gaps within one reply, not as long as the round trip to the device.
    QUIET_PERIOD_S: ClassVar[float] = 0.1
    #: How long to wait, once, for bytes already sitting unread before a
    #: fresh request — see :meth:`_drain`.
    DRAIN_TIMEOUT_S: ClassVar[float] = 0.02

    def __init__(
        self,
        device_id: int,
        transport: Transport,
        driver_config: dict[str, Any],
        status_sink: StatusSink,
    ) -> None:
        super().__init__(device_id, transport, driver_config, status_sink)
        self._routing: Routing = {}
        self._listeners: list[RoutingListener] = []
        #: Serialises every exchange on the serial line. The base driver runs
        #: the health probe from its own periodic task while the service
        #: layer calls ``route()`` independently — nothing else orders them.
        #: Without this, a probe's ``PAXXR`` can land while a switch is
        #: settling: ``_drain()`` can swallow the switch's own confirmation
        #: reply, or the confirmation can be read from whichever exchange's
        #: reply happens to arrive first, misreporting a good switch as
        #: unconfirmed or confirming it from state that was never requested.
        #: See :meth:`route` and :meth:`read_routing` for how it is held.
        self._lock = asyncio.Lock()

    # -- §5.3 contract: the connection model is §7.5's, not the base default -

    async def connect(self) -> None:
        """Open the serial port. §7.5: this proves nothing about the device —
        opening a serial node succeeds whether or not anything answers at the
        other end. A failure here is a configuration or permissions problem,
        reported with the two causes a person at commissioning should check,
        distinctly from the wiring failure :meth:`probe` reports."""
        try:
            await super().connect()
        except ConfigurationError as exc:
            path = getattr(self.transport, "config", {}).get("device_path", DEFAULT_DEVICE_PATH)
            raise ConfigurationError(
                f"cannot open the HDMI matrix serial port ({exc}). This is a "
                "configuration or permissions problem, not a wiring fault: "
                f"either {path} does not exist (the cable is unplugged, or "
                "the udev rule did not match — §4.12) or the application "
                "user is not a member of the 'dialout' group."
            ) from exc

    async def probe(self) -> ProbeResult:
        """A valid ``PAXXR`` reply is the connection event (§7.5), not the
        open. Also the health probe: the routing poll — every
        :data:`PROBE_INTERVAL` (25 s, not §11.1's literal 30 — see that
        constant) — is this same call, run periodically by
        :meth:`~Driver.probe_periodically`, so liveness and routing detection
        are one round trip, not two."""
        try:
            routing = await self.read_routing()
        except MatrixError as exc:
            return ProbeResult(False, str(exc))
        return ProbeResult(True, f"routing {routing}")

    def capabilities(self) -> MatrixCapabilities:
        return MatrixCapabilities(input_count=4, output_count=2, supports_atomic_route=True)

    # -- VideoMatrixDriver (§7.5) ---------------------------------------------

    async def route(self, outputs: list[str], input: str) -> None:  # noqa: A002 - §7.5 signature
        """Point every ref in ``outputs`` at ``input``: one ``PA{n}R`` given
        every output, sequential ``PS{o}{i}R`` for a subset — the encoding
        choice lives here, never with the caller (§5.5 single-intent rule).

        Never waits for an acknowledgement on the switch itself, because
        whether the LKV422 sends one is unverified (§7.5). Instead: send,
        settle, then confirm with the same ``PAXXR`` the health probe uses.
        A confirmation that does not show the requested routing raises
        :class:`MatrixError` rather than reporting success it cannot back up.

        The whole send-settle-verify sequence holds :attr:`_lock`, so the
        health probe (or any other caller of :meth:`read_routing`) cannot run
        a query in the middle of it — see :meth:`__init__`.
        """
        if input not in self.INPUT_REFS:
            raise ValueError(f"LKV422 inputs are {self.INPUT_REFS}, got {input!r}")
        unknown = [o for o in outputs if o not in self.OUTPUT_REFS]
        if unknown:
            raise ValueError(f"LKV422 outputs are {self.OUTPUT_REFS}, got {', '.join(unknown)}")

        async with self._lock:
            if set(outputs) == set(self.OUTPUT_REFS):
                await self._send(f"PA{input}R")  # atomic — both outputs in one command
            else:
                for output in outputs:  # partial group: sequential writes
                    await self._send(f"PS{output}{input}R")

            await self._sleep(self._settle_s)
            routing = await self._read_routing_locked()

        unconfirmed = [o for o in outputs if routing.get(o) != input]
        if unconfirmed:
            raise MatrixError(
                f"route of output(s) {', '.join(unconfirmed)} to input {input} "
                f"not confirmed by PAXXR: now {routing}"
            )

    async def read_routing(self) -> Routing:
        """Query ``PAXXR`` and parse the reply, holding :attr:`_lock` for the
        exchange so it cannot land in the middle of a switch's settle-then-
        verify sequence (see :meth:`route`). Every caller — the health
        probe, a switch's own confirmation, and anyone reading routing
        directly — ends up at :meth:`_read_routing_locked`, so the routing
        poll and the divergence-notifying side effect there never run twice
        for the same round trip (§7.5 *Out-of-band control*)."""
        async with self._lock:
            return await self._read_routing_locked()

    async def _read_routing_locked(self) -> Routing:
        """The actual ``PAXXR`` exchange. Callers must already hold
        :attr:`_lock` — :meth:`route` calls this directly (it is already
        inside the lock for its own send-settle-verify sequence) so that
        acquiring the lock here cannot deadlock against itself; every other
        caller goes through the public, lock-acquiring :meth:`read_routing`.
        """
        reply = await self._request("PAXXR")
        match = self.RESPONSE.search(reply)
        if match is not None and match.group(1) is not None:
            routing: Routing = {"1": match.group(1).decode(), "2": match.group(2).decode()}
            await self._notify_routing(routing)
            return routing
        if not reply:
            raise MatrixError(
                f"no reply to PAXXR within {self.READ_TIMEOUT:g} s — a wiring "
                "problem: TX and RX not crossed, no common ground, or a "
                "signalling-level mismatch (§7.5), not a configuration fault"
            )
        if match is not None:  # matched ERR
            raise MatrixError(f"LKV422 returned ERR for PAXXR, a well-formed query: {reply!r}")
        raise MatrixError(f"unexpected reply to PAXXR: {reply!r}")

    def available_refs(self) -> MatrixRefs:
        """Refs are opaque to the core; labels populate admin pickers only
        (B59). The two configured inputs today are side of stage and back of
        house (§7.5), but that mapping is configuration, not this driver's
        business — it only enumerates what the hardware has."""
        return MatrixRefs(
            inputs=[
                ChannelRef(ref=ref, label=f"Input {ref}", kind="input", stereo=False)
                for ref in self.INPUT_REFS
            ],
            outputs=[
                ChannelRef(ref=ref, label=f"Output {ref}", kind="output", stereo=False)
                for ref in self.OUTPUT_REFS
            ],
        )

    # -- out-of-band detection (§7.5 *Out-of-band control*, *Destinations*) --

    def add_routing_listener(self, listener: RoutingListener) -> None:
        """Register ``listener`` to be awaited with ``(new, previous)``
        whenever the routing this driver has last seen changes — from the
        routing poll (:data:`PROBE_INTERVAL`), or from a route this driver
        made itself.

        **A listener does not need to tell the two apart.** Both notify with
        the LKV422's own reported state, not with what a caller asked for, so
        applying ``new`` is idempotent whichever caused it: when this driver
        made the change, the caller already learned the outcome from
        ``route()``'s return (or its raised :class:`MatrixError`), so the
        notification only repeats what it already knows; when a front panel
        or the IR remote made it, the notification is the only way anyone
        finds out. Either way, "set routing to what PAXXR just reported" is
        the correct reaction. This is the same reasoning §7.5's *Destinations*
        section gives for divergence detection: "Because PAXXR returns real
        device state, this detects out-of-band changes as well as its own."

        A listener that raises is logged and isolated — it never stops
        another listener running or reaches :meth:`read_routing`'s caller,
        matching the isolation §5.6 requires of the event bus, even though
        this hook is a simpler, driver-local one.
        """
        self._listeners.append(listener)

    async def _notify_routing(self, routing: Routing) -> None:
        if routing == self._routing:
            return
        previous, self._routing = self._routing, dict(routing)
        for listener in list(self._listeners):
            try:
                await listener(dict(routing), previous)
            except Exception:
                log.exception("routing listener raised for device %s", self.device_id)

    # -- wire I/O --------------------------------------------------------------

    @property
    def _settle_s(self) -> float:
        return float(self.config.get("settle_ms", DEFAULT_SETTLE_MS)) / 1000

    async def _send(self, command: str) -> None:
        """Fire-and-forget: no read follows. A switch's own acknowledgement,
        if the device turns out to send one, is left unread and drained
        before the next request (see :meth:`_drain`) rather than parsed."""
        await self.transport.send(command.encode("ascii"))

    async def _request(self, command: str) -> bytes:
        await self._drain()
        await self.transport.send(command.encode("ascii"))
        return await self._read_reply()

    async def _drain(self) -> None:
        """Discard bytes already queued before sending a new request.

        The one thing that can be sitting there is an unread acknowledgement
        from a previous switch (§7.5: the driver never waits for one). Without
        this, those bytes would sit ahead of the next reply in the same
        stream and could be read as part of it. Discarding them first, rather
        than trying to recognise and skip them inline, keeps the reply parser
        simple: whatever it sees next belongs to the request it just sent.
        """
        while True:
            try:
                await self.transport.receive(self.DRAIN_TIMEOUT_S)
            except TimeoutError:
                return

    async def _read_reply(self) -> bytes:
        """Accumulate bytes until :data:`RESPONSE` matches or a quiet period
        passes — terminator-agnostic, exactly as §7.5 specifies, because
        whether replies end in CR, LF, CRLF or nothing is unverified."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.READ_TIMEOUT
        buf = bytearray()
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return bytes(buf)
            # Before any bytes arrive, wait for the whole remaining budget —
            # "no reply yet" is not "quiet after a reply started". Once bytes
            # have arrived, a much shorter gap means the reply is finished.
            wait = min(self.QUIET_PERIOD_S, remaining) if buf else remaining
            try:
                chunk = await self.transport.receive(wait)
            except TimeoutError:
                if buf:
                    return bytes(buf)
                continue
            buf.extend(chunk)
            # Line noise ahead of a reply is not a reply: the real unit sends
            # a stray NUL after a power cycle, which on its own would end the
            # read at the quiet period and be reported as an unexpected
            # reply. Only *leading* NULs go; anything else is kept, so a
            # genuinely wrong reply still reaches the caller to be rejected.
            while buf and buf[0] == 0:
                del buf[0]
            if self.RESPONSE.search(bytes(buf)):
                return bytes(buf)
