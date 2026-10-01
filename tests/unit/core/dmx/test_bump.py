"""BUMP: a group flashed to full while held (owner decision 2026-10-01, "Option A").

Three layers: the compositor's overlay (fake clock, exact), the holders and
their expiry (:class:`BumpHolds`, on the real event loop), and the lighting
service that joins them to the configuration, external control and the
renderer. The WebSocket wire — press, refresh, release, socket close,
background — is tested in ``tests/unit/api/test_lighting.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from proskenion.core.bus import EventBus
from proskenion.core.dmx.bump import BUMP_HOLD_TIMEOUT_S, BUMP_REFRESH_S, BumpHolds
from proskenion.core.dmx.compositor import level_to_dmx
from proskenion.core.dmx.fade import SceneRun, UnknownGroupError
from proskenion.core.lighting import BumpRefusedError, IndicatorOnlyGroupError, LightingService
from proskenion.core.state import StateStore
from tests.unit.core.dmx.conftest import (
    FakeDevices,
    FakeKnx,
    Rig,
    config,
    dmx,
    knx,
    wait_until,
)

HOUSE_GA = "1/1/10"
#: Fixtures 1–3 at DMX 1–3 (3 capped at 80 %), a house dimmer 4. Group 7 is
#: fixtures 1 and 3 with the house dimmer; group 8 holds only the house
#: dimmer; group 9 is indicator-only.
CFG = config(
    dmx(1, 1),
    dmx(2, 2),
    dmx(3, 3, max_value=80.0),
    knx(4, HOUSE_GA),
    groups={7: {1, 3, 4}, 8: {4}, 9: {1, 2}},
    indicator_only={9},
)


# -- the compositor's overlay ----------------------------------------------------------


def _rig(state: StateStore) -> Rig:
    rig = Rig(state, CFG)
    for channel_id, level in ((1, 30.0), (2, 30.0), (3, 30.0), (4, 30.0)):
        rig.set_level(channel_id, level)
    return rig


def test_a_bumped_dmx_channel_composites_at_full_times_the_master(state: StateStore) -> None:
    rig = _rig(state)
    rig.set_master(50.0)
    assert rig.compositor.set_bumped({1, 3, 4})
    rig.compositor.composite_dmx(0.0)
    frame = rig.frame()
    assert frame[0] == level_to_dmx(50.0)  # full × master 50 %
    assert frame[1] == level_to_dmx(15.0)  # not bumped: 30 × 50 %
    assert frame[2] == level_to_dmx(40.0)  # its own cap of 80, × master 50 %
    assert rig.compositor.bumped == {1, 3}  # the KNX dimmer is never bumped
    assert rig.compositor.composited_level(4) == 30.0  # its own level, unchanged
    # The overlay writes nothing: the stored levels are where they were.
    assert [state.lighting.get_item("levels", c) for c in (1, 2, 3, 4)] == [30.0] * 4


def test_releasing_the_bump_returns_the_output_to_the_stored_level(state: StateStore) -> None:
    rig = _rig(state)
    rig.compositor.set_bumped({1})
    rig.compositor.composite_dmx(0.0)
    assert rig.frame()[0] == 255
    assert rig.compositor.set_bumped(())
    rig.compositor.composite_dmx(0.025)
    assert rig.frame()[0] == level_to_dmx(30.0)
    assert not rig.compositor.set_bumped(())  # no change, nothing to redraw


def test_a_bump_is_instant_and_drops_a_glide_in_progress(state: StateStore) -> None:
    rig = _rig(state)
    rig.compositor.composite_dmx(0.0)
    rig.fades.fade_channel(1, level=60.0)
    rig.compositor.request_glide([1])
    rig.compositor.composite_dmx(1.0)
    assert 30.0 < (rig.compositor.output_level(1) or 0) < 60.0  # on its way
    rig.compositor.set_bumped({1})
    assert not rig.compositor.is_gliding(1)
    rig.compositor.composite_dmx(1.025)
    assert rig.compositor.output_level(1) == 100.0  # the whole step, in one frame
    rig.compositor.set_bumped(())
    rig.compositor.composite_dmx(1.05)
    assert rig.compositor.output_level(1) == 60.0  # and straight back


def test_overlay_moved_is_set_on_the_first_composite_after_a_change_only(
    state: StateStore,
) -> None:
    rig = _rig(state)
    rig.compositor.set_bumped({1})
    rig.compositor.composite_dmx(0.0)
    assert rig.compositor.overlay_moved
    rig.compositor.composite_dmx(0.025)
    assert not rig.compositor.overlay_moved


def test_a_reconfiguration_drops_a_bumped_channel_that_is_no_longer_patched(
    state: StateStore,
) -> None:
    rig = _rig(state)
    rig.compositor.set_bumped({1, 2})
    rig.configure(config(dmx(2, 2)))
    assert rig.compositor.bumped == {2}


# -- the holders, and their expiry -----------------------------------------------------


class Changes:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> None:
        self.count += 1


async def test_a_group_stays_bumped_until_every_holder_has_let_go() -> None:
    changes = Changes()
    holds = BumpHolds(changes)
    holds.hold(7, "tablet")
    holds.hold(7, "booth")
    assert holds.groups == {7} and changes.count == 1  # the second holder changes nothing
    holds.release(7, "tablet")
    assert holds.groups == {7} and changes.count == 1
    holds.release(7, "booth")
    assert holds.groups == frozenset() and changes.count == 2
    holds.release(7, "booth")  # a second release is no error
    assert changes.count == 2
    holds.stop()


async def test_a_holder_that_stops_refreshing_is_released_and_one_that_refreshes_is_not() -> None:
    changes = Changes()
    holds = BumpHolds(changes, timeout_s=0.15)
    holds.hold(7, "silent")
    holds.hold(8, "refreshing")
    for _ in range(6):  # 0.3 s: twice the timeout
        await asyncio.sleep(0.05)
        holds.hold(8, "refreshing")
    assert holds.groups == {8}
    assert holds.holders(8) == {"refreshing"}
    await wait_until(lambda: holds.groups == frozenset(), within=1.0)
    holds.stop()


async def test_a_closed_connection_lets_go_of_everything_it_held() -> None:
    holds = BumpHolds(Changes())
    holds.hold(7, 1)
    holds.hold(8, 1)
    holds.hold(8, 2)
    assert holds.release_holder(1) == {7}
    assert holds.groups == {8} and holds.holders(8) == {2}
    holds.release_all()
    assert holds.groups == frozenset()
    holds.stop()


def test_the_client_refreshes_well_inside_the_timeout() -> None:
    assert BUMP_HOLD_TIMEOUT_S >= 3 * BUMP_REFRESH_S


# -- the lighting service -------------------------------------------------------------


@pytest.fixture
async def svc(state: StateStore) -> AsyncIterator[tuple[LightingService, FakeDevices, FakeKnx]]:
    bus = EventBus()
    await bus.start()
    devices = FakeDevices()
    devices.connect()
    sink = FakeKnx()
    service = LightingService(state, bus, None, devices, sink, bump_timeout_s=0.3)
    await service.start(CFG)
    for channel_id in (1, 2, 3, 4):
        service.set_level(channel_id, 30.0)
    try:
        yield service, devices, sink
    finally:
        await service.stop()
        await bus.stop()


async def test_a_held_bump_flashes_the_groups_dmx_members_and_release_restores(
    svc: tuple[LightingService, FakeDevices, FakeKnx],
) -> None:
    service, devices, sink = svc
    service.set_master(50.0)
    out = devices.output()
    await wait_until(lambda: out.sent and out.last()[0] == level_to_dmx(15.0))
    knx_before = list(sink.writes)

    service.bump_group(7, "tablet", held=True)
    await wait_until(lambda: out.last()[0] == level_to_dmx(50.0))
    assert out.last()[1] == level_to_dmx(15.0)  # fixture 2 is not in the group
    assert out.last()[2] == level_to_dmx(40.0)  # capped at 80, × master
    assert service.output_level(1) == 50.0
    assert service.bumped_groups == {7}
    await asyncio.sleep(0.05)
    assert sink.writes == knx_before  # the house dimmer in the group: untouched
    assert [service._view.level(c) for c in (1, 3, 4)] == [30.0] * 3  # noqa: SLF001

    service.bump_group(7, "tablet", held=False)
    await wait_until(lambda: out.last()[0] == level_to_dmx(15.0))
    assert out.last()[2] == level_to_dmx(15.0)
    assert service.bumped_groups == frozenset()


async def test_two_holders_on_one_group_release_only_when_both_have(
    svc: tuple[LightingService, FakeDevices, FakeKnx],
) -> None:
    service, devices, _ = svc
    out = devices.output()
    service.bump_group(7, 1, held=True)
    service.bump_group(7, 2, held=True)
    await wait_until(lambda: out.sent and out.last()[0] == 255)
    service.bump_group(7, 1, held=False)
    await asyncio.sleep(0.06)
    assert out.last()[0] == 255
    service.bump_group(7, 2, held=False)
    await wait_until(lambda: out.last()[0] == level_to_dmx(30.0))


async def test_a_bump_the_client_stops_refreshing_is_released_by_the_timeout(
    svc: tuple[LightingService, FakeDevices, FakeKnx],
) -> None:
    service, devices, _ = svc
    out = devices.output()
    service.bump_group(7, "tablet", held=True)
    await wait_until(lambda: out.sent and out.last()[0] == 255)
    await wait_until(lambda: out.last()[0] == level_to_dmx(30.0), within=1.0)
    assert service.bumped_groups == frozenset()


async def test_releasing_a_holder_ends_its_bumps(
    svc: tuple[LightingService, FakeDevices, FakeKnx],
) -> None:
    service, devices, _ = svc
    out = devices.output()
    service.bump_group(7, 5, held=True)
    await wait_until(lambda: out.sent and out.last()[0] == 255)
    assert service.release_bumps(5) == {7}
    await wait_until(lambda: out.last()[0] == level_to_dmx(30.0))


async def test_external_control_releases_every_bump_and_refuses_new_ones(
    svc: tuple[LightingService, FakeDevices, FakeKnx],
) -> None:
    service, _, _ = svc
    service.bump_group(7, "tablet", held=True)
    service.set_external_manual(True)
    assert service.bumped_groups == frozenset()
    assert service.compositor.bumped == frozenset()
    with pytest.raises(BumpRefusedError) as refused:
        service.bump_group(7, "tablet", held=True)
    assert refused.value.reason == "external_control"
    service.bump_group(7, "tablet", held=False)  # a release is never refused
    service.set_external_manual(False)
    assert service.bumped_groups == frozenset()  # nothing waiting to flash on resume


async def test_indicator_only_unknown_and_house_only_groups_are_refused(
    svc: tuple[LightingService, FakeDevices, FakeKnx],
) -> None:
    service, _, _ = svc
    with pytest.raises(IndicatorOnlyGroupError):
        service.bump_group(9, "tablet", held=True)
    with pytest.raises(UnknownGroupError):
        service.bump_group(99, "tablet", held=True)
    with pytest.raises(BumpRefusedError) as refused:
        service.bump_group(8, "tablet", held=True)
    assert refused.value.reason == "no_dmx_members"
    assert service.bumped_groups == frozenset()


async def test_a_member_a_critical_scene_locks_is_not_bumped(
    svc: tuple[LightingService, FakeDevices, FakeKnx],
) -> None:
    service, _, _ = svc
    service.bump_group(7, "tablet", held=True)
    assert service.compositor.bumped == {1, 3}
    run = SceneRun(scene_id=1, priority="critical")
    service.begin_critical_scene(run, [1])
    assert service.compositor.bumped == {3}
    service.release_scene(run)
    assert service.compositor.bumped == {1, 3}
    service.bump_group(7, "tablet", held=False)


async def test_a_configuration_reload_lets_go_of_a_group_that_became_indicator_only(
    svc: tuple[LightingService, FakeDevices, FakeKnx],
) -> None:
    service, _, _ = svc
    service.bump_group(7, "tablet", held=True)
    service.apply_config(
        config(
            dmx(1, 1),
            dmx(2, 2),
            dmx(3, 3, max_value=80.0),
            knx(4, HOUSE_GA),
            groups={7: {1, 3, 4}},
            indicator_only={7},
        )
    )
    assert service.bumped_groups == frozenset()
    assert service.compositor.bumped == frozenset()
