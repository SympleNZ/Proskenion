"""The scene engine (spec §8.11–§8.16, §10.6, §5.5, §7.1, §7.2.7; §22.2 and §22.4 items)."""

from __future__ import annotations

import ast
import asyncio
import inspect
from datetime import datetime
from pathlib import Path

import pytest

import proskenion.scene as scene_package
from proskenion.core.broadcast import Broadcaster
from proskenion.core.dmx.fade import ChannelLockedError
from proskenion.core.drivers.capabilities import ProjectorCapabilities
from proskenion.core.knx import Priority
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import mixer as mixer_crud
from proskenion.db.crud import video as video_crud
from proskenion.scene import log as scene_log
from proskenion.scene.domains import ActionOutcome, DomainHandlers, scene_result
from proskenion.scene.engine import (
    SceneDisabledError,
    SceneEngine,
    SceneEngineStoppedError,
    SceneNotFoundError,
    SceneRunHandle,
    ScenesStillRunningError,
)
from tests.unit.scene.conftest import (
    Harness,
    RecordingHandler,
    dmx,
    knx,
    make_scene,
    mixer_capabilities,
    wait_for,
)

#: Windows' event loop wakes on a 15.6 ms timer tick; Linux is far tighter.
#: Accumulated sleeps would drift by a tick per group, so this still separates them.
TOLERANCE_MS = 40.0


# -- the entry point the rules engine is built against ----------------------------------


def test_run_keeps_exactly_the_agreed_signature() -> None:
    """``hirer_originated`` (Phase 5 contracts, "Firing a button", Q8b) is the one
    addition since the signature was first agreed: a run started from a
    hirer's button press, carried unchanged into every action's
    ``ActionContext`` for the clamp applied after a recall."""
    signature = inspect.signature(SceneEngine.run)
    params = list(signature.parameters.values())
    assert [(p.name, p.kind) for p in params] == [
        ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        ("scene_id", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        ("triggered_by", inspect.Parameter.KEYWORD_ONLY),
        ("trigger_value", inspect.Parameter.KEYWORD_ONLY),
        ("hirer_originated", inspect.Parameter.KEYWORD_ONLY),
    ]
    assert params[1].annotation == "int"
    assert params[2].annotation == "str"
    assert params[2].default is inspect.Parameter.empty
    assert params[3].annotation == "object | None"
    assert params[3].default is None
    assert params[4].annotation == "bool"
    assert params[4].default is False
    assert signature.return_annotation == "SceneRunHandle"
    assert inspect.iscoroutinefunction(SceneEngine.run)


async def test_the_handle_carries_the_run_id_and_an_awaitable_result(harness: Harness) -> None:
    scene = await make_scene(harness.db, dmx({harness.rig.front: 60.0}))
    handle = await harness.engine.run(scene.id, triggered_by="knx:1/0/1", trigger_value=True)
    assert isinstance(handle, SceneRunHandle)
    assert isinstance(handle.run_id, int)
    assert handle.scene_id == scene.id
    result = await handle.result()
    assert result.run_id == handle.run_id
    assert result.result == "success"
    assert handle.log_id == result.log_id is not None


async def test_a_caller_that_stops_waiting_never_cancels_the_scene(harness: Harness) -> None:
    scene = await make_scene(harness.db, dmx({harness.rig.front: 90.0}, fade_ms=200))
    handle = await harness.engine.run(scene.id, triggered_by="schedule")
    waiter = asyncio.create_task(handle.result())
    await asyncio.sleep(0.02)
    waiter.cancel()
    result = await handle.result()
    assert result.result == "success"
    assert harness.level(harness.rig.front) == 90.0


async def test_an_unknown_or_disabled_scene_is_refused_but_a_disabled_one_can_be_tested(
    harness: Harness,
) -> None:
    with pytest.raises(SceneNotFoundError):
        await harness.engine.run(999, triggered_by="api:admin")
    scene = await make_scene(harness.db, dmx({harness.rig.front: 40.0}), enabled=False)
    with pytest.raises(SceneDisabledError):
        await harness.engine.run(scene.id, triggered_by="api:operator")
    result = await (await harness.engine.test(scene.id)).result()
    assert result.result == "success"
    assert harness.level(harness.rig.front) == 40.0


# -- grouping and sequencing (§8.13) ------------------------------------------------------


async def test_actions_sharing_a_delay_fire_together_and_each_group_at_its_delay(
    harness: Harness,
) -> None:
    handler = RecordingHandler()
    harness.engine.handlers.register("projector_input", handler)
    await _projector(harness)
    scene = await make_scene(
        harness.db,
        {"domain": "projector_input", "projector_input": "hdmi1", "delay_ms": 0},
        {"domain": "projector_input", "projector_input": "hdmi2", "delay_ms": 0},
        {"domain": "projector_input", "projector_input": "hdmi3", "delay_ms": 120},
        {"domain": "projector_input", "projector_input": "hdmi4", "delay_ms": 120},
        {"domain": "projector_input", "projector_input": "hdmi5", "delay_ms": 300},
    )
    start = asyncio.get_running_loop().time()
    result = await (await harness.engine.run(scene.id, triggered_by="api:admin")).result()

    assert result.result == "success"
    fired = [a.fired_at_ms for a in result.actions]
    assert fired[0] == fired[1]  # one group, one dispatch instant
    assert fired[2] == fired[3]
    for report in result.actions:
        assert report.fired_at_ms is not None
        assert abs(report.fired_at_ms - report.delay_ms) < TOLERANCE_MS
    # Measured independently of the engine's own clock, by when the handler ran.
    for when, action_id, _ in handler.calls:
        delay = next(a.delay_ms for a in result.actions if a.action_id == action_id)
        assert abs((when - start) * 1000 - delay) < TOLERANCE_MS


async def test_groups_fire_on_deadlines_from_scene_start_never_accumulated(
    harness: Harness,
) -> None:
    """A late wake-up delays one group; it is never carried into the next (§8.13)."""
    now = [100.0]
    lag = 0.030

    def clock() -> float:
        return now[0]

    async def late_sleep(seconds: float) -> None:
        now[0] += seconds + lag  # every wake-up is 30 ms late
        await asyncio.sleep(0)

    handler = RecordingHandler()
    engine = SceneEngine(harness.db, harness.state, lighting=None, clock=clock, sleep=late_sleep)
    engine.handlers.register("projector_power", handler)
    scene = await make_scene(
        harness.db,
        *(
            {"domain": "projector_power", "projector_power": "on", "delay_ms": d}
            for d in (0, 1000, 2000, 3000)
        ),
    )
    result = await (await engine.run(scene.id, triggered_by="schedule")).result()
    # Deadline-based: each group is late by one wake-up's lag. Accumulated
    # sleeps would give 0, 1030, 2060, 3090.
    assert [a.fired_at_ms for a in result.actions] == [0.0, 1030.0, 2030.0, 3030.0]
    await engine.stop()


async def test_a_slow_action_never_holds_back_the_next_group(harness: Harness) -> None:
    class Slow(RecordingHandler):
        async def execute(self, action, context):  # type: ignore[no-untyped-def]
            await asyncio.sleep(0.25)
            return await super().execute(action, context)

    harness.engine.handlers.register("projector_power", Slow())
    fast = RecordingHandler()
    harness.engine.handlers.register("projector_input", fast)
    await _projector(harness)
    scene = await make_scene(
        harness.db,
        {"domain": "projector_power", "projector_power": "on", "delay_ms": 0},
        {"domain": "projector_input", "projector_input": "hdmi1", "delay_ms": 50},
    )
    result = await (await harness.engine.run(scene.id, triggered_by="api:admin")).result()
    fired = result.actions[1].fired_at_ms
    assert fired is not None and abs(fired - 50) < TOLERANCE_MS
    assert result.duration_ms >= 250


async def test_test_group_fires_only_that_group_and_at_once(harness: Harness) -> None:
    scene = await make_scene(
        harness.db,
        dmx({harness.rig.front: 10.0}),
        knx(harness.rig.lamp, "1", delay_ms=5000),
        dmx({harness.rig.mid: 70.0}, delay_ms=5000),
    )
    result = await (await harness.engine.test_group(scene.id, 5000)).result()
    assert [a.delay_ms for a in result.actions] == [5000, 5000]
    assert result.duration_ms < 1000
    assert harness.level(harness.rig.mid) == 70.0
    assert harness.level(harness.rig.front) == 0.0  # the 0 ms group did not fire


# -- the dmx domain: the level store through the fade engine, never a blackout -----------


async def test_a_snapshot_action_writes_the_level_store_through_the_fade_engine(
    harness: Harness,
) -> None:
    calls: list[tuple[dict[str, object], int, object]] = []
    original = harness.lighting.apply_snapshot

    def spy(snapshot, *, fade_ms=0, owner=None):  # type: ignore[no-untyped-def]
        calls.append((dict(snapshot), fade_ms, owner))
        return original(snapshot, fade_ms=fade_ms, owner=owner)

    harness.lighting.apply_snapshot = spy  # type: ignore[method-assign]
    scene = await make_scene(
        harness.db, dmx({harness.rig.front: 78.5, harness.rig.mid: 20.0}, fade_ms=100)
    )
    handle = await harness.engine.run(scene.id, triggered_by="api:admin")
    result = await handle.result()

    assert len(calls) == 1
    _, fade_ms, owner = calls[0]
    assert fade_ms == 100
    assert owner == handle.run  # the run owns its fades (§10.6)
    assert harness.level(harness.rig.front) == 78.5
    assert harness.level(harness.rig.mid) == 20.0
    report = result.actions[0]
    assert (report.result, report.marker) == ("sent", "✓")
    assert report.detail["channels"] == sorted([harness.rig.front, harness.rig.mid])


def test_no_code_path_in_the_scene_engine_calls_a_blackout() -> None:
    """§7.1 *Why DMX stays down*: a blackout would leave the model intact.

    Structural, so a maintainer "simplifying" the alarm into a blackout call
    fails here rather than in an empty, alarmed building the next morning.
    """
    package = Path(scene_package.__file__).parent
    offenders: list[str] = []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else func.id
                    if isinstance(func, ast.Name)
                    else ""
                )
                if "blackout" in name.lower() or name in ("suspend", "send_universe"):
                    offenders.append(f"{path.name}:{node.lineno} {name}()")
            if isinstance(node, ast.Attribute) and "blackout" in node.attr.lower():
                offenders.append(f"{path.name}:{node.lineno} .{node.attr}")
    assert offenders == []
    # And the DMX handler does reach the level store the one sanctioned way.
    handlers = (package / "handlers.py").read_text(encoding="utf-8")
    assert "apply_snapshot(" in handlers


async def test_unknown_and_refused_channels_are_detail_not_failure(harness: Harness) -> None:
    alarm = await make_scene(
        harness.db, dmx({harness.rig.front: 0.0}, fade_ms=400), name="All Off", priority="critical"
    )
    await harness.engine.run(alarm.id, triggered_by="knx:1/0/1", trigger_value=True)
    await asyncio.sleep(0.02)
    normal = await make_scene(
        harness.db, dmx({harness.rig.front: 90.0, harness.rig.mid: 50.0, 9999: 10.0})
    )
    result = await (await harness.engine.run(normal.id, triggered_by="api:operator")).result()
    report = result.actions[0]
    assert report.result == "sent"
    assert report.detail["refused"] == [harness.rig.front]  # locked by the alarm
    assert report.detail["unknown"] == ["9999"]
    assert report.detail["channels"] == [harness.rig.mid]
    assert result.result == "success"


# -- the knx domain ----------------------------------------------------------------------


async def test_knx_writes_a_literal_at_scene_priority(harness: Harness) -> None:
    scene = await make_scene(harness.db, knx(harness.rig.lamp, "on"), knx(harness.rig.level, "80"))
    result = await (await harness.engine.run(scene.id, triggered_by="api:admin")).result()
    assert result.result == "success"
    # One group: simultaneous, so in no particular order (§8.13).
    assert sorted((ga, v, p) for _, ga, v, p in harness.knx.writes) == [
        ("1/0/5", 80.0, Priority.SCENE_STATUS),
        ("1/0/9", True, Priority.SCENE_STATUS),
    ]


async def test_knx_passes_the_trigger_value_through_scaled(harness: Harness) -> None:
    scene = await make_scene(
        harness.db,
        knx(harness.rig.lamp, knx_source="trigger_value"),
        knx(harness.rig.byte, knx_source="trigger_value", knx_scale="2.55"),
    )
    handle = await harness.engine.run(scene.id, triggered_by="knx:1/0/7", trigger_value=50.0)
    result = await handle.result()
    assert result.result == "success"
    written = sorted((ga, v) for _, ga, v, _ in harness.knx.writes)
    assert written == [("1/0/20", 128), ("1/0/9", True)]  # 50 % x 2.55, half up


async def test_a_critical_scene_writes_knx_at_alarm_priority(harness: Harness) -> None:
    scene = await make_scene(harness.db, knx(harness.rig.lamp, "0"), priority="critical")
    await (await harness.engine.run(scene.id, triggered_by="knx:1/0/1")).result()
    assert harness.knx.writes[0][3] == Priority.ALARM


async def test_passthrough_without_a_trigger_value_fails_with_a_reason(harness: Harness) -> None:
    scene = await make_scene(harness.db, knx(harness.rig.lamp, knx_source="trigger_value"))
    result = await (await harness.engine.run(scene.id, triggered_by="api:admin")).result()
    report = result.actions[0]
    assert report.result == "failed"
    assert report.reason is not None and "api:admin" in report.reason
    assert harness.knx.writes == []


async def test_the_engine_writes_no_status_feedback_of_its_own(harness: Harness) -> None:
    """B51: indicators are derived from state; a scene's knx actions are its only writes."""
    scene = await make_scene(harness.db, dmx({harness.rig.front: 100.0}, fade_ms=50))
    await (await harness.engine.run(scene.id, triggered_by="api:admin")).result()
    assert harness.knx.writes == []


# -- failure policy (§8.15, §22.4) ----------------------------------------------------------


async def test_scene_execution_with_a_failing_action_is_partial_and_the_rest_still_ran(
    harness: Harness,
) -> None:
    harness.knx.unknown.add("1/0/5")
    boom = RecordingHandler(raises=RuntimeError("driver fault"))
    harness.engine.handlers.register("projector_power", boom)
    await _projector(harness)
    scene = await make_scene(
        harness.db,
        dmx({harness.rig.front: 80.0}),
        knx(harness.rig.level, "50"),  # refused by the subsystem
        {"domain": "projector_power", "projector_power": "on"},  # raises
        knx(harness.rig.lamp, "1", delay_ms=50),
    )
    result = await (await harness.engine.run(scene.id, triggered_by="api:admin")).result()

    assert result.result == "partial"
    outcomes = [(a.domain, a.result) for a in result.actions]
    assert outcomes == [
        ("dmx", "sent"),
        ("knx", "failed"),
        ("projector_power", "failed"),
        ("knx", "sent"),
    ]
    assert result.actions[1].reason is not None and "1/0/5" in result.actions[1].reason
    assert result.actions[2].reason == "RuntimeError: driver fault"
    assert harness.level(harness.rig.front) == 80.0  # everything that can execute does
    assert [ga for _, ga, _, _ in harness.knx.writes] == ["1/0/9"]


def test_scene_result_from_action_results() -> None:
    assert scene_result([]) == "success"
    assert scene_result(["sent", "confirmed"]) == "success"
    assert scene_result(["sent", "failed"]) == "partial"
    assert scene_result(["sent", "skipped"]) == "partial"
    assert scene_result(["failed", "unsupported", "external_control"]) == "failed"


# §8.16: success when every action is ✓; failed when at least one is ✗ and none
# is ✓; partial otherwise. Red must always mean a device needs attention.


def test_scene_result_is_success_when_every_action_is_a_tick() -> None:
    assert scene_result(["sent", "confirmed", "sent"]) == "success"


def test_scene_result_is_failed_when_an_action_failed_and_none_succeeded() -> None:
    assert scene_result(["failed"]) == "failed"
    assert scene_result(["skipped", "failed", "unsupported"]) == "failed"


def test_scene_result_is_partial_when_ticks_are_mixed_with_anything_else() -> None:
    assert scene_result(["sent", "failed"]) == "partial"
    assert scene_result(["confirmed", "external_control"]) == "partial"
    assert scene_result(["sent", "failed", "skipped"]) == "partial"


def test_scene_result_is_partial_not_failed_when_every_action_was_skipped_by_design() -> None:
    assert scene_result(["external_control"]) == "partial"
    assert scene_result(["skipped", "unsupported", "external_control"]) == "partial"


def test_scene_result_with_no_actions_is_success() -> None:
    assert scene_result([]) == "success"


# -- the domain handler registry -------------------------------------------------------------


async def test_a_domain_with_no_handler_is_skipped_and_registering_one_makes_it_run(
    harness: Harness,
) -> None:
    projector = await _projector(harness)
    scene = await make_scene(harness.db, {"domain": "projector_power", "projector_power": "on"})

    before = await (await harness.engine.run(scene.id, triggered_by="api:admin")).result()
    report = before.actions[0]
    assert (report.result, report.marker) == ("skipped", "⊘")
    assert report.reason is not None and "domain not configured" in report.reason

    handler = RecordingHandler(outcome=ActionOutcome.confirmed({"state": "on"}))
    harness.engine.handlers.register("projector_power", handler)  # all Phase 3 has to do
    after = await (await harness.engine.run(scene.id, triggered_by="api:admin")).result()
    assert after.actions[0].result == "confirmed"
    assert after.result == "success"
    # "null means the only device in that category" (§5.5)
    assert handler.calls[0][2].device_id == projector


def test_the_registry_holds_one_handler_per_known_domain() -> None:
    handlers = DomainHandlers()
    handlers.register("mixer_mute", RecordingHandler())
    with pytest.raises(ValueError):
        handlers.register("mixer_mute", RecordingHandler())
    with pytest.raises(ValueError):
        handlers.register("pjlink_power", RecordingHandler())  # renamed away (§8.12)
    handlers.register("mixer_mute", RecordingHandler(), replace=True)
    assert handlers.unregister("mixer_mute") is not None
    assert handlers.get("mixer_mute") is None


async def test_capability_gating_skips_an_unsupported_action_without_attempting_it(
    harness: Harness,
) -> None:
    """§22.2: skipped and logged ⊘ unsupported, not attempted (§5.5)."""
    mixer = await devices_crud.create(
        harness.db, category="mixer", driver_key="cq20b", name="CQ-20B", config={}
    )
    harness.devices.reports = {mixer.id: mixer_capabilities(scene_recall=False)}
    handler = RecordingHandler(
        gate=lambda action, caps: (
            None
            if getattr(caps, "supports_scene_recall", False)
            else "this mixer has no scene recall"
        )
    )
    harness.engine.handlers.register("mixer_recall", handler)
    desk_scene = await mixer_crud.create_desk_scene(
        harness.db, device_id=mixer.id, scene_ref="1", name="Lecture Baseline"
    )
    scene = await make_scene(
        harness.db,
        {"domain": "mixer_recall", "mixer_scene_id": desk_scene.id},
        dmx({harness.rig.front: 50.0}),
    )
    handle = await harness.engine.run(scene.id, triggered_by="api:admin")
    result = await handle.result()

    report = result.actions[0]
    assert (report.result, report.marker) == ("unsupported", "⊘")
    assert report.reason == "this mixer has no scene recall"
    assert handler.gated == [report.action_id]
    assert handler.calls == []  # never attempted
    assert result.actions[1].result == "sent"
    entry = await scene_log.get(harness.db, handle.log_id or 0)
    assert entry is not None
    assert entry.action_results[0]["result"] == "unsupported"
    assert entry.action_results[0]["marker"] == "⊘"


async def test_a_driver_domain_with_no_device_is_skipped(harness: Harness) -> None:
    harness.engine.handlers.register("hdmi_source", RecordingHandler())
    # hdmi_destination and hdmi_input_id are foreign keys since Phase 3
    # (§15.8): the row they name must exist, and matrix_inputs.device_id and
    # video_destinations.device_id are themselves NOT NULL, so some devices
    # row must own them. resolve_device only counts devices whose category
    # is video_matrix, so owning them with a different category keeps this
    # scenario — no video_matrix device configured — true.
    owner = await devices_crud.create(
        harness.db, category="projector", driver_key="pjlink", name="Not a matrix", config={}
    )
    matrix_input = await video_crud.create_input(
        harness.db, device_id=owner.id, driver_ref="1", name="Input 1"
    )
    destination = await video_crud.create_destination(harness.db, device_id=owner.id, name="Room")
    scene = await make_scene(
        harness.db,
        {
            "domain": "hdmi_source",
            "hdmi_destination": destination.id,
            "hdmi_input_id": matrix_input.id,
        },
    )
    result = await (await harness.engine.run(scene.id, triggered_by="api:admin")).result()
    assert result.actions[0].result == "skipped"
    assert result.actions[0].reason == "no video_matrix device is configured"


# -- priority and mutual exclusion (§8.14, §10.6, §22.2) ------------------------------------------


async def test_an_operator_write_cancels_a_normal_scenes_fade_on_that_channel_only(
    harness: Harness,
) -> None:
    rig = harness.rig
    scene = await make_scene(harness.db, dmx({rig.front: 100.0, rig.mid: 100.0}, fade_ms=400))
    handle = await harness.engine.run(scene.id, triggered_by="api:admin")
    await asyncio.sleep(0.1)
    await wait_for(lambda: harness.state.scenes.get("running") != {})
    assert _ring(harness, scene.id)["channels"] == [rig.front, rig.mid]

    harness.lighting.set_level(rig.front, 20.0)  # the operator grabs a fader
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # The ring leaves the taken channel without anyone polling.
    assert _ring(harness, scene.id)["channels"] == [rig.mid]
    result = await handle.result()

    assert harness.level(rig.front) == 20.0  # the operator won
    assert harness.level(rig.mid) == 100.0  # the scene carried on elsewhere
    assert result.actions[0].result == "sent"
    assert result.actions[0].detail["interrupted"] == [rig.front]
    assert result.result == "success"


async def test_under_a_critical_scene_an_operator_write_is_refused(harness: Harness) -> None:
    rig = harness.rig
    harness.lighting.set_level(rig.front, 80.0)
    alarm = await make_scene(
        harness.db, dmx({rig.front: 0.0}, fade_ms=300), name="All Off", priority="critical"
    )
    handle = await harness.engine.run(alarm.id, triggered_by="knx:1/0/1", trigger_value=True)
    await asyncio.sleep(0.05)
    assert _ring(harness, alarm.id)["locked"] is True
    with pytest.raises(ChannelLockedError):
        harness.lighting.set_level(rig.front, 100.0)
    await handle.result()
    assert harness.level(rig.front) == 0.0
    harness.lighting.set_level(rig.front, 30.0)  # released at the end
    assert harness.level(rig.front) == 30.0
    assert harness.lighting.locked_channels() == {}


async def test_a_critical_scene_cancels_running_scenes_at_their_current_values(
    harness: Harness,
) -> None:
    rig = harness.rig
    normal = await make_scene(
        harness.db,
        dmx({rig.front: 100.0, rig.mid: 100.0}, fade_ms=1000),
        knx(rig.lamp, "1", delay_ms=400),
        dmx({rig.back: 100.0}, delay_ms=400),
    )
    alarm = await make_scene(
        harness.db, dmx({rig.back: 0.0}), knx(rig.level, "0"), name="All Off", priority="critical"
    )
    first = await harness.engine.run(normal.id, triggered_by="api:operator")
    await asyncio.sleep(0.2)
    second = await harness.engine.run(alarm.id, triggered_by="knx:1/0/1", trigger_value=True)
    frozen = (harness.level(rig.front), harness.level(rig.mid))
    assert 0.0 < frozen[0] < 100.0 and 0.0 < frozen[1] < 100.0

    interrupted = await first.result()
    await second.result()
    await asyncio.sleep(0.5)  # well past where both would have ended

    # Interrupted where they stood: nothing snapped back and nothing finished.
    assert (harness.level(rig.front), harness.level(rig.mid)) == frozen
    # The pending group was discarded: no lamp write, the back wash untouched by it.
    reports = interrupted.actions
    assert [r.result for r in reports] == ["sent", "skipped", "skipped"]
    assert reports[1].reason is not None and "All Off" in reports[1].reason
    assert reports[1].fired_at_ms is None
    assert sorted(reports[0].detail["interrupted"]) == [rig.front, rig.mid]  # type: ignore[type-var]
    assert [ga for _, ga, _, _ in harness.knx.writes] == ["1/0/5"]
    assert interrupted.result == "partial"
    assert harness.level(rig.back) == 0.0


async def test_a_critical_scene_disables_external_control_before_its_dmx_actions(
    harness: Harness,
) -> None:
    """§8.14, §7.1: the blackout cannot be defeated by a visiting desk."""
    rig = harness.rig
    for channel in (rig.front, rig.mid, rig.back):
        harness.lighting.set_level(channel, 80.0)
    harness.lighting.set_external_manual(True)
    assert harness.lighting.external_active

    seen: list[bool] = []
    original = harness.lighting.apply_snapshot

    def spy(snapshot, *, fade_ms=0, owner=None):  # type: ignore[no-untyped-def]
        seen.append(harness.lighting.external_active)
        return original(snapshot, fade_ms=fade_ms, owner=owner)

    harness.lighting.apply_snapshot = spy  # type: ignore[method-assign]
    alarm = await make_scene(
        harness.db,
        dmx({rig.front: 0.0, rig.mid: 0.0, rig.back: 0.0}),
        name="All Off",
        priority="critical",
    )
    result = await (await harness.engine.run(alarm.id, triggered_by="knx:1/0/1")).result()

    assert seen == [False]  # disabled before the DMX action touched anything
    assert not harness.lighting.external_active
    assert result.actions[0].result == "sent"
    # The blackout reached the level store — the model, not merely the output.
    assert [harness.level(c) for c in (rig.front, rig.mid, rig.back)] == [0.0, 0.0, 0.0]
    assert not harness.lighting.renderer.suspended


async def test_under_external_control_a_normal_scene_skips_dmx_and_still_sends_knx(
    harness: Harness,
) -> None:
    rig = harness.rig
    harness.lighting.set_external_manual(True)
    scene = await make_scene(harness.db, dmx({rig.front: 100.0}), knx(rig.lamp, "1"))
    result = await (await harness.engine.run(scene.id, triggered_by="knx:1/0/2")).result()

    dmx_report, knx_report = result.actions
    assert (dmx_report.result, dmx_report.marker) == ("external_control", "⊘")
    assert dmx_report.reason is not None and "external control" in dmx_report.reason
    assert knx_report.result == "sent"
    assert harness.level(rig.front) == 0.0
    assert harness.lighting.external_active  # a normal scene never takes the rig back
    assert result.result == "partial"


async def test_a_lighting_only_scene_under_external_control_is_partial_not_failed(
    harness: Harness,
) -> None:
    # A visiting desk has the rig: the skip is deliberate and nothing is broken (§8.16).
    harness.lighting.set_external_manual(True)
    scene = await make_scene(harness.db, dmx({harness.rig.front: 100.0}))
    result = await (await harness.engine.run(scene.id, triggered_by="api:operator")).result()
    assert [a.result for a in result.actions] == ["external_control"]
    assert result.result == "partial"


async def test_under_external_control_house_dimmers_in_a_snapshot_still_apply(
    harness: Harness,
) -> None:
    """House lighting is never gated by DMX state (§7.2.3, §7.2.7)."""
    rig = harness.rig
    harness.lighting.set_external_manual(True)
    scene = await make_scene(harness.db, dmx({rig.front: 100.0, rig.house: 60.0}))
    result = await (await harness.engine.run(scene.id, triggered_by="api:admin")).result()
    report = result.actions[0]
    assert report.result == "external_control"
    assert report.detail["skipped_channels"] == [rig.front]
    assert report.detail["house_channels"] == [rig.house]
    assert harness.level(rig.house) == 60.0
    assert harness.level(rig.front) == 0.0


# -- completion, the log and live state (§8.15, §8.16) ---------------------------------


async def test_completion_waits_for_the_fades_and_the_log_records_the_true_time(
    harness: Harness,
) -> None:
    scene = await make_scene(harness.db, dmx({harness.rig.front: 100.0}, fade_ms=300))
    handle = await harness.engine.run(scene.id, triggered_by="api:admin")
    result = await handle.result()

    assert harness.level(harness.rig.front) == 100.0
    assert result.duration_ms >= 300 - TOLERANCE_MS
    entry = await scene_log.get(harness.db, handle.log_id or 0)
    assert entry is not None and entry.completed_at is not None
    elapsed = datetime.fromisoformat(entry.completed_at) - datetime.fromisoformat(entry.started_at)
    assert elapsed.total_seconds() >= 0.3 - TOLERANCE_MS / 1000
    assert entry.result == "success"
    assert entry.triggered_by == "api:admin"
    assert entry.action_results[0]["marker"] == "✓"
    assert entry.action_results[0]["domain"] == "dmx"


async def test_scene_messages_reach_websocket_clients_through_the_broadcaster(
    harness: Harness,
) -> None:
    """The real broadcaster and a real connection: §16.8's discrete messages arrive."""
    broadcaster = Broadcaster(harness.state, harness.state.bus)
    watching = broadcaster.connect(tier="operator", domains=["scenes"])
    elsewhere = broadcaster.connect(tier="operator", domains=["devices"])
    engine = SceneEngine(harness.db, harness.state, broadcaster=broadcaster)
    scene = await make_scene(harness.db, name="Assembly")
    await (await engine.run(scene.id, triggered_by="schedule")).result()
    watching.close("done")
    received = [message async for message in watching.messages()]
    assert received == [
        {"type": "scene_started", "scene_id": scene.id, "triggered_by": "schedule"},
        {"type": "scene_completed", "scene_id": scene.id, "result": "success"},
    ]
    assert elsewhere.queued == 0  # not subscribed to scenes
    await engine.stop()


async def test_live_state_and_the_websocket_messages(harness: Harness) -> None:
    scene = await make_scene(harness.db, dmx({harness.rig.front: 50.0}, fade_ms=150))
    handle = await harness.engine.run(scene.id, triggered_by="surface:3")
    await asyncio.sleep(0.02)
    running = _ring(harness, scene.id)
    assert running["run_id"] == handle.run_id
    assert running["triggered_by"] == "surface:3"
    assert running["locked"] is False
    assert running["channels"] == [harness.rig.front]
    assert harness.engine.is_running(scene.id)

    await handle.result()
    assert harness.state.scenes.get("running") == {}
    last = harness.state.scenes.get("last_result")
    assert isinstance(last, dict) and last["result"] == "success"
    assert harness.broadcaster.messages == [
        {"type": "scene_started", "scene_id": scene.id, "triggered_by": "surface:3"},
        {"type": "scene_completed", "scene_id": scene.id, "result": "success"},
    ]


async def test_stopping_discards_pending_actions_and_refuses_new_runs(harness: Harness) -> None:
    scene = await make_scene(
        harness.db, dmx({harness.rig.front: 30.0}), knx(harness.rig.lamp, "1", delay_ms=5000)
    )
    handle = await harness.engine.run(scene.id, triggered_by="api:admin")
    await asyncio.sleep(0.02)
    await harness.engine.stop()
    result = await handle.result()
    assert [a.result for a in result.actions] == ["sent", "skipped"]
    assert "shutting down" in (result.actions[1].reason or "")
    with pytest.raises(SceneEngineStoppedError):
        await harness.engine.run(scene.id, triggered_by="api:admin")


def _ring(harness: Harness, scene_id: int) -> dict[str, object]:
    running = harness.state.scenes.get("running")
    assert isinstance(running, dict)
    entry = running[str(scene_id)]
    assert isinstance(entry, dict)
    return entry


async def _projector(harness: Harness) -> int:
    """A projector in the database whose driver reports its inputs."""
    device = await devices_crud.create(
        harness.db, category="projector", driver_key="pjlink", name="Projector", config={}
    )
    harness.devices.reports[device.id] = ProjectorCapabilities(
        inputs=("hdmi1", "hdmi2"), supports_authentication=False
    )
    return device.id


async def test_hirer_originated_reaches_every_actions_context(harness: Harness) -> None:
    """Phase 5 contracts, "Firing a button" (Q8b): a hirer-originated run
    marks every action's ActionContext, so the clamp applied after a recall can
    tell a hirer's firing apart from a staff one."""
    await _projector(harness)
    scene = await make_scene(harness.db, {"domain": "projector_power", "projector_power": "on"})
    handler = RecordingHandler(outcome=ActionOutcome.confirmed({"state": "on"}))
    harness.engine.handlers.register("projector_power", handler)

    hirer_result = await (
        await harness.engine.run(scene.id, triggered_by="page:40", hirer_originated=True)
    ).result()
    assert hirer_result.result == "success"
    assert handler.calls[0][2].hirer_originated is True

    staff_result = await (await harness.engine.run(scene.id, triggered_by="api:admin")).result()
    assert staff_result.result == "success"
    assert handler.calls[1][2].hirer_originated is False


# -- the run lock (§13.5's baseline restore) ---------------------------------------------


async def test_the_run_lock_waits_for_a_run_and_holds_back_the_next(harness: Harness) -> None:
    """``exclusive()`` is what a venue baseline restore takes (§13.5): no
    scene starts while it is held, and none is part way through when it is
    acquired."""
    scene = await make_scene(harness.db, dmx({harness.rig.front: 70.0}, fade_ms=200))
    running = await harness.engine.run(scene.id, triggered_by="api:admin")
    inside = asyncio.Event()
    release = asyncio.Event()

    async def hold() -> None:
        async with harness.engine.exclusive():
            inside.set()
            await release.wait()

    holder = asyncio.create_task(hold())
    async with asyncio.timeout(5.0):
        await inside.wait()
    # The lock was only granted once the run in flight had finished.
    assert running.done()
    assert (await running.result()).result == "success"

    # A start while it is held waits rather than being refused or running.
    blocked = asyncio.create_task(harness.engine.run(scene.id, triggered_by="knx:1/0/1"))
    await asyncio.sleep(0)
    assert not blocked.done()
    release.set()
    await holder
    assert (await (await blocked).result()).result == "success"


async def test_the_run_lock_refuses_rather_than_cancelling_a_scene_that_will_not_finish(
    harness: Harness,
) -> None:
    scene = await make_scene(harness.db, dmx({harness.rig.front: 70.0}, delay_ms=60_000))
    handle = await harness.engine.run(scene.id, triggered_by="api:admin")
    with pytest.raises(ScenesStillRunningError) as raised:
        async with harness.engine.exclusive(limit_s=0.01):
            pass  # pragma: no cover - the body is never reached
    assert raised.value.scene_ids == (scene.id,)
    # Never cancelled: discarding a run is §12.4's business, not this one's.
    assert not handle.done()
    # And the lock was released, so ordinary triggers still work.
    quick = await make_scene(harness.db, dmx({harness.rig.mid: 10.0}), name="Quick")
    started = await harness.engine.run(quick.id, triggered_by="api:admin")
    assert (await started.result()).result == "success"
