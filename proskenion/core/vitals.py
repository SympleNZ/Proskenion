"""System vitals thresholds (spec §11.2).

:func:`classify` is a pure function of a metric name and a value so the health
screen and the email alerts colour a reading identically. Values are read
through the platform layer (§5.4); this module never touches hardware.

The §11.2 table:

=================================  =================  ==================
Metric                             Amber              Red
=================================  =================  ==================
CPU temperature                    > 70 °C            > 80 °C
Memory used                        > 80 %             > 90 %
SSD life used                      > 80 %             > 95 %
SSD temperature                    > 60 °C            > 70 °C
SSD media errors                   > 0                — (email immediately)
/data free space                   < 500 MB           < 100 MB
/data used                         > 80 %             > 90 %
Event loop lag, p99 over 5 min     > 100 ms           > 500 ms
Event bus drop count, continuous   > 0 in 5 min       sustained
=================================  =================  ==================

The two ``/data`` rows are not competing thresholds: absolute free space drives
auto-pruning and write suspension, percentage used only colours the health
screen's "used space" row.

"Sustained" for the bus drop row is not defined numerically in the spec. Here
``bus_drop_count`` is the drop count over the last five minutes (amber when
positive, never red on its own) and ``bus_drop_consecutive_windows`` is the
number of consecutive five-minute windows with at least one drop — one is
amber, two or more is sustained and therefore red.
"""

from __future__ import annotations

import math
from typing import Final, Literal

Level = Literal["green", "amber", "red", "unknown"]

Metric = Literal[
    "cpu_temperature_c",
    "memory_used_percent",
    "ssd_life_used_percent",
    "ssd_temperature_c",
    "ssd_media_errors",
    "data_free_mb",
    "data_used_percent",
    "loop_lag_p99_ms",
    "bus_drop_count",
    "bus_drop_consecutive_windows",
]

# (amber, red) — a reading strictly above (or, for "below" metrics, strictly
# below) the threshold trips it. ``None`` means the level is never reached.
_ABOVE: Final[dict[str, tuple[float, float | None]]] = {
    "cpu_temperature_c": (70.0, 80.0),
    "memory_used_percent": (80.0, 90.0),
    "ssd_life_used_percent": (80.0, 95.0),
    "ssd_temperature_c": (60.0, 70.0),
    "ssd_media_errors": (0.0, None),
    "data_used_percent": (80.0, 90.0),
    "loop_lag_p99_ms": (100.0, 500.0),
    "bus_drop_count": (0.0, None),
    "bus_drop_consecutive_windows": (0.0, 1.0),
}
_BELOW: Final[dict[str, tuple[float, float]]] = {
    "data_free_mb": (500.0, 100.0),
}

METRICS: Final[frozenset[str]] = frozenset(_ABOVE) | frozenset(_BELOW)


def classify(metric: Metric | str, value: float | None) -> Level:
    """Colour one vitals reading per the §11.2 table.

    ``None`` (or NaN) is ``"unknown"`` — the health screen shows "not available",
    never a wrong colour. An unrecognised metric name is a programming error and
    raises ``ValueError``.
    """
    if metric not in METRICS:
        raise ValueError(f"unknown vitals metric {metric!r}")
    if value is None or math.isnan(value):
        return "unknown"
    if metric in _BELOW:
        amber_below, red_below = _BELOW[metric]
        if value < red_below:
            return "red"
        if value < amber_below:
            return "amber"
        return "green"
    amber_above, red_above = _ABOVE[metric]
    if red_above is not None and value > red_above:
        return "red"
    if value > amber_above:
        return "amber"
    return "green"


def worst(*levels: Level) -> Level:
    """The most severe of several levels; ``unknown`` never outranks a real reading."""
    order: dict[Level, int] = {"unknown": 0, "green": 1, "amber": 2, "red": 3}
    if not levels:
        return "unknown"
    return max(levels, key=lambda level: order[level])
