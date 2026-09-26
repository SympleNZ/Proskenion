"""The WebSocket broadcaster (spec §16.8, §5.6).

Every live value the operator's tablet and the booth screen show arrives
through here. The broadcaster owns the set of open connections; the endpoint
in :mod:`proskenion.api.ws` owns one socket each and does nothing but move
bytes between a socket and its :class:`Connection`.

Two classes of traffic, and the distinction is the whole design (§5.6)
------------------------------------------------------------------------
*Continuous* — levels, fader positions, observed values, meters. Accumulated
into the state store's dirty set and drained by one ticking task into
**one message per domain** at 10–15 fps, carrying only what changed. Never a
message per channel. A tick with nothing dirty sends nothing. On a full
outbound queue the oldest continuous message is dropped and counted: the
newest value is the truth and a stale one is worthless.

*Discrete* — device status, banners, the show timer, scene results, projector
and HDMI transitions. Sent the moment they happen and **never dropped**. A
connection whose queue is full of discrete messages is closed and the fact
logged, rather than blocking the whole server: a client that cannot keep up
is a broken client, and stalling every other viewer for it is worse.

Backgrounded connections (§16.8)
--------------------------------
``{"type": "background"}`` drops a connection to discrete events only — a
suspended tablet still has meters composed, batched and queued for it
otherwise, and metering is the most expensive thing on the wire. A later
``resync`` restores it and returns a full snapshot. Meters are deliberately
**not** in a snapshot: a stale meter is worse than none.

Per-tier filtering (§6.12)
--------------------------
A connection is tagged with its tier at the upgrade, and the tier decides
what it receives. Staff receive everything. A hirer receives only the
``mixer``, ``lighting``, ``status`` and ``devices`` domains
(:data:`HIRER_DOMAINS`; a subscribe naming another is silently narrowed),
and within them only what the permission snapshot in ``state.hirer`` lets
them reach — see :func:`filter_for_hirer`. The snapshot is read live on
every frame and every resync, never from the token (B31), so a channel
removed from a page stops arriving on the next broadcast. Any other tier
receives nothing. Both halves of the filter stay injectable for tests.

Receiving a domain is not writing it: :meth:`Broadcaster.may_write` is a
separate check, made per target. Staff may write what they see. A hirer may
write exactly what the permission snapshot lets them reach and write —
:func:`~proskenion.core.hirer_enforcement.hirer_may_write`, read live from
``state.hirer`` on every ``set`` — and the lighting master never. How far a
permitted write may go (a mixer ceiling) is the write handler's business.

Timestamps on the wire
----------------------
``timer.started_at`` is ISO 8601 with offset (§4.9), never a running count of
elapsed milliseconds — the client computes elapsed from ``started_at`` and
``accumulated_ms`` (§21.7). ``mixer_meters.at`` is the exception and stays a
monotonic float, because §5.5 defines it for staleness rather than as a
wall-clock time.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from proskenion.core.auth import STAFF_TIERS
from proskenion.core.bus import EventBus, OverflowClass, Subscription
from proskenion.core.events import (
    DeviceStatusChanged,
    LampsChanged,
    SystemBannerChanged,
    TimerChanged,
    VideoSourceChanged,
)
from proskenion.core.hirer_enforcement import hirer_may_write
from proskenion.core.hirer_permissions import HirerPermissions
from proskenion.core.state import (
    LightingDomain,
    MixerDomain,
    ScenesDomain,
    StateStore,
    SystemDomain,
    TimerDomain,
)

log = logging.getLogger(__name__)

Message = dict[str, Any]
"""One frame on the wire, as JSON-serialisable Python."""

#: Frames per second for batched continuous state (§16.8 fixes 10–15).
MIN_FPS: Final = 10.0
MAX_FPS: Final = 15.0
DEFAULT_FPS: Final = 12.0

#: Messages one connection may have outstanding before the §5.6 policy applies.
DEFAULT_QUEUE_SIZE: Final = 64

#: The application owns liveness because §4.13 removes nginx's read timeout.
DEFAULT_PING_INTERVAL_S: Final = 20.0

#: The longest a socket waits before comparing the clock with its absolute
#: expiry again. A socket normally sleeps straight to ``aexp``; the cap only
#: bounds how late a close can be after the clock is stepped (§4.9).
DEFAULT_EXPIRY_CHECK_S: Final = 60.0

#: Domains a client may name in ``subscribe`` and ``resync`` (§16.8).
#: ``status`` (Phase 5 contracts, "Button lamps") carries the button-lamp
#: frame — discrete, not batched; see :func:`status_message`,
#: :data:`MESSAGE_DOMAIN` and :meth:`Broadcaster._snapshot_domain`.
BROADCAST_DOMAINS: Final[frozenset[str]] = frozenset(
    {"lighting", "mixer", "devices", "timer", "projector", "hdmi", "scenes", "system", "status"}
)

#: Domains whose changes travel as batched continuous frames. Everything else
#: reaches clients as a discrete message the moment it happens, so its dirty
#: keys are drained and discarded by the tick.
#:
#: ``scenes`` carries only "what is running right now" (``state.scenes``'s
#: ``running`` and ``last_result``) — the ``scene_started``/``scene_completed``
#: discrete messages still report "this just happened" and are unaffected.
#: Without this, a tablet that opens a websocket mid-run has no way to learn a
#: scene is executing until the next lifecycle event fires.
BATCHED_DOMAINS: Final[frozenset[str]] = frozenset({"lighting", "mixer", "scenes"})

#: The domains a hirer may receive (phase-5 contracts, WebSocket): their
#: mixer and lighting controls, their buttons' lamps, and device status for
#: the offline banner. Never ``system``, ``timer``, ``scenes``, ``projector``
#: or ``hdmi``. Within these, :func:`filter_for_hirer` narrows every frame.
HIRER_DOMAINS: Final[frozenset[str]] = frozenset({"mixer", "lighting", "status", "devices"})

#: The tier a hirer's token carries.
HIRER_TIER: Final = "hirer"

#: ``set.domain`` values §16.8 declares. Phase 1 implements no handler for any
#: of them; :class:`proskenion.api.ws.WriteRouter` answers ``not_found``.
SETTABLE_DOMAINS: Final[frozenset[str]] = frozenset(
    {"lighting", "lighting_group", "master", "mixer"}
)

#: Which state domain a ``set`` writes, for the per-tier check.
WRITE_DOMAIN_STATE: Final[Mapping[str, str]] = {
    "lighting": "lighting",
    "lighting_group": "lighting",
    "master": "lighting",
    "mixer": "mixer",
}

#: ``source`` on a batched frame. §16.8 shows ``"fade"`` on a lighting frame
#: and ``"sync"`` on a mixer one but never enumerates the vocabulary; later
#: phases carry the true cause of a change through to here.
DEFAULT_SOURCE: Final[Mapping[str, str]] = {"lighting": "fade", "mixer": "sync"}
RESYNC_SOURCE: Final = "resync"


# -- message builders ---------------------------------------------------------------
#
# One function per §16.8 message. The discrete shapes are declared here even
# though Phase 1 emits only three of them, so a later phase adds a *producer*
# rather than inventing the protocol a second time.


def device_status_message(device: str, status: str) -> Message:
    return {"type": "device_status", "device": device, "status": status}


def banner_message(level: str, key: str, text: str | None) -> Message:
    """A banner raised, changed or cleared (``text`` is ``None`` when cleared)."""
    return {"type": "banner", "level": level, "key": key, "text": text}


def timer_message(running: bool, started_at: str | None, accumulated_ms: int) -> Message:
    """The shared show timer (§21.7).

    ``started_at`` is ISO 8601 with offset (§4.9). Elapsed time is never on
    the wire: the client computes it from these two fields, so two clients
    agree without either trusting the other's clock drift.
    """
    return {
        "type": "timer",
        "running": running,
        "started_at": started_at,
        "accumulated_ms": accumulated_ms,
    }


def scene_started_message(scene_id: int, triggered_by: str) -> Message:
    return {"type": "scene_started", "scene_id": scene_id, "triggered_by": triggered_by}


def scene_completed_message(scene_id: int, result: str) -> Message:
    return {"type": "scene_completed", "scene_id": scene_id, "result": result}


def projector_state_message(state: str, input_ref: str | None) -> Message:
    """§16.5, §16.8: sent on every change of ``state`` or ``input_ref`` alike."""
    return {"type": "projector_state", "state": state, "input_ref": input_ref}


def hdmi_source_message(destination_id: int, input_id: int | None, diverged: bool) -> Message:
    """One destination's effective source (§16.8, as corrected).

    Sent per destination on every routing change, whether it came from this
    application (``POST /hdmi/destinations/{id}/source``) or from the front
    panel or IR remote (§7.5 *Out-of-band control*) — the client cannot tell
    the two apart from this message and does not need to.
    """
    return {
        "type": "hdmi_source",
        "destination_id": destination_id,
        "input_id": input_id,
        "diverged": diverged,
    }


def external_control_message(state: str, source: str) -> Message:
    return {"type": "external_control", "state": state, "source": source}


def surface_bank_message(bank_id: int, name: str) -> Message:
    return {"type": "surface_bank", "bank_id": bank_id, "name": name}


def surface_touch_message(strip: int, touched: bool) -> Message:
    return {"type": "surface_touch", "strip": strip, "touched": touched}


def progress_message(operation: str, step: int, of: int, message: str) -> Message:
    """Live output for a long operation, so the UI shows steps not a spinner."""
    return {"type": "progress", "operation": operation, "step": step, "of": of, "message": message}


def status_message(lamps: Mapping[str, Mapping[str, object]]) -> Message:
    """The button-lamp frame (Phase 5 contracts, "Button lamps"): discrete,
    sent on change, and carrying every lamp on a resync. ``lamps`` maps a
    derived-status id (string — a button's ``state_id``) to ``{"on": bool |
    None, "transitioning": bool}``. :func:`filter_for_hirer` narrows it to
    ``lamp_ids`` for a hirer connection."""
    return {"type": "status", "lamps": {k: dict(v) for k, v in lamps.items()}}


#: The state domain each discrete message belongs to, for subscription and
#: per-tier filtering. A client that did not subscribe to a domain is not sent
#: its discrete events either.
MESSAGE_DOMAIN: Final[Mapping[str, str]] = {
    "device_status": "devices",
    "banner": "system",
    "progress": "system",
    "timer": "timer",
    "scene_started": "scenes",
    "scene_completed": "scenes",
    "projector_state": "projector",
    "hdmi_source": "hdmi",
    "external_control": "lighting",
    "surface_bank": "mixer",
    "surface_touch": "mixer",
    "status": "status",
    # Rides the devices domain purely for delivery (both tiers always
    # subscribe to it), the same way external_control rides lighting — its
    # own "type" is what the client keys on (Phase 5 contracts, "Additions").
    "pages_changed": "devices",
}


# -- per-tier filtering (§6.12) ------------------------------------------------------

DomainVisibility = Callable[["Connection", str], bool]
"""``(connection, domain) -> may this connection receive this domain``."""

FrameFilter = Callable[["Connection", str, "Message"], "Message | None"]
"""``(connection, domain, message) -> what this connection is sent``; ``None`` is nothing."""


def default_visibility(connection: Connection, domain: str) -> bool:
    """Staff see every domain; a hirer the :data:`HIRER_DOMAINS`; anyone else none."""
    if connection.tier in STAFF_TIERS:
        return True
    if connection.tier == HIRER_TIER:
        return domain in HIRER_DOMAINS
    return False


WriteCheck = Callable[["Connection", str, "int | None"], bool]
"""``(connection, set domain, target id) -> may this connection write it``."""


#: Hirer frames that carry no channel, group or lamp id and pass unchanged.
_HIRER_UNFILTERED: Final[frozenset[str]] = frozenset({"device_status"})


def filter_for_hirer(message: Message, permissions: HirerPermissions) -> Message | None:
    """``message`` as a hirer with ``permissions`` may see it (phase-5 contracts).

    An allow-list: a message type not handled here is never sent to a hirer,
    and within a handled one only the sections named are carried.

    - ``mixer_state``: reachable channels only; ``main`` only when Main is
      reachable, and ``null`` in a resync otherwise.
    - ``mixer_meters``: reachable channels only, with ``metering`` as for staff.
    - ``lighting_state``: nothing while lighting is off (Q3). Otherwise
      reachable channels' levels and colour, and ``observed`` for them; the
      multipliers of reachable groups and of every group a reachable channel
      belongs to; and ``master`` and ``external_control`` — all read-only, so
      the ghost mark is computed exactly as for staff (§21.9). Stage-bank
      ``bindings`` are keyed by rule and never sent.
    - ``external_control``: while lighting is on.
    - ``status``: only the lamps in ``lamp_ids``.
    - ``device_status``: unchanged — the client decides which devices affect
      the hirer's controls (§21.15).
    - ``pages_changed``: rewritten to the hirer's own assigned page ids,
      never the published (staff, every page) list (Phase 5 contracts,
      "Additions, 2026-09-19").

    A partial frame left with nothing the hirer may see is dropped rather than
    sent empty; a resync (``source == "resync"``) keeps its sections, empty.
    """
    kind = message.get("type")
    if kind in _HIRER_UNFILTERED:
        return message
    if kind == "mixer_state":
        return _hirer_mixer_state(message, permissions)
    if kind == "mixer_meters":
        return _hirer_mixer_meters(message, permissions)
    if kind == "lighting_state":
        if not permissions.lighting_enabled:
            return None
        return _hirer_lighting_state(message, permissions)
    if kind == "external_control":
        return message if permissions.lighting_enabled else None
    if kind == "status":
        return _hirer_status(message, permissions)
    if kind == "pages_changed":
        return {"type": "pages_changed", "page_ids": sorted(permissions.pages)}
    return None


def _keep_ids(values: object, allowed: Callable[[int], bool]) -> dict[str, Any]:
    """The items of an id-keyed section whose id ``allowed`` accepts."""
    if not isinstance(values, Mapping):
        return {}
    kept: dict[str, Any] = {}
    for key, value in values.items():
        item_id = _as_id(key)
        if item_id is not None and allowed(item_id):
            kept[str(key)] = value
    return kept


def _as_id(key: object) -> int | None:
    if isinstance(key, int) and not isinstance(key, bool):
        return key
    if isinstance(key, str) and key.isdigit():
        return int(key)
    return None


def _finish(frame: Message, message: Message, *, resync: bool) -> Message | None:
    """Carry ``source`` over, or drop a partial frame with nothing in it."""
    if len(frame) == 1 and not resync:
        return None
    if "source" in message:
        frame["source"] = message["source"]
    return frame


def _hirer_mixer_state(message: Message, permissions: HirerPermissions) -> Message | None:
    resync = message.get("source") == RESYNC_SOURCE
    frame: Message = {"type": "mixer_state"}
    if "main" in message:
        if permissions.main_reachable:
            frame["main"] = message["main"]
        elif resync:
            frame["main"] = None
    for section in ("outputs", "inputs"):
        if section in message:
            kept = _keep_ids(message[section], permissions.mixer_reachable)
            if kept or resync:
                frame[section] = kept
    return _finish(frame, message, resync=resync)


def _hirer_mixer_meters(message: Message, permissions: HirerPermissions) -> Message | None:
    channels = _keep_ids(message.get("channels"), permissions.mixer_reachable)
    if not channels and "metering" not in message:
        return None
    frame: Message = {"type": "mixer_meters", "channels": channels, "at": message.get("at")}
    if "metering" in message:
        frame["metering"] = message["metering"]
    return frame


def _hirer_lighting_state(message: Message, permissions: HirerPermissions) -> Message | None:
    resync = message.get("source") == RESYNC_SOURCE
    frame: Message = {"type": "lighting_state"}
    if "channels" in message:
        channels = _keep_ids(message["channels"], permissions.lighting_reachable)
        if channels or resync:
            frame["channels"] = channels
    if "groups" in message:
        groups = _keep_ids(message["groups"], permissions.multiplier_groups.__contains__)
        if groups or resync:
            frame["groups"] = groups
    for section in ("master", "external_control"):
        if section in message:
            frame[section] = message[section]
    if "observed" in message:
        observed = _keep_ids(message["observed"], permissions.lighting_reachable)
        frame["observed"] = observed or None
    return _finish(frame, message, resync=resync)


def _hirer_status(message: Message, permissions: HirerPermissions) -> Message | None:
    """``status`` (the button-lamp frame): only the lamps in ``lamp_ids``.

    A change to lamps the hirer may not see is dropped; a frame that arrived
    with no lamps at all (a resync of none) is passed on, empty.
    """
    raw = message.get("lamps")
    lamps = _keep_ids(raw, permissions.lamp_ids.__contains__)
    if not lamps and isinstance(raw, Mapping) and raw:
        return None
    return {"type": "status", "lamps": lamps}


# -- one connection ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Queued:
    """One outbound message and the overflow class that governs it."""

    message: Message
    cls: OverflowClass


@dataclass(frozen=True, slots=True)
class ConnectionHealth:
    """One connection's row on the health screen (§11.2)."""

    id: int
    tier: str
    domains: tuple[str, ...]
    backgrounded: bool
    queued: int
    drops: int
    """Continuous messages dropped for this connection since it opened."""
    discrete_overflows: int
    """Times a discrete message found no room — each one closed the socket."""
    closed: bool


#: Close codes the broadcaster asks the endpoint to use.
CLOSE_SLOW_CLIENT: Final = 1008
CLOSE_GOING_AWAY: Final = 1001
#: The session reached its absolute expiry (``aexp``), for any tier. The
#: client sends staff to the login screen and a hirer to the PIN screen.
CLOSE_SESSION_EXPIRED: Final = 4002
#: Hirer access was revoked — disabled, or the PIN changed. The client shows
#: "Access updated" with no login prompt (§21.8).
CLOSE_ACCESS_REVOKED: Final = 4003


class Connection:
    """One open socket's fan-out state: tier, subscription, queue and counters.

    Nothing here touches the socket. The endpoint drains :meth:`messages` and
    writes them; the broadcaster only ever enqueues.
    """

    def __init__(
        self,
        connection_id: int,
        *,
        tier: str,
        session_id: str | None = None,
        address: str | None = None,
        queue_size: int = DEFAULT_QUEUE_SIZE,
    ) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be at least 1")
        self.id = connection_id
        self.tier = tier
        self.session_id = session_id
        #: The real client address at the upgrade (§4.13), for the audit rows
        #: written when the connection is closed on the server's initiative.
        self.address = address
        self.queue_size = queue_size
        self.domains: frozenset[str] = frozenset()
        self.backgrounded = False
        self.drops = 0
        self.discrete_overflows = 0
        self.close_code: int | None = None
        self.close_reason: str | None = None
        #: Monotonic time of the last ``pong``; the endpoint's liveness task
        #: reads it. Pongs are never session activity (§6.4, B65).
        self.last_pong = 0.0
        self._queue: deque[Queued] = deque()
        self._wake = asyncio.Event()
        self._closed = False

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"<Connection {self.id} tier={self.tier} domains={sorted(self.domains)}>"

    # -- subscription ------------------------------------------------------

    def set_domains(self, domains: Iterable[str]) -> frozenset[str]:
        """Replace the subscription. Returns what is now subscribed."""
        self.domains = frozenset(domains)
        return self.domains

    def background(self) -> None:
        """Discrete events only, until a ``resync`` (§16.8). Not a logout."""
        self.backgrounded = True

    def foreground(self) -> None:
        self.backgrounded = False

    def wants(self, domain: str, cls: OverflowClass) -> bool:
        """Whether this connection should be sent a ``cls`` message for ``domain``."""
        if self._closed or domain not in self.domains:
            return False
        return not (cls == "continuous" and self.backgrounded)

    # -- outbound queue (§5.6 overflow policy) ------------------------------

    def send(self, message: Message, *, cls: OverflowClass = "discrete") -> bool:
        """Queue ``message``. Returns ``False`` if it was dropped or refused.

        Full queue, continuous message: the oldest continuous message is
        dropped and counted. Full queue, discrete message: the oldest
        continuous message makes room, and if there is none the connection is
        closed — a discrete event is never dropped, and blocking every other
        viewer for one slow client is worse than losing that client.
        """
        if self._closed:
            return False
        if len(self._queue) >= self.queue_size:
            if self._evict_oldest_continuous():
                self.drops += 1
            elif cls == "continuous":
                self.drops += 1
                return False
            else:
                self.discrete_overflows += 1
                log.warning(
                    "websocket outbound queue full of discrete messages; closing the connection",
                    extra={
                        "connection": self.id,
                        "tier": self.tier,
                        "queue_size": self.queue_size,
                        "message_type": message.get("type"),
                    },
                )
                self.close("the client is not keeping up", code=CLOSE_SLOW_CLIENT)
                return False
        self._queue.append(Queued(message, cls))
        self._wake.set()
        return True

    def _evict_oldest_continuous(self) -> bool:
        for index, queued in enumerate(self._queue):
            if queued.cls == "continuous":
                del self._queue[index]
                return True
        return False

    async def messages(self) -> AsyncIterator[Message]:
        """Yield queued messages, ending once the connection is closed and drained."""
        while True:
            if self._queue:
                yield self._queue.popleft().message
                continue
            if self._closed:
                return
            self._wake.clear()
            if self._queue or self._closed:
                continue
            await self._wake.wait()

    # -- lifecycle ---------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def queued(self) -> int:
        return len(self._queue)

    def close(self, reason: str, *, code: int = CLOSE_GOING_AWAY) -> None:
        """Mark the connection finished; the endpoint closes the socket."""
        if self._closed:
            return
        self._closed = True
        self.close_code = code
        self.close_reason = reason
        self._wake.set()

    def health(self) -> ConnectionHealth:
        return ConnectionHealth(
            id=self.id,
            tier=self.tier,
            domains=tuple(sorted(self.domains)),
            backgrounded=self.backgrounded,
            queued=len(self._queue),
            drops=self.drops,
            discrete_overflows=self.discrete_overflows,
            closed=self._closed,
        )


# -- the broadcaster -----------------------------------------------------------------


class Broadcaster:
    """Owns the connections, the tick that batches state, and the discrete fan-out.

    One instance per process, created in the application's lifespan. Traffic
    never extends a session (§6.4, B65). The endpoint closes a socket at its
    token's absolute expiry and never at the idle one, re-reading the clock
    every ``expiry_check_s`` so a clock step is noticed; that cadence is set
    here beside the ping interval because both are liveness timing.
    """

    def __init__(
        self,
        state: StateStore,
        bus: EventBus,
        *,
        fps: float = DEFAULT_FPS,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        ping_interval_s: float = DEFAULT_PING_INTERVAL_S,
        expiry_check_s: float = DEFAULT_EXPIRY_CHECK_S,
        visibility: DomainVisibility = default_visibility,
        restrict: FrameFilter | None = None,
        writable: WriteCheck | None = None,
        subscriber_prefix: str = "broadcast",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if fps <= 0:
            raise ValueError("fps must be positive")
        if ping_interval_s <= 0:
            raise ValueError("ping_interval_s must be positive")
        if expiry_check_s <= 0:
            raise ValueError("expiry_check_s must be positive")
        if not MIN_FPS <= fps <= MAX_FPS:
            # Not an error: tests tick faster so they do not sleep. §16.8
            # fixes the production rate, and the default sits inside it.
            log.debug("broadcast tick outside the §16.8 range", extra={"fps": fps})
        self._state = state
        self._bus = bus
        self._fps = fps
        self._queue_size = queue_size
        self._ping_interval = ping_interval_s
        self._expiry_check = expiry_check_s
        self._visibility = visibility
        self._restrict_frame = restrict if restrict is not None else self._live_filter
        self._writable = writable if writable is not None else self._live_writable
        self._prefix = subscriber_prefix
        self._clock = clock
        self._connections: dict[int, Connection] = {}
        self._next_id = 0
        self._task: asyncio.Task[None] | None = None
        self._subscriptions: list[Subscription] = []

    # -- accessors ---------------------------------------------------------

    @property
    def state(self) -> StateStore:
        return self._state

    @property
    def bus(self) -> EventBus:
        return self._bus

    @property
    def ping_interval(self) -> float:
        return self._ping_interval

    @property
    def expiry_check(self) -> float:
        """The longest a socket sleeps before re-reading the clock against ``aexp``."""
        return self._expiry_check

    def now(self) -> float:
        """The broadcaster's monotonic clock, which the liveness task shares."""
        return self._clock()

    @property
    def interval(self) -> float:
        """Seconds between batched frames."""
        return 1.0 / self._fps

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    def connections(self) -> tuple[Connection, ...]:
        return tuple(self._connections.values())

    def health(self) -> tuple[ConnectionHealth, ...]:
        """Per-connection rows for the health screen, drop counts included."""
        return tuple(c.health() for c in self._connections.values())

    def visible(self, connection: Connection, domain: str) -> bool:
        """Whether ``connection``'s tier may see ``domain`` at all (§6.12)."""
        return self._visibility(connection, domain)

    def may_write(self, connection: Connection, set_domain: str, target_id: int | None) -> bool:
        """Whether ``connection`` may ``set`` ``target_id`` in ``set_domain`` (§16.8).

        Never wider than :meth:`visible`: a tier that may not see the state
        domain a ``set`` writes may certainly not write it (§21.2, §6.7). An
        unknown ``set`` domain is refused.
        """
        state_domain = WRITE_DOMAIN_STATE.get(set_domain)
        if state_domain is None or not self._visibility(connection, state_domain):
            return False
        return self._writable(connection, set_domain, target_id)

    def _live_writable(
        self, connection: Connection, set_domain: str, target_id: int | None
    ) -> bool:
        """The default write check: staff yes; a hirer per ``state.hirer``; else no.

        The snapshot is read here, on every ``set``, so a channel removed from
        a page is refused from the next write with no re-login (§6.7, B31).
        """
        if connection.tier in STAFF_TIERS:
            return True
        if connection.tier == HIRER_TIER:
            return hirer_may_write(self._state.hirer.permissions, set_domain, target_id)
        return False

    # -- connections -------------------------------------------------------

    def connect(
        self,
        *,
        tier: str,
        session_id: str | None = None,
        address: str | None = None,
        domains: Iterable[str] = (),
    ) -> Connection:
        """Register a newly accepted socket. The endpoint owns the socket itself."""
        self._next_id += 1
        connection = Connection(
            self._next_id,
            tier=tier,
            session_id=session_id,
            address=address,
            queue_size=self._queue_size,
        )
        connection.last_pong = self._clock()
        self._connections[connection.id] = connection
        self.subscribe(connection, domains)
        log.info("websocket connected", extra={"connection": connection.id, "tier": tier})
        return connection

    def disconnect(self, connection: Connection) -> None:
        """Remove a connection from the fan-out. Safe to call twice."""
        if self._connections.pop(connection.id, None) is None:
            return
        connection.close("disconnected")
        log.info(
            "websocket disconnected",
            extra={
                "connection": connection.id,
                "tier": connection.tier,
                "drops": connection.drops,
                "reason": connection.close_reason,
            },
        )

    def subscribe(self, connection: Connection, domains: Iterable[str]) -> frozenset[str]:
        """Set ``connection``'s subscription, dropping unknown and forbidden domains.

        Filtering here as well as at delivery is deliberate: the subscription
        a health screen shows is then the truth about what the client
        receives, and a tier can never see a domain it has no business
        seeing even if a later filter is loosened by mistake.
        """
        wanted = [d for d in domains if isinstance(d, str)]
        accepted = frozenset(
            d for d in wanted if d in BROADCAST_DOMAINS and self._visibility(connection, d)
        )
        refused = sorted(set(wanted) - accepted)
        if refused:
            log.info(
                "websocket subscription narrowed",
                extra={"connection": connection.id, "tier": connection.tier, "refused": refused},
            )
        return connection.set_domains(accepted)

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Subscribe to the discrete events and start the batching tick."""
        if self._task is not None:
            return
        self._subscriptions = [
            self._bus.subscribe(
                DeviceStatusChanged,
                self._on_device_status,
                name=f"{self._prefix}.device_status",
                cls="discrete",
            ),
            self._bus.subscribe(
                SystemBannerChanged,
                self._on_banner,
                name=f"{self._prefix}.banner",
                cls="discrete",
            ),
            self._bus.subscribe(
                TimerChanged,
                self._on_timer,
                name=f"{self._prefix}.timer",
                cls="discrete",
            ),
            self._bus.subscribe(
                VideoSourceChanged,
                self._on_video_source_changed,
                name=f"{self._prefix}.video_source",
                cls="discrete",
            ),
            self._bus.subscribe(
                LampsChanged,
                self._on_lamps_changed,
                name=f"{self._prefix}.lamps",
                cls="discrete",
            ),
        ]
        self._task = asyncio.get_running_loop().create_task(self._run(), name="broadcast-tick")

    async def stop(self) -> None:
        """Stop ticking, unsubscribe, and close every connection."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        for subscription in self._subscriptions:
            self._bus.unsubscribe(subscription)
        self._subscriptions = []
        for connection in list(self._connections.values()):
            connection.close("server shutting down")
        self._connections.clear()

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                self.tick()
            except Exception:  # pragma: no cover - defensive
                # The tick must outlive one bad frame; a broadcaster that
                # died would take every live value on the wire with it.
                log.exception("broadcast tick failed")

    # -- continuous: one batched frame per domain (§16.8) -------------------

    def tick(self) -> int:
        """Drain the dirty set and broadcast one frame per changed domain.

        Returns the number of messages built. A tick with nothing dirty
        sends nothing and costs one dictionary lookup.
        """
        dirty = self._state.take_dirty()
        if not dirty:
            return 0
        sent = 0
        for name, keys in dirty.items():
            # Domains outside BATCHED_DOMAINS already reached clients as
            # discrete messages the moment they changed; draining their keys
            # here is what stops the dirty set growing without limit.
            if name not in BATCHED_DOMAINS:
                continue
            for message, cls in self._frames(name, keys):
                self._deliver(message, domain=name, cls=cls)
                sent += 1
        return sent

    def _frames(self, name: str, keys: set[str] | None) -> list[tuple[Message, OverflowClass]]:
        """The messages for one domain: ``keys`` is ``None`` for a full snapshot."""
        domain = self._state.domain(name)
        source = RESYNC_SOURCE if keys is None else DEFAULT_SOURCE.get(name, "state")
        built: list[tuple[Message, OverflowClass]] = []
        if name == "lighting":
            assert isinstance(domain, LightingDomain)
            frame = _lighting_frame(domain, keys, source=source)
            if frame is not None:
                built.append((frame, "continuous"))
        elif name == "mixer":
            assert isinstance(domain, MixerDomain)
            frame = _mixer_frame(domain, keys, source=source)
            if frame is not None:
                built.append((frame, "continuous"))
            # Meters travel in their own message and are never replayed on
            # resync — a stale meter is worse than none (§16.8, B58).
            if keys is not None:
                meters = _meter_frame(domain, keys, at=time.monotonic())
                if meters is not None:
                    built.append((meters, "continuous"))
        elif name == "scenes":
            assert isinstance(domain, ScenesDomain)
            frame = _scenes_frame(domain, keys, source=source)
            if frame is not None:
                built.append((frame, "continuous"))
        return built

    # -- discrete: immediately, never dropped -------------------------------

    def publish(
        self, message: Message, *, domain: str | None = None, cls: OverflowClass = "discrete"
    ) -> int:
        """Send one message now to every connection entitled to it.

        ``domain`` defaults to the one :data:`MESSAGE_DOMAIN` gives for the
        message type. Returns the number of connections it was queued for.
        """
        name = domain or MESSAGE_DOMAIN.get(str(message.get("type")))
        if name is None:
            raise ValueError(f"no state domain for message type {message.get('type')!r}")
        return self._deliver(message, domain=name, cls=cls)

    def _deliver(self, message: Message, *, domain: str, cls: OverflowClass) -> int:
        sent = 0
        for connection in list(self._connections.values()):
            if not connection.wants(domain, cls):
                continue
            if not self._visibility(connection, domain):
                continue
            payload = self._restrict(message, connection, domain)
            if payload is None:
                continue
            if connection.send(payload, cls=cls):
                sent += 1
        return sent

    def _restrict(self, message: Message, connection: Connection, domain: str) -> Message | None:
        """Narrow a frame to what this connection may see (§6.12)."""
        return self._restrict_frame(connection, domain, message)

    def _live_filter(self, connection: Connection, domain: str, message: Message) -> Message | None:
        """The default filter: staff unfiltered, a hirer per ``state.hirer``, else nothing.

        The snapshot is read here, on every frame, so a permission change
        applies from the next broadcast with no re-login (§6.7, B31).
        """
        if connection.tier in STAFF_TIERS:
            return message
        if connection.tier == HIRER_TIER:
            return filter_for_hirer(message, self._state.hirer.permissions)
        return None

    def resync_tier(self, tier: str, domains: Iterable[str]) -> int:
        """Send every open, foregrounded ``tier`` connection a fresh snapshot of ``domains``.

        For when what a tier may see changes rather than the state itself: a
        hirer whose page gained a channel receives its value at once, and one
        whose page lost a channel receives a snapshot without it. Only the
        domains each connection subscribed to are sent, filtered as any
        snapshot is; a backgrounded connection resyncs itself on return.
        Returns the number of messages queued.
        """
        wanted = frozenset(domains)
        sent = 0
        for connection in list(self._connections.values()):
            if connection.tier != tier or connection.closed or connection.backgrounded:
                continue
            for message in self.snapshot(connection.domains & wanted, connection=connection):
                if connection.send(message):
                    sent += 1
        return sent

    async def _on_device_status(self, event: DeviceStatusChanged) -> None:
        self.publish(device_status_message(event.device, event.status), domain="devices")

    async def _on_banner(self, event: SystemBannerChanged) -> None:
        self.publish(banner_message(event.level, event.key, event.text), domain="system")

    async def _on_timer(self, event: TimerChanged) -> None:
        self.publish(
            timer_message(event.running, event.started_at, event.accumulated_ms), domain="timer"
        )

    async def _on_video_source_changed(self, event: VideoSourceChanged) -> None:
        self.publish(
            hdmi_source_message(event.destination_id, event.input_id, event.diverged),
            domain="hdmi",
        )

    async def _on_lamps_changed(self, event: LampsChanged) -> None:
        self.publish(status_message(event.lamps), domain="status")

    # -- resync (§10.7, §16.8) ---------------------------------------------

    def snapshot(self, domains: Iterable[str], *, connection: Connection) -> list[Message]:
        """A full snapshot per domain, in the shape of a batched frame.

        Carries every tracked value rather than only changes, and
        ``"source": "resync"`` where the shape has a source. Nothing is
        replayed: discrete events that fired during an outage are not re-sent
        (§10.7), and meters are never in a snapshot (§16.8).
        """
        messages: list[Message] = []
        for name in sorted(set(domains)):
            if name not in BROADCAST_DOMAINS or not self._visibility(connection, name):
                continue
            for message in self._snapshot_domain(name):
                payload = self._restrict(message, connection, name)
                if payload is not None:
                    messages.append(payload)
        return messages

    def _snapshot_domain(self, name: str) -> list[Message]:
        if name in BATCHED_DOMAINS:
            return [message for message, _ in self._frames(name, None)]
        domain = self._state.domain(name)
        if name == "status":
            # Every lamp, in the shape of the batched frame (§16.8); narrowed
            # to lamp_ids for a hirer by filter_for_hirer, exactly as every
            # other resync snapshot is.
            return [status_message(_as_map(domain.get("lamps")))]
        if name == "devices":
            return [
                device_status_message(device, record.status)
                for device, record in sorted(self._state.devices.records().items())
            ]
        if name == "system":
            assert isinstance(domain, SystemDomain)
            return [
                banner_message(banner.level, key, banner.text)
                for key, banner in sorted(domain.banners().items())
            ]
        if name == "timer":
            assert isinstance(domain, TimerDomain)
            return [timer_message(domain.running, domain.started_at, domain.accumulated_ms)]
        if name == "projector":
            state = domain.get("state")
            if state is None:
                return []
            input_ref = domain.get("input_ref")
            ref = None if input_ref is None else str(input_ref)
            return [projector_state_message(str(state), ref)]
        if name == "hdmi":
            destinations = _as_map(domain.get("destinations"))
            messages: list[Message] = []
            for destination_id, data in sorted(destinations.items(), key=lambda kv: int(kv[0])):
                if not isinstance(data, Mapping):
                    continue
                raw_input_id = data.get("input_id")
                messages.append(
                    hdmi_source_message(
                        int(destination_id),
                        None if raw_input_id is None else int(raw_input_id),
                        bool(data.get("diverged")),
                    )
                )
            return messages
        return []


# -- frame construction --------------------------------------------------------------


def _dirty_items(keys: set[str] | None, field_name: str) -> set[str] | None:
    """The map items of ``field_name`` in a dirty set; ``None`` means all of them."""
    if keys is None:
        return None
    prefix = f"{field_name}."
    return {key[len(prefix) :] for key in keys if key.startswith(prefix)}


def _changed(keys: set[str] | None, field_name: str) -> bool:
    return keys is None or field_name in keys


def _order(items: Iterable[str]) -> list[str]:
    """Channel ids in numeric order where they are numbers, else lexical."""
    return sorted(items, key=lambda item: (0, int(item), "") if item.isdigit() else (1, 0, item))


def _lighting_frame(
    domain: LightingDomain, keys: set[str] | None, *, source: str
) -> Message | None:
    """``lighting_state`` (§16.8): levels 0–100 with one decimal, colour 0–255.

    A partial frame carries only the sections that changed; a snapshot
    (``keys is None``) carries every one, ``observed`` included — clients
    display ``observed`` in preference to ``channels`` whenever it is
    present, with an indicator saying so (§7.2.7).
    """
    levels = _as_map(domain.get("levels"))
    colour = _as_map(domain.get("colour"))
    dirty_levels = _dirty_items(keys, "levels")
    dirty_colour = _dirty_items(keys, "colour")
    if dirty_levels is None or dirty_colour is None:
        channel_ids: set[str] = set(levels) | set(colour)
    else:
        channel_ids = dirty_levels | dirty_colour
    channels: dict[str, Message] = {}
    for channel_id in _order(channel_ids):
        entry: Message = {}
        if channel_id in levels:
            entry["level"] = levels[channel_id]
        components = colour.get(channel_id)
        if isinstance(components, Mapping):
            entry.update({str(k): v for k, v in components.items()})
        if entry:
            channels[channel_id] = entry

    frame: Message = {"type": "lighting_state"}
    if channels or keys is None:
        frame["channels"] = channels
    if _changed(keys, "group_multipliers") or _dirty_items(keys, "group_multipliers"):
        groups = _as_map(domain.get("group_multipliers"))
        dirty_groups = _dirty_items(keys, "group_multipliers")
        frame["groups"] = (
            groups
            if dirty_groups is None
            else {k: groups[k] for k in _order(dirty_groups) if k in groups}
        )
    if _changed(keys, "master"):
        frame["master"] = domain.get("master")
    # Stage-bank states, keyed by rule id (§7.1, §21.11): whether every member
    # of a binding rule's group is at its on_level, written by the rules engine.
    dirty_bindings = _dirty_items(keys, "binding_states")
    if dirty_bindings is None or dirty_bindings:
        bindings = _as_map(domain.get("binding_states"))
        frame["bindings"] = (
            bindings
            if dirty_bindings is None
            else {k: bindings[k] for k in _order(dirty_bindings) if k in bindings}
        )
    external = domain.get("external_control")
    if _changed(keys, "external_control"):
        frame["external_control"] = external
    observed = _as_map(domain.get("observed"))
    dirty_observed = _dirty_items(keys, "observed")
    if keys is None or dirty_observed or _changed(keys, "external_control"):
        # Absent rather than at the floor: null when nothing is observed, so
        # the interface renders the controller's own model instead (§7.2.7).
        frame["observed"] = observed or None
    if len(frame) == 1:
        return None
    frame["source"] = source
    return frame


def _mixer_frame(domain: MixerDomain, keys: set[str] | None, *, source: str) -> Message | None:
    """``mixer_state`` (§16.8). Values are dB in both directions (§5.5)."""
    frame: Message = {"type": "mixer_state"}
    if _changed(keys, "main"):
        frame["main"] = domain.get("main")
    for field_name, wire in (("outputs", "outputs"), ("inputs", "inputs")):
        values = _as_map(domain.get(field_name))
        dirty = _dirty_items(keys, field_name)
        if dirty is None:
            frame[wire] = values
        elif dirty:
            frame[wire] = {k: values[k] for k in _order(dirty) if k in values}
    if len(frame) == 1:
        return None
    frame["source"] = source
    return frame


def _meter_frame(domain: MixerDomain, keys: set[str], *, at: float) -> Message | None:
    """``mixer_meters`` (§16.8): one list per channel, in driver-reference order.

    ``at`` is monotonic, not wall-clock: §5.5 defines it for staleness. A
    channel with no meter data is absent from the object rather than present
    at the floor, which is what lets the interface render no bar (B58).

    ``metering`` — ``{"available": bool, "reason": str | None}`` — rides the
    same frame on every availability change (``docs/plans/phase-4-contracts.md``):
    :meth:`~proskenion.core.mixer.service.MixerService._set_metering_reason`
    writes ``meters`` and the scalar ``metering`` field in one batch, so both
    dirty together, but a whole-map replace of ``meters`` dirties only at the
    *field* level — invisible to the per-item check below, which is exactly
    why a loss previously reached no open view at all (see that method's own
    docstring). Keying this on ``metering`` having changed, not on
    ``channels`` being non-empty, is the fix: a frame is sent even with an
    empty ``channels`` on loss, and with whatever ``channels`` already holds
    on recovery. This is only ever called with a concrete ``keys`` set, never
    for a resync (``keys is None`` short-circuits in :meth:`Broadcaster._frames`
    before this is reached), so an availability change is never replayed
    either — a client not yet open learns the current reason from
    ``GET /mixer/state`` instead.
    """
    availability_changed = _changed(keys, "metering")
    dirty = _dirty_items(keys, "meters") or set()
    if not dirty and not availability_changed:
        return None
    meters = _as_map(domain.get("meters"))
    channels = {k: meters[k] for k in _order(dirty) if k in meters}
    if not channels and not availability_changed:
        return None
    frame: Message = {"type": "mixer_meters", "channels": channels, "at": at}
    if availability_changed:
        metering = domain.get("metering")
        if isinstance(metering, Mapping):
            frame["metering"] = {
                "available": bool(metering.get("available")),
                "reason": metering.get("reason"),
            }
    return frame


def _scenes_frame(domain: ScenesDomain, keys: set[str] | None, *, source: str) -> Message | None:
    """``scenes_state``: which scenes are currently running, and the last result.

    Not itself a §16.8 message — the resync gap it closes is that a client
    connecting mid-run previously learned nothing until the next
    ``scene_started``/``scene_completed`` (§21.10 needs the card to show
    executing immediately). ``running`` maps scene id (string) to the run
    context the scene engine publishes (run id, priority, trigger, channels
    driven); ``last_result`` is the most recent completed run, whichever
    scene it belongs to. Both are carried whole on a snapshot and only when
    changed on a partial frame, exactly like ``lighting_state``.
    """
    frame: Message = {"type": "scenes_state"}
    dirty_running = _dirty_items(keys, "running")
    if dirty_running is None:
        frame["running"] = _as_map(domain.get("running"))
    elif dirty_running:
        running = _as_map(domain.get("running"))
        frame["running"] = {k: running[k] for k in _order(dirty_running) if k in running}
    if _changed(keys, "last_result"):
        frame["last_result"] = domain.get("last_result")
    if len(frame) == 1:
        return None
    frame["source"] = source
    return frame


def _as_map(value: object) -> dict[str, Any]:
    return {str(k): v for k, v in value.items()} if isinstance(value, Mapping) else {}
