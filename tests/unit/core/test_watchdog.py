"""Application watchdog task (spec §4.7)."""

from __future__ import annotations

import pytest

from proskenion.core.watchdog import (
    LAG_WINDOW_S,
    NOTIFY_INTERVAL_S,
    SdNotifier,
    WatchdogTask,
)


class FakeLoop:
    """A fake clock and sleeper. Each sleep overshoots by the next scheduled lag."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.lags: list[float] = []
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        extra = self.lags.pop(0) if self.lags else 0.0
        self.now += delay + extra


class FakeNotifier:
    def __init__(self, *, succeed: bool = True) -> None:
        self.messages: list[str] = []
        self.succeed = succeed

    def __call__(self, state: str) -> bool:
        self.messages.append(state)
        return self.succeed


def make_task(
    loop: FakeLoop, notifier: FakeNotifier | None = None, *, window_s: float = LAG_WINDOW_S
) -> WatchdogTask:
    return WatchdogTask(
        notify=notifier or FakeNotifier(), clock=loop.clock, sleep=loop.sleep, window_s=window_s
    )


def test_defaults_match_spec() -> None:
    assert NOTIFY_INTERVAL_S == 10.0
    assert LAG_WINDOW_S == 300.0


async def test_tick_sends_watchdog_notification_every_interval() -> None:
    loop = FakeLoop()
    notifier = FakeNotifier()
    task = make_task(loop, notifier)
    for _ in range(3):
        await task.tick()
    assert notifier.messages == ["WATCHDOG=1"] * 3
    assert loop.sleeps == [10.0, 10.0, 10.0]
    assert task.notifications_sent == 3


async def test_lag_is_intended_wake_minus_actual_wake() -> None:
    loop = FakeLoop()
    loop.lags = [0.0, 0.025, 0.4]
    task = make_task(loop)
    assert await task.tick() == pytest.approx(0.0)
    assert await task.tick() == pytest.approx(25.0)
    assert await task.tick() == pytest.approx(400.0)
    assert task.sample_count == 3


async def test_percentiles_over_samples() -> None:
    loop = FakeLoop()
    # 100 samples: 1 ms .. 100 ms, spanning 1000 s, so widen the window.
    loop.lags = [i / 1000.0 for i in range(1, 101)]
    task = make_task(loop, window_s=10_000.0)
    for _ in range(100):
        await task.tick()
    assert task.lag_p50_ms == pytest.approx(50.0)
    assert task.lag_p99_ms == pytest.approx(99.0)


async def test_percentiles_are_none_without_samples() -> None:
    task = make_task(FakeLoop())
    assert task.lag_p50_ms is None
    assert task.lag_p99_ms is None


async def test_window_drops_samples_older_than_five_minutes() -> None:
    loop = FakeLoop()
    loop.lags = [0.9] + [0.0] * 40
    task = make_task(loop)
    await task.tick()  # one 900 ms stall
    assert task.lag_p99_ms == pytest.approx(900.0)
    # Twenty-nine more ticks: the stall sits ~300 s in the past and is pruned;
    # subsequent ticks are 10 s apart.
    for _ in range(29):
        await task.tick()
    assert task.lag_p99_ms == pytest.approx(900.0)
    await task.tick()
    assert task.lag_p99_ms == pytest.approx(0.0)
    assert task.sample_count == 30


async def test_one_long_stall_yields_one_sample_not_a_burst() -> None:
    loop = FakeLoop()
    loop.lags = [45.0, 0.0, 0.0]
    task = make_task(loop)
    await task.tick()
    await task.tick()
    await task.tick()
    assert loop.sleeps == [10.0, 10.0, 10.0]
    assert task.lag_p50_ms == pytest.approx(0.0)
    assert task.lag_p99_ms == pytest.approx(45_000.0)


async def test_run_loops_until_cancelled_and_sends_stopping() -> None:
    import asyncio

    notifier = FakeNotifier()
    ticks = 0

    async def sleep(_: float) -> None:
        nonlocal ticks
        ticks += 1
        if ticks >= 5:
            raise asyncio.CancelledError
        await asyncio.sleep(0)

    task = WatchdogTask(notify=notifier, clock=lambda: 0.0, sleep=sleep)
    with pytest.raises(asyncio.CancelledError):
        await task.run()
    assert notifier.messages == ["WATCHDOG=1"] * 4 + ["STOPPING=1"]


def test_ready_status_and_stopping_messages() -> None:
    notifier = FakeNotifier()
    task = WatchdogTask(notify=notifier, clock=lambda: 0.0)
    task.notify_ready()
    task.notify_status("serving 3 clients\nsecond line")
    task.notify_stopping()
    assert notifier.messages == [
        "READY=1",
        "STATUS=serving 3 clients second line",
        "STOPPING=1",
    ]


async def test_failed_notification_is_not_counted_and_never_raises() -> None:
    loop = FakeLoop()
    task = make_task(loop, FakeNotifier(succeed=False))
    await task.tick()
    assert task.notifications_sent == 0
    assert task.sample_count == 1


def test_sd_notifier_without_socket_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    notifier = SdNotifier()
    assert notifier.enabled is False
    assert notifier.notify("WATCHDOG=1") is False
    assert notifier.failures == 0


def test_sd_notifier_with_missing_socket_never_raises(tmp_path: object) -> None:
    notifier = SdNotifier(socket_path=f"{tmp_path}/no-such-notify-socket")
    assert notifier.notify("WATCHDOG=1") is False
    notifier.close()


def test_invalid_interval_rejected() -> None:
    with pytest.raises(ValueError):
        WatchdogTask(notify=FakeNotifier(), interval_s=0)
