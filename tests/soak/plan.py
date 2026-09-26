"""The soak's timetable (§22.7), and how a time-compression factor changes it.

§22.7's load, at compression 1 (the real 72-hour run):

========================  =============  ==========================================
Load                      Every          For
========================  =============  ==========================================
scheduled scene           15 minutes     (the application's own cron rules fire it)
KNX telegrams at 5/s      hour           30 seconds
external control          hour           5 minutes of Art-Net from the node
mixer reconnection        day            kill, refused, timed out, restore, resync
measurements              12 hours       plus one at the start and one at the end
scripted fader movement   continuous     a 3-second drag at 30 Hz every minute
========================  =============  ==========================================

A rehearsal divides every interval by the compression factor, so 72 hours at
compression 36 is two hours with every daily event twice and every hourly one
72 times. Two things cannot be compressed the same way:

* **Scheduled scenes run on cron**, which has one-minute resolution. The
  period is the largest of :data:`SCENE_PERIODS` not above ``15 / factor``,
  never below one minute. Each period divides 30, so the two alternating
  rules (A on even periods, B on odd ones) keep alternating across the hour.
* **Durations shrink too, but not below a floor**: the external-control
  window must outlast §7.2.7's five seconds of silence several times over,
  and a KNX burst must still be a burst. A duration is never longer than
  half its interval, so one occurrence ends before the next begins.

Fader movement is an operator's pace, not a clock's, so it is not compressed.

Everything here is a pure function of the options, so a test can check the
timetable under any factor without running anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Literal

#: §22.7's real run.
BASE_DURATION_S: Final = 72 * 3600.0
BASE_SCENE_PERIOD_MIN: Final = 15
BASE_SAMPLE_INTERVAL_S: Final = 12 * 3600.0
BASE_HOURLY_S: Final = 3600.0
BASE_DAILY_S: Final = 24 * 3600.0
BASE_KNX_BURST_S: Final = 30.0
BASE_EXTERNAL_S: Final = 5 * 60.0

#: Telegrams per second during a KNX burst — a rate, never compressed.
KNX_RATE_HZ: Final = 5.0
#: The shortest a compressed burst or external-control window may be.
KNX_BURST_FLOOR_S: Final = 10.0
EXTERNAL_FLOOR_S: Final = 20.0
#: A simulated desk sends at a lighting desk's usual rate.
DESK_FPS: Final = 30.0

#: Cron-expressible scene periods, in minutes: each divides 30, so "every
#: 2P minutes from 0" and "every 2P minutes from P" alternate cleanly.
SCENE_PERIODS: Final = (1, 2, 3, 5, 6, 10, 15)

#: Where in its own period each recurring event falls, as a fraction of it —
#: apart from each other and from the quarter hours the scenes run on.
KNX_PHASE: Final = 1 / 3  # 20 minutes past each hour
EXTERNAL_PHASE: Final = 2 / 3  # 40 minutes past each hour
MIXER_PHASE: Final = 1 / 4  # 06:00 into each day

#: Fader drags: an operator's pace, not compressed.
FADER_EVERY_S: Final = 60.0
FADER_DRAG_S: Final = 3.0
FADER_HZ: Final = 30.0

EventKind = Literal["sample", "knx_burst", "external_control", "mixer_cycle"]


@dataclass(frozen=True, slots=True)
class Event:
    """One scheduled load or measurement, ``at_s`` seconds after the soak starts."""

    at_s: float
    kind: EventKind
    duration_s: float = 0.0


@dataclass(frozen=True, slots=True)
class SoakPlan:
    """The whole timetable for one run."""

    compression: float
    duration_s: float
    scene_period_min: int
    sample_interval_s: float
    knx_interval_s: float
    knx_burst_s: float
    external_interval_s: float
    external_s: float
    mixer_interval_s: float
    events: tuple[Event, ...] = field(repr=False)

    def count(self, kind: EventKind) -> int:
        return sum(1 for e in self.events if e.kind == kind)

    @property
    def cron_a(self) -> str:
        """Scene A's rule: every other period, from the top of the hour."""
        return scene_cron(self.scene_period_min, odd=False)

    @property
    def cron_b(self) -> str:
        """Scene B's rule: the periods in between."""
        return scene_cron(self.scene_period_min, odd=True)

    def as_dict(self) -> dict[str, object]:
        return {
            "compression": self.compression,
            "duration_s": self.duration_s,
            "scene_period_min": self.scene_period_min,
            "scene_cron": [self.cron_a, self.cron_b],
            "sample_interval_s": self.sample_interval_s,
            "knx_interval_s": self.knx_interval_s,
            "knx_burst_s": self.knx_burst_s,
            "knx_rate_hz": KNX_RATE_HZ,
            "external_interval_s": self.external_interval_s,
            "external_s": self.external_s,
            "mixer_interval_s": self.mixer_interval_s,
            "fader": {"every_s": FADER_EVERY_S, "drag_s": FADER_DRAG_S, "hz": FADER_HZ},
            "counts": {
                kind: self.count(kind)
                for kind in ("sample", "knx_burst", "external_control", "mixer_cycle")
            },
        }


def scene_period(compression: float) -> int:
    """The scene period in whole minutes: 15 compressed, cron-expressible, at least 1."""
    wanted = BASE_SCENE_PERIOD_MIN / compression
    fitting = [p for p in SCENE_PERIODS if p <= wanted + 1e-9]
    return max(fitting) if fitting else SCENE_PERIODS[0]


def scene_cron(period_min: int, *, odd: bool) -> str:
    """A 5-field cron for every ``2 × period`` minutes, offset by one period when ``odd``."""
    if period_min not in SCENE_PERIODS:
        raise ValueError(f"a scene period must be one of {SCENE_PERIODS}, not {period_min}")
    step = 2 * period_min
    minute = f"{period_min}-59/{step}" if odd else f"*/{step}"
    return f"{minute} * * * *"


def scheduled_times(period_min: int, start: datetime, end: datetime) -> list[datetime]:
    """Every wall-clock minute boundary in ``(start, end]`` the scene rules fire on.

    Both rules together fire on every multiple of the period past the hour;
    this is what the scoring expects to find in the execution log.
    """
    first = start.replace(second=0, microsecond=0) + timedelta(minutes=1)
    times: list[datetime] = []
    moment = first
    while moment <= end:
        if moment.minute % period_min == 0:
            times.append(moment)
        moment += timedelta(minutes=1)
    return times


def _recurring(
    kind: EventKind, interval_s: float, phase: float, duration_s: float, total_s: float
) -> list[Event]:
    events: list[Event] = []
    at = interval_s * phase
    while at + duration_s <= total_s:
        events.append(Event(round(at, 3), kind, round(duration_s, 3)))
        at += interval_s
    return events


def _compressed_duration(base_s: float, floor_s: float, interval_s: float, factor: float) -> float:
    return min(max(base_s / factor, floor_s), base_s, interval_s / 2)


def build_plan(compression: float = 1.0, duration_s: float | None = None) -> SoakPlan:
    """The timetable for ``compression``; ``duration_s`` defaults to 72 hours compressed."""
    if compression < 1:
        raise ValueError("the compression factor is 1 (real time) or more")
    total = BASE_DURATION_S / compression if duration_s is None else duration_s
    if total <= 0:
        raise ValueError("the soak needs a positive duration")
    sample_interval = BASE_SAMPLE_INTERVAL_S / compression
    hourly = BASE_HOURLY_S / compression
    daily = BASE_DAILY_S / compression
    knx_burst = _compressed_duration(BASE_KNX_BURST_S, KNX_BURST_FLOOR_S, hourly, compression)
    external = _compressed_duration(BASE_EXTERNAL_S, EXTERNAL_FLOOR_S, hourly, compression)

    events: list[Event] = []
    at = 0.0
    while at < total - 1e-6:
        events.append(Event(round(at, 3), "sample"))
        at += sample_interval
    events.append(Event(round(total, 3), "sample"))  # the end, whatever the interval
    events += _recurring("knx_burst", hourly, KNX_PHASE, knx_burst, total)
    events += _recurring("external_control", hourly, EXTERNAL_PHASE, external, total)
    events += _recurring("mixer_cycle", daily, MIXER_PHASE, 0.0, total)
    events.sort(key=lambda e: (e.at_s, e.kind != "sample"))

    return SoakPlan(
        compression=compression,
        duration_s=total,
        scene_period_min=scene_period(compression),
        sample_interval_s=sample_interval,
        knx_interval_s=hourly,
        knx_burst_s=knx_burst,
        external_interval_s=hourly,
        external_s=external,
        mixer_interval_s=daily,
        events=tuple(events),
    )
