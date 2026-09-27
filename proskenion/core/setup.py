"""The first-run wizard's state machine (spec §10.4, §16.4).

The wizard is the only bootstrap path: the appliance must be fully
configurable from it with no command line. Everything it records lives in
``system_state`` (§15.13) — the step records under the ``setup`` domain, and
the single flag that ends first-run mode under the ``system`` domain.

Detection
    First-run state is the *absence* of ``first_run_completed = true``
    (§10.4). :func:`first_run_completed` reads that; :class:`FirstRunFlag`
    caches the answer in application state so the request gate (Q4) is not a
    database read per request, and is invalidated when step 7 commits.

Resumability
    Each step writes its result and marks itself complete. An abandoned setup
    resumes at the next incomplete step; every completed step keeps a small
    summary the client can display with an edit option. Re-submitting a step
    overwrites its record. A step may not be submitted before its
    predecessors are complete — except a step already completed, which may be
    revisited in any order.

Abort safety — deliberate, and it looks like a bug otherwise
    Closing the browser mid-setup writes nothing irreversible. The step
    records are inert descriptions; nothing outside the wizard reads them
    until it commits. The exception is passwords: steps 2 and 5 write real
    bcrypt hashes into ``users`` and bump ``token_version``, because there is
    no other way to set a credential and half-setting one is worse than
    setting it. So an abandoned setup **keeps the passwords already chosen**
    and stays in first-run mode until step 7 commits — the admin can sign in,
    but the gate still refuses every route outside ``/setup``, ``/health``
    and ``/auth`` (Q4). That is the intended behaviour, not a leak.

The summary of a step is safe to display: never a password, never any secret.

Re-running the wizard requires a database reset (§10.4); individual steps are
revisited afterwards through their normal admin locations.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import socket
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from pathlib import Path
from typing import Any, Final

from proskenion.core import auth, certs, network
from proskenion.core.cloudflare import CloudflareError
from proskenion.core.helper import HelperClient
from proskenion.core.platform import Platform
from proskenion.core.secrets import DeviceSecret
from proskenion.db.connection import Database
from proskenion.db.crud import system_state, users
from proskenion.db.crud.base import AUCKLAND

log = logging.getLogger(__name__)

SETUP_DOMAIN: Final = "setup"
"""``system_state`` domain holding one record per wizard step."""

SYSTEM_DOMAIN: Final = "system"
FIRST_RUN_COMPLETED_KEY: Final = "first_run_completed"
TRUE: Final = "true"

STATE_SOURCE: Final = "setup"

SUPPORTED_TIMEZONE: Final = "Pacific/Auckland"
"""§4.9: Pacific/Auckland throughout. The step confirms it; it does not choose."""

DEFAULT_LOCALE: Final = "en_NZ.UTF-8"

TEMPORARY_PASSWORD_BYTES: Final = 24
"""Entropy of the operator's seed password, which step 5 replaces (§10.4)."""

RESET_REQUIRED_MESSAGE: Final = (
    "First-run setup is already complete. Re-running the wizard requires a database "
    "reset; individual settings are changed from their admin pages."
)

LETS_ENCRYPT_UNAVAILABLE_REASON: Final = (
    "Certificate management is not running on this controller right now. Generate a "
    "self-signed certificate now; it can be replaced without repeating the wizard."
)


class Step(IntEnum):
    """The seven steps of §10.4, numbered as that section lists them."""

    WELCOME = 1
    ADMIN_PASSWORD = 2
    NETWORK = 3
    DEVICES = 4
    OPERATOR_PASSWORD = 5
    CERTIFICATE = 6
    SUMMARY = 7


STEP_KEYS: Final[Mapping[Step, str]] = {
    Step.WELCOME: "welcome",
    Step.ADMIN_PASSWORD: "admin_password",
    Step.NETWORK: "network",
    Step.DEVICES: "devices",
    Step.OPERATOR_PASSWORD: "operator_password",
    Step.CERTIFICATE: "certificate",
    Step.SUMMARY: "summary",
}

STEP_LABELS: Final[Mapping[Step, str]] = {
    Step.WELCOME: "Welcome",
    Step.ADMIN_PASSWORD: "Admin password",
    Step.NETWORK: "Network",
    Step.DEVICES: "Devices",
    Step.OPERATOR_PASSWORD: "Operator password",
    Step.CERTIFICATE: "Certificate",
    Step.SUMMARY: "Summary and commit",
}

CONFIGURATION_STEPS: Final[tuple[Step, ...]] = tuple(s for s in Step if s is not Step.SUMMARY)
"""Steps 1–6: everything ``POST /setup/complete`` requires before it commits."""


# -- errors --------------------------------------------------------------------------


FieldError = dict[str, str]


def field_error(name: str, message: str, kind: str) -> FieldError:
    return {"field": name, "message": message, "type": kind}


class SetupError(Exception):
    """Base class for wizard refusals."""


class StepRejected(SetupError):
    """The submission cannot be accepted; ``fields`` is the §16.1 field detail."""

    def __init__(self, message: str, fields: Sequence[FieldError] = ()) -> None:
        super().__init__(message)
        self.message = message
        self.fields: list[FieldError] = list(fields)


class SetupAlreadyComplete(SetupError):
    """First run has already been committed; the wizard is closed (§16.4)."""

    def __init__(self) -> None:
        super().__init__(RESET_REQUIRED_MESSAGE)
        self.message = RESET_REQUIRED_MESSAGE


# -- records -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StepRecord:
    """One step as stored: completion, when, and a summary safe to display."""

    step: Step
    completed: bool = False
    completed_at: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return STEP_KEYS[self.step]

    @property
    def label(self) -> str:
        return STEP_LABELS[self.step]

    def to_json(self) -> dict[str, Any]:
        return {
            "step": int(self.step),
            "key": self.key,
            "label": self.label,
            "completed": self.completed,
            "completed_at": self.completed_at,
            "summary": dict(self.summary),
        }


@dataclass(frozen=True, slots=True)
class SetupState:
    """Every step record plus the step the wizard should resume at."""

    records: dict[Step, StepRecord]
    first_run: bool

    @property
    def next_step(self) -> Step | None:
        """The first incomplete step, or ``None`` when all seven are done."""
        for step in Step:
            if not self.records[step].completed:
                return step
        return None

    def is_complete(self, step: Step) -> bool:
        return self.records[step].completed

    @property
    def incomplete(self) -> list[Step]:
        return [s for s in Step if not self.records[s].completed]

    def to_json(self) -> list[dict[str, Any]]:
        return [self.records[s].to_json() for s in Step]


def _state_key(step: Step) -> str:
    return f"step_{int(step)}"


def _record_from_value(step: Step, raw: str | None) -> StepRecord:
    if raw is None:
        return StepRecord(step=step)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("setup step %d has an unreadable record; treating it as incomplete", step)
        return StepRecord(step=step)
    if not isinstance(data, dict):
        return StepRecord(step=step)
    summary = data.get("summary")
    completed_at = data.get("completed_at")
    return StepRecord(
        step=step,
        completed=bool(data.get("completed", False)),
        completed_at=completed_at if isinstance(completed_at, str) else None,
        summary=dict(summary) if isinstance(summary, dict) else {},
    )


def now_iso() -> str:
    """Now, ISO 8601 with the Pacific/Auckland offset (§4.9)."""
    return datetime.now(tz=AUCKLAND).isoformat(timespec="seconds")


def _payload(record: StepRecord) -> str:
    return json.dumps(
        {
            "step": int(record.step),
            "completed": record.completed,
            "completed_at": record.completed_at,
            "summary": record.summary,
        },
        sort_keys=True,
    )


# -- detection (§10.4) ---------------------------------------------------------------


async def first_run_completed(db: Database) -> bool:
    """True once ``first_run_completed = true`` is present in ``system_state``."""
    value = await system_state.get_value(db, SYSTEM_DOMAIN, FIRST_RUN_COMPLETED_KEY)
    return value is not None and value.strip().lower() == TRUE


async def is_first_run(db: Database) -> bool:
    """True while the wizard has not committed — the absence of the flag (§10.4)."""
    return not await first_run_completed(db)


class FirstRunFlag:
    """The cached answer to "are we still in first run?", held in application state.

    The gate of Q4 runs on every request, so the flag is read from the
    database once and remembered. Only the wizard changes it, and it does so
    through :meth:`mark_complete`; :meth:`invalidate` exists for a test or a
    database reset that changes it behind our back.
    """

    __slots__ = ("_completed",)

    def __init__(self, *, completed: bool | None = None) -> None:
        self._completed = completed

    async def is_first_run(self, db: Database) -> bool:
        if self._completed is None:
            self._completed = await first_run_completed(db)
        return not self._completed

    def mark_complete(self) -> None:
        self._completed = True

    def invalidate(self) -> None:
        self._completed = None

    @property
    def cached(self) -> bool | None:
        """``True``/``False`` once read, ``None`` while unknown. For tests."""
        return self._completed


async def load_state(db: Database) -> SetupState:
    """Read every step record in one query (§15.13)."""
    stored = await system_state.get_domain(db, SETUP_DOMAIN)
    records = {step: _record_from_value(step, stored.get(_state_key(step))) for step in Step}
    return SetupState(records=records, first_run=await is_first_run(db))


async def _require_first_run(db: Database) -> SetupState:
    state = await load_state(db)
    if not state.first_run:
        raise SetupAlreadyComplete
    return state


async def _check_order(db: Database, step: Step) -> SetupState:
    """Refuse a step whose predecessors are not complete (a completed step may be revisited)."""
    state = await _require_first_run(db)
    if state.is_complete(step):
        return state
    missing = [s for s in Step if s < step and not state.is_complete(s)]
    if missing:
        raise StepRejected(
            f"{STEP_LABELS[step]} cannot be submitted before "
            f"{', '.join(STEP_LABELS[s].lower() for s in missing)}.",
            [
                field_error(
                    "step",
                    f"Complete step {int(missing[0])} ({STEP_LABELS[missing[0]].lower()}) first.",
                    "out_of_order",
                )
            ],
        )
    return state


async def _write(db: Database, step: Step, summary: Mapping[str, Any]) -> StepRecord:
    record = StepRecord(
        step=step, completed=True, completed_at=now_iso(), summary=dict(summary)
    )
    await system_state.set(
        db, SETUP_DOMAIN, _state_key(step), _payload(record), source=STATE_SOURCE
    )
    return record


# -- environment the wizard displays --------------------------------------------------


@dataclass(frozen=True, slots=True)
class Environment:
    """What ``GET /setup/state`` shows: detected locale, timezone, platform, address."""

    locale: str
    timezone: str
    platform: str
    hostname: str
    address: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "locale": self.locale,
            "timezone": self.timezone,
            "platform": self.platform,
            "hostname": self.hostname,
            "address": self.address,
        }


def _detected_locale() -> str:
    for name in ("LC_ALL", "LC_CTYPE", "LANG"):
        value = os.environ.get(name)
        if value and value not in {"C", "POSIX"}:
            return value
    return DEFAULT_LOCALE


def _detected_timezone(
    *,
    localtime: Path = Path("/etc/localtime"),
    etc_timezone: Path = Path("/etc/timezone"),
) -> str:
    """The OS timezone, shown for confirmation (§10.4); Pacific/Auckland if unknown.

    ``/etc/localtime`` is read first: it is what the system actually uses, and
    ``timedatectl set-timezone`` changes it without touching the older
    ``/etc/timezone``, which can then be stale. ``/etc/timezone`` is the
    fallback for a system whose localtime is a copy rather than a link.
    """
    try:
        target = Path(os.readlink(localtime))
    except OSError:
        target = None
    if target is not None:
        parts = target.parts
        if "zoneinfo" in parts:
            zone = "/".join(parts[parts.index("zoneinfo") + 1 :])
            if zone:
                return zone
    try:
        text = etc_timezone.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    return text or SUPPORTED_TIMEZONE


def primary_address() -> str | None:
    """The address this appliance would use to reach the network, or ``None``.

    A connected UDP socket to a documentation address (RFC 5737) sends no
    packets; it just asks the routing table which local address would be
    chosen. Blocking only in the sense that it touches the socket API.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        address = str(probe.getsockname()[0])
    except OSError:
        return None
    finally:
        probe.close()
    return address or None


def _environment_sync(platform_name: str, configured_hostname: str | None) -> Environment:
    return Environment(
        locale=_detected_locale(),
        timezone=_detected_timezone(),
        platform=platform_name,
        hostname=configured_hostname or socket.gethostname(),
        address=primary_address(),
    )


async def detect_environment(
    platform: Platform, *, configured_hostname: str | None = None
) -> Environment:
    """Everything the wizard displays about the machine it is running on (§5.3)."""
    return await asyncio.to_thread(_environment_sync, platform.name(), configured_hostname)


def certificate_options(
    *, self_signed_available: bool = True, lets_encrypt_available: bool = True
) -> list[dict[str, Any]]:
    """Step 6's two options (Q7).

    ``lets_encrypt_available`` is false only if the application's
    certificate manager never started — a wiring failure, not the expected
    state; the caller (``api/setup.py``) passes
    ``request.app.state.certs is not None``.
    """
    return [
        {
            "id": "self_signed",
            "label": "Generate a self-signed certificate",
            "available": self_signed_available,
            "reason": None,
            "guidance": list(certs.IOS_TRUST_GUIDANCE),
        },
        {
            "id": "lets_encrypt",
            "label": "Issue via Let's Encrypt (Cloudflare DNS-01)",
            "available": lets_encrypt_available,
            "reason": None if lets_encrypt_available else LETS_ENCRYPT_UNAVAILABLE_REASON,
            "guidance": [],
        },
    ]


# -- steps ----------------------------------------------------------------------------


def _password_errors(password: str, confirmation: str, field_name: str) -> list[FieldError]:
    errors: list[FieldError] = []
    if len(password) < auth.MIN_PASSWORD_LENGTH:
        errors.append(
            field_error(
                field_name,
                f"The password must be at least {auth.MIN_PASSWORD_LENGTH} characters.",
                "too_short",
            )
        )
    if password != confirmation:
        errors.append(
            field_error(f"{field_name}_confirm", "The two passwords do not match.", "mismatch")
        )
    return errors


async def submit_welcome(db: Database, *, locale: str, timezone: str) -> StepRecord:
    """Step 1 — confirm the locale and the Pacific/Auckland timezone (§10.4, §4.9)."""
    await _check_order(db, Step.WELCOME)
    if timezone.strip() != SUPPORTED_TIMEZONE:
        raise StepRejected(
            f"The appliance runs on {SUPPORTED_TIMEZONE} time.",
            [
                field_error(
                    "timezone",
                    f"Only {SUPPORTED_TIMEZONE} is supported (§4.9).",
                    "unsupported",
                )
            ],
        )
    if not locale.strip():
        raise StepRejected(
            "A locale is required.", [field_error("locale", "Choose a locale.", "required")]
        )
    return await _write(
        db, Step.WELCOME, {"locale": locale.strip(), "timezone": SUPPORTED_TIMEZONE}
    )


async def submit_admin_password(db: Database, *, password: str, confirmation: str) -> StepRecord:
    """Step 2 — set the admin password and seed the operator account (§10.4, §6.4).

    Both accounts get a real bcrypt hash and a bumped ``token_version``,
    replacing the seed placeholders (§15.2). The operator's is a random
    temporary password that step 5 replaces and that is never displayed,
    logged or stored anywhere but its own hash: until step 5 runs, nobody can
    sign in as the operator, which is the intended state.
    """
    await _check_order(db, Step.ADMIN_PASSWORD)
    errors = _password_errors(password, confirmation, "password")
    if errors:
        raise StepRejected("The admin password was not accepted.", errors)

    await auth.set_staff_password(db, "admin", password)

    operator = await users.get_by_tier(db, "operator")
    seeded = operator is not None and operator.has_placeholder_password
    if seeded:
        # The value is never named, returned or logged — only its hash survives.
        temporary = secrets.token_urlsafe(TEMPORARY_PASSWORD_BYTES)
        await auth.set_staff_password(db, "operator", temporary)
    return await _write(
        db,
        Step.ADMIN_PASSWORD,
        {"admin_password_set": True, "operator_seeded": seeded},
    )


async def submit_network(
    db: Database,
    *,
    address: str | None,
    hostname: str | None,
    skipped: bool,
    prefix_length: int | None = None,
    gateway: str | None = None,
    dns: Sequence[str] = (),
    data_dir: Path | None = None,
    helper: HelperClient | None = None,
    secret: DeviceSecret | None = None,
    device_addresses: Sequence[str] = (),
) -> StepRecord:
    """Step 3 — review, or apply a changed address for real (§10.4, §10.8).

    Reviewing an address that is already correct — the common case, and
    every call before this task — records what was reviewed and applies
    nothing: ``skipped`` or a bare ``address``/``hostname`` with no mask,
    gateway or DNS behaves exactly as before. A full submission that is not
    skipped goes through the same path the admin Network card uses
    (:func:`proskenion.core.network.validate` then
    :func:`~proskenion.core.network.apply_change`): the same validation, the
    same confirm-or-revert (§10.8) — ``data_dir`` and ``helper`` are what
    make that real rather than recorded, and are only absent in tests that
    do not exercise this path. A validation failure is a
    :class:`StepRejected`, the wizard's usual shape, carrying the same
    per-field detail (§16.1) :mod:`proskenion.api.network` raises.
    """
    await _check_order(db, Step.NETWORK)
    change: dict[str, Any] | None = None
    if (
        not skipped
        and address
        and hostname
        and prefix_length is not None
        and gateway
        and data_dir is not None
        and helper is not None
    ):
        try:
            settings = network.validate(
                hostname=hostname,
                address=address,
                prefix_length=prefix_length,
                gateway=gateway,
                dns=dns,
                device_addresses=device_addresses,
            )
        except network.NetworkValidationError as exc:
            raise StepRejected(
                "The network configuration is not valid.",
                [
                    field_error(field, message, "invalid")
                    for field, messages in exc.fields.items()
                    for message in messages
                ],
            ) from exc
        applied = await network.apply_change(data_dir, helper, settings, secret=secret)
        change = {
            "confirm_token": applied.confirm_token,
            "applied_at": applied.applied_at,
            "reverts_at": applied.reverts_at,
            "address": settings.address,
            "hostname": settings.hostname,
            "dns_updated": applied.dns_updated,
        }
    note = (
        "The address change was applied. Confirm it from the new address within "
        "3 minutes, or the previous settings are restored automatically (§10.8)."
        if change is not None
        else "Changing the address is done from the admin network page (Phase 6)."
    )
    return await _write(
        db,
        Step.NETWORK,
        {
            "address": address,
            "hostname": hostname,
            "skipped": skipped,
            "changed": change is not None,
            **({"change": change} if change is not None else {}),
            "note": note,
        },
    )


async def submit_devices(
    db: Database, *, device_ids: Sequence[int], skipped: bool
) -> StepRecord:
    """Step 4 — record that devices were configured, and which existed then (§10.4).

    The client carries out this step against the devices API; the wizard does
    not require anything to be online, and devices may be added later.
    """
    await _check_order(db, Step.DEVICES)
    ids = sorted({int(i) for i in device_ids})
    return await _write(
        db,
        Step.DEVICES,
        {"device_ids": ids, "device_count": len(ids), "skipped": skipped},
    )


async def submit_operator_password(
    db: Database, *, password: str, confirmation: str
) -> StepRecord:
    """Step 5 — replace the operator's temporary password (§10.4, §6.3)."""
    await _check_order(db, Step.OPERATOR_PASSWORD)
    errors = _password_errors(password, confirmation, "password")
    if errors:
        raise StepRejected("The operator password was not accepted.", errors)
    await auth.set_staff_password(db, "operator", password)
    return await _write(db, Step.OPERATOR_PASSWORD, {"operator_password_set": True})


async def _submit_self_signed(
    hostname: str,
    *,
    data_dir: Path,
    addresses: Sequence[str],
    reload_hook: certs.ReloadHook | None,
) -> dict[str, Any]:
    """The self-signed half of step 6 — also Let's Encrypt's fallback on failure.

    A self-signed certificate already installed for this name and these
    addresses, with time left on it, is kept rather than replaced: the
    application installs one at its first start (§3.2), and the browser
    running this wizard has already accepted it. A new one would be refused
    on the wizard's very next request, before it ever reached the controller
    (see :func:`~proskenion.core.certs.reusable_self_signed`).
    ``certificate_replaced`` says which happened, so the client can warn the
    user before their next action when it did.
    """
    paths = certs.certificate_paths(data_dir, hostname)
    reused = await asyncio.to_thread(certs.reusable_self_signed, data_dir, hostname, addresses)
    if reused is not None:
        info: certs.CertificateInfo | None = reused
        outcome = certs.ReloadOutcome(ok=True, detail="the installed certificate was kept")
    else:
        paths = await certs.issue_self_signed(hostname, data_dir=data_dir, addresses=addresses)
        info = await certs.describe(paths)
        outcome = await certs.reload_nginx(reload_hook)
        if not outcome.ok:
            log.warning("certificate written but nginx was not reloaded: %s", outcome.detail)
    summary: dict[str, Any] = {
        "option": "self_signed",
        "hostname": hostname,
        "certificate_path": str(paths.certificate),
        "nginx_reloaded": outcome.ok,
        "nginx_detail": outcome.detail,
        "certificate_replaced": reused is None,
        "valid_for": await asyncio.to_thread(certs.served_certificate_names, data_dir, hostname),
    }
    if info is not None:
        summary |= {
            "issuer": info.issuer,
            "issued": info.issued,
            "expires": info.expires,
            "days_remaining": info.days_remaining,
            "self_signed": info.self_signed,
        }
    return summary


async def _submit_lets_encrypt(
    hostname: str,
    token: str | None,
    manager: certs.CertificateManager | None,
    *,
    data_dir: Path,
    addresses: Sequence[str],
    reload_hook: certs.ReloadHook | None,
) -> dict[str, Any]:
    """The Let's Encrypt half of step 6 (Q7).

    A missing token is the admin's mistake — rejected the same way a blank
    hostname is, so they can supply one or switch to self-signed. A token
    Cloudflare itself refuses to authenticate is
    treated the same way: rejected up front, with Cloudflare's own reason,
    before anything is attempted. Before that fix the token went straight to
    issuance, which failed halfway with an ACME-flavoured message and
    silently wrote a self-signed fallback — on the real appliance (24
    September 2026) this was an administrator who had pasted a token's id
    rather than the token itself, sent looking at DNS by a message that had
    nothing to do with the real problem.

    A token that authenticates but cannot manage *this* hostname's zone is
    not caught by that pre-flight — wrong domain, or a zone this Cloudflare
    account does not hold — and, along with every other failure past
    authentication (no network reaching Cloudflare or Let's Encrypt, a DNS
    problem the venue has to fix later), remains an infrastructure failure
    that falls back to self-signed automatically: first-run must never be
    blocked by a failed *issuance* (Q7) — only by a token that is simply
    wrong, which is not something retrying will ever fix. The failure is
    recorded in the summary so the admin sees it, not hidden.
    """
    if not token or not token.strip():
        raise StepRejected(
            "A Cloudflare API token is required for Let's Encrypt.",
            [field_error("token", "Enter the Cloudflare API token.", "required")],
        )
    error: str | None
    if manager is None:
        error = "Let's Encrypt issuance is not available right now."
    else:
        try:
            await manager.set_token(token)
            await manager.verify_token()
        except certs.TokenError as exc:
            raise StepRejected(
                str(exc), [field_error("token", str(exc), "invalid")]
            ) from exc
        except CloudflareError as exc:
            raise StepRejected(
                f"Cloudflare rejected this token ({exc}). Check you pasted the token "
                "itself, which Cloudflare shows once when it is created, not its ID.",
                [field_error("token", str(exc), "rejected")],
            ) from exc
        try:
            info = await manager.issue(hostname, method="manual")
        except (certs.TokenError, certs.IssuanceError) as exc:
            error = str(exc)
        else:
            return {
                "option": "lets_encrypt",
                "hostname": hostname,
                "issuer": info.issuer,
                "issued": info.issued,
                "expires": info.expires,
                "days_remaining": info.days_remaining,
                "self_signed": info.self_signed,
                "certificate_replaced": True,
                "valid_for": await asyncio.to_thread(
                    certs.served_certificate_names, data_dir, hostname
                ),
            }
    log.warning(
        "Let's Encrypt issuance failed during setup for %s; falling back to self-signed: %s",
        hostname,
        error,
    )
    fallback = await _submit_self_signed(
        hostname, data_dir=data_dir, addresses=addresses, reload_hook=reload_hook
    )
    fallback |= {"requested_option": "lets_encrypt", "fallback_reason": error}
    return fallback


async def submit_certificate(
    db: Database,
    *,
    option: str,
    hostname: str,
    data_dir: Path,
    addresses: Sequence[str] = (),
    reload_hook: certs.ReloadHook | None = None,
    token: str | None = None,
    manager: certs.CertificateManager | None = None,
) -> StepRecord:
    """Step 6 — issue the certificate and reload nginx (§10.4, §6.16, Q7).

    ``lets_encrypt`` runs real DNS-01 issuance through ``manager`` (the same
    :class:`~proskenion.core.certs.CertificateManager` the Certificates admin
    screen uses — "the same code path", per the WORKLOG) and falls back to
    self-signed on any failure, with a clear message in the summary. A
    failing nginx reload is likewise recorded, not raised: the certificate is
    on disk and takes effect at the next reload.

    Refuses a hostname nginx's site does not actually serve
    — see :meth:`~proskenion.core.certs.CertificateManager._ensure_target_is_served`
    for why issuing for another name has no legitimate use here. Checked once,
    for both options, rather than only inside ``manager.issue`` /
    ``manager.use_self_signed``: the self-signed option never reaches the
    manager at all (:func:`certs.issue_self_signed` directly), and this way
    the wizard refuses before spending a Cloudflare or ACME call on a
    certificate nginx would never load.
    """
    await _check_order(db, Step.CERTIFICATE)
    if option not in ("self_signed", "lets_encrypt"):
        raise StepRejected(
            "Choose either a self-signed certificate or Let's Encrypt.",
            [field_error("option", "Choose a certificate option.", "invalid")],
        )
    if not hostname.strip():
        raise StepRejected(
            "A hostname is required for the certificate.",
            [field_error("hostname", "Enter the name this controller is reached by.", "required")],
        )
    hostname = hostname.strip()
    # certs.NGINX_SITE_CONFIG is read here, at call time, rather than relying
    # on served_hostnames' own default parameter (bound once, at import
    # time) — the same reason api.setup._served_hostname reads it explicitly,
    # and how a test overrides it with monkeypatch.setattr.
    served = await asyncio.to_thread(certs.served_hostnames, certs.NGINX_SITE_CONFIG)
    if served and hostname not in served:
        names = " or ".join(repr(name) for name in served)
        raise StepRejected(
            f"nginx serves {names}, not {hostname!r}. Issue the certificate for "
            "the name nginx actually serves, or it will never be used.",
            [
                field_error(
                    "hostname",
                    f"Enter {served[0]!r} — the name nginx serves.",
                    "not_served",
                )
            ],
        )
    if option == "lets_encrypt":
        summary = await _submit_lets_encrypt(
            hostname,
            token,
            manager,
            data_dir=data_dir,
            addresses=addresses,
            reload_hook=reload_hook,
        )
    else:
        summary = await _submit_self_signed(
            hostname, data_dir=data_dir, addresses=addresses, reload_hook=reload_hook
        )
    return await _write(db, Step.CERTIFICATE, summary)


async def submit_review(db: Database) -> StepRecord:
    """Step 7 — the review was seen. Committing is :func:`commit` (§10.4)."""
    state = await _check_order(db, Step.SUMMARY)
    return await _write(
        db,
        Step.SUMMARY,
        {"reviewed": True, "steps_complete": [int(s) for s in Step if state.is_complete(s)]},
    )


# -- commit ---------------------------------------------------------------------------


async def placeholder_tiers(db: Database) -> list[str]:
    """The staff accounts still holding a seed placeholder hash (§15.2)."""
    return sorted(u.tier for u in await users.get_all(db) if u.has_placeholder_password)


async def commit(db: Database, *, ip_address: str | None = None) -> SetupState:
    """Step 7's commit: set ``first_run_completed`` and leave first-run mode (§10.4).

    Refuses while any of steps 1–6 is incomplete or either staff account
    still holds a placeholder hash — committing then would leave an appliance
    nobody can sign in to. The step-7 record and the flag are written in one
    transaction, so a crash between them is not possible.
    """
    state = await _require_first_run(db)
    errors: list[FieldError] = []
    for step in CONFIGURATION_STEPS:
        if not state.is_complete(step):
            errors.append(
                field_error(
                    f"step_{int(step)}",
                    f"{STEP_LABELS[step]} has not been completed.",
                    "incomplete",
                )
            )
    for tier in await placeholder_tiers(db):
        errors.append(
            field_error(
                f"{tier}_password",
                f"The {tier} password has not been set.",
                "placeholder",
            )
        )
    if errors:
        raise StepRejected("Setup cannot be committed yet.", errors)

    record = StepRecord(
        step=Step.SUMMARY,
        completed=True,
        completed_at=now_iso(),
        summary={"reviewed": True, "committed": True},
    )
    await system_state.set_many(
        db,
        [
            (SETUP_DOMAIN, _state_key(Step.SUMMARY), _payload(record)),
            (SYSTEM_DOMAIN, FIRST_RUN_COMPLETED_KEY, TRUE),
        ],
        source=STATE_SOURCE,
    )
    await auth.record_event(
        db,
        "config_changed",
        user_ident="setup",
        ip_address=ip_address,
        detail={"change": "first_run_completed", "completed_at": record.completed_at},
    )
    log.info("first-run setup committed at %s", record.completed_at)
    return await load_state(db)
