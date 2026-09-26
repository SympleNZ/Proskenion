"""Scoring a soak against §22.7's pass/fail criteria and §23.3's resource targets.

Pure functions of what the run recorded (:class:`SoakData`), so every
threshold can be tested with a pass case and a fail case without running
anything.

The criteria, and how each is decided
-------------------------------------
§22.7 **Pass** — each is a ``fail``-severity criterion; any one failing fails
the soak:

``memory_growth``
    Resident memory at the last sample minus the first, under 5 MB
    (5 000 000 bytes: the stricter reading of "MB").
``unhandled_exceptions``
    No line in the application's log carries a traceback after the soak
    started. A traceback is how an exception that escaped its handler is
    logged here (``proskenion.core.tasks``), whoever caught it in the end.
``device_connection_decay``
    Outside an injected failure, every device reads ``connected`` at every
    sample, no device that was not injected reconnected, and no device's
    measured round trip more than doubled (and grew by 50 ms or more).
``scheduled_scenes_on_time``
    Every expected scheduled time fired, none was logged ``missed``, and
    each fire's dispatch *and* its scene's start were within 100 ms of the
    time it was scheduled for.
``ssd_writes``
    Bytes written per 24 hours, extrapolated from the whole run, within
    §23.3's "under load" budget of 1 GB. The partition's own counter is used
    where there is one (it is the SSD's wear); inside a container there is
    none, and the application's ``/proc/<pid>/io`` figure is used instead.
``task_count_stable``
    The asyncio task count at the end within :attr:`Thresholds.task_growth`
    of the start, and never more than :attr:`Thresholds.task_excursion`
    above it. §22.7 says "stable" and gives no number; these are ours.

§22.7 **Fail** additions, also ``fail`` severity:

``no_forced_restart``
    The same process (pid and start time) and the same boot throughout, and
    systemd's restart counter unchanged where it was read.
``no_untraceable_red``
    Every change of a device to red (``error``) falls inside a window in
    which the harness was injecting a failure into that device.
``load_exercised``
    Every planned load ran, and each cycle's own checks passed — the mixer
    showed refused (amber) and then timed out (red), came back and resynced;
    external control engaged and released. A soak whose load did not run
    has not tested anything.

§23.3's resources are ``warn`` severity: "not hard limits, but monitored".
RSS under 400 MB, descriptors under 150, tasks under 80 and loop-lag p99
under 100 ms (§11.2's amber) at every sample, and the WebSocket count steady.

A compressed or shortened run's growth figures are indicative only: the
report says so (:attr:`Report.indicative`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Final, Literal

Severity = Literal["fail", "warn"]

MB: Final = 1_000_000
DAY_S: Final = 24 * 3600.0


@dataclass(frozen=True, slots=True)
class Thresholds:
    """Every number the scoring compares against, in one place."""

    memory_growth_bytes: int = 5 * MB  # §22.7
    scene_latency_ms: float = 100.0  # §22.7, §23.2
    ssd_bytes_per_day: int = 1000 * MB  # §23.3, under load
    ssd_idle_bytes_per_day: int = 200 * MB  # §23.3, idle (reported only)
    task_growth: int = 5  # ours: §22.7 says "stable"
    task_excursion: int = 10  # ours
    fd_growth: int = 5  # ours: §22.7 measures descriptors and sets no number
    latency_growth_ms: float = 50.0  # ours: "no decay"
    rss_bytes: int = 400 * MB  # §23.3, under load
    fds: int = 150  # §23.3, under load
    tasks: int = 80  # §23.3, under load
    loop_lag_p99_ms: float = 100.0  # §11.2 amber
    #: A device may still read red this long after its injected failure was lifted.
    red_grace_s: float = 5.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DeviceReading:
    status: str
    reconnects: int = 0
    latency_ms: float | None = None


@dataclass(frozen=True, slots=True)
class Sample:
    """One 12-hourly measurement (§22.7), flattened."""

    t: float
    at: str
    pid: int | None
    starttime_ticks: int | None
    boot_id: str | None
    rss_bytes: int | None
    fds: int | None
    threads: int | None
    io_write_bytes: int | None
    device_write_bytes: int | None
    db_bytes: int | None
    tasks: int | None
    websockets: int | None
    loop_lag_p50_ms: float | None
    loop_lag_p99_ms: float | None
    devices: Mapping[str, DeviceReading] = field(default_factory=dict)
    systemd_restarts: int | None = None


@dataclass(frozen=True, slots=True)
class StatusChange:
    t: float
    device: str
    status: str


@dataclass(frozen=True, slots=True)
class Injection:
    """A window in which the harness was deliberately breaking ``device``."""

    device: str
    start_t: float
    end_t: float
    what: str


@dataclass(frozen=True, slots=True)
class ScheduledFire:
    """One scheduled rule firing, from the rule execution log and the scene log."""

    scheduled_for: str
    result: str
    dispatch_latency_ms: float | None
    scene_start_latency_ms: float | None


@dataclass(frozen=True, slots=True)
class CycleCheck:
    """One run of a load that has checks of its own (mixer, external control)."""

    kind: str
    t: float
    checks: Mapping[str, bool]

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(self.checks.values())


@dataclass(slots=True)
class SoakData:
    """Everything the scoring reads, as the runner recorded it."""

    compression: float
    duration_s: float
    planned: Mapping[str, int]
    executed: Mapping[str, int]
    samples: Sequence[Sample]
    status_changes: Sequence[StatusChange] = ()
    injections: Sequence[Injection] = ()
    fires: Sequence[ScheduledFire] = ()
    expected_fire_times: Sequence[str] = ()
    missed: int = 0
    exceptions: Sequence[Mapping[str, Any]] = ()
    cycles: Sequence[CycleCheck] = ()
    expected_websockets: int | None = None


@dataclass(frozen=True, slots=True)
class Criterion:
    name: str
    severity: Severity
    passed: bool
    measured: Any
    threshold: Any
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Report:
    verdict: Literal["PASS", "FAIL"]
    indicative: bool
    note: str
    criteria: tuple[Criterion, ...]
    samples: tuple[Sample, ...]
    thresholds: Thresholds

    @property
    def failed(self) -> list[Criterion]:
        return [c for c in self.criteria if not c.passed and c.severity == "fail"]

    @property
    def warnings(self) -> list[Criterion]:
        return [c for c in self.criteria if not c.passed and c.severity == "warn"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "indicative": self.indicative,
            "note": self.note,
            "criteria": [c.as_dict() for c in self.criteria],
            "samples": [asdict(s) for s in self.samples],
            "thresholds": self.thresholds.as_dict(),
        }


# -- helpers --------------------------------------------------------------------------


def _first_last[T](values: Iterable[T | None]) -> tuple[T, T] | None:
    known = [v for v in values if v is not None]
    return (known[0], known[-1]) if known else None


def _in_window(t: float, device: str, injections: Sequence[Injection], grace_s: float) -> bool:
    return any(i.device == device and i.start_t <= t <= i.end_t + grace_s for i in injections)


def _injected_devices(injections: Sequence[Injection]) -> set[str]:
    return {i.device for i in injections}


def _mb(value: float) -> str:
    return f"{value / MB:.2f} MB"


# -- the criteria ---------------------------------------------------------------------


def memory_growth(data: SoakData, th: Thresholds) -> Criterion:
    pair = _first_last(s.rss_bytes for s in data.samples)
    if pair is None:
        return Criterion(
            "memory_growth",
            "fail",
            False,
            None,
            th.memory_growth_bytes,
            "resident memory was never read",
        )
    growth = pair[1] - pair[0]
    return Criterion(
        "memory_growth",
        "fail",
        growth < th.memory_growth_bytes,
        growth,
        th.memory_growth_bytes,
        f"RSS {_mb(pair[0])} at the start, {_mb(pair[1])} at the end: {_mb(growth)} growth",
    )


def unhandled_exceptions(data: SoakData, th: Thresholds) -> Criterion:
    count = len(data.exceptions)
    first = data.exceptions[0] if data.exceptions else None
    detail = (
        "no traceback in the application log after the soak started"
        if first is None
        else f"{count} traceback(s); first at {first.get('timestamp')}: "
        f"{first.get('logger')}: {first.get('message')}"
    )
    return Criterion("unhandled_exceptions", "fail", count == 0, count, 0, detail)


def device_connection_decay(data: SoakData, th: Thresholds) -> Criterion:
    problems: list[str] = []
    injected = _injected_devices(data.injections)
    for sample in data.samples:
        for key, reading in sample.devices.items():
            if reading.status == "connected":
                continue
            if _in_window(sample.t, key, data.injections, th.red_grace_s):
                continue
            problems.append(f"{key} read {reading.status} at t={sample.t:.0f}s")
    if data.samples:
        first, last = data.samples[0], data.samples[-1]
        for key, end in last.devices.items():
            start = first.devices.get(key)
            if start is None:
                continue
            if key not in injected and end.reconnects > start.reconnects:
                problems.append(
                    f"{key} reconnected {end.reconnects - start.reconnects} time(s) "
                    "with nothing injected"
                )
            if start.latency_ms is not None and end.latency_ms is not None:
                limit = max(start.latency_ms * 2, start.latency_ms + th.latency_growth_ms)
                if end.latency_ms > limit:
                    problems.append(
                        f"{key} round trip {start.latency_ms:.1f} ms -> {end.latency_ms:.1f} ms"
                    )
        missing = sorted(set(first.devices) - set(last.devices))
        problems += [f"{key} present at the start and gone at the end" for key in missing]
    return Criterion(
        "device_connection_decay",
        "fail",
        not problems and bool(data.samples),
        len(problems),
        0,
        "; ".join(problems[:10]) if problems else "every device connected at every sample",
    )


def scheduled_scenes_on_time(data: SoakData, th: Thresholds) -> Criterion:
    fired = {f.scheduled_for for f in data.fires}
    missing = [t for t in data.expected_fire_times if t not in fired]
    late: list[str] = []
    worst = 0.0
    for fire in data.fires:
        for label, value in (
            ("dispatch", fire.dispatch_latency_ms),
            ("scene start", fire.scene_start_latency_ms),
        ):
            if value is None:
                late.append(f"{fire.scheduled_for}: no {label} time")
                continue
            worst = max(worst, value)
            if value > th.scene_latency_ms:
                late.append(f"{fire.scheduled_for}: {label} {value:.1f} ms")
    ok = not missing and not late and data.missed == 0 and bool(data.fires)
    parts = [
        f"{len(data.fires)} fired of {len(data.expected_fire_times)} expected",
        f"worst {worst:.1f} ms",
    ]
    if data.missed:
        parts.append(f"{data.missed} logged missed")
    if missing:
        parts.append(f"not fired: {', '.join(missing[:5])}")
    if late:
        parts.append("; ".join(late[:5]))
    return Criterion(
        "scheduled_scenes_on_time",
        "fail",
        ok,
        {"fired": len(data.fires), "expected": len(data.expected_fire_times), "worst_ms": worst},
        th.scene_latency_ms,
        "; ".join(parts),
    )


def ssd_writes(data: SoakData, th: Thresholds) -> Criterion:
    elapsed = data.samples[-1].t - data.samples[0].t if len(data.samples) > 1 else 0.0
    device = _first_last(s.device_write_bytes for s in data.samples)
    process = _first_last(s.io_write_bytes for s in data.samples)
    source, pair = ("partition", device) if device is not None else ("process", process)
    if pair is None or elapsed <= 0:
        return Criterion(
            "ssd_writes", "fail", False, None, th.ssd_bytes_per_day, "no write counter was read"
        )
    per_day = (pair[1] - pair[0]) * DAY_S / elapsed
    detail = f"{_mb(per_day)} per 24 h from the {source} counter"
    if source == "partition" and process is not None:
        own = (process[1] - process[0]) * DAY_S / elapsed
        detail += f" (the application's own share: {_mb(own)})"
    if per_day > th.ssd_idle_bytes_per_day:
        detail += f"; above the idle target of {_mb(th.ssd_idle_bytes_per_day)}, as load allows"
    return Criterion(
        "ssd_writes",
        "fail",
        per_day <= th.ssd_bytes_per_day,
        round(per_day),
        th.ssd_bytes_per_day,
        detail,
    )


def task_count_stable(data: SoakData, th: Thresholds) -> Criterion:
    counts = [s.tasks for s in data.samples if s.tasks is not None]
    if not counts:
        return Criterion("task_count_stable", "fail", False, None, th.task_growth, "never read")
    growth, excursion = counts[-1] - counts[0], max(counts) - counts[0]
    ok = growth <= th.task_growth and excursion <= th.task_excursion
    return Criterion(
        "task_count_stable",
        "fail",
        ok,
        {"start": counts[0], "end": counts[-1], "max": max(counts)},
        {"growth": th.task_growth, "excursion": th.task_excursion},
        f"tasks {counts[0]} -> {counts[-1]} (max {max(counts)})",
    )


def no_forced_restart(data: SoakData, th: Thresholds) -> Criterion:
    identities = {(s.pid, s.starttime_ticks) for s in data.samples if s.pid is not None}
    boots = {s.boot_id for s in data.samples if s.boot_id is not None}
    restarts = _first_last(s.systemd_restarts for s in data.samples)
    problems = []
    if len(identities) > 1:
        problems.append(f"{len(identities)} different processes")
    if len(boots) > 1:
        problems.append(f"{len(boots)} different boots")
    if restarts is not None and restarts[1] != restarts[0]:
        problems.append(f"systemd restarted the service {restarts[1] - restarts[0]} time(s)")
    return Criterion(
        "no_forced_restart",
        "fail",
        not problems and bool(identities),
        len(identities),
        1,
        "; ".join(problems) if problems else "one process, one boot, throughout",
    )


def no_untraceable_red(data: SoakData, th: Thresholds) -> Criterion:
    untraced = [
        c
        for c in data.status_changes
        if c.status == "error" and not _in_window(c.t, c.device, data.injections, th.red_grace_s)
    ]
    return Criterion(
        "no_untraceable_red",
        "fail",
        not untraced,
        len(untraced),
        0,
        "; ".join(f"{c.device} red at t={c.t:.0f}s" for c in untraced[:10])
        or "every red was inside an injected failure",
    )


def load_exercised(data: SoakData, th: Thresholds) -> Criterion:
    problems = [
        f"{kind}: {data.executed.get(kind, 0)} of {planned} ran"
        for kind, planned in data.planned.items()
        if data.executed.get(kind, 0) < planned
    ]
    for cycle in data.cycles:
        if not cycle.passed:
            failed = [name for name, ok in cycle.checks.items() if not ok] or ["no checks"]
            problems.append(f"{cycle.kind} at t={cycle.t:.0f}s: {', '.join(failed)}")
    return Criterion(
        "load_exercised",
        "fail",
        not problems,
        dict(data.executed),
        dict(data.planned),
        "; ".join(problems[:10]) if problems else "every planned load ran and passed its checks",
    )


def _every_sample_below(
    name: str, values: Sequence[float | None], limit: float, unit: str
) -> Criterion:
    known = [v for v in values if v is not None]
    worst = max(known) if known else None
    return Criterion(
        name,
        "warn",
        worst is not None and worst < limit,
        worst,
        limit,
        "never read" if worst is None else f"worst {worst:g} {unit} against {limit:g} {unit}",
    )


def resources(data: SoakData, th: Thresholds) -> list[Criterion]:
    """§23.3: monitored, not hard limits."""
    s = data.samples
    rows = [
        _every_sample_below("rss_under_23_3", [x.rss_bytes for x in s], th.rss_bytes, "bytes"),
        _every_sample_below("fds_under_23_3", [x.fds for x in s], th.fds, "descriptors"),
        _every_sample_below("tasks_under_23_3", [x.tasks for x in s], th.tasks, "tasks"),
        _every_sample_below(
            "loop_lag_p99_under_amber", [x.loop_lag_p99_ms for x in s], th.loop_lag_p99_ms, "ms"
        ),
    ]
    counts = [x.websockets for x in s if x.websockets is not None]
    expected = data.expected_websockets
    steady = bool(counts) and len(set(counts)) == 1 and (expected is None or counts[0] == expected)
    rows.append(
        Criterion(
            "websockets_steady",
            "warn",
            steady,
            counts,
            expected,
            f"WebSocket connections at each sample: {counts}",
        )
    )
    fds = [x.fds for x in s if x.fds is not None]
    rows.append(
        Criterion(
            "fds_steady",
            "warn",
            bool(fds) and fds[-1] - fds[0] <= th.fd_growth,
            fds,
            th.fd_growth,
            f"descriptors at each sample: {fds}",
        )
    )
    return rows


def score(data: SoakData, thresholds: Thresholds | None = None) -> Report:
    th = thresholds or Thresholds()
    criteria = [
        memory_growth(data, th),
        unhandled_exceptions(data, th),
        device_connection_decay(data, th),
        scheduled_scenes_on_time(data, th),
        ssd_writes(data, th),
        task_count_stable(data, th),
        no_forced_restart(data, th),
        no_untraceable_red(data, th),
        load_exercised(data, th),
        *resources(data, th),
    ]
    failed = any(not c.passed and c.severity == "fail" for c in criteria)
    real_run = data.compression == 1 and data.duration_s >= 72 * 3600 - 60
    note = (
        "A full-length, real-time run: the figures are the §22.7 result."
        if real_run
        else (
            f"Compressed x{data.compression:g} over {data.duration_s / 3600:.2f} h: the load "
            "is denser than a real day and the run is shorter than 72 h, so memory growth, "
            "task stability and SSD writes per 24 h are indicative only."
        )
    )
    return Report(
        verdict="FAIL" if failed else "PASS",
        indicative=not real_run,
        note=note,
        criteria=tuple(criteria),
        samples=tuple(data.samples),
        thresholds=th,
    )


# -- the readable report --------------------------------------------------------------


def _cell(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def render_text(report: Report, *, title: str = "Soak test report (§22.7)") -> str:
    lines = [title, "=" * len(title), "", f"Verdict: {report.verdict}", report.note, ""]
    lines.append("Criteria")
    lines.append("--------")
    for c in report.criteria:
        mark = "PASS" if c.passed else ("FAIL" if c.severity == "fail" else "WARN")
        lines.append(f"[{mark}] {c.name} ({c.severity}): {c.detail}")
    lines += ["", "Samples (start, every 12 h compressed, end)", "-" * 43]
    header = (
        "t (s)",
        "RSS MB",
        "FDs",
        "threads",
        "tasks",
        "WS",
        "lag p99 ms",
        "io write MB",
        "dev write MB",
        "DB MB",
    )
    lines.append(" | ".join(header))
    for s in report.samples:
        row = (
            f"{s.t:.0f}",
            _cell(None if s.rss_bytes is None else s.rss_bytes / MB),
            _cell(s.fds),
            _cell(s.threads),
            _cell(s.tasks),
            _cell(s.websockets),
            _cell(s.loop_lag_p99_ms),
            _cell(None if s.io_write_bytes is None else s.io_write_bytes / MB),
            _cell(None if s.device_write_bytes is None else s.device_write_bytes / MB),
            _cell(None if s.db_bytes is None else s.db_bytes / MB),
        )
        lines.append(" | ".join(row))
    lines.append("")
    devices = sorted({key for s in report.samples for key in s.devices})
    if devices:
        lines.append("Devices at each sample: " + ", ".join(devices))
        for s in report.samples:
            states = ", ".join(
                f"{key}={s.devices[key].status}/{s.devices[key].reconnects}"
                for key in devices
                if key in s.devices
            )
            lines.append(f"  t={s.t:.0f}s: {states}")
    return "\n".join(lines) + "\n"
