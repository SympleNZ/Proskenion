"""Smooth DMX output while a fader moves (field finding 2026-09-30; §7.2.3, §21.2, §23.1).

On the rig a fader dragged on the web UI produced a frame only when a write
arrived — a median of one every 50 ms, gaps past 100 ms — each a jump that
grew with the drag speed, which the room saw as flicker. Two things fix it:
the compositor glides a direct operator write's *output* to its new value
over :data:`GLIDE_S` (the stored level is the target at once), and the
renderer sends on a steady 25 ms grid while anything moves.

The compositor tests run on explicit times — a fake clock — so they are exact.
The renderer tests run on the real event loop and allow for timer jitter.
"""

from __future__ import annotations

import asyncio

import pytest

from proskenion.core.dmx.compositor import (
    GLIDE_LEAD_S,
    GLIDE_S,
    Compositor,
    LevelStoreView,
    level_to_dmx,
)
from proskenion.core.dmx.fade import FadeEngine
from proskenion.core.dmx.renderer import CADENCE_IDLE_S
from proskenion.core.state import StateStore
from tests.unit.core.dmx.conftest import Pipeline, Rig, config, dmx, wait_until

FRAME_S = 0.025
ROW = (1, 2, 3, 4)


def _row_rig(state: StateStore) -> Rig:
    return Rig(state, config(*(dmx(c, c) for c in ROW), dmx(5, 5)))


def _output(rig: Rig, channel_id: int) -> int:
    return rig.frame()[channel_id - 1]


# -- the compositor: the glide itself (fake clock) ----------------------------------------


def test_the_stored_level_is_the_target_at_once_and_only_the_output_glides(
    state: StateStore,
) -> None:
    rig = _row_rig(state)
    rig.compositor.composite_dmx(0.0)  # the output as it stands: 0
    rig.fades.fade_channel(1, level=90.0)  # a direct write, fade_ms 0
    rig.compositor.request_glide([1])
    assert state.lighting.get_item("levels", 1) == 90.0  # what the UI shows and persists
    assert rig.compositor.composited_level(1) == 90.0  # the ghost mark: where it lands

    outputs = []
    for frame in range(1, 5):
        rig.compositor.composite_dmx(1.0 + (frame - 1) * FRAME_S)
        outputs.append(rig.compositor.output_level(1))
    # Counted from one frame before the frame that first carries it: a third, two
    # thirds, there — and it stays there.
    assert GLIDE_S == pytest.approx(3 * FRAME_S) and GLIDE_LEAD_S == FRAME_S
    assert outputs == [30.0, 60.0, 90.0, 90.0]
    assert _output(rig, 1) == level_to_dmx(90.0)
    assert not rig.compositor.is_gliding(1) and not rig.compositor.dmx_gliding


def test_irregular_fader_writes_give_monotone_bounded_steps(state: StateStore) -> None:
    """Writes 20, 60 and 110 ms apart — as the rig's Wi-Fi delivered them —
    sampled on the 25 ms grid: the output never steps back, and its largest
    step is well under the largest jump the writes themselves made."""
    rig = _row_rig(state)
    rig.compositor.composite_dmx(0.0)
    speed = 100.0 / 3.0  # %/s: 0 → 100 in three seconds, like the owner's drag
    writes: list[float] = []
    t = 0.0
    for gap in (0.02, 0.06, 0.11) * 12:
        t += gap
        writes.append(round(t, 3))
    frames = [round(FRAME_S * k, 3) for k in range(1, int(t / FRAME_S) + 8)]

    outputs: list[float] = []
    stored: list[float] = []
    pending = list(writes)
    for now in frames:
        while pending and pending[0] <= now:
            at = pending.pop(0)
            rig.fades.fade_channel(1, level=min(at * speed, 100.0))
            rig.compositor.request_glide([1])
            stored.append(state.lighting.get_item("levels", 1))  # type: ignore[arg-type]
        rig.compositor.composite_dmx(now)
        output = rig.compositor.output_level(1)
        assert output is not None
        outputs.append(output)

    steps = [b - a for a, b in zip(outputs, outputs[1:], strict=False)]
    jumps = [b - a for a, b in zip([0.0, *stored], stored, strict=False)]
    assert all(step >= 0 for step in steps)  # monotone: no reversal
    assert max(steps) <= 0.6 * max(jumps)  # spread across frames, not one jump
    assert outputs[-1] == stored[-1]  # and it lands where the fader stopped


def test_a_group_write_moves_every_member_in_the_same_frame(state: StateStore) -> None:
    rig = _row_rig(state)
    for channel_id, level in zip(ROW, (0.0, 20.0, 50.0, 80.0), strict=True):
        rig.fades.fade_channel(channel_id, level=level)
    rig.compositor.composite_dmx(0.0)
    for channel_id in ROW:  # a group fader: every member, one synchronous call
        rig.fades.fade_channel(channel_id, level=100.0)
    rig.compositor.request_glide(ROW)

    history: list[tuple[float | None, ...]] = []
    for frame in range(4):
        rig.compositor.composite_dmx(1.0 + frame * FRAME_S)
        history.append(tuple(rig.compositor.output_level(c) for c in ROW))
    # Every member moves on the first frame, and all land on the same one.
    assert all(out != start for out, start in zip(history[0], (0.0, 20.0, 50.0, 80.0), strict=True))
    assert history[1] != (100.0,) * 4 and history[2] == (100.0,) * 4
    assert rig.compositor.output_level(5) == 0.0  # a channel outside the group: untouched


def test_the_master_glides_every_fixture(state: StateStore) -> None:
    rig = _row_rig(state)
    for channel_id in ROW:
        rig.fades.fade_channel(channel_id, level=100.0)
    rig.compositor.composite_dmx(0.0)
    rig.set_master(40.0)
    rig.compositor.request_glide()  # the master fader: None = every DMX channel
    rig.compositor.composite_dmx(1.0)
    assert {rig.compositor.output_level(c) for c in ROW} == {80.0}
    rig.compositor.composite_dmx(1.0 + 2 * FRAME_S)
    assert {rig.compositor.output_level(c) for c in ROW} == {40.0}


def test_an_explicit_fade_keeps_its_own_timing_exactly(state: StateStore) -> None:
    """No glide is asked for, so every frame carries the fade's own value — the
    last one included — and it ends on the fade's deadline."""
    clock = [100.0]
    fades = FadeEngine(state, clock=lambda: clock[0])
    compositor = Compositor(LevelStoreView(state.lighting), destinations=fades)
    cfg = config(dmx(1, 1))
    fades.configure(cfg.ranges())
    compositor.configure(cfg)
    compositor.composite_dmx(clock[0])
    handle = fades.fade_channel(1, level=100.0, fade_ms=1000)
    while not handle.done:
        clock[0] += FRAME_S
        fades.step()
        compositor.composite_dmx(clock[0])
        assert compositor.output_level(1) == state.lighting.get_item("levels", 1)
        assert not compositor.glided
    assert handle.finished_at == pytest.approx(101.0)
    assert compositor.output_level(1) == 100.0


def test_without_a_clock_or_under_external_control_nothing_glides(state: StateStore) -> None:
    rig = _row_rig(state)
    rig.compositor.composite_dmx(0.0)
    rig.fades.fade_channel(1, level=60.0)
    rig.compositor.request_glide([1])
    rig.compositor.composite_dmx()  # no clock: the target at once
    assert rig.compositor.output_level(1) == 60.0

    rig.fades.fade_channel(1, level=0.0)
    rig.compositor.request_glide([1])
    rig.compositor.suspend_dmx(True)  # external control drops every glide …
    rig.compositor.suspend_dmx(False)
    rig.compositor.composite_dmx(1.0)  # … so resuming composites the model as it stands
    assert rig.compositor.output_level(1) == 0.0
    assert not rig.compositor.dmx_gliding


# -- the renderer: a steady cadence while anything moves (real loop) ----------------------


async def test_a_fade_goes_out_on_a_steady_25_ms_cadence_that_stops_after_idle(
    state: StateStore,
) -> None:
    pipe = Pipeline(state, config(dmx(1, 1)))
    await pipe.start()
    try:
        await wait_until(lambda: len(pipe.output.sent) == 1)
        assert not pipe.renderer.cadence_running  # the boot frame is one frame, not motion
        loop = asyncio.get_running_loop()
        start = loop.time()
        handle = pipe.fades.fade_channel(1, level=100.0, fade_ms=500)
        await handle.wait()
        ended = loop.time()
        await wait_until(lambda: not pipe.renderer.cadence_running, within=1.0)
        stopped = loop.time()
        tail = [s for s in pipe.output.sent if s[0] > ended]
        during = [s for s in pipe.output.sent if start <= s[0] <= ended]

        gaps = [b[0] - a[0] for a, b in zip(during, during[1:], strict=False)]
        assert 0.022 <= sum(gaps) / len(gaps) <= 0.03  # 40 fps on average: a fixed grid
        assert max(gaps) <= 0.05  # never a long hole mid-fade
        assert pipe.output.last()[0] == 255
        # After the last change the cadence runs on for CADENCE_IDLE_S, then stops …
        assert CADENCE_IDLE_S - 0.03 <= stopped - ended <= CADENCE_IDLE_S + 0.1
        assert all(data == tail[0][2] for _, _, data in tail)  # resending the last frame
        sent = len(pipe.output.sent)
        await asyncio.sleep(0.3)
        assert len(pipe.output.sent) == sent  # … and the 1 s keepalive takes over
    finally:
        await pipe.stop()


async def test_irregular_direct_writes_come_out_evenly_spaced_and_monotone(
    state: StateStore,
) -> None:
    pipe = Pipeline(state, config(*(dmx(c, c) for c in ROW)))
    await pipe.start()
    try:
        await wait_until(lambda: len(pipe.output.sent) == 1)
        loop = asyncio.get_running_loop()
        start = loop.time()
        level = 0.0
        for gap in (0.02, 0.06, 0.11) * 3:
            await asyncio.sleep(gap)
            level += gap * 100.0  # a drag at constant speed: jumps follow the gaps
            for channel_id in ROW:  # a group fader, in one synchronous call
                pipe.fades.fade_channel(channel_id, level=level)
            pipe.rig.compositor.request_glide(ROW)
        await wait_until(lambda: not pipe.renderer.cadence_running, within=1.0)

        frames = [s for s in pipe.output.sent if s[0] > start]
        moving = [s for s in frames if s[2][0] != frames[-1][2][0]] + [frames[-1]]
        gaps = [b[0] - a[0] for a, b in zip(frames, frames[1:], strict=False)]
        assert max(gaps) <= 0.05  # no 110 ms hole: the grid carries on between writes
        assert 0.022 <= sum(gaps) / len(gaps) <= 0.03
        values = [data[0] for _, _, data in frames]
        assert values == sorted(values)  # monotone
        assert all(len(set(data[:4])) == 1 for _, _, data in frames)  # the row moves as one
        assert values[-1] == level_to_dmx(round(level, 1))
        assert len(moving) > 9  # more frames than writes carried the movement
    finally:
        await pipe.stop()
