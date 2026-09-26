"""Stub mixer driver (§5.5 *Validating the abstraction*, §7.3, §18 Phase 4):
registration, honest capabilities before and after connect, the eight refs
with a Main, dB round trip / off / clamping against the published law, mutes,
the fader law's shape, the two unsupported operations, and the external-change
hook that drives change-origin tracking."""

from __future__ import annotations

import pytest

from proskenion.core.drivers import registry
from proskenion.core.drivers.base import ProbeResult
from proskenion.core.drivers.capabilities import ChannelState, MixerCapabilities, MixerChange
from proskenion.core.drivers.categories import Category
from proskenion.core.drivers.stub_mixer import (
    MAX_DB,
    MIN_DB,
    MixerCapabilityError,
    StubMixerDriver,
)
from proskenion.core.transport.loopback import LoopbackTransport
from tests.stubs.echo_driver import RecordingSink


def _driver(transport: LoopbackTransport | None = None) -> StubMixerDriver:
    return StubMixerDriver(1, transport or LoopbackTransport(), {}, RecordingSink())


# -- registration --------------------------------------------------------------


def test_registered_under_mixer_as_stub() -> None:
    assert registry.DRIVERS[(Category.MIXER, "stub")] is StubMixerDriver


def test_schema_carries_no_addressing_and_is_empty() -> None:
    # Eight channels is fixed (§18), not a configuration knob.
    assert StubMixerDriver.CONFIG_SCHEMA == []


async def test_registry_build_produces_a_ready_driver_over_loopback() -> None:
    config = {"transport": {"type": "loopback"}, "driver": {}}
    driver = await registry.build(4, Category.MIXER, "stub", config, RecordingSink())
    assert isinstance(driver, StubMixerDriver)
    assert isinstance(driver.transport, LoopbackTransport)


# -- capabilities before and after connect (B56) -------------------------------


def _assert_honest(caps: object) -> None:
    assert isinstance(caps, MixerCapabilities)
    assert caps.supports_scene_recall is False
    assert caps.supports_pan is False
    assert caps.supports_metering is False
    assert caps.meter_min_db is None
    assert caps.meter_max_db is None
    assert caps.meter_point is None
    assert caps.supports_mute is True
    assert caps.supports_dca is False
    assert caps.input_count == 6
    assert caps.output_count == 1
    assert caps.min_db == MIN_DB
    assert caps.max_db == MAX_DB


def test_capabilities_are_honest_before_connect() -> None:
    # The registry's "declared maximum" path never calls connect().
    caps = registry.declared_capabilities(StubMixerDriver)
    _assert_honest(caps)


async def test_capabilities_are_the_same_and_still_honest_after_connect() -> None:
    driver = _driver()
    await driver.connect()
    _assert_honest(driver.capabilities())


# -- eight refs, with a Main (§7.3 *Channels*) ---------------------------------


def test_available_refs_are_eight_with_human_labels() -> None:
    refs = _driver().available_refs()
    assert len(refs) == 8
    assert all(isinstance(r.label, str) and r.label for r in refs)


def test_available_refs_always_include_a_main() -> None:
    refs = {r.ref: r for r in _driver().available_refs()}
    assert "main" in refs
    main = refs["main"]
    assert main.kind == "main"
    assert main.stereo is True
    assert main.label == "Main"


def test_available_refs_have_six_inputs_and_one_output_besides_main() -> None:
    refs = _driver().available_refs()
    assert sum(1 for r in refs if r.kind == "input") == 6
    assert sum(1 for r in refs if r.kind == "output") == 1
    assert sum(1 for r in refs if r.kind == "main") == 1


# -- dB round trip, off, and clamping ------------------------------------------


async def test_set_level_round_trips_through_read_state() -> None:
    driver = _driver()
    await driver.set_level(["in1"], -12.5)
    state = (await driver.read_state(["in1"]))["in1"]
    assert state == ChannelState(db=-12.5, muted=False, pan=None)


async def test_set_level_none_is_off_and_round_trips_as_none() -> None:
    driver = _driver()
    await driver.set_level(["in1"], None)
    state = (await driver.read_state(["in1"]))["in1"]
    assert state.db is None


async def test_set_level_applies_to_every_ref_in_one_call() -> None:
    driver = _driver()
    await driver.set_level(["in1", "in2", "out1"], -6.0)
    state = await driver.read_state(["in1", "in2", "out1"])
    assert all(s.db == -6.0 for s in state.values())


async def test_set_level_clamps_above_and_below_the_laws_range() -> None:
    driver = _driver()
    await driver.set_level(["main"], MAX_DB + 50.0)
    assert (await driver.read_state(["main"]))["main"].db == MAX_DB
    await driver.set_level(["main"], MIN_DB - 50.0)
    assert (await driver.read_state(["main"]))["main"].db == MIN_DB


async def test_set_level_unknown_ref_raises_value_error() -> None:
    driver = _driver()
    with pytest.raises(ValueError, match="no channel"):
        await driver.set_level(["nope"], 0.0)


async def test_read_state_unknown_ref_raises_value_error() -> None:
    driver = _driver()
    with pytest.raises(ValueError, match="no channel"):
        await driver.read_state(["nope"])


async def test_read_state_pan_is_always_none() -> None:
    driver = _driver()
    for ref in ("main", "in1", "out1"):
        assert (await driver.read_state([ref]))[ref].pan is None


# -- mutes ----------------------------------------------------------------------


async def test_set_mute_round_trips() -> None:
    driver = _driver()
    await driver.set_mute(["in1", "in2"], True)
    state = await driver.read_state(["in1", "in2"])
    assert state["in1"].muted is True
    assert state["in2"].muted is True
    await driver.set_mute(["in1"], False)
    assert (await driver.read_state(["in1"]))["in1"].muted is False
    assert (await driver.read_state(["in2"]))["in2"].muted is True  # untouched


async def test_channels_start_unmuted_at_unity() -> None:
    driver = _driver()
    state = (await driver.read_state(["main"]))["main"]
    assert state.muted is False
    assert state.db == 0.0


# -- the fader law's shape, against what faderLaw.ts expects --------------------


def test_fader_law_has_an_off_point_at_the_bottom_of_travel() -> None:
    law = _driver().fader_law()
    bottom = min(law, key=lambda p: p.position)
    assert bottom.position == 0.0
    assert bottom.db is None  # off, not a number (§5.5)


def test_fader_law_has_exactly_one_unity_detent() -> None:
    law = _driver().fader_law()
    detents = [p for p in law if p.detent]
    assert len(detents) == 1
    assert detents[0].db == 0.0
    assert detents[0].label == "0"


def test_fader_law_positions_span_0_to_1_and_are_ascending() -> None:
    law = _driver().fader_law()
    positions = [p.position for p in law]
    assert positions == sorted(positions)
    assert positions[0] == 0.0
    assert positions[-1] == 1.0
    assert all(0.0 <= p <= 1.0 for p in positions)


def test_fader_law_top_of_travel_matches_capabilities_max_db() -> None:
    law = _driver().fader_law()
    top = max(law, key=lambda p: p.position)
    assert top.db == MAX_DB


def test_fader_law_has_at_least_one_point_with_a_printed_label() -> None:
    # faderLaw.ts's scaleTicks() reads points with a non-empty label; a law
    # with none would print no scale at all.
    law = _driver().fader_law()
    labelled = [p for p in law if p.label]
    assert len(labelled) >= 3


def test_fader_law_is_not_the_cq20bs_printed_scale() -> None:
    # The CQ-20B prints "+10 +5 0 -5 -10 -20 -30 -40 -inf" (spec §5.5) — this
    # stub must not be mistaken for it.
    law = _driver().fader_law()
    cq20b_labels = {"+10", "+5", "0", "-5", "-10", "-20", "-30", "-40", "-∞"}
    stub_labels = {p.label for p in law if p.label}
    assert stub_labels != cq20b_labels


def test_fader_law_returns_a_fresh_list_each_call() -> None:
    driver = _driver()
    first = driver.fader_law()
    first.clear()
    assert len(driver.fader_law()) > 0


# -- unsupported operations -------------------------------------------------------


async def test_recall_scene_raises_a_typed_capability_error() -> None:
    driver = _driver()
    with pytest.raises(MixerCapabilityError) as exc_info:
        await driver.recall_scene("1")
    assert exc_info.value.capability == "supports_scene_recall"


async def test_set_pan_raises_a_typed_capability_error() -> None:
    driver = _driver()
    with pytest.raises(MixerCapabilityError) as exc_info:
        await driver.set_pan("in1", 0.5)
    assert exc_info.value.capability == "supports_pan"


def test_capability_error_is_distinguishable_from_an_unknown_ref() -> None:
    # A caller must be able to tell "not supported" apart from "bad input".
    assert not issubclass(MixerCapabilityError, ValueError)


# -- probe / connect --------------------------------------------------------------


async def test_probe_is_alive_once_connected() -> None:
    driver = _driver()
    await driver.connect()
    assert await driver.probe() == ProbeResult(True)


async def test_probe_is_not_alive_before_connecting() -> None:
    driver = _driver()
    result = await driver.probe()
    assert result.alive is False


# -- change reporting (§7.3 *Change origin tracking*, MixerChange) ------------------


async def test_set_level_reports_an_app_change_once_applied() -> None:
    driver = _driver()
    calls: list[MixerChange] = []

    async def listener(change: MixerChange) -> None:
        calls.append(change)

    driver.add_change_listener(listener)
    await driver.set_level(["in1"], -3.0)

    assert calls == [MixerChange("in1", "level", -3.0, "app")]
    # Reported only once the store already reflects it.
    assert (await driver.read_state(["in1"]))["in1"].db == -3.0


async def test_set_level_reports_the_clamped_value_not_the_raw_one() -> None:
    driver = _driver()
    calls: list[MixerChange] = []

    async def listener(change: MixerChange) -> None:
        calls.append(change)

    driver.add_change_listener(listener)
    await driver.set_level(["main"], MAX_DB + 100.0)

    assert calls == [MixerChange("main", "level", MAX_DB, "app")]


async def test_set_level_reports_one_change_per_ref() -> None:
    driver = _driver()
    calls: list[MixerChange] = []

    async def listener(change: MixerChange) -> None:
        calls.append(change)

    driver.add_change_listener(listener)
    await driver.set_level(["in1", "in2"], -6.0)

    assert calls == [
        MixerChange("in1", "level", -6.0, "app"),
        MixerChange("in2", "level", -6.0, "app"),
    ]


async def test_set_mute_reports_an_app_change() -> None:
    driver = _driver()
    calls: list[MixerChange] = []

    async def listener(change: MixerChange) -> None:
        calls.append(change)

    driver.add_change_listener(listener)
    await driver.set_mute(["in1"], True)

    assert calls == [MixerChange("in1", "mute", True, "app")]


async def test_simulate_external_change_reports_a_level_change_as_external() -> None:
    driver = _driver()
    calls: list[MixerChange] = []

    async def listener(change: MixerChange) -> None:
        calls.append(change)

    driver.add_change_listener(listener)
    await driver.simulate_external_change("in1", db=-8.0)

    assert calls == [MixerChange("in1", "level", -8.0, "external")]
    # And the store itself was updated, exactly as set_level would update it.
    assert (await driver.read_state(["in1"]))["in1"].db == -8.0


async def test_simulate_external_change_reports_a_mute_change_as_external() -> None:
    driver = _driver()
    calls: list[MixerChange] = []

    async def listener(change: MixerChange) -> None:
        calls.append(change)

    driver.add_change_listener(listener)
    await driver.simulate_external_change("in1", muted=True)

    assert calls == [MixerChange("in1", "mute", True, "external")]


async def test_simulate_external_change_reports_both_fields_when_both_given() -> None:
    driver = _driver()
    calls: list[MixerChange] = []

    async def listener(change: MixerChange) -> None:
        calls.append(change)

    driver.add_change_listener(listener)
    await driver.simulate_external_change("in1", db=-4.0, muted=True)

    assert calls == [
        MixerChange("in1", "level", -4.0, "external"),
        MixerChange("in1", "mute", True, "external"),
    ]


async def test_simulate_external_change_leaves_unset_fields_untouched() -> None:
    driver = _driver()
    await driver.set_level(["in1"], -10.0)
    await driver.simulate_external_change("in1", muted=True)  # db left alone
    state = (await driver.read_state(["in1"]))["in1"]
    assert state.db == -10.0
    assert state.muted is True


async def test_simulate_external_change_clamps_like_set_level() -> None:
    driver = _driver()
    await driver.simulate_external_change("main", db=MAX_DB + 100.0)
    assert (await driver.read_state(["main"]))["main"].db == MAX_DB


async def test_simulate_external_change_unknown_ref_raises() -> None:
    driver = _driver()
    with pytest.raises(ValueError, match="no channel"):
        await driver.simulate_external_change("nope", db=0.0)


async def test_a_raising_change_listener_is_isolated() -> None:
    driver = _driver()
    calls: list[str] = []

    async def broken(change: MixerChange) -> None:
        raise RuntimeError("a listener that misbehaves")

    async def fine(change: MixerChange) -> None:
        calls.append(change.ref)

    driver.add_change_listener(broken)
    driver.add_change_listener(fine)
    await driver.simulate_external_change("in1", db=1.0)  # must not raise

    assert calls == ["in1"]


async def test_listeners_are_awaited_in_registration_order() -> None:
    driver = _driver()
    seen: list[int] = []

    async def first(change: MixerChange) -> None:
        seen.append(1)

    async def second(change: MixerChange) -> None:
        seen.append(2)

    driver.add_change_listener(first)
    driver.add_change_listener(second)
    await driver.simulate_external_change("in1", db=2.0)
    assert seen == [1, 2]
