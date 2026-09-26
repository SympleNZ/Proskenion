"""§23.1 row: "Database writes — 100 inserts/sec sustained — Scene execution
log under scripted load."

Creates one transient, action-less scene (admin only, §16.5), triggers it
repeatedly — each trigger writes at least a started row to
``scene_execution_log`` (§8.16) without touching any device: an action-less
scene has nothing to dispatch, and a scene with an action naming an
unconfigured device reports it ``skipped`` rather than reaching for
hardware (``proskenion/scene/domains.py``'s ``resolve_device``) — then
deletes the scene, always, in a ``finally``.

Gated on :attr:`~tools.perf.client.Safety.allow_scene_triggers` rather than
``allow_device_writes``: nothing here moves hardware, but it is still a
scene, created and triggered on the live configuration, and the tool's brief
says that needs its own explicit flag ("no scenes on real devices unless
explicitly flagged"). The scene is named unmistakably and removed in a
``finally``, so a run that is interrupted still tries to clean up before
raising.
"""

from __future__ import annotations

from dataclasses import dataclass

from tools.perf.client import PerfClient, Safety
from tools.perf.report import ScenarioResult
from tools.perf.stats import run_concurrent_workers
from tools.perf.targets import DB_INSERTS

SCENE_NAME = "Proskenion perf harness (transient — safe to delete)"


@dataclass(frozen=True, slots=True)
class Options:
    duration_s: float = 3.0
    warm_up_s: float = 0.3
    concurrency: int = 5


async def run(client: PerfClient, safety: Safety, options: Options | None = None) -> ScenarioResult:
    options = options or Options()
    if not safety.allow_scene_triggers:
        return ScenarioResult.skip(
            DB_INSERTS,
            "dry-run: pass --allow-scene-triggers to create a transient, action-less scene and "
            "trigger it repeatedly (it is deleted afterwards).",
        )
    if client.tier != "admin":
        return ScenarioResult.skip(
            DB_INSERTS,
            f"signed in as {client.tier!r}; creating and deleting a scene needs admin (§16.5).",
        )

    created = await client.http.post(client.api("/scenes"), json={"name": SCENE_NAME})
    if created.status_code != 201:
        return ScenarioResult.skip(
            DB_INSERTS, f"could not create the harness scene: HTTP {created.status_code}"
        )
    scene_id = created.json()["id"]
    try:

        async def trigger(_worker_index: int) -> None:
            response = await client.http.post(client.api(f"/scenes/{scene_id}/trigger"))
            if response.status_code != 202:
                raise RuntimeError(f"trigger: HTTP {response.status_code}")

        measurement, window_s = await run_concurrent_workers(
            concurrency=options.concurrency,
            duration_s=options.duration_s,
            warm_up_s=options.warm_up_s,
            operation=trigger,
        )
        rate = measurement.rate_per_second(window_s)

        logged = await client.http.get(client.api(f"/scenes/{scene_id}/log"), params={"limit": 500})
        logged_count = len(logged.json().get("entries", [])) if logged.status_code == 200 else None

        passed = rate >= (DB_INSERTS.target_value or 0.0)
        return ScenarioResult(
            target=DB_INSERTS,
            achieved=rate,
            p50_ms=measurement.p50_ms,
            p95_ms=measurement.p95_ms,
            max_ms=measurement.max_ms,
            sample_count=measurement.count,
            passed=passed,
            notes=(
                f"{options.concurrency} workers triggering scene {scene_id} ({SCENE_NAME!r}) for "
                f"{options.duration_s:g} s; {measurement.failures} failure(s); "
                f"{logged_count if logged_count is not None else 'an unknown number of'} rows now "
                f"in its execution log. p50/p95/max are the trigger call's own round trip "
                f"(202 Accepted, §16.8), not the log row's write time — the scene engine's own "
                f"run-start lock (§8.13's ``_run_gate``) serialises the actual insert, so this is "
                f"the real production path, not an artificial ceiling."
            ),
            extra={
                "scene_id": scene_id,
                "logged_count": logged_count,
                "failures": measurement.failures,
            },
        )
    finally:
        await client.http.delete(client.api(f"/scenes/{scene_id}"))
