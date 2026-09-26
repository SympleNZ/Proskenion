"""Vitals thresholds (spec §11.2), every row at its boundaries."""

from __future__ import annotations

import math

import pytest

from proskenion.core.vitals import METRICS, Level, classify, worst

# (metric, value, expected) — the §11.2 table, sampled exactly at and around
# each threshold. Thresholds are strict: "> 70" means 70 is still green.
ROWS: list[tuple[str, float, Level]] = [
    ("cpu_temperature_c", 70.0, "green"),
    ("cpu_temperature_c", 70.1, "amber"),
    ("cpu_temperature_c", 80.0, "amber"),
    ("cpu_temperature_c", 80.1, "red"),
    ("memory_used_percent", 80.0, "green"),
    ("memory_used_percent", 80.5, "amber"),
    ("memory_used_percent", 90.0, "amber"),
    ("memory_used_percent", 90.5, "red"),
    ("ssd_life_used_percent", 80.0, "green"),
    ("ssd_life_used_percent", 81.0, "amber"),
    ("ssd_life_used_percent", 95.0, "amber"),
    ("ssd_life_used_percent", 96.0, "red"),
    ("ssd_temperature_c", 60.0, "green"),
    ("ssd_temperature_c", 61.0, "amber"),
    ("ssd_temperature_c", 70.0, "amber"),
    ("ssd_temperature_c", 71.0, "red"),
    ("ssd_media_errors", 0, "green"),
    ("ssd_media_errors", 1, "amber"),
    ("ssd_media_errors", 1_000_000, "amber"),  # never red: email instead
    ("data_free_mb", 500.0, "green"),
    ("data_free_mb", 499.9, "amber"),
    ("data_free_mb", 100.0, "amber"),
    ("data_free_mb", 99.9, "red"),
    ("data_free_mb", 0.0, "red"),
    ("data_used_percent", 80.0, "green"),
    ("data_used_percent", 80.1, "amber"),
    ("data_used_percent", 90.0, "amber"),
    ("data_used_percent", 90.1, "red"),
    ("loop_lag_p99_ms", 100.0, "green"),
    ("loop_lag_p99_ms", 100.1, "amber"),
    ("loop_lag_p99_ms", 500.0, "amber"),
    ("loop_lag_p99_ms", 500.1, "red"),
    ("bus_drop_count", 0, "green"),
    ("bus_drop_count", 1, "amber"),
    ("bus_drop_count", 10_000, "amber"),
    ("bus_drop_consecutive_windows", 0, "green"),
    ("bus_drop_consecutive_windows", 1, "amber"),
    ("bus_drop_consecutive_windows", 2, "red"),
]


@pytest.mark.parametrize(("metric", "value", "expected"), ROWS)
def test_thresholds_at_boundaries(metric: str, value: float, expected: Level) -> None:
    assert classify(metric, value) == expected


def test_every_metric_is_covered_by_the_table() -> None:
    assert {metric for metric, _, _ in ROWS} == set(METRICS)


@pytest.mark.parametrize("metric", sorted(METRICS))
def test_none_and_nan_are_unknown(metric: str) -> None:
    assert classify(metric, None) == "unknown"
    assert classify(metric, math.nan) == "unknown"


def test_unknown_metric_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        classify("gpu_temperature_c", 50.0)


def test_worst_orders_levels() -> None:
    assert worst("green", "amber") == "amber"
    assert worst("red", "amber", "green") == "red"
    assert worst("unknown", "green") == "green"
    assert worst("unknown") == "unknown"
    assert worst() == "unknown"
