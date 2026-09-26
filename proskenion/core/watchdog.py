"""Application watchdog task (spec §4.7).

``auditorium-core.service`` declares ``WatchdogSec=30s``. :class:`WatchdogTask`
sends ``WATCHDOG=1`` over the ``NOTIFY_SOCKET`` every ten seconds; if the event
loop stalls the notifications stop and systemd restarts the service. The task
performs no I/O beyond that one datagram — a blocked loop is exactly what it
exists to detect, so the notify is the canary, not merely a liveness ping.

Because it knows when it intended to wake and when it actually did, event-loop
lag comes for free. It keeps a rolling five-minute window and exposes the p50
and p99 for the health screen (§11.2); ``classify("loop_lag_p99_ms", …)`` in
:mod:`proskenion.core.vitals` colours it.

The 60-second suspension of ``WatchdogSec`` after an update (§4.7, §14.5) is
not this module's concern: it is applied by the updater as a systemd unit
drop-in (``WatchdogSec=0`` for the first start, or an ``EXTEND_TIMEOUT_USEC``
sent by the unit's ``ExecStartPre``), under ``appliance/systemd/``. The
application keeps notifying on the same schedule regardless.

``sd_notify`` is implemented here with the stdlib socket module; there is no
``sdnotify`` dependency. When ``NOTIFY_SOCKET`` is unset — every developer
machine, and any test — nothing is sent, but lag is still measured.
"""

from __future__ import annotations

import asyncio
import math
import os
import socket
import time
from collections import deque
from collections.abc import Awaitable, Callable

#: How often ``WATCHDOG=1`` is sent. A third of ``WatchdogSec=30s`` (§4.7).
NOTIFY_INTERVAL_S = 10.0
#: Lag samples older than this are dropped from the percentile window (§4.7).
LAG_WINDOW_S = 300.0

Notifier = Callable[[str], bool]


class SdNotifier:
    """Sends ``sd_notify(3)`` state strings to ``$NOTIFY_SOCKET``.

    A datagram to a Unix socket; abstract-namespace addresses start with ``@``.
    Sending never raises: a missing socket, an unsupported platform or a full
    buffer all return ``False``. The socket is opened lazily and non-blocking.
    """

    def __init__(self, socket_path: str | None = None) -> None:
        self._path = os.environ.get("NOTIFY_SOCKET") if socket_path is None else socket_path
        self._sock: socket.socket | None = None
        self.failures = 0

    @property
    def enabled(self) -> bool:
        return bool(self._path) and _AF_UNIX is not None

    def __call__(self, state: str) -> bool:
        return self.notify(state)

    def notify(self, state: str) -> bool:
        if not self._path or _AF_UNIX is None:
            return False
        address = "\0" + self._path[1:] if self._path.startswith("@") else self._path
        try:
            if self._sock is None:
                self._sock = socket.socket(_AF_UNIX, socket.SOCK_DGRAM)
                self._sock.setblocking(False)
            self._sock.sendto(state.encode("utf-8"), address)
        except OSError:
            self.failures += 1
            return False
        return True

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None


_AF_UNIX: int | None = getattr(socket, "AF_UNIX", None)


class WatchdogTask:
    """The ten-second ``WATCHDOG=1`` heartbeat and event-loop lag monitor.

    ``clock`` and ``sleep`` are injectable so tests can drive the schedule with
    a fake clock; production uses ``time.monotonic`` and ``asyncio.sleep``.
    """

    def __init__(
        self,
        *,
        notify: Notifier | None = None,
        interval_s: float = NOTIFY_INTERVAL_S,
        window_s: float = LAG_WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        self._notify: Notifier = SdNotifier() if notify is None else notify
        self._interval = interval_s
        self._window = window_s
        self._clock = clock
        self._sleep = sleep
        self._samples: deque[tuple[float, float]] = deque()  # (wake time, lag ms)
        self._next_wake: float | None = None
        self.notifications_sent = 0

    # -- lag ---------------------------------------------------------------

    @property
    def lag_p50_ms(self) -> float | None:
        return self._percentile(50.0)

    @property
    def lag_p99_ms(self) -> float | None:
        return self._percentile(99.0)

    @property
    def sample_count(self) -> int:
        return len(self._samples)

    def _percentile(self, percent: float) -> float | None:
        self._prune(self._clock())
        if not self._samples:
            return None
        values = sorted(lag for _, lag in self._samples)
        rank = max(1, math.ceil(percent / 100.0 * len(values)))  # nearest rank
        return values[rank - 1]

    def _prune(self, now: float) -> None:
        cutoff = now - self._window
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    # -- schedule ----------------------------------------------------------

    async def tick(self) -> float:
        """Sleep until the next intended wake, measure the lag, notify. Returns lag in ms."""
        now = self._clock()
        if self._next_wake is None:
            self._next_wake = now + self._interval
        intended = self._next_wake
        await self._sleep(max(0.0, intended - now))
        actual = self._clock()
        lag_ms = max(0.0, actual - intended) * 1000.0
        self._samples.append((actual, lag_ms))
        self._prune(actual)
        # Schedule from the actual wake, not the intended one, so one long stall
        # yields one large sample rather than a burst of catch-up ticks.
        self._next_wake = actual + self._interval
        if self._notify("WATCHDOG=1"):
            self.notifications_sent += 1
        return lag_ms

    async def run(self) -> None:
        """Run until cancelled. Sends ``STOPPING=1`` on the way out."""
        try:
            while True:
                await self.tick()
        finally:
            self.notify_stopping()

    # -- other sd_notify states ---------------------------------------------

    def notify_ready(self) -> bool:
        """``READY=1`` — the service has finished starting (``Type=notify``)."""
        return self._notify("READY=1")

    def notify_status(self, text: str) -> bool:
        """``STATUS=…`` — a one-line human-readable status shown by ``systemctl status``."""
        return self._notify("STATUS=" + " ".join(text.splitlines()))

    def notify_stopping(self) -> bool:
        return self._notify("STOPPING=1")
