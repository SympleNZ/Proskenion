"""Email alerts (spec §11.4; phase-6 contracts §7).

:class:`AlertSink` is the contract's interface exactly. Everything else here
wires it to §11.4's triggers:

- :class:`SmtpAlertSink` — the real implementation. It reads
  ``email_config`` live on every send (§7: "the sink reads the live
  configuration"), so a change through ``PUT /system/email`` takes effect on
  the next alert with nothing to restart. With no relay configured it logs
  once, raises the ``email_unconfigured`` banner, and returns — an alert
  that cannot be sent is not a reason to crash whatever raised it. A send
  that succeeds mirrors the configuration used to
  ``/srv/appliance/smtp-fallback.toml`` (:mod:`proskenion.core.email`).
- :class:`RecordingAlertSink` — for tests: every call is stored, never sent.
- :func:`wire_rule_alerts` — bridges :class:`~proskenion.rules.engine.RuleAlert`
  (the ``notify`` rule action) onto a sink.
- :class:`DeviceRedAlertMonitor` — "a device goes from green to red, after
  60 seconds": a fixed, always-on watch over every device and subsystem,
  independent of anything a rule configures.
- :func:`login_failures_alert_callback` — adapts
  :data:`proskenion.core.ratelimit.AlertCallback` (synchronous, built before
  the sink exists) onto a sink resolved lazily, for the PIN/staff limiter's
  ten-in-an-hour alert.

Backup and media failures are wired by :mod:`proskenion.core.backup`:
its ``BackupStatusWatcher`` reads what the nightly job persisted to
``system_state`` and calls ``AlertSink.send`` with :data:`AlertKind.BACKUP_FAILED`
(the job itself failed, §13.4's ladder) or :data:`AlertKind.MEDIA_FAILED`
(both the USB and the network destination are down); the same watcher calls
it with :data:`AlertKind.BACKUP_UNTRUSTED` when the monthly verification
finds a bad archive. One trigger remains a seam: an automatic rollback
(:data:`AlertKind.ROLLBACK`, ``priority="high"``). Every one of these
reaches a sink the same way everything above does: ``app.state.alert_sink``
(proskenion/api/app.py).

Reconnection sends nothing: nothing here subscribes to a device's recovery,
only to a sustained failure, and the rate limiter's own reset is not routed
through this module at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

from proskenion.core.bus import EventBus, Subscription
from proskenion.core.email import EmailSettings, SmtpError, send_email, write_fallback
from proskenion.core.events import DeviceStatusChanged
from proskenion.core.ratelimit import AlertCallback, LoginFailuresExceeded
from proskenion.core.secrets import DeviceSecret, SecretMismatch
from proskenion.core.state import StateStore
from proskenion.core.tasks import contained, spawn
from proskenion.db.connection import Database
from proskenion.db.crud import email as email_crud
from proskenion.db.crud import system_state
from proskenion.rules.engine import RuleAlert

log = logging.getLogger(__name__)

class AlertSink(Protocol):
    """Phase-6 contracts §7, verbatim."""

    async def send(
        self, kind: str, subject: str, body: str, *, priority: str = "normal"
    ) -> None: ...


class AlertKind(StrEnum):
    """§11.4's triggers. The contract leaves ``kind`` free-form (``str``);
    this is the closed vocabulary this task defines for it, extensible in
    code only — the same convention the rules engine uses for its own
    triggers (proskenion/rules/engine.py)."""

    LOGIN_FAILURES = "login_failures"  # the PIN/staff limiter, §6.8, §6.9
    RULE_NOTIFY = "rule_notify"  # a `notify` rule action, §8.9
    DEVICE_RED = "device_red"  # green -> red, sustained 60 s
    HIRE_ACTION_FAILED = "hire_action_failed"  # a scene had failed actions during a hire
    DISK_CRITICAL = "disk_critical"  # /data free space critical, §11.3
    BACKUP_FAILED = "backup_failed"  # the nightly job itself failed (§13.4)
    MEDIA_FAILED = "media_failed"  # both the USB and the network destination are down
    BACKUP_UNTRUSTED = "backup_untrusted"  # the monthly verification found a bad archive
    BACKUP_MISSING = "backup_missing"  # the archive it chose is held by no destination at all
    ROLLBACK = "rollback"  # seam for an automatic rollback; priority="high"


# -- a sink that only records, for tests --------------------------------------------


@dataclass(frozen=True, slots=True)
class RecordedAlert:
    kind: str
    subject: str
    body: str
    priority: str


class RecordingAlertSink:
    """An :class:`AlertSink` that stores what it was sent instead of sending it."""

    def __init__(self) -> None:
        self.sent: list[RecordedAlert] = []

    async def send(
        self, kind: str, subject: str, body: str, *, priority: str = "normal"
    ) -> None:
        self.sent.append(RecordedAlert(kind, subject, body, priority))


# -- the real sink --------------------------------------------------------------------

ALERT_OWNER: Final = "alerts"
EMAIL_UNCONFIGURED_BANNER_KEY: Final = "email_unconfigured"
EMAIL_UNCONFIGURED_TEXT: Final = (
    "No SMTP relay is configured. Alerts cannot be sent until one is set "
    "in Admin -> System -> Email."
)
_PASSWORD_FIELD: Final = "email_password"

SendFn = Callable[..., Awaitable[None]]  # matches proskenion.core.email.send_email


class SmtpAlertSink:
    """The production :class:`AlertSink`. See the module docstring."""

    def __init__(
        self,
        db: Database,
        state: StateStore,
        secret: DeviceSecret,
        *,
        state_dir: Path,
        send: SendFn = send_email,
        owner: str = ALERT_OWNER,
    ) -> None:
        self._db = db
        self._secret = secret
        self._state_dir = Path(state_dir)
        self._send = send
        state.register_owner("system", owner, allow_multiple=True)
        self._writer = state.system.writer(owner)
        self._unconfigured_banner_raised = False

    async def send(
        self, kind: str, subject: str, body: str, *, priority: str = "normal"
    ) -> None:
        settings = await self._settings()
        if settings is None:
            if not self._unconfigured_banner_raised:
                log.warning("email alert %r suppressed: no SMTP relay configured", kind)
                self._writer.set_banner(
                    EMAIL_UNCONFIGURED_BANNER_KEY, "amber", EMAIL_UNCONFIGURED_TEXT
                )
                self._unconfigured_banner_raised = True
            return
        if self._unconfigured_banner_raised:
            self._writer.clear_banner(EMAIL_UNCONFIGURED_BANNER_KEY)
            self._unconfigured_banner_raised = False
        try:
            await self._send(settings, subject=subject, body=body, priority=priority)
        except SmtpError as exc:
            log.warning("email alert %r failed at %s: %s", kind, exc.stage.value, exc.detail)
            return
        except Exception:  # a send failure must never propagate into the caller (§5.6 isolation)
            log.exception("email alert %r failed to send", kind)
            return
        # Last-known-good only (contracts §7): mirrored because this exact
        # configuration just proved it can send, never on failure. The alert
        # has already gone; a mirror that cannot be written (a root-owned file
        # in sticky /srv/appliance, 24 September 2026) costs emergency mode
        # its fallback relay, and must not become an exception in whatever
        # raised the alert (§5.6) — that killed device-red alerting.
        try:
            write_fallback(self._state_dir, settings, self._secret)
        except OSError as exc:
            log.error(
                "email alert %r was sent, but the fallback copy for emergency mode "
                "(§4.6) could not be saved: %s",
                kind,
                exc,
            )

    async def _settings(self) -> EmailSettings | None:
        row = await email_crud.get(self._db)
        if row is None or not row.host or not row.port or not row.sender or not row.recipient:
            return None
        password: str | None = None
        if row.password is not None:
            try:
                password = self._secret.decrypt_value(_PASSWORD_FIELD, row.password)
            except SecretMismatch:
                log.error(
                    "the stored SMTP password was encrypted with a different device secret; "
                    "re-enter it in Admin -> System -> Email"
                )
                return None
        tls_mode = row.tls_mode if row.tls_mode in ("none", "starttls", "tls") else "starttls"
        return EmailSettings(
            host=row.host,
            port=row.port,
            tls_mode=tls_mode,  # type: ignore[arg-type]  # narrowed by the membership check above
            username=row.username,
            password=password,
            sender=row.sender,
            recipient=row.recipient,
        )


# -- RuleAlert -> AlertSink (contracts §7; rules/engine.py's `notify` action) --------


def wire_rule_alerts(bus: EventBus, sink: AlertSink) -> Subscription:
    """Subscribe ``sink`` to every :class:`RuleAlert` a ``notify`` rule raises."""

    async def _on_rule_alert(event: RuleAlert) -> None:
        await sink.send(
            AlertKind.RULE_NOTIFY,
            f"Rule alert: {event.rule_name}",
            event.message,
        )

    return bus.subscribe(RuleAlert, _on_rule_alert, name="alerts:rule-notify")


# -- device green -> red, sustained 60 s ---------------------------------------------

RED_ALERT_AFTER_S: Final = 60.0

#: ``system_state`` domain for the "already alerted this outage" flag
#: (carry-forward 6, phase-7 plan). One key per device, ``"1"`` while an
#: outage has been alerted, cleared to ``"0"`` on recovery.
DEVICE_RED_DOMAIN: Final = "device_red_alerts"


class DeviceRedAlertMonitor:
    """§11.4: "a device goes from green to red, after 60 seconds — avoids a
    flurry on a brief hiccup." One email per outage, never one per retry.

    An outage begins when a device enters ``error``, and ends only when it
    has genuinely recovered: ``connected`` (green) or ``degraded`` (amber,
    which only a connected device reaches), or ``unconfigured`` (taken out
    of service). ``connecting`` is neither: every retry passes through it on
    the way back to ``error`` (proskenion/core/drivers/base.py's run loop,
    backoff capped at 300 s), and treating each retry as a fresh
    green-to-red transition sent an email every five minutes for one
    unreachable DMX node on the CM5 (24 September 2026).

    Within an outage: the first ``error`` starts the 60-second timer; a
    retry, or a change of failure kind or detail, neither restarts nor
    cancels it, since the device has been red throughout; once it has
    alerted, nothing more is sent. A recovery cancels a pending timer and
    ends the outage, so the next failure alerts again. §11.4: "Reconnection
    does not generate email", so recovery itself sends nothing.
    """

    def __init__(
        self,
        bus: EventBus,
        sink: AlertSink,
        *,
        db: Database | None = None,
        after_s: float = RED_ALERT_AFTER_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        names: Mapping[str, str] | None = None,
    ) -> None:
        self._bus = bus
        self._sink = sink
        #: Carry-forward 6 (phase-7 plan): persists which devices have
        #: already been alerted this outage, so an app restart mid-outage
        #: does not forget and send one more. ``None`` only in tests that do
        #: not exercise persistence.
        self._db = db
        self._after = after_s
        self._sleep = sleep
        self._names = dict(names or {})
        self._tasks: dict[str, asyncio.Task[None]] = {}
        # Devices already alerted in their current outage (see the docstring).
        self._alerted: set[str] = set()
        self._subscription: Subscription | None = None

    @property
    def pending(self) -> frozenset[str]:
        """Devices with a sixty-second timer currently running. Exposed for
        tests, which prove a recovery cancelled the timer rather than merely
        outracing it."""
        return frozenset(self._tasks)

    async def start(self) -> None:
        """Seed already-alerted devices from ``system_state`` before
        subscribing, so a device whose outage was already alerted before an
        app restart does not arm a fresh 60-second timer and send one more
        (carry-forward 6, phase-7 plan)."""
        if self._db is not None:
            persisted = await system_state.get_domain(self._db, DEVICE_RED_DOMAIN)
            self._alerted = {device for device, value in persisted.items() if value == "1"}
        self._subscription = self._bus.subscribe(
            DeviceStatusChanged, self._on_status, name="alerts:device-red"
        )

    async def stop(self) -> None:
        if self._subscription is not None:
            self._bus.unsubscribe(self._subscription)
            self._subscription = None
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

    async def _on_status(self, event: DeviceStatusChanged) -> None:
        device = event.device
        if event.status == "connecting":
            return  # a retry, not a recovery: the outage and its timer go on
        if event.status == "error":
            if device in self._alerted or device in self._tasks:
                return  # still the same outage
            self._tasks[device] = spawn(
                self._watch(device), name=f"alerts:device-red:{device}"
            )
            return
        # connected, degraded or unconfigured: the outage is over.
        was_alerted = device in self._alerted
        self._alerted.discard(device)
        existing = self._tasks.pop(device, None)
        if existing is not None:
            existing.cancel()
        if was_alerted and self._db is not None:
            await system_state.set(
                self._db, DEVICE_RED_DOMAIN, device, "0", source="device_red_alert"
            )

    async def _watch(self, device: str) -> None:
        try:
            await self._sleep(self._after)
        except asyncio.CancelledError:
            return
        self._tasks.pop(device, None)
        # Marked before sending: an email that fails to send is logged
        # (below), not retried at every backoff cycle of the same outage.
        self._alerted.add(device)
        if self._db is not None:
            await system_state.set(
                self._db, DEVICE_RED_DOMAIN, device, "1", source="device_red_alert"
            )
        name = self._names.get(device, device)
        # Contained (proskenion.core.tasks): a sink that raises costs this
        # one email, logged, never the watch over the next failure.
        await contained(
            f"the device-red alert for {name}",
            self._sink.send(
                AlertKind.DEVICE_RED,
                f"{name} has gone offline",
                f"{name} has been in the red (error) state for over a minute.",
            ),
        )


# -- the PIN/staff limiter's ten-in-an-hour alert (§6.8, §6.9) -----------------------


def login_failures_alert_callback(get_sink: Callable[[], AlertSink | None]) -> AlertCallback:
    """A :data:`~proskenion.core.ratelimit.AlertCallback` that forwards to
    whatever sink ``get_sink`` returns *at call time*.

    ``RateLimiter`` is built in ``create_app`` (proskenion/api/app.py),
    before the database is open and :class:`SmtpAlertSink` can be built in
    the lifespan; a plain closure over the sink would freeze ``None`` from
    before it existed. ``get_sink`` is typically ``lambda: app.state.alert_sink``.
    """

    def _on_alert(event: LoginFailuresExceeded) -> None:
        sink = get_sink()
        if sink is None:
            return
        spawn(
            sink.send(
                AlertKind.LOGIN_FAILURES,
                f"{event.count} failed sign-ins from {event.ip}",
                f"More than 10 failed sign-in attempts arrived from {event.ip} "
                f"within an hour ({event.count} total). The address is locked out "
                "under the usual policy; this is only the alert (§6.8).",
            ),
            name="alerts:login-failures",
        )

    return _on_alert


__all__ = [
    "ALERT_OWNER",
    "EMAIL_UNCONFIGURED_BANNER_KEY",
    "EMAIL_UNCONFIGURED_TEXT",
    "RED_ALERT_AFTER_S",
    "AlertKind",
    "AlertSink",
    "DeviceRedAlertMonitor",
    "RecordedAlert",
    "RecordingAlertSink",
    "SmtpAlertSink",
    "login_failures_alert_callback",
    "wire_rule_alerts",
]
