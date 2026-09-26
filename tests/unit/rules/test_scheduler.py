"""The scheduler against an injected clock: planning, re-planning, misses, restarts, DST.

The target here is a recorder, so these tests see exactly what the scheduler
asks the rules engine to do. ``test_schedule_engine.py`` covers the engine's
side — the same firing path as any other trigger, and the execution log.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime

import pytest

from proskenion.db.crud.base import AUCKLAND
from proskenion.db.crud.rules import Rule
from proskenion.rules.scheduler import LATE_LIMIT_S, MAX_SLEEP_S, Scheduler
from tests.unit.rules.conftest import ScheduleTestClock

TEMPLATE = Rule(
    id=1,
    name="Nightly",
    enabled=True,
    sort_order=0,
    notes=None,
    trigger_type="schedule",
    knx_address_id=None,
    match_type="any",
    match_value=None,
    match_value_max=None,
    debounce_ms=None,
    cron="0 19 * * *",
    trigger_device_id=None,
    trigger_state=None,
    trigger_for_ms=None,
    guard_type=None,
    guard_value=None,
    action_type="run_scene",
    scene_id=1,
    lighting_group_id=None,
    on_level=None,
    off_level=None,
    fade_ms=None,
    message=None,
    created_at="2026-09-01T09:00:00.000000+12:00",
    updated_at="",
)


def rule(rule_id: int = 1, cron: str = "0 19 * * *", **changes: object) -> Rule:
    return dataclasses.replace(TEMPLATE, id=rule_id, cron=cron, **changes)  # type: ignore[arg-type]


@dataclass
class Missed:
    rule_id: int
    scheduled_for: str
    first_missed: str
    count: int


@dataclass
class Recorder:
    """The rules engine, as the scheduler sees it."""

    rules: list[Rule] = field(default_factory=list)
    fired: list[tuple[int, str, float]] = field(default_factory=list)
    """``(rule id, scheduled_for, seconds late)``."""
    missed: list[Missed] = field(default_factory=list)

    def schedule_rules(self) -> Sequence[Rule]:
        return [r for r in self.rules if r.enabled and r.trigger_type == "schedule"]

    async def fire_scheduled(
        self, rule_id: int, *, scheduled_for: datetime, dispatched_at: datetime
    ) -> None:
        late = (dispatched_at - scheduled_for).total_seconds()
        self.fired.append((rule_id, scheduled_for.isoformat(), late))

    def record_missed(
        self,
        rule_id: int,
        *,
        scheduled_for: datetime,
        first_missed: datetime,
        count: int,
        reason: str,
    ) -> None:
        assert reason
        self.missed.append(
            Missed(rule_id, scheduled_for.isoformat(), first_missed.isoformat(), count)
        )

    def times(self, rule_id: int | None = None) -> list[str]:
        return [t for r, t, _ in self.fired if rule_id is None or r == rule_id]


@dataclass
class Harness:
    clock: ScheduleTestClock
    target: Recorder
    scheduler: Scheduler

    async def start(self, last: dict[int, str] | None = None) -> None:
        known = {k: datetime.fromisoformat(v) for k, v in (last or {}).items()}
        await self.scheduler.start(known)
        await self.clock.settled()

    async def restart(self, at: str, last: dict[int, str] | None = None) -> None:
        """Stop, set the wall clock (the controller was off), start a fresh scheduler."""
        await self.scheduler.stop()
        self.clock.now = datetime.fromisoformat(at)
        self.clock.asleep = False
        self.scheduler = Scheduler(self.target, self.clock)
        await self.start(last)

    async def edit(self, *rules: Rule) -> None:
        naps = len(self.clock.naps)
        self.target.rules = list(rules)
        self.scheduler.replan()
        await self.clock.settled(naps)

    def next(self, rule_id: int = 1) -> str | None:
        moment = self.scheduler.next_fire_at(rule_id)
        return None if moment is None else moment.isoformat()


def harness_at(start: str, *rules: Rule) -> Harness:
    """A scheduler whose clock reads ``start``. A rule not given an ``updated_at``
    was last edited then, so nothing before it counts as missed."""
    clock = ScheduleTestClock(datetime.fromisoformat(start))
    target = Recorder(
        [r if r.updated_at else dataclasses.replace(r, updated_at=start) for r in rules]
    )
    return Harness(clock, target, Scheduler(target, clock))


@pytest.fixture
async def h() -> AsyncIterator[Harness]:
    running = harness_at("2026-09-24T18:59:00+12:00", rule())
    try:
        yield running
    finally:
        await running.scheduler.stop()


# -- firing on time ---------------------------------------------------------------------


async def test_it_fires_at_the_planned_time_and_not_before(h: Harness) -> None:
    await h.start()
    assert h.next() == "2026-09-24T19:00:00+12:00"
    assert h.clock.naps[-1] == MAX_SLEEP_S  # a minute away: sleeps in short stretches
    await h.clock.run_until("2026-09-24T18:59:59.990+12:00")
    assert h.target.fired == []
    await h.clock.run_until("2026-09-24T19:00:00+12:00")
    assert h.target.fired == [(1, "2026-09-24T19:00:00+12:00", 0.0)]
    assert h.next() == "2026-09-25T19:00:00+12:00"


async def test_the_last_sleep_ends_exactly_on_the_planned_time(h: Harness) -> None:
    await h.start()
    await h.clock.advance(55.5)
    assert h.clock.naps[-1] == pytest.approx(4.5)


async def test_rules_due_together_fire_in_rule_order() -> None:
    h = harness_at(
        "2026-09-24T18:59:00+12:00", rule(3, sort_order=0), rule(1, sort_order=1), rule(2)
    )
    await h.start()
    await h.clock.run_until("2026-09-24T19:00:00+12:00")
    assert [r for r, _, _ in h.target.fired] == [3, 1, 2]
    await h.scheduler.stop()


async def test_a_late_wake_within_the_limit_still_fires_with_its_lateness(h: Harness) -> None:
    """The loop was held up for 20 s: late, not missed."""
    await h.start()
    await h.clock.advance(80.0)
    assert h.target.fired == [(1, "2026-09-24T19:00:00+12:00", 20.0)]
    assert h.target.missed == []


async def test_a_time_passed_asleep_beyond_the_limit_is_missed_and_not_run(h: Harness) -> None:
    await h.start()
    await h.clock.advance(60.0 + LATE_LIMIT_S + 1)
    assert h.target.fired == []
    assert h.target.missed == [
        Missed(1, "2026-09-24T19:00:00+12:00", "2026-09-24T19:00:00+12:00", 1)
    ]
    assert h.next() == "2026-09-25T19:00:00+12:00"


async def test_a_disabled_rule_and_a_bad_expression_do_not_fire(h: Harness) -> None:
    h.target.rules = [rule(1, enabled=False), rule(2, cron="not a cron")]
    await h.start()
    await h.clock.run_until("2026-09-24T19:01:00+12:00")
    assert h.target.fired == [] and h.target.missed == []
    assert h.next(1) is None and h.next(2) is None


# -- re-planning when rules change ---------------------------------------------------


async def test_a_rule_edited_while_the_scheduler_sleeps_fires_at_its_new_time(
    h: Harness,
) -> None:
    await h.start()
    await h.edit(rule(cron="30 18 * * *"))  # 18:30 today has passed
    assert h.next() == "2026-09-25T18:30:00+12:00"
    await h.edit(rule(cron="0,59 19 * * *"))
    assert h.next() == "2026-09-24T19:00:00+12:00"
    await h.edit(rule(cron="59 19 * * *"))
    await h.clock.run_until("2026-09-24T19:30:00+12:00")
    assert h.target.fired == []  # 19:00 was edited away
    await h.clock.run_until("2026-09-24T19:59:00+12:00")
    assert h.target.times() == ["2026-09-24T19:59:00+12:00"]


async def test_an_edit_wakes_the_loop_from_a_long_sleep(h: Harness) -> None:
    h.target.rules = [rule(cron="0 23 * * *")]
    await h.start()
    naps = len(h.clock.naps)
    h.target.rules = [rule(cron="* * * * *")]
    h.scheduler.replan()
    await h.clock.settled(naps)
    assert len(h.clock.naps) == naps + 1  # woken, without the clock moving
    assert h.clock.naps[-1] == MAX_SLEEP_S  # 19:00 is now the earliest, a minute away
    assert h.next() == "2026-09-24T19:00:00+12:00"


async def test_created_disabled_and_deleted_rules_are_planned_and_dropped(h: Harness) -> None:
    await h.start()
    await h.edit(rule(), rule(2, cron="0 19 * * *"))
    assert h.next(2) == "2026-09-24T19:00:00+12:00"
    await h.edit(rule(), rule(2, enabled=False))
    assert h.next(2) is None
    await h.edit(rule(2))
    assert h.next(1) is None
    await h.clock.run_until("2026-09-24T19:00:30+12:00")
    assert [r for r, _, _ in h.target.fired] == [2]


async def test_an_unchanged_rule_keeps_its_plan_across_a_reload(h: Harness) -> None:
    """A reload in the moment after a time is due must not re-plan it into tomorrow."""
    await h.start()
    await h.clock.run_until("2026-09-24T18:59:59+12:00")
    # 19:00 has come, and a reload lands before the loop's own wake does.
    h.clock.now = datetime.fromisoformat("2026-09-24T19:00:00.500+12:00")
    h.clock.mono += 1.5
    await h.edit(rule(name="Renamed"))
    assert [(t, late) for _, t, late in h.target.fired] == [("2026-09-24T19:00:00+12:00", 0.5)]


# -- the wall clock stepping --------------------------------------------------------


async def test_a_forward_step_over_a_time_misses_it(h: Harness) -> None:
    await h.start()
    await h.clock.advance(5.0, step=120.0)  # NTP: 18:59:05 becomes 19:01:05
    assert h.scheduler.clock_steps == 1
    assert h.target.fired == []
    assert [m.scheduled_for for m in h.target.missed] == ["2026-09-24T19:00:00+12:00"]
    assert h.next() == "2026-09-25T19:00:00+12:00"


async def test_a_small_forward_step_onto_a_time_fires_it_late(h: Harness) -> None:
    await h.start()
    await h.clock.advance(5.0, step=58.0)  # 18:59:05 becomes 19:00:03
    assert h.target.fired == [(1, "2026-09-24T19:00:00+12:00", 3.0)]


async def test_a_backward_step_never_fires_the_same_time_twice() -> None:
    h = harness_at("2026-09-24T18:59:00+12:00", rule(cron="0 * * * *"))
    await h.start()
    await h.clock.run_until("2026-09-24T19:00:00+12:00")
    await h.clock.advance(5.0, step=-1800.0)  # back to 18:30:05
    assert h.scheduler.clock_steps == 1
    assert h.next() == "2026-09-24T20:00:00+12:00"
    await h.clock.run_until("2026-09-24T20:00:00+12:00")
    assert h.target.times() == ["2026-09-24T19:00:00+12:00", "2026-09-24T20:00:00+12:00"]
    await h.scheduler.stop()


async def test_a_backward_step_that_lands_just_past_a_time_still_fires_it(h: Harness) -> None:
    """Asleep from 18:59:55 until 19:00; the clock steps back 3 s during a 10 s stall."""
    await h.start()
    await h.clock.run_until("2026-09-24T18:59:55+12:00")
    await h.clock.advance(10.0, step=-3.0)  # wakes reading 19:00:02
    assert h.scheduler.clock_steps == 1
    assert h.target.fired == [(1, "2026-09-24T19:00:00+12:00", 2.0)]
    assert h.next() == "2026-09-25T19:00:00+12:00"


async def test_a_clock_that_booted_in_the_future_is_replanned_when_corrected() -> None:
    h = harness_at("2027-01-01T10:59:00+13:00", rule(cron="0 * * * *"))
    await h.start()
    await h.clock.run_until("2027-01-01T11:00:00+13:00")  # fired on the wrong clock
    await h.clock.advance(1.0, step=-(98 * 24 * 3600.0))  # NTP corrects to September
    local = h.clock.local()
    assert (local.year, local.month) == (2026, 9)
    assert h.next() == "2026-09-25T11:00:00+12:00"  # 10:00:01 NZST now
    await h.scheduler.stop()


async def test_a_step_is_measured_against_the_monotonic_clock(h: Harness) -> None:
    await h.start()
    await h.clock.advance(1.0, step=1.5)  # within tolerance: slewing, not a step
    await h.clock.advance(1.0, step=-1.5)
    assert h.scheduler.clock_steps == 0


# -- restarts and downtime (D1) --------------------------------------------------------


async def test_a_restart_inside_the_fired_minute_does_not_fire_it_again(h: Harness) -> None:
    await h.start()
    await h.clock.run_until("2026-09-24T19:00:00+12:00")
    await h.restart("2026-09-24T19:00:20+12:00", last={1: "2026-09-24T19:00:00+12:00"})
    await h.clock.run_until("2026-09-24T19:05:00+12:00")
    assert h.target.times() == ["2026-09-24T19:00:00+12:00"]
    assert h.target.missed == []
    assert h.next() == "2026-09-25T19:00:00+12:00"


async def test_a_restart_with_the_clock_behind_the_fired_minute_does_not_fire_it_again(
    h: Harness,
) -> None:
    """The memory of the last firing, not "now", is what stops the double fire here."""
    await h.start()
    await h.clock.run_until("2026-09-24T19:00:00+12:00")
    await h.restart("2026-09-24T18:59:50+12:00", last={1: "2026-09-24T19:00:00+12:00"})
    await h.clock.run_until("2026-09-24T19:01:00+12:00")
    assert h.target.times() == ["2026-09-24T19:00:00+12:00"]


async def test_times_passed_while_down_are_logged_as_missed_and_never_run(h: Harness) -> None:
    await h.restart("2026-09-26T20:00:00+12:00", last={1: "2026-09-23T19:00:00+12:00"})
    assert h.target.fired == []
    assert h.target.missed == [
        Missed(1, "2026-09-26T19:00:00+12:00", "2026-09-24T19:00:00+12:00", 3)
    ]
    assert h.next() == "2026-09-27T19:00:00+13:00"


async def test_a_start_just_after_a_time_misses_it_rather_than_running_it(h: Harness) -> None:
    """Compute from now: even inside the late limit, a time before start is not run."""
    await h.restart("2026-09-24T19:00:05+12:00", last={1: "2026-09-23T19:00:00+12:00"})
    await h.clock.run_until("2026-09-24T19:01:00+12:00")
    assert h.target.fired == []
    assert [m.scheduled_for for m in h.target.missed] == ["2026-09-24T19:00:00+12:00"]


async def test_with_no_firing_on_record_downtime_counts_from_the_last_edit() -> None:
    h = harness_at(
        "2026-09-24T19:30:00+12:00",
        rule(updated_at="2026-09-24T12:00:00.000000+12:00"),
        rule(2, updated_at="2026-09-24T19:10:00.000000+12:00"),
    )
    await h.start()
    assert [(m.rule_id, m.scheduled_for) for m in h.target.missed] == [
        (1, "2026-09-24T19:00:00+12:00")
    ]
    await h.scheduler.stop()


async def test_a_remembered_firing_far_in_the_future_is_disregarded() -> None:
    h = harness_at("2026-09-24T18:59:00+12:00", rule())
    await h.start({1: "2031-01-01T19:00:00+13:00"})
    assert h.next() == "2026-09-24T19:00:00+12:00"
    await h.clock.run_until("2026-09-24T19:00:00+12:00")
    assert h.target.times() == ["2026-09-24T19:00:00+12:00"]
    await h.scheduler.stop()


# -- daylight saving (D2), through the loop -------------------------------------------------


async def test_a_gap_time_fires_once_at_three_through_the_loop() -> None:
    h = harness_at("2026-09-27T01:50:00+12:00", rule(cron="0,30 2,3 * * *"))
    await h.start()
    await h.clock.run_until("2026-09-27T04:00:00+13:00")
    assert h.target.times() == ["2026-09-27T03:00:00+13:00", "2026-09-27T03:30:00+13:00"]
    assert h.target.missed == []
    await h.scheduler.stop()


async def test_a_repeated_time_fires_once_on_the_first_pass_through_the_loop() -> None:
    h = harness_at("2027-04-04T02:00:00+13:00", rule(cron="30 2 * * *"))
    await h.start()
    await h.clock.run_until("2027-04-04T03:30:00+12:00")  # through both passes
    assert h.target.times() == ["2027-04-04T02:30:00+13:00"]
    assert h.next() == "2027-04-05T02:30:00+12:00"
    await h.scheduler.stop()


async def test_a_restart_during_the_second_pass_does_not_fire_the_repeat() -> None:
    h = harness_at("2027-04-04T02:20:00+13:00", rule(cron="30 2 * * *"))
    await h.start()
    await h.clock.run_until("2027-04-04T02:31:00+13:00")
    await h.restart("2027-04-04T02:20:00+12:00", last={1: "2027-04-04T02:30:00+13:00"})
    await h.clock.run_until("2027-04-04T02:40:00+12:00")
    assert h.target.times() == ["2027-04-04T02:30:00+13:00"]
    assert h.target.missed == []
    await h.scheduler.stop()


async def test_next_fire_at_is_the_plan_in_auckland(h: Harness) -> None:
    assert h.scheduler.next_fire_at(1) is None  # nothing is planned before start
    await h.start()
    moment = h.scheduler.next_fire_at(1)
    assert moment is not None and moment.tzinfo is AUCKLAND


# -- trusting the wall clock (§4.9) ------------------------------------------------------


async def test_by_default_the_clock_is_always_trusted(h: Harness) -> None:
    """No ``trusted`` callable (every test above): unchanged from before this
    feature — nothing is held."""
    await h.start()
    assert h.scheduler.held is False


async def test_nothing_is_planned_or_fired_while_the_clock_is_not_trusted() -> None:
    trusted = [False]
    clock = ScheduleTestClock(datetime.fromisoformat("2026-09-24T18:59:00+12:00"))
    target = Recorder([rule(updated_at="2026-09-24T18:59:00+12:00")])
    scheduler = Scheduler(target, clock, trusted=lambda: trusted[0])
    try:
        await scheduler.start()
        await clock.settled()
        assert scheduler.held is True
        assert scheduler.next_fire_at(1) is None

        # Past the rule's own time, still on an untrusted clock: nothing.
        await clock.advance(120)
        assert target.fired == []
        assert target.missed == []
        assert scheduler.held is True
    finally:
        await scheduler.stop()


async def test_a_rule_due_while_held_is_logged_missed_once_trust_returns() -> None:
    """§4.9: "hold ... (log it) until sync" — the hold itself is logged once
    (not asserted here, by the calling convention the rest of this module
    uses), and a rule whose time passed during it is logged missed the same
    way a rule that passed while the controller was off would be — it is
    not silently dropped."""
    trusted = [False]
    clock = ScheduleTestClock(datetime.fromisoformat("2026-09-24T18:59:00+12:00"))
    target = Recorder([rule(updated_at="2026-09-24T18:59:00+12:00")])
    scheduler = Scheduler(target, clock, trusted=lambda: trusted[0])
    try:
        await scheduler.start()
        await clock.settled()
        await clock.advance(120)  # 19:01: the rule's 19:00 has passed, still held

        trusted[0] = True
        await clock.advance(MAX_SLEEP_S)  # wakes the loop to notice

        assert scheduler.held is False
        assert target.fired == []
        assert [m.rule_id for m in target.missed] == [1]
        moment = scheduler.next_fire_at(1)
        assert moment is not None and moment.isoformat() == "2026-09-25T19:00:00+12:00"
    finally:
        await scheduler.stop()


async def test_a_rule_fires_normally_once_trust_is_established_before_its_time() -> None:
    trusted = [False]
    clock = ScheduleTestClock(datetime.fromisoformat("2026-09-24T18:59:00+12:00"))
    target = Recorder([rule(updated_at="2026-09-24T18:59:00+12:00")])
    scheduler = Scheduler(target, clock, trusted=lambda: trusted[0])
    try:
        await scheduler.start()
        await clock.settled()

        trusted[0] = True
        await clock.advance(MAX_SLEEP_S)  # still well before 19:00
        assert scheduler.held is False
        moment = scheduler.next_fire_at(1)
        assert moment is not None and moment.isoformat() == "2026-09-24T19:00:00+12:00"

        await clock.run_until("2026-09-24T19:00:00+12:00")
        assert target.fired == [(1, "2026-09-24T19:00:00+12:00", 0.0)]
    finally:
        await scheduler.stop()


async def test_losing_trust_mid_run_holds_without_a_downtime_report() -> None:
    """Unlike a hold that starts at boot, a clock that is trusted, then is
    not, then is again mid-run is not a restart — it gets a plain replan,
    the same as a backward clock step, and no "controller was not running"
    report for the gap."""
    trusted = [True]
    clock = ScheduleTestClock(datetime.fromisoformat("2026-09-24T18:59:00+12:00"))
    target = Recorder([rule(updated_at="2026-09-24T18:59:00+12:00")])
    scheduler = Scheduler(target, clock, trusted=lambda: trusted[0])
    try:
        await scheduler.start()
        await clock.settled()
        assert scheduler.held is False
        moment = scheduler.next_fire_at(1)
        assert moment is not None and moment.isoformat() == "2026-09-24T19:00:00+12:00"

        trusted[0] = False
        await clock.advance(MAX_SLEEP_S)
        assert scheduler.held is True

        trusted[0] = True
        await clock.advance(MAX_SLEEP_S)
        assert scheduler.held is False
        assert target.missed == []
        assert scheduler.next_fire_at(1) is not None
    finally:
        await scheduler.stop()


async def test_an_edit_while_held_is_not_lost() -> None:
    """A rule created or changed while held is picked up once trust
    returns — replan() is a safe no-op while held, not a way to lose it."""
    trusted = [False]
    clock = ScheduleTestClock(datetime.fromisoformat("2026-09-24T18:59:00+12:00"))
    target = Recorder([])
    scheduler = Scheduler(target, clock, trusted=lambda: trusted[0])
    try:
        await scheduler.start()
        await clock.settled()
        assert scheduler.held is True

        target.rules = [rule(updated_at="2026-09-24T18:59:00+12:00")]
        scheduler.replan()  # a no-op while held; must not raise or plan early
        assert scheduler.next_fire_at(1) is None

        trusted[0] = True
        await clock.advance(MAX_SLEEP_S)  # ~18:59:10: still before 19:00 today
        assert scheduler.held is False
        moment = scheduler.next_fire_at(1)
        assert moment is not None and moment.isoformat() == "2026-09-24T19:00:00+12:00"
    finally:
        await scheduler.stop()
