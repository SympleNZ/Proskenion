"""The WebSocket endpoint, ``WS /ws?v=1`` (spec §16.8, §6.12, §21.2).

This socket carries every live value to the operator's tablet and the booth
screen, and every continuous control write back. It is mounted at the root,
not under ``/api/v1/``.

Upgrade
-------
:func:`proskenion.api.deps.authenticate_websocket` validates the Origin
(403) and the JWT cookie (401) on the HTTP upgrade and tags the connection
with the tier, which decides what it receives (§6.12). The version is checked
straight afterwards: an unknown or missing ``v`` is closed with **4001** and
the message the client presents as "Refresh required — the application has
been updated." A close code cannot be delivered without completing the
handshake, so the socket is accepted and then immediately closed with 4001 —
that is the only way a stale service worker is diagnosable rather than
mysterious, and it is worth the one accepted socket.

Liveness is the application's job (§16.8)
-----------------------------------------
§4.13 removes nginx's ``proxy_read_timeout`` on ``/ws`` precisely so an idle
socket stays open, which makes detecting a dead one ours. A closed browser
is detected immediately — the TCP FIN closes the socket and the handler
unsubscribes it. A sleeping tablet is not: it sends nothing and is
indistinguishable from an idle one. So the server sends ``{"type": "ping"}``
every 20 s and closes the socket after two intervals (40 s) without a
``pong``. The client sends its own ``ping`` to keep intermediaries from
closing an idle connection; it is answered with a ``pong``.

Sessions
--------
Traffic never extends a session: foreground presence holds one, and pings,
pongs, resyncs and broadcasts are never activity (§6.4, B65). So the idle
expiry is **not** enforced on this socket — the client's own
``GET /auth/session`` governs it — but the absolute one is: every socket, of
every tier, is closed with **4002** when its token's ``aexp`` is reached.
Re-issue carries ``aexp`` forward unchanged, so the socket's deadline is the
session's.

Revocation (§6.6, §6.7)
-----------------------
A hirer socket is closed with **4003** the moment access is disabled or the
PIN changes: :class:`~proskenion.core.hirer_access.HirerAccess` marks every
open hirer connection closed in the same synchronous step that updates
``state.hirer``. Two further checks close the gaps either side of that sweep:

* an upgrade registers its connection and re-checks admission with no
  ``await`` between, so a socket accepted while the switch landed is either
  swept or refused here;
* every hirer ``set`` is re-checked against the in-memory ``enabled`` and
  ``token_version`` and holds an access slot while it is applied, so the
  switch waits for it; one that fails the check is answered ``nack``
  (``unauthenticated``, ``detail.reason = "hirer_revoked"``, as REST answers
  it) and the socket closes with 4003.

Writes (§21.2)
--------------
``set`` frames are applied in arrival order per connection with no batching —
a coalesced drag at 30 Hz is 30 small frames, which is what a WebSocket is
for — and each is answered by ``ack`` or ``nack`` echoing the client's
``token``. The token is what ties a rejection back to the correct pending
entry when several gestures are in flight; without it the two-map model
cannot know which gesture a rejection refers to. A ``nack`` carries the
authoritative value where one exists, so the client settles at the right
place rather than reverting to a stale one.

The lighting and mixer modules register a handler per domain
(:class:`WriteRouter`); a domain with none is answered ``not_found``.

Hirer writes (§6.7, the phase-5 contract)
-----------------------------------------
Enforcement on the socket is the same as on REST. Before a hirer's ``set``
reaches its handler, :meth:`~proskenion.core.broadcast.Broadcaster.may_write`
checks the one target it names against the live ``state.hirer`` snapshot. A
refusal is answered ``nack`` with ``permission_denied`` — carrying the
authoritative value where the connection may see one (a group member shown
read-only, say), and nothing for a target it cannot see — and writes a
``permission_denied`` audit row, at most one per socket, target and minute so
a client dragging a refused fader cannot flood the log. How far a permitted
mixer write may go (the ceiling) is the mixer handler's check.

Holds that end with the connection (``lighting_bump``)
-----------------------------------------------------
A group's BUMP flashes it while held (owner decision 2026-10-01), and a hold
belongs to the connection that made it: the handler is told which one
(:attr:`SetRequest.connection`). Whatever a domain holds for a connection is
let go through :meth:`WriteRouter.release_connection` when the socket closes,
for any reason, and when the client reports going to the background — a
hidden page cannot be holding a button. The lighting service also expires a
hold the client stops refreshing (:mod:`proskenion.core.dmx.bump`).

``value`` and ``null`` (§16.8, phase-4-contracts.md)
------------------------------------------------------
Every domain's value is a JSON number except ``mixer``, whose level is a dB
float **or** ``null`` for off (§5.5, B41) — the same shape
``POST /mixer/channels/{id}/level`` takes in its body. :func:`parse_set`
accepts ``null`` only when ``domain == "mixer"``; every other domain still
requires a number, so lighting's, the group's and the master's validation is
unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Final, TypeIs

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from proskenion.api.deps import WebSocketDenied, authenticate_websocket, client_address
from proskenion.api.errors import ErrorCode
from proskenion.core.auth import TokenClaims, TokenError, TokenService, record_event
from proskenion.core.broadcast import (
    CLOSE_ACCESS_REVOKED,
    CLOSE_SESSION_EXPIRED,
    SETTABLE_DOMAINS,
    WRITE_DOMAIN_STATE,
    Broadcaster,
    Connection,
    Message,
)
from proskenion.core.elapsed import seconds_between
from proskenion.core.hirer_access import ACCESS_UPDATED_MESSAGE, REVOKED_REASON, HirerAccess
from proskenion.db.connection import Database

log = logging.getLogger(__name__)

router = APIRouter()

#: The protocol version in ``?v=``. Bumped only by a breaking wire change.
PROTOCOL_VERSION: Final = "1"

#: Close codes. 4001–4003 are application codes (§16.8 and the Phase 5
#: contract); the rest are RFC 6455.
CLOSE_REFRESH_REQUIRED: Final = 4001
CLOSE_EXPIRED: Final = CLOSE_SESSION_EXPIRED  # 4002, the absolute expiry
CLOSE_REVOKED: Final = CLOSE_ACCESS_REVOKED  # 4003, hirer access revoked
CLOSE_LIVENESS: Final = 1001  # going away — no pong within two intervals
CLOSE_NORMAL: Final = 1000

REFRESH_REQUIRED_MESSAGE: Final = "Refresh required — the application has been updated."
LIVENESS_MESSAGE: Final = "No pong within two ping intervals"
SESSION_EXPIRED_MESSAGE: Final = "The session has reached its 12-hour limit"

#: How long a closing socket may spend sending what was queued before the
#: close — a ``nack`` explaining it, say — before it is closed regardless.
CLOSE_FLUSH_TIMEOUT_S: Final = 1.0

#: A refused write is audited at most once per socket and target in this
#: window: a hirer dragging a refused fader sends thirty frames a second.
DENIAL_AUDIT_INTERVAL_S: Final = 60.0


# -- inbound writes ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SetRequest:
    """One ``set`` frame, parsed and validated (§16.8).

    ``value`` is in the core's units, never a wire format: lighting 0–100 with
    one decimal, mixer dB with ``None`` for off (§5.5, B41). ``id`` is ``None``
    only for ``master``, which addresses no channel. ``value`` is ``None``
    only for ``mixer`` (see the module docstring).
    """

    domain: str
    id: int | None
    value: float | None
    token: int
    connection: int | None = None
    """The connection the frame arrived on, for a handler whose effect is held
    by it (``lighting_bump``). Set by the endpoint, never by the client."""


class _NoValue:
    """Sentinel distinguishing "no authoritative value to report" from an
    authoritative value that is itself ``None`` — a mixer channel's off
    (§5.5). A plain ``None`` default could not tell the two apart."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<no value>"


NO_VALUE: Final = _NoValue()


@dataclass(frozen=True, slots=True)
class SetResult:
    """A handler's answer: ``ack``, or ``nack`` with a reason and the truth."""

    ok: bool
    reason: ErrorCode | None = None
    value: object = NO_VALUE

    @classmethod
    def accepted(cls) -> SetResult:
        return cls(True)

    @classmethod
    def rejected(cls, reason: ErrorCode, value: object = NO_VALUE) -> SetResult:
        """Reject with a code from the closed §16.1 vocabulary.

        ``value`` is the authoritative value where one exists — a hirer write
        clamped to a ceiling returns the clamped dB, and a mixer channel that
        is off returns ``None`` explicitly rather than nothing (see
        :class:`_NoValue`).
        """
        return cls(False, reason, value)


SetHandler = Callable[[SetRequest, TokenClaims], Awaitable[SetResult]]
"""What a domain registers to accept writes. It must not raise; if it does,
the client is sent ``internal_error`` and the socket stays open."""

ConnectionRelease = Callable[[int], object]
"""Let go of whatever a domain holds for a connection id (module docstring)."""


class WriteRouter:
    """Routes a validated ``set`` to the handler for its domain.

    Phase 1 registers nothing, so every write is answered ``not_found``: the
    domain is in the protocol but not in this build. The lighting and mixer
    tasks call :meth:`register` and the protocol above them does not change.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, SetHandler] = {}
        self._releases: list[ConnectionRelease] = []

    def register(self, domain: str, handler: SetHandler) -> None:
        if domain not in SETTABLE_DOMAINS:
            raise ValueError(f"{domain!r} is not a settable domain (§16.8)")
        if domain in self._handlers:
            raise ValueError(f"{domain!r} already has a write handler")
        self._handlers[domain] = handler

    def handles(self, domain: str) -> bool:
        return domain in self._handlers

    def on_release(self, release: ConnectionRelease) -> None:
        """Call ``release(connection id)`` when a connection closes or goes to the background."""
        self._releases.append(release)

    def release_connection(self, connection_id: int) -> None:
        """Let go of everything held for ``connection_id``. Never raises."""
        for release in self._releases:
            try:
                release(connection_id)
            except Exception:
                log.exception(
                    "websocket connection release raised", extra={"connection": connection_id}
                )

    async def apply(self, request: SetRequest, claims: TokenClaims) -> SetResult:
        handler = self._handlers.get(request.domain)
        if handler is None:
            return SetResult.rejected(ErrorCode.NOT_FOUND)
        try:
            return await handler(request, claims)
        except Exception:
            log.exception(
                "websocket write handler raised",
                extra={"domain": request.domain, "id": request.id},
            )
            return SetResult.rejected(ErrorCode.INTERNAL_ERROR)


def _is_number(value: object) -> TypeIs[int | float]:
    """A JSON number that is neither a bool nor a NaN or infinity."""
    return not isinstance(value, bool) and isinstance(value, int | float) and math.isfinite(value)


def _is_int(value: object) -> TypeIs[int]:
    """A JSON integer. ``True`` is not one, however much Python disagrees."""
    return not isinstance(value, bool) and isinstance(value, int)


def parse_set(message: Message) -> tuple[SetRequest | None, ErrorCode | None, int | None]:
    """Validate a ``set`` frame. Returns ``(request, reason, token)``.

    The token is returned even when the frame is otherwise unusable, because
    a ``nack`` the client cannot match to a pending entry is no better than
    silence (§21.2). A frame with no usable token is nacked with a ``null``
    one, which at least shows up in the console of whatever sent it.
    """
    raw_token = message.get("token")
    token = raw_token if _is_int(raw_token) else None
    if token is None:
        return None, ErrorCode.VALIDATION_FAILED, None
    domain = message.get("domain")
    if not isinstance(domain, str) or domain not in SETTABLE_DOMAINS:
        return None, ErrorCode.VALIDATION_FAILED, token
    value = message.get("value")
    if value is None:
        # null is a value only for mixer, where it means "off" (§5.5, B41,
        # see the module docstring) — every other domain still requires a
        # number.
        if domain != "mixer":
            return None, ErrorCode.VALIDATION_FAILED, token
    elif not _is_number(value):
        return None, ErrorCode.VALIDATION_FAILED, token
    raw_id = message.get("id")
    identifier: int | None
    if domain == "master":
        # master addresses no channel, so an id is optional there and
        # meaningless anywhere else.
        if raw_id is not None and not _is_int(raw_id):
            return None, ErrorCode.VALIDATION_FAILED, token
        identifier = raw_id if _is_int(raw_id) else None
    elif _is_int(raw_id):
        identifier = raw_id
    else:
        return None, ErrorCode.VALIDATION_FAILED, token
    parsed_value = None if value is None else float(value)
    return SetRequest(domain, identifier, parsed_value, token), None, token


def ack_message(token: int) -> Message:
    return {"type": "ack", "token": token}


def nack_message(
    token: int | None,
    reason: ErrorCode,
    value: object = NO_VALUE,
    *,
    detail: dict[str, Any] | None = None,
) -> Message:
    """A ``nack`` frame. ``value`` carries the authoritative value where one
    exists, ``None`` included — a mixer channel that is off (§5.5) — and is
    left out of the frame only when the caller passed :data:`NO_VALUE`.
    ``detail`` is the §16.1 envelope's ``detail``, for when the code alone
    does not say why."""
    message: Message = {"type": "nack", "token": token, "reason": reason.value}
    if value is not NO_VALUE:
        message["value"] = value
    if detail is not None:
        message["detail"] = detail
    return message


def revoked_nack(token: int) -> Message:
    """The answer to a hirer ``set`` after access was revoked, shaped as REST's 401."""
    return nack_message(token, ErrorCode.UNAUTHENTICATED, detail={"reason": REVOKED_REASON})


# -- the endpoint --------------------------------------------------------------------


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """``WS /ws?v=1`` — the live socket (§16.8)."""
    try:
        claims = await authenticate_websocket(websocket)
    except WebSocketDenied:
        return

    if websocket.query_params.get("v") != PROTOCOL_VERSION:
        # A close code needs a completed handshake, so accept and close at
        # once. The client presents 4001 as "Refresh required".
        log.info(
            "websocket rejected: unsupported protocol version",
            extra={"version": websocket.query_params.get("v"), "tier": claims.tier},
        )
        await websocket.accept()
        await websocket.close(code=CLOSE_REFRESH_REQUIRED, reason=REFRESH_REQUIRED_MESSAGE)
        return

    broadcaster: Broadcaster = websocket.app.state.broadcaster
    writes: WriteRouter = websocket.app.state.writes
    access: HirerAccess = websocket.app.state.hirer_access
    tokens: TokenService = websocket.app.state.tokens
    await websocket.accept()
    connection = broadcaster.connect(
        tier=claims.tier,
        session_id=claims.session_id,
        address=client_address(websocket.scope),
    )
    # No await since connect(): a revocation that landed during accept() is
    # seen here, and one that lands later sweeps this connection.
    if claims.is_hirer and not access.admits(claims):
        connection.close(ACCESS_UPDATED_MESSAGE, code=CLOSE_REVOKED)
    session = _Session(claims, access, tokens, websocket.app.state.db)
    await _serve(websocket, connection, session, broadcaster, writes)


@dataclass(frozen=True, slots=True)
class _Session:
    """What one socket knows about the session behind it."""

    claims: TokenClaims
    access: HirerAccess
    tokens: TokenService
    db: Database
    #: ``(set domain, target) -> when its refusal was last audited`` (monotonic).
    denials: dict[tuple[str, int | None], float] = field(default_factory=dict)


async def _serve(
    websocket: WebSocket,
    connection: Connection,
    session: _Session,
    broadcaster: Broadcaster,
    writes: WriteRouter,
) -> None:
    """Run the four concurrent parts of a connection until one of them ends.

    Reader, writer, liveness and the absolute expiry. Whichever finishes
    first decides why the socket closes and the others are stopped —
    except that after a close the writer is first given a moment to send
    what was queued before it. Only the writer ever calls ``send``, so two
    frames can never interleave on one socket.

    The reader and the writer are tasks. Liveness and the expiry are loop
    timers (:class:`_Timers`) that resolve one future when either of them
    ends, so a socket costs two tasks of ours rather than four (§23.3).
    """
    sender = asyncio.create_task(_send(websocket, connection), name="ws-send")
    receiver = asyncio.create_task(
        _receive(websocket, connection, session, broadcaster, writes), name="ws-receive"
    )
    timers = _Timers(
        connection,
        ping_interval=broadcaster.ping_interval,
        now=broadcaster.now,
        expires_at=session.claims.absolute_expires_at,
        token_now=session.tokens.now,
        recheck=broadcaster.expiry_check,
    )
    names: dict[asyncio.Future[None], str] = {
        receiver: "ws-receive",
        sender: "ws-send",
        timers.ended: "ws-timers",
    }
    parts: set[asyncio.Future[None]] = set(names)
    try:
        timers.start()
        done, pending = await asyncio.wait(parts, return_when=asyncio.FIRST_COMPLETED)
        if connection.closed and sender in pending:
            # The writer ends by itself once the closed queue is drained.
            flushed, _ = await asyncio.wait({sender}, timeout=CLOSE_FLUSH_TIMEOUT_S)
            done |= flushed
            pending -= flushed
        for part in pending:
            part.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for part in done:
            error = part.exception()
            if error is not None and not isinstance(error, WebSocketDisconnect):
                log.warning(
                    "websocket task failed",
                    extra={"connection": connection.id, "task": names[part]},
                    exc_info=error,
                )
    finally:
        # Synchronous first: this must survive the cancellation a shutdown
        # delivers, so a closed browser leaves nothing running and nothing
        # subscribed (§16.8).
        timers.stop()
        for part in parts:
            part.cancel()
        # A held bump never outlives its socket (module docstring).
        writes.release_connection(connection.id)
        code = connection.close_code or CLOSE_NORMAL
        reason = connection.close_reason or ""
        broadcaster.disconnect(connection)
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close(code=code, reason=reason)


async def _send(websocket: WebSocket, connection: Connection) -> None:
    """The only writer on the socket: drains the connection's outbound queue."""
    async for message in connection.messages():
        await websocket.send_json(message)


class _Timers:
    """Liveness and the absolute expiry of one socket, as loop timers.

    Liveness pings every ``ping_interval`` and gives up after two of them
    without a pong (§16.8). The expiry closes the socket with 4002 at the
    token's ``aexp`` (§6.4), never at ``exp``: it waits towards the deadline
    in steps of at most ``recheck`` seconds and re-reads the token clock after
    each, so a clock step is honoured.

    Each ends by resolving :attr:`ended`, which :func:`_serve` waits on beside
    the reader and the writer. That matters even when the socket was already
    closed by something else: a close whose writer is stuck is still ended by
    liveness giving up. An exception in either timer resolves :attr:`ended`
    with it, as a failed task would have.
    """

    def __init__(
        self,
        connection: Connection,
        *,
        ping_interval: float,
        now: Callable[[], float],
        expires_at: datetime,
        token_now: Callable[[], datetime],
        recheck: float,
    ) -> None:
        self._connection = connection
        self._interval = ping_interval
        self._now = now
        self._expires_at = expires_at
        self._token_now = token_now
        self._recheck = recheck
        self._loop = asyncio.get_running_loop()
        self.ended: asyncio.Future[None] = self._loop.create_future()
        self._ping: asyncio.Handle | None = None
        self._expiry: asyncio.Handle | None = None

    def start(self) -> None:
        self._ping = self._loop.call_later(self._interval, self._guarded, self._ping_due)
        self._expiry = self._loop.call_soon(self._guarded, self._expiry_due)

    def stop(self) -> None:
        for handle in (self._ping, self._expiry):
            if handle is not None:
                handle.cancel()
        self._ping = self._expiry = None
        if not self.ended.done():
            self.ended.cancel()

    def _guarded(self, step: Callable[[], None]) -> None:
        try:
            step()
        except Exception as exc:
            self._end(exc)

    def _end(self, error: BaseException | None) -> None:
        if self.ended.done():
            return
        if error is None:
            self.ended.set_result(None)
        else:
            self.ended.set_exception(error)

    def _ping_due(self) -> None:
        connection = self._connection
        if self._now() - connection.last_pong >= 2 * self._interval:
            log.info(
                "websocket closed: no pong within two ping intervals",
                extra={"connection": connection.id, "interval_s": self._interval},
            )
            connection.close(LIVENESS_MESSAGE, code=CLOSE_LIVENESS)
            self._end(None)
            return
        connection.send({"type": "ping"})
        self._ping = self._loop.call_later(self._interval, self._guarded, self._ping_due)

    def _expiry_due(self) -> None:
        connection = self._connection
        # Real seconds: both datetimes are Pacific/Auckland, and subtracting two
        # that share a zone compares wall clocks, an hour out across a
        # daylight-saving change.
        remaining = seconds_between(self._token_now(), self._expires_at)
        if remaining <= 0:
            log.info(
                "websocket closed: the session reached its absolute expiry",
                extra={"connection": connection.id, "tier": connection.tier},
            )
            connection.close(SESSION_EXPIRED_MESSAGE, code=CLOSE_EXPIRED)
            self._end(None)
            return
        self._expiry = self._loop.call_later(
            min(remaining, self._recheck), self._guarded, self._expiry_due
        )


async def _receive(
    websocket: WebSocket,
    connection: Connection,
    session: _Session,
    broadcaster: Broadcaster,
    writes: WriteRouter,
) -> None:
    """Client-to-server messages, handled strictly in arrival order (§16.8)."""
    while True:
        try:
            frame = await websocket.receive()
        except (WebSocketDisconnect, RuntimeError):
            return
        if frame["type"] == "websocket.disconnect":
            return
        text = frame.get("text")
        if text is None:
            log.warning(
                "websocket binary frame ignored: the protocol is JSON text (§16.8)",
                extra={"connection": connection.id},
            )
            continue
        try:
            payload = json.loads(text)
        except ValueError:
            log.warning(
                "websocket frame is not valid JSON; ignored",
                extra={"connection": connection.id},
            )
            continue
        if not isinstance(payload, dict):
            log.warning(
                "websocket frame is not an object; ignored", extra={"connection": connection.id}
            )
            continue
        await _handle(payload, websocket, connection, session, broadcaster, writes)
        if connection.closed:
            return


async def _handle(
    payload: dict[str, Any],
    websocket: WebSocket,
    connection: Connection,
    session: _Session,
    broadcaster: Broadcaster,
    writes: WriteRouter,
) -> None:
    """One client message. Never closes the socket for an unknown type."""
    kind = payload.get("type")
    if kind == "subscribe":
        accepted = broadcaster.subscribe(connection, _domains(payload, connection))
        log.debug(
            "websocket subscribed",
            extra={"connection": connection.id, "domains": sorted(accepted)},
        )
    elif kind == "resync":
        # Returning to the foreground, or reconnecting after an outage: a
        # full snapshot per domain, nothing replayed (§10.7). No
        # re-authentication — the upgrade already carried the cookie.
        connection.foreground()
        domains = broadcaster.subscribe(connection, _domains(payload, connection))
        for message in broadcaster.snapshot(domains, connection=connection):
            connection.send(message)
    elif kind == "ping":
        # The client's keep-alive against intermediaries. Never activity (§6.4).
        connection.send({"type": "pong"})
    elif kind == "pong":
        connection.last_pong = broadcaster.now()
    elif kind == "background":
        connection.background()
        # A hidden page is holding nothing down (module docstring).
        writes.release_connection(connection.id)
        log.debug("websocket backgrounded", extra={"connection": connection.id})
    elif kind == "set":
        await _handle_set(payload, connection, session, broadcaster, writes)
    else:
        # Ignored, never a close: an unknown type is a newer client or a
        # stray frame, and dropping the socket would cost the operator every
        # live value on screen.
        log.warning(
            "unknown websocket message type ignored",
            extra={"connection": connection.id, "message_type": kind},
        )


def _domains(payload: dict[str, Any], connection: Connection) -> list[str]:
    """The ``domains`` of a subscribe or resync; the current set when absent."""
    domains = payload.get("domains")
    if isinstance(domains, list):
        return [d for d in domains if isinstance(d, str)]
    return sorted(connection.domains)


async def _handle_set(
    payload: dict[str, Any],
    connection: Connection,
    session: _Session,
    broadcaster: Broadcaster,
    writes: WriteRouter,
) -> None:
    """Parse, re-check a hirer's access, check the tier, route, and answer (§21.2)."""
    request, reason, token = parse_set(payload)
    if request is None:
        assert reason is not None
        connection.send(nack_message(token, reason))
        return
    claims = session.claims
    if not claims.is_hirer:
        await _apply_set(request, connection, session, broadcaster, writes)
        return
    # Admission and the slot are one synchronous step, and the kill switch
    # waits for the slot: this write lands before the switch answers, or not
    # at all.
    try:
        session.access.hold(claims)
    except TokenError:
        connection.send(revoked_nack(request.token))
        connection.close(ACCESS_UPDATED_MESSAGE, code=CLOSE_REVOKED)
        return
    # The revocation's sweep closes this socket, which cancels this task. The
    # write itself is shielded so a device command is never abandoned half
    # sent, and the slot is released only when the write has really finished —
    # which is what the switch is waiting for.
    applying = asyncio.ensure_future(_apply_set(request, connection, session, broadcaster, writes))
    applying.add_done_callback(lambda task: _finish_hirer_write(task, session.access))
    await asyncio.shield(applying)


def _finish_hirer_write(task: asyncio.Future[None], access: HirerAccess) -> None:
    access.release()
    if not task.cancelled() and task.exception() is not None:
        log.error("hirer websocket write failed", exc_info=task.exception())


async def _apply_set(
    request: SetRequest,
    connection: Connection,
    session: _Session,
    broadcaster: Broadcaster,
    writes: WriteRouter,
) -> None:
    """Check the target is writable, route to the domain's handler and answer."""
    claims = session.claims
    # Server-side permission enforcement applies identically to WebSocket
    # writes (§21.2, §6.7): a tier that may not even see the domain may
    # certainly not write it, and a hirer may write only the targets their
    # pages reach.
    if not broadcaster.may_write(connection, request.domain, request.id):
        await _audit_denial(request, connection, session)
        connection.send(
            nack_message(
                request.token,
                ErrorCode.PERMISSION_DENIED,
                visible_value(broadcaster, connection, request),
            )
        )
        return
    result = await writes.apply(replace(request, connection=connection.id), claims)
    if result.ok:
        # §21.2: on acknowledgement the client deletes its pending entry and
        # "falls through to a value that is already correct". The write's own
        # frame is batched to the next tick, so without this the ack would
        # overtake it: the control would flash back to the old value, and a
        # second keyboard step in that window would count from it (§24.2).
        # Broadcasting what is dirty now queues that frame ahead of the ack.
        broadcaster.tick()
        connection.send(ack_message(request.token))
        return
    assert result.reason is not None
    connection.send(nack_message(request.token, result.reason, result.value))


def visible_value(broadcaster: Broadcaster, connection: Connection, request: SetRequest) -> object:
    """The authoritative value of a refused ``set``'s target, where the
    connection may see it; :data:`NO_VALUE` where it may not.

    Read from what a snapshot would send this connection, in the wire's
    units: a lighting level 0–100, the master 0–100, a mixer channel in dB
    (``None`` is off); nothing for a group, which has no value of its own. A
    hirer refused a channel they cannot see is told nothing about it.
    """
    state_domain = WRITE_DOMAIN_STATE.get(request.domain)
    if state_domain is None:
        return NO_VALUE
    for message in broadcaster.snapshot([state_domain], connection=connection):
        found = _value_in(message, request)
        if found is not NO_VALUE:
            return found
    return NO_VALUE


def _value_in(message: Message, request: SetRequest) -> object:
    key = str(request.id)
    kind = message.get("type")
    if kind == "lighting_state":
        if request.domain == "master":
            return message.get("master", NO_VALUE)
        if request.domain != "lighting":
            # A group has no stored value of its own: its fader shows its
            # members' levels (owner decision 2026-09-30), which the client
            # already holds in ``channels``.
            return NO_VALUE
        entries = message.get("channels")
        if not isinstance(entries, Mapping) or key not in entries:
            return NO_VALUE
        entry = entries[key]
        return entry.get("level", NO_VALUE) if isinstance(entry, Mapping) else NO_VALUE
    if kind == "mixer_state" and request.domain == "mixer":
        # Main is not keyed by id on the wire; it is never refused to a
        # connection that can see it (Main on an assigned page is writable).
        for section in ("inputs", "outputs"):
            entries = message.get(section)
            if isinstance(entries, Mapping):
                entry = entries.get(key)
                if isinstance(entry, Mapping):
                    return entry.get("db", NO_VALUE)
    return NO_VALUE


async def _audit_denial(request: SetRequest, connection: Connection, session: _Session) -> None:
    """A ``permission_denied`` row for a refused ``set``, throttled per target."""
    target = (request.domain, request.id)
    now = time.monotonic()
    last = session.denials.get(target)
    if last is not None and now - last < DENIAL_AUDIT_INTERVAL_S:
        return
    session.denials[target] = now
    try:
        await record_event(
            session.db,
            "permission_denied",
            user_ident=session.claims.tier,
            ip_address=connection.address,
            detail={
                "transport": "websocket",
                "domain": request.domain,
                "id": request.id,
                "session_id": session.claims.session_id,
            },
        )
    except Exception:  # pragma: no cover - the refusal stands without its row
        log.exception("permission_denied could not be audited", extra={"domain": request.domain})
