"""Compositor arithmetic, the value scale and profile-driven output (spec §7.2.3, §9.2, §22.2)."""

from __future__ import annotations

import pytest

from proskenion.core.dmx.compositor import (
    Colour,
    level_to_dmx,
    level_to_knx,
    resolve_level,
)
from proskenion.core.state import StateStore
from tests.unit.core.dmx.conftest import (
    DEVICE,
    DIMMER,
    MOVING_HEAD,
    RGB,
    RGBAU,
    Rig,
    config,
    dmx,
    knx,
)

# -- the value scale: 0–100 in, 0–255 (DMX) or 0–100 (KNX) out -----------------


@pytest.mark.parametrize(
    ("level", "dmx_value"),
    [(0.0, 0), (0.1, 0), (0.2, 1), (1.0, 3), (50.0, 128), (78.5, 200), (99.9, 255), (100.0, 255)],
)
def test_value_scale_dmx_converts_0_100_to_0_255(level: float, dmx_value: int) -> None:
    assert level_to_dmx(level) == dmx_value


def test_value_scale_knx_stays_on_0_100_with_one_decimal() -> None:
    assert level_to_knx(78.5) == 78.5
    assert level_to_knx(33.333) == 33.3
    assert level_to_knx(120.0) == 100.0
    assert level_to_knx(-3.0) == 0.0


def test_value_scale_through_both_passes(state: StateStore) -> None:
    rig = Rig(state, config(dmx(1, 1), knx(2, "1/1/2")))
    rig.set_level(1, 78.5)
    rig.set_level(2, 78.5)
    rig.compositor.composite_dmx()
    rig.compositor.baseline_knx()
    rig.set_level(2, 40.0)
    assert rig.frame()[0] == 200  # 78.5 % of 255, rounded
    writes = rig.compositor.composite_knx(now=10.0).writes
    assert [(w.group_address, w.value) for w in writes] == [("1/1/2", 40.0)]


# -- compositor arithmetic -----------------------------------------------------


def test_arithmetic_output_is_level_times_master_whatever_the_groups(state: StateStore) -> None:
    # Owner decision 2026-09-30: a group fader sets levels, so groups are no
    # input to the compositor at all; a fixture in two groups lands at its
    # own level × the master.
    rig = Rig(state, config(dmx(1, 1), knx(2), groups={10: {1, 2}, 11: {1, 2}}))
    rig.set_level(1, 80.0)
    rig.set_level(2, 80.0)
    rig.set_master(50.0)
    ch1, ch2 = rig.compositor.config.dmx_channels[0], rig.compositor.config.knx_channels[0]
    assert rig.compositor.resolve(ch1) == pytest.approx(40.0)
    assert rig.compositor.resolve(ch2) == 80.0  # the master does not scale a house dimmer (§9.5)


def test_the_store_has_no_group_input_for_the_compositor(state: StateStore) -> None:
    rig = Rig(state, config(dmx(1, 1), groups={10: {1}}))
    assert "group_multipliers" not in state.lighting.SPECS
    assert not rig.compositor.touches_dmx("group_multipliers", "10")


def test_arithmetic_clamps_before_scaling() -> None:
    # §7.2.3 Clamp point: max_value 80 with the master at 50 % outputs 40.
    assert resolve_level(100.0, min_value=0, max_value=80, master=50) == 40.0


def test_arithmetic_min_value_above_zero_is_exempt_from_the_master() -> None:
    kwargs = {"min_value": 20.0, "max_value": 100.0, "master": 5.0}
    assert resolve_level(60.0, **kwargs) == 60.0
    assert resolve_level(3.0, **kwargs) == 20.0  # the floor, applied to the output


def test_arithmetic_min_value_zero_scales_by_the_master() -> None:
    assert resolve_level(80.0, min_value=0, max_value=100, master=50) == 40.0


def test_a_channel_in_a_group_lands_at_its_own_level(state: StateStore) -> None:
    rig = Rig(state, config(dmx(1, 1), groups={10: {1}}))
    rig.set_level(1, 50.0)
    assert rig.compositor.composited_level(1) == 50.0


# -- profile-driven output (§7.2.3 Colour, §9.1) --------------------------------


def test_colour_components_are_scaled_by_the_composited_level(state: StateStore) -> None:
    rig = Rig(state, config(dmx(1, 1, RGB)))
    rig.set_level(1, 50.0)
    rig.set_master(50.0)
    rig.set_colour(1, r=255, g=120, b=0)
    rig.compositor.composite_dmx()
    assert list(rig.frame()[:3]) == [64, 30, 0]  # 25 % of each


def test_colour_roles_without_a_stored_component_use_the_profile_default(
    state: StateStore,
) -> None:
    # The store's colour model is r/g/b/w (§5.6, §8.12); amber and uv take the
    # profile default, still scaled by the level.
    rig = Rig(state, config(dmx(1, 1, RGBAU)))
    rig.set_level(1, 50.0)
    rig.set_colour(1, r=200, g=100, b=50)
    rig.compositor.composite_dmx()
    assert list(rig.frame()[:5]) == [100, 50, 25, 100, 50]


def test_positional_roles_are_written_unscaled_from_their_defaults(state: StateStore) -> None:
    rig = Rig(state, config(dmx(1, 1, MOVING_HEAD)))
    rig.set_level(1, 100.0)
    rig.set_colour(1, r=255, g=255, b=255)
    rig.set_master(20.0)
    rig.compositor.composite_dmx()
    frame = rig.frame()
    assert frame[0] == 51  # dimmer: 20 %
    assert (frame[1], frame[2], frame[3], frame[4]) == (128, 100, 0, 7)  # pan, tilt, strobe, macro
    assert frame[8] == 0  # unused stays at zero, whatever its default


def test_a_dimmer_and_colour_fixture_dims_at_its_dimmer_alone(
    state: StateStore,
) -> None:
    # §7.2.3 Colour: the dimmer slot carries the level and the colour slots
    # carry pure colour, so 50 % on the fader is 50 % of light, not 25 %.
    rig = Rig(state, config(dmx(1, 1, MOVING_HEAD)))
    rig.set_level(1, 50.0)
    rig.set_colour(1, r=200, g=0, b=0)
    rig.compositor.composite_dmx()
    assert rig.frame()[0] == 128 and rig.frame()[5] == 200


def test_fixtures_land_at_their_own_address_and_unpatched_slots_stay_zero(
    state: StateStore,
) -> None:
    rig = Rig(state, config(dmx(1, 10), dmx(2, 512)))
    rig.set_level(1, 100.0)
    rig.set_level(2, 100.0)
    rig.compositor.composite_dmx()
    frame = rig.frame()
    assert frame[9] == 255 and frame[511] == 255
    assert sum(frame) == 510


def test_a_fixture_patched_past_slot_512_is_clipped_not_fatal(state: StateStore) -> None:
    rig = Rig(state, config(dmx(1, 511, RGB)))
    rig.set_level(1, 100.0)
    rig.set_colour(1, r=10, g=20, b=30)
    rig.compositor.composite_dmx()
    assert list(rig.frame()[510:512]) == [10, 20]


def test_each_device_and_universe_has_its_own_buffer(state: StateStore) -> None:
    rig = Rig(state, config(dmx(1, 1), dmx(2, 1, universe=2), dmx(3, 1, device=2)))
    for channel_id, level in ((1, 100.0), (2, 50.0), (3, 20.0)):
        rig.set_level(channel_id, level)
    rig.compositor.composite_dmx()
    assert rig.frame(1, DEVICE)[0] == 255
    assert rig.frame(2, DEVICE)[0] == 128
    assert rig.frame(1, 2)[0] == 51
    assert rig.compositor.output_devices() == {DEVICE, 2}


def test_a_removed_fixture_leaves_its_slots_at_zero(state: StateStore) -> None:
    rig = Rig(state, config(dmx(1, 1), dmx(2, 2)))
    rig.set_level(1, 100.0)
    rig.set_level(2, 100.0)
    rig.compositor.composite_dmx()
    rig.configure(config(dmx(1, 1)))
    rig.compositor.composite_dmx()
    assert list(rig.frame()[:2]) == [255, 0]


def test_the_ghost_mark_is_the_composited_level(state: StateStore) -> None:
    # §9.4: the thumb is what was set, the ghost is where the fixture lands.
    rig = Rig(state, config(dmx(1, 1, DIMMER)))
    rig.set_level(1, 85.0)
    rig.set_master(50.0)
    assert rig.compositor.composited_level(1) == 42.5
    assert rig.state.lighting.get_item("levels", 1) == 85.0  # the thumb does not move


def test_the_ghost_mark_of_a_house_dimmer_ignores_the_master(state: StateStore) -> None:
    # §9.5: a KNX house dimmer lands at its own level, which is what it is sent.
    rig = Rig(state, config(knx(2, max_value=80.0), groups={10: {2}}))
    rig.set_level(2, 85.0)
    rig.set_master(50.0)
    assert rig.compositor.composited_level(2) == 80.0  # its clamp and nothing else


def test_colour_lerp_interpolates_every_component_from_the_start() -> None:
    start, end = Colour(255, 0, 0, 0), Colour(0, 0, 255, 100)
    assert start.lerp(end, 0.0) == start
    assert start.lerp(end, 1.0) == end
    assert start.lerp(end, 0.5) == Colour(128, 0, 128, 50)


def test_a_colour_write_without_white_keeps_the_current_white() -> None:
    assert Colour(1, 2, 3).with_white_from(Colour(9, 9, 9, 40)) == Colour(1, 2, 3, 40)
    assert Colour(1, 2, 3, 5).with_white_from(Colour(9, 9, 9, 40)) == Colour(1, 2, 3, 5)
