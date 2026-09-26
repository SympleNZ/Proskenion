"""A socket's liveness and absolute expiry, run as loop timers (§16.8, §6.4, §23.3)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from proskenion.api.ws import CLOSE_EXPIRED, CLOSE_LIVENESS, _Timers
from proskenion.core.broadcast import Connection

INTERVAL = 0.05


def timers_for(
    connection: Connection,
    *,
    expires_in: float = 3600.0,
    recheck: float = 60.0,
    now: float | None = None,
) -> _Timers:
    return _Timers(
        connection,
        ping_interval=INTERVAL,
        now=time.monotonic if now is None else (lambda: now),
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        token_now=lambda: datetime.now(UTC),
        recheck=recheck,
    )


def drain(connection: Connection) -> list[dict[str, object]]:
    messages = []
    while connection.queued:
        messages.append(dict(connection._queue.popleft().message))  # noqa: SLF001
    return messages


async def test_pings_every_interval_while_pongs_arrive() -> None:
    connection = Connection(1, tier="admin")
    connection.last_pong = time.monotonic()
    timers = timers_for(connection)
    timers.start()
    try:
        pings = 0
        for _ in range(4):
            await asyncio.sleep(INTERVAL)
            connection.last_pong = time.monotonic()
            pings += sum(1 for m in drain(connection) if m == {"type": "ping"})
        assert pings >= 3
        assert not connection.closed
        assert not timers.ended.done()
    finally:
        timers.stop()


async def test_two_intervals_without_a_pong_close_the_socket_and_end() -> None:
    connection = Connection(1, tier="admin")
    connection.last_pong = time.monotonic()
    timers = timers_for(connection)
    timers.start()
    await asyncio.wait_for(timers.ended, 1.0)
    assert connection.closed
    assert connection.close_code == CLOSE_LIVENESS
    timers.stop()


async def test_liveness_still_ends_a_socket_something_else_already_closed() -> None:
    # A close whose writer is stuck must still end: liveness is the backstop.
    connection = Connection(1, tier="admin")
    connection.last_pong = time.monotonic()
    connection.close("revoked", code=4401)
    timers = timers_for(connection)
    timers.start()
    await asyncio.wait_for(timers.ended, 1.0)
    assert connection.close_code == 4401  # the first reason stands
    timers.stop()


async def test_the_absolute_expiry_closes_with_4002() -> None:
    connection = Connection(1, tier="hirer")
    timers = timers_for(connection, expires_in=0.05, recheck=0.02, now=float("inf"))
    connection.last_pong = float("inf")  # liveness never gives up here
    timers.start()
    await asyncio.wait_for(timers.ended, 1.0)
    assert connection.close_code == CLOSE_EXPIRED
    timers.stop()


async def test_an_already_expired_session_closes_at_once() -> None:
    connection = Connection(1, tier="hirer")
    connection.last_pong = float("inf")
    timers = timers_for(connection, expires_in=-1.0, now=float("inf"))
    timers.start()
    await asyncio.wait_for(timers.ended, 0.5)
    assert connection.close_code == CLOSE_EXPIRED
    timers.stop()


async def test_a_timer_that_raises_ends_with_the_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = Connection(1, tier="admin")
    connection.last_pong = time.monotonic()

    def broken(message: object, **_: object) -> bool:
        raise RuntimeError("send failed")

    monkeypatch.setattr(connection, "send", broken)
    timers = timers_for(connection)
    timers.start()
    with pytest.raises(RuntimeError, match="send failed"):
        await asyncio.wait_for(timers.ended, 1.0)
    timers.stop()


async def test_stop_cancels_both_timers() -> None:
    connection = Connection(1, tier="admin")
    connection.last_pong = time.monotonic()
    timers = timers_for(connection)
    timers.start()
    timers.stop()
    await asyncio.sleep(INTERVAL * 3)
    assert drain(connection) == []
    assert not connection.closed
    assert timers.ended.cancelled()


AUCKLAND = ZoneInfo("Pacific/Auckland")


def auckland_clock(start: datetime) -> Callable[[], datetime]:
    """Real time running forward from ``start``, as the token service reads it."""
    began = time.monotonic()
    return lambda: (start.astimezone(UTC) + timedelta(seconds=time.monotonic() - began)).astimezone(
        AUCKLAND
    )


def auckland_timers(connection: Connection, *, token_now: datetime, expires_in: float) -> _Timers:
    # Built as the socket builds it: both ends Pacific/Auckland, the expiry
    # decoded from the token's Unix time.
    expires_at = datetime.fromtimestamp(token_now.timestamp() + expires_in, tz=AUCKLAND)
    return _Timers(
        connection,
        ping_interval=INTERVAL,
        now=lambda: float("inf"),
        expires_at=expires_at,
        token_now=auckland_clock(token_now),
        recheck=60.0,
    )


async def test_expiry_near_clocks_going_forward_is_not_an_hour_late() -> None:
    # 01:59:59.95 on 27 September 2026; the session ends 50 ms later, in
    # daylight time. Wall clocks differ by an hour and 50 ms.
    connection = Connection(1, tier="hirer")
    connection.last_pong = float("inf")
    start = datetime(2026, 9, 27, 1, 59, 59, 950000, tzinfo=AUCKLAND)
    timers = auckland_timers(connection, token_now=start, expires_in=0.05)
    timers.start()
    await asyncio.wait_for(timers.ended, 1.0)
    assert connection.close_code == CLOSE_EXPIRED
    timers.stop()


async def test_expiry_near_clocks_going_back_is_not_an_hour_early() -> None:
    # 02:50 daylight time on 4 April 2027, half an hour of session left, which
    # ends at 02:20 standard time: earlier on the wall clock than now.
    connection = Connection(1, tier="hirer")
    connection.last_pong = float("inf")
    start = datetime(2027, 4, 4, 2, 50, tzinfo=AUCKLAND)
    timers = auckland_timers(connection, token_now=start, expires_in=1800.0)
    timers.start()
    try:
        await asyncio.sleep(INTERVAL * 2)
        assert not connection.closed
        assert not timers.ended.done()
    finally:
        timers.stop()
