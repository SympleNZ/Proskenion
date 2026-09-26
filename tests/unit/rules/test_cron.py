"""Cron semantics: every field, ranges, steps, lists, names, the OR rule, daylight saving.

Daylight saving in Pacific/Auckland, the dates these tests use:

* 2026-09-27 (Sunday): 02:00 NZST jumps to 03:00 NZDT — 02:00–02:59 does not exist
* 2027-04-04 (Sunday): 03:00 NZDT falls back to 02:00 NZST — 02:00–02:59 happens twice
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from proskenion.db.crud.base import AUCKLAND
from proskenion.rules.cron import CronSchedule, resolve_local
from proskenion.rules.model import validate_cron


def at(text: str) -> datetime:
    """An ISO 8601 instant with offset, as the log writes it."""
    moment = datetime.fromisoformat(text)
    assert moment.tzinfo is not None
    return moment


def nxt(expression: str, after: str) -> str | None:
    """The next firing after ``after``, as Pacific/Auckland ISO 8601."""
    moment = CronSchedule.parse(expression).next_after(at(after))
    return None if moment is None else moment.astimezone(AUCKLAND).isoformat()


def series(expression: str, after: str, n: int) -> list[str]:
    schedule = CronSchedule.parse(expression)
    out: list[str] = []
    moment: datetime | None = at(after)
    for _ in range(n):
        assert moment is not None
        moment = schedule.next_after(moment)
        assert moment is not None
        out.append(moment.astimezone(AUCKLAND).isoformat())
    return out


# -- the grammar, field by field ----------------------------------------------------------


def test_every_field_expands_values_ranges_steps_and_lists() -> None:
    schedule = CronSchedule.parse("5,10-12,*/20 0-12/4 1,15 JAN-MAR,dec MON-FRI")
    assert schedule.minutes == (0, 5, 10, 11, 12, 20, 40)
    assert schedule.hours == (0, 4, 8, 12)
    assert schedule.days == {1, 15}
    assert schedule.months == {1, 2, 3, 12}
    assert schedule.weekdays == {1, 2, 3, 4, 5}


def test_a_value_with_a_step_runs_to_the_end_of_the_range() -> None:
    assert CronSchedule.parse("5/20 * * * *").minutes == (5, 25, 45)
    assert CronSchedule.parse("* 20/2 * * *").hours == (20, 22)
    assert CronSchedule.parse("* * * 10/1 *").months == {10, 11, 12}


def test_a_star_covers_each_fields_whole_range() -> None:
    schedule = CronSchedule.parse("* * * * *")
    assert schedule.minutes == tuple(range(60))
    assert schedule.hours == tuple(range(24))
    assert schedule.days == set(range(1, 32))
    assert schedule.months == set(range(1, 13))
    assert schedule.weekdays == set(range(7))


def test_sunday_is_both_0_and_7_and_names_ignore_case() -> None:
    assert CronSchedule.parse("0 0 * * 7").weekdays == {0}
    assert CronSchedule.parse("0 0 * * sun").weekdays == {0}
    assert CronSchedule.parse("0 0 * * 5-7").weekdays == {5, 6, 0}
    assert CronSchedule.parse("0 0 * * */7").weekdays == {0}
    assert CronSchedule.parse("0 0 * jun *").months == {6}


@pytest.mark.parametrize(
    "expression",
    ["23:00 daily", "* * * *", "60 * * * *", "* 24 * * *", "* * 0 * *", "* * * 13 *",
     "* * * * 8", "5-1 * * * *", "*/0 * * * *", "1,,2 * * * *", "* * * FOO *"],
)
def test_what_validate_cron_refuses_does_not_parse(expression: str) -> None:
    assert validate_cron(expression) is not None
    with pytest.raises(ValueError):
        CronSchedule.parse(expression)


# -- each field decides the next time ----------------------------------------------------------


def test_minute_and_hour() -> None:
    assert nxt("30 14 * * *", "2026-09-24T10:00:00+12:00") == "2026-09-24T14:30:00+12:00"
    assert nxt("30 14 * * *", "2026-09-24T14:30:00+12:00") == "2026-09-25T14:30:00+12:00"
    assert nxt("*/15 * * * *", "2026-09-24T10:07:59+12:00") == "2026-09-24T10:15:00+12:00"
    assert series("0 8-9 * * *", "2026-09-24T07:00:00+12:00", 3) == [
        "2026-09-24T08:00:00+12:00",
        "2026-09-24T09:00:00+12:00",
        "2026-09-25T08:00:00+12:00",
    ]


def test_strictly_after_even_within_the_same_minute() -> None:
    assert nxt("* * * * *", "2026-09-24T10:00:00+12:00") == "2026-09-24T10:01:00+12:00"
    assert nxt("* * * * *", "2026-09-24T10:00:59.999+12:00") == "2026-09-24T10:01:00+12:00"


def test_day_of_month_and_month() -> None:
    assert nxt("0 9 15 * *", "2026-09-24T10:00:00+12:00") == "2026-10-15T09:00:00+13:00"
    assert nxt("0 0 1 JAN *", "2026-09-24T10:00:00+12:00") == "2027-01-01T00:00:00+13:00"
    assert nxt("0 0 31 * *", "2026-09-24T10:00:00+12:00") == "2026-10-31T00:00:00+13:00"


def test_day_of_week() -> None:
    # 2026-09-24 is a Thursday.
    assert nxt("0 7 * * MON-FRI", "2026-09-25T08:00:00+12:00") == "2026-09-28T07:00:00+13:00"
    assert nxt("0 7 * * 0", "2026-09-24T08:00:00+12:00") == "2026-09-27T07:00:00+13:00"


def test_both_day_fields_restricted_means_either() -> None:
    """``0 9 1 * MON``: the 1st of the month *and* every Monday."""
    schedule = CronSchedule.parse("0 9 1 * MON")
    assert schedule.matches_day(date(2026, 9, 1))  # the 1st, a Tuesday
    assert schedule.matches_day(date(2026, 9, 7))  # a Monday, not the 1st
    assert not schedule.matches_day(date(2026, 9, 8))
    assert series("0 9 1 * MON", "2026-09-24T10:00:00+12:00", 3) == [
        "2026-09-28T09:00:00+13:00",  # Monday
        "2026-10-01T09:00:00+13:00",  # the 1st, a Thursday
        "2026-10-05T09:00:00+13:00",  # Monday
    ]


def test_one_day_field_unrestricted_means_both_must_match() -> None:
    only_the_first = CronSchedule.parse("0 9 1 * *")
    assert only_the_first.matches_day(date(2026, 9, 1))
    assert not only_the_first.matches_day(date(2026, 9, 7))
    only_mondays = CronSchedule.parse("0 9 * * MON")
    assert only_mondays.matches_day(date(2026, 9, 7))
    assert not only_mondays.matches_day(date(2026, 9, 1))


def test_a_field_starting_with_a_star_is_unrestricted_as_in_vixie_cron() -> None:
    """``*/2`` in day of month does not trigger the OR rule: odd-dated Mondays only."""
    schedule = CronSchedule.parse("0 9 */2 * MON")
    assert not schedule.days_restricted and schedule.weekdays_restricted
    assert schedule.matches_day(date(2026, 9, 7))  # Monday the 7th
    assert not schedule.matches_day(date(2026, 9, 14))  # Monday the 14th: even
    assert not schedule.matches_day(date(2026, 9, 9))  # the 9th, a Wednesday


def test_a_date_that_never_occurs_has_no_next_time() -> None:
    assert nxt("0 0 30 2 *", "2026-09-24T10:00:00+12:00") is None
    assert nxt("0 0 31 4 *", "2026-09-24T10:00:00+12:00") is None


def test_29_february_is_found_years_ahead() -> None:
    assert nxt("0 12 29 2 *", "2026-09-24T10:00:00+12:00") == "2028-02-29T12:00:00+13:00"


def test_instants_are_utc_and_matching_is_in_auckland() -> None:
    moment = CronSchedule.parse("0 9 * * *").next_after(at("2026-09-24T00:00:00+00:00"))
    assert moment is not None and moment.tzinfo is UTC
    assert moment == at("2026-09-24T21:00:00+00:00")  # 09:00 on the 25th, NZST


# -- daylight saving (D2) --------------------------------------------------------------


def test_a_time_in_the_september_gap_fires_once_at_three() -> None:
    assert series("30 2 * * *", "2026-09-26T12:00:00+12:00", 2) == [
        "2026-09-27T03:00:00+13:00",
        "2026-09-28T02:30:00+13:00",
    ]


def test_every_gap_minute_collapses_into_one_three_oclock_firing() -> None:
    """02:00, 02:15, 02:30, 02:45 and 03:00 itself all match; 03:00 fires once."""
    schedule = CronSchedule.parse("*/15 2-3 * * *")
    window = (at("2026-09-27T00:00:00+12:00"), at("2026-09-27T04:00:00+13:00"))
    got = [m.astimezone(AUCKLAND).isoformat() for m in schedule.occurrences(*window)]
    assert got == [
        "2026-09-27T03:00:00+13:00",
        "2026-09-27T03:15:00+13:00",
        "2026-09-27T03:30:00+13:00",
        "2026-09-27T03:45:00+13:00",
    ]


def test_every_minute_across_the_gap() -> None:
    assert series("* * * * *", "2026-09-27T01:58:00+12:00", 3) == [
        "2026-09-27T01:59:00+12:00",
        "2026-09-27T03:00:00+13:00",
        "2026-09-27T03:01:00+13:00",
    ]


def test_a_time_in_the_april_repeat_fires_once_on_the_first_pass() -> None:
    assert series("30 2 * * *", "2027-04-03T12:00:00+13:00", 2) == [
        "2027-04-04T02:30:00+13:00",
        "2027-04-05T02:30:00+12:00",
    ]


def test_from_inside_the_second_pass_the_repeated_time_is_already_past() -> None:
    assert nxt("30 2 * * *", "2027-04-04T02:10:00+12:00") == "2027-04-05T02:30:00+12:00"
    assert nxt("45 2 * * *", "2027-04-04T02:50:00+13:00") == "2027-04-05T02:45:00+12:00"


def test_every_minute_is_quiet_during_the_repeated_hour() -> None:
    first, second = series("* * * * *", "2027-04-04T02:58:30+13:00", 2)
    assert first == "2027-04-04T02:59:00+13:00"
    assert second == "2027-04-04T03:00:00+12:00"  # an hour of real time later
    assert at(second) - at(first) == timedelta(hours=1, minutes=1)


def test_an_hourly_rule_across_the_repeat() -> None:
    assert series("0 * * * *", "2027-04-04T01:30:00+13:00", 3) == [
        "2027-04-04T02:00:00+13:00",
        "2027-04-04T03:00:00+12:00",
        "2027-04-04T04:00:00+12:00",
    ]


def test_resolve_local_by_the_same_rules() -> None:
    gap = resolve_local(datetime(2026, 9, 27, 2, 30))
    assert gap.astimezone(AUCKLAND).isoformat() == "2026-09-27T03:00:00+13:00"
    repeat = resolve_local(datetime(2027, 4, 4, 2, 30))
    assert repeat.astimezone(AUCKLAND).isoformat() == "2027-04-04T02:30:00+13:00"
    plain = resolve_local(datetime(2026, 9, 24, 19, 0))
    assert plain.astimezone(AUCKLAND).isoformat() == "2026-09-24T19:00:00+12:00"
