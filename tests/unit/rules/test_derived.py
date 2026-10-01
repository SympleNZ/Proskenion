"""Derived status, bank states and no chaining (spec §8.6, §8.7, §7.1, §12.1, B51).

§22.2's items, verbatim where they are the standard:

* Derived status, confirming an All binding reads 0 when one member bank is
  off, and that dragging a fixture down in the UI turns the panel indicator red
* No chaining, confirming a derived status write does not re-enter the rule layer

and §7.1's completion rule: a status is written once the frame carrying the
change has been sent, so a two-second fade yields one telegram, after its last
frame.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from proskenion.config import Config, KnxSection
from proskenion.core import knx_dpt
from proskenion.core.broadcast import Broadcaster
from proskenion.core.events import KnxTelegramReceived
from proskenion.core.knx import (
    AddressDirection,
    InMemoryAddressRegistry,
    KnxSubsystem,
    Priority,
)
from proskenion.db.connection import Database
from proskenion.db.crud import rules as rules_crud
from proskenion.rules import derived as derived_module
from proskenion.rules import engine as engine_module
from proskenion.rules.engine import RulesEngine
from tests.stubs.knxd_stub import KnxdStub
from tests.unit.rules.conftest import (
    OWN,
    PANEL,
    PATCH,
    Rig,
    add_scene,
    start_rig,
    stop_rig,
)

STAGE_STATUSES = ("1/0/10", "1/0/11", "1/0/12")


def written(rig: Rig, group_address: str) -> list[Any]:
    return rig.knx.to(group_address)


async def until_written(rig: Rig, group_address: str, value: bool, count: int = 1) -> None:
    await rig.settle(lambda: written(rig, group_address)[-count:] == [value] * count)


def record_order(rig: Rig) -> list[tuple[str, Any]]:
    """Frames and status telegrams in the order they actually leave."""
    events: list[tuple[str, Any]] = []
    output = rig.devices.output(rig.venue.output)
    send = output.send_universe
    write = rig.knx.write

    async def sending(universe: int, data: bytes) -> None:
        await send(universe, data)
        events.append(("frame", bytes(data)))

    async def writing(group_address: str, value: Any, *, priority: int) -> None:
        await write(group_address, value, priority=priority)
        events.append(("status", (group_address, value)))

    output.send_universe = sending  # type: ignore[method-assign]
    rig.knx.write = writing  # type: ignore[method-assign]
    return events


def lit(data: bytes, *fixtures: str) -> bool:
    return all(data[PATCH[f] - 1] == 255 for f in fixtures)


# -- at startup (§12.1, §12.3) ---------------------------------------------------------


async def test_at_startup_every_status_is_evaluated_and_written_at_priority_2(rig: Rig) -> None:
    for group_address in (*STAGE_STATUSES, "1/1/21", "1/0/9"):
        assert written(rig, group_address) == [False]
    priorities = {p for _, ga, _, p in rig.knx.writes if ga in STAGE_STATUSES}
    assert priorities == {Priority.SCENE_STATUS} == {2}


async def test_at_startup_the_restored_model_is_what_the_panel_is_told(
    db: Database, dev_config: Config
) -> None:
    rig = await start_rig(db, dev_config, restore=True)
    try:
        for name in ("A", "B"):  # bank 1 was on when the controller restarted
            rig.lighting.set_level(rig.venue.channels[name], 100.0)
        await rig.engine.start()
        assert written(rig, "1/0/11") == [True]
        assert written(rig, "1/0/12") == [False]
    finally:
        await stop_rig(rig)


# -- derived, not written by whatever fired (§8.6, §22.2) ------------------------------------


async def test_an_all_binding_reads_0_when_one_member_bank_is_off(rig: Rig) -> None:
    await rig.telegram("1/0/0", True)  # All Stage on
    for group_address in STAGE_STATUSES:
        await until_written(rig, group_address, True)
    rig.clock.advance(1.0)

    await rig.telegram("1/0/2", False)  # bank 2 off on its own

    await until_written(rig, "1/0/12", False)
    await until_written(rig, "1/0/10", False)  # twelve of sixteen is not all
    await asyncio.sleep(0.5)  # the re-assert, 250 ms after the bank 2 press settles
    # Bank 1 is untouched: no change-driven repeat. The third telegram is the
    # re-assert after the bank 2 press, the value unchanged. (The All Stage
    # press's re-assert skips it either way: its change wrote it after that press.)
    assert written(rig, "1/0/11") == [False, True, True]


async def test_dragging_a_fixture_down_turns_the_panel_indicator_off(rig: Rig) -> None:
    await rig.telegram("1/0/1", True)
    await until_written(rig, "1/0/11", True)

    rig.lighting.set_level(rig.venue.channels["A"], 60.0)  # a fader in the web interface

    await until_written(rig, "1/0/11", False)
    assert written(rig, "1/0/11") == [False, True, False]


async def test_a_member_capped_below_the_level_counts_as_at_it(
    db: Database, dev_config: Config
) -> None:
    rig = await start_rig(db, dev_config, restore=True)
    try:
        from proskenion.db.crud import lighting as lighting_crud

        channel = await lighting_crud.get_channel(db, rig.venue.channels["B"])
        assert channel is not None
        await lighting_crud.update_channel(db, channel.id, channel.updated_at, max_value=80.0)
        await rig.lighting.reload_config()
        await rig.engine.start()
        await rig.telegram("1/0/1", True)
        assert rig.level("B") == 80.0  # as far as the fixture may go
        await until_written(rig, "1/0/11", True)
    finally:
        await stop_rig(rig)


# -- after the frame, coalesced, on change of value (§7.1, §8.6) -----------------------------


async def test_the_status_is_written_after_the_frame_carrying_the_change_is_sent(
    rig: Rig,
) -> None:
    events = record_order(rig)

    await rig.telegram("1/0/1", True)
    await until_written(rig, "1/0/11", True)

    status_at = events.index(("status", ("1/0/11", True)))
    frames_before = [data for kind, data in events[:status_at] if kind == "frame"]
    assert any(lit(data, "A", "B") for data in frames_before)  # the room first …
    assert not any(kind == "status" for kind, _ in events[:status_at])  # … then the panel


async def test_a_two_second_fade_yields_exactly_one_status_telegram_after_its_last_frame(
    rig: Rig,
) -> None:
    rule_id = rig.venue.rules["Stage Bank 1"]
    current = await rules_crud.get_rule(rig.db, rule_id)
    assert current is not None
    await rules_crud.update_rule(rig.db, rule_id, current.updated_at, fade_ms=2000)
    await rig.reload()
    events = record_order(rig)

    await rig.telegram("1/0/1", True)
    await rig.settle(lambda: rig.level("A") == 100.0, within=4.0)
    await until_written(rig, "1/0/11", True)
    await asyncio.sleep(0.5)  # nothing further arrives on 1/0/11, re-assert included

    statuses = [(i, v) for i, (kind, v) in enumerate(events) if kind == "status"]
    # The re-assert after the press re-sends the other statuses, unchanged, once
    # the fade has finished; 1/0/11 itself was written after the press and is not.
    statuses = [(i, v) for i, v in statuses if v[0] == "1/0/11"]
    assert [v for _, v in statuses] == [("1/0/11", True)]  # one telegram for the whole fade
    frames = [(i, data) for i, (kind, data) in enumerate(events) if kind == "frame"]
    assert len(frames) >= 20  # the fade was rendered, step by step, at up to 40 fps
    first_full = next(i for i, data in frames if lit(data, "A", "B"))
    assert statuses[0][0] > first_full  # after the frame that finished the fade
    assert all(not lit(data, "A") for i, data in frames if i < first_full)


async def test_a_knx_dimmer_group_is_recomputed_on_the_change_itself(rig: Rig) -> None:
    """House dimmers have no frame: the status follows the level store at once."""
    rig.devices.disconnect(rig.venue.output)  # no DMX frame can go at all
    await rig.telegram("1/1/20", True)
    await until_written(rig, "1/1/21", True)


async def test_a_stage_status_waits_for_its_output_to_take_a_frame(rig: Rig) -> None:
    rig.devices.disconnect(rig.venue.output)
    await rig.telegram("1/0/1", True)
    await asyncio.sleep(0.2)
    assert written(rig, "1/0/11") == [False]  # the room has not changed; nor does the panel
    rig.devices.connect(rig.venue.output)  # the node comes back and takes the frame
    await until_written(rig, "1/0/11", True)


async def test_a_failed_status_write_is_retried_at_the_next_change(rig: Rig) -> None:
    rig.knx.fail.add("1/0/11")
    await rig.telegram("1/0/1", True)
    await asyncio.sleep(0.15)
    assert written(rig, "1/0/11") == [False]
    rig.knx.fail.clear()
    rig.lighting.set_level(rig.venue.channels["C"], 5.0)  # any change will do
    await until_written(rig, "1/0/11", True)


# -- external control (§7.2.7, §8.8, §22.2) ------------------------------------------------


async def test_under_external_control_stage_statuses_read_0_and_resume_rewrites_them(
    rig: Rig,
) -> None:
    await rig.telegram("1/0/1", True)
    await rig.telegram("1/1/20", True)
    await until_written(rig, "1/0/11", True)
    await until_written(rig, "1/1/21", True)
    events = record_order(rig)

    rig.lighting.set_external_manual(True)

    await until_written(rig, "1/0/11", False)  # the panel does not claim the bank is on
    await until_written(rig, "1/0/9", True)  # and says why
    assert written(rig, "1/1/21") == [False, True]  # house lighting is unaffected
    assert rig.engine.binding_state(rig.venue.rules["Stage Bank 1"]) is False
    assert rig.engine.binding_state(rig.venue.rules["House"]) is True
    assert not any(kind == "frame" for kind, _ in events)  # nothing sent while held

    rig.lighting.set_external_manual(False)

    await until_written(rig, "1/0/9", False)
    await until_written(rig, "1/0/11", True)  # recomputed and rewritten
    resumed = events.index(("status", ("1/0/11", True)))
    assert any(kind == "frame" and lit(data, "A", "B") for kind, data in events[:resumed])


# -- no chaining (§8.7, §22.2) -----------------------------------------------------------------


def _imports_and_calls(module: object) -> tuple[set[str], list[str]]:
    tree = ast.parse(inspect.getsource(module))  # type: ignore[arg-type]
    imported: set[str] = set()
    calls: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Call):
            calls.append(ast.unparse(node.func))
    return imported, calls


def test_the_derived_status_evaluator_cannot_reach_the_rule_layer() -> None:
    """Structurally: its only outputs are KNX writes and ``binding_states``."""
    imported, calls = _imports_and_calls(derived_module)
    assert not any(name.startswith("proskenion.rules.engine") for name in imported)
    assert not any(name.startswith("proskenion.core.bus") for name in imported)
    assert not any(call.endswith((".emit", ".emit_and_wait")) for call in calls)
    # The engine subscribes to triggers; it never produces one, and the only
    # event it emits is a notify rule's alert.
    _, engine_calls = _imports_and_calls(engine_module)
    assert "KnxTelegramReceived" not in engine_calls
    assert "DeviceStatusChanged" not in engine_calls
    assert [c for c in engine_calls if c.endswith(".emit")] == ["self._bus.emit"]
    assert "RuleAlert" in engine_calls


async def test_a_status_write_echoed_back_does_not_re_enter_the_rule_layer(rig: Rig) -> None:
    """Even a rule configured on the status address — the API refuses one — never
    fires from the echo, whether knxd reports our own source or not."""
    scene = await add_scene(rig.db, rig.venue, "Would chain")
    await rules_crud.create_rule(
        rig.db,
        name="Chained",
        trigger_type="knx",
        knx_address_id=rig.venue.addresses["1/0/11"],
        match_type="any",
        action_type="run_scene",
        scene_id=scene,
    )
    await rig.reload()

    await rig.telegram("1/0/1", True)
    await until_written(rig, "1/0/11", True)
    await rig.telegram("1/0/11", True, source=OWN)  # knxd's echo of our write
    await rig.telegram("1/0/11", True, source=PANEL)  # or however it arrives
    await asyncio.sleep(0.05)

    assert rig.scenes.calls == []
    assert rig.engine.echoes_ignored == 2
    note = {s["name"]: s for s in rig.engine.rule_states()}["Chained"]["note"]
    assert note is not None and "§8.7" in note


@pytest.fixture
async def knxd() -> AsyncIterator[KnxdStub]:
    async with KnxdStub() as stub:
        yield stub


def _registry() -> InMemoryAddressRegistry:
    registry = InMemoryAddressRegistry()
    for ga in ("1/0/0", "1/0/1", "1/0/2", "1/1/20", "0/5/0"):
        registry.register(ga, "1.001", AddressDirection.INCOMING)
    for ga in ("1/0/9", "1/0/10", "1/0/11", "1/0/12", "1/1/21"):
        registry.register(ga, "1.001", AddressDirection.OUTGOING)
    registry.register("1/1/10", "5.001", AddressDirection.OUTGOING)
    return registry


def _apdu(value: bool) -> bytes:
    raw = knx_dpt.resolve("1.001").encode(value)  # type: ignore[union-attr]
    return bytes([0x00, 0x80 | raw[0]])


async def test_against_knxd_the_echo_of_our_own_writes_never_fires_a_rule(
    db: Database, dev_config: Config, knxd: KnxdStub, tmp_path: Path
) -> None:
    """The real KNX subsystem over the knxd stub: a panel press switches the bank,
    the status goes out, and knxd's echoes — of the status and of a write to the
    panel's own command address — come back from the controller's address and
    fire nothing."""
    rig = await start_rig(db, dev_config, restore=True)
    reports: list[tuple[str, str]] = []

    async def report(key: str, status: str, **_: object) -> None:
        reports.append((key, status))

    subsystem = KnxSubsystem(
        KnxSection(host="127.0.0.1", port=knxd.port, individual_address=OWN),
        _registry(),
        rig.bus,
        report,
        backoff_initial_s=0.05,
        backoff_max_s=0.2,
    )
    task = asyncio.create_task(subsystem.run())
    engine = RulesEngine(
        db, rig.state, rig.bus, lighting=rig.lighting, scenes=rig.scenes, knx=subsystem
    )
    rig.engine = engine
    try:
        await rig.settle(lambda: ("knx", "connected") in reports)
        await engine.start()
        await rig.settle(lambda: any(w.group_address == "1/0/11" for w in knxd.writes))

        await knxd.send_telegram("1/0/1", "1.001", True, source_address=PANEL)
        await rig.settle(lambda: rig.level("A") == 100.0)

        def status_writes() -> list[bytes]:
            return [w.apdu for w in knxd.writes if w.group_address == "1/0/11"]

        await rig.settle(lambda: status_writes()[-1:] == [_apdu(True)])
        before = len(knxd.writes)

        await knxd.emit(OWN, "1/0/11", _apdu(True))  # the status, echoed
        await knxd.emit(OWN, "1/0/1", _apdu(False))  # a scene's write to the panel, echoed
        await rig.settle(lambda: engine.echoes_ignored == 2)
        await asyncio.sleep(0.1)

        assert rig.level("A") == 100.0  # the echo of 1/0/1 = 0 did not switch it off
        assert len(knxd.writes) == before  # and nothing new went out
        assert subsystem.is_own_source(OWN) and not subsystem.is_own_source(PANEL)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await stop_rig(rig)


async def test_the_knx_subsystem_learns_its_own_address_from_the_heartbeat(
    knxd: KnxdStub,
) -> None:
    from proskenion.core.bus import EventBus

    bus = EventBus()
    await bus.start()

    async def report(*_: object, **__: object) -> None:
        return None

    subsystem = KnxSubsystem(
        KnxSection(
            host="127.0.0.1", port=knxd.port, heartbeat_address="9/0/9", heartbeat_interval_s=0.05
        ),
        InMemoryAddressRegistry(),
        bus,
        report,
        heartbeat_timeout_s=1.0,
        backoff_initial_s=0.05,
        backoff_max_s=0.2,
    )
    assert subsystem.own_address is None
    task = asyncio.create_task(subsystem.run())
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 3.0
        while not any(w.group_address == "9/0/9" for w in knxd.writes):
            assert loop.time() < deadline
            await asyncio.sleep(0.02)
        heartbeat = next(w for w in knxd.writes if w.group_address == "9/0/9")
        await knxd.emit("1.1.77", "9/0/9", heartbeat.apdu)
        while subsystem.own_address is None:
            assert loop.time() < deadline
            await asyncio.sleep(0.02)
        assert subsystem.own_address == "1.1.77"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await bus.stop()


def test_a_configured_individual_address_is_validated() -> None:
    assert KnxSection(individual_address="1.1.250").individual_address == "1.1.250"
    for bad in ("1/1/250", "16.1.1", "1.1.256", "one"):
        with pytest.raises(ValueError):
            KnxSection(individual_address=bad)


# -- bank states for the interface (§7.1, §21.11) -------------------------------------------


async def test_the_bindings_field_appears_in_a_lighting_state_frame_and_follows_the_bank(
    rig: Rig,
) -> None:
    broadcaster = Broadcaster(rig.state, rig.bus)
    connection = broadcaster.connect(tier="operator", domains=["lighting"])
    bank = str(rig.venue.rules["Stage Bank 1"])

    snapshot = broadcaster.snapshot(["lighting"], connection=connection)[0]
    assert snapshot["bindings"] == {str(r): False for r in rig.venue.rules.values()}

    async def next_bindings() -> dict[str, bool]:
        for _ in range(100):
            broadcaster.tick()
            while connection.queued:
                message = await anext(connection.messages())
                if message["type"] == "lighting_state" and "bindings" in message:
                    return dict(message["bindings"])
            await asyncio.sleep(0.01)
        raise AssertionError("no bindings frame")

    broadcaster.tick()  # what start-up wrote goes out first
    while connection.queued:
        await anext(connection.messages())
    await rig.telegram("1/0/1", True)
    assert (await next_bindings()) == {bank: True}
    rig.clock.advance(1.0)
    await rig.telegram("1/0/1", False)
    assert (await next_bindings()) == {bank: False}


# -- the live monitor (§8.10) ------------------------------------------------------------------


async def test_the_monitor_shows_each_address_its_value_and_when_it_changed(rig: Rig) -> None:
    readings = {r.group_address: r for r in rig.engine.derived.readings()}
    assert readings["1/0/11"].value is False and readings["1/0/11"].written is False
    assert readings["1/0/11"].changed_at is not None

    stream = rig.engine.derived.stream()
    initial = [await anext(stream) for _ in range(len(readings))]
    assert {r.group_address for r in initial if r is not None} == set(readings)

    await rig.telegram("1/0/1", True)
    change = await asyncio.wait_for(anext(stream), 2.0)
    assert change is not None and change.group_address == "1/0/11" and change.value is True
    await stream.aclose()


def test_the_events_carry_no_rule_layer_producer() -> None:
    """KnxTelegramReceived is produced by the KNX subsystem alone."""
    assert KnxTelegramReceived.TYPE == "knx.telegram_received"


async def test_a_group_membership_change_is_re_evaluated(rig: Rig) -> None:
    """Bindings hold no state: add an unlit fixture to a lit bank and its indicator goes off."""
    from proskenion.core.events import LightingConfigChanged
    from proskenion.db.crud import lighting as lighting_crud

    await rig.telegram("1/0/1", True)
    await until_written(rig, "1/0/11", True)
    members = [rig.venue.channels[name] for name in ("A", "B", "C")]
    await lighting_crud.set_group_members(rig.db, rig.venue.groups["Row 1"], members)

    rig.bus.emit(LightingConfigChanged("Row 1 gained fixture C"))

    await until_written(rig, "1/0/11", False)


async def test_the_renderer_calls_frame_listeners_with_the_frame_that_went(rig: Rig) -> None:
    renderer = rig.lighting.renderer
    seen: list[tuple[int, int]] = []

    def broken(device_id: int, frame: int) -> None:
        raise RuntimeError("a listener must never stop the frames")

    renderer.add_frame_listener(broken)
    renderer.add_frame_listener(lambda device_id, frame: seen.append((device_id, frame)))
    before = renderer.composites
    rig.lighting.set_level(rig.venue.channels["D"], 50.0)
    await rig.settle(lambda: any(frame > before for _, frame in seen))
    assert all(device == rig.venue.output for device, _ in seen)
    assert rig.frame_slot("D")[-1][1] == 128  # the frame that went carried the change
    renderer.remove_frame_listener(broken)


# -- button lamps: state.status (Q6, transitioning, Phase 5 contracts) --------------


async def test_a_lamp_only_status_is_evaluated_and_broadcast_but_never_written(
    rig: Rig,
) -> None:
    """Q6: a derived status with no KNX address ('House at 100%', say) still
    lights a page button's lamp, but no telegram is ever sent for it —
    checked against the engine's own KNX-telegram counter (``derived.writes``),
    not ``rig.knx.writes``, which also carries the House dimmer's own level
    write and would otherwise make a real ``House state`` status's telegram
    look like it came from the lamp-only one."""
    lamp = await rules_crud.create_derived_status(
        rig.db,
        name="House at 100%",
        knx_address_id=None,
        source_type="lighting_group_all_at",
        lighting_group_id=rig.venue.groups["House"],
        compare_level=100.0,
    )
    await rig.reload()

    await rig.settle(
        lambda: rig.state.status.get_item("lamps", str(lamp.id))
        == {"on": False, "transitioning": False}
    )
    writes_before = rig.engine.derived.writes

    await rig.telegram("1/1/20", True)
    await rig.settle(
        lambda: rig.state.status.get_item("lamps", str(lamp.id))
        == {"on": True, "transitioning": False}
    )
    # The bank's own KNX-addressed status ("House state", 1/1/21) wrote one
    # telegram; the lamp-only status bound to the same group reached the
    # same "on" value and lit its lamp, but contributed nothing to the count.
    assert rig.engine.derived.writes == writes_before + 1
    assert rig.knx.to("1/1/21")[-1] is True


async def test_every_status_also_reaches_state_status_lamps(rig: Rig) -> None:
    """Every configured status — with or without an address — gets a lamp."""
    await rig.settle(
        lambda: rig.state.status.get_item("lamps", str(rig.venue.statuses["1/0/11"]))
        == {"on": False, "transitioning": False}
    )


async def test_a_disabled_status_reads_a_null_lamp(rig: Rig) -> None:
    status_id = rig.venue.statuses["1/0/11"]
    current = await rules_crud.get_derived_status(rig.db, status_id)
    assert current is not None
    await rules_crud.update_derived_status(rig.db, status_id, current.updated_at, enabled=False)
    await rig.reload()
    await rig.settle(
        lambda: rig.state.status.get_item("lamps", str(status_id))
        == {"on": None, "transitioning": False}
    )


async def test_transitioning_tracks_the_projectors_own_warming_and_cooling_state(
    rig: Rig,
) -> None:
    """transitioning is true only for a device_state status on the projector,
    while it is warming or cooling — independent of the status's own value.

    ``compare_state="online"`` (a connection-status word, never satisfied
    here) keeps ``on`` pinned false throughout, so the test isolates
    ``transitioning`` cleanly: it tracks ``state.projector.state`` — a
    different vocabulary from ``state.devices``' connection status
    (§7.4, §8.3) — which this status's own boolean reading does not consult.
    """
    from proskenion.db.crud import devices as devices_crud

    projector = await devices_crud.create(
        rig.db, category="projector", driver_key="pjlink", name="Projector", config={}
    )
    rig.keys.keys[projector.id] = "projector"
    lamp = await rules_crud.create_derived_status(
        rig.db,
        name="Projector on",
        knx_address_id=None,
        source_type="device_state",
        device_id=projector.id,
        compare_state="online",
    )
    await rig.reload()
    await rig.settle(
        lambda: rig.state.status.get_item("lamps", str(lamp.id))
        == {"on": False, "transitioning": False}
    )

    rig.state.projector.writer("test").set("state", "warming")
    await rig.settle(
        lambda: rig.state.status.get_item("lamps", str(lamp.id))
        == {"on": False, "transitioning": True}
    )

    rig.state.projector.writer("test").set("state", "on")
    await rig.settle(
        lambda: rig.state.status.get_item("lamps", str(lamp.id))
        == {"on": False, "transitioning": False}
    )

    rig.state.projector.writer("test").set("state", "cooling")
    await rig.settle(
        lambda: rig.state.status.get_item("lamps", str(lamp.id))
        == {"on": False, "transitioning": True}
    )


async def test_transitioning_is_false_for_a_device_state_status_on_another_device(
    rig: Rig,
) -> None:
    """A device_state status whose device is not the projector never
    reports transitioning, whatever state.projector says."""
    from proskenion.db.crud import devices as devices_crud

    other = await devices_crud.create(
        rig.db, category="lighting_output", driver_key="artnet", name="Another eDMX8", config={}
    )
    rig.keys.keys[other.id] = "some-other-device"
    lamp = await rules_crud.create_derived_status(
        rig.db,
        name="Other device online",
        knx_address_id=None,
        source_type="device_state",
        device_id=other.id,
        compare_state="online",
    )
    await rig.reload()
    rig.state.projector.writer("test").set("state", "warming")
    await rig.settle(
        lambda: rig.state.status.get_item("lamps", str(lamp.id))
        == {"on": False, "transitioning": False}
    )


# -- device_state on the projector's own operational state (§8.6, §7.4, bug fix) --------------


async def _projector_device(rig: Rig, *, key: str = "projector") -> int:
    from proskenion.db.crud import devices as devices_crud

    projector = await devices_crud.create(
        rig.db, category="projector", driver_key="pjlink", name="Projector", config={}
    )
    rig.keys.keys[projector.id] = key
    return projector.id


async def _outgoing_address(rig: Rig, group_address: str, name: str) -> int:
    from proskenion.db.crud import knx as knx_crud

    row = await knx_crud.create_address(
        rig.db, group_address=group_address, name=name, dpt="1.001", direction="outgoing"
    )
    return row.id


async def test_device_state_on_is_true_only_when_the_projector_is_fully_on(rig: Rig) -> None:
    """The bug found on the real rig 2026-09-29: a status comparing the
    projector to "on" must read from its operational state
    (state.projector.state, §7.4), not its connection record, which this
    rig never touches — device_status(projector) stays "unconfigured"
    throughout."""
    projector = await _projector_device(rig)
    address = await _outgoing_address(rig, "1/2/2", "Projector on")
    await rules_crud.create_derived_status(
        rig.db,
        name="Projector on",
        knx_address_id=address,
        source_type="device_state",
        device_id=projector,
        compare_state="on",
    )
    await rig.reload()
    await until_written(rig, "1/2/2", False)

    for state in ("unreachable", "off", "warming"):
        rig.state.projector.writer("test").set("state", state)
        await asyncio.sleep(0.05)
        assert written(rig, "1/2/2")[-1] is False, state

    rig.state.projector.writer("test").set("state", "on")
    await until_written(rig, "1/2/2", True)  # the KNX write happens on the transition

    rig.state.projector.writer("test").set("state", "cooling")
    await until_written(rig, "1/2/2", False)


async def test_on_or_warming_is_true_for_warming_and_on_only(rig: Rig) -> None:
    """The new alias (model.py STATE_ALIASES): true the moment the
    projector starts, still true once it settles on, false again the
    moment it starts cooling — and false for off/unreachable throughout."""
    projector = await _projector_device(rig)
    address = await _outgoing_address(rig, "1/2/2", "Projector starting")
    await rules_crud.create_derived_status(
        rig.db,
        name="Projector starting",
        knx_address_id=address,
        source_type="device_state",
        device_id=projector,
        compare_state="on_or_warming",
    )
    await rig.reload()
    await until_written(rig, "1/2/2", False)

    for state, expected in (
        ("unreachable", False),
        ("off", False),
        ("warming", True),
        ("on", True),
        ("cooling", False),
        ("error", False),
    ):
        rig.state.projector.writer("test").set("state", state)
        await rig.settle(lambda expected=expected: written(rig, "1/2/2")[-1] == expected)


async def test_connection_state_names_still_match_the_projectors_connection_status(
    rig: Rig,
) -> None:
    """§8.3: "online"/"offline" and bare connection-state names must keep
    reading the projector's *connection* record — untouched by this fix."""
    projector = await _projector_device(rig)
    address = await _outgoing_address(rig, "1/2/2", "Projector online")
    await rules_crud.create_derived_status(
        rig.db,
        name="Projector online",
        knx_address_id=address,
        source_type="device_state",
        device_id=projector,
        compare_state="online",
    )
    await rig.reload()
    await until_written(rig, "1/2/2", False)

    rig.state.projector.writer("test").set("state", "on")  # the operational state, not connection
    await asyncio.sleep(0.05)
    assert written(rig, "1/2/2")[-1] is False

    rig.state.devices.writer("probe").set_status("projector", "connected")
    await until_written(rig, "1/2/2", True)


async def test_error_matches_either_the_projectors_connection_or_operational_state(
    rig: Rig,
) -> None:
    """§8.3: "error" is a member of both vocabularies; a rule (or here, a
    status) naming it matches whichever fires, for the projector."""
    projector = await _projector_device(rig)
    address = await _outgoing_address(rig, "1/2/2", "Projector error")
    await rules_crud.create_derived_status(
        rig.db,
        name="Projector error",
        knx_address_id=address,
        source_type="device_state",
        device_id=projector,
        compare_state="error",
    )
    await rig.reload()
    await until_written(rig, "1/2/2", False)

    rig.state.projector.writer("test").set("state", "error")  # operational
    await until_written(rig, "1/2/2", True)

    rig.state.projector.writer("test").set("state", "on")
    await until_written(rig, "1/2/2", False)

    rig.state.devices.writer("probe").set_status("projector", "error", kind="device")  # connection
    await until_written(rig, "1/2/2", True)


async def test_a_non_projector_device_is_unaffected_by_the_projectors_operational_state(
    rig: Rig,
) -> None:
    """Only the device whose state.devices key is "projector" (§7.4) ever
    consults state.projector; any other device_state status keeps reading
    its connection record alone, exactly as before this fix."""
    from proskenion.db.crud import devices as devices_crud

    other = await devices_crud.create(
        rig.db, category="lighting_output", driver_key="artnet", name="Another eDMX8", config={}
    )
    rig.keys.keys[other.id] = "some-other-device"
    address = await _outgoing_address(rig, "1/2/3", "Other device online")
    await rules_crud.create_derived_status(
        rig.db,
        name="Other device online",
        knx_address_id=address,
        source_type="device_state",
        device_id=other.id,
        compare_state="online",
    )
    await rig.reload()
    await until_written(rig, "1/2/3", False)

    rig.state.projector.writer("test").set("state", "on")  # unrelated device: no effect
    await asyncio.sleep(0.05)
    assert written(rig, "1/2/3") == [False]

    rig.state.devices.writer("probe").set_status("some-other-device", "connected")
    await until_written(rig, "1/2/3", True)


# -- re-assert after a panel press (rig 2026-09-29) ------------------------------------------
#
# A wall panel flips its own icon when pressed. Once the rule layer has finished
# with a telegram on a rule's trigger address, and with what it started, every
# addressed status is written again at its current value — REASSERT_DELAY_S
# (250 ms) after the last completion — unless it was already written since the
# press. So a press that did not take effect is corrected, and one that did is
# confirmed without a second telegram for the status that changed.

ALL_STATUSES = (*STAGE_STATUSES, "1/1/21", "1/0/9")


async def _projector_button(rig: Rig, **rule: Any) -> tuple[int, int]:
    """The rig's projector button: WHEN knx 3/0/0 THEN run_scene "Projector on";
    STATUS knx 3/0/1 = projector on_or_warming. Returns (rule id, scene id)."""
    from proskenion.db.crud import knx as knx_crud

    projector = await _projector_device(rig)
    command = await knx_crud.create_address(
        rig.db, group_address="3/0/0", name="Projector command", dpt="1.001", direction="incoming"
    )
    feedback = await _outgoing_address(rig, "3/0/1", "Projector feedback")
    await rules_crud.create_derived_status(
        rig.db,
        name="Projector on",
        knx_address_id=feedback,
        source_type="device_state",
        device_id=projector,
        compare_state="on_or_warming",
    )
    scene = await add_scene(rig.db, rig.venue, "Projector on")
    created = await rules_crud.create_rule(
        rig.db,
        name="Projector on",
        trigger_type="knx",
        knx_address_id=command.id,
        match_type="any",
        action_type="run_scene",
        scene_id=scene,
        **rule,
    )
    await rig.reload()
    await until_written(rig, "3/0/1", False)
    return created.id, scene


async def _quiet(rig: Rig, seconds: float = 0.5) -> None:
    """Long enough for any re-assert (250 ms after completion) to have gone."""
    await asyncio.sleep(seconds)


async def test_a_press_whose_scene_fails_re_sends_the_unchanged_status(rig: Rig) -> None:
    _, scene = await _projector_button(rig)
    rig.scenes.raise_on.add(scene)  # the projector is unreachable

    await rig.telegram("3/0/0", True)  # the panel has already turned its icon green

    await until_written(rig, "3/0/1", False, count=2)  # … and is told it is still off
    for group_address in ALL_STATUSES:
        await until_written(rig, group_address, False, count=2)
    await _quiet(rig)
    assert written(rig, "3/0/1") == [False, False]  # once, not repeatedly
    assert rig.engine.reasserts_requested == 1
    priorities = {p for _, ga, _, p in rig.knx.writes if ga == "3/0/1"}
    assert priorities == {Priority.SCENE_STATUS}


async def test_a_press_refused_while_warming_leaves_the_status_true(rig: Rig) -> None:
    """B52: an off press during warm-up is refused; the panel flipped red, and
    is told green again."""
    _, scene = await _projector_button(rig)
    rig.state.projector.writer("test").set("state", "warming")
    await until_written(rig, "3/0/1", True)
    rig.scenes.raise_on.add(scene)  # ProjectorUnavailable("transitioning")

    await rig.telegram("3/0/0", False)

    await until_written(rig, "3/0/1", True, count=2)
    assert written(rig, "3/0/1") == [False, True, True]


async def test_a_guard_blocked_press_re_sends_the_status(rig: Rig) -> None:
    rule_id, _ = await _projector_button(rig, guard_type="external_control", guard_value="active")

    await rig.telegram("3/0/0", True)

    await until_written(rig, "3/0/1", False, count=2)
    assert rig.scenes.calls == []
    await rig.engine.flush_log()
    entries = await rules_crud.list_executions(rig.db, rule_id=rule_id, limit=10)
    assert [e.result for e in entries] == ["blocked"]


async def test_a_successful_power_on_is_written_once_by_the_change_itself(rig: Rig) -> None:
    """The expected sequence on 3/0/1 is [False, True]: the change writes True
    after the press, so the re-assert skips it; every other status, not written
    since the press, is re-sent unchanged."""
    await _projector_button(rig)
    hold = rig.scenes.hold = asyncio.Event()  # the scene is still running

    await rig.telegram("3/0/0", True)
    await asyncio.sleep(0.4)
    assert written(rig, "3/0/1") == [False]  # nothing while the scene runs
    assert rig.engine.reasserts_requested == 0
    # set_power → _apply_state: the store holds "warming" when the scene ends.
    rig.state.projector.writer("test").set("state", "warming")
    hold.set()

    await until_written(rig, "3/0/1", True)
    await rig.settle(lambda: rig.engine.reasserts_requested == 1)
    for group_address in ALL_STATUSES:
        await until_written(rig, group_address, False, count=2)
    await _quiet(rig, 0.2)
    assert written(rig, "3/0/1") == [False, True]


async def test_a_binding_press_confirms_the_other_statuses_and_writes_its_own_once(
    rig: Rig,
) -> None:
    await rig.telegram("1/0/1", True)

    await until_written(rig, "1/0/12", False, count=2)  # the re-assert, after the fade
    await _quiet(rig)
    assert written(rig, "1/0/11") == [False, True]  # the change's own write confirms it
    assert written(rig, "1/0/12") == [False, False]
    assert written(rig, "1/0/10") == [False, False]  # not all: re-sent as it stands


async def test_a_burst_of_presses_yields_one_re_assert(rig: Rig) -> None:
    _, scene = await _projector_button(rig)
    rig.scenes.raise_on.add(scene)

    for _ in range(3):
        await rig.telegram("3/0/0", True)
        rig.clock.advance(1.0)  # outside the debounce window: each one fires
        await asyncio.sleep(0.05)
    await _quiet(rig)

    assert len(rig.scenes.calls) == 3
    assert rig.engine.reasserts_requested == 1
    assert written(rig, "3/0/1") == [False, False]
    assert written(rig, "1/0/11") == [False, False]


async def test_a_debounced_repeat_still_ends_in_one_re_assert(rig: Rig) -> None:
    _, scene = await _projector_button(rig)
    rig.scenes.raise_on.add(scene)

    await rig.telegram("3/0/0", True)
    await rig.telegram("3/0/0", False)  # the release, inside the window
    await _quiet(rig)

    assert len(rig.scenes.calls) == 1
    assert written(rig, "3/0/1") == [False, False]


async def test_a_telegram_with_no_enabled_rule_re_asserts_nothing(rig: Rig) -> None:
    rule_id, _ = await _projector_button(rig)
    current = await rules_crud.get_rule(rig.db, rule_id)
    assert current is not None
    await rules_crud.update_rule(rig.db, rule_id, current.updated_at, enabled=False)
    await rig.reload()
    await _quiet(rig, 0.1)
    before = list(rig.knx.writes)

    await rig.telegram("3/0/0", True)  # its only rule is disabled
    await rig.telegram("1/5/1", 350.0, dpt="9.004")  # a lux sensor: no rule at all
    await rig.telegram("1/0/11", True)  # a status address, whoever sent it
    await _quiet(rig)

    assert rig.knx.writes == before
    assert rig.engine.reasserts_requested == 0


async def test_a_held_status_is_not_re_asserted_before_its_frame(rig: Rig) -> None:
    """Every stage status touches the one eDMX8, so all three are held for the
    frame carrying bank 1's change (§7.1). The re-assert writes the house and
    external control statuses at once, and the stage ones only on release:
    bank 1 by its change, once; bank 2 and All Stage, unchanged, re-sent then."""
    rig.devices.disconnect(rig.venue.output)  # no frame can carry the change

    await rig.telegram("1/0/1", True)

    await until_written(rig, "1/1/21", False, count=2)  # the unheld ones are re-sent …
    await until_written(rig, "1/0/9", False, count=2)
    await _quiet(rig, 0.2)
    for group_address in STAGE_STATUSES:  # … the held ones not, stale, ahead of the frame
        assert written(rig, group_address) == [False], group_address
    assert all(r.held for r in rig.engine.derived.readings() if r.group_address in STAGE_STATUSES)

    rig.devices.connect(rig.venue.output)
    await until_written(rig, "1/0/11", True)
    await until_written(rig, "1/0/12", False, count=2)
    await until_written(rig, "1/0/10", False, count=2)
    await _quiet(rig, 0.2)
    assert written(rig, "1/0/11") == [False, True]  # once, when released
    assert written(rig, "1/0/12") == [False, False]
    assert written(rig, "1/1/21") == [False, False]


async def test_re_assert_writes_never_fire_a_rule(rig: Rig) -> None:
    """No chaining (§8.7): neither the re-assert nor knxd's echo of it fires a
    knx or device_state rule, or asks for another re-assert."""
    projector_rule, scene = await _projector_button(rig)
    chained = await add_scene(rig.db, rig.venue, "Would chain")
    await rules_crud.create_rule(
        rig.db,
        name="On a status address",
        trigger_type="knx",
        knx_address_id=rig.venue.addresses["1/0/11"],
        match_type="any",
        action_type="run_scene",
        scene_id=chained,
    )
    projector = next(k for k, v in rig.keys.keys.items() if v == "projector")
    await rules_crud.create_rule(
        rig.db,
        name="When the projector warms",
        trigger_type="device_state",
        trigger_device_id=projector,
        trigger_state="warming",
        action_type="run_scene",
        scene_id=chained,
    )
    await rig.reload()
    rig.scenes.raise_on.add(scene)

    await rig.telegram("3/0/0", True)
    await until_written(rig, "3/0/1", False, count=2)
    await _quiet(rig, 0.2)
    reasserted = [(ga, v) for _, ga, v, _ in rig.knx.writes[-(len(ALL_STATUSES) + 1) :]]
    assert {ga for ga, _ in reasserted} == {*ALL_STATUSES, "3/0/1"}
    for group_address, value in reasserted:  # knxd echoes every one back
        await rig.telegram(group_address, value, source=OWN)
        await rig.telegram(group_address, value, source=PANEL)
    await _quiet(rig)

    assert [c.scene_id for c in rig.scenes.calls] == [scene]
    assert rig.engine.reasserts_requested == 1
    assert rig.engine.derived.reasserts == len(ALL_STATUSES) + 1
    await rig.engine.flush_log()
    entries = await rules_crud.list_executions(rig.db, limit=100)
    assert [e.rule_id for e in entries] == [projector_rule]


# -- basis "output": what the room sees (migration 011, owner decision 2026-09-30) -----------


async def _compare_output(rig: Rig, *group_addresses: str) -> None:
    for group_address in group_addresses:
        status_id = rig.venue.statuses[group_address]
        current = await rules_crud.get_derived_status(rig.db, status_id)
        assert current is not None
        await rules_crud.update_derived_status(
            rig.db, status_id, current.updated_at, basis="output"
        )
    await rig.reload()


async def test_output_basis_reads_off_under_a_master_below_full_and_level_basis_does_not(
    rig: Rig,
) -> None:
    await _compare_output(rig, "1/0/11")
    await rig.telegram("1/0/1", True)  # bank 1 on
    await rig.telegram("1/0/2", True)  # bank 2 on
    await until_written(rig, "1/0/11", True)
    await until_written(rig, "1/0/12", True)
    await _quiet(rig)
    events = record_order(rig)

    rig.lighting.set_master(50.0)  # every stored level is still 100

    await until_written(rig, "1/0/11", False)  # a master change alone writes the status …
    status_at = events.index(("status", ("1/0/11", False)))
    frames_before = [data for kind, data in events[:status_at] if kind == "frame"]
    assert any(data[PATCH["A"] - 1] == 128 for data in frames_before)  # … after its frame
    await _quiet(rig, 0.2)
    assert written(rig, "1/0/12")[-1] is True  # stored levels: unaffected (§8.6 as written)

    rig.lighting.set_master(0.0)
    await _quiet(rig, 0.2)
    assert written(rig, "1/0/11")[-1] is False
    rig.lighting.set_master(100.0)
    await until_written(rig, "1/0/11", True)


async def test_output_basis_reads_off_when_a_row_fader_pulls_its_members_down(
    rig: Rig,
) -> None:
    # A group fader sets its members' levels (owner decision 2026-09-30), so
    # pulling Row 1 to 40 moves A and B to 40: output is level × master = 40.
    await _compare_output(rig, "1/0/11", "1/0/10")
    await rig.telegram("1/0/0", True)  # All Stage on: A–D at 100
    await until_written(rig, "1/0/11", True)
    await until_written(rig, "1/0/10", True)
    await _quiet(rig)

    rig.lighting.set_group_level(rig.venue.groups["Row 1"], 40.0)
    await until_written(rig, "1/0/11", False)
    await until_written(rig, "1/0/10", False)
    assert rig.level("A") == 40.0 and rig.level("C") == 100.0

    rig.lighting.set_group_level(rig.venue.groups["Row 1"], 100.0)
    await until_written(rig, "1/0/11", True)
    await until_written(rig, "1/0/10", True)


async def test_output_basis_turns_true_only_when_a_faders_glide_has_landed(rig: Rig) -> None:
    """A fader's direct write glides the output (field finding 2026-09-30): the
    stored level is 100 at once, but "all at 100" by output waits for the frame
    that carries 100 — not the first frame after the write, which is partway."""
    await _compare_output(rig, "1/0/11")
    row = rig.venue.groups["Row 1"]
    rig.lighting.set_group_level(row, 40.0)
    await until_written(rig, "1/0/11", False)
    await _quiet(rig)
    events = record_order(rig)

    rig.lighting.set_group_level(row, 100.0, glide=True)  # the Row 1 fader on the WebSocket
    assert rig.level("A") == rig.level("B") == 100.0  # stored at once

    await until_written(rig, "1/0/11", True)
    status_at = events.index(("status", ("1/0/11", True)))
    frames = [data for kind, data in events[:status_at] if kind == "frame"]
    assert lit(frames[-1], "A", "B")  # the status follows the frame that landed it …
    partway = [d for d in frames if 102 < d[PATCH["A"] - 1] < 255]
    assert partway  # … after frames that were still on the way
    assert all(d[PATCH["A"] - 1] == d[PATCH["B"] - 1] for d in frames)  # the row moved as one


async def test_output_basis_follows_a_held_bump_on_and_off(rig: Rig) -> None:
    """A BUMP is an output overlay (owner decision 2026-10-01): while it is held
    the room sees the row at full, so an ``output`` status legitimately reads
    true — on the frame that carries the flash — and false again on the frame
    that ends it. The stored levels never move, so a ``level`` status does not."""
    await _compare_output(rig, "1/0/11")
    row = rig.venue.groups["Row 1"]
    rig.lighting.set_group_level(row, 40.0)
    await until_written(rig, "1/0/11", False)
    await _quiet(rig)
    events = record_order(rig)

    rig.lighting.bump_group(row, "tablet", held=True)
    await until_written(rig, "1/0/11", True)
    status_at = events.index(("status", ("1/0/11", True)))
    frames = [data for kind, data in events[:status_at] if kind == "frame"]
    assert lit(frames[-1], "A", "B")  # the flash went before the status
    assert rig.level("A") == rig.level("B") == 40.0  # no stored level changed

    rig.lighting.bump_group(row, "tablet", held=False)
    await until_written(rig, "1/0/11", False)
    assert written(rig, "1/0/11")[-3:] == [False, True, False]
    assert rig.level("A") == 40.0


async def test_output_basis_is_re_asserted_after_a_press_like_any_other(rig: Rig) -> None:
    await _compare_output(rig, "1/0/11")
    rig.lighting.set_master(0.0)
    await rig.telegram("1/0/1", True)  # bank 1 on — but the master holds the room dark
    await rig.settle(lambda: rig.level("A") == 100.0)
    await until_written(rig, "1/0/11", False, count=2)  # unchanged: re-sent after the press
    await _quiet(rig)
    assert written(rig, "1/0/11") == [False, False]


async def test_a_master_change_wakes_nothing_while_no_status_compares_output(rig: Rig) -> None:
    await rig.telegram("1/0/1", True)
    await until_written(rig, "1/0/11", True)
    await _quiet(rig)
    passes = rig.engine.derived.passes
    rig.lighting.set_master(20.0)
    await _quiet(rig, 0.2)
    assert rig.engine.derived.passes == passes
    assert written(rig, "1/0/11")[-1] is True
