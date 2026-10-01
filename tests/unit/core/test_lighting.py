"""The lighting service: boot, external control, snapshots, configuration (§7.2, §9, §12)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest

from proskenion.config import Config
from proskenion.core import knx_dpt
from proskenion.core.bus import EventBus
from proskenion.core.dmx.compositor import (
    KNX_ECHO_WINDOW_S,
    Colour,
    DmxChannel,
    FadeMode,
    KnxChannel,
    LightingConfig,
)
from proskenion.core.dmx.fade import (
    ChannelLockedError,
    SceneRun,
    UnknownChannelError,
    UnknownGroupError,
)
from proskenion.core.events import DeviceStatusChanged, LightingConfigChanged
from proskenion.core.lighting import (
    IndicatorOnlyGroupError,
    LightingService,
    load_lighting_config,
)
from proskenion.core.persist import StatePersister
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import knx as knx_crud
from proskenion.db.crud import lighting as lighting_crud
from proskenion.db.crud import system_state
from tests.unit.core.dmx.conftest import (
    RGB,
    RGBW,
    FakeDevices,
    FakeKnx,
    config,
    dmx,
    knx,
    wait_until,
)

HOUSE_GA = "1/1/10"


@pytest.fixture
async def bus() -> AsyncIterator[EventBus]:
    events = EventBus()
    await events.start()
    try:
        yield events
    finally:
        await events.stop()


@pytest.fixture
def state(dev_config: Config, bus: EventBus) -> StateStore:
    return StateStore(dev_config, bus)


@dataclass
class Venue:
    device_id: int
    dimmer: int
    rgb: int
    rgbw: int
    house: int
    group: int


async def build_venue(db: Database) -> Venue:
    """A small rig in the database: three DMX fixtures, a house dimmer, a group."""
    device = await devices_crud.create(
        db, category="lighting_output", driver_key="artnet", name="eDMX8", config={}
    )
    address = await knx_crud.create_address(
        db, group_address=HOUSE_GA, name="House centre", dpt="5.001", direction="outgoing"
    )
    dimmer = await lighting_crud.create_channel(
        db, name="Front wash", type="dmx", profile_id=1, device_id=device.id, address=1
    )
    rgb = await lighting_crud.create_channel(
        db,
        name="Cyc",
        type="dmx",
        profile_id=2,
        device_id=device.id,
        address=2,
        colour_r=255,
        colour_g=100,
        colour_b=0,
    )
    rgbw = await lighting_crud.create_channel(
        db, name="Backlight", type="dmx", profile_id=3, device_id=device.id, address=5
    )
    house = await lighting_crud.create_channel(
        db, name="House centre", type="knx_dimmer", knx_command_address_id=address.id
    )
    group = await lighting_crud.create_group(db, name="Stage")
    await lighting_crud.set_group_members(db, group.id, [dimmer.id, rgb.id])
    return Venue(device.id, dimmer.id, rgb.id, rgbw.id, house.id, group.id)


def service(
    state: StateStore,
    bus: EventBus,
    db: Database | None = None,
    *,
    devices: FakeDevices | None = None,
    keepalive_s: float = 1.0,
) -> tuple[LightingService, FakeDevices, FakeKnx]:
    devices = devices or FakeDevices()
    sink = FakeKnx()
    return (
        LightingService(state, bus, db, devices, sink, keepalive_s=keepalive_s),
        devices,
        sink,
    )


RIG = config(dmx(1, 1), dmx(2, 2, RGB), dmx(3, 5, RGBW), knx(4, HOUSE_GA), groups={7: {1, 4}})


# -- restore at boot (§12.1, §12.3) ------------------------------------------


async def test_restore_at_boot_brings_the_model_back_and_sends_one_frame_on_connection(
    db: Database, dev_config: Config
) -> None:
    venue = await build_venue(db)

    # Before the reboot: a look is built, and the master pulled down.
    before = StateStore(dev_config, EventBus())
    persister = StatePersister(before, db)
    first, _, _ = service(before, EventBus(), db)
    await first.start()
    first.set_group_level(venue.group, 90.0)  # a group fader sets levels; then trims
    first.set_level(venue.dimmer, 40.0)
    first.set_channel(venue.rgb, level=25.0, colour=Colour(200, 100, 0))
    first.set_level(venue.house, 60.0)
    first.set_master(30.0)
    await first.stop()
    await persister.stop()  # flushes, as graceful shutdown does (§12.4)
    persisted = await system_state.get_domain(db, "lighting")
    assert "master" not in persisted  # never persisted
    assert "group_multipliers" not in persisted  # groups have no state of their own
    # A row an older release persisted, when a group was a multiplier, is ignored.
    await system_state.set(db, "lighting", "group_multipliers", json.dumps({str(venue.group): 0.1}))

    # The reboot.
    bus = EventBus()
    await bus.start()
    try:
        after = StateStore(dev_config, bus)
        await after.restore(db)
        devices = FakeDevices(venue.device_id)
        second, _, sink = service(after, bus, db, devices=devices)
        await second.start()
        try:
            lighting = after.lighting
            assert lighting.get_item("levels", venue.dimmer) == 40.0
            assert lighting.get_item("colour", venue.rgb) == {"r": 200, "g": 100, "b": 0}
            assert "group_multipliers" not in lighting.SPECS
            assert second.master == 100.0  # §12.3: the master resets to 100

            await asyncio.sleep(0.1)
            output = devices.output(venue.device_id)
            assert output.sent == []  # nothing until the output reports connected

            devices.connect(venue.device_id)
            bus.emit(DeviceStatusChanged("dmx", "connected"))
            await wait_until(lambda: len(output.sent) == 1)
            await asyncio.sleep(0.2)
            assert len(output.sent) == 1  # one frame (the keepalive follows at 1 s)
            frame = output.last()
            assert frame[0] == 102  # 40 × master 100 % = 40 %; the old 0.1 is ignored
            assert list(frame[1:4]) == [50, 25, 0]  # 25 % of (200, 100, 0)
            assert sink.writes == []  # KNX dimmers hold their own state (§12.2)
        finally:
            await second.stop()
    finally:
        await bus.stop()


async def test_nothing_is_sent_while_external_control_is_restored_as_manual(
    db: Database, dev_config: Config
) -> None:
    venue = await build_venue(db)
    before = StateStore(dev_config, EventBus())
    persister = StatePersister(before, db)
    first, _, _ = service(before, EventBus(), db)
    await first.start()
    first.set_level(venue.dimmer, 100.0)
    first.set_external_manual(True)
    await first.stop()
    await persister.stop()

    bus = EventBus()
    await bus.start()
    try:
        after = StateStore(dev_config, bus)
        await after.restore(db)
        devices = FakeDevices(venue.device_id)
        second, _, sink = service(after, bus, db, devices=devices, keepalive_s=0.1)
        await second.start()
        try:
            assert second.external.state == "manual" and second.external_active
            devices.connect(venue.device_id)
            bus.emit(DeviceStatusChanged("dmx", "connected"))
            await asyncio.sleep(0.35)
            assert devices.output(venue.device_id).sent == []  # suspended from boot
            second.set_level(venue.house, 70.0)  # house lighting is never gated
            await wait_until(lambda: sink.values() == [70.0])
        finally:
            await second.stop()
    finally:
        await bus.stop()


async def test_detected_external_control_is_not_restored(db: Database, dev_config: Config) -> None:
    venue = await build_venue(db)
    before = StateStore(dev_config, EventBus())
    persister = StatePersister(before, db)
    first, _, _ = service(before, EventBus(), db)
    await first.start()
    first.set_external_detected(True)
    await first.stop()
    await persister.stop()

    after = StateStore(dev_config, EventBus())
    await after.restore(db)
    devices = FakeDevices(venue.device_id)
    devices.connect(venue.device_id)
    second, _, _ = service(after, EventBus(), db, devices=devices)
    await second.start()
    try:
        assert second.external.state == "off"  # re-derived from the booth input
        await wait_until(lambda: len(devices.output(venue.device_id).sent) == 1)
    finally:
        await second.stop()


# -- external control (§7.2.7) -------------------------------------------------


@pytest.fixture
async def running(
    state: StateStore, bus: EventBus
) -> AsyncIterator[tuple[LightingService, FakeDevices, FakeKnx]]:
    svc, devices, sink = service(state, bus, keepalive_s=0.1)
    devices.connect()
    await svc.start(RIG)
    await wait_until(lambda: len(devices.output().sent) >= 1)
    try:
        yield svc, devices, sink
    finally:
        await svc.stop()


async def test_external_control_gates_only_the_dmx_pass(
    running: tuple[LightingService, FakeDevices, FakeKnx], state: StateStore
) -> None:
    svc, devices, sink = running
    svc.set_level(1, 50.0)
    await wait_until(lambda: devices.output().last()[0] == 128)

    svc.set_external_manual(True)
    assert state.lighting.get("external_control") == "manual"
    sent = len(devices.output().sent)
    svc.set_level(4, 65.0)  # a KNX dimmer still receives writes
    await wait_until(lambda: sink.values(HOUSE_GA) == [65.0])
    await asyncio.sleep(0.3)
    assert len(devices.output().sent) == sent  # and no DMX frame is sent


async def test_clearing_external_control_resumes_from_the_controllers_own_levels(
    running: tuple[LightingService, FakeDevices, FakeKnx], state: StateStore
) -> None:
    svc, devices, _ = running
    svc.set_level(1, 50.0)
    await wait_until(lambda: devices.output().last()[0] == 128)
    svc.set_external_manual(True)

    # The desk puts its own look on the wire; the controller's model moves on.
    state.register_owner("lighting", "artnet_input", allow_multiple=True)
    desk = state.lighting.writer("artnet_input")
    desk.set_item("observed", 1, 100.0)
    desk.set_item("observed", 2, 100.0)
    svc.set_level(1, 20.0)
    await asyncio.sleep(0.15)

    svc.set_external_manual(False)
    await wait_until(lambda: devices.output().last()[0] == 51)  # 20 % — its own model
    assert state.lighting.get("external_control") == "off"


async def test_detection_wins_over_the_manual_toggle(
    running: tuple[LightingService, FakeDevices, FakeKnx], state: StateStore
) -> None:
    svc, devices, _ = running
    svc.set_external_detected(True)  # the detection input slot
    assert svc.external.state == "detected" and svc.renderer.suspended
    assert svc.set_external_manual(True) == "manual"  # the persisted flag reads manual
    assert svc.set_external_manual(False) == "detected"  # cannot force off while frames arrive
    assert svc.renderer.suspended
    svc.set_external_detected(False)
    assert state.lighting.get("external_control") == "off" and not svc.renderer.suspended


async def test_a_manual_flag_set_while_a_desk_is_detected_survives_a_reboot(
    db: Database, dev_config: Config
) -> None:
    await build_venue(db)
    before = StateStore(dev_config, EventBus())
    persister = StatePersister(before, db)
    first, _, _ = service(before, EventBus(), db)
    await first.start()
    first.set_external_detected(True)
    first.set_external_manual(True)
    await first.stop()
    await persister.stop()
    after = StateStore(dev_config, EventBus())
    await after.restore(db)
    assert after.lighting.get("external_control") == "manual"


async def test_a_critical_scene_can_force_external_control_off(
    running: tuple[LightingService, FakeDevices, FakeKnx], state: StateStore
) -> None:
    svc, devices, _ = running
    svc.set_external_manual(True)
    svc.set_external_detected(True)
    sent = len(devices.output().sent)

    alarm = SceneRun(scene_id=12, priority="critical")
    svc.begin_critical_scene(alarm, [1, 2, 3])
    svc.force_external_control_off()
    svc.apply_snapshot({"1": {"level": 0.0}, "2": {"level": 0.0}}, owner=alarm)

    assert svc.external.state == "off" and not svc.renderer.suspended
    assert state.lighting.get("external_control") == "off"
    await wait_until(lambda: len(devices.output().sent) > sent)
    # The desk is still sending: the override holds rather than re-suspending
    # the moment the alarm scene finishes...
    svc.set_external_detected(True)
    svc.release_scene(alarm)
    assert svc.external.state == "off"
    # ...until that desk goes quiet, after which detection works as before.
    svc.set_external_detected(False)
    svc.set_external_detected(True)
    assert svc.external.state == "detected"


async def test_an_operator_handing_over_again_clears_the_override(
    running: tuple[LightingService, FakeDevices, FakeKnx],
) -> None:
    svc, _, _ = running
    svc.set_external_detected(True)
    svc.force_external_control_off()
    assert svc.external.state == "off"
    svc.set_external_manual(True)
    assert svc.external.state == "manual" and svc.external_active
    svc.set_external_manual(False)
    assert svc.external.state == "detected"  # the override went with the hand-over


# -- control -------------------------------------------------------------------


async def test_the_master_applies_to_a_bank_recalled_after_its_fader_was_left_at_40(
    running: tuple[LightingService, FakeDevices, FakeKnx],
) -> None:
    svc, devices, sink = running
    svc.set_group_level(7, 40.0)  # the row fader, left at 40 % the night before
    svc.set_master(50.0)
    # The binding rule's recall: every member to on_level.
    svc.recall_group(7, 100.0)
    await wait_until(lambda: devices.output().last()[0] == 128)  # 100 × master 50 %
    # The house dimmer in the bank is outside the master (§9.5): exactly on_level.
    await wait_until(lambda: sink.values(HOUSE_GA)[-1:] == [100.0])


async def test_control_calls_validate_and_clamp(
    running: tuple[LightingService, FakeDevices, FakeKnx],
) -> None:
    svc, _, _ = running
    assert svc.set_master(140.0) == 100.0 and svc.set_master(-5.0) == 0.0
    with pytest.raises(ValueError):
        svc.set_master(float("inf"))
    with pytest.raises(UnknownChannelError):
        svc.set_level(99, 10.0)
    assert svc.set_level(1, 104.0).target_level == 100.0  # the API reports the clamp
    result = svc.set_group_level(7, 25.0, fade_ms=100)
    assert await result.handles[1].wait() == "completed"
    assert svc.composited_level(1) == 0.0  # master still 0 from above
    svc.set_master(100.0)
    assert svc.composited_level(1) == 25.0
    with pytest.raises(UnknownGroupError):
        svc.set_group_level(99, 10.0)
    with pytest.raises(ValueError):
        svc.set_group_level(7, float("nan"))


async def test_an_indicator_only_group_loads_as_such_and_has_no_fader(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    venue = await build_venue(db)
    everything = await lighting_crud.create_group(db, name="Stage all", indicator_only=True)
    await lighting_crud.set_group_members(db, everything.id, [venue.dimmer, venue.rgb])
    cfg = await load_lighting_config(db)
    assert cfg.indicator_only == frozenset({everything.id})
    assert everything.id in cfg.groups  # a derived status still reads its members

    svc, devices, _ = service(state, bus, keepalive_s=0.1)
    devices.connect()
    await svc.start(cfg)
    try:
        svc.set_level(venue.dimmer, 100.0)
        with pytest.raises(IndicatorOnlyGroupError):
            svc.set_group_level(everything.id, 10.0)
        with pytest.raises(IndicatorOnlyGroupError):
            svc.recall_group(everything.id, 10.0)
        assert state.lighting.get_item("levels", venue.dimmer) == 100.0  # untouched
        assert svc.composited_level(venue.dimmer) == 100.0
    finally:
        await svc.stop()


async def test_an_operator_write_under_a_critical_scene_is_refused(
    running: tuple[LightingService, FakeDevices, FakeKnx],
) -> None:
    svc, _, _ = running
    alarm = SceneRun(scene_id=12, priority="critical")
    svc.begin_critical_scene(alarm, [1])
    with pytest.raises(ChannelLockedError):
        svc.set_level(1, 100.0)
    assert svc.driven_channels() == {1: alarm} and svc.locked_channels() == {1: alarm}
    svc.release_scene(alarm)
    svc.set_level(1, 100.0)


async def test_a_knx_status_report_updates_the_store_without_an_echo(
    running: tuple[LightingService, FakeDevices, FakeKnx], state: StateStore
) -> None:
    svc, _, sink = running
    svc.begin_critical_scene(SceneRun(scene_id=12, priority="critical"), [4])
    svc.apply_knx_status(4, 55.0)  # what the dimmer says it is doing is not refused
    await asyncio.sleep(0.15)
    assert state.lighting.get_item("levels", 4) == 55.0
    assert sink.writes == []
    with pytest.raises(UnknownChannelError):
        svc.apply_knx_status(1, 10.0)  # a DMX channel has no status address


async def test_a_status_report_under_a_low_master_is_never_sent_back(
    running: tuple[LightingService, FakeDevices, FakeKnx], state: StateStore
) -> None:
    # §9.6: the store follows the wall panel, and nothing is sent back — not
    # when the report lands, and not when the master or a stage fixture moves.
    svc, devices, sink = running
    svc.set_master(50.0)
    await asyncio.sleep(0.15)
    svc.apply_knx_status(4, 80.0)
    assert state.lighting.get_item("levels", 4) == 80.0
    assert svc.composited_level(4) == 80.0  # the ghost mark agrees with the dimmer
    await asyncio.sleep(0.15)
    svc.set_master(100.0)
    svc.set_level(1, 100.0)
    await wait_until(lambda: devices.output().last()[0] == 255)  # the stage did move
    await asyncio.sleep(0.15)
    assert sink.writes == []


async def test_a_recall_and_a_scene_still_set_a_house_dimmer(
    running: tuple[LightingService, FakeDevices, FakeKnx],
) -> None:
    # §9.4: a KNX house dimmer in a group is set directly by a recall or a
    # scene, whatever the master holds.
    svc, _, sink = running
    svc.set_master(30.0)
    recall = svc.recall_group(7, 70.0, fade_ms=500)  # a hardware dimmer: the target, once
    await recall.handles[4].wait()
    await asyncio.sleep(0.15)
    assert sink.values(HOUSE_GA) == [70.0]
    result = svc.apply_snapshot({"4": {"level": 25.0}}, owner=SceneRun(scene_id=3))
    assert not result.unknown and not result.refused
    await wait_until(lambda: sink.values(HOUSE_GA) == [70.0, 25.0])


# -- a group fader sets levels (owner decision 2026-09-30) ----------------------


class ManualClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


#: Fixture 1 plain; fixture 2 with a 10 % floor; fixture 3 capped at 80 % and
#: also in group 8; a KNX house dimmer; fixture 5 in no group.
GROUP_RIG = config(
    dmx(1, 1),
    dmx(2, 2, min_value=10.0),
    dmx(3, 3, max_value=80.0),
    knx(4, HOUSE_GA),
    dmx(5, 5),
    groups={7: {1, 2, 3, 4}, 8: {3}},
)


def _group_service(state: StateStore, bus: EventBus) -> tuple[LightingService, ManualClock]:
    clock = ManualClock()
    svc = LightingService(state, bus, None, FakeDevices(), FakeKnx(), fade_clock=clock)
    svc.apply_config(GROUP_RIG)
    return svc, clock


def _levels(state: StateStore, *channel_ids: int) -> list[object]:
    return [state.lighting.get_item("levels", c) for c in channel_ids]


async def test_a_group_level_sets_every_member_within_its_own_range(
    state: StateStore, bus: EventBus
) -> None:
    svc, _ = _group_service(state, bus)
    svc.set_level(5, 33.0)

    result = svc.set_group_level(7, 90.0)

    assert result.refused == ()
    assert {c: h.target_level for c, h in result.handles.items()} == {
        1: 90.0,
        2: 90.0,
        3: 80.0,  # held to its max_value, as recall_group always did
        4: 90.0,  # the KNX house dimmer is a member like any other
    }
    assert _levels(state, 1, 2, 3, 4, 5) == [90.0, 90.0, 80.0, 90.0, 33.0]
    svc.set_group_level(7, 0.0)
    assert _levels(state, 1, 2, 3, 4) == [0.0, 10.0, 0.0, 0.0]  # the floor holds
    assert svc.group_level(7) == 10.0  # what the fader shows: the highest member


async def test_a_group_level_fades_every_member_together_with_smoothstep(
    state: StateStore, bus: EventBus
) -> None:
    svc, clock = _group_service(state, bus)
    svc.set_level(1, 20.0)
    svc.set_level(3, 60.0)

    result = svc.set_group_level(7, 70.0, fade_ms=1000)

    assert _levels(state, 1, 3) == [20.0, 60.0]  # nothing jumps
    clock.now += 0.5
    svc.fades.step()
    assert _levels(state, 1, 3) == [45.0, 65.0]  # halfway on smoothstep, from each start
    clock.now += 0.5
    svc.fades.step()
    assert _levels(state, 1, 3) == [70.0, 70.0]
    assert all(h.outcome == "completed" for h in result.handles.values())


async def test_a_group_level_leaves_members_a_critical_scene_holds(
    state: StateStore, bus: EventBus
) -> None:
    svc, _ = _group_service(state, bus)
    alarm = SceneRun(scene_id=12, priority="critical")
    svc.begin_critical_scene(alarm, [3])

    result = svc.set_group_level(7, 50.0)

    assert result.refused == (3,)
    assert set(result.handles) == {1, 2, 4}
    assert _levels(state, 1, 3) == [50.0, None]
    # The scene itself may still write it.
    svc.set_group_level(8, 40.0, owner=alarm)
    assert _levels(state, 3) == [40.0]


async def test_a_group_level_is_not_refused_under_external_control(
    state: StateStore, bus: EventBus
) -> None:
    # As a fixture fader is not: the store takes the levels, DMX output stays
    # suspended until control returns (§7.2.7). Only a binding is suppressed.
    svc, _ = _group_service(state, bus)
    svc.set_external_manual(True)
    svc.set_group_level(7, 60.0)
    assert _levels(state, 1, 4) == [60.0, 60.0]


async def test_a_member_stays_where_the_group_put_it_and_trims_from_there(
    state: StateStore, bus: EventBus
) -> None:
    # The traditional desk the owner asked for: the group sets, the fixture
    # fader trims, and the master scales the result (DMX only).
    svc, _ = _group_service(state, bus)
    svc.set_group_level(7, 60.0)
    svc.set_level(1, 45.0)
    svc.set_master(50.0)
    assert svc.composited_level(1) == 22.5
    assert svc.composited_level(3) == 30.0
    assert svc.composited_level(4) == 60.0  # a house dimmer: its level, unscaled
    assert svc.group_level(7) == 60.0


# -- a dimmer's report during the controller's own fade (§9.6, §7.1) -----------

#: The fade engine's step, and how often these tests run the KNX pass.
TICK_S = 0.02
_SCALING = knx_dpt.resolve("5.001")
assert _SCALING is not None


def as_reported(value: float) -> float:
    """``value`` after DPT 5.001's round trip through a 0–255 byte, as a dimmer reports it."""
    assert _SCALING is not None
    reported: float = _SCALING.decode(_SCALING.encode(value))
    return reported


class HandDriven:
    """A lighting service with one KNX dimmer, driven by hand on a manual clock.

    Each 20 ms tick delivers the dimmer's reports that have fallen due, steps
    the fade engine and runs the KNX pass, as the running service does on
    every change. Deterministic, so a run with a reporting dimmer can be
    compared write for write with the same fade on a silent one.
    """

    def __init__(self, dev_config: Config, bus: EventBus, mode: FadeMode, level: float) -> None:
        self.clock = ManualClock()
        self.state = StateStore(dev_config, bus)
        self.svc = LightingService(
            self.state, bus, None, FakeDevices(), FakeKnx(), fade_clock=self.clock
        )
        self.svc.apply_config(config(knx(4, HOUSE_GA, mode)))
        self.svc.set_level(4, level)
        self.svc.compositor.baseline_knx()  # the dimmer is already there (§12.2)
        self.sent: list[tuple[float, float]] = []
        """``(seconds since the first tick, value)`` for every KNX write, in order."""
        self._t0 = self.clock.now
        self._reports: list[tuple[float, float]] = []

    @property
    def elapsed(self) -> float:
        return round(self.clock.now - self._t0, 6)

    def report(self, after_s: float, level: float) -> None:
        """The dimmer reports ``level`` ``after_s`` from now."""
        self._reports.append((self.elapsed + after_s, level))

    def run(self, seconds: float, *, echo_after_s: float | None = None) -> None:
        """Advance ``seconds``. With ``echo_after_s`` the dimmer reports every
        value it is sent, that long after it was sent, as a stepped dimmer does."""
        for _ in range(round(seconds / TICK_S)):
            self.clock.now += TICK_S
            due = [r for r in self._reports if r[0] <= self.elapsed + 1e-9]
            self._reports = [r for r in self._reports if r not in due]
            for _, level in sorted(due):
                self.svc.apply_knx_status(4, level)
            self.svc.fades.step()
            for write in self.svc.compositor.composite_knx(self.clock.now).writes:
                self.sent.append((self.elapsed, write.value))
                if echo_after_s is not None:
                    self.report(echo_after_s, as_reported(write.value))

    @property
    def level(self) -> object:
        return self.state.lighting.get_item("levels", 4)


@pytest.mark.parametrize(
    ("fade_ms", "start", "target", "echo_after_s"),
    [
        (2000, 10.0, 80.0, 0.04),
        # The last step goes shortly before the fade's end and the rate limit
        # holds the settled value back; the step's echo arrives in between.
        (660, 10.0, 80.0, 0.04),
        (680, 50.0, 0.0, 0.06),
    ],
)
async def test_a_software_fade_whose_dimmer_reports_every_step_completes_at_its_target(
    dev_config: Config,
    bus: EventBus,
    fade_ms: int,
    start: float,
    target: float,
    echo_after_s: float,
) -> None:
    silent = HandDriven(dev_config, bus, "software", start)
    silent.svc.set_level(4, target, fade_ms=fade_ms)
    silent.run(fade_ms / 1000 + 0.5)

    dimmer = HandDriven(dev_config, bus, "software", start)
    handle = dimmer.svc.set_level(4, target, fade_ms=fade_ms)
    dimmer.run(fade_ms / 1000 + 0.5, echo_after_s=echo_after_s)

    assert handle.outcome == "completed"
    assert dimmer.level == target
    assert len(dimmer.sent) >= 5 and dimmer.sent[-1][1] == target  # stepped, landing on target
    assert dimmer.sent == silent.sent  # every step the fade sends, and nothing else


async def test_a_hardware_fade_with_reports_along_the_ramp_lets_a_waiting_scene_complete(
    dev_config: Config, bus: EventBus
) -> None:
    silent = HandDriven(dev_config, bus, "hardware", 10.0)
    silent.svc.set_level(4, 80.0, fade_ms=1000)
    silent.run(1.5)

    dimmer = HandDriven(dev_config, bus, "hardware", 10.0)
    scene = SceneRun(scene_id=5)
    result = dimmer.svc.apply_snapshot({"4": {"level": 80.0}}, fade_ms=1000, owner=scene)
    waiting = asyncio.create_task(result.handles[4].wait())  # as the scene engine waits
    # The dimmer runs its own ramp, 0.6 s here, and reports along the way;
    # the last report, at the target, lands while the level store still fades.
    for n in range(1, 7):
        dimmer.report(n * 0.1, as_reported(10.0 + 70.0 * n / 6))
    dimmer.run(1.5)

    assert await waiting == "completed"
    assert dimmer.level == 80.0
    assert dimmer.sent == silent.sent == [(0.02, 80.0)]  # the target, once, and nothing else


@pytest.mark.parametrize(
    ("mode", "panel"),
    [
        ("software", 5.0),  # below where the fade started
        ("hardware", 5.0),
        ("software", 70.0),  # within the fade's span, but ahead of every step it has sent
    ],
)
async def test_a_panel_press_mid_fade_cancels_the_fade_and_the_panel_wins(
    dev_config: Config, bus: EventBus, mode: FadeMode, panel: float
) -> None:
    silent = HandDriven(dev_config, bus, mode, 20.0)
    silent.svc.set_level(4, 80.0, fade_ms=2000)
    silent.run(2.5)

    dimmer = HandDriven(dev_config, bus, mode, 20.0)
    handle = dimmer.svc.set_level(4, 80.0, fade_ms=2000)
    dimmer.run(0.8)
    before = list(dimmer.sent)
    dimmer.report(0.01, panel)  # a person at the wall panel
    dimmer.run(1.7)

    assert handle.outcome == "cancelled"
    assert dimmer.level == panel
    assert before and before == silent.sent[: len(before)]  # the fade's own writes
    assert dimmer.sent == before  # and nothing after the press: no echo, no more steps


async def test_a_report_with_no_fade_running_is_written_as_the_panels(
    dev_config: Config, bus: EventBus
) -> None:
    dimmer = HandDriven(dev_config, bus, "hardware", 30.0)
    dimmer.svc.set_level(4, 80.0, fade_ms=500)
    dimmer.run(1.0)
    dimmer.report(0.01, 79.8)  # the dimmer settling a little below, after the fade
    dimmer.run(0.5)
    assert dimmer.level == 79.8
    assert dimmer.sent == [(0.02, 80.0)]


# -- a dimmer's reply to any value it was sent (§9.6) ---------------------------

#: A pointer dragging a house-dimmer fader: a new level every 40 ms, faster
#: than the pass's 100 ms rate limit sends them, ending on 63.
DRAG = [20.0 + 4.3 * n for n in range(11)]
#: The time after a write its report still counts as our own (the lighting
#: service's docstring gives the reasoning). These tests use it directly so a
#: change to it is a change they notice.
ECHO_WINDOW_S = 0.5


def drag(dimmer: HandDriven, *, echo_after_s: float | None = None) -> None:
    """Drag the fader through :data:`DRAG`, then let it settle for a second."""
    for level in DRAG:
        dimmer.svc.set_level(4, level)
        dimmer.run(0.04, echo_after_s=echo_after_s)
    dimmer.run(1.0, echo_after_s=echo_after_s)


def test_the_echo_window_is_the_one_the_service_uses() -> None:
    assert KNX_ECHO_WINDOW_S == ECHO_WINDOW_S


@pytest.mark.parametrize("mode", ["software", "hardware"])
@pytest.mark.parametrize("echo_after_s", [0.02, 0.06, 0.1, 0.14])
async def test_a_drag_whose_dimmer_echoes_every_value_late_ends_at_the_drags_last_value(
    dev_config: Config, bus: EventBus, mode: FadeMode, echo_after_s: float
) -> None:
    silent = HandDriven(dev_config, bus, mode, 10.0)
    drag(silent)

    dimmer = HandDriven(dev_config, bus, mode, 10.0)
    drag(dimmer, echo_after_s=echo_after_s)

    final = DRAG[-1]
    assert silent.sent[-1][1] == final and len(silent.sent) >= 4  # rate limited, not every value
    assert dimmer.level == final
    assert dimmer.sent == silent.sent  # the last value sent, and no value that was not


@pytest.mark.parametrize("echo_after_s", [0.04, 0.1, 0.14])
async def test_a_fade_replacing_another_survives_a_late_echo_of_the_old_fades_last_step(
    dev_config: Config, bus: EventBus, echo_after_s: float
) -> None:
    def replace(driven: HandDriven, echo: float | None) -> tuple[object, object]:
        first = driven.svc.set_level(4, 80.0, fade_ms=1000)
        driven.run(0.5, echo_after_s=echo)
        second = driven.svc.set_level(4, 30.0, fade_ms=1000)  # starts with no steps of its own
        driven.run(1.5, echo_after_s=echo)
        return first.outcome, second.outcome

    silent = HandDriven(dev_config, bus, "software", 20.0)
    replace(silent, None)

    dimmer = HandDriven(dev_config, bus, "software", 20.0)
    first, second = replace(dimmer, echo_after_s)

    assert (first, second) == ("cancelled", "completed")  # replaced, then finished
    assert dimmer.level == 30.0
    assert dimmer.sent[-1][1] == 30.0
    assert dimmer.sent == silent.sent


@pytest.mark.parametrize("mode", ["software", "hardware"])
async def test_a_panel_press_inside_the_window_to_a_value_we_did_not_send_wins(
    dev_config: Config, bus: EventBus, mode: FadeMode
) -> None:
    silent = HandDriven(dev_config, bus, mode, 10.0)
    drag(silent)

    dimmer = HandDriven(dev_config, bus, mode, 10.0)
    for level in DRAG:
        dimmer.svc.set_level(4, level)
        dimmer.run(0.04, echo_after_s=0.1)
    dimmer.run(0.2, echo_after_s=0.1)  # the drag's last value has gone
    assert dimmer.sent == silent.sent
    dimmer.report(0.01, 5.0)  # a person at the wall panel, well inside the window
    dimmer.run(1.0)

    assert dimmer.level == 5.0
    assert dimmer.sent == silent.sent  # nothing sent back after the press


@pytest.mark.parametrize(
    ("age_s", "own"),
    [
        (ECHO_WINDOW_S - 0.1, True),  # still our own reply: ignored
        (ECHO_WINDOW_S + 0.1, False),  # the window has closed: the panel's
    ],
)
@pytest.mark.parametrize("mode", ["software", "hardware"])
async def test_a_report_of_a_value_we_sent_counts_as_ours_only_inside_the_window(
    dev_config: Config, bus: EventBus, mode: FadeMode, age_s: float, own: bool
) -> None:
    dimmer = HandDriven(dev_config, bus, mode, 10.0)
    dimmer.svc.set_level(4, 40.0)
    dimmer.run(0.2)
    dimmer.svc.set_level(4, 60.0)
    dimmer.run(0.1)
    assert dimmer.sent == [(0.02, 40.0), (0.22, 60.0)]
    # A report of the first value, age_s after it was sent: an echo held up
    # somewhere, or a person at the panel setting it back.
    dimmer.report(0.02 + age_s - dimmer.elapsed, as_reported(40.0))
    dimmer.run(1.0)

    assert dimmer.level == (60.0 if own else as_reported(40.0))
    assert dimmer.sent == [(0.02, 40.0), (0.22, 60.0)]  # a report never causes a write


# -- snapshots (§8.12, §9.7) ---------------------------------------------------


async def test_capture_the_current_look_in_the_812_format(
    running: tuple[LightingService, FakeDevices, FakeKnx],
) -> None:
    svc, _, _ = running
    svc.set_level(1, 100.0)
    svc.set_channel(2, level=78.5, colour=Colour(255, 120, 0))
    svc.set_channel(3, level=70.6, colour=Colour(255, 180, 0, 60))
    svc.set_level(4, 30.0)
    snapshot = svc.capture_snapshot()
    assert snapshot == {
        "1": {"level": 100.0},
        "2": {"level": 78.5, "r": 255, "g": 120, "b": 0},
        "3": {"level": 70.6, "r": 255, "g": 180, "b": 0, "w": 60},
    }
    assert json.loads(json.dumps(snapshot)) == snapshot
    assert svc.capture_snapshot(include_knx=True)["4"] == {"level": 30.0}


async def test_apply_a_snapshot_fades_to_it_and_reports_what_it_could_not_do(
    running: tuple[LightingService, FakeDevices, FakeKnx], state: StateStore
) -> None:
    svc, _, _ = running
    alarm = SceneRun(scene_id=12, priority="critical")
    svc.begin_critical_scene(alarm, [3])
    scene = SceneRun(scene_id=5)
    result = svc.apply_snapshot(
        {
            "1": {"level": 60.0},
            "2": {"level": 40.0, "r": 0, "g": 0, "b": 255},
            "3": {"level": 10.0},
            "99": {"level": 10.0},
            "bogus": {"level": 1.0},
        },
        fade_ms=100,
        owner=scene,
    )
    assert set(result.handles) == {1, 2}
    assert result.refused == (3,) and set(result.unknown) == {"99", "bogus"}
    assert svc.driven_channels()[1] == scene
    await asyncio.gather(*(h.wait() for h in result.handles.values()))
    assert state.lighting.get_item("levels", 1) == 60.0
    assert state.lighting.get_item("colour", 2) == {"r": 0, "g": 0, "b": 255}


# -- configuration -----------------------------------------------------------------


async def test_configuration_loads_from_the_database(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    venue = await build_venue(db)
    timed_ga = await knx_crud.create_address(
        db, group_address="1/1/11", name="Side", dpt="5.001", direction="outgoing"
    )
    timed = await lighting_crud.create_channel(
        db,
        name="Side",
        type="knx_dimmer",
        knx_command_address_id=timed_ga.id,
        fade_mode="hardware_timed",
    )
    with caplog.at_level(logging.WARNING):
        cfg = await load_lighting_config(db)
    channels = cfg.channels()
    dimmer, rgbw = channels[venue.dimmer], channels[venue.rgbw]
    house, side = channels[venue.house], channels[timed.id]
    assert isinstance(dimmer, DmxChannel) and isinstance(rgbw, DmxChannel)
    assert isinstance(house, KnxChannel) and isinstance(side, KnxChannel)
    assert [s.role for s in dimmer.slots] == ["dimmer"]
    assert [s.role for s in rgbw.slots] == ["red", "green", "blue", "white"]
    assert (dimmer.device_id, dimmer.universe, dimmer.address) == (venue.device_id, 1, 1)
    assert house.group_address == HOUSE_GA and house.fade_mode == "hardware"
    assert side.fade_mode == "hardware"  # hardware_timed cannot be honoured: no column
    assert any("hardware_timed" in r.message for r in caplog.records)
    assert cfg.groups[venue.group] == {venue.dimmer, venue.rgb}
    assert cfg.power_on_colours == {venue.rgb: Colour(255, 100, 0)}


async def test_power_on_colour_is_seeded_but_a_restored_colour_wins(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    venue = await build_venue(db)
    row = await lighting_crud.get_channel(db, venue.rgbw)
    assert row is not None
    await lighting_crud.update_channel(
        db, venue.rgbw, row.updated_at, colour_r=1, colour_g=2, colour_b=3, colour_w=4
    )
    state.register_owner("lighting", "restore", allow_multiple=True)
    state.lighting.writer("restore").set_item("colour", venue.rgbw, {"r": 9, "g": 9, "b": 9})
    svc, _, _ = service(state, bus, db)
    await svc.start()
    try:
        assert state.lighting.get_item("colour", venue.rgb) == {"r": 255, "g": 100, "b": 0}
        assert state.lighting.get_item("colour", venue.rgbw) == {"r": 9, "g": 9, "b": 9}
    finally:
        await svc.stop()


async def test_a_config_change_event_reloads_and_clamps(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    venue = await build_venue(db)
    devices = FakeDevices(venue.device_id)
    devices.connect(venue.device_id)
    svc, _, _ = service(state, bus, db, devices=devices)
    await svc.start()
    try:
        svc.set_level(venue.dimmer, 90.0)
        row = await lighting_crud.get_channel(db, venue.dimmer)
        assert row is not None
        await lighting_crud.update_channel(db, venue.dimmer, row.updated_at, max_value=60.0)
        added = await lighting_crud.create_channel(
            db, name="Spare", type="dmx", profile_id=1, device_id=venue.device_id, address=20
        )
        bus.emit(LightingConfigChanged("channel edited"))
        await wait_until(lambda: added.id in svc.config.channels())
        assert state.lighting.get_item("levels", venue.dimmer) == 60.0  # clamped on reload
        svc.set_level(added.id, 100.0)
        output = devices.output(venue.device_id)
        await wait_until(lambda: output.last()[19] == 255 and output.last()[0] == 153)
    finally:
        await svc.stop()


async def test_the_lighting_domain_is_shared_by_every_writer(
    state: StateStore, bus: EventBus
) -> None:
    svc, _, _ = service(state, bus)
    await svc.start(LightingConfig())
    try:
        assert {"fade_engine", "lighting"} <= state.owners("lighting")
        state.register_owner("lighting", "artnet_input", allow_multiple=True)  # a later task
    finally:
        await svc.stop()


async def test_there_is_no_blackout_at_startup(state: StateStore, bus: EventBus) -> None:
    devices = FakeDevices()
    devices.connect()
    state.register_owner("lighting", "restore", allow_multiple=True)
    state.lighting.writer("restore").set_item("levels", 1, 75.0)  # as restored
    svc, _, _ = service(state, bus, devices=devices)
    await svc.start(config(dmx(1, 1)))
    try:
        await wait_until(lambda: len(devices.output().sent) == 1)
        assert devices.output().sent[0][2][0] == 191  # the restored look, never zeros first
    finally:
        await svc.stop()
