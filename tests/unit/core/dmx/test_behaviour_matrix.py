"""Every row of the §7.2.3 behaviour matrix, one test per row (§22.2).

The matrix exists to make omissions visible: blockers B1 and B4 both came from
a compositor that quietly accumulated cases. Each test is named for its row,
in the order the specification lists them, and exercises both passes where
the row has a KNX column.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from proskenion.core.bus import EventBus
from proskenion.core.dmx.compositor import KNX_DIMMER_PRIORITY, ProfileSlot
from proskenion.core.lighting import LightingService
from proskenion.core.state import StateStore
from tests.unit.core.dmx.conftest import (
    RGBW,
    FakeDevices,
    FakeKnx,
    Pipeline,
    Rig,
    config,
    dmx,
    knx,
    wait_until,
)

KNX_GA = "1/2/3"
#: §15.9's common LED fixture shape: a dimmer channel as well as colour channels.
DIMMER_RGBWAU: tuple[ProfileSlot, ...] = (
    ProfileSlot(0, "dimmer"),
    ProfileSlot(1, "red"),
    ProfileSlot(2, "green"),
    ProfileSlot(3, "blue"),
    ProfileSlot(4, "white"),
    ProfileSlot(5, "amber", 200),
    ProfileSlot(6, "uv", 100),
)


@pytest.fixture
async def pipe(state: StateStore) -> AsyncIterator[Pipeline]:
    pipeline = Pipeline(state, config(dmx(1, 1), dmx(2, 2, RGBW), knx(3, KNX_GA)), keepalive_s=0.1)
    await pipeline.start()
    await wait_until(lambda: len(pipeline.output.sent) >= 1)
    try:
        yield pipeline
    finally:
        await pipeline.stop()


# Row 1 ------------------------------------------------------------------------


async def test_row_normal_operation_composites_both_passes(pipe: Pipeline) -> None:
    pipe.fades.fade_channel(1, level=100.0)
    pipe.fades.fade_channel(3, level=60.0)
    await wait_until(lambda: pipe.output.last()[0] == 255)
    await wait_until(lambda: pipe.knx.values(KNX_GA) == [60.0])


# Row 2 ------------------------------------------------------------------------


async def test_row_external_control_active_suspends_dmx_and_never_knx(pipe: Pipeline) -> None:
    before = pipe.rig.compositor.frames()
    sent = len(pipe.output.sent)
    pipe.renderer.suspend()

    pipe.fades.fade_channel(1, level=100.0)
    pipe.fades.fade_channel(3, level=45.0)
    await wait_until(lambda: pipe.knx.values(KNX_GA) == [45.0])  # KNX runs normally
    await asyncio.sleep(0.35)  # three keepalive intervals

    assert len(pipe.output.sent) == sent  # nothing sent: no frame, no keepalive
    assert pipe.rig.compositor.composite_dmx() is False  # suspended — nothing written
    assert pipe.rig.compositor.frames() == before


# Row 3 ------------------------------------------------------------------------


async def test_row_channel_is_a_dimmer_fixture(pipe: Pipeline) -> None:
    pipe.fades.fade_channel(1, level=50.0)
    pipe.fades.fade_channel(3, level=50.0)
    await wait_until(lambda: pipe.output.last()[0] == 128)
    assert sum(pipe.output.last()) == 128  # a single DMX slot
    await wait_until(lambda: len(pipe.knx.writes) == 1)
    _, group_address, value, priority = pipe.knx.writes[0]
    # A DPT 5.001 write: the KNX subsystem is handed 0–100 and converts at its boundary.
    assert (group_address, value, priority) == (KNX_GA, 50.0, KNX_DIMMER_PRIORITY)


# Row 4 ------------------------------------------------------------------------


def test_row_channel_is_rgb_rgbw_without_a_dimmer_level_scales_each_component(
    state: StateStore,
) -> None:
    rig = Rig(state, config(dmx(2, 1, RGBW), knx(3, KNX_GA)))
    rig.compositor.baseline_knx()
    rig.set_level(2, 50.0)
    rig.set_colour(2, r=200, g=100, b=50, w=20)
    rig.compositor.composite_dmx()
    assert list(rig.frame()[:4]) == [100, 50, 25, 10]  # all four written, each scaled
    # n/a for KNX: a colour on a dimmer channel produces no dimmer write.
    rig.set_colour(3, r=255, g=0, b=0)
    assert rig.compositor.composite_knx(now=5.0).writes == ()


def test_row_channel_is_rgb_rgbw_with_a_dimmer_the_dimmer_carries_the_level_alone(
    state: StateStore,
) -> None:
    rig = Rig(state, config(dmx(2, 1, DIMMER_RGBWAU), groups={9: {2}}))
    rig.set_level(2, 80.0)
    rig.set_group(9, 0.5)
    rig.set_master(50.0)
    rig.set_colour(2, r=200, g=100, b=50, w=20)
    rig.compositor.composite_dmx()
    frame = list(rig.frame()[:7])
    assert frame[0] == 51  # the dimmer: 80 × group 0.5 × master 50 % = 20 %
    # Colour unscaled: r, g, b and w as stored, amber and UV at their profile default.
    assert frame[1:] == [200, 100, 50, 20, 200, 100]


# Row 5 ------------------------------------------------------------------------


async def test_row_colour_changed_level_unchanged_is_recomposited(pipe: Pipeline) -> None:
    from proskenion.core.dmx.compositor import Colour

    pipe.fades.fade_channel(2, level=100.0, colour=Colour(10, 20, 30, 40))
    await wait_until(lambda: list(pipe.output.last()[1:5]) == [10, 20, 30, 40])
    composites = pipe.renderer.composites
    pipe.fades.fade_channel(2, colour=Colour(200, 0, 0, 0))  # level untouched
    await wait_until(lambda: list(pipe.output.last()[1:5]) == [200, 0, 0, 0])
    assert pipe.renderer.composites > composites
    assert pipe.rig.state.lighting.get_item("levels", 2) == 100.0


# Row 6 ------------------------------------------------------------------------


def test_row_min_value_zero_dmx_clamp_group_master_knx_clamp_only(state: StateStore) -> None:
    rig = Rig(
        state,
        config(dmx(1, 1, max_value=80.0), knx(3, KNX_GA, max_value=80.0), groups={9: {1, 3}}),
    )
    rig.compositor.baseline_knx()
    for channel_id in (1, 3):
        rig.set_level(channel_id, 100.0)
    rig.set_group(9, 0.5)
    rig.set_master(50.0)
    rig.compositor.composite_dmx()
    assert rig.frame()[0] == 51  # 100 → clamp 80 → × 0.5 → × 0.5 = 20 %
    # Group faders and the master do not scale a house dimmer (§9.5): clamp only.
    assert [w.value for w in rig.compositor.composite_knx(now=5.0).writes] == [80.0]


# Row 7 ------------------------------------------------------------------------


def test_row_min_value_above_zero_clamp_only_exempt(state: StateStore) -> None:
    rig = Rig(
        state,
        config(dmx(1, 1, min_value=30.0), knx(3, KNX_GA, min_value=30.0), groups={9: {1, 3}}),
    )
    rig.compositor.baseline_knx()
    rig.set_group(9, 0.2)
    rig.set_master(10.0)
    rig.set_level(1, 60.0)
    rig.set_level(3, 60.0)
    rig.compositor.composite_dmx()
    assert rig.frame()[0] == 153  # 60 %, untouched by group and master
    # Clamp only, as for every house dimmer.
    assert [w.value for w in rig.compositor.composite_knx(now=5.0).writes] == [60.0]
    rig.set_level(1, 10.0)
    rig.set_level(3, 10.0)
    rig.compositor.composite_dmx()
    assert rig.frame()[0] == 77  # the 30 % floor, applied to the output
    assert [w.value for w in rig.compositor.composite_knx(now=6.0).writes] == [30.0]


# Row 8 ------------------------------------------------------------------------


async def test_row_recalled_by_a_binding_rule_dmx_forced_to_full_knx_set_directly(
    state: StateStore, bus: EventBus
) -> None:
    devices = FakeDevices()
    devices.connect()
    sink = FakeKnx()
    lighting = LightingService(state, bus, None, devices, sink, keepalive_s=0.1)
    await lighting.start(config(dmx(1, 1), knx(3, KNX_GA), groups={7: {1, 3}}))
    try:
        # A bank left at 40 % from the night before; the wall panel switches it on.
        lighting.set_group_multiplier(7, 0.4)
        lighting.set_master(50.0)
        lighting.recall_group(7, 100.0)  # the binding rule's recall
        # DMX: the multiplier is forced to 1.0 by a write, and the master applies.
        assert state.lighting.get_item("group_multipliers", 7) == 1.0
        await wait_until(lambda: devices.output().last()[0] == 128)
        # KNX: the level is set directly; no multiplier applies, the master included.
        await wait_until(lambda: sink.values(KNX_GA) == [100.0])
        # And with the master at full the stage is exactly on_level (§22.2),
        # while the house dimmer is sent nothing more.
        lighting.set_master(100.0)
        await wait_until(lambda: devices.output().last()[0] == 255)
        await asyncio.sleep(0.15)
        assert sink.values(KNX_GA) == [100.0]
    finally:
        await lighting.stop()


# Row 9 ------------------------------------------------------------------------


def test_row_channel_in_several_groups_highest_multiplier_applies(state: StateStore) -> None:
    rig = Rig(state, config(dmx(1, 1), knx(3, KNX_GA), groups={1: {1, 3}, 2: {1, 3}}))
    rig.compositor.baseline_knx()
    rig.set_level(1, 100.0)
    rig.set_level(3, 100.0)
    rig.set_group(1, 0.3)
    rig.set_group(2, 0.7)
    rig.compositor.composite_dmx()
    assert rig.frame()[0] == 179  # 70 %, not 21 %
    # n/a for KNX: groups do not scale a house dimmer, so it is sent its own level.
    assert [w.value for w in rig.compositor.composite_knx(now=5.0).writes] == [100.0]


# Row 10 -----------------------------------------------------------------------


async def test_row_level_unchanged_since_last_frame_no_frame_no_telegram(
    pipe: Pipeline,
) -> None:
    pipe.fades.fade_channel(1, level=40.0)
    pipe.fades.fade_channel(3, level=40.0)
    await wait_until(lambda: pipe.output.last()[0] == 102 and len(pipe.knx.writes) == 1)
    composites = pipe.renderer.composites
    frames = pipe.output.frames()

    pipe.fades.fade_channel(1, level=40.0)  # the same value again: the store is unchanged
    pipe.fades.fade_channel(3, level=40.0)
    await asyncio.sleep(0.25)

    assert pipe.renderer.composites == composites  # the compositor did not run
    resent = pipe.output.frames()[len(frames) :]
    assert resent and all(frame == frames[-1] for frame in resent)  # keepalive only
    assert len(pipe.knx.writes) == 1  # no telegram


# Row 11 -----------------------------------------------------------------------


async def test_row_level_changed_frame_sent_capped_and_telegram_at_priority_3(
    pipe: Pipeline,
) -> None:
    loop = asyncio.get_running_loop()
    start = loop.time()
    sent = len(pipe.output.sent)
    level = 0.0
    while loop.time() - start < 0.5:  # a fader dragged continuously for half a second
        level = (level + 1.3) % 100
        pipe.fades.fade_channel(1, level=level)
        pipe.fades.fade_channel(3, level=level)
        await asyncio.sleep(0.001)
    elapsed = loop.time() - start
    await asyncio.sleep(0.05)

    frames = pipe.output.sent[sent:]
    assert frames, "a changed level sends a frame"
    assert len(frames) <= elapsed * 40 + 2  # capped at 40 fps
    assert pipe.knx.writes and all(w[3] == KNX_DIMMER_PRIORITY for w in pipe.knx.writes)
