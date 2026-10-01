"""PJLink Class 1 projector driver (spec §7.4, §5.5, §5.3, §6.10, §11.1).

Registered as ``projector/pjlink``. Drives the Panasonic PT-EZ570E installed
in the auditorium (and its PT-VW360 bench stand-in) — any PJLink Class 1
projector, in principle, since Class 1 is a fixed command set §7.4 does not
extend.

**One TCP connection per command.** This mirrors the controller this driver
replaces (``docs/hardware/legacy-controller.md``): open TCP 4352, read the
greeting, prefix the MD5 challenge-response digest when the projector asks
for one, send exactly one command terminated with CR, read one reply, close.
Nothing here keeps a connection open between commands or between probes —
:meth:`PJLinkDriver.connect` and :meth:`PJLinkDriver.probe` each open and
close their own, and so does every call to :meth:`PJLinkDriver.set_power`,
:meth:`PJLinkDriver.set_input` and :meth:`PJLinkDriver.read_state`. This
departs from most of this codebase's TCP drivers, which hold one connection
open for the life of ``maintain()``; it is deliberate, is the pattern already
proven against the real device, and keeps every PJLink exchange self
contained — a dropped connection never leaves the driver's next command
guessing at stale server-side state.

**Every exchange is serialised through one lock per driver instance**
(:attr:`PJLinkDriver._lock`). The health probe runs from the base driver's
own periodic task while ``set_power``, ``set_input`` and ``read_state`` are
called by the service layer; without the lock, two overlapping calls would
each open their own connection to a projector that — believed but not yet
confirmed on the bench, see ``docs/protocols/pjlink.md`` — accepts only one
PJLink connection at a time and refuses or resets a second. The lock covers
the whole of :meth:`PJLinkDriver._run_command`: open, greeting, authenticate,
send, read, close.

**Authentication** (§7.4). The greeting is either ``PJLINK 0`` (no
authentication) or ``PJLINK 1 <nonce>`` (authenticate with the MD5 of the
nonce concatenated with the password, sent as hex before the command). Three
cases, verbatim from §7.4:

- required, no password configured — :data:`MSG_AUTH_NONE_CONFIGURED`
- required, wrong password — :data:`MSG_AUTH_WRONG_PASSWORD`
- not required, a password is configured anyway — ignored; the connection
  proceeds without a digest

After :data:`AUTH_FAILURE_THRESHOLD` consecutive authentication failures the
run loop's backoff holds at a fixed :data:`AUTH_HOLD_SECONDS` instead of
continuing to double (see :meth:`PJLinkDriver._backoff`) — a wrong or missing
password needs a human in the device settings, not a socket retrying ever
more often against a wall.

**The state model** (§7.4) is :class:`~proskenion.core.drivers.capabilities.ProjectorState`.
``%1POWR ?`` answers ``0``/``1``/``2``/``3`` for off/on/cooling/warming; a
network-level failure (refused, timed out, dropped mid-reply, or an
authentication problem the driver cannot resolve) is ``unreachable``; a
projector-reported fault (``ERR1``, ``ERR2`` or ``ERR4`` in reply to
``%1POWR ?``) is ``error``. ``ERR3`` — "unavailable time" — only ever appears
in reply to a *command* (``%1POWR <n>``/``%1INPT <n>``), never to a read, and
means the projector is warming or cooling and refused it; :meth:`set_power`
and :meth:`set_input` raise :class:`ProjectorBusyError` carrying the state
that caused the refusal rather than waiting or retrying (§7.4, B52) — the
driver never queues a command.

**Capabilities are resolved at connect** (§5.5): the input list comes from
``%1INST ?``, each entry PJLink's own two-character code (e.g. ``"31"``),
opaque to the core.

**Health and boot** (§7.4, §11.1): ``connect()`` discovers the projector's
current power state and its input list and never sends a power command — the
projector may be mid-cycle from before a restart, and discovering beats
overwriting. The periodic probe cadence depends on the discovered state; see
:meth:`PJLinkDriver.maintain`.

**State listeners.** :meth:`PJLinkDriver.add_state_listener` registers an
async callback invoked as ``callback(new, previous)`` exactly when the
driver's own view of :class:`ProjectorState` changes — never on a repeat read
of the same value. This is the one place the projector is polled; nothing
downstream should set up a second poll of its own.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, ClassVar

from proskenion.core.drivers.base import Driver, ProbeResult, StatusSink
from proskenion.core.drivers.capabilities import ProjectorCapabilities, ProjectorState
from proskenion.core.drivers.categories import Category
from proskenion.core.drivers.fields import Field
from proskenion.core.drivers.registry import register
from proskenion.core.transport.base import ConfigurationError, Transport, TransportClosed

log = logging.getLogger(__name__)

#: PJLink's registered TCP port (§7.4).
PJLINK_PORT = 4352

_ENCODING = "ascii"
_CR = "\r"

#: §7.4's exact operator-facing messages for the two authentication failures.
MSG_AUTH_NONE_CONFIGURED = "Authentication required — set the password in device settings"
MSG_AUTH_WRONG_PASSWORD = "Authentication failed — check the password."

#: docs/protocols/pjlink.md §8, bench-confirmed 21 September 2026: the
#: PT-EZ570E accepts exactly one PJLink connection, and a second client's TCP
#: connect neither completes nor is refused — it simply hangs until
#: ``TcpTransport.CONNECT_TIMEOUT`` (10 s) gives up. That is a genuinely
#: different situation from a wrong address or an unplugged unit, which fail
#: fast, so this message is shown amber rather than red (see ``connect_busy``).
MSG_BUSY = "Busy — another controller may be connected."

#: After this many consecutive authentication failures, the run loop's
#: backoff holds at :data:`AUTH_HOLD_SECONDS` instead of doubling (§7.4).
AUTH_FAILURE_THRESHOLD = 3
AUTH_HOLD_SECONDS = 60.0

#: ``%1POWR ?``'s four success values, verbatim from §7.4.
_POWER_STATES: dict[str, ProjectorState] = {
    "0": ProjectorState.OFF,
    "1": ProjectorState.ON,
    "2": ProjectorState.COOLING,
    "3": ProjectorState.WARMING,
}

#: A command-level error a device can answer with (§7.4, the PJLink Class 1
#: specification). ``ERR3`` is handled separately — see :class:`ProjectorBusyError`.
_ERROR_CODES = frozenset(("ERR1", "ERR2", "ERR3", "ERR4"))

StateListener = Callable[[ProjectorState, ProjectorState], Awaitable[None]]


class PJLinkError(Exception):
    """Base of every error this driver raises for a protocol-level problem —
    never for a plain transport failure, which is left as
    :class:`~proskenion.core.transport.base.TransportClosed` or ``OSError``
    for the §5.3 run loop to classify on its own."""


class PJLinkAuthenticationError(PJLinkError):
    """Authentication could not be completed (§7.4): no password is
    configured for a projector that requires one, or the configured password
    was refused (``PJLINK ERRA``). :attr:`message` is the exact operator
    wording §7.4 gives for whichever case applied."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class PJLinkCommandError(PJLinkError):
    """The projector answered a command with ``ERR1`` (undefined command),
    ``ERR2`` (out of parameter) or ``ERR4`` (projector/display failure) —
    anything other than the ``ERR3`` busy case, which raises
    :class:`ProjectorBusyError` instead."""

    def __init__(self, code: str, command: str) -> None:
        super().__init__(f"{command} -> {code}")
        self.code = code
        self.command = command


class ProjectorBusyError(PJLinkError):
    """``set_power`` or ``set_input`` was refused with ``ERR3`` because the
    projector is warming or cooling (§7.4, B52). Carries :attr:`state` — the
    driver's last discovered state, which caused the refusal — so the
    service layer can answer ``device_unavailable`` with it instead of
    queuing or retrying the command, which this driver never does."""

    def __init__(self, state: ProjectorState) -> None:
        super().__init__(f"the projector is {state.value} and rejected the command")
        self.state = state


@dataclass(frozen=True)
class _Greeting:
    auth_required: bool
    nonce: str | None


def _digest(nonce: str, password: str) -> str:
    """The hex MD5 of ``nonce + password`` (§7.4). MD5 here is PJLink's own
    challenge-response scheme, not a security control of this application's —
    ``usedforsecurity=False`` records that rather than disputes it."""
    return hashlib.md5((nonce + password).encode(_ENCODING), usedforsecurity=False).hexdigest()


def _parse_reply(command: str, reply: str) -> str:
    """The value after ``=`` in a reply to ``command`` — ``"0"`` from
    ``"%1POWR=0"``, or an ``ERRx`` code. ``command`` may carry a parameter
    (``"%1POWR 1"``); only the verb before the first space is matched,
    exactly as the reply never repeats the parameter.
    """
    verb = command.split(" ", 1)[0]
    prefix = f"{verb}="
    if not reply.startswith(prefix):
        raise PJLinkError(f"unexpected reply to {command!r}: {reply!r}")
    return reply[len(prefix) :]


def _parse_inst(value: str) -> tuple[str, ...]:
    """``"11 21 31"`` -> ``("11", "21", "31")`` — PJLink's own two-character
    input codes (§5.5), opaque to the core and never decoded here."""
    return tuple(value.split())


@register
class PJLinkDriver(Driver):
    """PJLink Class 1 over TCP (§7.4). Registered as ``projector/pjlink``."""

    key: ClassVar[str] = "pjlink"
    category: ClassVar[Category] = Category.PROJECTOR
    name: ClassVar[str] = "PJLink (Class 1)"

    SUPPORTED_TRANSPORTS: ClassVar[list[str]] = ["tcp"]
    TRANSPORT_DEFAULTS: ClassVar[dict[str, dict[str, Any]]] = {"tcp": {"port": PJLINK_PORT}}
    CONFIG_SCHEMA: ClassVar[list[Field]] = [
        Field(
            "password",
            type="password",
            label="Password",
            required=False,
            encrypted=True,
            help="Leave blank if the projector has no PJLink password set (§6.10).",
        ),
        # Read by the projector service, not by this driver: from a successful
        # power-on until this long has passed the service shows the projector
        # as warming and refuses power-off, even once PJLink reports "on" —
        # the lamp is still warming then (§7.4, B52; owner decision
        # 2026-09-30). 0 disables the hold.
        Field(
            "min_warmup_s",
            type="int",
            label="Minimum warm-up (seconds)",
            required=False,
            default=60,
            min=0,
            max=600,
            help=(
                "The projector may report 'on' before its lamp is fully warm. "
                "Power-off is refused until this long after power-on."
            ),
        ),
    ]

    #: One command's connect-and-reply budget. Real PJLink devices answer in
    #: well under a second on a LAN; generous but short enough that a test
    #: connection (§7.4) does not sit out a much longer external timeout.
    COMMAND_TIMEOUT: ClassVar[float] = 2.0

    #: Probe cadence by discovered state (§7.4, §11.1) — see :meth:`maintain`.
    ON_INTERVAL: ClassVar[float] = 30.0
    OFF_INTERVAL: ClassVar[float] = 300.0
    TRANSITION_INTERVAL: ClassVar[float] = 5.0
    ERROR_INTERVAL: ClassVar[float] = 30.0

    def __init__(
        self,
        device_id: int,
        transport: Transport,
        driver_config: dict[str, Any],
        status_sink: StatusSink,
    ) -> None:
        super().__init__(device_id, transport, driver_config, status_sink)
        #: Nothing is known before the first successful discovery.
        self._state: ProjectorState = ProjectorState.UNREACHABLE
        #: Declared maximum before ``connect()`` (B56): no inputs are
        #: knowable yet, but this driver always knows how to authenticate.
        self._capabilities = ProjectorCapabilities(inputs=(), supports_authentication=True)
        #: Set by ``connect()`` when discovery could not complete, so the
        #: run loop's very next ``probe()`` reports it without a second,
        #: equally doomed connection (§7.4 "rather than hammering").
        self._pending_error: str | None = None
        self._consecutive_auth_failures = 0
        #: True from the most recent connect timeout believed to mean the
        #: projector's one slot is held elsewhere, cleared by the next
        #: successful exchange (see ``_note_success``). Read by
        #: :class:`~proskenion.core.devices.DeviceManager`, duck-typed exactly
        #: as ``auth_holding`` (§7.4 bench, docs/protocols/pjlink.md §8).
        self._connect_busy = False
        self._listeners: list[StateListener] = []
        # Resolved on a state change while maintain() waits out the current
        # state's cadence, so it stops waiting: see _wait_for_next_probe.
        self._state_changed: asyncio.Future[None] | None = None
        #: Serialises every exchange (§7.4). The health probe runs from the
        #: base driver's own periodic task while ``set_power``, ``set_input``
        #: and ``read_state`` are called by the service layer — without this,
        #: two calls overlapping would each open their own connection to a
        #: device that, per the bench, accepts only one at a time (see
        #: ``docs/protocols/pjlink.md``), and the loser would look like a
        #: transient failure or feed the authentication-failure counter for
        #: no real authentication reason. Held for the whole of
        #: :meth:`_run_command` — connect, greeting, command, reply, close —
        #: never just part of it.
        self._lock = asyncio.Lock()

    # -- capabilities and state listeners ------------------------------------

    def capabilities(self) -> ProjectorCapabilities:
        return self._capabilities

    def current_state(self) -> ProjectorState:
        """The last-known :class:`ProjectorState`, with no I/O.

        For a caller — the projector service — that registers
        :meth:`add_state_listener` after ``connect()`` has already run and
        discovered the state, which the listener alone would miss: it fires
        only on a *change*, and the discovery happened before anything could
        be listening. Never triggers a probe; this is exactly what the last
        successful ``connect()``, ``probe()`` or on-demand read last found.
        """
        return self._state

    @property
    def auth_holding(self) -> bool:
        """True once §7.4's 60 s authentication hold has begun.

        Read by :class:`~proskenion.core.devices.DeviceManager` to show amber
        rather than red for the whole of the hold, even though no connection
        has ever succeeded (the ``degraded`` mapping otherwise requires one).
        Duck-typed exactly as :attr:`Driver.latency_ms`-style optional
        attributes already are in ``proskenion/core/devices.py``: a driver
        with nothing to report about this simply lacks the attribute, so no
        other driver's colour changes.
        """
        return self._consecutive_auth_failures >= AUTH_FAILURE_THRESHOLD

    @property
    def connect_busy(self) -> bool:
        """True while the latest failure is a connect timeout read as "the
        projector's one slot is held elsewhere" rather than offline
        (docs/protocols/pjlink.md §8). Read by
        :class:`~proskenion.core.devices.DeviceManager` exactly as it reads
        ``auth_holding`` and the CQ-20B's ``amber_failure``: shown amber
        whatever ``connected_once`` says, and — because amber is not red — it
        never starts a device-red alert (``DeviceRedAlertMonitor`` only
        watches ``status == "error"``).
        """
        return self._connect_busy

    def _note_success(self) -> None:
        """Call after any exchange completes normally: clears both failure
        counters this driver keeps between exchanges."""
        self._consecutive_auth_failures = 0
        self._connect_busy = False

    def add_state_listener(self, callback: StateListener) -> None:
        """Register ``callback(new, previous)``, awaited once per actual
        change to the driver's view of :class:`ProjectorState` (never on a
        repeated read of the same value). This is the projector's only poll;
        a caller that wants to notice a transition subscribes here instead
        of calling :meth:`read_state` on a timer of its own.
        """
        self._listeners.append(callback)

    async def _update_state(self, new: ProjectorState) -> None:
        previous, self._state = self._state, new
        if previous is new:
            return
        changed = self._state_changed
        if changed is not None and not changed.done():
            changed.set_result(None)
        for listener in list(self._listeners):
            try:
                await listener(new, previous)
            except Exception:  # a listener's bug must not break polling
                log.exception("device %s: state listener raised", self.device_id)

    # -- one connection per command -------------------------------------------

    def _password(self) -> str | None:
        value = self.config.get("password")
        return value if isinstance(value, str) and value else None

    async def _read_line(self) -> str:
        """One CR- or LF-terminated line (§7.4 uses CR; a stray LF is
        tolerated rather than fought over)."""
        buf = bytearray()
        while b"\r" not in buf and b"\n" not in buf:
            buf.extend(await self.transport.receive(self.COMMAND_TIMEOUT))
        cr, lf = buf.find(b"\r"), buf.find(b"\n")
        end = min(i for i in (cr, lf) if i != -1)
        return bytes(buf[:end]).decode(_ENCODING, errors="replace")

    async def _read_greeting(self) -> _Greeting:
        line = await self._read_line()
        if not line.startswith("PJLINK"):
            raise PJLinkError(f"not a PJLink greeting: {line!r}")
        rest = line[len("PJLINK") :].strip()
        if rest.startswith("0"):
            return _Greeting(False, None)
        if rest.startswith("1"):
            nonce = rest[1:].strip()
            if not nonce:
                raise PJLinkError(f"authentication greeting with no nonce: {line!r}")
            return _Greeting(True, nonce)
        raise PJLinkError(f"unrecognised greeting: {line!r}")

    async def _run_command(self, command: str) -> str:
        """Open one TCP connection, authenticate if asked, send ``command``,
        return its one-line reply, then close — the venue's proven pattern
        (module docstring). Raises :class:`PJLinkAuthenticationError` for
        both authentication mismatch cases (§7.4); anything the transport
        itself raises (``TransportClosed``, ``OSError``, ``TimeoutError``)
        propagates for the caller to classify.

        Held under :attr:`_lock` for its entire duration. The health probe
        runs from the base driver's own periodic task while ``set_power``,
        ``set_input`` and ``read_state`` are called by the service layer;
        without this, two such calls overlapping would each open their own
        connection at once — and the venue's projector is believed to accept
        only one at a time (unconfirmed; see ``docs/protocols/pjlink.md``),
        so the loser would either fail outright or, worse, look exactly like
        a wrong password and feed :attr:`_consecutive_auth_failures` for no
        real authentication reason. ``self.transport`` is also one shared
        object reused across every call (open, then close, never held open
        between calls); without the lock, two concurrent callers could race
        on it directly, independent of anything the projector itself does.
        """
        async with self._lock:
            await self.transport.open()
            try:
                greeting = await self._read_greeting()
                prefix = ""
                if greeting.auth_required:
                    password = self._password()
                    if not password:
                        raise PJLinkAuthenticationError(MSG_AUTH_NONE_CONFIGURED)
                    assert greeting.nonce is not None
                    prefix = _digest(greeting.nonce, password)
                await self.transport.send(f"{prefix}{command}{_CR}".encode(_ENCODING))
                reply = await self._read_line()
                if reply == "PJLINK ERRA":
                    raise PJLinkAuthenticationError(MSG_AUTH_WRONG_PASSWORD)
                return reply
            finally:
                await self.transport.close()

    # -- §5.3 contract --------------------------------------------------------

    async def connect(self) -> None:
        """Discover the current state and the input list; send no power
        command (§7.4). A genuine network failure in the very first exchange
        (refused, no route, a name that will not resolve) still raises
        :class:`~proskenion.core.transport.base.ConfigurationError` from the
        transport, exactly like any other TCP driver — that happens inside
        :meth:`_run_command` before this method gets a chance to intervene,
        and is left to propagate: the run loop reports it ``config``-kind,
        red.

        A **timeout** is different — the bench (docs/protocols/pjlink.md §8)
        found the projector accepts exactly one PJLink client, and a second
        one is not refused: either its TCP connect itself never completes
        (:class:`~proskenion.core.transport.base.ConfigurationError` whose
        ``__cause__`` is :class:`TimeoutError`), or it completes and the
        greeting simply never arrives (a bare :class:`TimeoutError` from
        :meth:`_read_greeting`). Either shape reads as "busy", not "offline",
        and is caught below and recorded on ``self._pending_error`` instead
        of raised — the same way authentication and a dropped second command
        already are: the run loop only expects ``connect()`` to raise for a
        config-kind failure, and this is deliberately not one. ``probe()``,
        called immediately afterwards, reports it.
        """
        self._pending_error = None
        try:
            reply = await self._run_command("%1POWR ?")
        except PJLinkAuthenticationError as exc:
            self._pending_error = exc.message
            self._consecutive_auth_failures += 1
            await self._update_state(ProjectorState.UNREACHABLE)
            return
        except ConfigurationError as exc:
            if not isinstance(exc.__cause__, TimeoutError):
                raise  # refused, unreachable, unresolved — genuinely offline (§5.3)
            self._note_busy(exc)
            return
        except TimeoutError as exc:
            self._note_busy(exc)  # connected, but no greeting arrived in time
            return
        except (TransportClosed, OSError) as exc:
            self._pending_error = str(exc) or type(exc).__name__
            await self._update_state(ProjectorState.UNREACHABLE)
            return
        self._note_success()
        await self._apply_power_reply("%1POWR ?", reply)

        try:
            inst_reply = await self._run_command("%1INST ?")
        except PJLinkAuthenticationError as exc:
            # Vanishingly unlikely straight after the check above succeeded,
            # but handled the same way rather than assumed impossible.
            self._pending_error = exc.message
            self._consecutive_auth_failures += 1
            return
        except ConfigurationError as exc:
            if not isinstance(exc.__cause__, TimeoutError):
                raise
            self._note_busy(exc)
            return
        except TimeoutError as exc:
            self._note_busy(exc)
            return
        except (TransportClosed, OSError) as exc:
            self._pending_error = str(exc) or type(exc).__name__
            return
        self._note_success()
        value = _parse_reply("%1INST ?", inst_reply)
        inputs = () if value in _ERROR_CODES else _parse_inst(value)
        if value in _ERROR_CODES:
            log.warning("device %s: %%1INST ? -> %s", self.device_id, value)
        self._capabilities = ProjectorCapabilities(inputs=inputs, supports_authentication=True)

    def _note_busy(self, exc: BaseException) -> None:
        """Record a connect-or-greeting timeout as "busy", not "offline"
        (docs/protocols/pjlink.md §8): sets :attr:`connect_busy`, which
        :class:`~proskenion.core.devices.DeviceManager` reads exactly as it
        reads ``auth_holding``, and leaves :attr:`_state` — the last
        power-state reading — untouched, since a busy slot tells us nothing
        about it either way.
        """
        log.info(
            "device %s: PJLink read as busy, not offline (%s)", self.device_id, exc
        )
        self._pending_error = MSG_BUSY
        self._connect_busy = True

    async def probe(self) -> ProbeResult:
        """§11.1: ``%1POWR ?`` is the liveness indicator. Any well-formed
        reply — even one reporting a projector fault — proves the
        connection works; the state it carries is applied either way.

        A device that connected fine once can still find its one PJLink slot
        held elsewhere on a *later* probe — the legacy controller at
        ``10.2.30.250`` polls the same projector until it is retired — so the
        busy classification :meth:`connect` applies is repeated here rather
        than assumed to matter only on the first connection
        (docs/protocols/pjlink.md §8).
        """
        if self._pending_error is not None:
            return ProbeResult(False, self._pending_error)
        try:
            reply = await self._run_command("%1POWR ?")
        except PJLinkAuthenticationError as exc:
            self._consecutive_auth_failures += 1
            await self._update_state(ProjectorState.UNREACHABLE)
            return ProbeResult(False, exc.message)
        except ConfigurationError as exc:
            if not isinstance(exc.__cause__, TimeoutError):
                # Unexpected this deep into a connection that was working,
                # but treated the same as any other lost connection rather
                # than left to crash the run loop (§5.3): device-kind, red.
                self._connect_busy = False
                await self._update_state(ProjectorState.UNREACHABLE)
                return ProbeResult(False, str(exc))
            self._note_busy(exc)
            return ProbeResult(False, MSG_BUSY)
        except TimeoutError as exc:
            self._note_busy(exc)
            return ProbeResult(False, MSG_BUSY)
        except (TransportClosed, OSError) as exc:
            self._connect_busy = False
            await self._update_state(ProjectorState.UNREACHABLE)
            return ProbeResult(False, str(exc) or type(exc).__name__)
        self._note_success()
        await self._apply_power_reply("%1POWR ?", reply)
        return ProbeResult(True, self._state.value)

    async def maintain(self) -> None:
        """Probe at the cadence §7.4/§11.1 give for the current state,
        rather than :class:`~proskenion.core.drivers.base.Driver`'s single
        fixed interval:

        - **on** — 30 s (§7.4, §11.1, verbatim).
        - **off** — 5 min (§7.4, §11.1, verbatim).
        - **warming**/**cooling** — 5 s. Not in the specification: a lamp
          projector typically warms or cools in under two minutes, and §7.4
          wants a rule to fire "the moment the projector settles" once the
          transition ends. 5 s keeps that moment close to real without
          hammering a projector that is busy anyway — a handful of read-only
          status queries over a warm-up is not a meaningful load.
        - **error** — 30 s, the same as *on*: a faulted projector still needs
          its recovery noticed promptly, not on the idle cadence.

        *unreachable* is never the state while this method is running — the
        run loop only calls ``maintain()`` after a successful ``probe()``,
        which by definition just set some other state — so it has no cadence
        of its own here; while genuinely unreachable, the interval that
        matters is the run loop's own exponential backoff (§5.3), capped at
        300 s, which already slows down exactly when hammering an unreachable
        device would help least.

        Two consecutive probe failures return, exactly as the base class.
        """
        failures = 0
        while True:
            await self._wait_for_next_probe()
            result = await self._safe_probe()
            if result.alive:
                failures = 0
                continue
            failures += 1
            log.info(
                "device %s probe failed (%d): %s", self.device_id, failures, result.detail
            )
            if failures >= 2:
                return

    async def _wait_for_next_probe(self) -> None:
        """Wait the cadence for the current state, cut short when the state changes.

        The interval is chosen when the wait begins, so without this a projector
        that was off when the wait began would be probed again only after the
        five-minute off cadence, even once a command had started it warming:
        the interface would show *warming* long after it was on, and a scene's
        input action would be refused as still warming (§8.13). A change,
        whether from a command's own read or anything else, ends the wait at
        once, and the next wait uses the new state's cadence.

        A change noticed while no wait is in progress is not carried into the
        next one: that wait's interval is already the new state's.

        The change is a plain future rather than a task waiting on an event,
        so a projector idling between probes holds one helper task, the
        sleep, not two (§23.3).
        """
        changed: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._state_changed = changed
        sleeper = asyncio.ensure_future(self._sleep(self._interval_for(self._state)))
        try:
            await asyncio.wait({sleeper, changed}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self._state_changed = None
            sleeper.cancel()
            changed.cancel()
            await asyncio.gather(sleeper, return_exceptions=True)

    def _interval_for(self, state: ProjectorState) -> float:
        if state is ProjectorState.ON:
            return self.ON_INTERVAL
        if state is ProjectorState.OFF:
            return self.OFF_INTERVAL
        if state in (ProjectorState.WARMING, ProjectorState.COOLING):
            return self.TRANSITION_INTERVAL
        return self.ERROR_INTERVAL  # ERROR, and UNREACHABLE as a defensive fallback

    async def _backoff(self) -> None:
        """The normal §5.3 exponential backoff, except after
        :data:`AUTH_FAILURE_THRESHOLD` consecutive authentication failures,
        which instead holds at a fixed :data:`AUTH_HOLD_SECONDS` (§7.4). A
        wrong or missing password does not get better by trying again sooner
        — every attempt from the fourth on tells the operator nothing the
        third did not, and there is a human who has to visit device settings
        regardless of how often the socket knocks in the meantime.
        """
        if self._consecutive_auth_failures >= AUTH_FAILURE_THRESHOLD:
            await self._sleep(AUTH_HOLD_SECONDS)
            return
        await super()._backoff()

    # -- ProjectorDriver (§5.5) -----------------------------------------------

    async def _apply_power_reply(self, command: str, reply: str) -> None:
        value = _parse_reply(command, reply)
        mapped = _POWER_STATES.get(value)
        if mapped is not None:
            await self._update_state(mapped)
            return
        if value in _ERROR_CODES:
            log.warning("device %s: %s -> %s", self.device_id, command, value)
            await self._update_state(ProjectorState.ERROR)
            return
        raise PJLinkError(f"unrecognised reply to {command!r}: {reply!r}")

    async def _write_command(self, verb: str, param: str) -> None:
        """One ``verb param`` command (§7.4). Never queued, never retried
        (B52): a single connection, a single attempt, and either it succeeds
        or it raises. A connect timeout read as busy (§8 of
        docs/protocols/pjlink.md) raises :class:`ProjectorBusyError` — the
        same thing an operator sees for ``ERR3`` — rather than the raw
        transport error, since "another controller has the slot" is the same
        fact for the caller either way.
        """
        try:
            reply = await self._run_command(f"{verb} {param}")
        except PJLinkAuthenticationError:
            self._consecutive_auth_failures += 1
            await self._update_state(ProjectorState.UNREACHABLE)
            raise
        except ConfigurationError as exc:
            if not isinstance(exc.__cause__, TimeoutError):
                raise
            self._connect_busy = True
            raise ProjectorBusyError(self._state) from exc
        except TimeoutError as exc:
            self._connect_busy = True
            raise ProjectorBusyError(self._state) from exc
        self._note_success()
        value = _parse_reply(verb, reply)
        if value == "OK":
            return
        if value == "ERR3":
            raise ProjectorBusyError(self._state)
        if value in _ERROR_CODES:  # ERR1, ERR2, ERR4
            raise PJLinkCommandError(value, f"{verb} {param}")
        raise PJLinkError(f"unrecognised reply to {verb} {param!r}: {reply!r}")

    async def set_power(self, on: bool) -> None:
        await self._write_command("%1POWR", "1" if on else "0")

    async def set_input(self, input_ref: str) -> None:
        await self._write_command("%1INPT", input_ref)

    async def read_input(self) -> str:
        """The projector's current input reference (``%1INPT ?``), read on
        demand (§7.4). Not sent by :meth:`connect`, :meth:`probe` or
        :meth:`maintain` — this driver's poll is the power state alone; the
        service layer calls this after a state change to ``on`` and after
        :meth:`set_input`, never on a timer of its own.
        """
        reply = await self._run_command("%1INPT ?")
        value = _parse_reply("%1INPT ?", reply)
        if value in _ERROR_CODES:
            raise PJLinkCommandError(value, "%1INPT ?")
        return value

    async def read_state(self) -> ProjectorState:
        """A fresh, on-demand query (§5.5) — one more one-shot connection,
        not a second poll loop. A caller that wants to notice every future
        change should use :meth:`add_state_listener` instead of calling this
        on a timer; the driver already polls once, at the cadence
        :meth:`maintain` chooses.
        """
        try:
            reply = await self._run_command("%1POWR ?")
        except PJLinkAuthenticationError:
            self._consecutive_auth_failures += 1
            await self._update_state(ProjectorState.UNREACHABLE)
            return self._state
        except ConfigurationError as exc:
            if not isinstance(exc.__cause__, TimeoutError):
                self._connect_busy = False
                await self._update_state(ProjectorState.UNREACHABLE)
                return self._state
            self._connect_busy = True
            return self._state  # last-known state stands; we simply could not refresh it
        except TimeoutError:
            self._connect_busy = True
            return self._state  # last-known state stands; we simply could not refresh it
        except (TransportClosed, OSError):
            self._connect_busy = False
            await self._update_state(ProjectorState.UNREACHABLE)
            return self._state
        self._note_success()
        await self._apply_power_reply("%1POWR ?", reply)
        return self._state
