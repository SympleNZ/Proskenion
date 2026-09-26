"""Hirer ceilings outside a hirer's own write: the pull-down and the clamp after a recall.

Spec §6.7 (the live-effect table: a lowered ceiling pulls the fader down and
writes it to the mixer), §16.6 ("the fader is clamped immediately after
recall") and the phase-5 plan's Q8: (a) a lowered ceiling — and access being
enabled — pulls down whenever hirer access is enabled, whether or not a hirer
is connected; (b) a run a hirer started is clamped, its ``mixer_fader``
actions included; (c) a run staff started, Restore Venue Default included, is
never clamped.

The recall tests run the real CQ-20B driver and mixer service against the
MIDI stub, whose desk scene 2 raises input 1 to 0 dB. Whether a run is
hirer-originated is the context's ``hirer_originated`` flag, set here
directly; the pages API sets it when a hirer presses a page button.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

import pytest

from proskenion.core.auth import hash_secret
from proskenion.core.broadcast import Broadcaster
from proskenion.core.bus import EventBus
from proskenion.core.dmx.fade import SceneRun
from proskenion.core.drivers.cq20b import CQ20BDriver
from proskenion.core.hirer_access import HirerAccess
from proskenion.core.hirer_enforcement import (
    CeilingEnforcer,
    ceilings_of,
    clamp_to_ceiling,
    hirer_may_colour,
    hirer_may_write,
)
from proskenion.core.hirer_permissions import HirerPermissions
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import hirer as hirer_crud
from proskenion.db.crud import mixer as mixer_crud
from proskenion.scene.domains import ActionContext
from proskenion.scene.mixer_handlers import MixerFaderHandler, MixerRecallHandler
from tests.stubs.cq_midi_stub import CqMidiStub
from tests.unit.scene.test_mixer_handlers import (
    _SCENE,
    IP1_LEVEL,
    IP2_LEVEL,
    _channel,
    make_action,
    mixer_rig,
    until,
)

ZERO_DB = 12544  # the stub's 14-bit value for 0 dB
SCENE_2_RAISES_INPUTS: dict[int, dict[tuple[int, int], int]] = {
    2: {IP1_LEVEL: ZERO_DB, IP2_LEVEL: ZERO_DB}
}


def ctx(*, hirer: bool) -> ActionContext:
    return ActionContext(
        run=SceneRun(scene_id=1),
        scene=_SCENE,
        triggered_by="page:40" if hirer else "api:operator",
        hirer_originated=hirer,
    )


def permissions(ceilings: Mapping[int, float | None], **kwargs: Any) -> HirerPermissions:
    return HirerPermissions(mixer_ceilings=MappingProxyType(dict(ceilings)), **kwargs)


# -- the checks ---------------------------------------------------------------------------


def test_a_ceiling_clamps_only_what_is_above_it() -> None:
    assert clamp_to_ceiling(6.0, -10.0) == (-10.0, True)
    assert clamp_to_ceiling(-10.0, -10.0) == (-10.0, False)
    assert clamp_to_ceiling(-20.0, -10.0) == (-20.0, False)
    assert clamp_to_ceiling(None, -10.0) == (None, False)  # off is never above
    assert clamp_to_ceiling(6.0, None) == (6.0, False)  # no ceiling


def test_what_a_hirer_may_write() -> None:
    snapshot = permissions(
        {1: -10.0, 2: None},
        lighting_enabled=True,
        colour_enabled=False,
        lighting_channels=frozenset({7, 8}),
        writable_lighting_channels=frozenset({7}),
        groups=frozenset({3}),
    )
    assert hirer_may_write(snapshot, "mixer", 1) and hirer_may_write(snapshot, "mixer", 2)
    assert not hirer_may_write(snapshot, "mixer", 9)
    assert hirer_may_write(snapshot, "lighting", 7)
    assert not hirer_may_write(snapshot, "lighting", 8)
    assert hirer_may_write(snapshot, "lighting_group", 3)
    assert not hirer_may_write(snapshot, "lighting_group", 4)
    assert not hirer_may_write(snapshot, "master", None)
    assert not hirer_may_write(snapshot, "mixer", None)
    assert not hirer_may_colour(snapshot, 7)  # colour is off
    assert ceilings_of(snapshot) == {1: -10.0}


# -- the pull-down triggers (Q8a) --------------------------------------------------------------


class RecordingMixer:
    """The mixer service as the pull-down sees it."""

    def __init__(self) -> None:
        self.calls: list[dict[int, float]] = []

    async def pull_down(self, ceilings: Mapping[int, float]) -> dict[int, float]:
        self.calls.append(dict(ceilings))
        return dict(ceilings)


async def test_the_pull_down_follows_access_and_lowered_ceilings(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    await hirer_crud.set_pin_hash(db, hash_secret("246810", rounds=4), updated_by=None)
    broadcaster = Broadcaster(state, bus)
    access = HirerAccess(state, broadcaster)
    await access.load(db)
    access.publish_permissions(permissions({1: -10.0, 2: None, 3: -3.0}))
    mixer = RecordingMixer()
    enforcer = CeilingEnforcer(state, lambda: mixer)
    await enforcer.start()
    try:
        # Access disabled: a lowered ceiling moves nothing.
        handled = enforcer.handled
        access.publish_permissions(permissions({1: -20.0, 2: None, 3: -3.0}))
        await until(lambda: enforcer.handled > handled)
        assert mixer.calls == []

        # Enabling applies every ceiling at once; a channel with none is not asked.
        await access.set_enabled(db, True, actor="admin", ip_address=None)
        await until(lambda: len(mixer.calls) == 1)
        assert mixer.calls == [{1: -20.0, 3: -3.0}]

        # Enabled: a lowered ceiling pulls exactly that channel down.
        access.publish_permissions(permissions({1: -20.0, 2: -12.0, 3: -3.0}))
        await until(lambda: len(mixer.calls) == 2)
        assert mixer.calls[1] == {2: -12.0}

        # A raised ceiling is not a pull-down; the next lowering still is.
        access.publish_permissions(permissions({1: -5.0, 2: -12.0, 3: -3.0}))
        access.publish_permissions(permissions({1: -5.0, 2: -12.0, 3: -9.0}))
        await until(lambda: len(mixer.calls) == 3)
        assert mixer.calls[2] == {3: -9.0}
    finally:
        await enforcer.stop()


# -- MixerService.pull_down over the real driver --------------------------------------------------


async def test_pull_down_moves_only_what_is_above_and_one_ceiling_is_one_driver_call(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    async with CqMidiStub() as stub:
        async with mixer_rig(db, state, bus, stub) as rig:
            in1 = await _channel(db, rig, bus, name="Wireless 1", refs=["ip1"])
            in2 = await _channel(db, rig, bus, name="Wireless 2", refs=["ip2"])
            in3 = await _channel(db, rig, bus, name="Lectern", refs=["ip3"])
            for channel, level in ((in1, 0.0), (in2, 3.0), (in3, -30.0)):
                await rig.service.set_level(channel.id, level)
            driver = rig.devices.running_driver(rig.device_id)
            assert driver is not None
            calls: list[tuple[list[str], float | None]] = []
            original = driver.set_level  # type: ignore[attr-defined]

            async def counted(refs: list[str], level: float | None) -> None:
                calls.append((list(refs), level))
                await original(refs, level)

            driver.set_level = counted  # type: ignore[attr-defined]

            moved = await rig.service.pull_down({in1.id: -10.0, in2.id: -10.0, in3.id: -10.0})

            assert moved == {in1.id: -10.0, in2.id: -10.0}
            assert calls == [(["ip1", "ip2"], -10.0)]  # one intent, one call (B47)
            await until(lambda: (rig.service.live(in2.id) or _no_live()).db != 3.0)
            assert rig.service.live(in1.id).db == pytest.approx(-10.0, abs=0.2)  # type: ignore[union-attr]
            assert rig.service.live(in3.id).db == pytest.approx(-30.0, abs=0.2)  # type: ignore[union-attr]
            assert await rig.service.pull_down({in3.id: -10.0}) == {}


def _no_live() -> Any:
    raise AssertionError("the channel has no live state")


# -- the clamp after a recall (Q8b, Q8c) ----------------------------------------------------------


async def _recall_rig_scene(db: Database, device_id: int, *, venue_default: bool = False) -> int:
    scene = await mixer_crud.create_desk_scene(
        db,
        device_id=device_id,
        scene_ref="2",
        name="Band",
        is_venue_default=venue_default,
    )
    return scene.id


async def test_a_hirer_recall_is_clamped_after_its_resync_lands(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    async with CqMidiStub(recall_delay=0.02, presets=SCENE_2_RAISES_INPUTS) as stub:
        async with mixer_rig(db, state, bus, stub) as rig:
            in1 = await _channel(db, rig, bus, name="Wireless 1", refs=["ip1"])
            in2 = await _channel(db, rig, bus, name="Wireless 2", refs=["ip2"])
            await rig.service.set_level(in1.id, -40.0)
            await rig.service.set_level(in2.id, -40.0)
            scene_id = await _recall_rig_scene(db, rig.device_id)
            # Input 2 is reachable with no ceiling; input 1 is held to -10 dB.
            snapshot = permissions({in1.id: -10.0, in2.id: None})
            handler = MixerRecallHandler(rig.service, db, lambda: snapshot)

            outcome = await handler.execute(
                make_action("mixer_recall", mixer_scene_id=scene_id), ctx(hirer=True)
            )

            assert outcome.result == "confirmed", outcome.reason
            assert outcome.detail["clamped"] == {str(in1.id): -10.0}
            assert rig.service.live(in1.id).db == pytest.approx(-10.0, abs=0.2)  # type: ignore[union-attr]
            assert rig.service.live(in2.id).db == pytest.approx(0.0, abs=0.2)  # type: ignore[union-attr]
            await until(lambda: stub.value(IP1_LEVEL) < ZERO_DB)


@pytest.mark.parametrize("venue_default", [False, True])
async def test_a_staff_recall_is_never_clamped(
    db: Database, state: StateStore, bus: EventBus, venue_default: bool
) -> None:
    """Q8c: including Restore Venue Default (a recall with no desk scene named)."""
    async with CqMidiStub(recall_delay=0.02, presets=SCENE_2_RAISES_INPUTS) as stub:
        async with mixer_rig(db, state, bus, stub) as rig:
            in1 = await _channel(db, rig, bus, name="Wireless 1", refs=["ip1"])
            await rig.service.set_level(in1.id, -40.0)
            scene_id = await _recall_rig_scene(db, rig.device_id, venue_default=venue_default)
            snapshot = permissions({in1.id: -10.0}, enabled=True)
            handler = MixerRecallHandler(rig.service, db, lambda: snapshot)
            action = make_action("mixer_recall", mixer_scene_id=None if venue_default else scene_id)

            outcome = await handler.execute(action, ctx(hirer=False))

            assert outcome.result == "confirmed", outcome.reason
            assert "clamped" not in outcome.detail
            await until(lambda: stub.value(IP1_LEVEL) == ZERO_DB)
            assert rig.service.live(in1.id).db == pytest.approx(0.0, abs=0.2)  # type: ignore[union-attr]


async def test_a_hirer_runs_fader_actions_are_clamped_and_a_staff_runs_are_not(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    async with CqMidiStub() as stub:
        async with mixer_rig(db, state, bus, stub) as rig:
            in1 = await _channel(db, rig, bus, name="Wireless 1", refs=["ip1"])
            snapshot = permissions({in1.id: -10.0})
            handler = MixerFaderHandler(rig.service, lambda: snapshot)
            loud = make_action("mixer_fader", mixer_channel_id=in1.id, mixer_db=6.0)

            hirer = await handler.execute(loud, ctx(hirer=True))
            assert hirer.result == "confirmed"
            assert hirer.detail == {"channel_id": in1.id, "db": -10.0, "clamped": True}

            staff = await handler.execute(loud, ctx(hirer=False))
            assert staff.result == "confirmed"
            assert staff.detail == {"channel_id": in1.id, "db": 6.0}
            await until(lambda: stub.value(IP1_LEVEL) > ZERO_DB)


# -- a desk that was offline when a pull-down fired (§6.7, Q8a) ---------------------------------


@pytest.mark.parametrize("enabled", [True, False])
async def test_a_ceiling_lowered_while_the_desk_is_offline_is_applied_after_its_resync(
    db: Database,
    state: StateStore,
    bus: EventBus,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
) -> None:
    """The pull-down cannot reach an offline desk, and the desk comes back
    with its stored level. While access is enabled the ceiling is applied
    once the reconnection's opening sync has landed — never before it, so
    the desk's own value is what is judged. While access is disabled nothing
    is sent at all."""
    monkeypatch.setattr(CQ20BDriver, "INITIAL_RETRY_DELAY", 0.05)  # reconnect promptly
    await hirer_crud.set_pin_hash(db, hash_secret("246810", rounds=4), updated_by=None)
    access = HirerAccess(state, Broadcaster(state, bus))
    await access.load(db)
    if enabled:
        await access.set_enabled(db, True, actor="admin", ip_address=None)
    async with CqMidiStub(state={IP1_LEVEL: ZERO_DB}) as stub:
        async with mixer_rig(db, state, bus, stub) as rig:
            in1 = await _channel(db, rig, bus, name="Wireless 1", refs=["ip1"])
            await until(lambda: _db(rig.service.live(in1.id)) == pytest.approx(0.0, abs=0.2))
            access.publish_permissions(permissions({in1.id: None}))
            enforcer = CeilingEnforcer(state, lambda: rig.service)
            await enforcer.start()
            try:
                await stub.refuse_connections()
                await until(lambda: not stub.connected)

                handled = enforcer.handled
                access.publish_permissions(permissions({in1.id: -10.0}))
                await until(lambda: enforcer.handled > handled)
                assert stub.value(IP1_LEVEL) == ZERO_DB  # nothing could reach the desk

                stub.clear_record()
                handled = enforcer.handled
                await stub.accept_connections()
                await until(lambda: enforcer.handled > handled)  # the resync was considered

                kinds = [(m.kind, m.address) for m in stub.messages]
                assert ("get", IP1_LEVEL) in kinds  # the connection's sync read the desk
                sets = [i for i, (kind, _) in enumerate(kinds) if kind == "set"]
                if enabled:
                    assert [kinds[i] for i in sets] == [("set", IP1_LEVEL)]
                    last_get = max(i for i, (kind, _) in enumerate(kinds) if kind == "get")
                    assert sets[0] > last_get  # after the resync, never before it
                    await until(lambda: stub.value(IP1_LEVEL) < ZERO_DB)
                    level = _db(rig.service.live(in1.id))
                    assert level == pytest.approx(-10.0, abs=0.2)
                else:
                    assert sets == []
                    assert stub.value(IP1_LEVEL) == ZERO_DB
                    assert _db(rig.service.live(in1.id)) == pytest.approx(0.0, abs=0.2)
            finally:
                await enforcer.stop()


def _db(live: Any) -> float | None:
    assert live is not None
    level: float | None = live.db
    return level
