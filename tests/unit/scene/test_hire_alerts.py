"""§11.4: "a scene executes with failed actions during a hire session."

Built directly on the same fakes as ``tests/unit/scene/conftest.py``'s
``harness`` fixture, rather than that fixture itself, because this is the
one place that needs an injected :class:`~proskenion.core.alerts.AlertSink`
and a hirer session switched on.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from proskenion.core.alerts import AlertKind, RecordingAlertSink
from proskenion.core.bus import EventBus
from proskenion.core.lighting import LightingService
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from proskenion.scene.engine import SceneEngine
from tests.unit.core.dmx.conftest import FakeDevices, FakeKnx
from tests.unit.scene.conftest import (
    FakeBroadcaster,
    FakeCapabilities,
    FakeKnxWriter,
    Rig,
    build_rig,
    knx,
    make_scene,
    wait_for,
)


@pytest.fixture
async def rig(db: Database) -> Rig:
    return await build_rig(db)


@pytest.fixture
async def lighting(
    db: Database, bus: EventBus, state: StateStore, rig: Rig
) -> AsyncIterator[LightingService]:
    service = LightingService(state, bus, db, FakeDevices(rig.output), FakeKnx())
    await service.start()
    try:
        yield service
    finally:
        await service.stop()


def _engine(
    db: Database, state: StateStore, lighting: LightingService, *, alert_sink: object
) -> SceneEngine:
    return SceneEngine(
        db,
        state,
        lighting=lighting,
        knx=FakeKnxWriter(),
        devices=FakeCapabilities(),
        broadcaster=FakeBroadcaster(),
        alert_sink=alert_sink,  # type: ignore[arg-type]
    )


async def _enable_hire(state: StateStore) -> None:
    state.register_owner("hirer", "test-owner", allow_multiple=True)
    state.hirer.writer("test-owner").set("enabled", True)


async def test_a_failed_action_during_a_hire_alerts_exactly_once(
    db: Database, state: StateStore, lighting: LightingService, rig: Rig
) -> None:
    await _enable_hire(state)
    sink = RecordingAlertSink()
    engine = _engine(db, state, lighting, alert_sink=sink)
    try:
        # No value to write: KnxActionHandler.execute fails this action.
        scene = await make_scene(db, knx(rig.lamp, None))
        handle = await engine.run(scene.id, triggered_by="rule:1")
        result = await handle.result()
        assert result.result in ("failed", "partial")
        await wait_for(lambda: len(sink.sent) == 1)
        assert sink.sent[0].kind == AlertKind.HIRE_ACTION_FAILED
    finally:
        await engine.stop()


async def test_a_failed_action_with_no_hire_session_sends_nothing(
    db: Database, state: StateStore, lighting: LightingService, rig: Rig
) -> None:
    # state.hirer.enabled left at its default False.
    sink = RecordingAlertSink()
    engine = _engine(db, state, lighting, alert_sink=sink)
    try:
        scene = await make_scene(db, knx(rig.lamp, None))
        handle = await engine.run(scene.id, triggered_by="rule:1")
        result = await handle.result()
        assert result.result in ("failed", "partial")
    finally:
        await engine.stop()
    assert sink.sent == []


async def test_a_fully_successful_scene_during_a_hire_sends_nothing(
    db: Database, state: StateStore, lighting: LightingService, rig: Rig
) -> None:
    await _enable_hire(state)
    sink = RecordingAlertSink()
    engine = _engine(db, state, lighting, alert_sink=sink)
    try:
        scene = await make_scene(db, knx(rig.lamp, "1"))
        handle = await engine.run(scene.id, triggered_by="rule:1")
        result = await handle.result()
        assert result.result == "success"
    finally:
        await engine.stop()
    assert sink.sent == []
