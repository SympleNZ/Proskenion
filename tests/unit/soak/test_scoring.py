"""Scoring a soak against §22.7 and §23.3: a pass case and a fail case for each criterion."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.soak import run, scoring
from tests.soak.scoring import (
    CycleCheck,
    DeviceReading,
    Injection,
    Sample,
    ScheduledFire,
    SoakData,
    StatusChange,
)

MB = 1_000_000
DAY = 24 * 3600.0
CONNECTED = {k: DeviceReading("connected", 0, 2.0) for k in ("dmx", "mixer", "knx")}


def sample(t: float, **changes: Any) -> Sample:
    base = Sample(
        t=t,
        at=f"t{t}",
        pid=100,
        starttime_ticks=5,
        boot_id="boot",
        rss_bytes=80 * MB,
        fds=40,
        threads=6,
        io_write_bytes=0,
        device_write_bytes=None,
        db_bytes=2 * MB,
        tasks=40,
        websockets=2,
        loop_lag_p50_ms=1.0,
        loop_lag_p99_ms=5.0,
        devices=CONNECTED,
        systemd_restarts=0,
    )
    return replace(base, **changes)


def good() -> SoakData:
    fires = [ScheduledFire(f"2026-09-25T0{h}:00:00+00:00", "success", 8.0, 12.0) for h in range(3)]
    return SoakData(
        compression=1,
        duration_s=72 * 3600,
        planned={"sample": 2, "mixer_cycle": 1},
        executed={"sample": 2, "mixer_cycle": 1},
        samples=[
            sample(0),
            sample(DAY, rss_bytes=82 * MB, io_write_bytes=300 * MB, tasks=42),
        ],
        status_changes=[
            StatusChange(100, "mixer", "degraded"),
            StatusChange(110, "mixer", "error"),
        ],
        injections=[Injection("mixer", 90, 200, "mixer cycle")],
        fires=fires,
        expected_fire_times=[f.scheduled_for for f in fires],
        cycles=[CycleCheck("mixer_cycle", 90, {"refused_is_amber": True, "state_resynced": True})],
        expected_websockets=2,
    )


def criterion(report: scoring.Report, name: str) -> scoring.Criterion:
    return next(c for c in report.criteria if c.name == name)


def test_a_clean_real_run_passes_every_criterion() -> None:
    report = scoring.score(good())
    assert report.verdict == "PASS", [c for c in report.criteria if not c.passed]
    assert not report.indicative
    assert all(c.passed for c in report.criteria)


def test_memory_growth_of_5_mb_fails() -> None:
    data = good()
    data.samples = [data.samples[0], replace(data.samples[1], rss_bytes=85 * MB)]
    report = scoring.score(data)
    assert not criterion(report, "memory_growth").passed
    assert report.verdict == "FAIL"
    data.samples = [data.samples[0], replace(data.samples[1], rss_bytes=85 * MB - 1)]
    assert criterion(scoring.score(data), "memory_growth").passed


def test_one_traceback_fails() -> None:
    data = good()
    data.exceptions = [{"timestamp": "x", "logger": "proskenion.core", "message": "boom"}]
    c = criterion(scoring.score(data), "unhandled_exceptions")
    assert not c.passed and "boom" in c.detail


def test_a_device_down_outside_an_injection_is_decay_but_inside_one_is_not() -> None:
    data = good()
    mid = sample(150, devices={**CONNECTED, "mixer": DeviceReading("error", 0, None)})
    data.samples = [data.samples[0], mid, data.samples[1]]
    assert criterion(scoring.score(data), "device_connection_decay").passed
    data.samples = [data.samples[0], replace(mid, t=500), data.samples[1]]
    assert not criterion(scoring.score(data), "device_connection_decay").passed


def test_reconnects_on_a_device_nothing_was_injected_into_are_decay() -> None:
    data = good()
    end = {**CONNECTED, "dmx": DeviceReading("connected", 3, 2.0)}
    data.samples = [data.samples[0], replace(data.samples[1], devices=end)]
    assert not criterion(scoring.score(data), "device_connection_decay").passed
    end = {**CONNECTED, "mixer": DeviceReading("connected", 3, 2.0)}  # the injected one
    data.samples = [data.samples[0], replace(data.samples[1], devices=end)]
    assert criterion(scoring.score(data), "device_connection_decay").passed


def test_a_round_trip_that_more_than_doubles_by_50_ms_is_decay() -> None:
    data = good()
    end = {**CONNECTED, "dmx": DeviceReading("connected", 0, 60.0)}
    data.samples = [data.samples[0], replace(data.samples[1], devices=end)]
    assert not criterion(scoring.score(data), "device_connection_decay").passed


@pytest.mark.parametrize(
    ("dispatch", "start", "ok"),
    [(99.9, 100.0, True), (100.1, 20.0, False), (5.0, 100.5, False), (5.0, None, False)],
)
def test_every_scheduled_scene_within_100_ms(
    dispatch: float, start: float | None, ok: bool
) -> None:
    data = good()
    data.fires = [
        *data.fires[:-1],
        replace(data.fires[-1], dispatch_latency_ms=dispatch, scene_start_latency_ms=start),
    ]
    assert criterion(scoring.score(data), "scheduled_scenes_on_time").passed is ok


def test_an_expected_time_that_never_fired_or_was_missed_fails() -> None:
    data = good()
    data.expected_fire_times = [*data.expected_fire_times, "2026-09-25T05:00:00+00:00"]
    assert not criterion(scoring.score(data), "scheduled_scenes_on_time").passed
    data = good()
    data.missed = 1
    assert not criterion(scoring.score(data), "scheduled_scenes_on_time").passed


def test_ssd_writes_over_1_gb_a_day_fail_and_the_partition_counter_wins() -> None:
    data = good()
    assert criterion(scoring.score(data), "ssd_writes").measured == 300 * MB
    data.samples = [data.samples[0], replace(data.samples[1], io_write_bytes=1001 * MB)]
    assert not criterion(scoring.score(data), "ssd_writes").passed
    # The partition's own counter, where there is one, is what is scored.
    data.samples = [
        replace(data.samples[0], device_write_bytes=10 * MB),
        replace(data.samples[1], device_write_bytes=510 * MB),
    ]
    c = criterion(scoring.score(data), "ssd_writes")
    assert c.passed and c.measured == 500 * MB and "partition" in c.detail


def test_task_count_growth_fails() -> None:
    data = good()
    data.samples = [data.samples[0], replace(data.samples[1], tasks=46)]
    assert not criterion(scoring.score(data), "task_count_stable").passed
    data.samples = [data.samples[0], replace(data.samples[0], t=10, tasks=51), data.samples[1]]
    assert not criterion(scoring.score(data), "task_count_stable").passed


@pytest.mark.parametrize(
    "change", [{"pid": 101}, {"starttime_ticks": 6}, {"boot_id": "other"}, {"systemd_restarts": 1}]
)
def test_a_restart_or_a_reboot_fails(change: dict[str, Any]) -> None:
    data = good()
    data.samples = [data.samples[0], replace(data.samples[1], **change)]
    assert not criterion(scoring.score(data), "no_forced_restart").passed


def test_a_red_outside_an_injection_fails() -> None:
    data = good()
    data.status_changes = [*data.status_changes, StatusChange(5000, "dmx", "error")]
    assert not criterion(scoring.score(data), "no_untraceable_red").passed
    # Amber outside an injection is not red.
    data.status_changes = [StatusChange(5000, "dmx", "degraded")]
    assert criterion(scoring.score(data), "no_untraceable_red").passed


def test_a_load_that_did_not_run_or_failed_its_checks_fails() -> None:
    data = good()
    data.executed = {"sample": 2}
    assert not criterion(scoring.score(data), "load_exercised").passed
    data = good()
    data.cycles = [CycleCheck("mixer_cycle", 90, {"timed_out_is_red": False})]
    c = criterion(scoring.score(data), "load_exercised")
    assert not c.passed and "timed_out_is_red" in c.detail


def test_section_23_3_is_a_warning_not_a_failure() -> None:
    data = good()
    data.samples = [data.samples[0], replace(data.samples[1], tasks=85, fds=160)]
    data.samples = [replace(data.samples[0], tasks=84), data.samples[1]]
    report = scoring.score(data)
    assert not criterion(report, "tasks_under_23_3").passed
    assert not criterion(report, "fds_under_23_3").passed
    assert report.verdict == "PASS"
    assert {c.name for c in report.warnings} >= {"tasks_under_23_3", "fds_under_23_3"}


def test_a_compressed_run_is_marked_indicative() -> None:
    data = good()
    data.compression, data.duration_s = 36, 7200
    report = scoring.score(data)
    assert report.indicative and "indicative" in report.note


def test_a_results_directory_scores_the_same_as_the_data(tmp_path: Path) -> None:
    """``python -m tests.soak score`` reads what the runner wrote."""
    rows = [
        {
            "t": t,
            "at": "x",
            "process": {
                "pid": 7,
                "starttime_ticks": 1,
                "boot_id": "b",
                "rss_bytes": rss,
                "fds": 30,
                "threads": 5,
                "io_write_bytes": io,
                "device_write_bytes": None,
                "db_bytes": 100,
            },
            "diagnostics": {
                "tasks": 30,
                "websocket_connections": 2,
                "loop_lag_p50_ms": 1.0,
                "loop_lag_p99_ms": 3.0,
            },
            "devices": {"mixer": {"status": "connected", "reconnects": 0, "latency_ms": None}},
            "systemd_restarts": None,
        }
        for t, rss, io in ((0.0, 50 * MB, 0), (7200.0, 51 * MB, 10 * MB))
    ]
    (tmp_path / "samples.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    setup_red = {  # KNX red before its stub was up: commissioning, not soak
        "t": 1.0,
        "kind": "device_status",
        "device": "knx",
        "status": "error",
        "phase": "setup",
    }
    injected_red = {"t": 30.0, "kind": "device_status", "device": "mixer", "status": "error"}
    (tmp_path / "events.jsonl").write_text(
        json.dumps(setup_red) + "\n" + json.dumps(injected_red) + "\n"
    )
    (tmp_path / "collected.json").write_text(
        json.dumps(
            {
                "compression": 36,
                "duration_s": 7200,
                "planned": {"sample": 2},
                "executed": {"sample": 2},
                "fires": [
                    {
                        "scheduled_for": "2026-09-25T10:00:00+12:00",
                        "result": "success",
                        "dispatch_latency_ms": 9.0,
                        "scene_start_latency_ms": 11.0,
                    }
                ],
                "expected_fire_times": ["2026-09-24T22:00:00+00:00"],
                "injections": [{"device": "mixer", "start_t": 20.0, "end_t": 60.0, "what": "x"}],
                "cycles": [],
                "exceptions": [],
            }
        )
    )
    report = run.score_directory(tmp_path)
    # The same instant in two offsets is one fire; the red was injected.
    assert report.verdict == "PASS", [c for c in report.criteria if not c.passed]
    assert (tmp_path / "report.json").exists()
    assert "Verdict: PASS" in (tmp_path / "report.txt").read_text(encoding="utf-8")
