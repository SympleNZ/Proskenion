"""``mixer_recall``, ``mixer_fader`` and ``mixer_mute`` (§8.12, §7.3, §13.5) —
through the real :class:`~proskenion.core.mixer.service.MixerService` and
:class:`~proskenion.core.drivers.cq20b.CQ20BDriver`, against
:mod:`tests.stubs.cq_midi_stub`, over the real
:class:`~proskenion.core.devices.DeviceManager` — the same stack
``tests/unit/core/test_mixer.py`` and ``tests/unit/scene/test_av_handlers.py``
sit on top of. The stub mixer driver stands in for the unsupported-recall
case, exactly as it does in ``proskenion/scene/domains.py``'s own docstring.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from proskenion.core.bus import EventBus
from proskenion.core.devices import DeviceManager
from proskenion.core.dmx.fade import SceneRun
from proskenion.core.drivers.stub_mixer import StubMixerDriver
from proskenion.core.events import MixerConfigChanged
from proskenion.core.mixer.service import MixerService
from proskenion.core.state import StateStore
from proskenion.core.transport.loopback import LoopbackTransport
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import mixer as mixer_crud
from proskenion.db.crud.scenes import Scene, SceneAction
from proskenion.scene.domains import ActionContext
from proskenion.scene.mixer_handlers import MixerFaderHandler, MixerMuteHandler, MixerRecallHandler
from tests.stubs.cq_midi_stub import CqMidiStub
from tests.stubs.echo_driver import RecordingSink
from tests.unit.scene.conftest import dev_config, mixer_capabilities

IP1_LEVEL, IP1_MUTE = (0x40, 0x00), (0x00, 0x00)
IP2_LEVEL, IP2_MUTE = (0x40, 0x01), (0x00, 0x01)


# `asyncio_mode = "auto"` (pyproject.toml) collects every `async def test_...`
# without a marker; no module-level `pytestmark` here because one plain,
# synchronous test lives alongside them (a marker on a sync function warns).


async def until(condition: Callable[[], bool], limit: float = 5.0) -> None:
    """Mirrors ``tests/unit/core/test_mixer.py``'s own helper of the same name."""
    async with asyncio.timeout(limit):
        while not condition():  # noqa: ASYNC110 - condition spans the stub, driver and service
            await asyncio.sleep(0.01)


# -- shared test helpers, matching test_av_handlers.py's own ------------------------

_ACTION_DEFAULTS: dict[str, Any] = {
    "id": 1,
    "scene_id": 1,
    "sort_order": 0,
    "delay_ms": 0,
    "knx_address_id": None,
    "knx_value": None,
    "knx_source": "literal",
    "knx_scale": None,
    "dmx_snapshot": None,
    "dmx_fade_ms": None,
    "mixer_scene_id": None,
    "mixer_channel_id": None,
    "mixer_db": None,
    "mixer_muted": None,
    "projector_power": None,
    "projector_input": None,
    "hdmi_destination": None,
    "hdmi_input_id": None,
    "device_id": None,
    "created_at": "",
    "updated_at": "",
}

_SCENE = Scene(
    id=1,
    name="Test",
    description=None,
    enabled=True,
    icon=None,
    priority="normal",
    protected=False,
    visible_operator=True,
    sort_order=0,
    created_at="",
    updated_at="",
)


def make_action(domain: str, **overrides: Any) -> SceneAction:
    return SceneAction(**{**_ACTION_DEFAULTS, "domain": domain, **overrides})


def ctx(*, discarded: bool = False) -> ActionContext:
    reason = "discarded — critical scene took over" if discarded else None
    return ActionContext(
        run=SceneRun(scene_id=1),
        scene=_SCENE,
        triggered_by="api:admin",
        discard_reason=lambda: reason,
    )


# -- the mixer rig: the real DeviceManager and MixerService, over TCP ----------------


@dataclass
class MixerRig:
    devices: DeviceManager
    service: MixerService
    device_id: int


@asynccontextmanager
async def mixer_rig(
    db: Database, state: StateStore, bus: EventBus, stub: CqMidiStub
) -> AsyncIterator[MixerRig]:
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
    manager = DeviceManager(
        db, state, bus, dev_config(), connect_timeout=2.0, probe_timeout=1.0, stop_timeout=2.0
    )
    await manager.start()
    await manager.wait_for_connection(device.id)
    service = MixerService(state, bus, db, manager)
    await service.start()
    try:
        yield MixerRig(manager, service, device.id)
    finally:
        await service.stop()
        await manager.stop()


async def _create_channel(
    db: Database, device_id: int, *, name: str, refs: list[str]
) -> mixer_crud.MixerChannel:
    channel = await mixer_crud.create_channel(db, device_id=device_id, name=name)
    await mixer_crud.set_channel_refs(db, channel.id, refs)
    return channel


async def _channel(
    db: Database, rig: MixerRig, bus: EventBus, *, name: str, refs: list[str]
) -> mixer_crud.MixerChannel:
    """A channel created straight through the CRUD layer, the way other
    mixer-channel tests do, with a running service already attached — so it has to pick
    the new channel up through ``MixerConfigChanged`` exactly as it would
    from the admin API's own ``_emit_config_changed``
    (``proskenion/api/mixer.py``), rather than already knowing about it
    from its own start-up load."""
    channel = await _create_channel(db, rig.device_id, name=name, refs=refs)
    bus.emit(MixerConfigChanged(reason="test"))
    await until(lambda: rig.service.is_configured(channel.id))
    return channel


# -- mixer_fader ----------------------------------------------------------------------


async def test_fader_ok_moves_every_reference_of_a_ganged_channel_in_one_call(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    async with CqMidiStub() as stub:
        async with mixer_rig(db, state, bus, stub) as rig:
            channel = await _channel(db, rig, bus, name="Wireless pair", refs=["ip1", "ip2"])

            calls: list[tuple[int, float | None]] = []
            original = rig.service.set_level

            async def counted(channel_id: int, level: float | None, **kwargs: Any) -> float | None:
                calls.append((channel_id, level))
                return await original(channel_id, level, **kwargs)

            rig.service.set_level = counted  # type: ignore[method-assign]

            handler = MixerFaderHandler(rig.service)
            outcome = await handler.execute(
                make_action("mixer_fader", mixer_channel_id=channel.id, mixer_db=-10.0), ctx()
            )

            assert outcome.result == "confirmed"
            assert len(calls) == 1  # one intent, one service call — never one per reference (§5.5)
            await until(lambda: stub.value(IP1_LEVEL) != 0)
            await until(lambda: stub.value(IP2_LEVEL) != 0)


async def test_fader_off_is_null_db(db: Database, state: StateStore, bus: EventBus) -> None:
    async with CqMidiStub() as stub:
        async with mixer_rig(db, state, bus, stub) as rig:
            channel = await _channel(db, rig, bus, name="Wireless 1", refs=["ip1"])
            handler = MixerFaderHandler(rig.service)
            outcome = await handler.execute(
                make_action("mixer_fader", mixer_channel_id=channel.id, mixer_db=None), ctx()
            )
            assert outcome.result == "confirmed"
            assert outcome.detail["db"] is None


async def test_fader_unknown_channel_fails(db: Database, state: StateStore, bus: EventBus) -> None:
    async with CqMidiStub() as stub:
        async with mixer_rig(db, state, bus, stub) as rig:
            handler = MixerFaderHandler(rig.service)
            outcome = await handler.execute(
                make_action("mixer_fader", mixer_channel_id=999999, mixer_db=-6.0), ctx()
            )
            assert outcome.result == "failed"
            assert outcome.reason == "no such mixer channel"


async def test_fader_mixer_offline_fails(db: Database, state: StateStore, bus: EventBus) -> None:
    # A device row that points nowhere real: never connects, so
    # `running_driver` stays `None` throughout (mirrors
    # tests/unit/core/test_projector.py's own "unreachable" rig).
    device = await devices_crud.create(
        db,
        category="mixer",
        driver_key="cq20b",
        name="Mixer",
        config={
            "transport": {"type": "tcp", "host": "127.0.0.1", "port": 1},
            "driver": {"metering": False},
        },
    )
    channel = await _create_channel(db, device.id, name="Wireless 1", refs=["ip1"])
    manager = DeviceManager(
        db, state, bus, dev_config(), connect_timeout=0.2, probe_timeout=0.2, stop_timeout=1.0
    )
    await manager.start()
    service = MixerService(state, bus, db, manager)
    await service.start()
    try:
        handler = MixerFaderHandler(service)
        outcome = await handler.execute(
            make_action("mixer_fader", mixer_channel_id=channel.id, mixer_db=-6.0), ctx()
        )
        assert outcome.result == "failed"
        assert outcome.reason == "the mixer is not available"
    finally:
        await service.stop()
        await manager.stop()


def test_fader_is_never_gated() -> None:
    handler = MixerFaderHandler(None)
    assert handler.unsupported(make_action("mixer_fader"), mixer_capabilities()) is None


async def test_fader_context_discarded_is_respected_before_the_service_call(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    async with CqMidiStub() as stub:
        async with mixer_rig(db, state, bus, stub) as rig:
            channel = await _channel(db, rig, bus, name="Wireless 1", refs=["ip1"])
            before = stub.value(IP1_LEVEL)
            handler = MixerFaderHandler(rig.service)
            outcome = await handler.execute(
                make_action("mixer_fader", mixer_channel_id=channel.id, mixer_db=-6.0),
                ctx(discarded=True),
            )
            assert outcome.result == "skipped"
            assert stub.value(IP1_LEVEL) == before


# -- mixer_mute — absolute, never a toggle (§7.3, cq20b.md §2) -----------------------


async def test_mute_ok_and_never_a_toggle(db: Database, state: StateStore, bus: EventBus) -> None:
    async with CqMidiStub() as stub:
        async with mixer_rig(db, state, bus, stub) as rig:
            channel = await _channel(db, rig, bus, name="Wireless 1", refs=["ip1"])
            handler = MixerMuteHandler(rig.service)

            first = await handler.execute(
                make_action("mixer_mute", mixer_channel_id=channel.id, mixer_muted=True), ctx()
            )
            assert first.result == "confirmed"
            await until(lambda: stub.value(IP1_MUTE) == 1)

            # Sent again — an absolute mute is idempotent; a toggle would
            # have unmuted it.
            second = await handler.execute(
                make_action("mixer_mute", mixer_channel_id=channel.id, mixer_muted=True), ctx()
            )
            assert second.result == "confirmed"
            await until(
                lambda: len([m for m in stub.messages_of("set") if m.address == IP1_MUTE]) == 2
            )
            assert stub.value(IP1_MUTE) == 1
            assert stub.mute_toggles == []  # VF=00 increment/decrement (cq20b.md §2) — never sent
            set_messages = [m for m in stub.messages_of("set") if m.address == IP1_MUTE]
            assert [m.value for m in set_messages] == [1, 1]


async def test_mute_unknown_channel_fails(db: Database, state: StateStore, bus: EventBus) -> None:
    async with CqMidiStub() as stub:
        async with mixer_rig(db, state, bus, stub) as rig:
            handler = MixerMuteHandler(rig.service)
            outcome = await handler.execute(
                make_action("mixer_mute", mixer_channel_id=999999, mixer_muted=True), ctx()
            )
            assert outcome.result == "failed"
            assert outcome.reason == "no such mixer channel"


async def test_mute_mixer_offline_fails(db: Database, state: StateStore, bus: EventBus) -> None:
    device = await devices_crud.create(
        db,
        category="mixer",
        driver_key="cq20b",
        name="Mixer",
        config={
            "transport": {"type": "tcp", "host": "127.0.0.1", "port": 1},
            "driver": {"metering": False},
        },
    )
    channel = await _create_channel(db, device.id, name="Wireless 1", refs=["ip1"])
    manager = DeviceManager(
        db, state, bus, dev_config(), connect_timeout=0.2, probe_timeout=0.2, stop_timeout=1.0
    )
    await manager.start()
    service = MixerService(state, bus, db, manager)
    await service.start()
    try:
        handler = MixerMuteHandler(service)
        outcome = await handler.execute(
            make_action("mixer_mute", mixer_channel_id=channel.id, mixer_muted=True), ctx()
        )
        assert outcome.result == "failed"
        assert outcome.reason == "the mixer is not available"
    finally:
        await service.stop()
        await manager.stop()


def test_mute_is_gated_on_the_driver_declaring_mute_support() -> None:
    handler = MixerMuteHandler(None)
    action = make_action("mixer_mute")
    assert handler.unsupported(action, mixer_capabilities(supports_mute=False)) is not None
    assert handler.unsupported(action, mixer_capabilities(supports_mute=True)) is None


async def test_mute_context_discarded_is_respected_before_the_service_call(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    async with CqMidiStub() as stub:
        async with mixer_rig(db, state, bus, stub) as rig:
            channel = await _channel(db, rig, bus, name="Wireless 1", refs=["ip1"])
            handler = MixerMuteHandler(rig.service)
            outcome = await handler.execute(
                make_action("mixer_mute", mixer_channel_id=channel.id, mixer_muted=True),
                ctx(discarded=True),
            )
            assert outcome.result == "skipped"
            assert stub.value(IP1_MUTE) == 0


# -- mixer_recall — a desk scene, or the Venue Default (§13.5) -----------------------


async def test_recall_ok(db: Database, state: StateStore, bus: EventBus) -> None:
    async with CqMidiStub(recall_delay=0.02) as stub:
        async with mixer_rig(db, state, bus, stub) as rig:
            scene = await mixer_crud.create_desk_scene(
                db, device_id=rig.device_id, scene_ref="2", name="Lecture Baseline"
            )
            handler = MixerRecallHandler(rig.service, db)
            outcome = await handler.execute(
                make_action("mixer_recall", mixer_scene_id=scene.id), ctx()
            )
            assert outcome.result == "confirmed"
            assert outcome.detail == {"scene_id": scene.id, "name": "Lecture Baseline"}


async def test_recall_null_scene_id_recalls_the_venue_default(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    async with CqMidiStub(recall_delay=0.02) as stub:
        async with mixer_rig(db, state, bus, stub) as rig:
            await mixer_crud.create_desk_scene(
                db, device_id=rig.device_id, scene_ref="1", name="Not the default"
            )
            venue_default = await mixer_crud.create_desk_scene(
                db,
                device_id=rig.device_id,
                scene_ref="2",
                name="Venue Default",
                is_venue_default=True,
            )
            handler = MixerRecallHandler(rig.service, db)
            outcome = await handler.execute(make_action("mixer_recall", mixer_scene_id=None), ctx())
            assert outcome.result == "confirmed"
            assert outcome.detail["scene_id"] == venue_default.id
            assert outcome.detail["default"] is True


async def test_recall_null_scene_id_with_no_venue_default_fails(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    async with CqMidiStub() as stub:
        async with mixer_rig(db, state, bus, stub) as rig:
            handler = MixerRecallHandler(rig.service, db)
            outcome = await handler.execute(make_action("mixer_recall", mixer_scene_id=None), ctx())
            assert outcome.result == "failed"
            assert outcome.reason == "no Venue Default desk scene is designated"


async def test_recall_unknown_scene_fails(db: Database, state: StateStore, bus: EventBus) -> None:
    async with CqMidiStub() as stub:
        async with mixer_rig(db, state, bus, stub) as rig:
            handler = MixerRecallHandler(rig.service, db)
            outcome = await handler.execute(
                make_action("mixer_recall", mixer_scene_id=999999), ctx()
            )
            assert outcome.result == "failed"
            assert outcome.reason == "no such desk scene"


async def test_recall_mixer_offline_fails(db: Database, state: StateStore, bus: EventBus) -> None:
    device = await devices_crud.create(
        db,
        category="mixer",
        driver_key="cq20b",
        name="Mixer",
        config={
            "transport": {"type": "tcp", "host": "127.0.0.1", "port": 1},
            "driver": {"metering": False},
        },
    )
    scene = await mixer_crud.create_desk_scene(
        db, device_id=device.id, scene_ref="1", name="Baseline"
    )
    manager = DeviceManager(
        db, state, bus, dev_config(), connect_timeout=0.2, probe_timeout=0.2, stop_timeout=1.0
    )
    await manager.start()
    service = MixerService(state, bus, db, manager)
    await service.start()
    try:
        handler = MixerRecallHandler(service, db)
        outcome = await handler.execute(
            make_action("mixer_recall", mixer_scene_id=scene.id), ctx()
        )
        assert outcome.result == "failed"
        assert outcome.reason == "the mixer is not available"
    finally:
        await service.stop()
        await manager.stop()


def test_recall_is_gated_on_the_stub_driver_declaring_no_scene_recall(db: Database) -> None:
    """The stub mixer driver has no scene recall — the same driver §18's
    stub-driver milestone uses to prove the interface degrades honestly
    (proskenion/scene/domains.py's own module docstring)."""
    transport = LoopbackTransport()
    driver = StubMixerDriver(1, transport, {}, RecordingSink())
    handler = MixerRecallHandler(None, db)
    action = make_action("mixer_recall", mixer_scene_id=None)
    assert handler.unsupported(action, driver.capabilities()) is not None
    assert handler.unsupported(action, mixer_capabilities(scene_recall=True)) is None


async def test_recall_context_discarded_is_respected_before_the_service_call(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    async with CqMidiStub() as stub:
        async with mixer_rig(db, state, bus, stub) as rig:
            scene = await mixer_crud.create_desk_scene(
                db, device_id=rig.device_id, scene_ref="1", name="Baseline"
            )
            handler = MixerRecallHandler(rig.service, db)
            outcome = await handler.execute(
                make_action("mixer_recall", mixer_scene_id=scene.id), ctx(discarded=True)
            )
            assert outcome.result == "skipped"
