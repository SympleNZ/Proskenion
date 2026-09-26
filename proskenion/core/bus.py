"""In-process event bus (spec §5.6).

Publish/subscribe with asynchronous delivery: :meth:`EventBus.emit` enqueues
an event for every subscriber of its ``TYPE`` and returns at once, so it can
be called from synchronous code (a state-store write, a protocol parser);
each subscriber's handler runs in that subscriber's own task, in order.

That task exists only while the subscriber has work. Enqueueing an event for
a subscriber with nothing running starts its consumer; the consumer drains
the queue and ends when it finds it empty. A subscriber therefore never has
two consumers, a slow one still holds back nobody but itself, and an idle
appliance with forty subscriptions is not also running forty tasks parked on
empty queues (§23.3 counts asyncio tasks).

Subscribers are isolated. A handler that raises has the exception logged with
the event type and subscriber name; it never reaches the emitter or another
subscriber. A handler that raises on :data:`MAX_CONSECUTIVE_FAILURES` events
in a row is unsubscribed and the fact surfaces in :meth:`EventBus.health`
— a permanently broken consumer must not burn CPU silently for the rest of
the deployment. One success resets the count.

Every subscription has a bounded queue and one of two overflow classes:

``continuous``
    Levels, fader positions, observed values. When the queue is full the
    oldest event is dropped and the drop counted — the newest value is the
    truth and a stale one is worthless. Drops per consumer, the count over
    the last five minutes and the number of consecutive five-minute windows
    with drops are exposed in :meth:`EventBus.health` for §11.2
    (``vitals.classify("bus_drop_count", …)`` and
    ``"bus_drop_consecutive_windows"``).

``discrete``
    Device status, scene results, mute toggles, banners. Never dropped. When
    the queue is full the producer is held back and the stall logged with its
    duration. ``await bus.emit_and_wait(event)`` gives a producer real
    back-pressure: it does not return until every subscriber has room. A
    synchronous ``bus.emit`` cannot block, so a discrete event that finds a
    full queue is parked in an ordered backlog and pushed into the queue by
    a pump task as room appears — still never dropped, still logged as a
    stall, but the synchronous producer itself carries on. (The backlog is
    unbounded by necessity: the alternative is dropping a discrete event.)

The class is a property of the subscription, not the event: the same event
type may be consumed by a continuous broadcaster queue and a discrete
logger. Events emitted before :meth:`EventBus.start` wait in their queues and
are delivered once the bus starts, which lets boot-time components emit
before the consumers exist.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, overload

from proskenion.core.events import Event

log = logging.getLogger(__name__)

OverflowClass = Literal["continuous", "discrete"]

#: Events a subscription's queue holds before the overflow class applies.
DEFAULT_QUEUE_SIZE = 256
#: Consecutive handler failures after which a subscriber is unsubscribed (§5.6).
MAX_CONSECUTIVE_FAILURES = 10
#: The §11.2 "in 5 min" window for continuous drop counts.
DROP_WINDOW_S = 300.0
#: How long :meth:`EventBus.stop` waits for each queue to empty before cancelling.
DEFAULT_DRAIN_TIMEOUT_S = 5.0

Handler = Callable[[Any], Awaitable[None]]
Clock = Callable[[], float]


class SubscriptionError(ValueError):
    """A subscription request that cannot be honoured (duplicate name, bad size)."""


@dataclass(frozen=True, slots=True)
class SubscriberHealth:
    """One subscriber's row on the health screen."""

    name: str
    event_type: str
    cls: OverflowClass
    queue_size: int
    queue_depth: int
    drops: int
    failures: int
    consecutive_failures: int
    stalls: int
    stall_seconds: float
    unsubscribed: bool


@dataclass(frozen=True, slots=True)
class BusHealth:
    """What the health screen (§11.2) and email alerts read."""

    subscribers: dict[str, SubscriberHealth]
    unsubscribed: frozenset[str]
    drop_count: int
    """Continuous drops across every consumer since the bus was created."""
    drop_count_window: int
    """Drops in the last :data:`DROP_WINDOW_S` — ``vitals.classify("bus_drop_count", …)``."""
    drop_consecutive_windows: int
    """Consecutive five-minute windows with at least one drop, the current
    window included — ``vitals.classify("bus_drop_consecutive_windows", …)``."""


class _DropWindow:
    """Rolling drop statistics: a sliding five-minute count and consecutive windows."""

    def __init__(self, clock: Clock, window_s: float) -> None:
        self._clock = clock
        self._window_s = window_s
        self._buckets: deque[tuple[int, int]] = deque()  # (whole second, drops)
        self._window_start = clock()
        self._window_drops = 0
        self._consecutive = 0
        self.total = 0

    def record(self, count: int = 1) -> None:
        now = self._clock()
        self._roll(now)
        self.total += count
        self._window_drops += count
        second = int(now)
        if self._buckets and self._buckets[-1][0] == second:
            self._buckets[-1] = (second, self._buckets[-1][1] + count)
        else:
            self._buckets.append((second, count))

    def count_in_window(self) -> int:
        self._roll(self._clock())
        return sum(count for _, count in self._buckets)

    def consecutive_windows(self) -> int:
        self._roll(self._clock())
        return self._consecutive + (1 if self._window_drops else 0)

    def _roll(self, now: float) -> None:
        elapsed_windows = int((now - self._window_start) // self._window_s)
        if elapsed_windows >= 1:
            # The window that just closed extends the run only if it had drops;
            # any further empty windows in between break the run.
            self._consecutive = self._consecutive + 1 if self._window_drops else 0
            if elapsed_windows >= 2:
                self._consecutive = 0
            self._window_drops = 0
            self._window_start += elapsed_windows * self._window_s
        cutoff = now - self._window_s
        while self._buckets and self._buckets[0][0] < cutoff:
            self._buckets.popleft()


class Subscription:
    """One subscriber's queue, task and counters. Returned by :meth:`EventBus.subscribe`."""

    def __init__(
        self,
        event_type: str,
        handler: Handler,
        *,
        name: str,
        cls: OverflowClass,
        queue_size: int,
    ) -> None:
        self.event_type = event_type
        self.handler = handler
        self.name = name
        self.cls: OverflowClass = cls
        self.queue_size = queue_size
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=queue_size)
        # Discrete events waiting for room, in emit order, with the future an
        # ``emit_and_wait`` caller is blocked on (``None`` for plain ``emit``).
        self.backlog: deque[tuple[Event, asyncio.Future[None] | None]] = deque()
        self.pump: asyncio.Task[None] | None = None
        self.task: asyncio.Task[None] | None = None
        self.drops = 0
        self.failures = 0
        self.consecutive_failures = 0
        self.stalls = 0
        self.stall_seconds = 0.0
        self.unsubscribed = False

    def health(self) -> SubscriberHealth:
        return SubscriberHealth(
            name=self.name,
            event_type=self.event_type,
            cls=self.cls,
            queue_size=self.queue_size,
            queue_depth=self.queue.qsize() + len(self.backlog),
            drops=self.drops,
            failures=self.failures,
            consecutive_failures=self.consecutive_failures,
            stalls=self.stalls,
            stall_seconds=self.stall_seconds,
            unsubscribed=self.unsubscribed,
        )


class EventBus:
    """The bus. One per process; components receive it at construction."""

    def __init__(
        self,
        *,
        default_queue_size: int = DEFAULT_QUEUE_SIZE,
        max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES,
        drop_window_s: float = DROP_WINDOW_S,
        drain_timeout_s: float = DEFAULT_DRAIN_TIMEOUT_S,
        clock: Clock = time.monotonic,
    ) -> None:
        if default_queue_size < 1:
            raise ValueError("default_queue_size must be at least 1")
        if max_consecutive_failures < 1:
            raise ValueError("max_consecutive_failures must be at least 1")
        self._default_queue_size = default_queue_size
        self._max_failures = max_consecutive_failures
        self._drain_timeout = drain_timeout_s
        self._clock = clock
        self._drops = _DropWindow(clock, drop_window_s)
        self._by_type: dict[str, list[Subscription]] = {}
        self._by_name: dict[str, Subscription] = {}
        self._retired: dict[str, Subscription] = {}
        self._started = False
        self._closed = False
        # Set once stop() has drained and cancelled: no consumer starts after it.
        self._halted = False

    # -- subscriptions -----------------------------------------------------

    @overload
    def subscribe[E: Event](
        self,
        event_type: type[E],
        handler: Callable[[E], Awaitable[None]],
        *,
        name: str,
        cls: OverflowClass = "discrete",
        queue_size: int | None = None,
    ) -> Subscription: ...

    @overload
    def subscribe(
        self,
        event_type: str,
        handler: Callable[[Event], Awaitable[None]],
        *,
        name: str,
        cls: OverflowClass = "discrete",
        queue_size: int | None = None,
    ) -> Subscription: ...

    def subscribe(
        self,
        event_type: str | type[Event],
        handler: Handler,
        *,
        name: str,
        cls: OverflowClass = "discrete",
        queue_size: int | None = None,
    ) -> Subscription:
        """Register ``handler`` for events of ``event_type``.

        ``name`` identifies the subscriber in logs and health and must be
        unique on the bus. ``cls`` is the overflow class (see the module
        docstring); ``queue_size`` bounds the queue. Subscribing after
        :meth:`start` is fine — the first event starts the consumer. A
        subscriber that was unsubscribed for repeated failures may subscribe
        again under the same name, which clears its health entry.
        """
        type_name = event_type if isinstance(event_type, str) else event_type.TYPE
        size = self._default_queue_size if queue_size is None else queue_size
        if size < 1:
            raise SubscriptionError("queue_size must be at least 1")
        if self._closed:
            raise SubscriptionError("the event bus has been stopped")
        if name in self._by_name:
            raise SubscriptionError(f"a subscriber named {name!r} already exists")
        sub = Subscription(type_name, handler, name=name, cls=cls, queue_size=size)
        self._by_type.setdefault(type_name, []).append(sub)
        self._by_name[name] = sub
        self._retired.pop(name, None)
        return sub

    def unsubscribe(self, subscription: Subscription) -> None:
        """Remove a subscription. Events already queued for it are discarded."""
        if self._by_name.get(subscription.name) is not subscription:
            return
        self._detach(subscription)
        self._release_backlog(subscription)
        if subscription.task is not None and not subscription.task.done():
            subscription.task.cancel()
        subscription.task = None

    def subscriptions(self, event_type: str | type[Event] | None = None) -> list[Subscription]:
        """Active subscriptions, optionally for one event type."""
        if event_type is None:
            return list(self._by_name.values())
        type_name = event_type if isinstance(event_type, str) else event_type.TYPE
        return list(self._by_type.get(type_name, ()))

    # -- emitting ----------------------------------------------------------

    def emit(self, event: Event) -> None:
        """Enqueue ``event`` for every subscriber of its type and return.

        Safe from synchronous code. Never raises on behalf of a subscriber.
        A discrete queue that is full parks the event in a backlog (see the
        module docstring) — use :meth:`emit_and_wait` for real back-pressure.
        """
        if self._closed:
            log.debug("event emitted after bus stop discarded", extra={"event_type": event.TYPE})
            return
        for sub in self._subscribers_for(event):
            self._enqueue(sub, event, None)

    async def emit_and_wait(self, event: Event) -> None:
        """Enqueue ``event`` and wait until every subscriber's queue has taken it.

        For discrete producers that want to be held back rather than run
        ahead of a slow consumer. Returns once the event is *queued*
        everywhere, not once it has been handled. Continuous subscribers
        never hold the producer.
        """
        if self._closed:
            log.debug("event emitted after bus stop discarded", extra={"event_type": event.TYPE})
            return
        waits = [
            future
            for sub in self._subscribers_for(event)
            if (future := self._enqueue(sub, event, self._new_future())) is not None
        ]
        if waits:
            await asyncio.gather(*waits)

    def _subscribers_for(self, event: Event) -> tuple[Subscription, ...]:
        # A tuple copy: a handler may subscribe or unsubscribe while we iterate.
        return tuple(self._by_type.get(event.TYPE, ()))

    @staticmethod
    def _new_future() -> asyncio.Future[None]:
        return asyncio.get_running_loop().create_future()

    def _enqueue(
        self, sub: Subscription, event: Event, future: asyncio.Future[None] | None
    ) -> asyncio.Future[None] | None:
        """Place ``event`` on ``sub``'s queue; return a future only if the caller must wait."""
        if sub.cls == "continuous":
            if sub.queue.full():
                sub.queue.get_nowait()
                sub.queue.task_done()
                sub.drops += 1
                self._drops.record()
            sub.queue.put_nowait(event)
            self._wake(sub)
            return None
        if not sub.backlog and not sub.queue.full():
            sub.queue.put_nowait(event)
            self._wake(sub)
            return None
        sub.backlog.append((event, future))
        self._ensure_pump(sub)
        return future

    def _ensure_pump(self, sub: Subscription) -> None:
        if sub.pump is None or sub.pump.done():
            sub.pump = asyncio.get_running_loop().create_task(
                self._pump(sub), name=f"bus-pump:{sub.name}"
            )

    async def _pump(self, sub: Subscription) -> None:
        """Push a discrete backlog into the queue as room appears, logging the stall."""
        started = self._clock()
        sub.stalls += 1
        log.warning(
            "event bus stall: discrete queue full, producer held back",
            extra={
                "subscriber": sub.name,
                "event_type": sub.event_type,
                "queue_size": sub.queue_size,
            },
        )
        try:
            while sub.backlog:
                event, future = sub.backlog[0]
                await sub.queue.put(event)
                # The consumer that made room may have emptied the queue and
                # ended while this put waited to resume.
                self._wake(sub)
                sub.backlog.popleft()
                if future is not None and not future.done():
                    future.set_result(None)
        finally:
            duration = self._clock() - started
            sub.stall_seconds += duration
            log.warning(
                "event bus stall ended",
                extra={
                    "subscriber": sub.name,
                    "event_type": sub.event_type,
                    "duration_ms": round(duration * 1000, 1),
                    "backlog": len(sub.backlog),
                },
            )

    # -- delivery ----------------------------------------------------------

    def _wake(self, sub: Subscription) -> None:
        """Start ``sub``'s consumer if it has work and none is running.

        Synchronous, like the check in :meth:`_consume` that ends a consumer
        on an empty queue: with no ``await`` between them, an event can never
        land in a queue whose consumer has already decided to stop.
        """
        if not self._started or self._halted or sub.queue.empty():
            return
        if self._by_name.get(sub.name) is not sub:
            return  # unsubscribed or retired; its queue is abandoned
        if sub.task is not None and not sub.task.done():
            return
        sub.task = asyncio.get_running_loop().create_task(
            self._consume(sub), name=f"bus-consumer:{sub.name}"
        )

    async def _consume(self, sub: Subscription) -> None:
        """Handle ``sub``'s queued events in order; end when the queue is empty."""
        while True:
            try:
                event = sub.queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            failed = False
            try:
                await sub.handler(event)
            except Exception:
                failed = True
                sub.failures += 1
                sub.consecutive_failures += 1
                log.exception(
                    "event subscriber raised",
                    extra={
                        "event_type": event.TYPE,
                        "subscriber": sub.name,
                        "consecutive_failures": sub.consecutive_failures,
                    },
                )
            else:
                sub.consecutive_failures = 0
            finally:
                sub.queue.task_done()
            if failed and sub.consecutive_failures >= self._max_failures:
                log.error(
                    "event subscriber unsubscribed after repeated failures",
                    extra={
                        "event_type": sub.event_type,
                        "subscriber": sub.name,
                        "consecutive_failures": sub.consecutive_failures,
                    },
                )
                self._retire(sub)
                return

    def _retire(self, sub: Subscription) -> None:
        self._detach(sub)
        sub.unsubscribed = True
        self._retired[sub.name] = sub
        self._release_backlog(sub)

    def _detach(self, sub: Subscription) -> None:
        self._by_name.pop(sub.name, None)
        siblings = self._by_type.get(sub.event_type)
        if siblings is not None:
            with_sub = [s for s in siblings if s is not sub]
            if with_sub:
                self._by_type[sub.event_type] = with_sub
            else:
                del self._by_type[sub.event_type]

    @staticmethod
    def _release_backlog(sub: Subscription) -> None:
        """Let producers blocked on a dead consumer go; their events are discarded."""
        if sub.pump is not None and not sub.pump.done():
            sub.pump.cancel()
        while sub.backlog:
            _, future = sub.backlog.popleft()
            if future is not None and not future.done():
                future.set_result(None)

    # -- lifecycle ---------------------------------------------------------

    @property
    def started(self) -> bool:
        return self._started and not self._closed

    async def start(self) -> None:
        """Begin delivery: consume what was emitted before now. Idempotent."""
        if self._closed:
            raise RuntimeError("the event bus has been stopped")
        self._started = True
        for sub in list(self._by_name.values()):
            self._wake(sub)

    async def stop(self) -> None:
        """Drain every queue (bounded by the drain timeout), then cancel the tasks.

        Events emitted after this returns are discarded. Safe to call twice.
        """
        if self._closed:
            return
        self._closed = True
        subs = list(self._by_name.values())
        if self._started:
            for sub in subs:
                try:
                    await asyncio.wait_for(self._drained(sub), self._drain_timeout)
                except TimeoutError:
                    log.warning(
                        "event bus stop: subscriber did not drain in time",
                        extra={
                            "subscriber": sub.name,
                            "pending": sub.queue.qsize() + len(sub.backlog),
                        },
                    )
        self._halted = True
        tasks: list[asyncio.Task[None]] = []
        for sub in subs:
            self._release_backlog(sub)
            if sub.task is not None:
                sub.task.cancel()
                tasks.append(sub.task)
            if sub.pump is not None:
                tasks.append(sub.pump)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    async def _drained(sub: Subscription) -> None:
        if sub.pump is not None and not sub.pump.done():
            await asyncio.wait({sub.pump})
        if sub.task is not None and not sub.task.done():
            await sub.queue.join()

    # -- health ------------------------------------------------------------

    def health(self) -> BusHealth:
        rows: dict[str, SubscriberHealth] = {
            name: sub.health() for name, sub in self._by_name.items()
        }
        for name, sub in self._retired.items():
            rows.setdefault(name, sub.health())
        return BusHealth(
            subscribers=rows,
            unsubscribed=frozenset(self._retired),
            drop_count=self._drops.total,
            drop_count_window=self._drops.count_in_window(),
            drop_consecutive_windows=self._drops.consecutive_windows(),
        )
