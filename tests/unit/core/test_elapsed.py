"""Real elapsed time on aware datetimes, across Pacific/Auckland's changes (§4.9)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from proskenion.core.elapsed import elapsed_after, elapsed_before, seconds_between

AUCKLAND = ZoneInfo("Pacific/Auckland")

MOMENTS = pytest.mark.parametrize(
    "moment",
    [
        datetime(2026, 9, 27, 1, 59, tzinfo=AUCKLAND),
        datetime(2027, 4, 4, 2, 59, tzinfo=AUCKLAND),
        datetime(2027, 4, 4, 2, 30, fold=1, tzinfo=AUCKLAND),
        datetime(2026, 9, 26, 20, 0, tzinfo=AUCKLAND),
    ],
    ids=["spring-forward", "fall-back-first-pass", "fall-back-second-pass", "twelve-hours-out"],
)


@MOMENTS
@pytest.mark.parametrize("delta", [timedelta(minutes=3), timedelta(hours=12)])
def test_after_and_before_are_real_time_and_read_as_the_wall_clock(
    moment: datetime, delta: timedelta
) -> None:
    later = elapsed_after(moment, delta)
    earlier = elapsed_before(moment, delta)
    assert later.timestamp() - moment.timestamp() == delta.total_seconds()
    assert moment.timestamp() - earlier.timestamp() == delta.total_seconds()
    for result in (later, earlier):
        assert result.tzinfo is AUCKLAND
        assert result.utcoffset() == result.astimezone(UTC).astimezone(AUCKLAND).utcoffset()


@MOMENTS
def test_seconds_between_compares_instants_not_wall_clocks(moment: datetime) -> None:
    later = datetime.fromtimestamp(moment.timestamp() + 1800, tz=AUCKLAND)
    assert seconds_between(moment, later) == 1800
    assert seconds_between(later, moment) == -1800


def test_a_naive_datetime_is_refused_rather_than_read_as_host_time() -> None:
    with pytest.raises(ValueError):
        elapsed_after(datetime(2026, 9, 27, 1, 59), timedelta(minutes=3))
    with pytest.raises(ValueError):
        seconds_between(datetime(2026, 9, 27, 1, 59), datetime.now(tz=AUCKLAND))
