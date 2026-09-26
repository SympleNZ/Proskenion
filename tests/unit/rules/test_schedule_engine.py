"""Scheduled firings through the rules engine: the same path as any trigger, and the log.

The scheduler's own behaviour — planning, re-planning, misses, DST — is
``test_scheduler.py``'s. Here the real engine fires, over the rules rig's
database, with the scheduler's clock injected.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import pytest

from proskenion.config import Config
from proskenion.db.connection import Database
from proskenion.db.crud import rules as rules_crud
from proskenion.db.crud.base import AUCKLAND
from proskenion.rules.engine import RulesEngine
from tests.unit.rules.conftest import (
    FakeScenes,
    Rig,
    ScheduleTestClock,
    add_scene,
    start_rig,
    stop_rig,
)

START = "2026-09-24T18:59:00+12:00"


@pytest.fixture
def clock() -> ScheduleTestClock:
    return ScheduleTestClock(datetime.fromisoformat(START))


@pytest.fixture
async def rig(db: Database, dev_config: Config, clock: ScheduleTestClock) -> AsyncIterator[Rig]:
    running = await start_rig(db, dev_config, schedule_clock=clock)
    await clock.settled()
    try:
        yield running
    finally:
        await stop_rig(running)


async def logged(rig: Rig, rule_id: int) -> list[dict[str, Any]]:
    await rig.engine.flush_log()
    entries = await rules_crud.list_executions(rig.db, rule_id=rule_id, limit=100)
    return [
        {
            "triggered_by": e.triggered_by,
            "guard_result": e.guard_result,
            "result": e.result,
            "detail": json.loads(e.detail or "{}"),
        }
        for e in reversed(entries)
    ]


async def schedule_rule(rig: Rig, clock: ScheduleTestClock, **values: Any) -> int:
    naps = len(clock.naps)
    fields: dict[str, Any] = {
        "name": "Evening house",
        "trigger_type": "schedule",
        "cron": "0 19 * * *",
        "action_type": "run_scene",
    }
    fields.update(values)
    rule = await rules_crud.create_rule(rig.db, **fields)
    # Created "now" on the test's clock, not the machine's, so downtime is
    # counted from a time the test controls.
    async with rig.db.write() as conn:
        await conn.execute("UPDATE rules SET updated_at = ? WHERE id = ?", (START, rule.id))
    await rig.reload()
    await clock.settled(naps)
    return rule.id


async def scenes_settled(rig: Rig) -> None:
    for _ in range(100):
        await asyncio.sleep(0)
    await rig.engine.flush_log()


async def test_a_scheduled_scene_runs_through_the_rule_path_and_logs_both_times(
    rig: Rig, clock: ScheduleTestClock
) -> None:
    scene = await add_scene(rig.db, rig.venue, "Evening house")
    rule_id = await schedule_rule(rig, clock, scene_id=scene)

    await clock.run_until("2026-09-24T18:59:59+12:00")
    assert rig.scenes.calls == []
    await clock.run_until("2026-09-24T19:00:00+12:00")
    await scenes_settled(rig)

    assert [(c.scene_id, c.triggered_by, c.trigger_value) for c in rig.scenes.calls] == [
        (scene, "schedule", None)
    ]
    [entry] = await logged(rig, rule_id)
    assert entry["triggered_by"] == "schedule"
    assert entry["result"] == "success" and entry["guard_result"] is None
    detail = entry["detail"]
    assert detail["action"] == "run_scene" and detail["scene_id"] == scene
    assert detail["scheduled_for"] == "2026-09-24T19:00:00.000000+12:00"
    assert detail["dispatched_at"] == "2026-09-24T19:00:00.000000+12:00"
    assert detail["dispatch_latency_ms"] == 0.0

    state = {s["id"]: s for s in rig.engine.rule_states()}[rule_id]
    assert state["last_result"] == "success"
    assert state["next_fire_at"] == "2026-09-25T19:00:00+12:00"


async def test_the_guard_applies_to_a_scheduled_firing(
    rig: Rig, clock: ScheduleTestClock
) -> None:
    scene = await add_scene(rig.db, rig.venue, "Daytime only")
    rule_id = await schedule_rule(
        rig, clock, scene_id=scene, guard_type="time_window", guard_value="08:00-18:00"
    )
    await clock.run_until("2026-09-24T19:00:00+12:00")
    await scenes_settled(rig)
    assert rig.scenes.calls == []
    [entry] = await logged(rig, rule_id)
    assert (entry["result"], entry["guard_result"]) == ("blocked", "blocked")
    assert entry["detail"]["scheduled_for"] == "2026-09-24T19:00:00.000000+12:00"


async def test_a_disabled_schedule_rule_does_not_fire(rig: Rig, clock: ScheduleTestClock) -> None:
    scene = await add_scene(rig.db, rig.venue, "Off")
    rule_id = await schedule_rule(rig, clock, scene_id=scene, enabled=False)
    await clock.run_until("2026-09-24T19:00:30+12:00")
    await scenes_settled(rig)
    assert rig.scenes.calls == [] and await logged(rig, rule_id) == []
    state = {s["id"]: s for s in rig.engine.rule_states()}[rule_id]
    assert state["fires_automatically"] is False and state["next_fire_at"] is None


async def test_a_missed_time_is_logged_as_missed_and_the_scene_not_run(
    rig: Rig, clock: ScheduleTestClock
) -> None:
    scene = await add_scene(rig.db, rig.venue, "Evening house")
    rule_id = await schedule_rule(rig, clock, scene_id=scene)
    await clock.advance(5.0, step=300.0)  # the clock jumps from 18:59 to 19:04
    await scenes_settled(rig)
    assert rig.scenes.calls == []
    [entry] = await logged(rig, rule_id)
    assert entry["triggered_by"] == "schedule" and entry["result"] == "missed"
    assert entry["detail"]["scheduled_for"] == "2026-09-24T19:00:00.000000+12:00"
    assert entry["detail"]["missed"] == 1


# -- restarts, through the database ---------------------------------------------------------


async def engine_at(rig: Rig, at: str, *, scenes: FakeScenes | None = None) -> RulesEngine:
    """A second controller life over the same database, its clock reading ``at``."""
    clock = ScheduleTestClock(datetime.fromisoformat(at))
    engine = RulesEngine(
        rig.db,
        rig.state,
        rig.bus,
        scenes=scenes or FakeScenes(),
        schedule_clock=clock,
        wall_clock=lambda: clock.wall().astimezone(AUCKLAND),
        scene_drain_s=0.05,
    )
    await engine.start()
    await clock.settled()
    engine.test_clock = clock  # type: ignore[attr-defined]
    return engine


async def test_the_last_firing_survives_a_restart_so_no_minute_fires_twice(
    rig: Rig, clock: ScheduleTestClock
) -> None:
    scene = await add_scene(rig.db, rig.venue, "Evening house")
    rule_id = await schedule_rule(rig, clock, scene_id=scene)
    await clock.run_until("2026-09-24T19:00:00+12:00")
    await scenes_settled(rig)
    await rig.engine.stop()
    assert await rules_crud.last_scheduled(rig.db) == {
        rule_id: "2026-09-24T19:00:00.000000+12:00"
    }

    # Back up with the clock ten seconds behind the minute that fired.
    scenes = FakeScenes()
    second = await engine_at(rig, "2026-09-24T18:59:50+12:00", scenes=scenes)
    await second.test_clock.run_until("2026-09-24T19:01:00+12:00")  # type: ignore[attr-defined]
    await second.flush_log()
    assert scenes.calls == []
    assert second.scheduler.next_fire_at(rule_id) == datetime.fromisoformat(
        "2026-09-25T19:00:00+12:00"
    )
    await second.stop()

    # Down for a day: the evening passed while off is logged, not run.
    third = await engine_at(rig, "2026-09-25T20:00:00+12:00", scenes=scenes)
    await third.flush_log()
    assert scenes.calls == []
    entries = await logged(rig, rule_id)
    assert [e["result"] for e in entries] == ["success", "missed"]
    assert entries[-1]["detail"]["scheduled_for"] == "2026-09-25T19:00:00.000000+12:00"
    assert entries[-1]["detail"]["missed"] == 1
    await third.stop()


async def test_a_scene_cut_off_by_shutdown_is_still_logged_as_fired(
    rig: Rig, clock: ScheduleTestClock
) -> None:
    """Otherwise the next start would count a time that did fire as missed."""
    scene = await add_scene(rig.db, rig.venue, "Long")
    rule_id = await schedule_rule(rig, clock, scene_id=scene)
    await rig.engine.stop()
    scenes = FakeScenes()
    scenes.hold = asyncio.Event()  # never set: the scene outlasts the drain
    first = await engine_at(rig, "2026-09-24T18:59:30+12:00", scenes=scenes)
    await first.test_clock.run_until("2026-09-24T19:00:00+12:00")  # type: ignore[attr-defined]
    await scenes_settled(rig)
    assert len(scenes.calls) == 1
    await first.stop()
    [entry] = await logged(rig, rule_id)
    assert entry["result"] == "failed"
    assert "stopped" in entry["detail"]["reason"]
    assert entry["detail"]["scheduled_for"] == "2026-09-24T19:00:00.000000+12:00"

    second = await engine_at(rig, "2026-09-24T19:00:40+12:00")
    await second.flush_log()
    assert [e["result"] for e in await logged(rig, rule_id)] == ["failed"]  # nothing missed
    await second.stop()
