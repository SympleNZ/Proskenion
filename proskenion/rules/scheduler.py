"""The ``schedule`` trigger: one task that fires cron rules on time (spec §8.3, §22.7).

Owned by the rules engine
-------------------------
Schedules are one of the rule layer's four trigger sources (§8.3), so the
:class:`~proskenion.rules.engine.RulesEngine` owns this scheduler the way it
owns the KNX and device-state subscriptions: it starts and stops it with
itself, hands it the rules on every :meth:`~RulesEngine.reload`, and is the
:class:`ScheduleTarget` it fires through. A scheduled firing therefore takes
the same path as any other trigger — enabled check, debounce (a no-op: §8.4
makes it null for schedules), guard, action, execution log — and is told
apart downstream only by ``triggered_by = "schedule"``, the spelling §8.16
gives the scene log.

What *when* means is :mod:`proskenion.rules.cron`'s: Pacific/Auckland wall
time, a September gap time fires once at 03:00, an April repeat fires once on
the first pass.

Planning and sleeping
---------------------
Each enabled schedule rule holds one planned instant, in UTC. The loop sleeps
on the monotonic clock until the earliest, but never longer than
:data:`MAX_SLEEP_S`, then re-reads the wall clock and fires whatever is due.
Sleeping in short stretches and re-reading the wall clock on every wake is
what keeps a fire on time when NTP slews the clock under a long sleep.

The plan changes immediately when

* **a rule is created, edited, enabled, disabled or deleted** — the engine's
  reload calls :meth:`Scheduler.replan`, which wakes the loop. A rule whose
  expression is unchanged keeps its planned instant; a new or edited one is
  planned from now.
* **the wall clock steps** — every wake compares how far the wall clock moved
  with how far the monotonic clock did. A difference over
  :data:`CLOCK_STEP_S` is a step: what it skipped over is handled as missed,
  then every rule is planned again from the new now, so a clock that boots
  in the future and is then corrected does not leave rules waiting for it.

Missed fires are skipped, never replayed
----------------------------------------
A planned time that has passed by more than :data:`LATE_LIMIT_S` when the loop
gets to it — the controller was suspended, the loop was blocked, the clock
jumped forward — is not run. It is logged, one ``missed`` entry per rule per
wake with how many were skipped, and the rule is planned from now. A time
passed by less than that is late, not missed, and fires with its lateness in
the log.

Starting up, every rule is planned from now, never from a stored last run.
What passed while the controller was down is logged as missed: the times
after the rule's last scheduled entry in ``rule_execution_log`` (or its last
edit, if it has none) up to now.

Trusting the wall clock (§4.9)
-------------------------------
A schedule is a Pacific/Auckland wall-clock time, and planning one against
a clock that has not been verified — not NTP-synced, and with no RTC to
fall back on — is planning against whatever the OS happened to boot with,
which is exactly the case this whole module exists to be careful about.
While :data:`TimeSyncMonitor.trustworthy` says no, nothing is planned and
nothing fires: :attr:`Scheduler.held` is true, it is logged once, and the
loop just waits and re-checks. The moment it says yes, every rule is
(re)planned from the now-trustworthy clock — with the same "what passed
while down" report :meth:`Scheduler.start` gives if the hold began there,
or a plain replan (like a backward clock step) if trust was merely lost and
regained mid-run.

Never the same minute twice
---------------------------
The last scheduled instant of every rule, fired or missed, is remembered, and
nothing at or before it is planned again. Across a restart, that memory is
the newest ``scheduled_for`` in the rule's execution log, so a restart inside
the minute that just fired does not fire it again, and neither does a clock
stepped back over it. A remembered instant more than :data:`FUTURE_LIMIT_S`
ahead of now is a record of a wrong clock, not of a firing, and is dropped.

Timing (§22.7)
--------------
Every scheduled scene must start within 100 ms of its trigger. Each firing's
log entry carries ``scheduled_for``, ``dispatched_at`` and
``dispatch_latency_ms``, so the soak can measure it from the log.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Protocol

from proskenion.core.tasks import contained, spawn
from proskenion.db.crud.base import AUCKLAND
from proskenion.db.crud.rules import Rule
from proskenion.rules.cron import CronSchedule

log = logging.getLogger(__name__)

#: The longest the loop sleeps before re-reading the wall clock.
MAX_SLEEP_S: Final = 10.0
#: How late a planned time may be dispatched before it counts as missed.
LATE_LIMIT_S: Final = 60.0
#: A wall clock that moved this much more or less than the monotonic one stepped.
CLOCK_STEP_S: Final = 2.0
#: A remembered last firing further ahead of now than this is disregarded.
FUTURE_LIMIT_S: Final = 24 * 60 * 60.0
#: The most missed times counted for one log entry; the rest are skipped uncounted.
MAX_MISSED_COUNTED: Final = 10_000

TRIGGERED_BY: Final = "schedule"


class ScheduleClock(Protocol):
    """Wall time, monotonic time and an interruptible sleep — injectable for tests."""

    def wall(self) -> datetime:
        """Now, timezone-aware."""
        ...

    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float, wake: asyncio.Event) -> None:
        """Return after ``seconds``, or as soon as ``wake`` is set."""
        ...


class SystemClock:
    """The real clocks. Sleeps on the event loop's monotonic timer."""

    def __init__(self, wall: Callable[[], datetime] | None = None) -> None:
        self._wall = wall or (lambda: datetime.now(tz=UTC))

    def wall(self) -> datetime:
        return self._wall()

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float, wake: asyncio.Event) -> None:
        if wake.is_set():
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(wake.wait(), timeout=max(seconds, 0.0))


class ScheduleTarget(Protocol):
    """What the scheduler fires through: the rules engine."""

    def schedule_rules(self) -> Sequence[Rule]:
        """Every enabled ``schedule`` rule, in the order rules run (§8.7)."""
        ...

    async def fire_scheduled(
        self, rule_id: int, *, scheduled_for: datetime, dispatched_at: datetime
    ) -> object: ...

    def record_missed(
        self,
        rule_id: int,
        *,
        scheduled_for: datetime,
        first_missed: datetime,
        count: int,
        reason: str,
    ) -> None: ...


@dataclass(slots=True)
class _Plan:
    cron: str
    schedule: CronSchedule | None
    """``None`` for an expression that does not parse; never fires."""
    next: datetime | None


class Scheduler:
    """Fires ``schedule`` rules at their times. See the module docstring."""

    def __init__(
        self,
        target: ScheduleTarget,
        clock: ScheduleClock | None = None,
        *,
        trusted: Callable[[], bool] | None = None,
    ) -> None:
        self._target = target
        self._clock: ScheduleClock = clock or SystemClock()
        self._plans: dict[int, _Plan] = {}
        self._last: dict[int, datetime] = {}
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stepped_back = False
        self.clock_steps = 0
        """Wall-clock steps seen since start, for health and tests."""
        #: §4.9: whether the wall clock is fit to plan against right now —
        #: synced, or degraded with a trustworthy RTC
        #: (:attr:`~proskenion.core.timesync.TimeSyncMonitor.trustworthy`).
        #: Defaults to always-trustworthy so a caller that does not pass one
        #: (most tests) keeps today's behaviour.
        self._trusted = trusted or (lambda: True)
        #: True while fires are withheld because :attr:`_trusted` says no.
        self._held = False
        #: Set only while held straight from :meth:`start` (a fresh boot with
        #: no verified clock) — the one case that still owes a "what passed
        #: while not running" report once trust arrives, same as §4.9's own
        #: framing of a controller that was off. A later, mid-run loss of
        #: trust (NTP synced, then lost) is not a restart and gets a plain
        #: replan instead, the same as a backward clock step.
        self._boot_hold = False
        self._pending_last_scheduled: Mapping[int, datetime] | None = None

    # -- lifecycle ---------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def held(self) -> bool:
        """True while schedule fires are withheld for an unverified clock (§4.9)."""
        return self._held

    async def start(self, last_scheduled: Mapping[int, datetime] | None = None) -> None:
        """Plan every rule from now, log what passed while stopped, and start the loop.

        ``last_scheduled`` is each rule's newest scheduled time on record, fired
        or missed — the engine reads it from ``rule_execution_log``. If the
        wall clock is not trustworthy yet (§4.9: not synced, and no RTC to
        fall back on), nothing is planned and no fire happens until it is —
        planning now against a clock that might read 1970, or whatever the
        OS was last told, would log nonsense "missed" entries and could plan
        a real fire hours away from when it should run.
        """
        if self.running:
            return
        if self._trusted():
            self._start_planning(self._now(), last_scheduled)
        else:
            self._plans.clear()
            self._last = {}
            self._pending_last_scheduled = last_scheduled
            self._held = True
            self._boot_hold = True
            log.warning(
                "system time is not verified and there is no trustworthy RTC; "
                "schedule fires are held until it is (§4.9)"
            )
        self._task = spawn(self._run(), name="rules-schedule")

    def _start_planning(
        self, now: datetime, last_scheduled: Mapping[int, datetime] | None
    ) -> None:
        """Plan every rule from ``now``, logging what passed as missed."""
        self._plans.clear()
        self._last = {
            rule_id: moment.astimezone(UTC)
            for rule_id, moment in (last_scheduled or {}).items()
            if self._believable(rule_id, moment.astimezone(UTC), now)
        }
        for rule in self._target.schedule_rules():
            plan = self._plan(rule, now)
            if plan.schedule is not None:
                self._log_downtime(rule, plan.schedule, now)
            self._plans[rule.id] = plan

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def replan(self) -> None:
        """The rules changed: plan new and edited ones from now, drop the rest, wake."""
        if not self.running:
            return
        if self._held:
            # Nothing is planned while held (§4.9); the transition back to
            # trusted reads the current rule set fresh, so this edit is not
            # lost, only deferred with everything else.
            return
        now = self._now()
        plans: dict[int, _Plan] = {}
        for rule in self._target.schedule_rules():
            current = self._plans.get(rule.id)
            if current is not None and current.cron == rule.cron:
                plans[rule.id] = current
            else:
                plans[rule.id] = self._plan(rule, now)
        self._plans = plans
        self._wake.set()

    def next_fire_at(self, rule_id: int) -> datetime | None:
        """The rule's planned instant, in Pacific/Auckland, or ``None``."""
        plan = self._plans.get(rule_id)
        if plan is None or plan.next is None:
            return None
        return plan.next.astimezone(AUCKLAND)

    # -- the loop ----------------------------------------------------------------

    async def _run(self) -> None:
        while True:
            if self._trusted():
                if self._held:
                    self._held = False
                    log.info(
                        "system time is verified; schedule fires resume",
                        extra={"now": self._now().astimezone(AUCKLAND).isoformat()},
                    )
                    if self._boot_hold:
                        self._start_planning(self._now(), self._pending_last_scheduled)
                        self._pending_last_scheduled = None
                        self._boot_hold = False
                    else:
                        # A mid-run loss and recovery, not a restart: no
                        # "controller was not running" report, just replanned
                        # from now, the same as a backward clock step.
                        self._replan_all(self._now())
                ticked = await contained("rules schedule tick", self._tick(self._now()))
            else:
                if not self._held:
                    self._held = True
                    self._plans.clear()
                    log.warning(
                        "system time is not verified and there is no trustworthy RTC; "
                        "schedule fires are held until it is (§4.9)"
                    )
                ticked = True  # nothing due can be evaluated; do not spin on it
            if self._stepped_back:
                self._stepped_back = False
                if not self._held:
                    # After the tick, so a time the step landed just past
                    # still fires. Not while held: nothing is planned then
                    # (the trust transition above replans everything once it
                    # resolves), so there is nothing to replan yet.
                    self._replan_all(self._now())
            self._wake.clear()
            now = self._now()
            upcoming = [p.next for p in self._plans.values() if p.next is not None]
            delay = MAX_SLEEP_S
            if upcoming and ticked:
                delay = min(max((min(upcoming) - now).total_seconds(), 0.0), MAX_SLEEP_S)
            mono_before = self._clock.monotonic()
            await self._clock.sleep(delay, self._wake)
            now_after = self._now()
            step = (now_after - now).total_seconds() - (self._clock.monotonic() - mono_before)
            if abs(step) > CLOCK_STEP_S:
                self._stepped(step, now_after)

    def _stepped(self, step: float, now: datetime) -> None:
        """The wall clock stepped. Forward, the next tick finds what it skipped
        over and logs it missed; back, every rule is planned again after it."""
        self.clock_steps += 1
        log.warning(
            "the wall clock stepped; schedules are planned again from now",
            extra={"step_s": round(step, 3), "now": now.astimezone(AUCKLAND).isoformat()},
        )
        if step < 0:
            self._stepped_back = True

    def _replan_all(self, now: datetime) -> None:
        self._last = {k: v for k, v in self._last.items() if self._believable(k, v, now)}
        for rule in self._target.schedule_rules():
            self._plans[rule.id] = self._plan(rule, now)

    async def _tick(self, now: datetime) -> bool:
        """Log what is overdue as missed, fire what is due, plan the next time.

        Returns ``True``; :func:`~proskenion.core.tasks.contained` turns a
        failure into ``None``, and the loop then waits a full stretch rather
        than spinning on a plan that is still overdue.
        """
        due: list[tuple[int, datetime]] = []
        for rule_id, plan in list(self._plans.items()):
            if plan.schedule is None or plan.next is None or plan.next > now:
                continue
            fire: datetime | None = None
            first_missed: datetime | None = None
            last_missed: datetime | None = None
            counted = 0
            moment: datetime | None = plan.next
            while moment is not None and moment <= now:
                if (now - moment).total_seconds() < LATE_LIMIT_S:
                    fire = moment
                else:
                    first_missed = first_missed or moment
                    last_missed = moment
                    counted += 1
                    if counted >= MAX_MISSED_COUNTED:
                        moment = plan.schedule.next_after(now)
                        break
                moment = plan.schedule.next_after(moment)
            plan.next = moment
            if first_missed is not None and last_missed is not None:
                self._last[rule_id] = last_missed
                self._target.record_missed(
                    rule_id,
                    scheduled_for=last_missed.astimezone(AUCKLAND),
                    first_missed=first_missed.astimezone(AUCKLAND),
                    count=counted,
                    reason=(
                        "the scheduler reached it too late: the controller was suspended "
                        "or busy, or its clock jumped forward"
                    ),
                )
            if fire is not None:
                self._last[rule_id] = fire
                due.append((rule_id, fire))
        for rule_id, scheduled in sorted(due, key=lambda d: self._order(d[0])):
            dispatched = self._now()
            try:
                await self._target.fire_scheduled(
                    rule_id,
                    scheduled_for=scheduled.astimezone(AUCKLAND),
                    dispatched_at=dispatched.astimezone(AUCKLAND),
                )
            except Exception:
                log.exception("a scheduled rule could not be fired", extra={"rule_id": rule_id})
        return True

    # -- planning ----------------------------------------------------------------

    def _plan(self, rule: Rule, now: datetime) -> _Plan:
        cron = rule.cron or ""
        try:
            schedule = CronSchedule.parse(cron)
        except ValueError as exc:
            log.warning(
                "a schedule rule's cron expression does not parse; it will not fire",
                extra={"rule_id": rule.id, "cron": cron, "reason": str(exc)},
            )
            return _Plan(cron, None, None)
        after = now
        last = self._last.get(rule.id)
        if last is not None and last > after:
            after = last
        return _Plan(cron, schedule, schedule.next_after(after))

    def _log_downtime(self, rule: Rule, schedule: CronSchedule, now: datetime) -> None:
        """Log, as one missed entry, the times that passed while the controller was down."""
        known = [m for m in (self._last.get(rule.id), _parse(rule.updated_at)) if m is not None]
        if not known:
            return
        since = max(known)
        if since >= now:
            return
        first: datetime | None = None
        last: datetime | None = None
        count = 0
        for moment in schedule.occurrences(since, now):
            first = first or moment
            last = moment
            count += 1
            if count >= MAX_MISSED_COUNTED:
                break
        if first is None or last is None:
            return
        self._last[rule.id] = last
        self._target.record_missed(
            rule.id,
            scheduled_for=last.astimezone(AUCKLAND),
            first_missed=first.astimezone(AUCKLAND),
            count=count,
            reason="the controller was not running at the time",
        )

    def _believable(self, rule_id: int, moment: datetime, now: datetime) -> bool:
        if (moment - now).total_seconds() <= FUTURE_LIMIT_S:
            return True
        log.warning(
            "a rule's last scheduled time is in the future; the clock was wrong then "
            "and it is disregarded",
            extra={"rule_id": rule_id, "last": moment.astimezone(AUCKLAND).isoformat()},
        )
        return False

    def _order(self, rule_id: int) -> int:
        for index, rule in enumerate(self._target.schedule_rules()):
            if rule.id == rule_id:
                return index
        return 0

    def now(self) -> datetime:
        """The scheduler's wall clock, in UTC."""
        return self._clock.wall().astimezone(UTC)

    def _now(self) -> datetime:
        return self.now()


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(UTC)


def next_fire_at(cron: str | None, now: datetime | None = None) -> datetime | None:
    """When a cron expression next fires after ``now``, in Pacific/Auckland — for display."""
    if not cron:
        return None
    try:
        schedule = CronSchedule.parse(cron)
    except ValueError:
        return None
    moment = schedule.next_after(now or datetime.now(tz=UTC))
    return None if moment is None else moment.astimezone(AUCKLAND)


__all__ = [
    "CLOCK_STEP_S",
    "FUTURE_LIMIT_S",
    "LATE_LIMIT_S",
    "MAX_MISSED_COUNTED",
    "MAX_SLEEP_S",
    "TRIGGERED_BY",
    "ScheduleClock",
    "ScheduleTarget",
    "Scheduler",
    "SystemClock",
    "next_fire_at",
]

