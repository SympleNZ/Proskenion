"""A stub Lenkeng LKV422 for tests (§7.5, §22.2).

Reimplements the command set independently of :mod:`proskenion.core.drivers.lkv422`
— ``PA{n}R``, ``PS{o}{i}R``, ``PAXXR`` -> ``OKP{a}P{b}`` or ``ERR`` — rather
than importing the driver's own parsing, so a bug in the driver's parser
cannot be masked by a stub that shares it (the same reasoning
:mod:`tests.stubs.knxd_stub` gives for its own framing).

**Why the loopback transport, not a pty or a real serial pair.** The driver
talks to this stub over a real transport, not by calling its methods
directly, so the test exercises the actual send/receive boundary. A pseudo
terminal pair (``pty.openpty()``, or ``socat`` making two linked nodes) is
POSIX-only — there is no equivalent built into Windows, and this project's
tests run on Windows as well as Linux (see :mod:`tests.stubs.knxd_stub`'s
same reasoning for TCP over a Unix socket). A real USB-serial loopback needs
hardware. :class:`~proskenion.core.transport.loopback.LoopbackTransport` is
built for exactly this: the driver's ``send``/``receive`` calls go through
the same code path they would over a real port, and the stub plays the
device end through ``peer_send``/``peer_receive`` — in-memory ``asyncio``
queues, identical on every platform this project runs tests on.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from typing import Literal

from proskenion.core.transport.loopback import LoopbackTransport

Terminator = Literal["none", "cr", "lf", "crlf"]

#: Line termination the manual leaves unspecified (§7.5 *Undetermined until
#: bench testing*) — a test picks one to prove the driver's parser copes with
#: any of them.
TERMINATORS: dict[Terminator, bytes] = {
    "none": b"",
    "cr": b"\r",
    "lf": b"\n",
    "crlf": b"\r\n",
}

_PA = re.compile(r"PA(\d)R")
_PS = re.compile(r"PS(\d)(\d)R")

_INPUT_REFS = ("1", "2", "3", "4")
_OUTPUT_REFS = ("1", "2")


class LKV422Stub:
    """Plays the LKV422's device end of an already-open
    :class:`LoopbackTransport`. ``async with LKV422Stub(transport) as stub:``
    starts and stops its read loop."""

    def __init__(
        self,
        transport: LoopbackTransport,
        *,
        terminator: Terminator = "none",
        ack_switches: bool = False,
    ) -> None:
        self._transport = transport
        #: Powers on with everything following input 1, as small matrices do
        #: (matching the stub video matrix's own starting assumption).
        self._routing: dict[str, str] = {"1": "1", "2": "1"}
        self.terminator = terminator
        #: Whether a switch command gets a reply at all — unverified on the
        #: real hardware (§7.5); a test picks a side to prove the driver
        #: copes with either.
        self.ack_switches = ack_switches

        #: Every command received, in arrival order — raw bytes, unparsed.
        self.received: list[bytes] = []
        #: ``time.monotonic()`` alongside each entry in ``received`` — lets a
        #: test that races two exchanges against this stub assert on *when*
        #: each command arrived, not just what arrived, since two PAXXR
        #: queries are byte-identical and only their timing tells them apart.
        self.received_at: list[float] = []

        # -- failure injection -----------------------------------------------
        #: When set, no reply is ever sent — the wiring-fault case (§7.5).
        self.never_reply = False
        #: When set, replaces every reply with these exact bytes instead of
        #: the device's real answer — the "malformed reply" case.
        self.garbage_reply: bytes | None = None
        #: Added before every reply is sent — the "slow reply" case.
        self.reply_delay_s: float = 0.0
        #: When set, a well-formed PAXXR still gets ERR instead of OKPaPb —
        #: for testing how the driver reacts to a device that rejects the
        #: one command it is not supposed to be able to reject.
        self.err_on_paxxr = False
        #: When set, PA/PS switch commands are accepted (replied to, if
        #: ack_switches) but never change the routing — simulates a switch
        #: that silently fails to take effect, so its PAXXR confirmation
        #: still shows the old routing.
        self.accept_switches = True

        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> LKV422Stub:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def routing(self) -> dict[str, str]:
        """The routing as the device itself would report it — for assertions,
        never read by the driver directly."""
        return dict(self._routing)

    async def front_panel(self, output: str, input: str) -> None:  # noqa: A002
        """Simulate someone changing the routing from the front panel or the
        IR remote — entirely outside RS-232 (§7.5 *Out-of-band control*).

        No reply is sent, because a real front-panel or IR change generates
        no traffic on the serial line at all; the only way the driver
        notices is its next ``PAXXR`` poll disagreeing with what it last saw.
        """
        if output not in _OUTPUT_REFS or input not in _INPUT_REFS:
            raise ValueError(f"front_panel({output!r}, {input!r}): not a valid output/input")
        self._routing[output] = input

    # -- device-side protocol, reimplemented from §7.5 / the vendor manual ---

    async def _run(self) -> None:
        buf = bytearray()
        while True:
            chunk = await self._transport.peer_receive(3600)  # blocks for the next write
            buf.extend(chunk)
            # Every command in both syntaxes ends in a literal "R" — that is
            # the frame boundary; nothing this stub is sent contains one
            # mid-command.
            while (end := buf.find(b"R")) != -1:
                raw = bytes(buf[: end + 1])
                del buf[: end + 1]
                await self._handle(raw)

    async def _handle(self, raw: bytes) -> None:
        self.received.append(raw)
        self.received_at.append(time.monotonic())
        if self.never_reply:
            return
        if self.reply_delay_s:
            await asyncio.sleep(self.reply_delay_s)
        if self.garbage_reply is not None:
            await self._reply(self.garbage_reply)
            return

        text = raw.decode("ascii", errors="replace")

        if text == "PAXXR":
            if self.err_on_paxxr:
                await self._reply(b"ERR")
                return
            await self._reply(
                f"OKP{self._routing['1']}P{self._routing['2']}".encode("ascii")
            )
            return

        match = _PA.fullmatch(text)
        if match is not None:
            (n,) = match.groups()
            if n not in _INPUT_REFS:
                await self._reply(b"ERR")
                return
            if self.accept_switches:
                self._routing = {"1": n, "2": n}
            if self.ack_switches:
                await self._reply(b"OK")
            return

        match = _PS.fullmatch(text)
        if match is not None:
            output, n = match.groups()
            if output not in _OUTPUT_REFS or n not in _INPUT_REFS:
                await self._reply(b"ERR")
                return
            if self.accept_switches:
                self._routing[output] = n
            if self.ack_switches:
                await self._reply(b"OK")
            return

        await self._reply(b"ERR")  # §7.5: "A malformed command returns ERR"

    async def _reply(self, body: bytes) -> None:
        await self._transport.peer_send(body + TERMINATORS[self.terminator])
