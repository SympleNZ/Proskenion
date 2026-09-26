"""Per-address rate limiting for the login endpoints (spec §6.8).

=============  ==========================  ==========
endpoint       limit                       lockout
=============  ==========================  ==========
staff login    5 attempts per 5 minutes    15 minutes
hirer PIN      3 attempts per 10 minutes   30 minutes
=============  ==========================  ==========

Counters live in memory and reset when the application restarts — accepted
in §6.8: whoever can restart the service already has SSH access. The limiter
keys on the *real* client address, which depends entirely on nginx forwarding
it (§4.13); without that it degrades to a global limit.

More than ten failures from one address within an hour, across both
endpoints, raises the email alert of §6.8. Email arrives with Phase 6; here
the limiter hands a :class:`LoginFailuresExceeded` event to an injected
callback, once per address per hour.

The reset tool (§6.9) cannot reach the counters in a running process, so it
drops a signal file — ``<state_dir>/clear-lockouts`` — that the limiter looks
for and consumes on its next call.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final

log = logging.getLogger(__name__)

CLEAR_LOCKOUTS_FILENAME: Final = "clear-lockouts"

ALERT_THRESHOLD: Final = 10
ALERT_WINDOW_S: Final = 3600.0

MonotonicClock = Callable[[], float]


class LockedOut(Exception):
    """The address is locked out. The API layer renders this as ``rate_limited``."""

    def __init__(self, scope: Scope, ip: str, retry_after: int) -> None:
        super().__init__(f"{ip} is locked out of {scope.value} for {retry_after} s")
        self.scope = scope
        self.ip = ip
        self.retry_after = retry_after


class Scope(StrEnum):
    """Which unauthenticated endpoint an attempt belongs to."""

    STAFF_LOGIN = "staff_login"
    HIRER_PIN = "hirer_pin"
    # The first-run wizard sets passwords without a session (§10.4, §16.4), so
    # the bootstrap path carries the same limit as the hirer PIN rather than
    # being an open password-setting endpoint. Its own scope, so a hirer
    # hammering the PIN cannot lock the installer out and vice versa.
    SETUP_STEP = "setup_step"


@dataclass(frozen=True, slots=True)
class Policy:
    attempts: int
    window_s: float
    lockout_s: float


POLICIES: Final[dict[Scope, Policy]] = {
    Scope.STAFF_LOGIN: Policy(attempts=5, window_s=5 * 60, lockout_s=15 * 60),
    Scope.HIRER_PIN: Policy(attempts=3, window_s=10 * 60, lockout_s=30 * 60),
    Scope.SETUP_STEP: Policy(attempts=3, window_s=10 * 60, lockout_s=30 * 60),
}


@dataclass(frozen=True, slots=True)
class LoginFailuresExceeded:
    """More than :data:`ALERT_THRESHOLD` failures from ``ip`` within an hour."""

    ip: str
    count: int


AlertCallback = Callable[[LoginFailuresExceeded], None]


@dataclass(slots=True)
class _Bucket:
    failures: deque[float] = field(default_factory=deque)
    locked_until: float | None = None


class RateLimiter:
    """In-memory attempt counters per (scope, address).

    ``check`` is called before the credential is examined; ``record_failure``
    and ``record_success`` afterwards. The clock is monotonic seconds and
    injectable so lockouts are testable without waiting.
    """

    def __init__(
        self,
        *,
        clock: MonotonicClock = time.monotonic,
        on_alert: AlertCallback | None = None,
        signal_path: Path | None = None,
        policies: dict[Scope, Policy] | None = None,
    ) -> None:
        self._clock = clock
        self._on_alert = on_alert
        self._signal_path = signal_path
        self._policies = dict(POLICIES if policies is None else policies)
        self._buckets: dict[tuple[Scope, str], _Bucket] = {}
        # Hourly failures per address across every scope, for the alert.
        self._hourly: dict[str, deque[float]] = {}
        self._alerted: dict[str, float] = {}

    @property
    def signal_path(self) -> Path | None:
        return self._signal_path

    def policy(self, scope: Scope) -> Policy:
        return self._policies[scope]

    # -- the signal file (§6.9) ----------------------------------------------

    def consume_signal(self) -> bool:
        """Clear everything if the reset tool left its signal file. True if it did."""
        if self._signal_path is None:
            return False
        try:
            self._signal_path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            log.warning("cannot remove %s: %s", self._signal_path, exc)
            return False
        log.info("rate-limit counters cleared by the reset tool")
        self.clear_all()
        return True

    # -- checks --------------------------------------------------------------

    def retry_after(self, scope: Scope, ip: str) -> int | None:
        """Seconds until ``ip`` may try ``scope`` again, or ``None`` if not locked out."""
        self.consume_signal()
        bucket = self._buckets.get((scope, ip))
        if bucket is None or bucket.locked_until is None:
            return None
        remaining = bucket.locked_until - self._clock()
        if remaining <= 0:
            bucket.locked_until = None
            bucket.failures.clear()
            return None
        return max(1, math.ceil(remaining))

    def check(self, scope: Scope, ip: str) -> None:
        """Raise :class:`LockedOut` if ``ip`` is locked out of ``scope``."""
        retry_after = self.retry_after(scope, ip)
        if retry_after is not None:
            raise LockedOut(scope, ip, retry_after)

    def record_failure(self, scope: Scope, ip: str) -> bool:
        """Count a failed attempt. Returns True if this one started a lockout."""
        now = self._clock()
        policy = self._policies[scope]
        bucket = self._buckets.setdefault((scope, ip), _Bucket())
        self._prune(bucket.failures, now - policy.window_s)
        bucket.failures.append(now)
        locked = False
        if bucket.locked_until is None and len(bucket.failures) >= policy.attempts:
            bucket.locked_until = now + policy.lockout_s
            bucket.failures.clear()
            locked = True
            log.warning("lockout: %s from %s for %d s", scope.value, ip, int(policy.lockout_s))
        self._count_hourly(ip, now)
        return locked

    def record_success(self, scope: Scope, ip: str) -> None:
        """A successful attempt clears the address's counter for that scope."""
        self._buckets.pop((scope, ip), None)

    def clear(self, ip: str) -> None:
        """Forget every counter and lockout for one address."""
        for key in [k for k in self._buckets if k[1] == ip]:
            del self._buckets[key]
        self._hourly.pop(ip, None)
        self._alerted.pop(ip, None)

    def clear_all(self) -> None:
        self._buckets.clear()
        self._hourly.clear()
        self._alerted.clear()

    # -- alert (§6.8) --------------------------------------------------------

    def _count_hourly(self, ip: str, now: float) -> None:
        window = self._hourly.setdefault(ip, deque())
        self._prune(window, now - ALERT_WINDOW_S)
        window.append(now)
        count = len(window)
        if count <= ALERT_THRESHOLD:
            return
        last = self._alerted.get(ip)
        if last is not None and now - last < ALERT_WINDOW_S:
            return
        self._alerted[ip] = now
        log.warning("%d failed login attempts from %s within an hour", count, ip)
        if self._on_alert is not None:
            self._on_alert(LoginFailuresExceeded(ip=ip, count=count))

    @staticmethod
    def _prune(window: deque[float], cutoff: float) -> None:
        while window and window[0] <= cutoff:
            window.popleft()
