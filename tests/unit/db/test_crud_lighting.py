"""Fixture profiles, bars, channels, groups and colour presets (§9.1, §9.7, §15.9, §22.2)."""

import pytest

from proskenion.db.connection import Database
from proskenion.db.crud import devices, knx, lighting, rules, scenes
from proskenion.db.crud.base import ConflictError, NotFoundError
from proskenion.db.crud.base import InUseError as BaseInUseError
from proskenion.db.crud.refs import ConstraintError, InUseError

TWELVE_CHANNEL_PROFILE = [
    {"offset": 0, "role": "dimmer", "default": 0},
    {"offset": 1, "role": "red", "default": 0},
    {"offset": 2, "role": "green", "default": 0},
    {"offset": 3, "role": "blue", "default": 0},
    {"offset": 4, "role": "white", "default": 0},
    {"offset": 5, "role": "amber", "default": 0},
    {"offset": 6, "role": "uv", "default": 0},
    {"offset": 7, "role": "pan", "default": 0},
    {"offset": 8, "role": "tilt", "default": 0},
    {"offset": 9, "role": "strobe", "default": 0},
    {"offset": 10, "role": "macro", "default": 0},
    {"offset": 11, "role": "unused", "default": 0},
]


# -- fixture profiles ------------------------------------------------------------


async def test_twelve_channel_profile_occupies_twelve_addresses(db: Database) -> None:
    profile = await lighting.create_fixture_profile(
        db, name="Moving head", channel_count=12, channels=TWELVE_CHANNEL_PROFILE
    )
    assert profile.channel_count == 12
    assert len(profile.channels) == 12
    assert [c.offset for c in profile.channels] == list(range(12))
    assert {c.role for c in profile.channels} == set(lighting.ROLES)


async def test_unknown_role_is_refused_at_save(db: Database) -> None:
    bad = [{"offset": 0, "role": "gobo", "default": 0}]
    with pytest.raises(ValueError):
        await lighting.create_fixture_profile(db, name="Bad", channel_count=1, channels=bad)


async def test_profile_offsets_must_be_unique_and_start_at_zero(db: Database) -> None:
    with pytest.raises(ValueError):
        await lighting.create_fixture_profile(
            db,
            name="Dup offsets",
            channel_count=2,
            channels=[{"offset": 0, "role": "red"}, {"offset": 0, "role": "green"}],
        )
    with pytest.raises(ValueError):
        await lighting.create_fixture_profile(
            db,
            name="Starts at 1",
            channel_count=2,
            channels=[{"offset": 1, "role": "red"}, {"offset": 2, "role": "green"}],
        )
    with pytest.raises(ValueError):
        await lighting.create_fixture_profile(
            db, name="Count mismatch", channel_count=3, channels=[{"offset": 0, "role": "red"}]
        )


async def test_fixture_profile_update_and_optimistic_concurrency(db: Database) -> None:
    profile = await lighting.create_fixture_profile(
        db, name="RGB", channel_count=3, channels=TWELVE_CHANNEL_PROFILE[:3]
    )
    updated = await lighting.update_fixture_profile(
        db, profile.id, profile.updated_at, name="RGBv2"
    )
    assert updated.name == "RGBv2"
    with pytest.raises(ConflictError):
        await lighting.update_fixture_profile(db, profile.id, profile.updated_at, name="Stale")
    with pytest.raises(ValueError):
        await lighting.update_fixture_profile(
            db, profile.id, updated.updated_at, channels=[{"offset": 0, "role": "gobo"}]
        )


async def test_fixture_profile_delete_blocked_while_a_channel_uses_it(db: Database) -> None:
    profile = await lighting.create_fixture_profile(
        db, name="Dimmer", channel_count=1, channels=[{"offset": 0, "role": "dimmer"}]
    )
    output = await devices.create(
        db, category="lighting_output", driver_key="artnet", name="Rack 1", config={}
    )
    channel = await lighting.create_channel(
        db, name="Ch 1", type="dmx", profile_id=profile.id, device_id=output.id, address=1
    )
    with pytest.raises(InUseError) as excinfo:
        await lighting.delete_fixture_profile(db, profile.id)
    assert any(
        r.entity == "lighting_channels" and r.id == channel.id for r in excinfo.value.references
    )
    assert await lighting.get_fixture_profile(db, profile.id) is not None


# -- schema shape constraints (§22.2) ---------------------------------------------


async def test_dmx_channel_with_null_profile_is_refused(db: Database) -> None:
    output = await devices.create(
        db, category="lighting_output", driver_key="artnet", name="Rack 1", config={}
    )
    with pytest.raises(ConstraintError) as excinfo:
        await lighting.create_channel(
            db, name="Bad dmx", type="dmx", profile_id=None, device_id=output.id, address=1
        )
    assert excinfo.value.constraint == "lighting_channels_shape"


async def test_knx_dimmer_with_a_profile_is_refused(db: Database) -> None:
    profile = await lighting.create_fixture_profile(
        db, name="Dimmer", channel_count=1, channels=[{"offset": 0, "role": "dimmer"}]
    )
    address = await knx.create_address(
        db, group_address="1/1/1", name="Cmd", dpt="5.001", direction="outgoing"
    )
    with pytest.raises(ConstraintError) as excinfo:
        await lighting.create_channel(
            db,
            name="Bad knx",
            type="knx_dimmer",
            profile_id=profile.id,
            knx_command_address_id=address.id,
        )
    assert excinfo.value.constraint == "lighting_channels_shape"


async def test_valid_dmx_and_knx_dimmer_channels_are_accepted(db: Database) -> None:
    profile = await lighting.create_fixture_profile(
        db, name="Dimmer", channel_count=1, channels=[{"offset": 0, "role": "dimmer"}]
    )
    output = await devices.create(
        db, category="lighting_output", driver_key="artnet", name="Rack 1", config={}
    )
    dmx = await lighting.create_channel(
        db, name="Dmx1", type="dmx", profile_id=profile.id, device_id=output.id, address=1
    )
    assert dmx.type == "dmx"

    address = await knx.create_address(
        db, group_address="1/1/1", name="Cmd", dpt="5.001", direction="outgoing"
    )
    knx_ch = await lighting.create_channel(
        db, name="Knx1", type="knx_dimmer", knx_command_address_id=address.id
    )
    assert knx_ch.type == "knx_dimmer" and knx_ch.device_id is None


async def test_channel_update_and_optimistic_concurrency(db: Database) -> None:
    profile = await lighting.create_fixture_profile(
        db, name="Dimmer", channel_count=1, channels=[{"offset": 0, "role": "dimmer"}]
    )
    output = await devices.create(
        db, category="lighting_output", driver_key="artnet", name="Rack 1", config={}
    )
    channel = await lighting.create_channel(
        db, name="Ch 1", type="dmx", profile_id=profile.id, device_id=output.id, address=1
    )
    updated = await lighting.update_channel(db, channel.id, channel.updated_at, name="Ch 1 renamed")
    assert updated.name == "Ch 1 renamed"
    with pytest.raises(ConflictError):
        await lighting.update_channel(db, channel.id, channel.updated_at, name="Stale")


# -- device delete protection (Q2 of the Phase 2 slice A plan, §16.1) -------------


async def test_deleting_a_lighting_output_device_with_fixtures_is_refused(db: Database) -> None:
    profile = await lighting.create_fixture_profile(
        db, name="Dimmer", channel_count=1, channels=[{"offset": 0, "role": "dimmer"}]
    )
    output = await devices.create(
        db, category="lighting_output", driver_key="artnet", name="Rack 1", config={}
    )
    channel = await lighting.create_channel(
        db, name="Ch 1", type="dmx", profile_id=profile.id, device_id=output.id, address=1
    )
    with pytest.raises(BaseInUseError):
        await devices.delete(db, output.id)

    # The API layer builds the 409 body from this — the fixture is named.
    refs = await lighting.references_to_device(db, output.id)
    assert [(r.entity, r.id, r.name) for r in refs] == [("lighting_channels", channel.id, "Ch 1")]

    # The device and the fixture both survive the blocked delete.
    assert await devices.get(db, output.id) is not None
    assert await lighting.get_channel(db, channel.id) is not None


# -- the snapshot guard (§9.7) -----------------------------------------------------


async def test_deleting_a_channel_with_a_snapshot_reference_is_refused(db: Database) -> None:
    profile = await lighting.create_fixture_profile(
        db, name="Dimmer", channel_count=1, channels=[{"offset": 0, "role": "dimmer"}]
    )
    output = await devices.create(
        db, category="lighting_output", driver_key="artnet", name="Rack 1", config={}
    )
    channel = await lighting.create_channel(
        db, name="House lights", type="dmx", profile_id=profile.id, device_id=output.id, address=1
    )
    scene = await scenes.create_scene(db, name="Preshow look")
    await scenes.create_action(
        db,
        scene_id=scene.id,
        sort_order=0,
        domain="dmx",
        dmx_snapshot={str(channel.id): 75.0},
        dmx_fade_ms=2000,
    )

    with pytest.raises(InUseError) as excinfo:
        await lighting.delete_channel(db, channel.id)
    assert excinfo.value.references == [
        r for r in excinfo.value.references if r.entity == "scenes"
    ]
    assert any(r.id == scene.id for r in excinfo.value.references)

    # Neither the channel nor the scene is touched by the blocked delete.
    assert await lighting.get_channel(db, channel.id) is not None
    assert await scenes.get_scene(db, scene.id) is not None


async def test_channel_without_a_snapshot_reference_deletes_cleanly(db: Database) -> None:
    profile = await lighting.create_fixture_profile(
        db, name="Dimmer", channel_count=1, channels=[{"offset": 0, "role": "dimmer"}]
    )
    output = await devices.create(
        db, category="lighting_output", driver_key="artnet", name="Rack 1", config={}
    )
    channel = await lighting.create_channel(
        db, name="Spare", type="dmx", profile_id=profile.id, device_id=output.id, address=5
    )
    await lighting.delete_channel(db, channel.id)
    assert await lighting.get_channel(db, channel.id) is None
    with pytest.raises(NotFoundError):
        await lighting.delete_channel(db, channel.id)


# -- overlap detection (§9.1) -------------------------------------------------------


async def test_overlap_detection_finds_shared_slots_and_ignores_other_universes(
    db: Database,
) -> None:
    profile3 = await lighting.create_fixture_profile(
        db, name="RGB", channel_count=3, channels=TWELVE_CHANNEL_PROFILE[:3]
    )
    output = await devices.create(
        db, category="lighting_output", driver_key="artnet", name="Rack 1", config={}
    )
    a = await lighting.create_channel(
        db, name="A", type="dmx", profile_id=profile3.id, device_id=output.id, address=1, universe=1
    )
    b = await lighting.create_channel(
        db, name="B", type="dmx", profile_id=profile3.id, device_id=output.id, address=3, universe=1
    )
    # Same start address, but a different universe: must not be reported.
    await lighting.create_channel(
        db, name="C", type="dmx", profile_id=profile3.id, device_id=output.id, address=1, universe=2
    )
    # No overlap: starts right after B's range (3-5) ends.
    await lighting.create_channel(
        db, name="D", type="dmx", profile_id=profile3.id, device_id=output.id, address=6, universe=1
    )

    overlaps = await lighting.find_overlaps(db)
    assert len(overlaps) == 1
    overlap = overlaps[0]
    assert {overlap.channel_a_id, overlap.channel_b_id} == {a.id, b.id}
    assert overlap.universe == 1
    assert overlap.device_id == output.id


# -- lighting groups and memberships ------------------------------------------------


async def test_group_delete_blocked_lists_both_rules_and_derived_status(db: Database) -> None:
    group = await lighting.create_group(db, name="House")
    address = await knx.create_address(
        db, group_address="3/1/1", name="Binding", dpt="1.001", direction="both"
    )
    rule = await rules.create_rule(
        db,
        name="House binding",
        trigger_type="knx",
        knx_address_id=address.id,
        match_type="any",
        action_type="lighting_group",
        lighting_group_id=group.id,
        on_level=100.0,
        off_level=0.0,
    )
    status_address = await knx.create_address(
        db, group_address="3/1/2", name="Status", dpt="1.001", direction="incoming"
    )
    status = await rules.create_derived_status(
        db,
        name="House status",
        knx_address_id=status_address.id,
        source_type="lighting_group_all_at",
        lighting_group_id=group.id,
    )

    with pytest.raises(InUseError) as excinfo:
        await lighting.delete_group(db, group.id)
    entities = {(r.entity, r.id) for r in excinfo.value.references}
    assert ("rules", rule.id) in entities
    assert ("derived_status", status.id) in entities


async def test_set_group_members_replaces_the_whole_list_in_one_transaction(db: Database) -> None:
    profile = await lighting.create_fixture_profile(
        db, name="Dimmer", channel_count=1, channels=[{"offset": 0, "role": "dimmer"}]
    )
    output = await devices.create(
        db, category="lighting_output", driver_key="artnet", name="Rack 1", config={}
    )
    channels = [
        await lighting.create_channel(
            db, name=f"Ch{i}", type="dmx", profile_id=profile.id, device_id=output.id, address=i
        )
        for i in range(1, 4)
    ]
    group = await lighting.create_group(db, name="All")

    members = await lighting.set_group_members(db, group.id, [c.id for c in channels])
    assert [m.channel_id for m in members] == [c.id for c in channels]
    assert await lighting.get_group_members(db, group.id) == members

    # Replacing with a shorter list drops the rest.
    replaced = await lighting.set_group_members(db, group.id, [channels[1].id])
    assert [m.channel_id for m in replaced] == [channels[1].id]

    with pytest.raises(ValueError):
        await lighting.set_group_members(db, group.id, [channels[0].id, channels[0].id])


async def test_group_and_preset_and_bar_use_optimistic_concurrency(db: Database) -> None:
    group = await lighting.create_group(db, name="G")
    await lighting.update_group(db, group.id, group.updated_at, name="G2")
    with pytest.raises(ConflictError):
        await lighting.update_group(db, group.id, group.updated_at, name="G3")

    preset = await lighting.create_preset(db, name="Warm", r=255, g=180, b=100)
    await lighting.update_preset(db, preset.id, preset.updated_at, name="Warm white")
    with pytest.raises(ConflictError):
        await lighting.update_preset(db, preset.id, preset.updated_at, name="Stale")

    bar = await lighting.create_bar(db, name="Bar 2", sort_order=2)
    await lighting.update_bar(db, bar.id, bar.updated_at, name="Bar 2 renamed")
    with pytest.raises(ConflictError):
        await lighting.update_bar(db, bar.id, bar.updated_at, name="Stale")


async def test_preset_and_bar_delete_are_never_blocked(db: Database) -> None:
    preset = await lighting.create_preset(db, name="Warm", r=255, g=180, b=100)
    await lighting.delete_preset(db, preset.id)
    assert await lighting.get_preset(db, preset.id) is None

    bar = await lighting.create_bar(db, name="Spare bar")
    await lighting.delete_bar(db, bar.id)
    assert await lighting.get_bar(db, bar.id) is None


async def test_existing_seed_bar_carries_backfilled_timestamps(db: Database) -> None:
    bars = await lighting.list_bars(db)
    seeded = next(b for b in bars if b.name == "Proscenium")
    assert seeded.created_at == "2026-01-01T00:00:00+13:00"
    assert seeded.updated_at == "2026-01-01T00:00:00+13:00"
