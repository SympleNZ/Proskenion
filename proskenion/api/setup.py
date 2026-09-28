"""First-run wizard endpoints (spec §16.4, §10.4).

::

    GET  /setup/state       [public, first-run only]  completed steps, next step
    POST /setup/step/{n}    [public, first-run only]  submit one step
    POST /setup/complete    [public, first-run only]  commit; sets first_run_completed

All three are public because there is no credential to present until step 2
has run — this is the bootstrap path (§10.4). All three return ``403
permission_denied`` with ``detail.reason = "setup_complete"`` once
``first_run_completed`` is true (§16.4); re-running the wizard requires a
database reset.

Because they are public, ``POST /setup/step/2`` — the endpoint that sets the
admin password — carries a rate limit: :attr:`Scope.SETUP_STEP`, the hirer PIN
policy of §6.8 (three attempts per ten minutes, thirty-minute lockout) under
its own scope. Without it the bootstrap path would be an unauthenticated
password-setting endpoint left open on the network for as long as the
appliance sits uncommissioned. Every submission counts, not only rejected
ones: an attacker's attempt and an installer's are indistinguishable from
here, and an installer needs one.

The bodies differ per step, so ``POST /setup/step/{n}`` takes the raw JSON
object and validates it against that step's model; a bad password, a
mismatched confirmation and a step submitted out of order all come back as
``422 validation_failed`` with per-field detail.

Step 2 is the exception to "no credential yet": once it accepts the admin
password it signs the wizard in, the same way ``POST /auth/login`` does, so
the response carries ``tier`` and ``expires_at`` and sets the admin session
cookie (§6.4). Steps 3 onward run under that session; step 4 (devices) needs
it, because it drives the admin-tier devices API directly (§10.4 step 4,
§16.7) rather than through a setup endpoint of its own.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any, Final

from fastapi import APIRouter, Body, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from proskenion.api.auth import set_session_cookie
from proskenion.api.deps import client_ip, get_config, get_db, get_first_run, get_tokens
from proskenion.api.errors import ApiError, ErrorCode
from proskenion.config import Config
from proskenion.core import certs, setup, system_config
from proskenion.core.auth import TokenService, iso, record_event
from proskenion.core.bus import EventBus
from proskenion.core.devices import DeviceManager
from proskenion.core.events import MixerConfigChanged
from proskenion.core.helper import HelperClient
from proskenion.core.mixer import service as mixer_service
from proskenion.core.mixer.desk_channels import add_missing_channels
from proskenion.core.platform import Platform, detect_platform
from proskenion.core.ratelimit import LockedOut, RateLimiter, Scope
from proskenion.core.secrets import DEFAULT_SECRET_PATH, DeviceSecret, generate_secret_if_missing
from proskenion.core.setup import FirstRunFlag, SetupAlreadyComplete, Step, StepRejected
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import mixer as mixer_crud
from proskenion.db.crud import users as users_crud

log = logging.getLogger(__name__)

router = APIRouter(prefix="/setup", tags=["setup"])

SETUP_COMPLETE_REASON: Final = "setup_complete"


# -- bodies -------------------------------------------------------------------------


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WelcomeBody(_Body):
    locale: str = Field(min_length=1)
    timezone: str = setup.SUPPORTED_TIMEZONE


class PasswordBody(_Body):
    # Length is checked in the core so the refusal carries §16.1 field detail
    # rather than pydantic's, and so both password steps refuse identically.
    password: str
    password_confirm: str


class NetworkBody(_Body):
    address: str | None = None
    hostname: str | None = None
    skipped: bool = False
    #: Present together, and only when the address is actually being
    #: changed: without all three, step 3 behaves exactly as it did
    #: before — a review, never an apply.
    prefix_length: int | None = Field(default=None, ge=0, le=32)
    gateway: str | None = None
    dns: list[str] = Field(default_factory=list)


class DevicesBody(_Body):
    device_ids: list[int] = Field(default_factory=list)
    skipped: bool = False


class CertificateBody(_Body):
    option: str = "self_signed"
    hostname: str | None = None
    #: Cloudflare API token (Q7) — write-only, used once and never echoed back.
    token: str | None = None


class ReviewBody(_Body):
    reviewed: bool = True


# -- helpers ------------------------------------------------------------------------


def _validation_error(message: str, fields: list[dict[str, str]]) -> ApiError:
    return ApiError(ErrorCode.VALIDATION_FAILED, message, {"fields": fields})


def _from_pydantic(exc: ValidationError) -> ApiError:
    fields = [
        {
            "field": ".".join(str(part) for part in error["loc"]),
            "message": error["msg"],
            "type": error["type"],
        }
        for error in exc.errors()
    ]
    return _validation_error("The request failed validation", fields)


def _parse[BodyT: BaseModel](model: type[BodyT], body: dict[str, Any] | None) -> BodyT:
    try:
        return model.model_validate(body or {})
    except ValidationError as exc:
        raise _from_pydantic(exc) from exc


def _setup_complete() -> ApiError:
    return ApiError(
        ErrorCode.PERMISSION_DENIED,
        setup.RESET_REQUIRED_MESSAGE,
        {"reason": SETUP_COMPLETE_REASON},
    )


async def require_first_run(
    db: Annotated[Database, Depends(get_db)],
    flag: Annotated[FirstRunFlag, Depends(get_first_run)],
) -> None:
    """Refuse every ``/setup`` route once the wizard has committed (§16.4)."""
    if not await flag.is_first_run(db):
        raise _setup_complete()


def get_limiter(request: Request) -> RateLimiter:
    limiter: RateLimiter = request.app.state.limiter
    return limiter


def _device_secret(config: Config) -> DeviceSecret:
    """Step 3's DNS update (proskenion/core/network.py) uses the same device
    secret the Cloudflare token is stored under (proskenion/api/network.py's
    own copy of this — see its docstring for why it is not shared)."""
    path = config.app.state_dir / DEFAULT_SECRET_PATH.name
    generate_secret_if_missing(path)
    return DeviceSecret.load(path)


async def _platform(request: Request) -> Platform:
    """The platform layer, from application state when another component owns it (§5.4)."""
    existing = getattr(request.app.state, "platform", None)
    if existing is not None:
        return existing  # type: ignore[no-any-return]
    config: Config = request.app.state.config
    return await asyncio.to_thread(
        detect_platform,
        appliance_dir=config.app.state_dir,
        data_dir=config.app.data_dir,
    )


async def _ensure_mixer_channels(request: Request, db: Database) -> None:
    """The wizard's own half of giving a mixer its channels (§7.3): every
    desk channel for a mixer with none yet besides Main, and at least a Main
    channel for every other mixer.

    ``POST /devices`` already creates them at the moment a mixer device is
    made (``proskenion/api/devices.py``), which is how step 4 normally
    configures one; this is the defensive second call — a mixer that already
    existed before this wizard run (a database that was reset and re-run,
    say) still gets its channels here, without the admin ever visiting the
    device again. A mixer that already has channels of its own is given only
    a Main, if it lacks one: the rest is the admin's "Add missing channels".
    A device manager not yet built (an application embedded without one) or
    with no mixer configured is a silent no-op.
    """
    manager: DeviceManager | None = getattr(request.app.state, "devices", None)
    if manager is None:
        return
    bus: EventBus | None = getattr(request.app.state, "bus", None)
    created = False
    for device in await devices_crud.list_all(db, category="mixer"):
        existing = await mixer_crud.list_channels(db, device_id=device.id)
        if all(channel.channel_kind == "main" for channel in existing):
            if await add_missing_channels(db, manager, device):
                created = True
        elif await mixer_service.ensure_main_channel(db, manager, device) is not None:
            created = True
    if created and bus is not None:
        bus.emit(MixerConfigChanged(reason="desk_channels_created"))


def _reload_hook(request: Request) -> certs.ReloadHook | None:
    """The nginx reload hook, injectable through application state (see core.certs)."""
    hook: certs.ReloadHook | None = getattr(request.app.state, "nginx_reload_hook", None)
    return hook


async def _served_hostname() -> str | None:
    """The first name nginx's site actually reads a certificate for, or ``None``.

    The single source :class:`~proskenion.core.certs.CertificateManager`
    already uses when ``config.server.hostname`` is absent — reused
    here rather than re-derived (blocking file read, so off the event loop
    per §5.3). ``certs.NGINX_SITE_CONFIG`` is read as a module attribute at
    call time, not bound as a default argument, so a test that monkeypatches
    it is honoured.
    """
    names = await asyncio.to_thread(certs.served_hostnames, certs.NGINX_SITE_CONFIG)
    return names[0] if names else None


def _certificate_hostname(
    config: Config, served: str | None, requested: str | None, machine_hostname: str
) -> str:
    """Step 6's default hostname: the name nginx actually serves, ahead of the
    machine's own short hostname.

    Before this fix the certificate step's default fell straight through to
    ``environment.hostname`` — ``socket.gethostname()`` on the appliance,
    ``"auditorium"`` — a single-label name no Cloudflare zone can ever own
    (§4.14's bootstrap config deliberately carries no ``[server] hostname``,
    so ``config.server.hostname`` is ``None`` there too), whenever nginx
    *was* configured to serve something better. ``served`` is now tried
    first; ``machine_hostname`` (``environment.hostname``, unchanged) is
    still the last resort where nothing else is available at all — a
    development machine with no nginx site, still able to generate *some*
    self-signed certificate rather than none. ``environment`` itself is
    still correct as-is for the *network* step's own "Hostname" field
    (§10.4 step 3, ``proskenion.core.setup.Environment`` — see
    ``_dispatch``'s ``Step.NETWORK`` case), which is asking about the
    machine's own name, not a name to request a certificate for; only the
    certificate step's default order changes.
    """
    for candidate in (requested, config.server.hostname, served, machine_hostname):
        if candidate and candidate.strip():
            return candidate.strip()
    return ""


async def _count_attempt(db: Database, limiter: RateLimiter, ip: str) -> None:
    """Count one attempt at the password step, recording any lockout it starts (§6.8)."""
    if limiter.record_failure(Scope.SETUP_STEP, ip):
        policy = limiter.policy(Scope.SETUP_STEP)
        await record_event(
            db,
            "lockout",
            user_ident=ip,
            ip_address=ip,
            detail={"scope": Scope.SETUP_STEP.value, "lockout_seconds": int(policy.lockout_s)},
        )


async def _check_limit(limiter: RateLimiter, ip: str) -> None:
    try:
        limiter.check(Scope.SETUP_STEP, ip)
    except LockedOut as exc:
        raise ApiError(
            ErrorCode.RATE_LIMITED,
            "Too many attempts. Try again later.",
            {"retry_after": exc.retry_after},
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc


async def _state_payload(
    db: Database, request: Request, config: Config
) -> dict[str, Any]:
    state = await setup.load_state(db)
    platform = await _platform(request)
    environment = await setup.detect_environment(
        platform, configured_hostname=config.server.hostname
    )
    # Once step 6 has run, the certificate on disk is the one it issued —
    # which may not be the configured or detected name.
    issued_for = state.records[Step.CERTIFICATE].summary.get("hostname")
    served = await _served_hostname()
    hostname = _certificate_hostname(
        config, served, issued_for if isinstance(issued_for, str) else None, environment.hostname
    )
    installed = (
        await certs.describe(certs.certificate_paths(platform.data_dir(), hostname))
        if hostname
        else None
    )
    return {
        "first_run": state.first_run,
        "steps": state.to_json(),
        "next_step": None if state.next_step is None else int(state.next_step),
        "detected": environment.to_json(),
        "certificate": {
            "hostname": hostname,
            "options": setup.certificate_options(
                lets_encrypt_available=getattr(request.app.state, "certs", None) is not None
            ),
            "installed": None if installed is None else _certificate_json(installed),
        },
    }


def _certificate_json(info: certs.CertificateInfo) -> dict[str, Any]:
    return {
        "domain": info.domain,
        "issuer": info.issuer,
        "issued": info.issued,
        "expires": info.expires,
        "days_remaining": info.days_remaining,
        "self_signed": info.self_signed,
        "renewal_history": info.renewal_history,
    }


# -- endpoints ----------------------------------------------------------------------


@router.get("/state", dependencies=[Depends(require_first_run)])
async def setup_state(
    request: Request,
    db: Annotated[Database, Depends(get_db)],
    config: Annotated[Config, Depends(get_config)],
) -> dict[str, Any]:
    """Completed steps with their summaries, the next incomplete step, and what to show.

    ``detected`` carries the locale and timezone inherited from the OS, the
    platform name, and the current address and hostname (§10.4 steps 1 and 3).
    ``certificate.options`` lists both §10.4 options with Let's Encrypt marked
    unavailable and why (Q5).
    """
    return await _state_payload(db, request, config)


WIZARD_LOGIN_SOURCE: Final = "first_run"
"""``security_events.detail.source`` marking a ``login_success`` issued by the wizard."""


async def _issue_wizard_session(
    response: Response,
    db: Database,
    tokens: TokenService,
    config: Config,
    ip: str,
) -> dict[str, str]:
    """Sign the wizard in as admin once step 2 has set the password (§10.4, §6.4).

    Step 2 has just written the admin hash and bumped ``token_version``, so a
    token is issued for it the same way ``POST /auth/login`` does, with the
    same cookie flags and the same ``login_success`` event — marked with
    :data:`WIZARD_LOGIN_SOURCE` so an audit reader can tell the two apart. The
    client holds a real admin session for the rest of the wizard from here and
    never re-sends the password.
    """
    admin = await users_crud.get_by_tier(db, "admin")
    if admin is None:  # pragma: no cover - the seed guarantees it
        raise RuntimeError("admin account is missing")
    token, claims = tokens.issue(admin.tier, admin.token_version)
    set_session_cookie(response, token, config=config)
    await record_event(
        db,
        "login_success",
        user_ident="admin",
        ip_address=ip,
        detail={
            "tier": "admin",
            "source": WIZARD_LOGIN_SOURCE,
            "absolute_expires_at": iso(claims.absolute_expires_at),
        },
    )
    return {"tier": claims.tier, "expires_at": iso(claims.expires_at)}


@router.post("/step/{number}", dependencies=[Depends(require_first_run)])
async def submit_step(
    number: int,
    request: Request,
    response: Response,
    db: Annotated[Database, Depends(get_db)],
    config: Annotated[Config, Depends(get_config)],
    tokens: Annotated[TokenService, Depends(get_tokens)],
    limiter: Annotated[RateLimiter, Depends(get_limiter)],
    body: Annotated[dict[str, Any] | None, Body()] = None,
) -> dict[str, Any]:
    """Submit one step. Re-submitting a completed step overwrites its record (§10.4).

    Step 2 additionally signs the wizard in: the response carries ``tier`` and
    ``expires_at`` and the admin session cookie is set, exactly as
    ``POST /auth/login`` does, so steps 3 onward — including the devices step,
    which calls the admin-tier devices API — run under a real session.
    """
    if number not in {int(s) for s in Step}:
        raise _validation_error(
            "There is no such setup step",
            [{"field": "step", "message": "The wizard has seven steps.", "type": "unknown_step"}],
        )
    step = Step(number)
    ip = client_ip(request)
    if step is Step.ADMIN_PASSWORD:
        await _check_limit(limiter, ip)
        await _count_attempt(db, limiter, ip)

    try:
        record = await _dispatch(step, request, db, config, body)
    except StepRejected as exc:
        raise _validation_error(exc.message, exc.fields) from exc
    except SetupAlreadyComplete as exc:  # committed between the gate and here
        raise _setup_complete() from exc

    state = await setup.load_state(db)
    result: dict[str, Any] = {
        "step": record.to_json(),
        "next_step": None if state.next_step is None else int(state.next_step),
        "steps": state.to_json(),
    }
    if step is Step.ADMIN_PASSWORD:
        result |= await _issue_wizard_session(response, db, tokens, config, ip)
    if step is Step.CERTIFICATE:
        # Whether the certificate nginx serves just changed, and what the new
        # one is valid for. A browser that accepted the old self-signed
        # certificate refuses the new one at TLS, so the wizard's next request
        # never reaches the controller: the client has to warn before it.
        names = record.summary.get("valid_for")
        result |= {
            "certificate_replaced": bool(record.summary.get("certificate_replaced", True)),
            "certificate_names": list(names) if isinstance(names, list) else [],
        }
    return result


async def _dispatch(
    step: Step,
    request: Request,
    db: Database,
    config: Config,
    body: dict[str, Any] | None,
) -> setup.StepRecord:
    ip = client_ip(request)
    match step:
        case Step.WELCOME:
            welcome = _parse(WelcomeBody, body)
            return await setup.submit_welcome(
                db, locale=welcome.locale, timezone=welcome.timezone
            )
        case Step.ADMIN_PASSWORD:
            admin = _parse(PasswordBody, body)
            record = await setup.submit_admin_password(
                db, password=admin.password, confirmation=admin.password_confirm
            )
            await record_event(
                db,
                "password_changed",
                user_ident="admin",
                ip_address=ip,
                detail={"tier": "admin", "source": "first_run"},
            )
            return record
        case Step.NETWORK:
            network_body = _parse(NetworkBody, body)
            environment = await setup.detect_environment(
                await _platform(request), configured_hostname=config.server.hostname
            )
            helper: HelperClient | None = getattr(request.app.state, "helper", None)
            rows = await devices_crud.list_all(db)
            return await setup.submit_network(
                db,
                address=network_body.address or environment.address,
                hostname=network_body.hostname or environment.hostname,
                skipped=network_body.skipped,
                prefix_length=network_body.prefix_length,
                gateway=network_body.gateway,
                dns=network_body.dns,
                data_dir=config.app.data_dir,
                helper=helper,
                secret=_device_secret(config),
                device_addresses=sorted(system_config.device_hosts(rows)),
            )
        case Step.DEVICES:
            devices = _parse(DevicesBody, body)
            # The client configures devices against the devices API; this step
            # records that it was carried out and what existed at the time.
            record = await setup.submit_devices(
                db, device_ids=devices.device_ids, skipped=devices.skipped
            )
            # §7.3: a mixer has its channels once configured — see
            # _ensure_mixer_channels's docstring for why this call is a
            # defensive second one, not the primary path.
            await _ensure_mixer_channels(request, db)
            return record
        case Step.OPERATOR_PASSWORD:
            operator = _parse(PasswordBody, body)
            record = await setup.submit_operator_password(
                db, password=operator.password, confirmation=operator.password_confirm
            )
            await record_event(
                db,
                "password_changed",
                user_ident="operator",
                ip_address=ip,
                detail={"tier": "operator", "source": "first_run"},
            )
            return record
        case Step.CERTIFICATE:
            certificate = _parse(CertificateBody, body)
            platform = await _platform(request)
            environment = await setup.detect_environment(
                platform, configured_hostname=config.server.hostname
            )
            served = await _served_hostname()
            return await setup.submit_certificate(
                db,
                option=certificate.option,
                hostname=_certificate_hostname(
                    config, served, certificate.hostname, environment.hostname
                ),
                data_dir=platform.data_dir(),
                addresses=[environment.address] if environment.address else [],
                reload_hook=_reload_hook(request),
                token=certificate.token,
                manager=getattr(request.app.state, "certs", None),
            )
        case Step.SUMMARY:
            _parse(ReviewBody, body)
            return await setup.submit_review(db)


@router.post("/complete", dependencies=[Depends(require_first_run)])
async def complete(
    request: Request,
    db: Annotated[Database, Depends(get_db)],
    flag: Annotated[FirstRunFlag, Depends(get_first_run)],
) -> dict[str, Any]:
    """Commit: set ``first_run_completed`` and leave first-run mode (§10.4, §16.4).

    Refused while any of steps 1–6 is incomplete or a placeholder credential
    remains. On success the cached flag is invalidated, so the very next
    request is no longer gated.
    """
    try:
        state = await setup.commit(db, ip_address=client_ip(request))
    except StepRejected as exc:
        raise _validation_error(exc.message, exc.fields) from exc
    except SetupAlreadyComplete as exc:
        raise _setup_complete() from exc
    flag.mark_complete()
    return {
        "first_run": False,
        "completed_at": state.records[Step.SUMMARY].completed_at,
        "steps": state.to_json(),
    }
