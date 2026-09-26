"""The soak timetable under a time-compression factor (§22.7, tests/soak/plan.py)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from proskenion.rules.cron import CronSchedule
from tests.soak.plan import (
    EXTERNAL_FLOOR_S,
    KNX_BURST_FLOOR_S,
    build_plan,
    scene_cron,
    scene_period,
    scheduled_times,
)


def test_the_real_run_is_section_22_7_exactly() -> None:
    plan = build_plan(1)
    assert plan.duration_s == 72 * 3600
    assert plan.scene_period_min == 15
    assert (plan.cron_a, plan.cron_b) == ("*/30 * * * *", "15-59/30 * * * *")
    assert plan.knx_burst_s == 30 and plan.knx_interval_s == 3600
    assert plan.external_s == 300 and plan.external_interval_s == 3600
    assert plan.mixer_interval_s == 24 * 3600
    # Start, every 12 hours, and the end: 0, 12, … 60, 72.
    assert [e.at_s for e in plan.events if e.kind == "sample"] == [
        h * 3600.0 for h in (0, 12, 24, 36, 48, 60, 72)
    ]
    assert plan.count("knx_burst") == 72
    assert plan.count("external_control") == 72
    assert plan.count("mixer_cycle") == 3


def test_compression_36_is_two_hours_with_every_load_at_least_twice() -> None:
    plan = build_plan(36)
    assert plan.duration_s == 2 * 3600
    assert plan.sample_interval_s == 1200
    assert plan.count("sample") == 7
    assert plan.count("mixer_cycle") >= 2
    assert plan.count("knx_burst") >= 2 and plan.count("external_control") >= 2
    # Cron cannot go below a minute, so the scenes are compressed only 15x.
    assert plan.scene_period_min == 1
    assert (plan.cron_a, plan.cron_b) == ("*/2 * * * *", "1-59/2 * * * *")


@pytest.mark.parametrize("factor", [1, 2, 6, 12, 24, 36, 100])
def test_compressed_durations_stay_within_their_floors_and_intervals(factor: float) -> None:
    plan = build_plan(factor)
    assert KNX_BURST_FLOOR_S <= plan.knx_burst_s <= 30
    assert EXTERNAL_FLOOR_S <= plan.external_s <= 300 or plan.external_s == plan.knx_interval_s / 2
    for event in plan.events:
        assert 0 <= event.at_s and event.at_s + event.duration_s <= plan.duration_s + 1e-6
    # One occurrence of a recurring load ends before the next begins.
    for kind, interval in (
        ("knx_burst", plan.knx_interval_s),
        ("mixer_cycle", plan.mixer_interval_s),
    ):
        starts = [e.at_s for e in plan.events if e.kind == kind]
        assert all(
            b - a == pytest.approx(interval) for a, b in zip(starts, starts[1:], strict=False)
        )


def test_events_are_in_time_order_with_a_sample_first_at_a_tie() -> None:
    plan = build_plan(36)
    times = [e.at_s for e in plan.events]
    assert times == sorted(times)
    assert plan.events[0].kind == "sample" and plan.events[-1].kind == "sample"


@pytest.mark.parametrize(("factor", "period"), [(1, 15), (2, 6), (3, 5), (5, 3), (8, 1), (15, 1)])
def test_the_scene_period_is_the_largest_cron_friendly_one(factor: float, period: int) -> None:
    assert scene_period(factor) == period


@pytest.mark.parametrize("period", [1, 2, 3, 5, 6, 10, 15])
def test_the_two_scene_rules_alternate_and_cover_every_period(period: int) -> None:
    """The application's own cron parser agrees with the harness's expectation."""
    a = CronSchedule.parse(scene_cron(period, odd=False))
    b = CronSchedule.parse(scene_cron(period, odd=True))
    minutes_a = {m for m in range(60) if m in a.minutes}
    minutes_b = {m for m in range(60) if m in b.minutes}
    assert not minutes_a & minutes_b
    assert minutes_a | minutes_b == {m for m in range(60) if m % period == 0}


def test_an_uncronnable_period_is_refused() -> None:
    with pytest.raises(ValueError):
        scene_cron(7, odd=False)


def test_scheduled_times_are_the_period_boundaries_after_the_start() -> None:
    start = datetime(2026, 9, 25, 10, 0, 30, tzinfo=UTC)
    end = datetime(2026, 9, 25, 10, 31, 0, tzinfo=UTC)
    times = scheduled_times(15, start, end)
    assert [t.minute for t in times] == [15, 30]
    assert len(scheduled_times(1, start, end)) == 31


def test_a_compression_below_one_is_refused() -> None:
    with pytest.raises(ValueError):
        build_plan(0.5)
