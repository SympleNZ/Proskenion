"""Running the soak: the load, the measurements and the record (§22.7).

One process holds the stubs, the admin session, two WebSockets and the
timetable (:mod:`tests.soak.plan`). It runs until the plan ends or it is
told to stop (SIGTERM or Ctrl+C), and either way it finishes by collecting
the logs and writing the report, so a soak stopped early still says what it
saw — marked as stopped early.

The results directory
---------------------
Everything is written as it happens, one JSON object per line, so a crash
of the harness loses nothing already measured:

``meta.json``      the plan, the options, the start time, the room's ids
``events.jsonl``   every load run, every device-status change the
                   ``devices`` socket reported, every injected-failure
                   window, every WebSocket reconnect
``samples.jsonl``  the §22.7 measurements, one line per sample
``collected.json`` written at the end: the scheduled fires from the rule and
                   scene logs, tracebacks from the application log, each
                   cycle's checks, and what ran against what was planned
``report.json``    the scored result (:mod:`tests.soak.scoring`)
``report.txt``     the same, readable

``python -m tests.soak score <dir>`` re-scores a directory without running
anything, so a threshold can be revisited after the fact.

Time
----
``t`` everywhere is seconds since the soak's start on the harness's
monotonic clock. The wall clock is only used to talk to the application's
logs, which are in Pacific/Auckland with an offset (§4.9).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import signal
import sys
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from proskenion.config import load_config
from proskenion.core.drivers.cq20b import MSG_OFFLINE, MSG_REFUSED
from tests.soak import procfs, provision, rig, scoring
from tests.soak.api import API, AppClient, LiveSocket, SoakError
from tests.soak.plan import (
    DESK_FPS,
    FADER_DRAG_S,
    FADER_EVERY_S,
    FADER_HZ,
    KNX_RATE_HZ,
    Event,
    SoakPlan,
    build_plan,
    scheduled_times,
)

log = logging.getLogger("soak")

#: The state key the one mixer reports under (§5.6).
MIXER_KEY = "mixer"
#: The harness holds two sockets for the whole run: the watcher and the fader.
WEBSOCKETS = 2
#: How long the harness lets the room settle after provisioning, before t = 0.
SETTLE_S = 30.0


def now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="milliseconds")


def _utc_key(stamp: str) -> str:
    """One spelling for an instant, whatever offset it was written with."""
    return datetime.fromisoformat(stamp).astimezone(UTC).isoformat(timespec="seconds")


@dataclass
class Options:
    base_url: str
    password: str
    app_config: Path
    results: Path
    compression: float = 1.0
    duration_s: float | None = None
    systemd_unit: str | None = None
    settle_s: float = SETTLE_S


@dataclass
class Recorder:
    """Appends to the results directory as things happen."""

    root: Path
    t0: float = field(default_factory=time.monotonic)
    #: False while the room is being commissioned: what happens then is setup,
    #: not soak (the soak instance may well start before its stubs do).
    started: bool = False

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def start(self) -> None:
        self.t0 = time.monotonic()
        self.started = True

    def t(self) -> float:
        return round(time.monotonic() - self.t0, 3)

    def _append(self, name: str, row: Mapping[str, Any]) -> None:
        with (self.root / name).open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row, default=str) + "\n")

    def event(self, kind: str, **detail: Any) -> None:
        row = {"t": self.t(), "at": now_iso(), "kind": kind, **detail}
        if not self.started:
            row["phase"] = "setup"
        self._append("events.jsonl", row)

    def sample(self, row: Mapping[str, Any]) -> None:
        self._append("samples.jsonl", row)

    def write(self, name: str, data: Any) -> None:
        text = json.dumps(data, indent=2, default=str) + "\n"
        (self.root / name).write_text(text, encoding="utf-8", newline="\n")


class Soak:
    """One run. See the module docstring."""

    def __init__(self, options: Options, plan: SoakPlan) -> None:
        self.options = options
        self.plan = plan
        config = load_config(options.app_config)
        self.database = Path(config.database.path)
        self.app_log = Path(config.logging.path) / "application.log"
        self.rec = Recorder(options.results)
        self.client = AppClient(options.base_url, options.password)
        self.stubs = rig.SoakStubs()
        self.room: provision.Room | None = None
        self.pid: int | None = None
        self.started_wall: datetime | None = None
        self.executed: dict[str, int] = {}
        self.cycles: list[scoring.CycleCheck] = []
        self.injections: list[scoring.Injection] = []
        self.statuses: dict[str, str] = {}
        self.stopped_early = False
        self._loads: set[asyncio.Task[None]] = set()
        self._fader_tokens = 0

    # -- the sockets ------------------------------------------------------------------

    def _on_socket_event(self, kind: str, detail: Mapping[str, Any]) -> None:
        self.rec.event(kind, **detail)

    def _on_watch_frame(self, frame: Mapping[str, Any]) -> None:
        if frame.get("type") != "device_status":
            return
        device, status = str(frame.get("device")), str(frame.get("status"))
        if self.statuses.get(device) != status:
            self.statuses[device] = status
            self.rec.event("device_status", device=device, status=status)

    # -- measurements -------------------------------------------------------------------

    async def _systemd_restarts(self) -> int | None:
        unit = self.options.systemd_unit
        if unit is None:
            return None
        try:
            process = await asyncio.create_subprocess_exec(
                "systemctl",
                "show",
                "-p",
                "NRestarts",
                "--value",
                unit,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await process.communicate()
            return int(out.decode().strip())
        except (OSError, ValueError):
            return None

    async def sample(self, label: str) -> None:
        diagnostics = await self.client.get("/system/diagnostics")
        self.pid = int(diagnostics["pid"])
        # Device rows from GET /devices, which reads the device manager live;
        # /system/health serves a snapshot up to 30 s old (§21.24), so it is
        # used only for the subsystems that have no row: KNX (B42).
        listed = await self.client.get("/devices")
        health = await self.client.get("/system/health")
        reading = await asyncio.to_thread(procfs.read_process, self.pid, database=self.database)

        def row(record: Mapping[str, Any]) -> dict[str, Any]:
            return {
                "status": record.get("status"),
                "reconnects": record.get("reconnects"),
                "latency_ms": record.get("latency_ms"),
                "detail": record.get("detail"),
            }

        devices = {
            d["state_key"]: row(d["status"] or {"status": "unknown"}) for d in listed["devices"]
        }
        devices.update({h["key"]: row(h) for h in health["devices"] if h["key"] not in devices})
        # A subsystem the (up to 30 s old) health snapshot does not list yet,
        # as the live devices socket last reported it.
        for key, status in self.statuses.items():
            devices.setdefault(key, {"status": status, "reconnects": 0, "latency_ms": None})
        self.rec.sample(
            {
                "t": self.rec.t(),
                "at": now_iso(),
                "label": label,
                "process": reading.as_dict(),
                "diagnostics": diagnostics,
                "devices": devices,
                "systemd_restarts": await self._systemd_restarts(),
                "stubs": self.stubs.counts.as_dict(),
            }
        )
        self._count("sample")

    def _count(self, kind: str) -> None:
        self.executed[kind] = self.executed.get(kind, 0) + 1

    # -- helpers -------------------------------------------------------------------------

    async def _device(self, device_id: int) -> dict[str, Any]:
        row: dict[str, Any] = await self.client.get(f"/devices/{device_id}")
        return row.get("status") or {}

    async def _until(self, probe: Any, timeout_s: float, every_s: float = 0.5) -> Any:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            try:
                found = await probe()
            except SoakError:
                found = None
            if found:
                return found
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(every_s)

    # -- the loads ------------------------------------------------------------------------

    async def knx_burst(self, event: Event) -> None:
        """Telegrams at 5/s from a wall panel's address, alternating on and off."""
        expected = int(event.duration_s * KNX_RATE_HZ)
        sent = 0
        start = time.monotonic()
        for n in range(expected):
            await asyncio.sleep(max(0.0, start + n / KNX_RATE_HZ - time.monotonic()))
            await self.stubs.knx_telegram(provision.BANK_COMMAND, n % 2 == 0, provision.PANEL)
            sent += 1
        clients = self.stubs.knxd.connected_client_count()
        self.rec.event("knx_burst", sent=sent, expected=expected, knxd_clients=clients)
        self._count("knx_burst")

    async def _external_active(self) -> bool:
        states = await self.client.get("/rules/state")
        return bool(states["external_control"])

    async def external_control(self, event: Event) -> None:
        """On: desk frames from the node. Five minutes (compressed). Off: silence (§7.2.7)."""
        t = self.rec.t()
        frames = 0
        engaged = False
        start = time.monotonic()
        level = bytearray(512)
        while time.monotonic() - start < event.duration_s:
            level[0:8] = bytes([(frames * 3) % 256] * 8)
            self.stubs.desk_frame(provision.INPUT_UNIVERSE, bytes(level), frames % 255 + 1)
            frames += 1
            if not engaged and time.monotonic() - start > 2.0:
                engaged = await self._external_active()
            await asyncio.sleep(max(0.0, start + frames / DESK_FPS - time.monotonic()))

        async def released() -> bool:
            return not await self._external_active()

        # §7.2.7 stands down after 5 s of silence.
        freed = bool(await self._until(released, 20.0))
        checks = {"engaged": engaged, "released": freed}
        self.cycles.append(scoring.CycleCheck("external_control", t, checks))
        self.rec.event("external_control", frames=frames, **checks)
        self._count("external_control")

    async def _mixer_input_db(self, channel_id: int) -> float | None:
        state = await self.client.get("/mixer/state")
        for row in state.get("inputs") or []:
            if row.get("channel_id") == channel_id:
                value = row.get("db")
                return None if value is None else float(value)
        return None

    async def mixer_cycle(self, event: Event) -> None:
        """Kill the desk, see refused (amber), then timed out (red), restore, resync (§7.3)."""
        assert self.room is not None
        mixer = self.room.mixer
        start_t = self.rec.t()
        checks: dict[str, bool] = {}
        self.rec.event("injection_start", device=MIXER_KEY, what="mixer cycle")
        try:
            await self.stubs.kill_mixer()

            async def refused() -> bool:
                status = await self._device(mixer)
                return status.get("status") == "degraded" and status.get("detail") == MSG_REFUSED

            checks["refused_is_amber"] = bool(await self._until(refused, 60.0))
            # Someone at MixPad moves input 2 while the controller is away.
            target_db = -6.0 if len(self.cycles) % 2 == 0 else -18.0
            self.stubs.change_desk_level(rig.LEVEL_IP2, target_db)

            checks["blackhole_holds"] = await asyncio.to_thread(self.stubs.blackhole_mixer)

            async def timed_out() -> bool:
                status = await self._device(mixer)
                return status.get("status") == "error" and status.get("detail") == MSG_OFFLINE

            checks["timed_out_is_red"] = bool(await self._until(timed_out, 180.0))
        finally:
            await self.stubs.restore_mixer()

        async def connected() -> bool:
            return (await self._device(mixer)).get("status") == "connected"

        checks["reconnected"] = bool(await self._until(connected, 360.0))

        async def resynced() -> bool:
            value = await self._mixer_input_db(self.room.mixer_input_2)  # type: ignore[union-attr]
            return value is not None and math.isclose(value, target_db, abs_tol=0.6)

        checks["state_resynced"] = bool(await self._until(resynced, 60.0))
        end_t = self.rec.t()
        self.injections.append(scoring.Injection(MIXER_KEY, start_t, end_t, "mixer cycle"))
        self.rec.event("injection_end", device=MIXER_KEY, what="mixer cycle", start_t=start_t)
        self.cycles.append(scoring.CycleCheck("mixer_cycle", start_t, checks))
        self.rec.event("mixer_cycle", **checks)
        self._count("mixer_cycle")

    async def faders(self, socket: LiveSocket) -> None:
        """An operator's drag every minute: a lighting channel, then a mixer channel."""
        assert self.room is not None
        drag = 0
        while True:
            await asyncio.sleep(FADER_EVERY_S)
            if not socket.connected.is_set():
                continue
            lighting = drag % 2 == 0
            steps = int(FADER_DRAG_S * FADER_HZ)
            last: Mapping[str, Any] | None = None
            try:
                for step in range(steps):
                    phase = step / max(1, steps - 1)
                    if lighting:
                        value: float = round(100.0 * phase, 1)
                        target = self.room.fixtures[0]
                        domain = "lighting"
                    else:
                        value = round(-40.0 + 35.0 * phase, 1)
                        target = self.room.mixer_input_1
                        domain = "mixer"
                    last = await socket.set(domain, target, value, wait=step == steps - 1)
                    await asyncio.sleep(1.0 / FADER_HZ)
            except (SoakError, TimeoutError) as exc:
                self.rec.event("fader", domain="lighting" if lighting else "mixer", error=repr(exc))
            else:
                self.rec.event(
                    "fader",
                    domain="lighting" if lighting else "mixer",
                    frames=steps,
                    answer=None if last is None else last.get("type"),
                    reason=None if last is None else last.get("reason"),
                )
            self._count("fader_drag")
            drag += 1

    async def keep_session(self) -> None:
        while True:
            await asyncio.sleep(300.0)
            with contextlib.suppress(Exception):
                await self.client.keep_alive()

    # -- the run -------------------------------------------------------------------------

    def _spawn(self, coroutine: Any, name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._loads.add(task)

        def done(finished: asyncio.Task[None]) -> None:
            self._loads.discard(finished)
            if not finished.cancelled() and finished.exception() is not None:
                self.rec.event("load_error", load=name, error=repr(finished.exception()))

        task.add_done_callback(done)

    async def timetable(self) -> None:
        for event in self.plan.events:
            delay = self.rec.t0 + event.at_s - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            match event.kind:
                case "sample":
                    try:
                        await self.sample(f"t={event.at_s:.0f}s")
                    except Exception as exc:
                        self.rec.event("sample_error", error=repr(exc))
                case "knx_burst":
                    self._spawn(self.knx_burst(event), "knx_burst")
                case "external_control":
                    self._spawn(self.external_control(event), "external_control")
                case "mixer_cycle":
                    self._spawn(self.mixer_cycle(event), "mixer_cycle")

    async def wait_for_app(self, timeout_s: float = 180.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while not await self.client.healthy():
            if loop.time() >= deadline:
                raise SoakError(f"the application did not answer /health at {self.client.base_url}")
            await asyncio.sleep(1.0)

    async def run(self) -> scoring.Report:
        background: list[asyncio.Task[Any]] = []
        async with self.stubs:
            background.append(asyncio.create_task(self.stubs.trim_forever(), name="trim"))
            try:
                await self.wait_for_app()
                self.room = await provision.provision(self.client, self.options.password, self.plan)
                watcher = LiveSocket(
                    self.client,
                    "watcher",
                    ["devices", "system"],
                    on_frame=self._on_watch_frame,
                    on_event=self._on_socket_event,
                )
                fader = LiveSocket(
                    self.client, "fader", ["lighting", "mixer"], on_event=self._on_socket_event
                )
                background.append(asyncio.create_task(watcher.run(), name="watcher"))
                background.append(asyncio.create_task(fader.run(), name="fader-socket"))
                await asyncio.wait_for(watcher.connected.wait(), 30.0)
                await asyncio.wait_for(fader.connected.wait(), 30.0)
                await asyncio.sleep(self.options.settle_s)

                self.rec.start()
                self.started_wall = datetime.now(UTC)
                self.rec.write(
                    "meta.json",
                    {
                        "started_at": self.started_wall.astimezone().isoformat(),
                        "plan": self.plan.as_dict(),
                        "options": {
                            "base_url": self.options.base_url,
                            "app_config": str(self.options.app_config),
                            "systemd_unit": self.options.systemd_unit,
                        },
                        "database": str(self.database),
                        "application_log": str(self.app_log),
                        "room": self.room.as_dict(),
                        "stubs": {
                            "knxd": [rig.HOST, rig.KNXD_PORT],
                            "node": [rig.NODE_HOST, rig.NODE_PORT],
                            "pjlink": [rig.HOST, rig.PJLINK_PORT],
                            "cq": [rig.HOST, rig.CQ_MIDI_PORT, rig.CQ_NATIVE_PORT],
                        },
                    },
                )
                self.rec.event("soak_started")
                background.append(asyncio.create_task(self.faders(fader), name="faders"))
                background.append(asyncio.create_task(self.keep_session(), name="session"))
                await self.timetable()
                if self._loads:
                    await asyncio.wait(set(self._loads), timeout=900.0)
            except asyncio.CancelledError:
                self.stopped_early = True
                self.rec.event("stopped_early")
            finally:
                for task in [*background, *self._loads]:
                    task.cancel()
                await asyncio.gather(*background, *self._loads, return_exceptions=True)
                # An injected failure must never outlive the run.
                with contextlib.suppress(Exception):
                    await self.stubs.restore_mixer()
            report = await self.finish()
        await self.client.aclose()
        return report

    # -- the end ---------------------------------------------------------------------------

    async def _fires(self, since: str) -> tuple[list[dict[str, Any]], int]:
        assert self.room is not None
        rows: list[dict[str, Any]] = []
        for rule_id in (self.room.rule_a, self.room.rule_b):
            log_ = await self.client.get(
                "/rules/log", params={"rule_id": rule_id, "since": since, "limit": 1000}
            )
            rows += [e for e in log_["entries"] if e.get("triggered_by") == "schedule"]
        scenes: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = await self.client.get(
                "/scenes/log", params={"from": since, "limit": 1000, "offset": offset}
            )
            scenes += page["entries"]
            if len(page["entries"]) < 1000:
                break
            offset += 1000
        scene_starts = sorted(
            datetime.fromisoformat(s["started_at"])
            for s in scenes
            if s.get("triggered_by") == "schedule"
        )
        fires: list[dict[str, Any]] = []
        missed = 0
        for row in rows:
            detail = row.get("detail") or {}
            if row.get("result") == "missed":
                missed += int(detail.get("missed", 1))
                continue
            scheduled = detail.get("scheduled_for")
            if not scheduled:
                continue
            when = datetime.fromisoformat(scheduled)
            start = next(
                (
                    s
                    for s in scene_starts
                    if when - timedelta(seconds=1) <= s <= when + timedelta(seconds=5)
                ),
                None,
            )
            fires.append(
                {
                    "scheduled_for": scheduled,
                    "result": row.get("result"),
                    "dispatch_latency_ms": detail.get("dispatch_latency_ms"),
                    "scene_started_at": None if start is None else start.isoformat(),
                    "scene_start_latency_ms": None
                    if start is None
                    else round((start - when).total_seconds() * 1000, 1),
                }
            )
        return fires, missed

    def _tracebacks(self, since: datetime) -> tuple[list[dict[str, Any]], int]:
        """Lines with a traceback, or at CRITICAL, logged after ``since``. And those before."""
        found: list[dict[str, Any]] = []
        before = 0
        try:
            handle = self.app_log.open(encoding="utf-8", errors="replace")
        except OSError:
            return [{"message": f"could not read {self.app_log}", "logger": "soak"}], 0
        with handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not ("exception" in record or record.get("level") == "CRITICAL"):
                    continue
                try:
                    stamp = datetime.fromisoformat(record["timestamp"])
                except (KeyError, ValueError):
                    stamp = None
                if stamp is not None and stamp < since:
                    before += 1
                    continue
                found.append(
                    {
                        "timestamp": record.get("timestamp"),
                        "level": record.get("level"),
                        "logger": record.get("logger"),
                        "message": record.get("message"),
                        "exception": str(record.get("exception", ""))[-2000:],
                    }
                )
        return found, before

    async def finish(self) -> scoring.Report:
        collected: dict[str, Any] = {"stopped_early": self.stopped_early}
        if self.started_wall is not None and self.room is not None:
            since = self.started_wall.astimezone().isoformat()
            try:
                fires, missed = await self._fires(since)
            except Exception as exc:
                fires, missed = [], 0
                collected["fires_error"] = repr(exc)
            end = datetime.now(UTC) - timedelta(seconds=5)
            expected = [
                m.astimezone().isoformat()
                for m in scheduled_times(self.plan.scene_period_min, self.started_wall, end)
            ]
            try:
                tracebacks, before = await asyncio.to_thread(self._tracebacks, self.started_wall)
            except Exception as exc:  # the report must still be written
                tracebacks, before = [{"message": f"log scan failed: {exc!r}"}], 0
            planned = {
                kind: sum(1 for e in self.plan.events if e.kind == kind and e.at_s <= self.rec.t())
                for kind in ("sample", "knx_burst", "external_control", "mixer_cycle")
            }
            collected.update(
                {
                    "fires": fires,
                    "missed": missed,
                    "expected_fire_times": expected,
                    "exceptions": tracebacks,
                    "exceptions_before_start": before,
                    "cycles": [
                        {"kind": c.kind, "t": c.t, "checks": dict(c.checks)} for c in self.cycles
                    ],
                    "injections": [asdict(i) for i in self.injections],
                    "executed": self.executed,
                    "planned": planned,
                    "stubs": self.stubs.counts.as_dict(),
                    "duration_s": self.rec.t(),
                    "compression": self.plan.compression,
                }
            )
        self.rec.write("collected.json", collected)
        return score_directory(self.options.results)


# -- re-scoring a results directory ---------------------------------------------------


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        with contextlib.suppress(ValueError):
            rows.append(json.loads(line))
    return rows


def sample_from_row(row: Mapping[str, Any]) -> scoring.Sample:
    process = row.get("process") or {}
    diagnostics = row.get("diagnostics") or {}
    devices = {
        key: scoring.DeviceReading(
            status=str(value.get("status")),
            reconnects=int(value.get("reconnects") or 0),
            latency_ms=value.get("latency_ms"),
        )
        for key, value in (row.get("devices") or {}).items()
    }
    return scoring.Sample(
        t=float(row["t"]),
        at=str(row.get("at")),
        pid=process.get("pid"),
        starttime_ticks=process.get("starttime_ticks"),
        boot_id=process.get("boot_id"),
        rss_bytes=process.get("rss_bytes"),
        fds=process.get("fds"),
        threads=process.get("threads"),
        io_write_bytes=process.get("io_write_bytes"),
        device_write_bytes=process.get("device_write_bytes"),
        db_bytes=process.get("db_bytes"),
        tasks=diagnostics.get("tasks"),
        websockets=diagnostics.get("websocket_connections"),
        loop_lag_p50_ms=diagnostics.get("loop_lag_p50_ms"),
        loop_lag_p99_ms=diagnostics.get("loop_lag_p99_ms"),
        devices=devices,
        systemd_restarts=row.get("systemd_restarts"),
    )


def load_data(results: Path) -> scoring.SoakData:
    collected_path = results / "collected.json"
    collected: dict[str, Any] = (
        json.loads(collected_path.read_text(encoding="utf-8")) if collected_path.exists() else {}
    )
    events = _jsonl(results / "events.jsonl")
    samples = [sample_from_row(r) for r in _jsonl(results / "samples.jsonl")]
    changes = [
        scoring.StatusChange(float(e["t"]), str(e["device"]), str(e["status"]))
        for e in events
        if e.get("kind") == "device_status" and e.get("phase") != "setup"
    ]
    # Windows from the event log, so a window still open when the harness died counts too.
    injections = [scoring.Injection(**i) for i in collected.get("injections", [])]
    if not injections:
        open_: dict[str, float] = {}
        for e in events:
            if e.get("kind") == "injection_start":
                open_[str(e["device"])] = float(e["t"])
            elif e.get("kind") == "injection_end":
                device = str(e["device"])
                injections.append(
                    scoring.Injection(device, open_.pop(device, float(e["t"])), float(e["t"]), "")
                )
        injections += [scoring.Injection(d, s, math.inf, "unfinished") for d, s in open_.items()]
    fires = [
        scoring.ScheduledFire(
            scheduled_for=_utc_key(f["scheduled_for"]),
            result=str(f.get("result")),
            dispatch_latency_ms=f.get("dispatch_latency_ms"),
            scene_start_latency_ms=f.get("scene_start_latency_ms"),
        )
        for f in collected.get("fires", [])
    ]
    return scoring.SoakData(
        compression=float(collected.get("compression", 1.0)),
        duration_s=float(collected.get("duration_s", samples[-1].t if samples else 0.0)),
        planned=collected.get("planned", {}),
        executed=collected.get("executed", {}),
        samples=samples,
        status_changes=changes,
        injections=injections,
        fires=fires,
        expected_fire_times=[_utc_key(t) for t in collected.get("expected_fire_times", [])],
        missed=int(collected.get("missed", 0)),
        exceptions=collected.get("exceptions", []),
        cycles=[
            scoring.CycleCheck(c["kind"], float(c["t"]), c["checks"])
            for c in collected.get("cycles", [])
        ],
        expected_websockets=WEBSOCKETS,
    )


def score_directory(results: Path) -> scoring.Report:
    data = load_data(results)
    report = scoring.score(data)
    body = report.as_dict()
    collected_path = results / "collected.json"
    collected = (
        json.loads(collected_path.read_text(encoding="utf-8")) if collected_path.exists() else {}
    )
    body["stopped_early"] = bool(collected.get("stopped_early"))
    body["stubs"] = collected.get("stubs")
    body["exceptions_before_start"] = collected.get("exceptions_before_start")
    (results / "report.json").write_text(
        json.dumps(body, indent=2, default=str) + "\n", encoding="utf-8", newline="\n"
    )
    title = "Soak test report (§22.7)" + (" — STOPPED EARLY" if body["stopped_early"] else "")
    (results / "report.txt").write_text(
        scoring.render_text(report, title=title), encoding="utf-8", newline="\n"
    )
    return report


async def run_soak(options: Options) -> scoring.Report:
    plan = build_plan(options.compression, options.duration_s)
    soak = Soak(options, plan)
    main = asyncio.current_task()
    loop = asyncio.get_running_loop()
    if main is not None and sys.platform != "win32":
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, main.cancel)
    task = asyncio.create_task(soak.run(), name="soak")
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        task.cancel()
        return await task


__all__ = ["API", "Options", "load_data", "run_soak", "score_directory"]
