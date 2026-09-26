"""Honest measurement: monotonic clocks, linear-interpolated percentiles,
warm-up. Nothing here talks to the network — it only turns a list of
durations into the numbers the report needs, so it can be proven correct on
its own (``tests/unit/tools/test_perf_harness.py``'s self-check measures a
stub endpoint with a known injected delay through this same module).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from math import ceil, floor

Clock = Callable[[], float]

#: The default clock every scenario uses: monotonic, immune to wall-clock
#: adjustments (NTP, DST) that would corrupt a duration mid-measurement.
monotonic: Clock = time.monotonic


def percentile(values: list[float], pct: float) -> float:
    """The ``pct`` percentile (0-100) of ``values`` by linear interpolation
    between closest ranks — the same method ``numpy.percentile``'s default
    uses, chosen so a reader can sanity-check this against a spreadsheet."""
    if not values:
        return float("nan")
    if not 0.0 <= pct <= 100.0:
        raise ValueError(f"pct must be 0-100, got {pct}")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (pct / 100.0)
    lower, upper = floor(rank), ceil(rank)
    if lower == upper:
        return ordered[int(rank)]
    fraction = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


@dataclass(slots=True)
class Measurement:
    """Durations recorded for one scenario, in seconds. ``samples`` holds
    every one that survived warm-up — the p50/p95/max the report shows."""

    samples: list[float] = field(default_factory=list)
    #: Samples discarded as warm-up, kept only for the record.
    warm_up_discarded: int = 0
    #: Failures (an error response, a dropped connection) — counted
    #: separately from ``samples`` so a slow success and a failure are never
    #: confused with each other.
    failures: int = 0

    def record(self, duration_s: float) -> None:
        self.samples.append(duration_s)

    @property
    def count(self) -> int:
        return len(self.samples)

    def percentile_ms(self, pct: float) -> float:
        return percentile(self.samples, pct) * 1000.0

    @property
    def p50_ms(self) -> float:
        return self.percentile_ms(50.0)

    @property
    def p95_ms(self) -> float:
        return self.percentile_ms(95.0)

    @property
    def max_ms(self) -> float:
        return max(self.samples) * 1000.0 if self.samples else float("nan")

    @property
    def mean_ms(self) -> float:
        return (sum(self.samples) / len(self.samples)) * 1000.0 if self.samples else float("nan")

    def rate_per_second(self, wall_time_s: float) -> float:
        """Throughput over the whole run, warm-up excluded from the count but
        not from ``wall_time_s`` — the caller passes the window the samples
        were actually collected over."""
        if wall_time_s <= 0:
            return float("nan")
        return self.count / wall_time_s


async def timed(coro: Awaitable[object]) -> tuple[float, object, Exception | None]:
    """Await ``coro``, returning ``(duration_s, result, error)``. ``result``
    is ``None`` when ``error`` is not — the duration is still returned so a
    failure's latency is visible rather than silently dropped."""
    start = monotonic()
    try:
        result = await coro
    except Exception as exc:  # noqa: BLE001 - reported to the caller, not swallowed
        return monotonic() - start, None, exc
    return monotonic() - start, result, None


async def run_concurrent_workers(
    *,
    concurrency: int,
    duration_s: float,
    warm_up_s: float,
    operation: Callable[[int], Awaitable[object]],
) -> tuple[Measurement, float]:
    """Run ``concurrency`` workers, each calling ``operation(worker_index)``
    back to back until ``duration_s`` has elapsed, discarding the first
    ``warm_up_s`` of samples from each worker. Returns the measurement and
    the wall-clock length of the recording window (for the rate — throughput
    is counted over the window actually recorded, not the whole run
    including warm-up).

    ``operation`` receives the worker index so a caller can round-robin
    across several endpoints or give each worker its own write token space.
    """
    measurement = Measurement()
    start = monotonic()
    deadline = start + warm_up_s + duration_s
    warm_until = start + warm_up_s

    async def worker(index: int) -> None:
        while monotonic() < deadline:
            duration, _, error = await timed(operation(index))
            if monotonic() < warm_until:
                measurement.warm_up_discarded += 1
                continue
            if error is not None:
                measurement.failures += 1
                continue
            measurement.record(duration)

    await asyncio.gather(*(worker(i) for i in range(concurrency)))
    recording_window_s = monotonic() - warm_until
    return measurement, recording_window_s


def clamp_duration(
    requested_s: float, *, minimum_s: float = 0.2, maximum_s: float = 120.0
) -> float:
    """Keep a caller-supplied ``--duration`` sane: long enough to mean
    something, short enough that a mistyped ``--duration 3600`` does not
    hammer the CM5 for an hour by accident."""
    return max(minimum_s, min(maximum_s, requested_s))
