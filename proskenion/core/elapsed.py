"""Real elapsed time on aware datetimes (§4.9).

Adding a ``timedelta`` to a ``zoneinfo`` datetime moves the *wall clock*, not
real time: Python's aware arithmetic keeps the ``tzinfo`` and recomputes the
offset afterwards. Across a daylight-saving change in Pacific/Auckland that is
an hour out. Twelve hours from 20:00 on the Saturday before clocks go forward
is 08:00 on Sunday, which is eleven real hours later; three minutes from 02:59
on the first pass through the repeated hour in April is 03:02 in standard time,
sixty-three real minutes later. Subtracting two datetimes that share a
``zoneinfo`` object has the same flaw in reverse: the offsets are ignored and
the wall clocks are compared.

That is right for calendar arithmetic — "03:00 tomorrow", "ninety days of
retention", the cron scheduler — and wrong for anything meaning *this much real
time*: a session cap, a confirm window, a deadline. Those go through here,
which does the arithmetic in UTC and hands the answer back in the caller's
zone, so what is written out still carries the local offset §4.9 asks for.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        # A naive datetime would be read as the host's local time, which is the
        # ambiguity this module exists to remove.
        raise ValueError("elapsed-time arithmetic needs an aware datetime")
    return moment


def elapsed_after(moment: datetime, delta: timedelta) -> datetime:
    """The instant ``delta`` of real time after ``moment``, in ``moment``'s zone."""
    return (_aware(moment).astimezone(UTC) + delta).astimezone(moment.tzinfo)


def elapsed_before(moment: datetime, delta: timedelta) -> datetime:
    """The instant ``delta`` of real time before ``moment``, in ``moment``'s zone."""
    return (_aware(moment).astimezone(UTC) - delta).astimezone(moment.tzinfo)


def seconds_between(start: datetime, end: datetime) -> float:
    """Real seconds from ``start`` to ``end``; negative when ``end`` is earlier."""
    return (_aware(end).astimezone(UTC) - _aware(start).astimezone(UTC)).total_seconds()


__all__ = ["elapsed_after", "elapsed_before", "seconds_between"]
