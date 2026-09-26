"""Change-driven frame rendering (spec §7.2.3 *Rendering is change-driven*, §12.1, §22.2)."""

from __future__ import annotations

import asyncio
import logging

import pytest

from proskenion.core.bus import EventBus
from proskenion.core.dmx import renderer as renderer_module
from proskenion.core.dmx.renderer import DEFAULT_KEEPALIVE_S, FrameRenderer
from proskenion.core.events import DeviceStatusChanged
from proskenion.core.state import StateStore
from tests.unit.core.dmx.conftest import Pipeline, config, dmx, wait_until


async def test_no_frame_is_sent_while_the_level_store_is_clean(state: StateStore) -> None:
    pipe = Pipeline(state, config(dmx(1, 1)))  # the default 1 s keepalive
    await pipe.start()
    try:
        await wait_until(lambda: len(pipe.output.sent) == 1)
        await asyncio.sleep(0.4)
        assert len(pipe.output.sent) == 1
        assert pipe.renderer.composites == 1
    finally:
        await pipe.stop()


async def test_at_rest_the_compositor_does_not_run_and_the_keepalive_fires_on_its_interval(
    state: StateStore,
) -> None:
    pipe = Pipeline(state, config(dmx(1, 1)), keepalive_s=0.1)
    pipe.rig.set_level(1, 60.0)
    await pipe.start()
    try:
        await wait_until(lambda: len(pipe.output.sent) == 1)
        await asyncio.sleep(0.55)
        sent = pipe.output.sent
        assert pipe.renderer.composites == 1  # the compositor did not run again
        assert 4 <= len(sent) - 1 <= 6
        assert all(data == sent[0][2] for _, _, data in sent)  # the last frame, resent
        gaps = [b[0] - a[0] for a, b in zip(sent, sent[1:], strict=False)]
        assert all(0.09 <= gap <= 0.14 for gap in gaps)
    finally:
        await pipe.stop()


def test_the_keepalive_defaults_to_one_second_and_is_a_bounded_setting(
    state: StateStore,
) -> None:
    pipe = Pipeline(state, config(dmx(1, 1)))
    assert DEFAULT_KEEPALIVE_S == 1.0 and pipe.renderer.keepalive_s == 1.0
    pipe.renderer.set_keepalive(0.5)
    assert pipe.renderer.keepalive_s == 0.5
    for bad in (0.01, 2.6):  # at rest must mean at rest; inside E1.31's 2.5 s timeout
        with pytest.raises(ValueError):
            pipe.renderer.set_keepalive(bad)


async def test_a_fade_sends_no_more_than_40_frames_a_second(state: StateStore) -> None:
    pipe = Pipeline(state, config(dmx(1, 1)))
    await pipe.start()
    try:
        await wait_until(lambda: len(pipe.output.sent) == 1)
        loop = asyncio.get_running_loop()
        start = loop.time()
        handle = pipe.fades.fade_channel(1, level=100.0, fade_ms=1000)
        await handle.wait()
        elapsed = loop.time() - start
        await asyncio.sleep(0.05)
        during = [s for s in pipe.output.sent if s[0] >= start]
        assert during[-1][2][0] == 255
        assert len(during) <= elapsed * 40 + 2
        assert len(during) >= 15  # it does render the fade, not just its end
        gaps = [b[0] - a[0] for a, b in zip(during, during[1:], strict=False)]
        assert min(gaps) >= 0.024  # 1/40 s, less timer jitter
    finally:
        await pipe.stop()


async def test_each_frame_is_one_send_universe_call_per_universe(state: StateStore) -> None:
    pipe = Pipeline(state, config(dmx(1, 1), dmx(2, 1, universe=2), dmx(3, 7, universe=2)))
    await pipe.start()
    try:
        await wait_until(lambda: len(pipe.output.sent) == 2)
        pipe.fades.fade_channel(1, level=10.0)
        pipe.fades.fade_channel(2, level=20.0)
        pipe.fades.fade_channel(3, level=30.0)
        await wait_until(lambda: pipe.output.last(2)[6] == 77)
        await asyncio.sleep(0.05)
        universes = [u for _, u, _ in pipe.output.sent]
        assert universes.count(1) == universes.count(2) == len(universes) // 2
        assert pipe.output.last(1)[0] == 26 and pipe.output.last(2)[0] == 51
    finally:
        await pipe.stop()


async def test_frames_go_only_to_connected_devices_and_a_reconnection_gets_the_current_frame(
    state: StateStore, bus: EventBus
) -> None:
    pipe = Pipeline(state, config(dmx(1, 1)), bus=bus, connected=False)
    await pipe.start()
    try:
        pipe.fades.fade_channel(1, level=80.0)
        await asyncio.sleep(0.15)
        assert pipe.output.sent == []  # a frame to an unconnected backend goes nowhere

        loop = asyncio.get_running_loop()
        connected_at = loop.time()
        pipe.devices.connect()
        bus.emit(DeviceStatusChanged("dmx", "connected"))
        await wait_until(lambda: len(pipe.output.sent) == 1)
        assert pipe.output.sent[0][0] - connected_at < 0.1  # at once, not at the keepalive
        assert pipe.output.last()[0] == 204  # the current frame

        pipe.devices.disconnect()
        bus.emit(DeviceStatusChanged("dmx", "error", "config"))
        pipe.fades.fade_channel(1, level=20.0)
        await asyncio.sleep(0.1)
        assert len(pipe.output.sent) == 1

        pipe.devices.connect()
        bus.emit(DeviceStatusChanged("dmx", "connected"))
        await wait_until(lambda: len(pipe.output.sent) == 2)
        assert pipe.output.last()[0] == 51  # retried on reconnection, as it now stands
    finally:
        await pipe.stop()


async def test_a_failed_send_does_not_stop_rendering(state: StateStore) -> None:
    pipe = Pipeline(state, config(dmx(1, 1)))
    await pipe.start()
    try:
        await wait_until(lambda: len(pipe.output.sent) == 1)
        pipe.output.fail = True
        pipe.fades.fade_channel(1, level=50.0)
        await asyncio.sleep(0.05)
        pipe.output.fail = False
        pipe.fades.fade_channel(1, level=60.0)
        await wait_until(lambda: pipe.output.last()[0] == 153)
    finally:
        await pipe.stop()


async def test_the_first_frame_composites_the_model_as_it_stands(state: StateStore) -> None:
    pipe = Pipeline(state, config(dmx(1, 1), dmx(2, 2)))
    pipe.rig.set_level(1, 100.0)  # e.g. restored at boot
    pipe.rig.set_level(2, 40.0)
    await pipe.start()
    try:
        await wait_until(lambda: len(pipe.output.sent) == 1)
        assert list(pipe.output.last()[:2]) == [255, 102]
    finally:
        await pipe.stop()


async def test_rendering_never_drains_the_broadcasters_dirty_set(state: StateStore) -> None:
    pipe = Pipeline(state, config(dmx(1, 1)))
    await pipe.start()
    try:
        state.take_dirty()
        pipe.fades.fade_channel(1, level=70.0)
        await wait_until(lambda: pipe.output.last()[0] == 179)
        assert state.take_dirty() == {"lighting": {"levels.1"}}
    finally:
        await pipe.stop()


async def test_a_suspended_renderer_sends_nothing_even_on_reconnection(
    state: StateStore, bus: EventBus
) -> None:
    pipe = Pipeline(state, config(dmx(1, 1)), bus=bus, keepalive_s=0.1)
    await pipe.start()
    try:
        await wait_until(lambda: len(pipe.output.sent) == 1)
        pipe.renderer.suspend()
        pipe.devices.disconnect()
        bus.emit(DeviceStatusChanged("dmx", "error", "device"))
        await asyncio.sleep(0.05)
        pipe.devices.connect()
        bus.emit(DeviceStatusChanged("dmx", "connected"))
        pipe.fades.fade_channel(1, level=90.0)
        await asyncio.sleep(0.3)
        assert len(pipe.output.sent) == 1
        pipe.renderer.resume()
        await wait_until(lambda: len(pipe.output.sent) == 2)
        assert pipe.output.last()[0] == 230
    finally:
        await pipe.stop()


async def test_no_connection_after_ten_seconds_is_logged_once(
    state: StateStore, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(renderer_module, "CONNECT_WARNING_S", 0.05)
    pipe = Pipeline(state, config(dmx(1, 1)), connected=False)
    pipe.renderer.set_keepalive(0.1)
    with caplog.at_level(logging.WARNING, logger=renderer_module.__name__):
        await pipe.start()
        try:
            await asyncio.sleep(0.35)
        finally:
            await pipe.stop()
    warnings = [r for r in caplog.records if "no lighting output has connected" in r.message]
    assert len(warnings) == 1


def test_the_renderer_accepts_only_a_positive_frame_cap(state: StateStore) -> None:
    pipe = Pipeline(state, config(dmx(1, 1)))
    with pytest.raises(ValueError):
        FrameRenderer(pipe.rig.compositor, state, pipe.devices, max_fps=0)
