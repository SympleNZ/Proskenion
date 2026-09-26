"""The rules engine: bindings, debounce, ordering, suppression, triggers, the log (spec §8).

§22.2's items, verbatim where they are the standard:

* Binding rule recall, confirming the group multiplier is forced to 1.0 and the
  result is exactly on_level with a group left at 40%
* Binding value mapping, confirming telegram value 1 applies on_level and 0
  applies off_level through a single rule
* Selective suppression, confirming a run_scene rule fires during external
  control (the scene skipping its own DMX actions is the scene engine's)
* Rule debounce, confirming a repeated panel telegram inside the window does
  not restart a fade
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

import pytest

from proskenion.config import Config
from proskenion.core.bus import EventBus
from proskenion.core.events import DeviceStatusChanged, ProjectorStateChanged
from proskenion.core.lighting import LightingService
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import rules as rules_crud
from proskenion.db.crud.base import AUCKLAND
from proskenion.rules.engine import RuleAlert, RulesEngine, UnknownRuleError
from proskenion.rules.model import SCHEDULE_NEVER_OCCURS
from proskenion.scene.engine import SceneEngine
from tests.unit.core.dmx.conftest import FakeDevices
from tests.unit.rules.conftest import OWN, FakeDeviceKeys, FakeKnx, Rig, add_scene, build_venue


async def logged(rig: Rig, rule_id: int | None = None) -> list[dict[str, Any]]:
    await rig.engine.flush_log()
    entries = await rules_crud.list_executions(rig.db, rule_id=rule_id, limit=1000)
    return [
        {
            "rule_id": e.rule_id,
            "triggered_by": e.triggered_by,
            "guard_result": e.guard_result,
            "result": e.result,
            "detail": json.loads(e.detail or "{}"),
        }
        for e in reversed(entries)
    ]


async def make_rule(rig: Rig, **values: Any) -> int:
    rule = await rules_crud.create_rule(rig.db, **values)
    await rig.reload()
    return rule.id


# -- binding recall (§8.2, §8.8, §22.2) -------------------------------------------


async def test_binding_recall_forces_the_group_multiplier_and_lands_exactly_on_level(
    rig: Rig,
) -> None:
    """A group left at 40% from an evening show must not make the morning come up at
    40%: the recall writes the multiplier to 1.0 and every member lands on on_level.
    The master still applies (§8.8; Simon's Q1)."""
    lighting = rig.lighting
    lighting.set_group_multiplier(rig.venue.groups["Row 1"], 0.4)
    lighting.set_group_multiplier(rig.venue.groups["All Stage"], 0.4)
    lighting.set_master(50.0)
    fixture_a = rig.venue.channels["A"]
    assert lighting.composited_level(fixture_a) == 0.0

    await rig.telegram("1/0/1", True)

    assert rig.multiplier("Row 1") == 1.0  # forced, by a write
    assert rig.level("A") == 100.0 and rig.level("B") == 100.0  # exactly on_level
    # max(Row 1 = 1.0, All Stage = 0.4) × 100 × master 50 %: the master still scales it.
    assert lighting.composited_level(fixture_a) == 50.0
    assert rig.multiplier("All Stage") == 0.4  # a one-time write to this group only
    lighting.set_master(100.0)
    assert lighting.composited_level(fixture_a) == 100.0


async def test_an_off_press_with_a_fade_from_a_group_left_below_full_does_not_jump_up(
    rig: Rig,
) -> None:
    """Forcing the multiplier to 1.0 must not flash the bank up before it fades out:
    each member is rebased to what it was showing first (§8.8, §9.4)."""
    rule_id = rig.venue.rules["Stage Bank 1"]
    current = await rules_crud.get_rule(rig.db, rule_id)
    assert current is not None
    await rules_crud.update_rule(rig.db, rule_id, current.updated_at, fade_ms=1000)
    await rig.reload()
    lighting = rig.lighting
    fixture_a = rig.venue.channels["A"]
    lighting.set_level(fixture_a, 80.0)
    lighting.set_group_multiplier(rig.venue.groups["Row 1"], 0.4)
    lighting.set_group_multiplier(rig.venue.groups["All Stage"], 0.4)
    assert lighting.composited_level(fixture_a) == 32.0

    await rig.telegram("1/0/1", False)

    assert rig.multiplier("Row 1") == 1.0  # still forced, by a write
    assert rig.level("A") == 32.0  # rebased to what it was showing
    assert lighting.composited_level(fixture_a) == 32.0  # so nothing moved
    assert lighting.fades.level_destination(fixture_a) == 0.0  # and it fades out from there


async def test_the_forced_multiplier_is_a_write_not_a_lock(rig: Rig) -> None:
    await rig.telegram("1/0/1", True)
    rig.lighting.set_group_multiplier(rig.venue.groups["Row 1"], 0.3)  # the operator again
    assert rig.multiplier("Row 1") == 0.3


async def test_binding_value_mapping_one_rule_applies_on_for_one_and_off_for_zero(
    rig: Rig,
) -> None:
    rule_id = rig.venue.rules["Stage Bank 1"]
    await rules_crud.update_rule(
        rig.db,
        rule_id,
        (await rules_crud.get_rule(rig.db, rule_id)).updated_at,  # type: ignore[union-attr]
        on_level=80.0,
        off_level=10.0,
    )
    await rig.reload()

    await rig.telegram("1/0/1", True)
    assert (rig.level("A"), rig.level("B")) == (80.0, 80.0)
    rig.clock.advance(1.0)  # past the debounce window
    await rig.telegram("1/0/1", False)
    assert (rig.level("A"), rig.level("B")) == (10.0, 10.0)

    entries = await logged(rig, rule_id)
    assert [(e["result"], e["detail"]["level"]) for e in entries] == [
        ("success", 80.0),
        ("success", 10.0),
    ]
    assert all(e["triggered_by"] == "knx:1/0/1" for e in entries)


async def test_a_binding_fades_its_members_over_fade_ms(rig: Rig) -> None:
    rule_id = rig.venue.rules["Stage Bank 2"]
    current = await rules_crud.get_rule(rig.db, rule_id)
    assert current is not None
    await rules_crud.update_rule(rig.db, rule_id, current.updated_at, fade_ms=400)
    await rig.reload()
    await rig.telegram("1/0/2", True)
    assert rig.lighting.fades.level_destination(rig.venue.channels["C"]) == 100.0
    assert rig.level("C") < 100.0
    await rig.settle(lambda: rig.level("C") == 100.0)
    assert rule_id in {e["rule_id"] for e in await logged(rig)}


# -- debounce (§8.4, §22.2) ----------------------------------------------------------


async def test_a_repeated_panel_telegram_inside_the_window_does_not_restart_a_fade(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rule_id = rig.venue.rules["Stage Bank 1"]
    current = await rules_crud.get_rule(rig.db, rule_id)
    assert current is not None and current.debounce_ms is None  # the 500 ms default
    await rules_crud.update_rule(rig.db, rule_id, current.updated_at, fade_ms=2000)
    await rig.reload()
    calls: list[tuple[int, float]] = []
    real = rig.lighting.recall_group

    def spy(group_id: int, level: float, **kwargs: Any) -> Any:
        calls.append((group_id, level))
        return real(group_id, level, **kwargs)

    monkeypatch.setattr(rig.lighting, "recall_group", spy)

    await rig.telegram("1/0/1", True)  # press
    assert len(calls) == 1
    fixture_a = rig.venue.channels["A"]
    assert rig.lighting.fades.is_fading(fixture_a)
    await asyncio.sleep(0.1)
    level_before = rig.level("A")

    rig.clock.advance(0.3)  # a panel sending on press and release
    await rig.telegram("1/0/1", True)
    rig.clock.advance(0.19)  # still inside 500 ms of the press
    await rig.telegram("1/0/1", True)

    assert len(calls) == 1  # the fade was not restarted
    assert rig.lighting.fades.is_fading(fixture_a)
    await asyncio.sleep(0.05)
    assert rig.level("A") > level_before  # still progressing from where it was
    assert len(await logged(rig, rule_id)) == 1  # repeats are DEBUG, not firings

    rig.clock.advance(0.5)  # outside the window: a real second press fires
    await rig.telegram("1/0/1", True)
    assert len(calls) == 2


async def test_debounce_is_per_rule_and_zero_turns_it_off(rig: Rig) -> None:
    scene = await add_scene(rig.db, rig.venue, "Alarm")
    rule_id = await make_rule(
        rig,
        name="Alarm armed",
        trigger_type="knx",
        knx_address_id=rig.venue.addresses["0/5/0"],
        match_type="equal",
        match_value="1",
        debounce_ms=0,
        action_type="run_scene",
        scene_id=scene,
    )
    await rig.telegram("0/5/0", True)
    await rig.telegram("0/5/0", True)
    await rig.telegram("1/0/1", True)  # another rule's window is its own
    await rig.settle(lambda: len(rig.scenes.calls) == 2)
    assert len(await logged(rig, rule_id)) == 2


# -- every matching rule, in sort_order (§8.7) -----------------------------------------


async def test_every_matching_rule_runs_in_sort_order(rig: Rig) -> None:
    scenes = [await add_scene(rig.db, rig.venue, f"Scene {n}") for n in (5, 1, 3, 1)]
    for order, scene in zip((5, 1, 3, 1), scenes, strict=True):
        await make_rule(
            rig,
            name=f"Alarm step {order}",
            sort_order=order,
            trigger_type="knx",
            knx_address_id=rig.venue.addresses["0/5/0"],
            match_type="equal",
            match_value="1",
            action_type="run_scene",
            scene_id=scene,
        )
    await make_rule(  # matches 0, not 1: not run
        rig,
        name="Alarm disarmed",
        sort_order=0,
        trigger_type="knx",
        knx_address_id=rig.venue.addresses["0/5/0"],
        match_type="equal",
        match_value="0",
        action_type="run_scene",
        scene_id=scenes[0],
    )

    await rig.telegram("0/5/0", True)
    await rig.settle(lambda: len(rig.scenes.calls) == 4)
    await asyncio.sleep(0.02)
    # sort_order 1 (lower id first), 1, 3, 5 — no priority, no first-match-wins.
    assert [c.scene_id for c in rig.scenes.calls] == [scenes[1], scenes[3], scenes[2], scenes[0]]
    assert all(c.triggered_by == "knx:0/5/0" and c.trigger_value is True for c in rig.scenes.calls)


# -- selective suppression (§8.8, §7.2.7, §22.2) -----------------------------------------


async def test_during_external_control_a_run_scene_rule_fires_and_a_binding_does_not(
    rig: Rig,
) -> None:
    alarm = await add_scene(rig.db, rig.venue, "All Off", priority="critical")
    alarm_rule = await make_rule(
        rig,
        name="Alarm armed",
        trigger_type="knx",
        knx_address_id=rig.venue.addresses["0/5/0"],
        match_type="equal",
        match_value="1",
        action_type="run_scene",
        scene_id=alarm,
    )
    rig.lighting.set_external_manual(True)

    await rig.telegram("1/0/1", True)
    await rig.telegram("0/5/0", True)

    assert rig.level("A") == 0.0  # the bank's command telegram was ignored
    await rig.settle(lambda: len(rig.scenes.calls) == 1)
    assert rig.scenes.calls[0].scene_id == alarm  # the alarm scene was not dropped
    by_rule = {e["rule_id"]: e for e in await logged(rig)}
    assert by_rule[rig.venue.rules["Stage Bank 1"]]["result"] == "suppressed"
    assert by_rule[alarm_rule]["result"] == "success"
    states = {s["id"]: s for s in rig.engine.rule_states()}
    assert states[rig.venue.rules["Stage Bank 1"]]["suppressed"] is True
    assert states[alarm_rule]["suppressed"] is False


async def test_a_house_dimmer_binding_is_not_suppressed_by_external_control(rig: Rig) -> None:
    """External control never gates house lighting (§7.2.3, §7.2.7)."""
    rig.lighting.set_external_manual(True)
    await rig.telegram("1/1/20", True)
    assert rig.level("H") == 100.0
    states = {s["id"]: s for s in rig.engine.rule_states()}
    assert states[rig.venue.rules["House"]]["suppressed"] is False


async def test_suppression_releases_when_external_control_ends(rig: Rig) -> None:
    rig.lighting.set_external_manual(True)
    await rig.telegram("1/0/1", True)
    rig.lighting.set_external_manual(False)
    rig.clock.advance(1.0)
    await rig.telegram("1/0/1", True)
    assert rig.level("A") == 100.0


# -- no chaining: the controller's own echo (§8.7) -----------------------------------------


async def test_a_telegram_from_the_controllers_own_address_never_triggers_a_rule(
    rig: Rig,
) -> None:
    """A scene writing a panel's command address comes back from knxd as an incoming
    telegram with the controller's own source. It must not fire the bank's rule."""
    await rig.telegram("1/0/1", True, source=OWN)
    assert rig.level("A") == 0.0
    assert rig.engine.echoes_ignored == 1
    assert await logged(rig) == []


# -- guards (§8.5) --------------------------------------------------------------------------


async def test_one_guard_is_evaluated_and_logged(rig: Rig) -> None:
    scene = await add_scene(rig.db, rig.venue, "Morning")
    rule_id = await make_rule(
        rig,
        name="Morning reset",
        trigger_type="knx",
        knx_address_id=rig.venue.addresses["0/5/0"],
        match_type="equal",
        match_value="0",
        guard_type="time_window",
        guard_value="06:00-09:00",
        action_type="run_scene",
        scene_id=scene,
    )
    rig.engine._wall_clock = lambda: datetime(2026, 9, 11, 20, 0, tzinfo=AUCKLAND)
    await rig.telegram("0/5/0", False)
    rig.engine._wall_clock = lambda: datetime(2026, 9, 11, 7, 30, tzinfo=AUCKLAND)
    rig.clock.advance(1.0)
    await rig.telegram("0/5/0", False)
    await rig.settle(lambda: len(rig.scenes.calls) == 1)
    entries = await logged(rig, rule_id)
    assert [(e["guard_result"], e["result"]) for e in entries] == [
        ("blocked", "blocked"),
        ("passed", "success"),
    ]


async def test_an_external_control_guard(rig: Rig) -> None:
    scene = await add_scene(rig.db, rig.venue, "Desk notice")
    await make_rule(
        rig,
        name="Only with a desk",
        trigger_type="knx",
        knx_address_id=rig.venue.addresses["0/5/0"],
        match_type="any",
        guard_type="external_control",
        guard_value="active",
        action_type="run_scene",
        scene_id=scene,
    )
    await rig.telegram("0/5/0", True)
    rig.lighting.set_external_manual(True)
    rig.clock.advance(1.0)
    await rig.telegram("0/5/0", True)
    await rig.settle(lambda: len(rig.scenes.calls) == 1)


# -- device_state triggers (§8.3) -------------------------------------------------------------


async def mixer(rig: Rig) -> int:
    device = await devices_crud.create(
        rig.db, category="mixer", driver_key="cq", name="CQ-20B", config={}
    )
    rig.keys.keys[device.id] = "mixer"
    return device.id


def status(rig: Rig, key: str, value: str) -> DeviceStatusChanged:
    return DeviceStatusChanged(key, value)  # type: ignore[arg-type]


async def test_a_device_state_trigger_fires_on_the_transition_once_sustained(rig: Rig) -> None:
    device_id = await mixer(rig)
    rule_id = await make_rule(
        rig,
        name="Mixer alert",
        trigger_type="device_state",
        trigger_device_id=device_id,
        trigger_state="offline",
        trigger_for_ms=150,
        action_type="notify",
        message="The mixer has been offline for a while",
    )
    alerts: list[RuleAlert] = []

    async def on_alert(event: RuleAlert) -> None:
        alerts.append(event)

    rig.bus.subscribe(RuleAlert, on_alert, name="test:alerts")
    engine = rig.engine

    await engine.handle_device_status(status(rig, "mixer", "connected"))
    await engine.handle_device_status(status(rig, "mixer", "error"))  # offline …
    await asyncio.sleep(0.05)
    await engine.handle_device_status(status(rig, "mixer", "connected"))  # … briefly
    await asyncio.sleep(0.2)
    assert await logged(rig, rule_id) == []  # a brief reconnection fires nothing

    await engine.handle_device_status(status(rig, "mixer", "error"))
    await engine.handle_device_status(status(rig, "mixer", "error"))  # a detail, not a state
    await rig.settle(lambda: len(alerts) == 1)
    entries = await logged(rig, rule_id)
    assert [e["triggered_by"] for e in entries] == ["device_state:mixer"]
    assert alerts[0].message == "The mixer has been offline for a while"


async def test_notify_is_rate_limited_so_a_flapping_device_cannot_flood(rig: Rig) -> None:
    device_id = await mixer(rig)
    rule_id = await make_rule(
        rig,
        name="Mixer offline",
        trigger_type="device_state",
        trigger_device_id=device_id,
        trigger_state="offline",
        action_type="notify",
        message="Mixer offline",
    )
    for _ in range(3):  # flapping
        await rig.engine.handle_device_status(status(rig, "mixer", "error"))
        await rig.engine.handle_device_status(status(rig, "mixer", "connected"))
        rig.clock.advance(5.0)
    rig.clock.advance(15 * 60)
    await rig.engine.handle_device_status(status(rig, "mixer", "error"))
    results = [e["result"] for e in await logged(rig, rule_id)]
    assert results == ["success", "rate_limited", "rate_limited", "success"]


# -- device_state triggers: the projector's own state (§7.4, §8.3) ----------------------


async def projector_device(rig: Rig) -> int:
    device = await devices_crud.create(
        rig.db, category="projector", driver_key="pjlink", name="Projector", config={}
    )
    return device.id


def projector_status(device_id: int, state: str, previous: str) -> ProjectorStateChanged:
    return ProjectorStateChanged(device_id, state, previous)


async def test_a_device_state_rule_on_the_projector_fires_once_when_it_settles(rig: Rig) -> None:
    """A separate vocabulary and a separate dict from connection status (§8.3):
    the rule fires the moment the projector reaches "on" and not again while
    it stays there, exactly as a connection-status rule already does."""
    device_id = await projector_device(rig)
    rule_id = await make_rule(
        rig,
        name="Projector on",
        trigger_type="device_state",
        trigger_device_id=device_id,
        trigger_state="on",
        action_type="notify",
        message="The projector is on",
    )
    alerts: list[RuleAlert] = []

    async def on_alert(event: RuleAlert) -> None:
        alerts.append(event)

    rig.bus.subscribe(RuleAlert, on_alert, name="test:projector-alerts")
    engine = rig.engine

    await engine.handle_projector_state_changed(projector_status(device_id, "warming", "off"))
    assert alerts == []  # warming is not the trigger state
    await engine.handle_projector_state_changed(projector_status(device_id, "on", "warming"))
    await rig.settle(lambda: len(alerts) == 1)
    # A repeat of the same state (never produced by the real service, but the
    # engine's own tracking must not fire again even if one arrived) is a no-op.
    await engine.handle_projector_state_changed(projector_status(device_id, "on", "on"))
    await asyncio.sleep(0.05)
    assert len(alerts) == 1

    entries = await logged(rig, rule_id)
    assert [e["triggered_by"] for e in entries] == [f"device_state:projector:{device_id}"]
    assert alerts[0].message == "The projector is on"

    # Leaving and re-entering "on" fires the trigger again — past notify's own
    # rate limit (§8.9, unrelated to this trigger), so a second alert lands.
    rig.clock.advance(15 * 60 + 1)
    await engine.handle_projector_state_changed(projector_status(device_id, "cooling", "on"))
    await engine.handle_projector_state_changed(projector_status(device_id, "off", "cooling"))
    await engine.handle_projector_state_changed(projector_status(device_id, "warming", "off"))
    await engine.handle_projector_state_changed(projector_status(device_id, "on", "warming"))
    await rig.settle(lambda: len(alerts) == 2)


async def test_a_device_state_rule_on_the_projector_honours_trigger_for_ms(rig: Rig) -> None:
    device_id = await projector_device(rig)
    rule_id = await make_rule(
        rig,
        name="Projector settled",
        trigger_type="device_state",
        trigger_device_id=device_id,
        trigger_state="on",
        trigger_for_ms=150,
        action_type="notify",
        message="The projector has been on for a while",
    )
    alerts: list[RuleAlert] = []

    async def on_alert(event: RuleAlert) -> None:
        alerts.append(event)

    rig.bus.subscribe(RuleAlert, on_alert, name="test:projector-sustain")
    engine = rig.engine

    await engine.handle_projector_state_changed(projector_status(device_id, "on", "warming"))
    await asyncio.sleep(0.05)
    await engine.handle_projector_state_changed(projector_status(device_id, "cooling", "on"))
    await asyncio.sleep(0.2)
    assert await logged(rig, rule_id) == []  # never stayed on long enough

    await engine.handle_projector_state_changed(projector_status(device_id, "on", "off"))
    await rig.settle(lambda: len(alerts) == 1)
    assert alerts[0].message == "The projector has been on for a while"


# -- connection-status and projector-state dicts do not clobber each other ----------------------


async def test_connection_status_and_projector_state_are_tracked_separately(rig: Rig) -> None:
    """§8.3: a device can be "connected" (transport) and "on" (power) at
    once — one dict must not overwrite the other under the same key."""
    device_id = await projector_device(rig)
    rig.keys.keys[device_id] = "projector"
    on_rule = await make_rule(
        rig,
        name="Projector on",
        trigger_type="device_state",
        trigger_device_id=device_id,
        trigger_state="on",
        action_type="notify",
        message="on",
    )
    connected_rule = await make_rule(
        rig,
        name="Projector connected",
        trigger_type="device_state",
        trigger_device_id=device_id,
        trigger_state="online",
        action_type="notify",
        message="connected",
    )
    engine = rig.engine

    await engine.handle_projector_state_changed(projector_status(device_id, "on", "warming"))
    await engine.handle_device_status(status(rig, "projector", "connected"))

    assert [e["result"] for e in await logged(rig, on_rule)] == ["success"]
    assert [e["result"] for e in await logged(rig, connected_rule)] == ["success"]


# -- schedule and surface (§8.3, §18, §7.6) ----------------------------------------------------


async def test_a_schedule_rule_fires_on_its_own_and_says_when(rig: Rig) -> None:
    scene = await add_scene(rig.db, rig.venue, "All Off")
    rule_id = await make_rule(
        rig,
        name="Nightly off",
        trigger_type="schedule",
        cron="0 23 * * *",
        action_type="run_scene",
        scene_id=scene,
    )
    never = await make_rule(
        rig,
        name="30 February",
        trigger_type="schedule",
        cron="0 0 30 2 *",
        action_type="run_scene",
        scene_id=scene,
    )
    states = {s["id"]: s for s in rig.engine.rule_states()}
    state = states[rule_id]
    assert state["fires_automatically"] is True
    assert state["note"] is None
    upcoming = datetime.fromisoformat(state["next_fire_at"])
    assert upcoming.astimezone(AUCKLAND).strftime("%H:%M") == "23:00"
    assert states[never]["note"] == SCHEDULE_NEVER_OCCURS
    assert states[never]["next_fire_at"] is None


async def test_the_surface_dispatch_path_fires_through_the_rule_layer(rig: Rig) -> None:
    """Phase 9's surfaces only have to call this: guard, debounce and log for free (B53)."""
    bank = rig.venue.rules["Stage Bank 1"]
    report = await rig.engine.fire_surface(bank, button=12)  # no value: a toggle
    assert report.result == "success" and rig.level("A") == 100.0
    repeat = await rig.engine.fire_surface(bank, button=12)
    assert repeat.result == "debounced" and rig.level("A") == 100.0
    # The bank reads on once its frame has gone (§7.1); the next press turns it off.
    await rig.settle(lambda: rig.engine.binding_state(bank))
    rig.clock.advance(1.0)
    await rig.engine.fire_surface(bank, button=12)
    assert rig.level("A") == 0.0
    assert {e["triggered_by"] for e in await logged(rig, bank)} == {"surface:12"}


# -- firing explicitly (§16.5) -------------------------------------------------------------------


async def test_fire_is_as_if_triggered_and_test_reports_a_scene_inline(rig: Rig) -> None:
    scene = await add_scene(rig.db, rig.venue, "Projector on")
    rule_id = await make_rule(
        rig,
        name="Projector on",
        trigger_type="knx",
        knx_address_id=rig.venue.addresses["0/5/0"],
        match_type="equal",
        match_value="1",
        action_type="run_scene",
        scene_id=scene,
    )
    miss = await rig.engine.fire(rule_id, False, triggered_by="api:operator")
    assert miss.result == "not_matched" and not miss.fired
    started = await rig.engine.fire(rule_id, True, triggered_by="api:operator")
    assert started.result == "started"
    tested = await rig.engine.fire(rule_id, triggered_by="api:admin", inline=True)
    assert tested.result == "success" and tested.detail["scene_result"] == "success"
    with pytest.raises(UnknownRuleError):
        await rig.engine.fire(9999, triggered_by="api:admin")


async def test_a_scene_that_fails_is_logged_failed(rig: Rig) -> None:
    scene = await add_scene(rig.db, rig.venue, "Broken")
    rule_id = await make_rule(
        rig,
        name="Broken",
        trigger_type="surface",
        action_type="run_scene",
        scene_id=scene,
    )
    rig.scenes.raise_on.add(scene)
    report = await rig.engine.fire(rule_id, triggered_by="api:admin", inline=True)
    assert report.result == "failed" and "projector unreachable" in report.detail["reason"]


async def test_a_run_scene_rule_logs_the_real_scene_engines_own_outcome(
    db: Database, dev_config: Config
) -> None:
    """Wave 1's own finding (phase-7 plan carry-forward): ``SceneEngine.run()``
    returns its handle as soon as the run has *started* (its own docstring),
    not finished — ``_run_and_record`` used to await only that, so every
    run_scene rule logged "success" at dispatch with the ``SceneRunHandle``
    itself as ``scene_result`` (its ``repr()``, since it has no string
    ``result``/``status``/``outcome`` attribute). Proved here against a real
    :class:`~proskenion.scene.engine.SceneEngine`, not ``FakeScenes`` — a
    fake that happened to match the old, wrong contract (returning the
    outcome directly from ``run()``) could not have caught this.
    """
    bus = EventBus()
    await bus.start()
    try:
        state = StateStore(dev_config, bus)
        venue = await build_venue(db)
        devices = FakeDevices(venue.output)
        devices.connect(venue.output)
        lighting = LightingService(state, bus, db, devices, FakeKnx())
        await lighting.start()
        try:
            scene_id = await add_scene(db, venue, "Empty scene")  # no actions: a deterministic run
            scenes = SceneEngine(db, state, lighting=lighting)
            try:
                engine = RulesEngine(
                    db,
                    state,
                    bus,
                    lighting=lighting,
                    scenes=scenes,
                    knx=FakeKnx(),
                    devices=FakeDeviceKeys(),
                )
                await engine.start()
                try:
                    rule = await rules_crud.create_rule(
                        db,
                        name="Real scene",
                        trigger_type="surface",
                        action_type="run_scene",
                        scene_id=scene_id,
                    )
                    await engine.reload()
                    report = await engine.fire(rule.id, triggered_by="api:admin", inline=True)

                    assert report.result == "success"
                    assert report.detail["scene_result"] == "success"
                    # The bug's own signature: a handle's repr, not a real outcome.
                    assert "SceneRunHandle" not in str(report.detail["scene_result"])
                    assert "object at 0x" not in str(report.detail["scene_result"])

                    # What actually reached the log, not just what fire()
                    # returned — _run_and_record calls self._record() itself.
                    await engine.flush_log()
                    [entry] = await rules_crud.list_executions(db, rule_id=rule.id, limit=10)
                    assert entry.result == "success"
                    assert json.loads(entry.detail or "{}")["scene_result"] == "success"
                finally:
                    await engine.stop()
            finally:
                await scenes.stop()
        finally:
            await lighting.stop()
    finally:
        await bus.stop()


async def test_a_disabled_rule_does_not_fire_but_can_be_tested(rig: Rig) -> None:
    rule_id = rig.venue.rules["Stage Bank 1"]
    current = await rules_crud.get_rule(rig.db, rule_id)
    assert current is not None
    await rules_crud.update_rule(rig.db, rule_id, current.updated_at, enabled=False)
    await rig.reload()
    await rig.telegram("1/0/1", True)
    assert rig.level("A") == 0.0
    report = await rig.engine.fire(rule_id, 1, triggered_by="api:admin", ignore_enabled=True)
    assert report.result == "success" and rig.level("A") == 100.0


async def test_a_binding_refused_by_a_critical_scene_is_partial(rig: Rig) -> None:
    from proskenion.core.dmx.fade import SceneRun

    run = SceneRun(scene_id=1, priority="critical")
    rig.lighting.begin_critical_scene(run, [rig.venue.channels["A"]])
    await rig.telegram("1/0/1", True)
    assert rig.level("A") == 0.0 and rig.level("B") == 100.0
    (entry,) = await logged(rig, rig.venue.rules["Stage Bank 1"])
    assert entry["result"] == "partial"
    assert entry["detail"]["refused"] == [rig.venue.channels["A"]]


async def test_a_firing_whose_rule_was_deleted_meanwhile_is_still_logged(rig: Rig) -> None:
    await rules_crud.log_executions(
        rig.db,
        [
            {
                "rule_id": 9999,  # no such rule: deleted before the log was written
                "triggered_by": "knx:1/0/1",
                "fired_at": "2026-09-11T08:00:00.000000+12:00",
                "guard_result": None,
                "result": "success",
                "detail": "{}",
            }
        ],
    )
    (entry,) = await rules_crud.list_executions(rig.db)
    assert entry.rule_id is None and entry.result == "success"


async def test_hirer_originated_propagates_from_fire_to_the_scene_runner(rig: Rig) -> None:
    """A hirer's button firing (Phase 5 contracts, "Firing a button", Q8b)
    marks the run so a scene can clamp after a recall; a staff firing does
    not. Carried to proskenion.scene.engine.SceneEngine's own ActionContext
    is proved separately in tests/unit/scene/test_engine.py."""
    scene = await add_scene(rig.db, rig.venue, "Hirer button scene")
    rule_id = await make_rule(
        rig,
        name="Hirer button rule",
        trigger_type="surface",
        action_type="run_scene",
        scene_id=scene,
    )
    report = await rig.engine.fire(
        rule_id,
        triggered_by="page:40",
        hirer_originated=True,
        extra_detail={"tier": "hirer"},
    )
    assert report.result == "started"
    await rig.settle(lambda: len(rig.scenes.calls) >= 1)
    assert rig.scenes.calls[-1].hirer_originated is True
    assert rig.scenes.calls[-1].triggered_by == "page:40"
    assert report.detail["tier"] == "hirer"  # merged into the execution log's detail

    await rig.engine.fire(rule_id, triggered_by="api:admin")
    await rig.settle(lambda: len(rig.scenes.calls) >= 2)
    assert rig.scenes.calls[-1].hirer_originated is False
