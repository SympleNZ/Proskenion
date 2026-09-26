"""Event bus (spec §5.6, §22.2): isolation, unsubscribe on failure, queue overflow."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import ClassVar

import pytest

from proskenion.core.bus import (
    DROP_WINDOW_S,
    MAX_CONSECUTIVE_FAILURES,
    EventBus,
    SubscriptionError,
)
from proskenion.core.events import DeviceStatusChanged, Event, TimeSyncRecovered
from proskenion.core.vitals import classify


@dataclass(frozen=True, slots=True)
class Tick(Event):
    TYPE: ClassVar[str] = "test.tick"
    n: int


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Recorder:
    """A handler that records events and can be told to raise."""

    def __init__(self, *, fail_on: set[int] | None = None, fail_always: bool = False) -> None:
        self.seen: list[Event] = []
        self.fail_on = fail_on or set()
        self.fail_always = fail_always

    async def __call__(self, event: Event) -> None:
        self.seen.append(event)
        if self.fail_always or (isinstance(event, Tick) and event.n in self.fail_on):
            raise RuntimeError(f"handler failed on {event!r}")


async def settle(rounds: int = 20) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


@pytest.fixture
async def bus() -> AsyncIterator[EventBus]:
    bus = EventBus()
    await bus.start()
    try:
        yield bus
    finally:
        await bus.stop()


def test_defaults_match_spec() -> None:
    assert MAX_CONSECUTIVE_FAILURES == 10
    assert DROP_WINDOW_S == 300.0


# -- isolation ---------------------------------------------------------------


async def test_raising_subscriber_does_not_reach_emitter_or_other_subscribers(
    bus: EventBus, caplog: pytest.LogCaptureFixture
) -> None:
    bad = Recorder(fail_always=True)
    good = Recorder()
    bus.subscribe(Tick, bad, name="bad")
    bus.subscribe(Tick, good, name="good")

    with caplog.at_level(logging.ERROR, logger="proskenion.core.bus"):
        bus.emit(Tick(1))  # must not raise
        await bus.emit_and_wait(Tick(2))  # must not raise either
        await settle()

    assert [e.n for e in good.seen if isinstance(e, Tick)] == [1, 2]
    assert len(bad.seen) == 2
    record = next(r for r in caplog.records if r.getMessage() == "event subscriber raised")
    assert record.event_type == "test.tick"  # type: ignore[attr-defined]
    assert record.subscriber == "bad"  # type: ignore[attr-defined]


async def test_ten_consecutive_failures_unsubscribe_and_surface_in_health(
    bus: EventBus,
) -> None:
    bad = Recorder(fail_always=True)
    bus.subscribe(Tick, bad, name="bad")
    for n in range(MAX_CONSECUTIVE_FAILURES + 5):
        bus.emit(Tick(n))
    await settle(50)

    assert len(bad.seen) == MAX_CONSECUTIVE_FAILURES  # nothing delivered after the tenth
    assert bus.subscriptions(Tick) == []
    health = bus.health()
    assert "bad" in health.unsubscribed
    row = health.subscribers["bad"]
    assert row.unsubscribed is True
    assert row.consecutive_failures == MAX_CONSECUTIVE_FAILURES
    assert row.failures == MAX_CONSECUTIVE_FAILURES


async def test_a_success_between_failures_resets_the_count(bus: EventBus) -> None:
    # Fails on every event except every tenth: never ten in a row.
    flaky = Recorder(fail_on={n for n in range(40) if n % 10 != 9})
    bus.subscribe(Tick, flaky, name="flaky")
    for n in range(40):
        bus.emit(Tick(n))
    await settle(100)

    assert len(flaky.seen) == 40
    assert bus.subscriptions(Tick) != []
    row = bus.health().subscribers["flaky"]
    assert row.unsubscribed is False
    assert row.failures == 36
    assert row.consecutive_failures == 0
    assert bus.health().unsubscribed == frozenset()


async def test_unsubscribed_name_may_subscribe_again(bus: EventBus) -> None:
    bus.subscribe(Tick, Recorder(fail_always=True), name="again")
    for n in range(MAX_CONSECUTIVE_FAILURES):
        bus.emit(Tick(n))
    await settle(50)
    assert "again" in bus.health().unsubscribed

    fresh = Recorder()
    bus.subscribe(Tick, fresh, name="again")
    bus.emit(Tick(99))
    await settle()
    assert [e.n for e in fresh.seen if isinstance(e, Tick)] == [99]
    assert "again" not in bus.health().unsubscribed


# -- overflow: continuous ----------------------------------------------------


async def test_continuous_consumer_drops_oldest_and_counts() -> None:
    clock = FakeClock()
    bus = EventBus(clock=clock)
    seen = Recorder()
    bus.subscribe(Tick, seen, name="levels", cls="continuous", queue_size=2)
    for n in range(5):  # queued before the consumer runs
        bus.emit(Tick(n))
    health = bus.health()
    assert health.subscribers["levels"].drops == 3
    assert health.drop_count == 3
    assert health.drop_count_window == 3
    assert health.drop_consecutive_windows == 1
    assert classify("bus_drop_count", health.drop_count_window) == "amber"
    assert classify("bus_drop_consecutive_windows", health.drop_consecutive_windows) == "amber"

    await bus.start()
    await settle()
    assert [e.n for e in seen.seen if isinstance(e, Tick)] == [3, 4]  # the newest survive
    await bus.stop()


async def test_drop_windows_roll_and_sustained_is_red() -> None:
    clock = FakeClock()
    bus = EventBus(clock=clock)
    bus.subscribe(Tick, Recorder(), name="levels", cls="continuous", queue_size=1)

    bus.emit(Tick(0))
    bus.emit(Tick(1))  # one drop in window 1
    assert bus.health().drop_consecutive_windows == 1

    clock.now += DROP_WINDOW_S + 1  # window 2; the first drop leaves the sliding count
    bus.emit(Tick(2))  # drops
    health = bus.health()
    assert health.drop_consecutive_windows == 2
    assert classify("bus_drop_consecutive_windows", health.drop_consecutive_windows) == "red"
    assert health.drop_count_window == 1  # the sliding count only sees window 2's drop

    clock.now += DROP_WINDOW_S * 2.5  # two empty windows pass
    health = bus.health()
    assert health.drop_consecutive_windows == 0
    assert health.drop_count_window == 0
    assert health.drop_count == 2
    await bus.stop()


# -- overflow: discrete ------------------------------------------------------


async def test_discrete_consumer_blocks_producer_and_logs_stall(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock()
    bus = EventBus(clock=clock)
    gate = asyncio.Event()
    seen: list[Event] = []

    async def slow(event: Event) -> None:
        await gate.wait()
        seen.append(event)

    bus.subscribe(Tick, slow, name="status", cls="discrete", queue_size=1)
    await bus.start()

    await bus.emit_and_wait(Tick(0))  # taken by the consumer, which then blocks on the gate
    await settle()
    await bus.emit_and_wait(Tick(1))  # fills the queue
    with caplog.at_level(logging.WARNING, logger="proskenion.core.bus"):
        producer = asyncio.ensure_future(bus.emit_and_wait(Tick(2)))
        await settle()
        assert not producer.done()  # held back: no room
        clock.now += 1.25
        gate.set()
        await asyncio.wait_for(producer, 1.0)
        await settle()

    assert [e.n for e in seen if isinstance(e, Tick)] == [0, 1, 2]  # nothing dropped
    row = bus.health().subscribers["status"]
    assert row.drops == 0
    assert row.stalls == 1
    assert row.stall_seconds == pytest.approx(1.25)
    messages = [r for r in caplog.records if r.getMessage() == "event bus stall ended"]
    assert messages and messages[0].duration_ms == pytest.approx(1250.0)  # type: ignore[attr-defined]
    await bus.stop()


async def test_synchronous_emit_never_drops_discrete_events(bus: EventBus) -> None:
    seen = Recorder()
    bus.subscribe(Tick, seen, name="status", cls="discrete", queue_size=1)
    for n in range(20):
        bus.emit(Tick(n))  # synchronous: cannot block, so overflow is parked, not dropped
    await settle(100)
    assert [e.n for e in seen.seen if isinstance(e, Tick)] == list(range(20))
    row = bus.health().subscribers["status"]
    assert row.drops == 0
    assert row.stalls >= 1


# -- subscription and lifecycle ----------------------------------------------


async def test_subscribe_by_string_and_by_class_are_equivalent(bus: EventBus) -> None:
    by_string = Recorder()
    by_class = Recorder()
    bus.subscribe("devices.status_changed", by_string, name="s")
    bus.subscribe(DeviceStatusChanged, by_class, name="c")
    event = DeviceStatusChanged("mixer", "connected")
    bus.emit(event)
    bus.emit(TimeSyncRecovered())
    await settle()
    assert by_string.seen == [event]
    assert by_class.seen == [event]


async def test_duplicate_subscriber_name_is_rejected(bus: EventBus) -> None:
    bus.subscribe(Tick, Recorder(), name="one")
    with pytest.raises(SubscriptionError):
        bus.subscribe(Tick, Recorder(), name="one")
    with pytest.raises(SubscriptionError):
        bus.subscribe(Tick, Recorder(), name="zero", queue_size=0)


async def test_unsubscribe_stops_delivery(bus: EventBus) -> None:
    seen = Recorder()
    sub = bus.subscribe(Tick, seen, name="s")
    bus.emit(Tick(1))
    await settle()
    bus.unsubscribe(sub)
    bus.emit(Tick(2))
    await settle()
    assert [e.n for e in seen.seen if isinstance(e, Tick)] == [1]
    assert "s" not in bus.health().subscribers


async def test_stop_drains_queued_events_then_discards_later_ones() -> None:
    bus = EventBus()
    seen = Recorder()
    bus.subscribe(Tick, seen, name="s", cls="discrete", queue_size=1)
    await bus.start()
    for n in range(5):
        bus.emit(Tick(n))
    await bus.stop()
    assert [e.n for e in seen.seen if isinstance(e, Tick)] == list(range(5))
    bus.emit(Tick(99))  # discarded, never raises
    await bus.stop()  # idempotent
    assert bus.started is False
    with pytest.raises(SubscriptionError):
        bus.subscribe(Tick, Recorder(), name="late")


async def test_stop_gives_up_on_a_consumer_that_never_drains(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bus = EventBus(drain_timeout_s=0.05)

    async def stuck(event: Event) -> None:
        await asyncio.Event().wait()

    bus.subscribe(Tick, stuck, name="stuck")
    await bus.start()
    bus.emit(Tick(1))
    bus.emit(Tick(2))
    with caplog.at_level(logging.WARNING, logger="proskenion.core.bus"):
        await asyncio.wait_for(bus.stop(), 2.0)
    assert any("did not drain" in r.getMessage() for r in caplog.records)


# -- consumer tasks exist only while there is work (§23.3) --------------------


def consumer_tasks() -> list[asyncio.Task[object]]:
    return [t for t in asyncio.all_tasks() if t.get_name().startswith("bus-consumer:")]


async def test_an_idle_bus_runs_no_consumer_tasks(bus: EventBus) -> None:
    for n in range(40):
        bus.subscribe(Tick, Recorder(), name=f"idle-{n}")
    await settle()
    assert consumer_tasks() == []


async def test_a_consumer_starts_on_an_event_and_ends_once_its_queue_is_empty(
    bus: EventBus,
) -> None:
    gate = asyncio.Event()
    seen: list[int] = []

    async def held(event: Event) -> None:
        await gate.wait()
        assert isinstance(event, Tick)
        seen.append(event.n)

    bus.subscribe(Tick, held, name="held")
    for n in range(3):
        bus.emit(Tick(n))
    await settle()
    assert [t.get_name() for t in consumer_tasks()] == ["bus-consumer:held"]  # exactly one
    gate.set()
    await settle()
    assert seen == [0, 1, 2]
    assert consumer_tasks() == []
    bus.emit(Tick(3))  # a later event starts a fresh consumer, in order
    await settle()
    assert seen == [0, 1, 2, 3]
    assert consumer_tasks() == []


async def test_a_backlog_pushed_after_the_consumer_ended_is_still_delivered(
    bus: EventBus,
) -> None:
    # The pump waits for room; the consumer that made it may empty the queue
    # and end before the pump's put resumes. The pump must start another.
    seen = Recorder()
    bus.subscribe(Tick, seen, name="status", cls="discrete", queue_size=1)
    for n in range(10):
        bus.emit(Tick(n))
    await settle(100)
    assert [e.n for e in seen.seen if isinstance(e, Tick)] == list(range(10))
    assert consumer_tasks() == []


async def test_a_handler_that_emits_to_itself_is_served_by_the_same_consumer(
    bus: EventBus,
) -> None:
    seen: list[int] = []

    async def chain(event: Event) -> None:
        assert isinstance(event, Tick)
        seen.append(event.n)
        if event.n < 5:
            bus.emit(Tick(event.n + 1))

    bus.subscribe(Tick, chain, name="chain")
    bus.emit(Tick(0))
    await settle(50)
    assert seen == [0, 1, 2, 3, 4, 5]
    assert consumer_tasks() == []
