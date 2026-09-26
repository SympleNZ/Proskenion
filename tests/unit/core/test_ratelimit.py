"""Per-address rate limiting (§6.8) and the reset tool's signal file (§6.9)."""

from pathlib import Path

import pytest

from proskenion.core.ratelimit import (
    ALERT_THRESHOLD,
    POLICIES,
    LockedOut,
    LoginFailuresExceeded,
    RateLimiter,
    Scope,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def alerts() -> list[LoginFailuresExceeded]:
    return []


@pytest.fixture
def limiter(clock: Clock, alerts: list[LoginFailuresExceeded], tmp_path: Path) -> RateLimiter:
    return RateLimiter(clock=clock, on_alert=alerts.append, signal_path=tmp_path / "clear-lockouts")


def fail(limiter: RateLimiter, scope: Scope, ip: str, times: int) -> list[bool]:
    return [limiter.record_failure(scope, ip) for _ in range(times)]


def test_policies_match_the_table() -> None:
    staff = POLICIES[Scope.STAFF_LOGIN]
    hirer = POLICIES[Scope.HIRER_PIN]
    assert (staff.attempts, staff.window_s, staff.lockout_s) == (5, 300, 900)
    assert (hirer.attempts, hirer.window_s, hirer.lockout_s) == (3, 600, 1800)


def test_fifth_staff_failure_starts_a_fifteen_minute_lockout(
    limiter: RateLimiter, clock: Clock
) -> None:
    assert fail(limiter, Scope.STAFF_LOGIN, "10.0.0.1", 4) == [False] * 4
    limiter.check(Scope.STAFF_LOGIN, "10.0.0.1")  # still allowed
    assert limiter.record_failure(Scope.STAFF_LOGIN, "10.0.0.1") is True
    with pytest.raises(LockedOut) as exc:
        limiter.check(Scope.STAFF_LOGIN, "10.0.0.1")
    assert exc.value.retry_after == 900
    assert exc.value.scope is Scope.STAFF_LOGIN and exc.value.ip == "10.0.0.1"
    clock.advance(899)
    assert limiter.retry_after(Scope.STAFF_LOGIN, "10.0.0.1") == 1
    clock.advance(1)
    limiter.check(Scope.STAFF_LOGIN, "10.0.0.1")
    assert limiter.retry_after(Scope.STAFF_LOGIN, "10.0.0.1") is None


def test_other_addresses_and_scopes_are_unaffected(limiter: RateLimiter) -> None:
    fail(limiter, Scope.STAFF_LOGIN, "10.0.0.1", 5)
    limiter.check(Scope.STAFF_LOGIN, "10.0.0.2")
    limiter.check(Scope.HIRER_PIN, "10.0.0.1")


def test_fourth_hirer_failure_locks_for_thirty_minutes(limiter: RateLimiter, clock: Clock) -> None:
    assert fail(limiter, Scope.HIRER_PIN, "10.0.0.1", 3) == [False, False, True]
    with pytest.raises(LockedOut) as exc:
        limiter.check(Scope.HIRER_PIN, "10.0.0.1")
    assert exc.value.retry_after == 1800
    clock.advance(1800)
    limiter.check(Scope.HIRER_PIN, "10.0.0.1")


def test_failures_outside_the_window_do_not_count(limiter: RateLimiter, clock: Clock) -> None:
    fail(limiter, Scope.STAFF_LOGIN, "10.0.0.1", 4)
    clock.advance(301)
    assert limiter.record_failure(Scope.STAFF_LOGIN, "10.0.0.1") is False
    limiter.check(Scope.STAFF_LOGIN, "10.0.0.1")


def test_success_clears_the_counter(limiter: RateLimiter) -> None:
    fail(limiter, Scope.STAFF_LOGIN, "10.0.0.1", 4)
    limiter.record_success(Scope.STAFF_LOGIN, "10.0.0.1")
    assert fail(limiter, Scope.STAFF_LOGIN, "10.0.0.1", 4) == [False] * 4


def test_clear_forgets_one_address(limiter: RateLimiter) -> None:
    fail(limiter, Scope.STAFF_LOGIN, "10.0.0.1", 5)
    fail(limiter, Scope.STAFF_LOGIN, "10.0.0.2", 5)
    limiter.clear("10.0.0.1")
    limiter.check(Scope.STAFF_LOGIN, "10.0.0.1")
    with pytest.raises(LockedOut):
        limiter.check(Scope.STAFF_LOGIN, "10.0.0.2")


def test_alert_fires_once_past_ten_failures_in_an_hour(
    limiter: RateLimiter, clock: Clock, alerts: list[LoginFailuresExceeded]
) -> None:
    # Lockouts do not stop failures being counted for the alert: spread them
    # across both scopes and across lockout windows.
    for i in range(ALERT_THRESHOLD):
        limiter.record_failure(Scope.STAFF_LOGIN if i % 2 else Scope.HIRER_PIN, "10.0.0.1")
        clock.advance(60)
    assert alerts == []
    limiter.record_failure(Scope.STAFF_LOGIN, "10.0.0.1")
    assert alerts == [LoginFailuresExceeded(ip="10.0.0.1", count=11)]
    limiter.record_failure(Scope.STAFF_LOGIN, "10.0.0.1")
    limiter.record_failure(Scope.STAFF_LOGIN, "10.0.0.2")
    assert len(alerts) == 1  # not again this hour, and not for another address

    clock.advance(3600)
    for _ in range(ALERT_THRESHOLD + 1):
        limiter.record_failure(Scope.STAFF_LOGIN, "10.0.0.1")
    assert len(alerts) == 2


def test_alert_callback_is_optional(clock: Clock) -> None:
    limiter = RateLimiter(clock=clock)
    for _ in range(ALERT_THRESHOLD + 2):
        limiter.record_failure(Scope.STAFF_LOGIN, "10.0.0.1")


def test_signal_file_clears_everything_and_is_consumed(
    limiter: RateLimiter, tmp_path: Path
) -> None:
    fail(limiter, Scope.STAFF_LOGIN, "10.0.0.1", 5)
    fail(limiter, Scope.HIRER_PIN, "10.0.0.2", 3)
    signal = tmp_path / "clear-lockouts"
    signal.touch()
    limiter.check(Scope.STAFF_LOGIN, "10.0.0.1")
    limiter.check(Scope.HIRER_PIN, "10.0.0.2")
    assert not signal.exists()
    assert limiter.consume_signal() is False


def test_no_signal_path_means_no_signal(clock: Clock) -> None:
    limiter = RateLimiter(clock=clock)
    assert limiter.signal_path is None
    assert limiter.consume_signal() is False
