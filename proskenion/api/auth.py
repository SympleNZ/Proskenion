"""Authentication endpoints (spec §16.3, §6.3–§6.5, §6.8, §6.14).

::

    POST /auth/login             [public]  { password } → { tier, expires_at }
    POST /auth/hirer             [public]  { pin }      → { tier, expires_at }
    POST /auth/logout            [any]
    POST /auth/change-password   [admin, operator]  { current_password, new_password }
    POST /auth/operator-password [admin]  { current_password, new_password }
    GET  /auth/password-status   [admin]  → { admin, operator, identical }
    GET  /auth/session           [any]     → { tier, expires_at, absolute_expires_at, server_time,
                                                certificate }

There is one password field and no username: both staff hashes are always
verified and admin wins (§6.3). The token travels in an httpOnly,
``SameSite=Strict``, ``Secure`` cookie — never a response body, never
localStorage (§6.4). ``Secure`` is dropped only under a development
configuration, where there is no nginx terminating TLS.

``GET /auth/session`` is the one thing that extends a session: it re-issues
the cookie with a fresh 30-minute idle window, never past the absolute
12-hour cap. The browser calls it every five minutes while the page is
visible, and nothing else — no middleware, no WebSocket frame — touches the
expiry (§6.4, B65, phase-1 plan Q3).

Every attempt writes a ``security_events`` row (§6.14) with the real client
address (§4.13).

Admin → Users (§21.23) has two cards and no creating or deleting. Each
staff tier changes its own password with ``POST /auth/change-password``,
requiring its own current password — the operator's own card in the account
popover, and the admin's own card here, both go through it. The admin's
card for the *operator* is different: an admin who does not know (or whose
operator has forgotten) the operator's password still needs to be able to
set a new one, so ``POST /auth/operator-password`` asks for the *admin's*
own current password instead. This is deliberately the stricter of two
readings of "changing a password requires the current one" — requiring the
operator's own password there would be unusable the moment it is actually
needed (an admin who already knows the operator's password has little
reason to force a change), and it is the reading that keeps the rule's own
stated purpose ("stops someone at an unattended device") intact for
whoever is actually sitting at this session. ``GET /auth/password-status``
backs the two cards' "Password last changed" line and the informational
note when both passwords end up the same (§21.23) — never a hash, and
never a plaintext.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from proskenion.api.deps import (
    FIRST_RUN_INCOMPLETE_REASON,
    FIRST_RUN_MESSAGE,
    client_ip,
    get_config,
    get_db,
    get_first_run,
    get_hirer_access,
    get_tokens,
    require_admin,
    require_staff,
    session_claims,
)
from proskenion.api.errors import ApiError, ErrorCode
from proskenion.config import Config
from proskenion.core import auth, certs
from proskenion.core.auth import TokenClaims, TokenService, record_event
from proskenion.core.hirer_access import HirerAccess
from proskenion.core.ratelimit import LockedOut, RateLimiter, Scope
from proskenion.core.setup import FirstRunFlag
from proskenion.db.connection import Database
from proskenion.db.crud import hirer as hirer_crud
from proskenion.db.crud import password_state as password_state_crud
from proskenion.db.crud import users as users_crud

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

HIRER_DISABLED_MESSAGE: Final = (
    "Hire guest access is not currently available. Please contact venue staff."
)
INCORRECT_PASSWORD_MESSAGE: Final = "Incorrect password"
INCORRECT_PIN_MESSAGE: Final = "Incorrect PIN"


# -- bodies and responses -------------------------------------------------------


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginBody(_Body):
    password: str = Field(min_length=1)


class HirerBody(_Body):
    pin: str = Field(pattern=rf"^[0-9]{{{auth.PIN_LENGTH}}}$")


class ChangePasswordBody(_Body):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=auth.MIN_PASSWORD_LENGTH)


class LoginResponse(BaseModel):
    tier: str
    expires_at: str


class SessionResponse(BaseModel):
    tier: str
    expires_at: str
    absolute_expires_at: str
    server_time: str
    certificate: certs.CertificateTrust


class ChangeOperatorPasswordBody(_Body):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=auth.MIN_PASSWORD_LENGTH)


class OperatorPasswordResponse(BaseModel):
    tier: str
    password_changed_at: str | None


class TierPasswordStatus(BaseModel):
    password_changed_at: str | None


class PasswordStatusResponse(BaseModel):
    admin: TierPasswordStatus
    operator: TierPasswordStatus
    #: Whether the two staff passwords are currently the same (§21.23) —
    #: computed and cached at change time (``password_state``), never by
    #: comparing the two stored hashes.
    identical: bool


# -- helpers ----------------------------------------------------------------------


def get_limiter(request: Request) -> RateLimiter:
    limiter: RateLimiter = request.app.state.limiter
    return limiter


def set_session_cookie(response: Response, token: str, *, config: Config) -> None:
    """Attach the session cookie with the §6.4 flags."""
    response.set_cookie(
        key=auth.COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="strict",
        secure=not config.is_development,
        path="/",
    )


def clear_session_cookie(response: Response, *, config: Config) -> None:
    response.delete_cookie(
        key=auth.COOKIE_NAME,
        httponly=True,
        samesite="strict",
        secure=not config.is_development,
        path="/",
    )


def _rate_limited(exc: LockedOut) -> ApiError:
    return ApiError(
        ErrorCode.RATE_LIMITED,
        "Too many attempts. Try again later.",
        {"retry_after": exc.retry_after},
        headers={"Retry-After": str(exc.retry_after)},
    )


async def _check_limit(limiter: RateLimiter, scope: Scope, ip: str) -> None:
    try:
        limiter.check(scope, ip)
    except LockedOut as exc:
        raise _rate_limited(exc) from exc


async def _failed(db: Database, limiter: RateLimiter, scope: Scope, ip: str, reason: str) -> None:
    """Record a failed attempt: the audit row, the counter and any lockout it starts."""
    locked = limiter.record_failure(scope, ip)
    await record_event(
        db,
        "login_failure",
        user_ident=ip,
        ip_address=ip,
        detail={"scope": scope.value, "reason": reason},
    )
    if locked:
        policy = limiter.policy(scope)
        await record_event(
            db,
            "lockout",
            user_ident=ip,
            ip_address=ip,
            detail={"scope": scope.value, "lockout_seconds": int(policy.lockout_s)},
        )


def _login_response(
    response: Response, tokens: TokenService, config: Config, token: str, claims: TokenClaims
) -> LoginResponse:
    set_session_cookie(response, token, config=config)
    return LoginResponse(tier=claims.tier, expires_at=auth.iso(claims.expires_at))


# -- endpoints --------------------------------------------------------------------


@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginBody,
    request: Request,
    response: Response,
    db: Annotated[Database, Depends(get_db)],
    tokens: Annotated[TokenService, Depends(get_tokens)],
    config: Annotated[Config, Depends(get_config)],
    limiter: Annotated[RateLimiter, Depends(get_limiter)],
) -> LoginResponse:
    """Staff sign-in: one password, both hashes verified, admin wins (§6.3)."""
    ip = client_ip(request)
    await _check_limit(limiter, Scope.STAFF_LOGIN, ip)

    admin = await users_crud.get_by_tier(db, "admin")
    operator = await users_crud.get_by_tier(db, "operator")
    if admin is None or operator is None:  # pragma: no cover - the seed guarantees both
        raise RuntimeError("staff accounts are missing")

    # Both are always verified — constant work regardless of outcome.
    admin_match, operator_match = await asyncio.gather(
        auth.verify_secret_async(body.password, admin.password),
        auth.verify_secret_async(body.password, operator.password),
    )

    if admin_match:
        matched = admin
    elif operator_match:
        matched = operator
    else:
        await _failed(db, limiter, Scope.STAFF_LOGIN, ip, "password")
        raise ApiError(ErrorCode.UNAUTHENTICATED, INCORRECT_PASSWORD_MESSAGE)

    limiter.record_success(Scope.STAFF_LOGIN, ip)
    token, claims = tokens.issue(matched.tier, matched.token_version)
    await record_event(
        db,
        "login_success",
        user_ident=matched.tier,
        ip_address=ip,
        detail={"tier": matched.tier, "absolute_expires_at": auth.iso(claims.absolute_expires_at)},
    )
    return _login_response(response, tokens, config, token, claims)


@router.post("/hirer", response_model=LoginResponse)
async def hirer_login(
    body: HirerBody,
    request: Request,
    response: Response,
    db: Annotated[Database, Depends(get_db)],
    tokens: Annotated[TokenService, Depends(get_tokens)],
    config: Annotated[Config, Depends(get_config)],
    limiter: Annotated[RateLimiter, Depends(get_limiter)],
    access: Annotated[HirerAccess, Depends(get_hirer_access)],
) -> LoginResponse:
    """Hirer sign-in by PIN. Refused while access is disabled (§6.2, §15.4).

    Whether access is enabled is read from ``state.hirer``, like every other
    hirer check, and read again after the PIN is verified: bcrypt takes long
    enough for the kill switch or a PIN change to land meanwhile, and a token
    issued across it would be dead on arrival.
    """
    ip = client_ip(request)
    await _check_limit(limiter, Scope.HIRER_PIN, ip)

    await access.ensure_loaded(db)
    if not access.enabled:
        raise await _hirer_disabled(db, ip)

    hirer = await hirer_crud.get(db)
    if not await auth.verify_secret_async(body.pin, hirer.pin):
        await _failed(db, limiter, Scope.HIRER_PIN, ip, "pin")
        raise ApiError(ErrorCode.UNAUTHENTICATED, INCORRECT_PIN_MESSAGE)

    if not access.enabled:
        raise await _hirer_disabled(db, ip)
    if access.token_version != hirer.token_version:
        # The PIN changed while this one was being checked: the PIN that
        # matched is no longer the PIN. Not the caller's attempt to count.
        await record_event(
            db,
            "login_failure",
            user_ident=ip,
            ip_address=ip,
            detail={"scope": Scope.HIRER_PIN.value, "reason": "pin_changed"},
        )
        raise ApiError(ErrorCode.UNAUTHENTICATED, INCORRECT_PIN_MESSAGE)

    limiter.record_success(Scope.HIRER_PIN, ip)
    token, claims = tokens.issue("hirer", hirer.token_version)
    await record_event(
        db,
        "login_success",
        user_ident="hirer",
        ip_address=ip,
        detail={
            "tier": "hirer",
            "session_id": claims.session_id,
            "absolute_expires_at": auth.iso(claims.absolute_expires_at),
        },
    )
    return _login_response(response, tokens, config, token, claims)


async def _hirer_disabled(db: Database, ip: str) -> ApiError:
    await record_event(
        db,
        "login_failure",
        user_ident=ip,
        ip_address=ip,
        detail={"scope": Scope.HIRER_PIN.value, "reason": "hirer_disabled"},
    )
    return ApiError(
        ErrorCode.PERMISSION_DENIED, HIRER_DISABLED_MESSAGE, {"reason": "hirer_disabled"}
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def logout(config: Annotated[Config, Depends(get_config)]) -> Response:
    """Clear the cookie. Accepted whether or not the token is still valid."""
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookie(response, config=config)
    return response


@router.post("/change-password", response_model=LoginResponse)
async def change_password(
    body: ChangePasswordBody,
    request: Request,
    response: Response,
    claims: Annotated[TokenClaims, Depends(require_staff)],
    db: Annotated[Database, Depends(get_db)],
    tokens: Annotated[TokenService, Depends(get_tokens)],
    config: Annotated[Config, Depends(get_config)],
) -> LoginResponse:
    """Change the caller's own password and bump ``token_version`` (§16.3).

    Every other session for the tier ends; the caller's cookie is re-issued
    with the new version so they are not signed out by their own change.
    """
    ip = client_ip(request)
    user = await users_crud.get_by_tier(db, claims.tier)
    if user is None:  # pragma: no cover - the seed guarantees both
        raise RuntimeError("staff account is missing")

    if not await auth.verify_secret_async(body.current_password, user.password):
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            INCORRECT_PASSWORD_MESSAGE,
            {
                "fields": [
                    {
                        "field": "current_password",
                        "message": INCORRECT_PASSWORD_MESSAGE,
                        "type": "incorrect",
                    }
                ]
            },
        )

    updated = await auth.set_staff_password(db, claims.tier, body.new_password)
    await record_event(
        db,
        "password_changed",
        user_ident=claims.tier,
        ip_address=ip,
        detail={"tier": claims.tier, "token_version": updated.token_version},
    )
    token, fresh = tokens.reissue(claims, token_version=updated.token_version)
    return _login_response(response, tokens, config, token, fresh)


@router.post("/operator-password", response_model=OperatorPasswordResponse)
async def change_operator_password(
    body: ChangeOperatorPasswordBody,
    request: Request,
    claims: Annotated[TokenClaims, Depends(require_admin)],
    db: Annotated[Database, Depends(get_db)],
) -> OperatorPasswordResponse:
    """Admin → Users' operator card, from the admin's own session (§21.23).

    ``current_password`` is the *admin's* own — see the module docstring for
    why. Bumps the operator's ``token_version``, ending every operator
    session; the admin's own session is untouched, since this never issues
    an operator token.
    """
    ip = client_ip(request)
    admin = await users_crud.get_by_tier(db, claims.tier)
    if admin is None:  # pragma: no cover - the seed guarantees both
        raise RuntimeError("staff account is missing")

    if not await auth.verify_secret_async(body.current_password, admin.password):
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            INCORRECT_PASSWORD_MESSAGE,
            {
                "fields": [
                    {
                        "field": "current_password",
                        "message": INCORRECT_PASSWORD_MESSAGE,
                        "type": "incorrect",
                    }
                ]
            },
        )

    updated = await auth.set_staff_password(db, "operator", body.new_password)
    await record_event(
        db,
        "password_changed",
        user_ident="operator",
        ip_address=ip,
        detail={"tier": "operator", "token_version": updated.token_version, "changed_by": "admin"},
    )
    return OperatorPasswordResponse(
        tier="operator", password_changed_at=updated.password_changed_at
    )


@router.get("/password-status", response_model=PasswordStatusResponse)
async def password_status(
    claims: Annotated[TokenClaims, Depends(require_admin)],
    db: Annotated[Database, Depends(get_db)],
) -> PasswordStatusResponse:
    """Admin → Users' two "Password last changed" lines and the identical-
    passwords note (§21.23). Never a hash, never a plaintext."""
    admin = await users_crud.get_by_tier(db, "admin")
    operator = await users_crud.get_by_tier(db, "operator")
    if admin is None or operator is None:  # pragma: no cover - the seed guarantees both
        raise RuntimeError("staff accounts are missing")
    state = await password_state_crud.get(db)
    return PasswordStatusResponse(
        admin=TierPasswordStatus(password_changed_at=admin.password_changed_at),
        operator=TierPasswordStatus(password_changed_at=operator.password_changed_at),
        identical=state.identical,
    )


@router.get("/session", response_model=SessionResponse)
async def session(
    request: Request,
    response: Response,
    tokens: Annotated[TokenService, Depends(get_tokens)],
    config: Annotated[Config, Depends(get_config)],
    db: Annotated[Database, Depends(get_db)],
    first_run: Annotated[FirstRunFlag, Depends(get_first_run)],
) -> SessionResponse:
    """Current tier and expiry; re-issues the cookie with a fresh idle window.

    The client calls this only while the page is visible (§6.4). The new
    ``exp`` never passes ``aexp``, which is carried forward unchanged.

    ``/auth`` is exempt from the first-run gate so signing in still works
    during commissioning (§16.4, Q4) — which means this is the one request an
    anonymous page makes at boot. Without a valid session it therefore
    distinguishes the two reasons instead of always answering
    ``unauthenticated``: while first run is incomplete the answer is
    ``403 permission_denied`` / ``first_run_incomplete``, the same shape every
    other gated route already returns, so the client's existing redirect
    fires before the login page ever renders; once setup has committed the
    answer reverts to ``401 unauthenticated`` as before. A valid session
    always answers ``200``, first run or not — the wizard signs in at step 2
    and needs this to keep working.
    """
    try:
        claims = await session_claims(request)
    except ApiError as exc:
        if exc.code is ErrorCode.UNAUTHENTICATED and await first_run.is_first_run(db):
            raise ApiError(
                ErrorCode.PERMISSION_DENIED,
                FIRST_RUN_MESSAGE,
                {"reason": FIRST_RUN_INCOMPLETE_REASON, "setup_path": "/setup"},
            ) from exc
        raise
    token, fresh = tokens.reissue(claims)
    set_session_cookie(response, token, config=config)
    return SessionResponse(
        tier=fresh.tier,
        expires_at=auth.iso(fresh.expires_at),
        absolute_expires_at=auth.iso(fresh.absolute_expires_at),
        server_time=auth.iso(tokens.now()),
        certificate=await _certificate(request, config),
    )


async def _certificate(request: Request, config: Config) -> certs.CertificateTrust:
    """``trusted`` or ``self_signed``, from the certificate nginx serves (§6.16).

    The client uses it to suppress the install prompt, which iOS refuses over
    an untrusted certificate (§21.8). See :func:`certs.served_trust`.

    ``config.server.hostname`` is ``None`` on the appliance (§4.14's
    bootstrap carries no ``[server] hostname``), which used to make this
    always answer ``self_signed`` regardless of what nginx was actually
    serving, since :func:`certs.served_trust` had no hostname to look a
    certificate up under. The running certificate manager already resolves
    the real served name the same way
    (``config.server.hostname or served_hostnames()[0]``) — reused here via
    :attr:`~proskenion.core.certs.CertificateManager.hostname` rather than
    re-derived; :func:`certs.served_hostnames` itself is the fallback where
    no manager is wired (a minimal test app).
    """
    platform = getattr(request.app.state, "platform", None)
    data_dir = platform.data_dir() if platform is not None else config.app.data_dir
    manager: certs.CertificateManager | None = getattr(request.app.state, "certs", None)
    if manager is not None:
        hostname = manager.hostname
    elif config.server.hostname:
        hostname = config.server.hostname
    else:
        served = await asyncio.to_thread(certs.served_hostnames, certs.NGINX_SITE_CONFIG)
        hostname = served[0] if served else None
    return await certs.served_trust(data_dir, hostname, development=config.is_development)
