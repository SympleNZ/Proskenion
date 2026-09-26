"""A schedule rule end to end: created through the API, fired by the scheduler, logged.

The real rules engine and the real scene engine, one recording action
handler at the end of the chain, and the scheduler's clock injected so the
test decides when 19:00 comes (spec §8.3, §8.10, §8.16, §22.7).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime

from proskenion.api.app import API_PREFIX, create_app
from proskenion.config import Config
from proskenion.core.auth import TokenService
from proskenion.core.bus import EventBus
from proskenion.core.drivers.capabilities import ProjectorCapabilities
from proskenion.core.ratelimit import RateLimiter
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import scenes as scenes_crud
from proskenion.db.crud.base import AUCKLAND
from proskenion.rules.engine import RulesEngine
from proskenion.scene.domains import ActionOutcome
from proskenion.scene.engine import SceneEngine
from tests.unit.api.conftest import ADMIN_PASSWORD, make_client
from tests.unit.rules.conftest import ScheduleTestClock
from tests.unit.scene.conftest import FakeCapabilities, RecordingHandler

RULES = f"{API_PREFIX}/rules"
SCENES = f"{API_PREFIX}/scenes"


async def until(predicate: Callable[[], bool], within: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


async def test_a_schedule_rule_created_through_the_api_runs_its_scene_and_logs_the_times(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    bus = EventBus()
    await bus.start()
    state = StateStore(config, bus)
    devices = FakeCapabilities()
    scene_engine = SceneEngine(db, state, devices=devices)
    handler = RecordingHandler(outcome=ActionOutcome.confirmed({"state": "on"}))
    scene_engine.handlers.register("projector_power", handler)
    projector = await devices_crud.create(
        db, category="projector", driver_key="pjlink", name="Projector", config={}
    )
    devices.reports[projector.id] = ProjectorCapabilities(
        inputs=("hdmi1",), supports_authentication=False
    )
    scene = await scenes_crud.create_scene(db, name="Evening projector")
    await scenes_crud.create_action(
        db, scene_id=scene.id, sort_order=0, domain="projector_power", projector_power="on"
    )

    schedule = ScheduleTestClock(datetime.fromisoformat("2026-09-24T18:58:00+12:00"))
    rules_engine = RulesEngine(
        db,
        state,
        bus,
        scenes=scene_engine,
        schedule_clock=schedule,
        wall_clock=lambda: schedule.wall().astimezone(AUCKLAND),
    )
    await rules_engine.start()
    await schedule.settled()
    app = create_app(config, db=db, tokens=tokens, limiter=limiter, bus=bus, state=state)
    app.state.rules = rules_engine
    try:
        async with make_client(app) as http:
            login = await http.post(f"{API_PREFIX}/auth/login", json={"password": ADMIN_PASSWORD})
            assert login.status_code == 200, login.text
            naps = len(schedule.naps)
            created = await http.post(
                RULES,
                json={
                    "name": "Projector on at seven",
                    "trigger_type": "schedule",
                    "cron": "0 19 * * MON-FRI",
                    "action_type": "run_scene",
                    "scene_id": scene.id,
                },
            )
            assert created.status_code == 201, created.text
            rule = created.json()
            assert rule["next_fire_at"] == "2026-09-24T19:00:00+12:00"  # a Thursday
            await schedule.settled(naps)  # the new rule woke the scheduler

            await schedule.run_until("2026-09-24T18:59:59+12:00")
            assert handler.calls == []
            await schedule.run_until("2026-09-24T19:00:00+12:00")
            await until(lambda: len(handler.calls) == 1)

            async def logged() -> list[dict[str, object]]:
                await rules_engine.flush_log()
                response = await http.get(f"{RULES}/log", params={"rule_id": rule["id"]})
                assert response.status_code == 200
                entries: list[dict[str, object]] = response.json()["entries"]
                return entries

            for _ in range(200):
                if await logged():
                    break
                await asyncio.sleep(0.005)
            [entry] = await logged()
            assert entry["triggered_by"] == "schedule"
            assert entry["result"] == "success"
            detail = entry["detail"]
            assert isinstance(detail, dict)
            assert detail["scheduled_for"] == "2026-09-24T19:00:00.000000+12:00"
            assert detail["dispatched_at"] == "2026-09-24T19:00:00.000000+12:00"
            assert detail["dispatch_latency_ms"] == 0.0

            runs = await http.get(f"{SCENES}/{scene.id}/log")
            assert runs.status_code == 200
            [run] = runs.json()["entries"]
            assert run["triggered_by"] == "schedule"  # §8.16's spelling
            assert run["result"] == "success"

            states = await http.get(f"{RULES}/state")
            [mine] = [s for s in states.json()["rules"] if s["id"] == rule["id"]]
            assert mine["fires_automatically"] is True and mine["note"] is None
            assert mine["next_fire_at"] == "2026-09-25T19:00:00+12:00"
    finally:
        await rules_engine.stop()
        await bus.stop()
