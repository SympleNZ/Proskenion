"""Five-field cron, evaluated in Pacific/Auckland (spec §8.3).

Pure functions and one frozen value type, no I/O. :mod:`proskenion.rules.model`'s
``validate_cron`` decides what is accepted; this module gives exactly that
grammar its meaning, and :mod:`proskenion.rules.scheduler` fires it.

Grammar
-------
Five whitespace-separated fields::

    minute  hour  day-of-month  month  day-of-week
    0-59    0-23  1-31          1-12   0-7

Month also accepts ``JAN``–``DEC`` and day of week ``SUN``–``SAT``, in any
case. Day of week 0 and 7 are both Sunday. Each field is a comma-separated
list of items, and each item is one of:

``*``        every value in the field's range
``a``        that value
``a-b``      every value from ``a`` to ``b`` inclusive (never backwards)
``*/n``      every ``n``-th value from the start of the range: ``*/15`` is 0, 15, 30, 45
``a-b/n``    every ``n``-th value from ``a`` up to ``b``
``a/n``      every ``n``-th value from ``a`` to the end of the range: ``5/20`` is 5, 25, 45

Names work wherever a number does, including in ranges (``MON-FRI``).

Day of month and day of week
----------------------------
As in Vixie cron, the cron Debian ships: when **both** fields are restricted,
a day matches if **either** does (``0 9 1 * MON`` runs on the 1st *and* on
every Monday). When either is unrestricted, both must match, which in
practice means the restricted one decides. A field is unrestricted when it
starts with ``*`` — so ``*/2`` in day of month counts as unrestricted, exactly
as Vixie cron treats it.

An expression can be valid and still never occur: ``0 0 30 2 *`` (30 February).
:meth:`CronSchedule.next_after` returns ``None`` for it after searching
:data:`HORIZON_DAYS` ahead, which covers every leap-day combination.

Local time and daylight saving
------------------------------
Every field is matched against Pacific/Auckland wall-clock time. Two days a
year that clock is not a simple sequence:

* **The September gap.** 02:00–02:59 does not exist. A time that falls in it
  fires **once, at 03:00** — however many gap minutes match, and even if 03:00
  itself also matches.
* **The April repeat.** 02:00–02:59 happens twice. A time in it fires
  **once, on the first pass** (NZDT, +13:00). An every-minute rule is
  therefore quiet during the repeated hour.

Instants are returned in UTC. Aware datetimes sharing one ``tzinfo`` compare
and subtract by wall time, ignoring the offset (PEP 495), which is wrong across
a transition; callers compare and subtract in UTC.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Final

from proskenion.db.crud.base import AUCKLAND
from proskenion.rules.model import CRON_FIELDS, cron_value, validate_cron

#: How far ahead :meth:`CronSchedule.next_after` looks before deciding an
#: expression never occurs: eight years plus a margin, because 29 February
#: on a given weekday recurs within that and 2100 is not a leap year.
HORIZON_DAYS: Final = 8 * 366 + 7

_MINUTE: Final = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class CronSchedule:
    """A parsed cron expression. Build with :meth:`parse`."""

    expression: str
    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    """Sunday is 0; a 7 in the expression is stored as 0."""
    days_restricted: bool
    weekdays_restricted: bool

    @classmethod
    def parse(cls, expression: str) -> CronSchedule:
        """Parse what ``validate_cron`` accepts. Raises ``ValueError`` otherwise."""
        problem = validate_cron(expression)
        if problem is not None:
            raise ValueError(problem)
        parts = expression.split()
        fields = [
            _expand(part, low, high, names)
            for part, (_, low, high, names) in zip(parts, CRON_FIELDS, strict=True)
        ]
        minutes, hours, days, months, weekdays = fields
        return cls(
            expression=expression,
            minutes=tuple(sorted(minutes)),
            hours=tuple(sorted(hours)),
            days=frozenset(days),
            months=frozenset(months),
            weekdays=frozenset(0 if d == 7 else d for d in weekdays),
            days_restricted=not parts[2].startswith("*"),
            weekdays_restricted=not parts[4].startswith("*"),
        )

    def matches_day(self, day: date) -> bool:
        """Whether ``day`` is one the expression runs on (month, then the OR rule)."""
        if day.month not in self.months:
            return False
        by_day = day.day in self.days
        by_weekday = (day.isoweekday() % 7) in self.weekdays
        if self.days_restricted and self.weekdays_restricted:
            return by_day or by_weekday
        return by_day and by_weekday

    def matches(self, wall: datetime) -> bool:
        """Whether a naive local wall-clock minute matches every field."""
        return (
            wall.minute in self.minutes
            and wall.hour in self.hours
            and self.matches_day(wall.date())
        )

    def next_after(self, after: datetime, zone: tzinfo = AUCKLAND) -> datetime | None:
        """The first firing instant strictly after ``after``, in UTC, or ``None``.

        ``after`` must be aware. Gap and repeat are resolved as the module
        docstring says, so two matching gap minutes give one instant.
        """
        after_utc = after.astimezone(UTC)
        start = after_utc.astimezone(zone).replace(tzinfo=None, second=0, microsecond=0)
        first_day = start.date()
        for offset in range(HORIZON_DAYS):
            day = first_day + timedelta(days=offset)
            if not self.matches_day(day):
                continue
            for hour in self.hours:
                if offset == 0 and hour < start.hour:
                    continue
                for minute in self.minutes:
                    if offset == 0 and hour == start.hour and minute < start.minute:
                        continue
                    instant = resolve_local(datetime.combine(day, time(hour, minute)), zone)
                    if instant > after_utc:
                        return instant
        return None

    def occurrences(
        self, after: datetime, until: datetime, zone: tzinfo = AUCKLAND
    ) -> Iterator[datetime]:
        """Every firing instant in ``(after, until]``, in UTC, oldest first."""
        end = until.astimezone(UTC)
        moment = self.next_after(after, zone)
        while moment is not None and moment <= end:
            yield moment
            moment = self.next_after(moment, zone)


def resolve_local(wall: datetime, zone: tzinfo = AUCKLAND) -> datetime:
    """A naive local wall-clock time as a UTC instant, by the DST rules above.

    A repeated time is its first occurrence (``fold=0``). A time that does not
    exist becomes the first local minute after the gap.
    """
    moment = wall
    for _ in range(24 * 60):
        instant = moment.replace(tzinfo=zone, fold=0).astimezone(UTC)
        if instant.astimezone(zone).replace(tzinfo=None) == moment:
            return instant
        moment += _MINUTE
    raise ValueError(f"no local time after {wall.isoformat()} exists in {zone}")


def _expand(field: str, low: int, high: int, names: tuple[str, ...] | None) -> set[int]:
    values: set[int] = set()
    for item in field.split(","):
        base, slash, step_text = item.partition("/")
        step = int(step_text) if slash else 1
        if base == "*":
            start, end = low, high
        else:
            first, dash, last = base.partition("-")
            start = _value(first, low, names)
            if dash:
                end = _value(last, low, names)
            else:
                end = high if slash else start
        values.update(range(start, end + 1, step))
    return values


def _value(text: str, low: int, names: tuple[str, ...] | None) -> int:
    number = cron_value(text, low, names)
    if number is None:  # validate_cron has already refused anything else
        raise ValueError(f"{text!r} is not a value")
    return number


__all__ = ["HORIZON_DAYS", "CronSchedule", "resolve_local"]
