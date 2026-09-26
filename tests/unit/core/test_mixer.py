"""The mixer service against the CQ-20B stubs, through the real driver (spec
§7.3, §5.6, §21.13).

Every test runs the real :class:`~proskenion.core.drivers.cq20b.CQ20BDriver`
over the TCP transport against :mod:`tests.stubs.cq_midi_stub` (and, for the
meter-mapping tests, :mod:`tests.stubs.cq_native_stub` too), supervised by the
real :class:`~proskenion.core.devices.DeviceManager` — the same stack the API
and the scene engine sit on top of, mirroring
``tests/unit/core/test_projector.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import pytest

from proskenion.config import Config
from proskenion.core.bus import EventBus
from proskenion.core.devices import DeviceManager
from proskenion.core.drivers.cq20b import CQ20BDriver
from proskenion.core.events import MixerConfigChanged
from proskenion.core.mixer.native import NativeMeterClient
from proskenion.core.mixer.service import MixerService, _meter_refs
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import mixer as mixer_crud
from tests.stubs.cq_midi_stub import CqMidiStub
from tests.stubs.cq_native_stub import CqNativeStub

IP1_LEVEL, IP1_MUTE = (0x40, 0x00), (0x00, 0x00)
IP2_LEVEL, IP2_MUTE = (0x40, 0x01), (0x00, 0x01)
MAIN_LEVEL, MAIN_MUTE = (0x4F, 0x00), (0x00, 0x44)
OUT1_LEVEL, OUT1_MUTE = (0x4F, 0x01), (0x00, 0x45)
OUT2_LEVEL = (0x4F, 0x02)


async def until(condition: Callable[[], bool], limit: float = 5.0) -> None:
    async with asyncio.timeout(limit):
        while not condition():  # noqa: ASYNC110 - conditions span the stub, driver and service
            await asyncio.sleep(0.01)


def _muted(rig: Rig, channel_id: int) -> bool | None:
    live = rig.service.live(channel_id)
    return None if live is None else live.muted


def _origin(rig: Rig, channel_id: int) -> str | None:
    live = rig.service.live(channel_id)
    return None if live is None else live.origin


@dataclass
class Rig:
    bus: EventBus
    state: StateStore
    devices: DeviceManager
    service: MixerService
    device_id: int
    driver: CQ20BDriver


async def _build(
    db: Database,
    dev_config: Config,
    stub: CqMidiStub,
    *,
    native_port: int | None = None,
) -> Rig:
    bus = EventBus()
    await bus.start()
    state = StateStore(dev_config, bus)
    driver_config: dict[str, Any] = {"metering": native_port is not None}
    device = await devices_crud.create(
        db,
        category="mixer",
        driver_key="cq20b",
        name="Mixer",
        config={
            "transport": {"type": "tcp", "host": "127.0.0.1", "port": stub.port},
            "driver": driver_config,
        },
    )
    manager = DeviceManager(
        db, state, bus, dev_config, connect_timeout=2.0, probe_timeout=1.0, stop_timeout=2.0
    )
    if native_port is not None:
        # A throwaway instance for CONFIG_SCHEMA discovery is never built by
        # DeviceManager itself; the native port is instead pinned on every
        # CQ20BDriver instance this manager builds, mirroring
        # tests/unit/core/drivers/test_cq20b_metering.py's own pattern.
        CQ20BDriver.NATIVE_PORT = native_port  # type: ignore[misc]
    await manager.start()
    await manager.wait_for_connection(device.id)
    driver = manager.running_driver(device.id)
    assert isinstance(driver, CQ20BDriver)
    service = MixerService(state, bus, db, manager)
    await service.start()
    return Rig(bus, state, manager, service, device.id, driver)


async def _teardown(rig: Rig) -> None:
    await rig.service.stop()
    await rig.devices.stop()
    await rig.bus.stop()
    CQ20BDriver.NATIVE_PORT = 51326  # type: ignore[misc] - restore the class default


async def _channel(
    db: Database,
    device_id: int,
    *,
    kind: str = "input",
    name: str = "Channel",
    refs: list[str],
    show_pan: bool = False,
    tracked: bool = True,
) -> mixer_crud.MixerChannel:
    channel = await mixer_crud.create_channel(
        db, device_id=device_id, channel_kind=kind, name=name, show_pan=show_pan, tracked=tracked
    )
    await mixer_crud.set_channel_refs(db, channel.id, refs)
    return channel


# -- _meter_refs: pure-function mapping (§7.3, B58) -----------------------------------


def test_meter_refs_stereo_and_linked_pairs_are_ordered_left_first() -> None:
    assert _meter_refs("main") == ("mainl", "mainr")
    assert _meter_refs("st1") == ("st1l", "st1r")
    assert _meter_refs("st2") == ("st2l", "st2r")
    assert _meter_refs("usb") == ("usbl", "usbr")
    assert _meter_refs("bt") == ("btl", "btr")
    assert _meter_refs("out12") == ("out1", "out2")
    assert _meter_refs("out34") == ("out3", "out4")
    assert _meter_refs("out56") == ("out5", "out6")


def test_meter_refs_a_mono_reference_meters_as_itself() -> None:
    assert _meter_refs("ip1") == ("ip1",)
    assert _meter_refs("ip16") == ("ip16",)
    assert _meter_refs("out1") == ("out1",)
    assert _meter_refs("out6") == ("out6",)


# -- a level write reaches the stub and the frame (§7.3, §16.8) -----------------------


async def test_level_write_reaches_the_stub_and_the_frame(db: Database, dev_config: Config) -> None:
    async with CqMidiStub() as stub:
        # The channel is created before the service starts, so its own
        # start-up load of the channel index (§7.3) is what is exercised.
        rig, channel = await _built_with_one_channel(db, dev_config, stub, refs=["ip1"])
        try:
            applied = await rig.service.set_level(channel.id, -10.0)
            assert applied is not None

            await until(lambda: stub.value(IP1_LEVEL) != 0)
            live = rig.service.live(channel.id)
            assert live is not None and live.db == pytest.approx(applied, abs=0.01)
            frame_entry = rig.state.mixer.get_item("inputs", channel.id)
            assert isinstance(frame_entry, dict)
            assert frame_entry["db"] == pytest.approx(applied, abs=0.01)
            assert "origin" not in frame_entry  # our own write; no badge
        finally:
            await _teardown(rig)


# -- mute toggle sends an absolute mute (§7.3, cq20b.md §2) ---------------------------


async def test_mute_toggle_sends_an_absolute_mute(db: Database, dev_config: Config) -> None:
    async with CqMidiStub() as stub:
        rig, channel = await _built_with_one_channel(db, dev_config, stub, refs=["ip1"])
        try:
            await until(lambda: rig.service.live(channel.id) is not None)
            assert _muted(rig, channel.id) is False

            await rig.service.toggle_mute(channel.id)
            await until(lambda: stub.value(IP1_MUTE) == 1)
            await until(lambda: _muted(rig, channel.id) is True)

            await rig.service.toggle_mute(channel.id)
            await until(lambda: stub.value(IP1_MUTE) == 0)

            # Never a toggle (VF=00 increment/decrement on a mute address):
            # cq20b.md §2, the driver's own mute-safety rule.
            assert stub.mute_toggles == []
            set_messages = [m for m in stub.messages_of("set") if m.address == IP1_MUTE]
            assert [m.value for m in set_messages] == [1, 0]
        finally:
            await _teardown(rig)


# -- a MixPad push sets the badge; a later app write clears it (§7.3, §21.13) ---------


async def test_mixpad_push_badges_and_a_later_app_write_clears_it(
    db: Database, dev_config: Config
) -> None:
    async with CqMidiStub() as stub:
        rig, channel = await _built_with_one_channel(db, dev_config, stub, refs=["ip1"])
        try:
            await until(lambda: rig.service.live(channel.id) is not None)

            # A change MixPad made, echoed to every MIDI client including us
            # (cq20b.md §1) — not one of our own writes, so it is external.
            await stub.push(IP1_LEVEL, 10048)  # -5 dB (cq20b.md §4)
            await until(lambda: _origin(rig, channel.id) == "mixpad")
            entry = rig.state.mixer.get_item("inputs", channel.id)
            assert isinstance(entry, dict) and entry["origin"] == "mixpad"

            await rig.service.set_level(channel.id, 0.0)
            await until(lambda: _origin(rig, channel.id) is None)
            entry = rig.state.mixer.get_item("inputs", channel.id)
            assert isinstance(entry, dict) and "origin" not in entry
        finally:
            await _teardown(rig)


# -- a recall's resync sets no badge (§7.3 *Change origin tracking*) ------------------


async def test_recall_resync_sets_no_badge(db: Database, dev_config: Config) -> None:
    preset = {IP1_LEVEL: 10048}  # -5 dB, a value distinct from the channel's start
    async with CqMidiStub(presets={2: preset}, recall_delay=0.05) as stub:
        rig, channel = await _built_with_one_channel(db, dev_config, stub, refs=["ip1"])
        scene = await mixer_crud.create_desk_scene(
            db, device_id=rig.device_id, scene_ref="2", name="Lecture Baseline"
        )
        try:
            await until(lambda: rig.service.live(channel.id) is not None)

            await rig.service.recall_desk_scene(scene.id)

            live = rig.service.live(channel.id)
            assert live is not None and live.db == pytest.approx(-5.0, abs=0.5)
            assert _origin(rig, channel.id) is None
            entry = rig.state.mixer.get_item("inputs", channel.id)
            assert isinstance(entry, dict) and "origin" not in entry
            assert rig.service.last_recalled_scene == {"id": scene.id, "name": "Lecture Baseline"}
        finally:
            await _teardown(rig)


# -- a recall's resync records observed levels (Phase 5 contracts, "GET /hirer/conflicts") ---


async def test_recall_records_observed_levels_for_tracked_channels_only(
    db: Database, dev_config: Config
) -> None:
    """The mixer service writes ``mixer_desk_scene_observed`` from the resync
    that follows a recall, so a ceiling-conflict check has something to
    compare against without recalling the scene itself (§7.3). Only tracked
    channels are recorded — an untracked channel is never queried on a
    resync at all (§21.21), so it has nothing to observe."""
    preset = {IP1_LEVEL: 10048}  # -5 dB (cq20b.md §4)
    async with CqMidiStub(presets={2: preset}, recall_delay=0.05) as stub:
        rig, tracked = await _built_with_one_channel(db, dev_config, stub, refs=["ip1"])
        untracked = await _channel(
            db, rig.device_id, name="Aux", refs=["ip2"], tracked=False
        )
        rig.bus.emit(MixerConfigChanged(reason="test"))
        await until(lambda: rig.service.is_configured(untracked.id))
        scene = await mixer_crud.create_desk_scene(
            db, device_id=rig.device_id, scene_ref="2", name="Band"
        )
        try:
            await until(lambda: rig.service.live(tracked.id) is not None)

            await rig.service.recall_desk_scene(scene.id)

            observed = {o.channel_id: o for o in await mixer_crud.get_observed_levels(db, scene.id)}
            assert set(observed) == {tracked.id}  # the untracked channel is absent, not zero
            assert observed[tracked.id].db == pytest.approx(-5.0, abs=0.5)
        finally:
            await _teardown(rig)


async def test_a_second_recall_replaces_the_first_scenes_observed_levels(
    db: Database, dev_config: Config
) -> None:
    """``mixer_desk_scene_observed`` is replaced wholesale per desk scene
    (migration 006), so a stale level from a previous recall of the same
    scene never lingers."""
    async with CqMidiStub(presets={2: {IP1_LEVEL: 10048}}, recall_delay=0.05) as stub:
        rig, channel = await _built_with_one_channel(db, dev_config, stub, refs=["ip1"])
        scene = await mixer_crud.create_desk_scene(
            db, device_id=rig.device_id, scene_ref="2", name="Band"
        )
        try:
            await until(lambda: rig.service.live(channel.id) is not None)

            await rig.service.recall_desk_scene(scene.id)
            [first] = await mixer_crud.get_observed_levels(db, scene.id)
            assert first.db == pytest.approx(-5.0, abs=0.5)

            await rig.service.set_level(channel.id, 0.0)
            await until(lambda: stub.value(IP1_LEVEL) != 10048)
            await rig.service.recall_desk_scene(scene.id)

            [second] = await mixer_crud.get_observed_levels(db, scene.id)
            assert second.db == pytest.approx(-5.0, abs=0.5)  # recalled back
            assert second.observed_at != first.observed_at
        finally:
            await _teardown(rig)


# -- meters map to channel ids, stereo and linked pairs in order (§7.3, B58) ----------


async def test_meters_map_to_channel_ids_main_stereo_pair_in_order(
    db: Database, dev_config: Config
) -> None:
    async with CqMidiStub() as stub, CqNativeStub() as native:
        rig = await _build(db, dev_config, stub, native_port=native.port)
        try:
            main_ref = next(r.ref for r in rig.driver.available_refs() if r.kind == "main")
            main_channel = await _channel(
                db, rig.device_id, kind="main", name="Main LR", refs=[main_ref]
            )
            rig.bus.emit(MixerConfigChanged(reason="test"))
            await until(lambda: rig.service.is_tracked(main_channel.id))

            raw: dict[str, float | None] = {}

            async def capture(levels: Mapping[str, float | None]) -> None:
                raw.update(levels)

            rig.driver.add_meter_listener(capture)

            await until(lambda: "mainl" in raw and "mainr" in raw)

            def _meters_match() -> bool:
                meters = rig.state.mixer.get_item("meters", main_channel.id)
                return meters == [raw["mainl"], raw["mainr"]]

            await until(_meters_match)
        finally:
            await _teardown(rig)


async def test_meters_for_a_ganged_two_output_channel_are_in_reference_order(
    db: Database, dev_config: Config
) -> None:
    async with CqMidiStub() as stub, CqNativeStub() as native:
        rig = await _build(db, dev_config, stub, native_port=native.port)
        try:
            channel = await _channel(
                db, rig.device_id, kind="output", name="Out 1+2", refs=["out1", "out2"]
            )
            rig.bus.emit(MixerConfigChanged(reason="test"))
            await until(lambda: rig.service.is_tracked(channel.id))

            raw: dict[str, float | None] = {}

            async def capture(levels: Mapping[str, float | None]) -> None:
                raw.update(levels)

            rig.driver.add_meter_listener(capture)

            await until(lambda: "out1" in raw and "out2" in raw)
            await until(
                lambda: rig.state.mixer.get_item("meters", channel.id) == [raw["out1"], raw["out2"]]
            )
        finally:
            await _teardown(rig)


async def test_metering_unavailable_clears_every_meter(db: Database, dev_config: Config) -> None:
    """§21.9, B58: "where metering is unavailable, show nothing" — every
    meter is cleared at once rather than left to go stale, and the closed
    reason is "no_response": a session that was up and then went quiet
    (``docs/plans/phase-4-contracts.md``; see
    ``proskenion/core/mixer/native.py``'s ``_lost_message``)."""
    async with CqMidiStub() as stub, CqNativeStub() as native:
        rig = await _build(db, dev_config, stub, native_port=native.port)
        try:
            channel = await _channel(db, rig.device_id, name="Wireless 1", refs=["ip1"])
            rig.bus.emit(MixerConfigChanged(reason="test"))
            await until(lambda: rig.service.is_tracked(channel.id))
            # Metering comes up and at least one meter value is recorded.
            await until(lambda: rig.driver.capabilities().supports_metering)
            await until(lambda: rig.state.mixer.get_item("meters", channel.id) is not None)
            assert rig.service.metering_reason is None

            await native.stop()  # the native session drops

            await until(lambda: not rig.driver.capabilities().supports_metering)
            await until(lambda: rig.state.mixer.get("meters") == {})
            await until(lambda: rig.service.metering_reason == "no_response")
            assert rig.state.mixer.get("metering") == {"available": False, "reason": "no_response"}
        finally:
            await _teardown(rig)


async def test_metering_refused_reports_the_closed_reason(
    db: Database, dev_config: Config
) -> None:
    """Both MixPad slots already taken (cq20b-native.md §9): the native
    connection is refused before it ever gets in, distinct from one that
    connected and then went quiet — "refused", not "no_response"."""
    async with CqMidiStub() as stub, CqNativeStub(max_connections=0) as native:
        rig = await _build(db, dev_config, stub, native_port=native.port)
        try:
            await until(lambda: rig.service.metering_reason == "refused")
            assert rig.driver.capabilities().supports_metering is False
            assert rig.state.mixer.get("metering") == {"available": False, "reason": "refused"}
            assert rig.state.mixer.get("meters") == {}
        finally:
            await _teardown(rig)


async def test_metering_recovers_and_clears_the_reason(db: Database, dev_config: Config) -> None:
    """A session lost and then re-established (§7.3: "the native connection
    needs its own retry state ... so metering returns once a slot frees")
    clears the reason and lets meters flow again — never replayed, a fresh
    reading."""
    original_delay = NativeMeterClient.INITIAL_RETRY_DELAY
    NativeMeterClient.INITIAL_RETRY_DELAY = 0.05  # the machine is shared; do not wait 5 s
    async with CqMidiStub() as stub, CqNativeStub() as native:
        rig = await _build(db, dev_config, stub, native_port=native.port)
        try:
            channel = await _channel(db, rig.device_id, name="Wireless 1", refs=["ip1"])
            rig.bus.emit(MixerConfigChanged(reason="test"))
            await until(lambda: rig.service.is_tracked(channel.id))
            await until(lambda: rig.driver.capabilities().supports_metering)
            await until(lambda: rig.service.metering_reason is None)

            await native.drop_all_connections()  # the port stays open; the client retries it
            await until(lambda: rig.service.metering_reason == "no_response")

            await until(lambda: rig.driver.capabilities().supports_metering, limit=10.0)
            await until(lambda: rig.service.metering_reason is None)
            assert rig.state.mixer.get("metering") == {"available": True, "reason": None}
        finally:
            await _teardown(rig)
            NativeMeterClient.INITIAL_RETRY_DELAY = original_delay


async def test_stub_driver_metering_is_unsupported(db: Database, dev_config: Config) -> None:
    """The stub mixer driver has no metering at all — never "refused" (it
    was never offered a connection to be refused) — see
    ``proskenion/core/drivers/stub_mixer.py``."""
    bus = EventBus()
    await bus.start()
    state = StateStore(dev_config, bus)
    device = await devices_crud.create(
        db,
        category="mixer",
        driver_key="stub",
        name="Mixer",
        config={"transport": {"type": "loopback"}, "driver": {}},
    )
    manager = DeviceManager(
        db, state, bus, dev_config, connect_timeout=2.0, probe_timeout=1.0, stop_timeout=2.0
    )
    await manager.start()
    await manager.wait_for_connection(device.id)
    service = MixerService(state, bus, db, manager)
    await service.start()
    try:
        await until(lambda: service.metering_reason == "unsupported")
        assert state.mixer.get("metering") == {"available": False, "reason": "unsupported"}
        assert state.mixer.get("meters") == {}
    finally:
        await service.stop()
        await manager.stop()
        await bus.stop()


# -- set_tracked follows configuration changes (§7.3) ----------------------------------


async def test_set_tracked_follows_configuration_changes(db: Database, dev_config: Config) -> None:
    async with CqMidiStub() as stub:
        rig, channel = await _built_with_one_channel(db, dev_config, stub, refs=["ip1"])
        try:
            await until(lambda: rig.driver.tracked == frozenset({"ip1"}))

            second = await _channel(db, rig.device_id, name="Wireless 2", refs=["ip2"])
            rig.bus.emit(MixerConfigChanged(reason="channel_created"))

            await until(lambda: rig.driver.tracked == frozenset({"ip1", "ip2"}))
            assert rig.service.is_tracked(second.id)

            current = await mixer_crud.get_channel(db, channel.id)
            assert current is not None
            await mixer_crud.update_channel(db, channel.id, current.updated_at, tracked=False)
            rig.bus.emit(MixerConfigChanged(reason="channel_updated"))
            await until(lambda: rig.driver.tracked == frozenset({"ip2"}))
            assert not rig.service.is_tracked(channel.id)
            # Untracked, not unconfigured (§21.21) — see the dedicated tests below.
            assert rig.service.is_configured(channel.id)
        finally:
            await _teardown(rig)


# -- untracked channels stay configured, displayed and controllable (§21.21) ----------


async def test_untracked_channel_appears_in_state_before_any_write(
    db: Database, dev_config: Config
) -> None:
    async with CqMidiStub() as stub:
        rig, _tracked = await _built_with_one_channel(db, dev_config, stub, refs=["ip1"])
        try:
            untracked = await _channel(db, rig.device_id, name="Aux", refs=["ip2"], tracked=False)
            rig.bus.emit(MixerConfigChanged(reason="channel_created"))

            await until(lambda: rig.state.mixer.get_item("inputs", untracked.id) is not None)
            entry = rig.state.mixer.get_item("inputs", untracked.id)
            assert entry == {"db": None, "muted": False}
            assert rig.service.is_configured(untracked.id)
            assert not rig.service.is_tracked(untracked.id)
        finally:
            await _teardown(rig)


async def test_untracked_channel_write_reaches_the_stub_and_the_state(
    db: Database, dev_config: Config
) -> None:
    async with CqMidiStub() as stub:
        rig, _tracked = await _built_with_one_channel(db, dev_config, stub, refs=["ip1"])
        try:
            untracked = await _channel(db, rig.device_id, name="Aux", refs=["ip2"], tracked=False)
            rig.bus.emit(MixerConfigChanged(reason="channel_created"))
            await until(lambda: rig.state.mixer.get_item("inputs", untracked.id) is not None)

            applied = await rig.service.set_level(untracked.id, -10.0)

            await until(lambda: stub.value(IP2_LEVEL) != 0)
            entry = rig.state.mixer.get_item("inputs", untracked.id)
            assert entry is not None
            assert entry["db"] == pytest.approx(applied, abs=0.01)
            assert "origin" not in entry  # never a MixPad badge (§21.21)
            live = rig.service.live(untracked.id)
            assert live is not None and live.db == pytest.approx(applied, abs=0.01)

            await rig.service.set_mute(untracked.id, True)
            await until(lambda: stub.value(IP2_MUTE) == 1)
        finally:
            await _teardown(rig)


async def test_untracked_channel_ignores_a_mixpad_push(
    db: Database, dev_config: Config
) -> None:
    async with CqMidiStub() as stub:
        rig, _tracked = await _built_with_one_channel(db, dev_config, stub, refs=["ip1"])
        try:
            untracked = await _channel(db, rig.device_id, name="Aux", refs=["ip2"], tracked=False)
            rig.bus.emit(MixerConfigChanged(reason="channel_created"))
            await until(lambda: rig.state.mixer.get_item("inputs", untracked.id) is not None)
            applied = await rig.service.set_level(untracked.id, -10.0)
            await until(lambda: stub.value(IP2_LEVEL) != 0)

            # A change MixPad made on its reference — discarded by the driver
            # (§21.21, cq20b.md's "unconfigured channels are not tracked",
            # applied here to a deliberately untracked one).
            await stub.push(IP2_LEVEL, 10048)  # -5 dB, distinct from applied
            await asyncio.sleep(0.2)

            live = rig.service.live(untracked.id)
            assert live is not None and live.db == pytest.approx(applied, abs=0.01)
            entry = rig.state.mixer.get_item("inputs", untracked.id)
            assert entry is not None and "origin" not in entry
        finally:
            await _teardown(rig)


async def test_untracked_channel_is_not_queried_on_a_resync(
    db: Database, dev_config: Config
) -> None:
    async with CqMidiStub(presets={2: {}}) as stub:
        rig, _tracked = await _built_with_one_channel(db, dev_config, stub, refs=["ip1"])
        try:
            untracked = await _channel(db, rig.device_id, name="Aux", refs=["ip2"], tracked=False)
            rig.bus.emit(MixerConfigChanged(reason="channel_created"))
            await until(lambda: rig.state.mixer.get_item("inputs", untracked.id) is not None)
            await until(lambda: rig.driver.tracked == frozenset({"ip1"}))

            scene = await mixer_crud.create_desk_scene(
                db, device_id=rig.device_id, scene_ref="2", name="Test"
            )
            stub.clear_record()

            await rig.service.recall_desk_scene(scene.id)

            queried = {m.address for m in stub.messages_of("get")}
            assert IP1_LEVEL in queried  # the tracked channel is still synced
            assert IP2_LEVEL not in queried
            assert IP2_MUTE not in queried
        finally:
            await _teardown(rig)


# -- keeping the view in step with the desk (§7.3 *State synchronisation*) -------------


async def test_channels_the_connection_sync_missed_are_read_when_the_service_attaches(
    db: Database, dev_config: Config
) -> None:
    """The driver's connect-time sync runs before the service has told it what
    to track, so it reads Main alone; the service reads the rest on attaching."""
    async with CqMidiStub(state={IP1_LEVEL: 10048, IP1_MUTE: 1}) as stub:
        rig, channel = await _built_with_one_channel(db, dev_config, stub, refs=["ip1"])
        try:
            live = rig.service.live(channel.id)
            assert live is not None
            assert live.db == pytest.approx(-5.0, abs=0.01)
            assert (live.muted, live.origin) == (True, None)
            assert IP1_LEVEL in {m.address for m in stub.messages_of("get")}
            assert stub.messages_of("set") == []
        finally:
            await _teardown(rig)


async def test_a_channel_configured_while_connected_is_read_from_the_desk(
    db: Database, dev_config: Config
) -> None:
    async with CqMidiStub(state={IP2_LEVEL: 10048, IP2_MUTE: 1}) as stub:
        rig, first = await _built_with_one_channel(db, dev_config, stub, refs=["ip1"])
        try:
            # MixPad moved the first channel: badged, and a configuration
            # change elsewhere must not clear that (it is not a desk change).
            await stub.push(IP1_LEVEL, 7936)  # -10 dB
            await until(lambda: _origin(rig, first.id) == "mixpad")
            stub.clear_record()

            second = await _channel(db, rig.device_id, name="Lectern", refs=["ip2"])
            rig.bus.emit(MixerConfigChanged(reason="channel_created"))

            # Level and mute are read by separate queries; wait for both.
            await until(lambda: _muted(rig, second.id) is True)
            live = rig.service.live(second.id)
            assert live is not None
            assert live.db == pytest.approx(-5.0, abs=0.01)
            assert (live.muted, live.origin) == (True, None)
            queried = {m.address for m in stub.messages_of("get")}
            assert {IP2_LEVEL, IP2_MUTE} <= queried
            assert IP1_LEVEL not in queried  # only the new channel is read
            assert stub.messages_of("set") == []
            assert _origin(rig, first.id) == "mixpad"
        finally:
            await _teardown(rig)


async def test_a_mixer_announced_by_configuration_is_known_before_it_ever_connects(
    db: Database, dev_config: Config
) -> None:
    """``POST /devices`` announces a new mixer with ``MixerConfigChanged``; a
    desk that has not answered is the configured mixer, shown disconnected."""
    bus = EventBus()
    await bus.start()
    state = StateStore(dev_config, bus)
    manager = DeviceManager(
        db, state, bus, dev_config, connect_timeout=2.0, probe_timeout=1.0, stop_timeout=2.0
    )
    await manager.start()
    service = MixerService(state, bus, db, manager)
    await service.start()
    try:
        assert service.device_id is None
        async with CqMidiStub() as stub:
            closed = stub.port  # nothing listens here once the stub has stopped
        device = await devices_crud.create(
            db,
            category="mixer",
            driver_key="cq20b",
            name="Mixer",
            config={
                "transport": {"type": "tcp", "host": "127.0.0.1", "port": closed},
                "driver": {"metering": False},
            },
        )
        await manager.reload(device.id)
        main = await _channel(db, device.id, kind="main", name="Main LR", refs=["main"])
        bus.emit(MixerConfigChanged(reason="main_channel_created"))

        await until(lambda: service.device_id == device.id)
        assert service.connected is False
        assert service.is_configured(main.id)
    finally:
        await service.stop()
        await manager.stop()
        await bus.stop()


# -- helpers ---------------------------------------------------------------------------


async def _built_with_one_channel(
    db: Database, dev_config: Config, stub: CqMidiStub, *, refs: list[str]
) -> tuple[Rig, mixer_crud.MixerChannel]:
    device = await devices_crud.create(
        db,
        category="mixer",
        driver_key="cq20b",
        name="Mixer",
        config={
            "transport": {"type": "tcp", "host": "127.0.0.1", "port": stub.port},
            "driver": {"metering": False},
        },
    )
    channel = await _channel(db, device.id, name="Wireless 1", refs=refs)
    bus = EventBus()
    await bus.start()
    state = StateStore(dev_config, bus)
    manager = DeviceManager(
        db, state, bus, dev_config, connect_timeout=2.0, probe_timeout=1.0, stop_timeout=2.0
    )
    await manager.start()
    await manager.wait_for_connection(device.id)
    driver = manager.running_driver(device.id)
    assert isinstance(driver, CQ20BDriver)
    service = MixerService(state, bus, db, manager)
    await service.start()
    return Rig(bus, state, manager, service, device.id, driver), channel
