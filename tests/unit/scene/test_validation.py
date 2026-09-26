"""Scene actions validated on save: domains, fields, snapshot channels, capability (§8.12, §5.5)."""

from __future__ import annotations

from typing import Any

import pytest

from proskenion.core.drivers.capabilities import ProjectorCapabilities
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import mixer as mixer_crud
from proskenion.db.crud import video as video_crud
from proskenion.scene.av_handlers import ProjectorInputHandler
from proskenion.scene.domains import DomainHandlers
from proskenion.scene.mixer_handlers import MixerMuteHandler, MixerRecallHandler
from proskenion.scene.validation import ActionValidationError, validate_action
from tests.unit.scene.conftest import FakeCapabilities, RecordingHandler, Rig, mixer_capabilities


def action(**values: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"sort_order": 0, "delay_ms": 0, "knx_source": "literal"}
    return {**base, **values}


async def problems(
    db: Database,
    values: dict[str, Any],
    *,
    handlers: DomainHandlers | None = None,
    devices: FakeCapabilities | None = None,
) -> dict[str, list[str]]:
    try:
        await validate_action(db, 1, values, handlers=handlers or DomainHandlers(), devices=devices)
    except ActionValidationError as exc:
        return exc.fields
    return {}


async def test_the_eight_domains_and_nothing_else(db: Database, rig: Rig) -> None:
    assert "domain" in await problems(db, action(domain="pjlink_power"))
    assert "domain" in await problems(db, action(domain="dmx_fade"))  # collapsed (B8)
    ok = action(domain="projector_power", projector_power="on")
    assert await problems(db, ok) == {}


async def test_each_domain_needs_its_fields(db: Database, rig: Rig) -> None:
    mixer = await devices_crud.create(
        db, category="mixer", driver_key="cq20b", name="CQ-20B", config={}
    )
    channel = await mixer_crud.create_channel(db, device_id=mixer.id, name="Monitors")

    assert "knx_address_id" in await problems(db, action(domain="knx"))
    assert "dmx_snapshot" in await problems(db, action(domain="dmx"))
    # mixer_scene_id is never required: null recalls the Venue Default (§13.5).
    assert await problems(db, action(domain="mixer_recall")) == {}
    assert "mixer_channel_id" in await problems(db, action(domain="mixer_mute", mixer_muted=False))
    assert "mixer_muted" in await problems(
        db, action(domain="mixer_mute", mixer_channel_id=channel.id)
    )
    assert "projector_power" in await problems(
        db, action(domain="projector_power", projector_power="toggle")
    )
    assert "projector_input" in await problems(db, action(domain="projector_input"))
    # hdmi_input_id is never required: null is "Restore Venue Default" (§13.5).
    assert set(await problems(db, action(domain="hdmi_source"))) == {"hdmi_destination"}
    assert "mixer_channel_id" in await problems(db, action(domain="mixer_fader"))
    # mixer_db null is "off", not missing (§5.5).
    assert await problems(db, action(domain="mixer_fader", mixer_channel_id=channel.id)) == {}


async def test_mixer_recall_scene_id_must_exist_when_given(db: Database, rig: Rig) -> None:
    mixer = await devices_crud.create(
        db, category="mixer", driver_key="cq20b", name="CQ-20B", config={}
    )
    scene = await mixer_crud.create_desk_scene(
        db, device_id=mixer.id, scene_ref="1", name="Baseline"
    )
    # null recalls the Venue Default (§13.5) — never a validation error.
    assert await problems(db, action(domain="mixer_recall")) == {}
    assert await problems(db, action(domain="mixer_recall", mixer_scene_id=scene.id)) == {}
    assert "mixer_scene_id" in await problems(
        db, action(domain="mixer_recall", mixer_scene_id=999999)
    )


async def test_mixer_fader_channel_must_exist_and_db_within_the_fader_law(
    db: Database, rig: Rig
) -> None:
    mixer = await devices_crud.create(
        db, category="mixer", driver_key="cq20b", name="CQ-20B", config={}
    )
    channel = await mixer_crud.create_channel(db, device_id=mixer.id, name="Monitors")
    assert "mixer_channel_id" in await problems(
        db, action(domain="mixer_fader", mixer_channel_id=999999)
    )

    devices = FakeCapabilities({mixer.id: mixer_capabilities()})  # min_db=-90.0, max_db=10.0
    assert (
        await problems(
            db,
            action(domain="mixer_fader", mixer_channel_id=channel.id, mixer_db=-6.0),
            devices=devices,
        )
        == {}
    )
    # off is never out of range (§5.5).
    assert (
        await problems(
            db,
            action(domain="mixer_fader", mixer_channel_id=channel.id, mixer_db=None),
            devices=devices,
        )
        == {}
    )
    assert "mixer_db" in await problems(
        db,
        action(domain="mixer_fader", mixer_channel_id=channel.id, mixer_db=99.0),
        devices=devices,
    )


async def test_mixer_mute_channel_must_exist_and_muted_is_never_a_toggle(
    db: Database, rig: Rig
) -> None:
    mixer = await devices_crud.create(
        db, category="mixer", driver_key="cq20b", name="CQ-20B", config={}
    )
    channel = await mixer_crud.create_channel(db, device_id=mixer.id, name="Monitors")
    assert "mixer_channel_id" in await problems(
        db, action(domain="mixer_mute", mixer_channel_id=999999, mixer_muted=True)
    )
    assert (
        await problems(
            db, action(domain="mixer_mute", mixer_channel_id=channel.id, mixer_muted=True)
        )
        == {}
    )
    assert "mixer_muted" in await problems(
        db, action(domain="mixer_mute", mixer_channel_id=channel.id, mixer_muted="toggle")
    )


async def test_fields_of_another_domain_are_refused(db: Database, rig: Rig) -> None:
    found = await problems(db, action(domain="projector_power", projector_power="on", mixer_db=0.0))
    assert found == {"mixer_db": ["is not used by projector_power actions"]}


async def test_snapshot_channels_must_exist_and_entries_be_well_formed(
    db: Database, rig: Rig
) -> None:
    good = action(domain="dmx", dmx_snapshot={str(rig.front): {"level": 50.0}}, dmx_fade_ms=0)
    assert await problems(db, good) == {}
    found = await problems(
        db,
        action(
            domain="dmx",
            dmx_snapshot={
                "999": {"level": 10.0},
                str(rig.mid): {"level": 120.0},
                str(rig.back): {"r": 10, "g": 20},
                "cyc": {"level": 1.0},
            },
        ),
    )
    assert found["dmx_snapshot.999"] == ["no such lighting channel"]
    assert "level must be a number from 0 to 100 (§9.2)" in found[f"dmx_snapshot.{rig.mid}"]
    assert "a colour needs r, g and b together" in found[f"dmx_snapshot.{rig.back}"]
    assert found["dmx_snapshot.cyc"] == ["is not a lighting channel id"]


async def test_knx_values_are_checked_against_the_address(db: Database, rig: Rig) -> None:
    assert await problems(db, action(domain="knx", knx_address_id=rig.lamp, knx_value="on")) == {}
    assert "knx_value" in await problems(
        db, action(domain="knx", knx_address_id=rig.level, knx_value="150")
    )
    assert "knx_address_id" in await problems(
        db,
        action(domain="knx", knx_address_id=rig.armed, knx_value="1"),  # incoming-only
    )
    assert "knx_scale" in await problems(
        db,
        action(domain="knx", knx_address_id=rig.lamp, knx_source="trigger_value", knx_scale="2"),
    )
    passthrough = action(
        domain="knx", knx_address_id=rig.byte, knx_source="trigger_value", knx_scale="2.55"
    )
    assert await problems(db, passthrough) == {}
    assert "device_id" in await problems(
        db,
        action(domain="knx", knx_address_id=rig.lamp, knx_value="1", device_id=rig.output),
    )


async def test_an_action_the_driver_does_not_support_is_refused(db: Database, rig: Rig) -> None:
    mixer = await devices_crud.create(
        db, category="mixer", driver_key="cq20b", name="CQ-20B", config={}
    )
    scene = await mixer_crud.create_desk_scene(
        db, device_id=mixer.id, scene_ref="1", name="Baseline"
    )
    handlers = DomainHandlers()
    handlers.register(
        "mixer_recall",
        RecordingHandler(
            gate=lambda a, caps: (
                None if getattr(caps, "supports_scene_recall", False) else "no scene recall"
            )
        ),
    )
    devices = FakeCapabilities({mixer.id: mixer_capabilities(scene_recall=False)})
    with pytest.raises(ActionValidationError) as caught:
        await validate_action(
            db,
            1,
            action(domain="mixer_recall", mixer_scene_id=scene.id),
            handlers=handlers,
            devices=devices,
        )
    assert caught.value.unsupported
    assert caught.value.fields == {"domain": ["CQ-20B does not support this: no scene recall"]}

    devices.reports[mixer.id] = mixer_capabilities(scene_recall=True)
    await validate_action(
        db,
        1,
        action(domain="mixer_recall", mixer_scene_id=scene.id),
        handlers=handlers,
        devices=devices,
    )


async def test_mixer_recall_unsupported_via_the_real_handler_is_refused_on_save(
    db: Database, rig: Rig
) -> None:
    """The same gate, through :class:`MixerRecallHandler` itself rather than
    a :class:`RecordingHandler` stand-in — the capability check the module
    docstring of :mod:`proskenion.scene.domains` names directly."""
    mixer = await devices_crud.create(
        db, category="mixer", driver_key="cq20b", name="CQ-20B", config={}
    )
    scene = await mixer_crud.create_desk_scene(
        db, device_id=mixer.id, scene_ref="1", name="Baseline"
    )
    handlers = DomainHandlers()
    handlers.register("mixer_recall", MixerRecallHandler(None, db))
    devices = FakeCapabilities({mixer.id: mixer_capabilities(scene_recall=False)})
    with pytest.raises(ActionValidationError) as caught:
        await validate_action(
            db,
            1,
            action(domain="mixer_recall", mixer_scene_id=scene.id),
            handlers=handlers,
            devices=devices,
        )
    assert caught.value.unsupported

    devices.reports[mixer.id] = mixer_capabilities(scene_recall=True)
    await validate_action(
        db,
        1,
        action(domain="mixer_recall", mixer_scene_id=scene.id),
        handlers=handlers,
        devices=devices,
    )


async def test_mixer_mute_unsupported_via_the_real_handler_is_refused_on_save(
    db: Database, rig: Rig
) -> None:
    mixer = await devices_crud.create(
        db, category="mixer", driver_key="cq20b", name="CQ-20B", config={}
    )
    channel = await mixer_crud.create_channel(db, device_id=mixer.id, name="Monitors")
    handlers = DomainHandlers()
    handlers.register("mixer_mute", MixerMuteHandler(None))
    devices = FakeCapabilities({mixer.id: mixer_capabilities(supports_mute=False)})
    with pytest.raises(ActionValidationError) as caught:
        await validate_action(
            db,
            1,
            action(domain="mixer_mute", mixer_channel_id=channel.id, mixer_muted=True),
            handlers=handlers,
            devices=devices,
        )
    assert caught.value.unsupported

    devices.reports[mixer.id] = mixer_capabilities(supports_mute=True)
    await validate_action(
        db,
        1,
        action(domain="mixer_mute", mixer_channel_id=channel.id, mixer_muted=True),
        handlers=handlers,
        devices=devices,
    )


async def test_projector_input_unsupported_is_refused_on_save(db: Database, rig: Rig) -> None:
    """§5.5's third place: the driver's own, connected input list gates a save
    exactly as it gates a run (§8.12)."""
    projector = await devices_crud.create(
        db, category="projector", driver_key="pjlink", name="Projector", config={}
    )
    handlers = DomainHandlers()
    handlers.register("projector_input", ProjectorInputHandler(None))
    devices = FakeCapabilities(
        {projector.id: ProjectorCapabilities(inputs=("31",), supports_authentication=False)}
    )
    with pytest.raises(ActionValidationError) as caught:
        await validate_action(
            db,
            1,
            action(domain="projector_input", projector_input="99"),
            handlers=handlers,
            devices=devices,
        )
    assert caught.value.unsupported

    await validate_action(
        db,
        1,
        action(domain="projector_input", projector_input="31"),
        handlers=handlers,
        devices=devices,
    )


async def test_hdmi_source_destination_and_input_must_exist_and_match(
    db: Database, rig: Rig
) -> None:
    """``hdmi_destination`` must exist; ``hdmi_input_id``, when given, must be
    an input of that destination's own matrix (§7.5, §13.5)."""
    device = await devices_crud.create(
        db, category="video_matrix", driver_key="lkv422", name="Matrix", config={}
    )
    other_device = await devices_crud.create(
        db, category="video_matrix", driver_key="lkv422", name="Other matrix", config={}
    )
    good_input = await video_crud.create_input(db, device_id=device.id, driver_ref="1", name="In 1")
    foreign_input = await video_crud.create_input(
        db, device_id=other_device.id, driver_ref="1", name="Foreign"
    )
    destination = await video_crud.create_destination(db, device_id=device.id, name="The room")

    assert "hdmi_destination" in await problems(
        db, action(domain="hdmi_source", hdmi_destination=99999)
    )
    # No input at all: "Restore Venue Default" (§13.5) — not a validation error.
    assert (
        await problems(db, action(domain="hdmi_source", hdmi_destination=destination.id)) == {}
    )
    assert (
        await problems(
            db,
            action(
                domain="hdmi_source",
                hdmi_destination=destination.id,
                hdmi_input_id=good_input.id,
            ),
        )
        == {}
    )
    assert "hdmi_input_id" in await problems(
        db,
        action(
            domain="hdmi_source",
            hdmi_destination=destination.id,
            hdmi_input_id=foreign_input.id,
        ),
    )
