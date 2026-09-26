"""§11.4's triggers wired to :class:`~proskenion.core.alerts.AlertSink`.

Every trigger is proved to fire exactly once for the condition it names, and
the recovery/reconnection case is proved to fire none — not by racing a real
60-second timer, but by handing :class:`~proskenion.core.alerts.DeviceRedAlertMonitor`
a ``sleep`` the test controls entirely (an indefinite one proves a
cancellation actually happened, rather than merely being outraced).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from proskenion.config import Config
from proskenion.core import email as email_module
from proskenion.core.alerts import (
    DEVICE_RED_DOMAIN,
    EMAIL_UNCONFIGURED_BANNER_KEY,
    AlertKind,
    DeviceRedAlertMonitor,
    RecordingAlertSink,
    SmtpAlertSink,
    login_failures_alert_callback,
    wire_rule_alerts,
)
from proskenion.core.bus import EventBus
from proskenion.core.email import EmailSettings, SmtpError, SmtpFailureStage, read_fallback
from proskenion.core.events import DeviceStatusChanged
from proskenion.core.ratelimit import LoginFailuresExceeded
from proskenion.core.secrets import DeviceSecret
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import email as email_crud
from proskenion.db.crud import system_state
from proskenion.rules.engine import RuleAlert


async def wait_until(
    predicate: Callable[[], bool], *, timeout_s: float = 2.0, interval: float = 0.005
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition was not met in time")


async def wait_until_async(
    predicate: Callable[[], Awaitable[bool]], *, timeout_s: float = 2.0, interval: float = 0.005
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition was not met in time")


@pytest.fixture
def secret() -> DeviceSecret:
    return DeviceSecret(key=bytes(range(32)))


@pytest.fixture
async def bus() -> EventBus:
    b = EventBus()
    await b.start()
    try:
        yield b
    finally:
        await b.stop()


@pytest.fixture
def state(dev_config: Config, bus: EventBus) -> StateStore:
    return StateStore(dev_config, bus)


# -- RuleAlert -> AlertSink (a `notify` rule action) -----------------------------------


async def test_wire_rule_alerts_forwards_exactly_one_alert(bus: EventBus) -> None:
    sink = RecordingAlertSink()
    wire_rule_alerts(bus, sink)
    bus.emit(
        RuleAlert(
            rule_id=7,
            rule_name="Fire alarm silence check",
            message="No response",
            triggered_by="knx:1/0/1",
        )
    )
    await wait_until(lambda: len(sink.sent) == 1)
    alert = sink.sent[0]
    assert alert.kind == AlertKind.RULE_NOTIFY
    assert "Fire alarm silence check" in alert.subject
    assert alert.body == "No response"


# -- device green -> red, sustained 60 s -----------------------------------------------


def _immediate_sleep(_seconds: float) -> Awaitable[None]:
    return asyncio.sleep(0)


async def _never_returns(_seconds: float) -> None:
    await asyncio.Event().wait()  # never set: only a cancellation ends this


async def test_sustained_red_fires_the_device_red_alert(bus: EventBus) -> None:
    sink = RecordingAlertSink()
    monitor = DeviceRedAlertMonitor(bus, sink, sleep=_immediate_sleep, names={"mixer": "Mixer"})
    await monitor.start()
    try:
        bus.emit(DeviceStatusChanged(device="mixer", status="error"))
        await wait_until(lambda: len(sink.sent) == 1)
        alert = sink.sent[0]
        assert alert.kind == AlertKind.DEVICE_RED
        assert "Mixer" in alert.subject
    finally:
        await monitor.stop()


async def test_reconnection_before_sixty_seconds_sends_nothing(bus: EventBus) -> None:
    """§11.4/contracts §7: "reconnection sends nothing." The sleep here never
    completes on its own — the only way the test passes is if the recovery
    transition actually cancelled the pending timer."""
    sink = RecordingAlertSink()
    monitor = DeviceRedAlertMonitor(bus, sink, sleep=_never_returns)
    await monitor.start()
    try:
        bus.emit(DeviceStatusChanged(device="mixer", status="error"))
        await wait_until(lambda: "mixer" in monitor.pending)  # the watch has started
        bus.emit(DeviceStatusChanged(device="mixer", status="connected"))
        await wait_until(lambda: "mixer" not in monitor.pending)  # cancelled, not merely queued
    finally:
        await monitor.stop()
    assert sink.sent == []


async def test_a_second_red_after_recovery_starts_a_fresh_timer(bus: EventBus) -> None:
    sink = RecordingAlertSink()
    monitor = DeviceRedAlertMonitor(bus, sink, sleep=_immediate_sleep)
    await monitor.start()
    try:
        bus.emit(DeviceStatusChanged(device="projector", status="error"))
        await wait_until(lambda: len(sink.sent) == 1)
        bus.emit(DeviceStatusChanged(device="projector", status="connected"))
        bus.emit(DeviceStatusChanged(device="projector", status="error"))
        await wait_until(lambda: len(sink.sent) == 2)
    finally:
        await monitor.stop()


class _SteppedSleep:
    """A sleep that returns only when the test says a minute has passed, so
    the test decides exactly which retries fall inside the 60 s window."""

    def __init__(self) -> None:
        self._released = asyncio.Event()

    async def __call__(self, _seconds: float) -> None:
        await self._released.wait()

    def a_minute_passes(self) -> None:
        self._released.set()
        self._released = asyncio.Event()


async def test_one_outage_is_one_email_however_many_times_the_driver_retries(
    bus: EventBus,
) -> None:
    """The CM5, 24 September 2026: an unreachable DMX node emailed every five
    minutes. Each retry is CONNECTING then ERROR (drivers/base.py, backoff
    capped at 300 s); only a real recovery, CONNECTED, ends the outage."""
    sink = RecordingAlertSink()
    clock = _SteppedSleep()
    monitor = DeviceRedAlertMonitor(bus, sink, sleep=clock, names={"dmx": "DMX node"})
    await monitor.start()

    async def emit(status: str) -> None:
        bus.emit(DeviceStatusChanged(device="dmx", status=status))  # type: ignore[arg-type]
        await asyncio.sleep(0.02)  # let the bus deliver it

    try:
        await emit("error")
        await emit("connecting")  # the 5 s retry, inside the first minute
        await emit("error")
        clock.a_minute_passes()
        await wait_until(lambda: len(sink.sent) == 1)
        for _ in range(5):  # backoff cycles, up to the 300 s cap, all one outage
            await emit("connecting")
            await emit("error")
            clock.a_minute_passes()
            await asyncio.sleep(0.02)
        assert len(sink.sent) == 1, [a.subject for a in sink.sent]

        await emit("connected")  # a genuine recovery, which sends nothing (§11.4)
        assert len(sink.sent) == 1
        await emit("error")
        clock.a_minute_passes()
        await wait_until(lambda: len(sink.sent) == 2)
    finally:
        await monitor.stop()
    assert all(a.kind == AlertKind.DEVICE_RED for a in sink.sent)


class _FailsOnceSink(RecordingAlertSink):
    """A sink whose first send raises — the PermissionError that escaped
    ``AlertSink.send`` on 24 September 2026 — and records after that."""

    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def send(
        self, kind: str, subject: str, body: str, *, priority: str = "normal"
    ) -> None:
        if not self.failed:
            self.failed = True
            raise PermissionError(1, "Operation not permitted", "smtp-fallback.toml")
        await super().send(kind, subject, body, priority=priority)


async def test_a_watch_whose_send_raises_says_so_and_alerts_on_the_next_failure(
    bus: EventBus, caplog: pytest.LogCaptureFixture
) -> None:
    """The task alerts:device-red:dmx ended with "Task exception was never
    retrieved" and nothing else. A failed send is now logged by name the
    moment it happens, and the watch carries on."""
    sink = _FailsOnceSink()
    monitor = DeviceRedAlertMonitor(bus, sink, sleep=_immediate_sleep, names={"dmx": "DMX node"})
    await monitor.start()
    try:
        with caplog.at_level(logging.ERROR):
            bus.emit(DeviceStatusChanged(device="dmx", status="error"))
            await wait_until(lambda: any("DMX node" in r.getMessage() for r in caplog.records))
        failure = next(r for r in caplog.records if "DMX node" in r.getMessage())
        assert failure.exc_info is not None, "logged without the exception that caused it"

        bus.emit(DeviceStatusChanged(device="dmx", status="connected"))
        bus.emit(DeviceStatusChanged(device="dmx", status="error"))
        await wait_until(lambda: len(sink.sent) == 1)
        assert sink.sent[0].kind == AlertKind.DEVICE_RED
    finally:
        await monitor.stop()


async def test_the_real_sink_survives_a_fallback_it_cannot_write(
    bus: EventBus,
    db: Database,
    state: StateStore,
    secret: DeviceSecret,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both sides of the hand-off that killed the watcher: the real
    SmtpAlertSink, sending successfully, and write_fallback's rename refused
    as sticky /srv/appliance refuses it for a file the application does not
    own. The alert goes, the sink does not raise, and the next one goes too."""
    await _configure(db, secret)
    fake = _FakeSend()
    sink = SmtpAlertSink(db, state, secret, state_dir=tmp_path, send=fake)

    def refuse(src: object, dst: object) -> None:
        raise PermissionError(1, "Operation not permitted", str(dst))

    monkeypatch.setattr(email_module.os, "replace", refuse)
    monitor = DeviceRedAlertMonitor(bus, sink, sleep=_immediate_sleep)
    await monitor.start()
    try:
        await sink.send(AlertKind.DISK_CRITICAL, "subject", "body")  # must not raise
        bus.emit(DeviceStatusChanged(device="dmx", status="error"))
        await wait_until(lambda: len(fake.calls) == 2)
        bus.emit(DeviceStatusChanged(device="dmx", status="connected"))
        bus.emit(DeviceStatusChanged(device="dmx", status="error"))
        await wait_until(lambda: len(fake.calls) == 3)
    finally:
        await monitor.stop()
    assert not (tmp_path / "smtp-fallback.toml").exists()


async def test_two_devices_are_tracked_independently(bus: EventBus) -> None:
    sink = RecordingAlertSink()
    monitor = DeviceRedAlertMonitor(bus, sink, sleep=_immediate_sleep)
    await monitor.start()
    try:
        bus.emit(DeviceStatusChanged(device="mixer", status="error"))
        bus.emit(DeviceStatusChanged(device="projector", status="error"))
        await wait_until(lambda: len(sink.sent) == 2)
    finally:
        await monitor.stop()
    devices = {a.subject for a in sink.sent}
    assert len(devices) == 2


# -- carry-forward 6 (phase-7 plan): the "already alerted" state survives a restart ------


async def test_a_restart_mid_outage_does_not_send_a_second_email(
    bus: EventBus, db: Database
) -> None:
    """Before this fix, ``_alerted`` lived only in the monitor's own memory:
    a process restart mid-outage forgot the device had already been
    alerted, so the very next `error` status armed a fresh 60-second timer
    and sent one more email for the same still-ongoing outage."""
    sink = RecordingAlertSink()
    monitor = DeviceRedAlertMonitor(bus, sink, db=db, sleep=_immediate_sleep)
    await monitor.start()
    try:
        bus.emit(DeviceStatusChanged(device="mixer", status="error"))
        await wait_until(lambda: len(sink.sent) == 1)
    finally:
        await monitor.stop()

    # A new monitor — standing in for the process restarting — sees the
    # device is still red (the driver reconnects and reports "error" again,
    # as it does on every start) before it has recovered.
    sink2 = RecordingAlertSink()
    restarted = DeviceRedAlertMonitor(bus, sink2, db=db, sleep=_immediate_sleep)
    await restarted.start()
    try:
        bus.emit(DeviceStatusChanged(device="mixer", status="error"))
        # A second device's event, on the same FIFO subscriber queue,
        # confirms the mixer event ahead of it was already processed —
        # without depending on a timer that (correctly) never arms for mixer.
        bus.emit(DeviceStatusChanged(device="projector", status="error"))
        await wait_until(lambda: len(sink2.sent) == 1)
        assert sink2.sent[0].subject.startswith("projector"), "the fresh outage did not alert"
        assert "mixer" not in restarted.pending
        assert len(sink2.sent) == 1, (
            "the restarted monitor sent a second email for mixer's already-alerted outage"
        )
    finally:
        await restarted.stop()


async def test_recovery_clears_the_persisted_alerted_state(
    bus: EventBus, db: Database
) -> None:
    """Once a device recovers, a later outage must alert again — including
    across a restart, which is what persisting the clear (not just the set)
    proves."""
    sink = RecordingAlertSink()
    monitor = DeviceRedAlertMonitor(bus, sink, db=db, sleep=_immediate_sleep)
    await monitor.start()
    try:
        bus.emit(DeviceStatusChanged(device="mixer", status="error"))
        await wait_until(lambda: len(sink.sent) == 1)
        bus.emit(DeviceStatusChanged(device="mixer", status="connected"))

        async def _cleared() -> bool:
            return await system_state.get_value(db, DEVICE_RED_DOMAIN, "mixer") != "1"

        await wait_until_async(_cleared)
    finally:
        await monitor.stop()

    # A new monitor — standing in for a restart after the recovery — must
    # treat a fresh outage as fresh, not as already alerted.
    sink2 = RecordingAlertSink()
    restarted = DeviceRedAlertMonitor(bus, sink2, db=db, sleep=_immediate_sleep)
    await restarted.start()
    try:
        bus.emit(DeviceStatusChanged(device="mixer", status="error"))
        await wait_until(lambda: len(sink2.sent) == 1)
    finally:
        await restarted.stop()


# -- the PIN/staff limiter's ten-in-an-hour alert --------------------------------------


async def test_login_failures_callback_forwards_to_the_live_sink() -> None:
    sink = RecordingAlertSink()
    callback = login_failures_alert_callback(lambda: sink)
    callback(LoginFailuresExceeded(ip="10.2.30.55", count=11))
    await wait_until(lambda: len(sink.sent) == 1)
    alert = sink.sent[0]
    assert alert.kind == AlertKind.LOGIN_FAILURES
    assert "10.2.30.55" in alert.subject


async def test_login_failures_callback_is_a_no_op_with_no_sink_yet() -> None:
    callback = login_failures_alert_callback(lambda: None)
    callback(LoginFailuresExceeded(ip="10.2.30.55", count=11))
    await asyncio.sleep(0.05)  # nothing to wait_until for: proving *absence* of a task


# -- SmtpAlertSink: live configuration, the unconfigured banner, last-known-good ------


class _FakeSend:
    def __init__(self, *, fail: SmtpError | None = None) -> None:
        self.calls: list[EmailSettings] = []
        self._fail = fail

    async def __call__(
        self, settings: EmailSettings, *, subject: str, body: str, priority: str = "normal"
    ) -> None:
        self.calls.append(settings)
        if self._fail is not None:
            raise self._fail


async def _configure(
    db: Database, secret: DeviceSecret, *, password: str | None = "hunter2"
) -> None:
    await email_crud.upsert(
        db,
        host="relay.n4l.co.nz",
        port=25,
        tls_mode="starttls",
        username="svc" if password else None,
        password=secret.encrypt_value("email_password", password) if password else None,
        sender="controller@auditorium.school.nz",
        recipient="ict@obhs.school.nz",
        updated_by=None,
    )


async def test_sends_with_the_live_configuration_and_mirrors_on_success(
    db: Database, state: StateStore, secret: DeviceSecret, tmp_path: Path
) -> None:
    await _configure(db, secret)
    fake = _FakeSend()
    sink = SmtpAlertSink(db, state, secret, state_dir=tmp_path, send=fake)

    await sink.send(AlertKind.DISK_CRITICAL, "subject", "body")

    assert len(fake.calls) == 1
    assert fake.calls[0].host == "relay.n4l.co.nz"
    assert fake.calls[0].password == "hunter2"
    mirrored = read_fallback(tmp_path, secret)
    assert mirrored is not None
    assert mirrored.host == "relay.n4l.co.nz"
    assert state.system.banner(EMAIL_UNCONFIGURED_BANNER_KEY) is None


async def test_unconfigured_raises_the_banner_once_and_logs_once(
    db: Database,
    state: StateStore,
    secret: DeviceSecret,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = _FakeSend()
    sink = SmtpAlertSink(db, state, secret, state_dir=tmp_path, send=fake)

    with caplog.at_level(logging.WARNING, logger="proskenion.core.alerts"):
        await sink.send(AlertKind.DISK_CRITICAL, "subject", "body")
        await sink.send(AlertKind.DISK_CRITICAL, "subject", "body")

    assert fake.calls == []
    assert state.system.banner(EMAIL_UNCONFIGURED_BANNER_KEY) is not None
    suppressed_lines = [r for r in caplog.records if "suppressed" in r.message]
    assert len(suppressed_lines) == 1, "the log line must appear only once, not per alert"
    assert not (tmp_path / "smtp-fallback.toml").exists()


async def test_becoming_configured_clears_the_banner(
    db: Database, state: StateStore, secret: DeviceSecret, tmp_path: Path
) -> None:
    fake = _FakeSend()
    sink = SmtpAlertSink(db, state, secret, state_dir=tmp_path, send=fake)
    await sink.send(AlertKind.DISK_CRITICAL, "subject", "body")
    assert state.system.banner(EMAIL_UNCONFIGURED_BANNER_KEY) is not None

    await _configure(db, secret)
    await sink.send(AlertKind.DISK_CRITICAL, "subject", "body")
    assert state.system.banner(EMAIL_UNCONFIGURED_BANNER_KEY) is None
    assert len(fake.calls) == 1


async def test_a_failed_send_does_not_raise_and_is_never_mirrored(
    db: Database, state: StateStore, secret: DeviceSecret, tmp_path: Path
) -> None:
    await _configure(db, secret)
    fake = _FakeSend(fail=SmtpError(SmtpFailureStage.CONNECT, "connection refused"))
    sink = SmtpAlertSink(db, state, secret, state_dir=tmp_path, send=fake)

    await sink.send(AlertKind.DISK_CRITICAL, "subject", "body")  # must not raise

    assert len(fake.calls) == 1
    assert not (tmp_path / "smtp-fallback.toml").exists(), (
        "last-known-good only: a failure is never mirrored"
    )


async def test_a_password_from_a_different_device_secret_is_treated_as_unconfigured(
    db: Database, state: StateStore, secret: DeviceSecret, tmp_path: Path
) -> None:
    other_secret = DeviceSecret(key=bytes(range(32, 64)))
    await _configure(db, other_secret)  # encrypted under a secret this sink does not hold
    fake = _FakeSend()
    sink = SmtpAlertSink(db, state, secret, state_dir=tmp_path, send=fake)

    await sink.send(AlertKind.DISK_CRITICAL, "subject", "body")

    assert fake.calls == []
    assert state.system.banner(EMAIL_UNCONFIGURED_BANNER_KEY) is not None
