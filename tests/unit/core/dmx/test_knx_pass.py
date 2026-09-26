"""The KNX pass and the §7.1 fade modes (spec §7.1, §7.2.3, §12.2).

A ``hardware`` dimmer runs its own fade, so it is sent a fade's target once; a
``software`` dimmer is stepped at no more than ten values a second. Following
the level store's 20 ms interpolation instead would spend the building's whole
15-telegram-a-second budget on one fade.
"""

from __future__ import annotations

import asyncio

import pytest

from proskenion.core.dmx.compositor import KNX_DIMMER_PRIORITY
from proskenion.core.dmx.fade import FadeEngine
from proskenion.core.dmx.renderer import KnxDimmerPass
from proskenion.core.state import Change, StateStore
from tests.unit.core.dmx.conftest import (
    Pipeline,
    Rig,
    SyncKnx,
    config,
    dmx,
    knx,
    wait_until,
)

GA = "1/1/1"


async def test_a_two_second_fade_on_a_hardware_channel_sends_the_target_once(
    state: StateStore,
) -> None:
    pipe = Pipeline(state, config(knx(1, GA, "hardware")))
    store_writes: list[object] = []
    state.add_listener(lambda c: store_writes.append(c.new) if c.field == "levels" else None)
    await pipe.start()
    try:
        loop = asyncio.get_running_loop()
        start = loop.time()
        handle = pipe.fades.fade_channel(1, level=80.0, fade_ms=2000)
        await handle.wait()
        await asyncio.sleep(0.15)
        assert pipe.knx.values() == [80.0]  # the target, once
        assert pipe.knx.writes[0][0] - start < 0.05  # when the fade began
        assert len(store_writes) > 50  # while the store interpolated for the interface
    finally:
        await pipe.stop()


async def test_moving_a_group_fader_sends_nothing_to_a_dimmer(state: StateStore) -> None:
    # §9.4, §9.5: group faders scale stage (DMX) members only.
    rig = config(
        dmx(3, 1), knx(1, GA, "hardware"), knx(2, "1/1/2", "software"), groups={4: {1, 2, 3}}
    )
    pipe = Pipeline(state, rig)
    for channel_id in (1, 2, 3):
        pipe.rig.set_level(channel_id, 100.0)
    pipe.rig.compositor.baseline_knx()  # the dimmers are at 100, as discovered
    await pipe.start()
    try:
        await pipe.fades.fade_group(4, 0.5, fade_ms=300).wait()
        pipe.fades.fade_group(4, 0.2)  # and a direct move
        await wait_until(lambda: pipe.output.last()[0] == 51)  # the stage member follows
        await asyncio.sleep(0.15)
        assert pipe.knx.writes == []
    finally:
        await pipe.stop()


async def test_a_two_second_fade_on_a_software_channel_steps_no_faster_than_ten_a_second(
    state: StateStore,
) -> None:
    pipe = Pipeline(state, config(knx(1, GA, "software")))
    await pipe.start()
    try:
        await pipe.fades.fade_channel(1, level=100.0, fade_ms=2000).wait()
        await asyncio.sleep(0.25)
        times = [t for t, _, _, _ in pipe.knx.writes]
        values = pipe.knx.values()
        gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
        assert min(gaps) >= 0.099  # never faster than ten a second
        assert 10 <= len(values) <= 22  # stepped, not the target alone, not every tick
        assert values == sorted(values)
        assert values[-1] == 100.0  # the exact final value lands
        assert all(p == KNX_DIMMER_PRIORITY for _, _, _, p in pipe.knx.writes)
    finally:
        await pipe.stop()


async def test_a_software_fade_to_zero_lands_on_zero(state: StateStore) -> None:
    pipe = Pipeline(state, config(knx(1, GA, "software")))
    pipe.rig.set_level(1, 100.0)
    pipe.rig.compositor.baseline_knx()
    await pipe.start()
    try:
        await pipe.fades.fade_channel(1, level=0.0, fade_ms=600).wait()
        await wait_until(lambda: pipe.knx.values()[-1:] == [0.0])
    finally:
        await pipe.stop()


class ManualClock:
    def __init__(self) -> None:
        self.now = 50.0

    def __call__(self) -> float:
        return self.now


def test_the_deadband_holds_back_small_steps_while_a_software_fade_moves(
    state: StateStore,
) -> None:
    rig = Rig(state, config(knx(1, GA, "software")))
    clock = ManualClock()
    fades = FadeEngine(state, owner="fade_engine_manual", clock=clock)
    fades.configure({1: (0.0, 100.0)}, ())
    rig.compositor._destinations = fades  # this test's engine owns the fade
    fades.fade_channel(1, level=100.0, fade_ms=10_000)

    clock.now += 0.2  # smoothstep(0.02) → 0.1 %: inside the deadband
    fades.step()
    assert rig.compositor.composite_knx(clock.now).writes == ()
    clock.now += 0.3  # smoothstep(0.05) → 0.7 %: outside it
    fades.step()
    assert [w.value for w in rig.compositor.composite_knx(clock.now).writes] == [0.7]


def test_a_settled_value_is_sent_even_inside_the_deadband(state: StateStore) -> None:
    rig = Rig(state, config(knx(1, GA, "software"), knx(2, "1/1/2", "hardware")))
    rig.set_level(1, 50.0)
    rig.set_level(2, 50.0)
    rig.compositor.baseline_knx()
    rig.fades.fade_channel(1, level=50.3)
    rig.fades.fade_channel(2, level=50.3)
    writes = rig.compositor.composite_knx(now=1.0).writes
    assert sorted(w.value for w in writes) == [50.3, 50.3]


def test_loading_configuration_writes_nothing_to_a_dimmer(state: StateStore) -> None:
    rig = Rig(state)
    rig.set_level(1, 65.0)  # restored at boot
    rig.set_level(2, 30.0)
    rig.configure(config(knx(1, GA)))
    assert rig.compositor.composite_knx(now=1.0).writes == ()
    rig.configure(config(knx(1, GA), knx(2, "1/1/2")))  # a channel added later
    assert rig.compositor.composite_knx(now=2.0).writes == ()


async def test_a_fader_drag_across_a_dimmer_is_rate_limited_and_lands(state: StateStore) -> None:
    pipe = Pipeline(state, config(knx(1, GA, "hardware")))
    await pipe.start()
    try:
        loop = asyncio.get_running_loop()
        start = loop.time()
        for step in range(1, 51):
            pipe.fades.fade_channel(1, level=step * 1.5)
            await asyncio.sleep(0.01)
        elapsed = loop.time() - start
        await wait_until(lambda: pipe.knx.values()[-1:] == [75.0])
        assert len(pipe.knx.writes) <= elapsed * 10 + 2
    finally:
        await pipe.stop()


async def test_moving_the_master_sends_nothing_to_a_dimmer(state: StateStore) -> None:
    # §9.5: the master scales stage (DMX) output only.
    pipe = Pipeline(state, config(dmx(3, 1), knx(1, GA, "hardware"), knx(2, "1/1/2", "software")))
    pipe.rig.set_level(1, 80.0)
    pipe.rig.set_level(2, 60.0)
    pipe.rig.set_level(3, 100.0)
    pipe.rig.compositor.baseline_knx()
    await pipe.start()
    try:
        pipe.rig.set_master(50.0)
        await wait_until(lambda: pipe.output.last()[0] == 128)  # the stage follows
        pipe.rig.set_master(0.0)
        await wait_until(lambda: pipe.output.last()[0] == 0)
        await asyncio.sleep(0.15)
        assert pipe.knx.writes == []
    finally:
        await pipe.stop()


def test_a_reported_dimmer_level_is_not_echoed_back(state: StateStore) -> None:
    rig = Rig(state, config(knx(1, GA)))
    rig.fades.fade_channel(1, level=70.0, force=True)  # the panel moved it; status sync wrote it
    rig.compositor.baseline_knx([1])
    assert rig.compositor.composite_knx(now=1.0).writes == ()


def test_the_knx_pass_ignores_dmx_suspension(state: StateStore) -> None:
    rig = Rig(state, config(dmx(1, 1), knx(2, GA)))
    rig.compositor.suspend_dmx(True)
    rig.set_level(2, 35.0)
    assert [w.value for w in rig.compositor.composite_knx(now=1.0).writes] == [35.0]


async def test_a_knx_subsystem_that_enqueues_synchronously_is_accepted(
    state: StateStore,
) -> None:
    rig = Rig(state, config(knx(1, GA)))
    sink = SyncKnx()
    runner = KnxDimmerPass(rig.compositor, state, sink, fades=rig.fades)
    await runner.start()
    try:
        rig.fades.fade_channel(1, level=12.5)
        await wait_until(lambda: sink.writes == [(GA, 12.5, KNX_DIMMER_PRIORITY)])
    finally:
        await runner.stop()


async def test_a_failed_knx_write_is_logged_and_the_pass_carries_on(
    state: StateStore, caplog: pytest.LogCaptureFixture
) -> None:
    rig = Rig(state, config(knx(1, GA)))
    calls: list[float] = []

    class Flaky:
        def write(self, group_address: str, value: float, *, priority: int) -> None:
            calls.append(value)
            if len(calls) == 1:
                raise ConnectionError("knxd socket closed")

    runner = KnxDimmerPass(rig.compositor, state, Flaky(), fades=rig.fades)
    await runner.start()
    try:
        rig.fades.fade_channel(1, level=10.0)
        await wait_until(lambda: len(calls) == 1)
        await asyncio.sleep(0.12)
        rig.fades.fade_channel(1, level=20.0)
        await wait_until(lambda: calls == [10.0, 20.0])
    finally:
        await runner.stop()
    assert any("KNX dimmer write failed" in r.message for r in caplog.records)


def test_only_changes_to_knx_inputs_wake_the_pass(state: StateStore) -> None:
    rig = Rig(state, config(dmx(1, 1), knx(2, GA), groups={5: {1}, 6: {2}}))
    runner = KnxDimmerPass(rig.compositor, state, SyncKnx())

    def woke(field_name: str, item: str | None) -> bool:
        runner._wake.clear()
        runner._on_change(Change("lighting", field_name, item, None, 1.0))
        return runner._wake.is_set()

    assert woke("levels", "2")
    assert not woke("levels", "1") and not woke("group_multipliers", "5")
    assert not woke("colour", "2")
    # Groups and the master do not scale a house dimmer (§9.5), so they are not its inputs.
    assert not woke("group_multipliers", "6") and not woke("master", None)
