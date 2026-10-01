"""Fade engine: timing, easing, cancellation, clamping, colour and precedence (spec §7.2.6, §10.6).

Most tests drive the engine with a manual clock and call ``step`` themselves,
so every value is exact. The timing tests run the real ticker: against a
simulated loaded event loop (deterministic, and the acceptance test), and
against the real event loop with real blocking load.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from proskenion.core.dmx.compositor import Colour
from proskenion.core.dmx.fade import (
    ChannelLockedError,
    FadeEngine,
    SceneRun,
    UnknownChannelError,
    smoothstep,
)
from proskenion.core.state import Change, StateStore
from tests.unit.core.dmx.conftest import (
    RGB,
    Pipeline,
    Rig,
    VirtualClock,
    config,
    dmx,
    wait_until,
)


class ManualClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def engine(state: StateStore, *channels: int, **ranges: float) -> tuple[FadeEngine, ManualClock]:
    clock = ManualClock()
    fades = FadeEngine(state, clock=clock)
    lo, hi = ranges.get("min_value", 0.0), ranges.get("max_value", 100.0)
    fades.configure({c: (lo, hi) for c in channels})
    return fades, clock


def level(state: StateStore, channel_id: int) -> object:
    return state.lighting.get_item("levels", channel_id)


def record_levels(
    state: StateStore, clock: ManualClock | VirtualClock
) -> list[tuple[float, int, float]]:
    writes: list[tuple[float, int, float]] = []

    def listener(change: Change) -> None:
        if change.domain == "lighting" and change.field == "levels" and change.item is not None:
            assert isinstance(change.new, float)
            writes.append((clock(), int(change.item), change.new))

    state.add_listener(listener)
    return writes


# -- easing, direct sets, clamping -------------------------------------------


def test_easing_is_smoothstep() -> None:
    assert [smoothstep(t) for t in (0.0, 0.25, 0.5, 0.75, 1.0)] == [0.0, 0.15625, 0.5, 0.84375, 1.0]
    assert smoothstep(-1.0) == 0.0 and smoothstep(2.0) == 1.0


def test_a_direct_set_is_a_zero_duration_fade(state: StateStore) -> None:
    fades, _ = engine(state, 1)
    handle = fades.fade_channel(1, level=42.5)
    assert level(state, 1) == 42.5
    assert handle.outcome == "completed" and fades.active_fades() == 0


def test_levels_are_clamped_on_write_and_the_handle_carries_the_clamped_target(
    state: StateStore,
) -> None:
    fades, clock = engine(state, 1, max_value=80.0)
    writes = record_levels(state, clock)
    handle = fades.fade_channel(1, level=100.0, fade_ms=1000)
    assert handle.target_level == 80.0
    for _ in range(12):
        clock.now += 0.1
        fades.step()
    assert level(state, 1) == 80.0  # a fade to 100 on a channel capped at 80 stores 80
    assert all(value <= 80.0 for _, _, value in writes)
    assert all(round(value, 1) == value for _, _, value in writes)  # one decimal


def test_unknown_targets_and_bad_values_are_refused(state: StateStore) -> None:
    fades, _ = engine(state, 1)
    with pytest.raises(UnknownChannelError):
        fades.fade_channel(9, level=1.0)
    with pytest.raises(ValueError):
        fades.fade_channel(1, level=float("nan"))
    with pytest.raises(ValueError):
        fades.fade_channel(1)


# -- cancellation ------------------------------------------------------------


def test_a_new_fade_starts_from_the_current_value_with_no_snap_back(state: StateStore) -> None:
    fades, clock = engine(state, 1)
    first = fades.fade_channel(1, level=100.0, fade_ms=1000)
    clock.now += 0.5
    fades.step()
    assert level(state, 1) == 50.0
    clock.now += 0.1  # between ticks: the old fade has moved on since it last wrote

    writes = record_levels(state, clock)
    second = fades.fade_channel(1, level=0.0, fade_ms=1000)

    # Cancelled at its value *now* (smoothstep(0.6) = 0.648), not at its last
    # tick (50) and not back at its start (0) or on to its target (100).
    assert level(state, 1) == 64.8
    assert first.outcome == "cancelled" and first.finished_at == clock.now
    assert fades.active_fades() == 1  # at most one fade per channel
    for _ in range(11):
        clock.now += 0.1
        fades.step()
    values = [64.8] + [value for _, _, value in writes]
    assert values == sorted(values, reverse=True)  # straight down from 64.8
    assert level(state, 1) == 0.0 and second.outcome == "completed"


async def test_stopping_the_engine_leaves_fades_where_they_are(state: StateStore) -> None:
    fades, clock = engine(state, 1)
    handle = fades.fade_channel(1, level=100.0, fade_ms=1000)
    clock.now += 0.25
    await fades.stop()
    assert level(state, 1) == 15.6 and handle.outcome == "cancelled"


async def test_a_handle_can_be_awaited(state: StateStore) -> None:
    fades, clock = engine(state, 1)
    handle = fades.fade_channel(1, level=10.0, fade_ms=100)
    waiter = asyncio.ensure_future(handle.wait())
    clock.now += 0.2
    fades.step()
    assert await waiter == "completed"


# -- dirty set ---------------------------------------------------------------


def test_a_fade_dirties_only_the_channels_it_touches(state: StateStore) -> None:
    fades, clock = engine(state, 1, 2, 3, 4)
    for channel_id in (1, 2, 3, 4):
        fades.fade_channel(channel_id, level=0.0)
    state.take_dirty()
    fades.fade_channel(3, level=100.0, fade_ms=1000)
    clock.now += 0.3
    fades.step()
    assert state.take_dirty() == {"lighting": {"levels.3"}}


# -- timing (§7.2.6, §22.2) ---------------------------------------------------


async def test_a_ten_second_fade_completes_within_10ms_under_simulated_load(
    state: StateStore,
) -> None:
    clock = VirtualClock()
    fades = FadeEngine(state, clock=clock, sleep=clock.sleep)
    fades.configure({1: (0.0, 100.0)})
    await fades.start()
    try:
        t0 = clock()
        handle = fades.fade_channel(1, level=100.0, fade_ms=10_000)
        assert await handle.wait() == "completed"
    finally:
        await fades.stop()
    assert handle.finished_at is not None
    assert abs(handle.finished_at - t0 - 10.0) <= 0.010
    assert level(state, 1) == 100.0  # exact end value
    # The load was real: across some five hundred steps the loop overslept by
    # more than half a second in total — the §7.2.6 failure an accumulated-
    # sleep fade would have carried into its duration. Deadlines absorbed it.
    total_lag = (clock() - t0) - sum(clock.sleeps)
    assert len(clock.sleeps) > 400 and total_lag > 0.5


async def test_two_fades_started_together_finish_together(state: StateStore) -> None:
    clock = VirtualClock()
    fades = FadeEngine(state, clock=clock, sleep=clock.sleep)
    fades.configure({1: (0.0, 100.0), 2: (0.0, 100.0)})
    writes = record_levels(state, clock)
    await fades.start()
    try:
        a = fades.fade_channel(1, level=100.0, fade_ms=10_000)
        b = fades.fade_channel(2, level=50.0, fade_ms=10_000)
        await asyncio.gather(a.wait(), b.wait())
    finally:
        await fades.stop()
    assert a.finished_at == b.finished_at
    # And they never drift apart on the way: at every step, 2 is half of 1.
    by_time: dict[float, dict[int, float]] = {}
    for at, channel_id, value in writes:
        by_time.setdefault(at, {})[channel_id] = value
    # (A step that leaves a rounded value unchanged writes nothing, so not
    # every step pairs; hundreds do.)
    paired = [v for v in by_time.values() if len(v) == 2]
    assert len(paired) > 300
    assert all(abs(v[2] - v[1] / 2) <= 0.1 for v in paired)


async def test_a_fade_keeps_time_on_the_real_event_loop_under_blocking_load(
    state: StateStore,
) -> None:
    fades = FadeEngine(state)
    fades.configure({1: (0.0, 100.0)})
    stop = asyncio.Event()

    async def load() -> None:  # a busy neighbour hogging the loop 3 ms at a time
        while not stop.is_set():
            time.sleep(0.003)  # noqa: ASYNC251 — blocking the loop is the point
            await asyncio.sleep(0)

    await fades.start()
    hog = asyncio.create_task(load())
    try:
        t0 = time.monotonic()
        handle = fades.fade_channel(1, level=100.0, fade_ms=1000)
        await handle.wait()
    finally:
        stop.set()
        await hog
        await fades.stop()
    assert handle.finished_at is not None
    # The 10 ms acceptance figure is proved deterministically by the simulated-
    # load test above. On a real loop the bound must also absorb the host's
    # timer granularity (about 15.6 ms on Windows) and a shared CI runner's
    # jitter; it still separates deadline timing from accumulated sleeps, which
    # under this load would finish some 150 ms late (fifty 20 ms steps, each
    # overslept by the 3 ms hog).
    error = handle.finished_at - t0 - 1.0
    assert abs(error) <= 0.040, f"finished {error * 1000:.1f} ms off"
    assert level(state, 1) == 100.0


# -- colour (§7.2.6) ---------------------------------------------------------


def test_level_and_colour_share_one_eased_parameter(state: StateStore) -> None:
    fades, clock = engine(state, 1)
    fades.fade_channel(1, level=0.0, colour=Colour(255, 0, 0))
    start, target = Colour(255, 0, 0), Colour(0, 0, 255)
    fades.fade_channel(1, level=100.0, colour=target, fade_ms=1000)
    for t in (0.1, 0.3, 0.5, 0.7, 0.9):
        clock.now += 0.2 if t > 0.1 else 0.1
        fades.step()
        eased = smoothstep(t)
        assert level(state, 1) == round(100 * eased, 1)
        assert Colour.from_store(state.lighting.get_item("colour", 1)) == start.lerp(target, eased)


def test_a_level_fade_on_a_coloured_fixture_does_not_shift_hue(state: StateStore) -> None:
    rig = Rig(state, config(dmx(1, 1, RGB)))
    clock = ManualClock()
    fades = FadeEngine(state, owner="fade_engine_2", clock=clock)
    fades.configure({1: (0.0, 100.0)})
    fades.fade_channel(1, level=0.0, colour=Colour(240, 120, 60))
    fades.fade_channel(1, level=100.0, fade_ms=2000)
    for _ in range(19):
        clock.now += 0.1
        fades.step()
        rig.compositor.composite_dmx()
        r, g, b = rig.frame()[:3]
        if r >= 20:  # below that, rounding dominates the ratio
            assert abs(g - r / 2) <= 1 and abs(b - r / 4) <= 1


async def test_a_level_and_colour_change_resolve_in_one_frame(state: StateStore) -> None:
    pipe = Pipeline(state, config(dmx(1, 1, RGB)))
    await pipe.start()
    try:
        pipe.fades.fade_channel(1, level=100.0, colour=Colour(0, 0, 255))
        await wait_until(lambda: list(pipe.output.last()[:3]) == [0, 0, 255])
        seen = len(pipe.output.sent)
        pipe.fades.fade_channel(1, level=50.0, colour=Colour(200, 100, 0))
        await wait_until(lambda: len(pipe.output.sent) > seen)
        await asyncio.sleep(0.05)
        new_frames = pipe.output.frames()[seen:]
        # The first frame after the change carries both, never one without the other.
        assert list(new_frames[0][:3]) == [100, 50, 0]
    finally:
        await pipe.stop()


# -- scene precedence (§10.6, §8.14) ------------------------------------------


def test_an_operator_write_cancels_a_normal_scenes_fade_on_that_channel_only(
    state: StateStore,
) -> None:
    fades, clock = engine(state, 1, 2)
    scene = SceneRun(scene_id=4)
    one = fades.fade_channel(1, level=100.0, fade_ms=1000, owner=scene)
    two = fades.fade_channel(2, level=100.0, fade_ms=1000, owner=scene)
    assert fades.driven_channels() == {1: scene, 2: scene}
    clock.now += 0.5
    fades.step()

    fades.fade_channel(1, level=10.0)  # the operator grabs channel 1

    assert one.outcome == "cancelled" and level(state, 1) == 10.0
    assert fades.driven_channels() == {2: scene}
    clock.now += 0.5
    fades.step()
    assert two.outcome == "completed" and level(state, 2) == 100.0  # the scene carried on
    assert level(state, 1) == 10.0


def test_a_critical_scene_locks_its_channels_and_refuses_operator_writes(
    state: StateStore,
) -> None:
    fades, _ = engine(state, 1, 2)
    alarm = SceneRun(scene_id=9, priority="critical")
    fades.begin_critical(alarm, [1])
    fades.fade_channel(1, level=0.0, owner=alarm)  # the lock holder may write

    with pytest.raises(ChannelLockedError) as refused:
        fades.fade_channel(1, level=100.0)  # someone leaning on a fader
    assert refused.value.channel_id == 1 and refused.value.run == alarm
    with pytest.raises(ChannelLockedError):
        fades.fade_channel(1, level=100.0, owner=SceneRun(scene_id=3))  # another scene
    assert level(state, 1) == 0.0

    fades.fade_channel(2, level=55.0)  # unlocked channels are unaffected
    assert fades.locks() == {1: alarm} and fades.driven_channels() == {1: alarm}
    assert fades.release(alarm) == [1]
    fades.fade_channel(1, level=100.0)
    assert level(state, 1) == 100.0


def test_a_critical_scene_locks_every_channel_it_fades(state: StateStore) -> None:
    fades, _ = engine(state, 1, 2, 3)
    alarm = SceneRun(scene_id=9, priority="critical")
    fades.begin_critical(alarm)
    fades.fade_channel(3, level=0.0, fade_ms=500, owner=alarm)
    with pytest.raises(ChannelLockedError):
        fades.fade_channel(3, level=80.0)


def test_a_critical_scene_starting_cancels_every_fade_at_its_current_value(
    state: StateStore,
) -> None:
    fades, clock = engine(state, 1, 2)
    scene = SceneRun(scene_id=1)
    a = fades.fade_channel(1, level=100.0, fade_ms=1000, owner=scene)
    b = fades.fade_channel(2, level=100.0, fade_ms=1000)
    clock.now += 0.5

    fades.begin_critical(SceneRun(scene_id=2, priority="critical"), [1, 2])

    assert (a.outcome, b.outcome) == ("cancelled", "cancelled")
    assert level(state, 1) == 50.0 and level(state, 2) == 50.0
    assert fades.active_fades() == 0


def test_a_newer_critical_scene_takes_over_the_locks(state: StateStore) -> None:
    fades, _ = engine(state, 1)
    first = SceneRun(scene_id=1, priority="critical")
    second = SceneRun(scene_id=2, priority="critical")
    fades.begin_critical(first, [1])
    fades.begin_critical(second, [1])
    fades.fade_channel(1, level=0.0, owner=second)
    assert fades.locked_by(1) == second


def test_only_critical_runs_lock(state: StateStore) -> None:
    fades, _ = engine(state, 1)
    with pytest.raises(ValueError):
        fades.begin_critical(SceneRun(scene_id=1), [1])
    fades.fade_channel(1, level=10.0, fade_ms=100, owner=SceneRun(scene_id=1))
    assert fades.locks() == {}


def test_housekeeping_writes_bypass_a_lock_with_force(state: StateStore) -> None:
    fades, _ = engine(state, 1)
    fades.begin_critical(SceneRun(scene_id=9, priority="critical"), [1])
    fades.fade_channel(1, level=12.0, force=True)
    assert level(state, 1) == 12.0


def test_cancelling_a_run_stops_only_its_fades(state: StateStore) -> None:
    fades, clock = engine(state, 1, 2)
    scene = SceneRun(scene_id=1)
    fades.fade_channel(1, level=100.0, fade_ms=1000, owner=scene)
    other = fades.fade_channel(2, level=100.0, fade_ms=1000)
    clock.now += 0.5
    assert fades.cancel_owner(scene) == [1]
    assert level(state, 1) == 50.0 and other.outcome is None
