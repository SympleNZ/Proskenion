"""§22.7 in real time: a scheduled scene starts within 100 ms of its trigger.

Real clocks, a real event loop, the real scheduler and rules engine. Only
the wall clock is offset, by a constant, so that a whole minute — cron's
resolution — falls about a second from now instead of up to a minute away.
The scheduler sleeps on the real monotonic clock and reads the real (offset)
wall clock, exactly as on the appliance.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from proskenion.config import Config
from proskenion.db.connection import Database
from proskenion.db.crud import rules as rules_crud
from proskenion.rules.scheduler import SystemClock
from tests.unit.rules.conftest import Rig, add_scene, start_rig, stop_rig

#: §22.7: "every scheduled scene within 100 ms of its trigger".
BUDGET_S = 0.100


class OffsetClock(SystemClock):
    """The system clocks, with the wall clock moved by a fixed offset."""

    def __init__(self, offset: timedelta) -> None:
        super().__init__()
        self.offset = offset

    def wall(self) -> datetime:
        return datetime.now(tz=UTC) + self.offset


def next_minute_about_a_second_away() -> tuple[timedelta, float]:
    """An offset that puts a minute boundary on the next whole second plus one,
    and that moment on the monotonic clock."""
    real = datetime.now(tz=UTC)
    mono = time.monotonic()
    fire = real.replace(microsecond=0) + timedelta(seconds=2)
    boundary = fire.replace(second=0) + timedelta(minutes=1)
    return boundary - fire, mono + (fire - real).total_seconds()


@pytest.fixture
async def timed(db: Database, dev_config: Config) -> AsyncIterator[tuple[Rig, OffsetClock]]:
    clock = OffsetClock(timedelta(0))
    rig = await start_rig(db, dev_config, schedule_clock=clock)
    try:
        yield rig, clock
    finally:
        await stop_rig(rig)


@pytest.mark.parametrize("attempt", [1, 2, 3])
async def test_a_scheduled_scene_starts_within_100_ms(
    timed: tuple[Rig, OffsetClock], attempt: int
) -> None:
    rig, clock = timed
    scene = await add_scene(rig.db, rig.venue, "Every minute")
    # Set once the rig is up, so its start-up time cannot eat into the margin.
    clock.offset, fire_mono = next_minute_about_a_second_away()
    rule = await rules_crud.create_rule(
        rig.db,
        name="Every minute",
        trigger_type="schedule",
        cron="* * * * *",
        action_type="run_scene",
        scene_id=scene,
    )
    await rig.reload()
    assert fire_mono - time.monotonic() > 0.5, "set up too slowly to measure"

    for _ in range(1000):  # up to about five seconds
        if rig.scenes.calls:
            break
        await asyncio.sleep(0.005)
    assert rig.scenes.calls, "the scheduled scene never started"
    started = rig.scenes.calls[0].at
    latency = started - fire_mono
    print(f"attempt {attempt}: scene started {latency * 1000:.1f} ms after its trigger")
    assert -0.005 <= latency < BUDGET_S

    await rig.engine.flush_log()
    for _ in range(100):
        entries = await rules_crud.list_executions(rig.db, rule_id=rule.id)
        if entries:
            break
        await asyncio.sleep(0.01)
    detail = json.loads(entries[0].detail or "{}")
    assert entries[0].triggered_by == "schedule"
    assert 0.0 <= detail["dispatch_latency_ms"] < BUDGET_S * 1000
    scheduled = datetime.fromisoformat(detail["scheduled_for"])
    assert (scheduled.second, scheduled.microsecond) == (0, 0)
