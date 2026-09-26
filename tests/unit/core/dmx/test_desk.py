"""Visiting-desk detection and observed levels (spec §7.2.7).

The socket side is proved in ``test_artnet_handoff.py``; these drive
:class:`DeskInput` directly with an injected clock, so five seconds of
silence take no time at all.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from proskenion.core.dmx.compositor import LightingConfig, ProfileSlot
from proskenion.core.dmx.desk import DeskInput, observed_level
from proskenion.core.state import StateStore
from tests.unit.core.dmx.conftest import RGB, config, dmx, wait_until

NODE_DEVICE = 1
UNIVERSE = 0


class Clock:
    def __init__(self) -> None:
        self.now = 50.0

    def __call__(self) -> float:
        return self.now


def _desk(
    state: StateStore, cfg: LightingConfig | None = None, **kwargs: float
) -> tuple[DeskInput, list[bool], Clock]:
    calls: list[bool] = []
    clock = Clock()
    current = cfg or config()
    desk = DeskInput(state, calls.append, lambda: current, clock=clock, **kwargs)
    return desk, calls, clock


def test_the_first_frame_detects_and_five_seconds_of_silence_clears(state: StateStore) -> None:
    desk, calls, clock = _desk(state)
    desk.art_dmx(NODE_DEVICE, UNIVERSE, bytes(512))
    assert calls == [True]  # enter on the first frame, not after a delay

    clock.now += 3.0
    desk.art_dmx(NODE_DEVICE, UNIVERSE, bytes(512))
    clock.now += 4.9
    desk.check()
    assert calls == [True]  # 4.9 s since the last frame: still a desk

    clock.now += 0.1
    desk.check()
    assert calls == [True, False]
    desk.check()
    assert calls == [True, False]  # never reported twice


def test_a_desk_sending_all_zeros_is_still_a_desk(state: StateStore) -> None:
    desk, calls, _ = _desk(state)
    desk.art_dmx(NODE_DEVICE, UNIVERSE, bytes(512))
    assert desk.detected and calls == [True]


def test_detection_starts_false_and_nothing_is_persisted(state: StateStore) -> None:
    desk, calls, _ = _desk(state)
    assert desk.detected is False and calls == []
    assert state.lighting.persistence_class("observed") is None


def test_observed_carries_patched_channels_only(state: StateStore) -> None:
    cfg = config(
        dmx(1, 1, universe=UNIVERSE),  # a dimmer
        dmx(2, 10, RGB, universe=UNIVERSE),  # colour, no dimmer: its brightest component
        dmx(3, 20, (ProfileSlot(0, "pan"),), universe=UNIVERSE),  # nothing to show as a level
        dmx(4, 1, universe=5),  # another universe
        dmx(5, 1, device=9, universe=UNIVERSE),  # another output device
    )
    desk, _, _ = _desk(state, cfg)
    frame = bytearray(512)
    frame[0] = 128
    frame[9], frame[10], frame[11] = 10, 255, 20
    frame[19] = 200
    frame[100] = 255  # unpatched
    desk.art_dmx(NODE_DEVICE, UNIVERSE, bytes(frame))
    desk.check()
    assert state.lighting.get("observed") == {"1": 50.2, "2": 100.0}


def test_observed_is_throttled_not_written_per_frame(state: StateStore) -> None:
    desk, _, _ = _desk(state, config(dmx(1, 1, universe=UNIVERSE)))
    for value in range(40):  # a second of a 40 fps desk
        desk.art_dmx(NODE_DEVICE, UNIVERSE, bytes([value]) + bytes(511))
    assert state.lighting.get("observed") == {}  # frames alone write nothing
    state.take_dirty()
    desk.check()
    assert state.lighting.get("observed") == {"1": 15.3}  # only the newest frame
    assert state.take_dirty() == {"lighting": {"observed.1"}}


def test_observed_is_cleared_when_the_desk_stands_down(state: StateStore) -> None:
    desk, _, clock = _desk(state, config(dmx(1, 1, universe=UNIVERSE)))
    desk.art_dmx(NODE_DEVICE, UNIVERSE, bytes([255]) + bytes(511))
    desk.check()
    assert state.lighting.get("observed") == {"1": 100.0}
    clock.now += 5.0
    desk.check()
    assert state.lighting.get("observed") == {}


def test_a_patch_edit_drops_channels_no_longer_patched(state: StateStore) -> None:
    calls: list[bool] = []
    holder = {"cfg": config(dmx(1, 1, universe=UNIVERSE), dmx(2, 2, universe=UNIVERSE))}
    desk = DeskInput(state, calls.append, lambda: holder["cfg"], clock=Clock())
    desk.art_dmx(NODE_DEVICE, UNIVERSE, bytes([255, 255]) + bytes(510))
    desk.check()
    assert set(state.lighting.get("observed")) == {"1", "2"}  # type: ignore[arg-type]
    holder["cfg"] = config(dmx(1, 1, universe=UNIVERSE))
    desk.art_dmx(NODE_DEVICE, UNIVERSE, bytes([255, 255]) + bytes(510))
    desk.check()
    assert state.lighting.get("observed") == {"1": 100.0}


def test_observed_level_prefers_the_dimmer_slot() -> None:
    assert observed_level(bytes([0, 255]), (0, (1,))) == 0.0
    assert observed_level(bytes([0, 255]), (-1, (1,))) == 100.0
    assert observed_level(bytes(2), (-1, ())) is None


async def test_the_silence_watch_runs_on_its_own(state: StateStore) -> None:
    calls: list[bool] = []
    desk = DeskInput(state, calls.append, config, silence_s=0.1, interval_s=0.01)
    await desk.start()
    try:
        desk.art_dmx(NODE_DEVICE, UNIVERSE, bytes(512))
        await wait_until(lambda: calls == [True, False])
    finally:
        await desk.stop()


def test_attach_logs_an_input_universe_that_is_also_an_output(
    state: StateStore, caplog: pytest.LogCaptureFixture
) -> None:
    # §7.2.7 expects the node's merge on a shared universe; it is allowed, and
    # logged at INFO because it is the open bench question.
    desk, _, _ = _desk(state, config(dmx(1, 1, universe=2), dmx(2, 1, universe=3)))
    with caplog.at_level(logging.INFO, logger="proskenion.core.dmx.desk"):
        desk.attach(NODE_DEVICE, frozenset({0, 2}))
    overlaps = [r for r in caplog.records if "also an output universe" in r.getMessage()]
    assert [r.levelno for r in overlaps] == [logging.INFO]
    assert "universe 2" in overlaps[0].getMessage()


def test_attach_with_no_input_universe_says_detection_is_off(
    state: StateStore, caplog: pytest.LogCaptureFixture
) -> None:
    desk, _, _ = _desk(state)
    with caplog.at_level(logging.INFO, logger="proskenion.core.dmx.desk"):
        desk.attach(NODE_DEVICE, frozenset())
    assert any("detection off" in r.getMessage() for r in caplog.records)


async def test_stop_is_safe_before_start(state: StateStore) -> None:
    desk, _, _ = _desk(state)
    await desk.stop()
    await asyncio.sleep(0)


# -- §12.1's boot wait: "wait up to 5 s for booth frames" ---------------------------------


def _desk_with_stepped_clock(
    state: StateStore, **kwargs: float
) -> tuple[DeskInput, Clock]:
    """A :class:`DeskInput` whose ``clock`` and ``sleep`` share one virtual
    clock, so ``wait_at_boot``'s up-to-5-second wait advances instantly
    instead of taking real wall time — the same clock ``wait_at_boot`` reads
    to check its deadline is the one ``sleep`` advances."""
    clock = Clock()

    async def _sleep(seconds: float) -> None:
        clock.now += seconds

    desk = DeskInput(state, lambda _detected: None, config, clock=clock, sleep=_sleep, **kwargs)
    return desk, clock


async def test_wait_at_boot_returns_at_once_when_a_desk_is_already_detected(
    state: StateStore,
) -> None:
    desk, clock = _desk_with_stepped_clock(state)
    desk.art_dmx(NODE_DEVICE, UNIVERSE, bytes(512))
    assert desk.detected is True
    before = clock.now
    await desk.wait_at_boot(timeout_s=5.0)
    assert clock.now == before  # no waiting needed at all


async def test_wait_at_boot_returns_early_once_detection_is_confirmed_off(
    state: StateStore,
) -> None:
    """The common case today (no booth input wired, per the 25 September 2026
    site survey): the driver's attach() call, once it connects, must not cost
    the full 5 s just because nothing is configured."""
    desk, clock = _desk_with_stepped_clock(state)
    desk.attach(NODE_DEVICE, frozenset())  # "desk detection off"
    assert desk.configured is False
    before = clock.now
    await desk.wait_at_boot(timeout_s=5.0)
    assert clock.now == before


async def test_wait_at_boot_runs_its_full_course_when_configured_and_silent(
    state: StateStore,
) -> None:
    """Detection is on, but nothing has arrived: the wait cannot know a desk
    will never show up any sooner than the timeout, so it takes the full
    window — this is the case the feature exists for."""
    desk, clock = _desk_with_stepped_clock(state, interval_s=0.1)
    desk.attach(NODE_DEVICE, frozenset({0}))  # detection on, no frame yet
    assert desk.configured is True
    before = clock.now
    await desk.wait_at_boot(timeout_s=5.0)
    assert clock.now >= before + 5.0
    assert desk.detected is False


async def test_wait_at_boot_stops_the_moment_a_frame_arrives_mid_wait(
    state: StateStore,
) -> None:
    """A desk detected partway through the window ends the wait there —
    the point is not to burn the full 5 s once the question is answered."""
    desk, clock = _desk_with_stepped_clock(state, interval_s=0.1)
    desk.attach(NODE_DEVICE, frozenset({0}))

    calls = 0
    real_sleep = desk._sleep  # type: ignore[attr-defined]

    async def _sleep_then_detect(seconds: float) -> None:
        nonlocal calls
        calls += 1
        await real_sleep(seconds)
        if calls == 3:  # a frame arrives after 0.3 s of silence
            desk.art_dmx(NODE_DEVICE, UNIVERSE, bytes(512))

    desk._sleep = _sleep_then_detect  # type: ignore[attr-defined]
    before = clock.now
    await desk.wait_at_boot(timeout_s=5.0)
    assert desk.detected is True
    assert clock.now < before + 5.0  # ended well short of the full window
    assert clock.now == pytest.approx(before + 0.3, abs=1e-9)
