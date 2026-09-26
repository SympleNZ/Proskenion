"""Request-level dependencies: client address, origin, session and tier gates.

Client address (§4.13, §6.8)
    nginx is the only client of uvicorn and forwards the real address in
    ``X-Forwarded-For`` and ``X-Real-IP``. uvicorn runs with
    ``--proxy-headers --forwarded-allow-ips=127.0.0.1`` and rewrites
    ``request.client`` from those headers, so :func:`client_ip` normally just
    reads ``request.client.host``. When the immediate peer is still the
    loopback address (uvicorn was started without proxy handling, or a test
    client) the headers are read here under the same rule: trusted only from
    ``127.0.0.1``. Without the forwarded address, per-IP rate limiting
    degrades to a global limit and the audit log records the proxy.

Origin (§16.2, §6.12)
    HTTP requests whose ``Origin`` does not match ``[server] hostname`` are
    served normally — no preflight will succeed and non-browser clients do
    not care — but logged as ``unexpected_origin``. WebSocket upgrades are
    refused with 403 instead (:func:`authenticate_websocket`).

Sessions (§6.4, §6.7)
    :func:`session_claims` decodes the cookie and checks the live
    ``token_version`` — for a hirer against ``state.hirer`` in memory, never
    the database. :func:`current_session` is the dependency form: for a hirer
    it also holds an access slot for the whole request, so a kill switch or
    PIN change waits for a hirer request admitted before it and refuses every
    one after it (:mod:`proskenion.core.hirer_access`). :func:`require_tier`
    builds a 403 gate on top, and records the tiers it admits so the route
    table can be enumerated (:func:`admitted_tiers`); :func:`refuse_hirer` is
    the per-target refusal behind a gate that admits hirers. Nothing here
    extends a session: only ``GET /auth/session`` does.

First run (§10.4, phase-1 plan Q4)
    :class:`FirstRunGateMiddleware` refuses every API route while the wizard
    has not committed, so the redirect is enforced by the server and not only
    by the client. ``/health``, ``/setup/*`` and ``/auth/*`` are exempt, and so
    are ``/drivers/*`` and ``/devices/*``: the wizard's device step (§10.4
    step 4) needs them, and they stay admin-tier rather than public — reachable
    because ``POST /setup/step/2`` signs the wizard in, not because the gate
    stops checking.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Annotated, Final
from urllib.parse import urlsplit

from fastapi import Depends, Request, WebSocket
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from proskenion.api.errors import ApiError, ErrorCode, error_response
from proskenion.config import Config
from proskenion.core.auth import (
    COOKIE_NAME,
    TokenClaims,
    TokenError,
    TokenService,
    check_live_version,
    record_event,
)
from proskenion.core.bus import EventBus
from proskenion.core.devices import DeviceManager
from proskenion.core.dmx.desk import DeskInput
from proskenion.core.helper import HelperClient
from proskenion.core.hirer_access import HirerAccess
from proskenion.core.hirer_permissions import HirerPermissionResolver
from proskenion.core.knx import KnxSubsystem
from proskenion.core.knx_import import ImportSessionStore
from proskenion.core.knx_registry import DbAddressRegistry
from proskenion.core.lighting import LightingService
from proskenion.core.mixer.service import MixerService
from proskenion.core.projector import ProjectorService
from proskenion.core.setup import FirstRunFlag
from proskenion.core.state import StateStore
from proskenion.core.video import VideoService
from proskenion.db.connection import Database

log = logging.getLogger(__name__)

TRUSTED_PROXIES: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1"})
UNKNOWN_ADDRESS: Final = "unknown"

# WebSocket close codes used when the upgrade cannot be honoured (§6.12).
WS_CLOSE_POLICY_VIOLATION: Final = 1008


# -- client address ---------------------------------------------------------------


def _peer_host(scope: Scope) -> str | None:
    client = scope.get("client")
    return None if not client else str(client[0])


def _header(scope: Scope, name: bytes) -> str | None:
    headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
    for key, value in headers:
        if key == name:
            return value.decode("latin-1")
    return None


def client_address(scope: Scope) -> str:
    """The real client address for an HTTP or WebSocket scope (see module docstring)."""
    peer = _peer_host(scope)
    if peer is None:
        return UNKNOWN_ADDRESS
    if peer not in TRUSTED_PROXIES:
        return peer
    # nginx sets X-Real-IP from $remote_addr, which a client cannot forge.
    real_ip = _header(scope, b"x-real-ip")
    if real_ip and real_ip.strip():
        return real_ip.strip()
    # $proxy_add_x_forwarded_for appends: the rightmost entry that is not a
    # trusted proxy is the address nginx saw.
    forwarded = _header(scope, b"x-forwarded-for")
    if forwarded:
        for entry in reversed(forwarded.split(",")):
            host = entry.strip()
            if host and host not in TRUSTED_PROXIES:
                return host
    return peer


def client_ip(request: Request) -> str:
    """The real client address of ``request`` (§4.13)."""
    return client_address(request.scope)


# -- origin -----------------------------------------------------------------------


def origin_host(origin: str) -> str | None:
    """The host part of an ``Origin`` header value, lower-cased, or ``None``."""
    try:
        parts = urlsplit(origin.strip())
    except ValueError:
        return None
    return parts.hostname.lower() if parts.hostname else None


def origin_is_expected(origin: str | None, hostname: str | None) -> bool:
    """True when there is no configured hostname, no Origin, or they agree."""
    if hostname is None or origin is None:
        return True
    return origin_host(origin) == hostname.strip().lower()


class UnexpectedOriginMiddleware:
    """Log ``unexpected_origin`` for HTTP requests from another origin (§16.2).

    The request is served normally. Pure ASGI so it costs nothing on the
    common path: one header lookup when the hostname is configured.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            app = scope.get("app")
            config: Config | None = getattr(getattr(app, "state", None), "config", None)
            db: Database | None = getattr(getattr(app, "state", None), "db", None)
            hostname = config.server.hostname if config is not None else None
            origin = _header(scope, b"origin")
            if (
                hostname is not None
                and origin is not None
                and db is not None
                and not origin_is_expected(origin, hostname)
            ):
                await record_event(
                    db,
                    "unexpected_origin",
                    user_ident=None,
                    ip_address=client_address(scope),
                    detail={
                        "origin": origin,
                        "method": scope.get("method"),
                        "path": scope.get("path"),
                    },
                )
        await self.app(scope, receive, send)


# -- application services -----------------------------------------------------------


def get_db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


def get_tokens(request: Request) -> TokenService:
    tokens: TokenService = request.app.state.tokens
    return tokens


def get_config(request: Request) -> Config:
    config: Config = request.app.state.config
    return config


def get_state(request: Request) -> StateStore:
    """The state store the lifespan built (§5.6)."""
    store: StateStore = request.app.state.state_store
    return store


def get_bus(request: Request) -> EventBus:
    """The event bus the lifespan built (§5.6)."""
    bus: EventBus = request.app.state.bus
    return bus


def get_devices(request: Request) -> DeviceManager:
    """The running device manager (§5.5).

    A request that needs it before startup finished — or after a failed one —
    is answered with ``device_unavailable`` rather than a 500: nothing is
    wrong with the request.
    """
    manager: DeviceManager | None = getattr(request.app.state, "devices", None)
    if manager is None:
        raise ApiError(
            ErrorCode.DEVICE_UNAVAILABLE,
            "The device manager is not running",
            {"reason": "not_started"},
        )
    return manager


def get_lighting(request: Request) -> LightingService:
    """The running lighting service (§7.2, §16.5).

    Mirrors :func:`get_devices`: a request that arrives before the lifespan
    has built it — or after a failed boot — is answered ``device_unavailable``
    rather than a 500, since nothing is wrong with the request itself.
    """
    service: LightingService | None = getattr(request.app.state, "lighting", None)
    if service is None:
        raise ApiError(
            ErrorCode.DEVICE_UNAVAILABLE,
            "The lighting service is not running",
            {"reason": "not_started"},
        )
    return service


def get_desk_input(request: Request) -> DeskInput | None:
    """The booth input (§7.2.7), or ``None``.

    Unlike :func:`get_lighting`, absence is not an error to report: most
    venues have no booth input wired at all (``DeskInput.configured`` stays
    unresolved), which is the ordinary case, not a failed boot. Callers that
    only want ``last_frame_at`` (§16.5's ``GET /lighting/external-control``)
    treat ``None`` the same as "no frame yet".
    """
    return getattr(request.app.state, "desk_input", None)


def get_video(request: Request) -> VideoService:
    """The running HDMI matrix service (§7.5, §16.5).

    Mirrors :func:`get_lighting`: a request that arrives before the lifespan
    has built it — or after a failed boot — is answered ``device_unavailable``
    rather than a 500, since nothing is wrong with the request itself.
    """
    service: VideoService | None = getattr(request.app.state, "video", None)
    if service is None:
        raise ApiError(
            ErrorCode.DEVICE_UNAVAILABLE,
            "The HDMI matrix service is not running",
            {"reason": "not_started"},
        )
    return service


def get_mixer(request: Request) -> MixerService:
    """The running mixer service (§7.3, §16.5).

    Mirrors :func:`get_lighting`: a request that arrives before the lifespan
    has built it — or after a failed boot — is answered ``device_unavailable``
    rather than a 500, since nothing is wrong with the request itself.
    """
    service: MixerService | None = getattr(request.app.state, "mixer", None)
    if service is None:
        raise ApiError(
            ErrorCode.DEVICE_UNAVAILABLE,
            "The mixer service is not running",
            {"reason": "not_started"},
        )
    return service


def get_projector(request: Request) -> ProjectorService:
    """The running projector service (§7.4, §16.5).

    Mirrors :func:`get_lighting`: a request that arrives before the lifespan
    has built it — or after a failed boot — is answered ``device_unavailable``
    rather than a 500, since nothing is wrong with the request itself.
    """
    service: ProjectorService | None = getattr(request.app.state, "projector", None)
    if service is None:
        raise ApiError(
            ErrorCode.DEVICE_UNAVAILABLE,
            "The projector service is not running",
            {"reason": "not_started"},
        )
    return service


def get_helper(request: Request) -> HelperClient:
    """The application's side of the privileged helper (contracts §2).

    Always present once the app is built (``create_app`` constructs it
    unconditionally, the same as ``tokens``/``limiter``) — this raises only
    for a bare test app that skips ``create_app`` entirely.
    """
    helper: HelperClient | None = getattr(request.app.state, "helper", None)
    if helper is None:
        raise ApiError(
            ErrorCode.INTERNAL_ERROR,
            "The privileged helper is not available",
            {"reason": "not_started"},
        )
    return helper


def get_hirer_access(request: Request) -> HirerAccess:
    """The owner of hirer access and revocation (§6.6)."""
    access: HirerAccess = request.app.state.hirer_access
    return access


async def settle_hirer_permissions(request: Request, reason: str) -> None:
    """Rebuild ``state.hirer`` before a configuration write is answered (§6.7).

    The resolver also rebuilds on the configuration event, off the request
    path. That alone leaves a window after the admin's save is answered in
    which a hirer's write is still judged by the snapshot it replaced: a
    channel just taken off a page, or a ceiling just lowered, would still be
    written. §6.7 says an admin's edits "take effect immediately", and an
    admin who removes a channel because it is being misused must not watch it
    carry on. Awaiting a rebuild here means that by the time the admin is
    answered, every enforcement point reads the new snapshot. The event's own
    rebuild follows and finds nothing left to change.
    """
    resolver: HirerPermissionResolver | None = getattr(request.app.state, "hirer_permissions", None)
    if resolver is not None and resolver.started:
        await resolver.rebuild(reason=reason)


def get_first_run(request: Request) -> FirstRunFlag:
    flag: FirstRunFlag = request.app.state.first_run
    return flag


# -- KNX ------------------------------------------------------------------------------


def get_knx(request: Request) -> KnxSubsystem:
    """The running KNX subsystem (§7.1); 503 when it has not started.

    Mirrors :func:`get_devices`: endpoints that genuinely need a live knxd
    connection (the telegram monitor, test-write) are answered
    ``device_unavailable`` rather than crashing when the subsystem is not on
    ``app.state`` yet — nothing is wrong with the request.
    """
    subsystem: KnxSubsystem | None = getattr(request.app.state, "knx", None)
    if subsystem is None:
        raise ApiError(
            ErrorCode.DEVICE_UNAVAILABLE,
            "The KNX subsystem is not running",
            {"reason": "not_started"},
        )
    return subsystem


def get_knx_optional(request: Request) -> KnxSubsystem | None:
    """The running KNX subsystem, or ``None`` — for endpoints (the address
    library CRUD) that are useful with or without a live knxd connection."""
    subsystem: KnxSubsystem | None = getattr(request.app.state, "knx", None)
    return subsystem


def get_knx_registry(request: Request) -> DbAddressRegistry:
    """The database-backed KNX address registry (§7.1, §15.7); 503 when absent."""
    registry: DbAddressRegistry | None = getattr(request.app.state, "knx_registry", None)
    if registry is None:
        raise ApiError(
            ErrorCode.DEVICE_UNAVAILABLE,
            "The KNX address registry is not available",
            {"reason": "not_started"},
        )
    return registry


def get_knx_import_sessions(request: Request) -> ImportSessionStore:
    """The in-process store of pending import previews (§7.1, §21.19)."""
    sessions: ImportSessionStore = request.app.state.knx_import_sessions
    return sessions


# -- first-run gate (§10.4, Q4) -----------------------------------------------------


FIRST_RUN_INCOMPLETE_REASON: Final = "first_run_incomplete"
FIRST_RUN_MESSAGE: Final = (
    "The controller has not been set up yet. Complete first-run setup at /setup."
)


def _path_matches(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


class FirstRunGateMiddleware:
    """Refuse every gated route until the first-run wizard commits (§10.4, Q4).

    The vocabulary of §16.1 is closed and has no ``setup_required`` code, and
    it does not grow for this: the refusal is ``403 permission_denied`` with
    ``detail.reason = "first_run_incomplete"``, which the client turns into a
    redirect to ``/setup``. Enforced here rather than in the client so a
    scripted caller hitting an API route during first run is refused too.

    Only ``gated_prefix`` is gated — ``/health`` sits outside it and stays
    reachable for monitors and the reconnection screen — minus ``exempt``,
    which the wizard, the login endpoints and the wizard's device step need.
    Pure ASGI, and the answer comes from the cached :class:`FirstRunFlag`, so
    the common path (setup long since complete) costs one attribute read.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        gated_prefix: str,
        exempt: Sequence[str] = (),
    ) -> None:
        self.app = app
        self.gated_prefix = gated_prefix
        self.exempt = tuple(exempt)

    def gates(self, path: str) -> bool:
        if not _path_matches(path, self.gated_prefix):
            return False
        return not any(_path_matches(path, prefix) for prefix in self.exempt)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and self.gates(str(scope.get("path", ""))):
            state = getattr(scope.get("app"), "state", None)
            flag: FirstRunFlag | None = getattr(state, "first_run", None)
            db: Database | None = getattr(state, "db", None)
            if flag is not None and db is not None and await flag.is_first_run(db):
                response = error_response(
                    Request(scope),
                    ErrorCode.PERMISSION_DENIED,
                    FIRST_RUN_MESSAGE,
                    {"reason": FIRST_RUN_INCOMPLETE_REASON, "setup_path": "/setup"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


# -- session ------------------------------------------------------------------------


def _unauthenticated(reason: str, message: str) -> ApiError:
    return ApiError(ErrorCode.UNAUTHENTICATED, message, {"reason": reason})


async def _claims_from_cookie(
    cookie: str | None, tokens: TokenService, db: Database, access: HirerAccess
) -> TokenClaims:
    if not cookie:
        raise TokenError("missing", "Sign in to continue")
    claims = tokens.decode(cookie)
    await check_live_version(db, claims, access)
    return claims


async def session_claims(request: Request) -> TokenClaims:
    """The identity behind the request's cookie, or 401 ``unauthenticated``.

    ``detail.reason`` is one of ``missing``, ``invalid``, ``expired``,
    ``revoked`` (staff token_version changed) or ``hirer_revoked`` (PIN changed
    or access disabled), so the client can choose between the
    re-authentication overlay and a redirect (§6.5).
    """
    try:
        return await _claims_from_cookie(
            request.cookies.get(COOKIE_NAME),
            get_tokens(request),
            get_db(request),
            get_hirer_access(request),
        )
    except TokenError as exc:
        raise _unauthenticated(exc.reason, exc.message) from exc


async def current_session(request: Request) -> AsyncIterator[TokenClaims]:
    """:func:`session_claims` as a dependency; a hirer holds an access slot throughout.

    The slot is taken with no ``await`` after the check, so admission and the
    slot are one step, and it is released however the request ends.
    """
    claims = await session_claims(request)
    if not claims.is_hirer:
        yield claims
        return
    access = get_hirer_access(request)
    try:
        access.hold(claims)
    except TokenError as exc:
        raise _unauthenticated(exc.reason, exc.message) from exc
    try:
        yield claims
    finally:
        access.release()


TierGate = Callable[[Request, TokenClaims], Awaitable[TokenClaims]]

#: Every gate :func:`require_tier` has built, and the tiers it admits. Read by
#: :func:`admitted_tiers`, so the route table can be checked against an
#: explicit list of decisions rather than trusted route by route.
_GATE_TIERS: dict[Callable[..., object], frozenset[str]] = {}

#: What :func:`admitted_tiers` answers for a route that needs a session of any
#: tier but no particular one.
ANY_SESSION: Final[frozenset[str]] = frozenset({"admin", "operator", "hirer"})

PERMISSION_DENIED_MESSAGE: Final = "This action is not available to your account"


def admitted_tiers(call: Callable[..., object] | None) -> frozenset[str] | None:
    """The tiers a dependency admits: a gate's own, :data:`ANY_SESSION` for
    :func:`current_session`, and ``None`` for anything that is not a gate."""
    if call is None:
        return None
    if call is current_session:
        return ANY_SESSION
    return _GATE_TIERS.get(call)


def require_tier(*tiers: str) -> TierGate:
    """A dependency that admits only ``tiers``; otherwise 403 and an audit row."""
    allowed = frozenset(tiers)
    if not allowed:
        raise ValueError("require_tier needs at least one tier")

    async def gate(
        request: Request, claims: Annotated[TokenClaims, Depends(current_session)]
    ) -> TokenClaims:
        if claims.tier in allowed:
            return claims
        await record_event(
            get_db(request),
            "permission_denied",
            user_ident=claims.tier,
            ip_address=client_ip(request),
            detail={
                "method": request.method,
                "path": request.url.path,
                "required": sorted(allowed),
                "session_id": claims.session_id,
            },
        )
        raise ApiError(
            ErrorCode.PERMISSION_DENIED,
            PERMISSION_DENIED_MESSAGE,
            {"reason": "tier", "required": sorted(allowed)},
        )

    gate.__name__ = f"require_{'_or_'.join(sorted(allowed))}"
    _GATE_TIERS[gate] = allowed
    return gate


require_staff = require_tier("admin", "operator")
require_admin = require_tier("admin")
#: Pages (§16.5): staff plus a hirer, whose reach within a page is narrowed
#: by the permission resolver (state.hirer.permissions) and button_reachable,
#: not by this gate.
require_staff_or_hirer = require_tier("admin", "operator", "hirer")
#: The control routes a hirer shares with staff (§16.5 as the phase-5
#: contract narrows it). Admission is only the first check: a hirer's request
#: is then held to what ``state.hirer`` lets them reach, target by target
#: (:mod:`proskenion.core.hirer_enforcement`).
require_control = require_tier("admin", "operator", "hirer")


async def refuse_hirer(
    request: Request,
    claims: TokenClaims,
    *,
    domain: str,
    target_id: int | None,
    reason: str,
) -> ApiError:
    """Audit a hirer request for a target out of their reach; return the 403 to raise.

    The tier gate admitted the route; this is the per-target refusal behind it
    (§6.7). ``reason`` says which check failed — ``unreachable``,
    ``not_writable`` or ``colour_disabled`` — in the audit row only: the
    answer itself is the plain ``permission_denied`` a tier refusal gets, so
    it tells a probing client nothing about what exists.
    """
    await record_event(
        get_db(request),
        "permission_denied",
        user_ident=claims.tier,
        ip_address=client_ip(request),
        detail={
            "method": request.method,
            "path": request.url.path,
            "domain": domain,
            "id": target_id,
            "reason": reason,
            "session_id": claims.session_id,
        },
    )
    return ApiError(ErrorCode.PERMISSION_DENIED, PERMISSION_DENIED_MESSAGE)


# -- WebSocket upgrade (§6.12) -------------------------------------------------------


class WebSocketDenied(Exception):
    """The upgrade was refused; the denial has already been sent."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(f"websocket upgrade refused: {status} {reason}")
        self.status = status
        self.reason = reason


#: uvicorn's own log line — verbatim, from
#: ``uvicorn/protocols/websockets/{websockets_sansio_impl,wsproto_impl}.py``
#: — for the *harmless* case this module's :class:`_DenialHandshakeLogFilter`
#: exists to demote. See that class for why it is safe to match on the bare
#: text rather than anything more specific.
_HANDSHAKE_LOG_MESSAGE: Final = "ASGI callable returned without completing handshake."


class _DenialHandshakeLogFilter(logging.Filter):
    """Demotes uvicorn's spurious ERROR after every ``WS /ws`` refusal sent
    through :meth:`WebSocket.send_denial_response` (:func:`_deny`, the
    401/403 path §16.8 and §6.12 call for) to INFO, without changing what is
    delivered on the wire.

    Confirmed directly against this application's pinned uvicorn/starlette
    (a real ``uvicorn.Server``, a real ``websockets`` client, `uvicorn.error`
    captured): a refused upgrade delivers the correct HTTP status —
    ``websocket.http.response.start``/``.body`` reach the client exactly as
    401 or 403 — but uvicorn's own bookkeeping only marks a connection's
    handshake "complete" for ``websocket.accept`` and ``websocket.close``,
    never for that pair of messages. So ``run_asgi()`` logs this ERROR after
    every single refusal, roughly every 30 s once a client is stuck retrying
    one (the symptom seen on the CM5, 25 Sep) — even though nothing failed.

    Matching on the literal message is safe *in this application* because
    every return from :func:`websocket_endpoint` that has not called
    ``accept()`` first goes through :func:`_deny`; nothing else in
    ``proskenion.api.ws`` can produce this exact uvicorn log line. A genuine
    crash inside the endpoint is a different uvicorn log line entirely
    ("Exception in ASGI application"), which this filter does not touch.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # ``endswith`` rather than ``==``: proskenion.core.timesync installs a
        # log record factory that prepends "[unverified time] " to every
        # record while NTP has not yet verified the clock (§4.9) — including
        # this one, on a boot early enough that a refusal happens before
        # synchronisation finishes. The suffix is what uvicorn actually
        # controls and is what stays stable either way.
        if record.levelno == logging.ERROR and record.getMessage().endswith(_HANDSHAKE_LOG_MESSAGE):
            record.levelno = logging.INFO
            record.levelname = "INFO"
        return True  # never dropped — only ever demoted, so nothing goes missing


def install_denial_log_filter() -> None:
    """Attach :class:`_DenialHandshakeLogFilter` to ``uvicorn.error`` once.

    Called from :func:`proskenion.api.app.create_app`. Idempotent — a second
    call (every test that builds another app in the same process) is a
    no-op, checked by type rather than identity so it survives module
    reloads.
    """
    logger = logging.getLogger("uvicorn.error")
    if not any(isinstance(f, _DenialHandshakeLogFilter) for f in logger.filters):
        logger.addFilter(_DenialHandshakeLogFilter())


async def _deny(websocket: WebSocket, status: int, reason: str) -> WebSocketDenied:
    body = json.dumps({"error": {"code": _code_for(status), "message": reason}})
    try:
        await websocket.send_denial_response(
            Response(body, status_code=status, media_type="application/json")
        )
    except RuntimeError:
        # The server does not support the denial-response extension; the
        # handshake still fails, as an HTTP 403.
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION, reason=reason)
    return WebSocketDenied(status, reason)


def _code_for(status: int) -> str:
    return ErrorCode.UNAUTHENTICATED.value if status == 401 else ErrorCode.PERMISSION_DENIED.value


async def authenticate_websocket(websocket: WebSocket) -> TokenClaims:
    """Validate an upgrade request before ``accept()`` (§6.12).

    The Origin is checked against the configured hostname (403), then the
    cookie JWT and the live ``token_version`` (401). On refusal the denial is
    sent and :class:`WebSocketDenied` raised so the endpoint simply returns.
    """
    app = websocket.app
    config: Config = app.state.config
    db: Database = app.state.db
    tokens: TokenService = app.state.tokens
    access: HirerAccess = app.state.hirer_access
    ip = client_address(websocket.scope)

    origin = websocket.headers.get("origin")
    if not origin_is_expected(origin, config.server.hostname):
        await record_event(
            db,
            "unexpected_origin",
            user_ident=None,
            ip_address=ip,
            detail={"origin": origin, "path": websocket.url.path, "websocket": True},
        )
        raise await _deny(websocket, 403, "Origin not permitted")

    try:
        return await _claims_from_cookie(websocket.cookies.get(COOKIE_NAME), tokens, db, access)
    except TokenError as exc:
        raise await _deny(websocket, 401, exc.message) from exc
