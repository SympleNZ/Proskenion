"""System endpoints (spec §16.7).

``GET /health`` is deliberately unversioned and unauthenticated: external
monitors and the reconnection screen should not have to track API versions.
It is liveness only.

The emergency payload returned when ``/data`` is unavailable (§4.6) has a fixed
shape, modelled here as :class:`EmergencyHealth`. Nothing serves it yet — the
emergency-mode service is a later task — but the shape is pinned now so both
sides agree on it.

``/system/health``, ``/system/time`` and ``/system/version`` are the versioned,
staff-only endpoints of §16.7. They read what the boot sequence started
(§12.1): the health poller, the time-sync monitor and the detected platform.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from proskenion import __version__
from proskenion import logging as log_redact
from proskenion.api.deps import client_ip, get_db, get_helper, require_admin, require_staff
from proskenion.api.errors import ApiError, ErrorCode
from proskenion.api.os_upgrade import optional_os_upgrade
from proskenion.api.snapshots import PreChangeSnapshot
from proskenion.config import Config
from proskenion.core import debug_config, system_config
from proskenion.core.auth import TokenClaims, record_event
from proskenion.core.email import (
    DEFAULT_TLS_MODE,
    TLS_MODES,
    EmailSettings,
    SmtpError,
    SmtpFailureStage,
    TlsMode,
    send_email,
    write_fallback,
)
from proskenion.core.health import HealthPoller
from proskenion.core.helper import HelperClient
from proskenion.core.osupgrade import OsUpgradeService
from proskenion.core.platform import Platform
from proskenion.core.secrets import (
    DEFAULT_SECRET_PATH,
    DeviceSecret,
    SecretMismatch,
    generate_secret_if_missing,
)
from proskenion.core.timesync import TimeSyncMonitor
from proskenion.db.connection import Database
from proskenion.db.crud import email as email_crud
from proskenion.db.crud import security_events
from proskenion.db.crud import users as users_crud
from proskenion.db.crud.base import AUCKLAND, now_iso

log = logging.getLogger(__name__)

router = APIRouter(tags=["system"])


class HealthResponse(BaseModel):
    """``200`` from ``/health`` in normal operation."""

    status: Literal["ok"] = "ok"
    version: str
    uptime: float  # seconds since the application started


class EmergencyReason(StrEnum):
    """Closed set of reasons for emergency mode (§16.7)."""

    DATA_UNAVAILABLE = "data_unavailable"
    DATA_READONLY = "data_readonly"
    MIGRATION_FAILED = "migration_failed"
    DISK_FULL = "disk_full"
    NOT_INSTALLED = "not_installed"


class StorageState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detected: bool
    smart: str
    media_errors: int


class EmergencyDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mount_state: str
    storage: StorageState
    # Both read from /mnt/backup — a different device, so available precisely
    # when /data is not. They decide whether someone drives in tonight.
    last_backup: AwareDatetime | None
    backup_media_present: bool


class EmergencyHealth(BaseModel):
    """``503`` from ``/health`` while ``/data`` is unavailable (§16.7)."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["emergency"] = "emergency"
    version: str
    reason: EmergencyReason
    detail: EmergencyDetail


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    started_at: float = request.app.state.started_at
    return HealthResponse(version=__version__, uptime=time.monotonic() - started_at)


# -- versioned system endpoints (§16.7) -----------------------------------------
#
# ``/health`` above is public and unversioned by design; everything below sits
# under ``/api/v1/system`` and is staff-only. The payload of ``GET
# /system/health`` is pinned here with ``extra="forbid"`` because the health
# screen (§21.24) is built against exactly this shape: an unavailable metric is
# ``null`` with level ``"unknown"``, never a wrong number.

system_router = APIRouter(prefix="/system", tags=["system"])

Level = Literal["green", "amber", "red", "unknown"]


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CpuVitals(_Payload):
    temperature_c: float | None
    level: Level


class MemoryVitals(_Payload):
    used_bytes: int | None
    total_bytes: int | None
    percent: float | None
    level: Level


class StorageVitals(_Payload):
    model: str | None
    health: str
    life_used_percent: float | None
    temperature_c: float | None
    media_errors: int | None
    partial: bool
    level: Level


class PartitionVitals(_Payload):
    mount: str
    slot: str | None
    total_bytes: int
    used_bytes: int
    free_bytes: int
    percent: float
    level: Level


class BackupMediaVitals(_Payload):
    """Amber when absent, never red, and never in the operator status bar (§4.5)."""

    present: bool
    absent_since: str | None
    level: Level


class BusVitals(_Payload):
    drop_count_window: int
    drop_consecutive_windows: int
    unsubscribed: list[str]
    level: Level


class ApplicationVitals(_Payload):
    loop_lag_p50_ms: float | None
    loop_lag_p99_ms: float | None
    level: Level
    clients: int
    bus: BusVitals


class DeviceVitals(_Payload):
    key: str
    name: str
    category: str
    status: str
    kind: str | None
    detail: str | None
    last_seen: str | None
    host: str | None
    port: int | None
    protocol: str | None
    latency_ms: float | None
    reconnects: int
    last_error: str | None
    level: Level


class TimeVitals(_Payload):
    synced: bool
    degraded: bool
    server_time: str


class SystemHealthResponse(_Payload):
    """``GET /system/health`` [admin] — full vitals (§16.7, §11.2)."""

    platform: str
    version: str
    uptime_seconds: float | None
    cpu: CpuVitals
    memory: MemoryVitals
    storage: StorageVitals
    partitions: list[PartitionVitals]
    backup_media: BackupMediaVitals
    application: ApplicationVitals
    devices: list[DeviceVitals]
    time: TimeVitals
    overall: Level


class SystemTimeResponse(_Payload):
    """``GET /system/time`` [admin] — NTP status, RTC presence, degraded flag (§16.7)."""

    synced: bool
    degraded: bool
    #: ``None`` when it could not be determined; §4.9's RTC is a platform
    #: requirement, so "unknown" and "absent" must not be conflated.
    rtc_present: bool | None
    server_time: str
    checked_at: str
    source: str


class SystemVersionResponse(_Payload):
    """``GET /system/version`` [admin, operator] (§16.7)."""

    version: str
    platform: str | None
    environment: str


def get_health(request: Request) -> HealthPoller:
    """The health poller the boot sequence started (§12.1)."""
    poller: HealthPoller | None = getattr(request.app.state, "health", None)
    if poller is None:
        raise ApiError(
            ErrorCode.INTERNAL_ERROR,
            "Health polling is not running",
            {"reason": "not_started"},
        )
    return poller


def get_timesync(request: Request) -> TimeSyncMonitor:
    monitor: TimeSyncMonitor | None = getattr(request.app.state, "timesync", None)
    if monitor is None:
        raise ApiError(
            ErrorCode.INTERNAL_ERROR,
            "Time synchronisation monitoring is not running",
            {"reason": "not_started"},
        )
    return monitor


@system_router.get(
    "/health",
    response_model=SystemHealthResponse,
    dependencies=[Depends(require_admin)],
)
async def system_health(request: Request) -> SystemHealthResponse:
    """Full vitals: CPU, memory, SSD SMART, partitions, backup media, uptime, clients."""
    snapshot = await get_health(request).current()
    return SystemHealthResponse.model_validate(snapshot.as_dict())


@system_router.get(
    "/time",
    response_model=SystemTimeResponse,
    dependencies=[Depends(require_admin)],
)
async def system_time(request: Request) -> SystemTimeResponse:
    monitor = get_timesync(request)
    status = await monitor.read()
    return SystemTimeResponse(
        synced=status.synced,
        degraded=monitor.degraded,
        rtc_present=status.rtc_present,
        server_time=now_iso(),
        checked_at=status.checked_at,
        source=status.source,
    )


@system_router.get(
    "/version",
    response_model=SystemVersionResponse,
    dependencies=[Depends(require_staff)],
)
async def system_version(request: Request) -> SystemVersionResponse:
    platform: Platform | None = getattr(request.app.state, "platform", None)
    config: Config = request.app.state.config
    return SystemVersionResponse(
        version=__version__,
        platform=None if platform is None else platform.name(),
        environment=config.app.environment.value,
    )


# -- restart and reboot (§21.24, contracts §2, §5) --------------------------
#
# Both ask the helper and answer before the action takes effect
# (``HelperClient.submit``, not ``.run`` — the same non-blocking shape
# ``core/network.py``'s ``apply_change`` uses, and for the same reason: the
# process answering this request is about to go away, so waiting here would
# only hold the HTTP response open for no one left to read it. The Updates
# screen's existing wait-and-poll of ``/health`` takes it from there.


class RestartResponse(_Payload):
    """``202`` from ``POST /system/restart``."""

    requested: Literal["restart"]
    requested_at: str


class RebootResponse(_Payload):
    """``202`` from ``POST /system/reboot``."""

    requested: Literal["reboot"]
    mode: Literal["normal"]
    requested_at: str


@system_router.post(
    "/restart",
    response_model=RestartResponse,
    status_code=202,
)
async def restart(
    request: Request,
    claims: Annotated[TokenClaims, Depends(require_admin)],
    db: Annotated[Database, Depends(get_db)],
    helper: Annotated[HelperClient, Depends(get_helper)],
) -> RestartResponse:
    """Ask the helper to restart the application (contracts §2's ``restart-core``)."""
    await helper.submit("restart-core")
    at = now_iso()
    await record_event(
        db,
        "config_changed",
        user_ident=claims.tier,
        ip_address=client_ip(request),
        detail={"setting": "restart"},
    )
    return RestartResponse(requested="restart", requested_at=at)


@system_router.post(
    "/reboot",
    response_model=RebootResponse,
    status_code=202,
)
async def reboot(
    request: Request,
    claims: Annotated[TokenClaims, Depends(require_admin)],
    db: Annotated[Database, Depends(get_db)],
    helper: Annotated[HelperClient, Depends(get_helper)],
    os_service: Annotated[OsUpgradeService | None, Depends(optional_os_upgrade)],
) -> RebootResponse:
    """Ask the helper to reboot the appliance (contracts §2's ``reboot``, ``mode: "normal"``).

    Refused while an OS slot is on trial (Q11). ``tryboot.txt`` selects the
    trial slot for exactly one boot; a plain reboot would consume that one
    boot and the bootloader would not repeat it, so what looks like "just
    restart the appliance" would silently abandon the trial and fall back to
    the previous slot — the one thing OS roll back already does, and says so
    before it does it. An operator who wants that uses roll back; a reboot
    that quietly did the same thing would not be able to tell them it had.
    """
    if os_service is not None:
        trial = await os_service.trial()
        if trial is not None:
            raise ApiError(
                ErrorCode.VALIDATION_FAILED,
                "The appliance cannot reboot while an operating system trial is in "
                "progress — a plain reboot would abandon it silently. Use OS roll "
                "back instead, which says what it does.",
                {"reason": "os_trial", "slot": trial.slot, "version": trial.version},
            )
    await helper.submit("reboot", mode="normal")
    at = now_iso()
    await record_event(
        db,
        "config_changed",
        user_ident=claims.tier,
        ip_address=client_ip(request),
        detail={"setting": "reboot"},
    )
    return RebootResponse(requested="reboot", mode="normal", requested_at=at)


# -- email (§11.4, §16.7; contracts §5, §7) --------------------------------------
#
# The password is write-only: GET never returns it, only whether one is set
# (the same convention as GET/PUT /system/certs/token, contracts §5).
# PUT replaces the whole row; an absent or omitted `password` on PUT means
# "leave the stored one unchanged" (§6.10's convention — see
# proskenion/api/devices.py's `_keep_secrets`). There is no separate "clear
# the password" affordance: an unauthenticated relay (Q1) needs none, and an
# admin who wants to drop authentication can simply blank `username` —
# `SmtpAlertSink`/`send_email` only attempt `login()` when one is set.


class EmailConfigResponse(_Payload):
    """``GET``/``PUT /system/email`` [admin]."""

    host: str | None
    port: int | None
    tls_mode: TlsMode
    username: str | None
    password_set: bool
    sender: str | None
    recipient: str | None
    updated_at: str | None


class EmailConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    tls_mode: TlsMode = DEFAULT_TLS_MODE
    username: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, max_length=255)
    sender: str = Field(min_length=3, max_length=255)
    recipient: str = Field(min_length=3, max_length=255)


class EmailTestRequest(BaseModel):
    """Optional draft values for ``POST /system/email/test`` — any field left
    out falls back to the saved configuration, so the test button can prove
    an edited-but-unsaved form before it commits anything (§21.24's Email
    card). With nothing saved and nothing posted there is nothing to test."""

    model_config = ConfigDict(extra="forbid")

    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    tls_mode: TlsMode | None = None
    username: str | None = None
    password: str | None = None
    sender: str | None = None
    recipient: str | None = None


class EmailTestResponse(_Payload):
    """The test reports inline, naming the failure (contracts §5)."""

    ok: bool
    stage: Literal["dns", "connect", "tls", "auth", "rejected_recipient"] | None
    message: str
    #: Whether this configuration is now mirrored to
    #: /srv/appliance/smtp-fallback.toml for emergency mode (§4.6). Never after
    #: a failed send (last-known-good only, contracts §7); ``ok`` with this
    #: ``False`` is a delivered email whose mirror could not be written.
    fallback_saved: bool = False


def _device_secret(config: Config) -> DeviceSecret:
    path = config.app.state_dir / DEFAULT_SECRET_PATH.name
    generate_secret_if_missing(path)
    return DeviceSecret.load(path)


def _email_response(row: email_crud.EmailConfigRow | None) -> EmailConfigResponse:
    if row is None:
        return EmailConfigResponse(
            host=None,
            port=None,
            tls_mode=DEFAULT_TLS_MODE,
            username=None,
            password_set=False,
            sender=None,
            recipient=None,
            updated_at=None,
        )
    tls_mode: TlsMode = row.tls_mode if row.tls_mode in TLS_MODES else DEFAULT_TLS_MODE
    return EmailConfigResponse(
        host=row.host,
        port=row.port,
        tls_mode=tls_mode,
        username=row.username,
        password_set=row.password is not None,
        sender=row.sender,
        recipient=row.recipient,
        updated_at=row.updated_at,
    )


async def _actor_id(db: Database, claims: TokenClaims) -> int | None:
    user = await users_crud.get_by_tier(db, claims.tier)
    return None if user is None else user.id


@system_router.get(
    "/email",
    response_model=EmailConfigResponse,
    dependencies=[Depends(require_admin)],
)
async def get_email(db: Annotated[Database, Depends(get_db)]) -> EmailConfigResponse:
    return _email_response(await email_crud.get(db))


@system_router.put(
    "/email",
    response_model=EmailConfigResponse,
)
async def put_email(
    body: EmailConfigUpdate,
    request: Request,
    claims: Annotated[TokenClaims, Depends(require_admin)],
    db: Annotated[Database, Depends(get_db)],
    helper: Annotated[HelperClient, Depends(get_helper)],
) -> EmailConfigResponse:
    secret = _device_secret(request.app.state.config)
    existing = await email_crud.get(db)
    password_stored = existing.password if existing is not None else None
    if body.password:
        password_stored = secret.encrypt_value("email_password", body.password)
    row = await email_crud.upsert(
        db,
        host=body.host,
        port=body.port,
        tls_mode=body.tls_mode,
        username=body.username,
        password=password_stored,
        sender=body.sender,
        recipient=body.recipient,
        updated_by=await _actor_id(db, claims),
    )
    await record_event(
        db,
        "config_changed",
        user_ident=claims.tier,
        ip_address=client_ip(request),
        detail={
            "setting": "email",
            "host": body.host,
            "port": body.port,
            "tls_mode": body.tls_mode,
            "username": body.username,
            "sender": body.sender,
            "recipient": body.recipient,
            "password_changed": bool(body.password),
        },
    )
    await _sync_smtp_firewall(request.app.state.config, helper, body.host, body.port)
    return _email_response(row)


@system_router.delete("/email", status_code=204)
async def delete_email(
    request: Request,
    claims: Annotated[TokenClaims, Depends(require_admin)],
    snapshot: PreChangeSnapshot,
    db: Annotated[Database, Depends(get_db)],
    helper: Annotated[HelperClient, Depends(get_helper)],
) -> None:
    """Clear the relay: no row is "no relay configured" (contracts §7), and
    the firewall stops admitting the old relay's port (§3.4)."""
    await snapshot("clear the email relay")
    await email_crud.clear(db)
    await record_event(
        db,
        "config_changed",
        user_ident=claims.tier,
        ip_address=client_ip(request),
        detail={"setting": "email", "cleared": True},
    )
    await _sync_smtp_firewall(request.app.state.config, helper, None, None)


async def _sync_smtp_firewall(
    config: Config, helper: HelperClient, host: str | None, port: int | None
) -> None:
    """Mirror the relay into ``system.json``'s ``network.smtp_relay`` and ask
    the helper to re-render the firewall from it — the same "the rule follows
    the saved row" step a device save takes (proskenion/api/devices.py's
    ``_sync_firewall``). Outbound is default-drop (§3.4): until this, the
    saved relay was never admitted and every send timed out at connect.

    Best-effort, like the device mirror: the row is already committed, and a
    filesystem or helper hiccup is logged rather than turned into a failed
    save.
    """
    try:
        changed = await asyncio.to_thread(
            system_config.sync_smtp_relay, config.app.data_dir, host, port
        )
        if changed:
            await helper.submit("apply-network")
    except OSError as exc:
        log.warning("could not mirror the SMTP relay into the firewall: %s", exc)


def _draft_settings(
    body: EmailTestRequest, existing: email_crud.EmailConfigRow | None, secret: DeviceSecret
) -> EmailSettings | None:
    """Layer ``body``'s overrides onto the saved row (see :class:`EmailTestRequest`)."""
    host = body.host or (existing.host if existing else None)
    port = body.port or (existing.port if existing else None)
    tls_mode: TlsMode = body.tls_mode or (
        existing.tls_mode if existing and existing.tls_mode in TLS_MODES else DEFAULT_TLS_MODE
    )
    username = (
        body.username if body.username is not None else (existing.username if existing else None)
    )
    sender = body.sender or (existing.sender if existing else None)
    recipient = body.recipient or (existing.recipient if existing else None)
    if not host or not port or not sender or not recipient:
        return None
    password: str | None = body.password
    if password is None and existing is not None and existing.password is not None:
        password = secret.decrypt_value("email_password", existing.password)
    return EmailSettings(
        host=host,
        port=port,
        tls_mode=tls_mode,
        username=username,
        password=password,
        sender=sender,
        recipient=recipient,
    )


@system_router.post(
    "/email/test",
    response_model=EmailTestResponse,
)
async def test_email(
    body: EmailTestRequest,
    request: Request,
    _: Annotated[TokenClaims, Depends(require_admin)],
    db: Annotated[Database, Depends(get_db)],
) -> EmailTestResponse:
    config: Config = request.app.state.config
    secret = _device_secret(config)
    existing = await email_crud.get(db)
    try:
        settings = _draft_settings(body, existing, secret)
    except SecretMismatch as exc:  # a stored password from a different device secret (§6.10)
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "The stored password could not be read; re-enter it before testing",
            {"reason": "secret_mismatch", "detail": str(exc)},
        ) from exc
    if settings is None:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "Host, port, sender and recipient are required to send a test email",
            {"fields": ["host", "port", "sender", "recipient"]},
        )
    try:
        await send_email(
            settings,
            subject="Proskenion test email",
            body=(
                "This is a test email from the Proskenion auditorium controller "
                f"({config.server.hostname or 'unconfigured hostname'}). "
                "If you can read this, alerts will reach this address."
            ),
        )
    except SmtpError as exc:
        message = exc.detail
        saved = (existing.host, existing.port) if existing is not None else None
        unsaved = (settings.host, settings.port) != saved
        if exc.stage is SmtpFailureStage.CONNECT and unsaved:
            # Outbound is default-drop (§3.4): the firewall opens only to the
            # saved relay, so a draft relay cannot connect until it is saved.
            message = (
                f"{message}. The firewall admits only the saved relay: save these "
                "settings, then test again."
            )
        return EmailTestResponse(ok=False, stage=exc.stage.value, message=message)
    # Last-known-good only (contracts §7): mirrored because this exact
    # configuration just proved it can send. The email has been delivered
    # by now, so a mirror that cannot be written (24 September 2026: a
    # root-owned file in sticky /srv/appliance) is reported, not a 500 — the
    # admin must know emergency mode (§4.6) has no relay, and must not be
    # told the send failed when it did not.
    try:
        write_fallback(config.app.state_dir, settings, secret)
    except OSError as exc:
        log.error("test email sent, but the SMTP fallback copy could not be saved: %s", exc)
        return EmailTestResponse(
            ok=True,
            stage=None,
            message=(
                "Test email sent, but the fallback copy emergency mode uses was not "
                f"saved ({exc.strerror or exc}). Alerts work; emergency mode cannot send."
            ),
            fallback_saved=False,
        )
    return EmailTestResponse(ok=True, stage=None, message="Test email sent", fallback_saved=True)


# -- security log (§6.14, §21.24 "Logs") -------------------------------------------
#
# The Security tab of Admin → System → Logs, over ``security_events.query()``.
# Filtered by event type, outcome (a coarser grouping of event types — see
# ``_FAILURE_EVENT_TYPES`` below), client IP address and a time range;
# paginated the same way ``GET /scenes/log`` is, newest first.
#
# ``detail`` is redacted here, in the API layer, rather than trusted from the
# writer: every call site in this codebase (``proskenion/core/auth.py``,
# ``proskenion/core/hirer_access.py``, and so on) already avoids putting a
# secret in it, but the audit trail's own purpose — recording exactly what an
# attempt carried — is also the shape a secret would take if a future call
# site slipped, so the reader redacts by key name rather than relying on
# every writer staying careful forever.

#: The closed event_type vocabulary (§6.14).
EventType = Literal[
    "login_success",
    "login_failure",
    "lockout",
    "permission_denied",
    "config_changed",
    "pin_changed",
    "access_toggled",
    "password_changed",
    "forced_logout",
    "unexpected_origin",
    "update_applied",
    "update_auto_rollback",
    "baseline_restored",
    "certificate_renewed",
    "backup_restored",
    "os_upgrade_applied",
    "os_upgrade_rolled_back",
]

#: The same vocabulary as a plain set, kept beside :data:`EventType` rather
#: than derived from it (``Literal.__args__`` is a typing implementation
#: detail, not a documented interface) — the "outcome" filter's success side
#: is everything here that is not in :data:`_FAILURE_EVENT_TYPES`.
_ALL_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "login_success",
        "login_failure",
        "lockout",
        "permission_denied",
        "config_changed",
        "pin_changed",
        "access_toggled",
        "password_changed",
        "forced_logout",
        "unexpected_origin",
        "update_applied",
        "update_auto_rollback",
        "baseline_restored",
        "certificate_renewed",
        "backup_restored",
        "os_upgrade_applied",
        "os_upgrade_rolled_back",
    }
)

Outcome = Literal["success", "failure"]

#: Event types that record an attempt being refused, rejected or undone.
#: Everything else in the §6.14 vocabulary is a successful, intended change —
#: the "outcome" filter is this grouping, since the vocabulary itself carries
#: no separate outcome field.
_FAILURE_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "login_failure",
        "lockout",
        "permission_denied",
        "unexpected_origin",
        "update_auto_rollback",
        "os_upgrade_rolled_back",
    }
)

_SUCCESS_EVENT_TYPES: Final[frozenset[str]] = _ALL_EVENT_TYPES - _FAILURE_EVENT_TYPES

#: Redaction is the same rule the structured file log applies to ``extra=``
#: at every level (:mod:`proskenion.logging`, §4.10): a key that looks like a
#: credential is never returned as written, here or there.


def _parsed_detail(raw: str | None) -> dict[str, Any] | None:
    """``security_events.detail`` — JSON text — parsed and redacted."""
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        # Every writer serialises ``detail`` as JSON (§6.14); text that fails
        # to parse is unexpected rather than a value to trust as-is, so the
        # whole thing is withheld rather than risk a secret in free text.
        log.warning("a security_events row's detail was not valid JSON; withholding it")
        return {"unparsed": log_redact.REDACTED}
    if not isinstance(parsed, dict):
        return {"value": log_redact.redact(parsed)}
    redacted: dict[str, Any] = log_redact.redact(parsed)
    return redacted


def _event_outcome(event_type: str) -> Outcome:
    return "failure" if event_type in _FAILURE_EVENT_TYPES else "success"


class SecurityLogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    timestamp: str
    event_type: str
    outcome: Outcome
    user_ident: str | None
    ip_address: str | None
    detail: dict[str, Any] | None


class SecurityLogResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[SecurityLogEntry]


def _security_log_instant(value: datetime | None) -> str | None:
    """A query parameter as an ISO instant; a time without an offset is Auckland time (§4.9)."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=AUCKLAND)
    return value.isoformat()


@system_router.get(
    "/security-log",
    response_model=SecurityLogResponse,
    dependencies=[Depends(require_admin)],
)
async def get_security_log(
    db: Annotated[Database, Depends(get_db)],
    event_type: EventType | None = None,
    outcome: Outcome | None = None,
    ip_address: str | None = None,
    since: Annotated[datetime | None, Query(alias="from")] = None,
    until: Annotated[datetime | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=security_events.MAX_LIMIT)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SecurityLogResponse:
    """Every audit row (§6.14), newest first, filtered and redacted — admin only."""
    event_types: list[str] | None = None
    if outcome is not None:
        event_types = sorted(
            _FAILURE_EVENT_TYPES if outcome == "failure" else _SUCCESS_EVENT_TYPES
        )
    rows = await security_events.query(
        db,
        since=_security_log_instant(since),
        until=_security_log_instant(until),
        event_type=event_type,
        event_types=event_types,
        ip_address=ip_address,
        limit=limit,
        offset=offset,
    )
    return SecurityLogResponse(
        entries=[
            SecurityLogEntry(
                id=row.id,
                timestamp=row.timestamp,
                event_type=row.event_type,
                outcome=_event_outcome(row.event_type),
                user_ident=row.user_ident,
                ip_address=row.ip_address,
                detail=_parsed_detail(row.detail),
            )
            for row in rows
        ]
    )


# -- per-module DEBUG toggling (§4.10, §21.24 "Logs") ------------------------------
#
# ``debug.json`` under the configured data directory — never a hard-coded
# ``/data`` — names which of the application's top-level packages log at
# DEBUG instead of INFO. See ``proskenion/core/debug_config.py`` for the file
# format and the malformed-file handling; this is only the admin surface over
# it, and both routes apply the change live, through the same
# ``debug_config.apply`` the application lifespan calls at startup.


class DebugLoggerState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    enabled: bool


class DebugLoggingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    loggers: list[DebugLoggerState]


class DebugLoggingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    logger: str
    enabled: bool


def _debug_logging_response(overrides: dict[str, bool]) -> DebugLoggingResponse:
    return DebugLoggingResponse(
        loggers=[
            DebugLoggerState(name=name, enabled=overrides.get(name, False))
            for name in debug_config.available_loggers()
        ]
    )


@system_router.get(
    "/debug-logging",
    response_model=DebugLoggingResponse,
    dependencies=[Depends(require_admin)],
)
async def get_debug_logging(request: Request) -> DebugLoggingResponse:
    """Every top-level module and whether it is currently at DEBUG."""
    config: Config = request.app.state.config
    overrides = debug_config.load_overrides(config.app.data_dir)
    return _debug_logging_response(overrides)


@system_router.put(
    "/debug-logging",
    response_model=DebugLoggingResponse,
)
async def put_debug_logging(
    body: DebugLoggingUpdate,
    request: Request,
    claims: Annotated[TokenClaims, Depends(require_admin)],
    db: Annotated[Database, Depends(get_db)],
) -> DebugLoggingResponse:
    """Turn one module's DEBUG level on or off — takes effect immediately, no restart."""
    config: Config = request.app.state.config
    try:
        overrides = debug_config.set_enabled(config.app.data_dir, body.logger, body.enabled)
    except ValueError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            str(exc),
            {"fields": [{"field": "logger", "message": str(exc)}]},
        ) from exc
    await record_event(
        db,
        "config_changed",
        user_ident=claims.tier,
        ip_address=client_ip(request),
        detail={"setting": "debug_logging", "logger": body.logger, "enabled": body.enabled},
    )
    return _debug_logging_response(overrides)
