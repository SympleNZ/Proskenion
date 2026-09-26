"""``scenes`` and ``scene_actions`` (§8, §9.7, §15.8, §16.1)."""

import pytest

from proskenion.db.connection import Database
from proskenion.db.crud import devices, knx, lighting, rules, scenes
from proskenion.db.crud.base import ConflictError, NotFoundError
from proskenion.db.crud.refs import InUseError


async def test_scene_crud_and_optimistic_concurrency(db: Database) -> None:
    scene = await scenes.create_scene(db, name="House to half", priority="normal")
    assert scene.enabled is True and scene.protected is False
    updated = await scenes.update_scene(
        db, scene.id, scene.updated_at, name="House to half (renamed)"
    )
    assert updated.name == "House to half (renamed)"
    with pytest.raises(ConflictError):
        await scenes.update_scene(db, scene.id, scene.updated_at, name="Stale")
    with pytest.raises(ValueError):
        await scenes.create_scene(db, name="Bad priority", priority="urgent")


async def test_deleting_a_scene_used_by_a_rule_is_refused(db: Database) -> None:
    scene = await scenes.create_scene(db, name="Interval")
    rule = await rules.create_rule(
        db,
        name="Interval button",
        trigger_type="surface",
        action_type="run_scene",
        scene_id=scene.id,
    )
    with pytest.raises(InUseError) as excinfo:
        await scenes.delete_scene(db, scene.id)
    assert [(r.entity, r.id) for r in excinfo.value.references] == [("rules", rule.id)]
    assert await scenes.get_scene(db, scene.id) is not None

    await rules.delete_rule(db, rule.id)
    await scenes.delete_scene(db, scene.id)
    assert await scenes.get_scene(db, scene.id) is None


async def test_deleting_a_scene_cascades_its_actions(db: Database) -> None:
    scene = await scenes.create_scene(db, name="Blackout")
    action = await scenes.create_action(db, scene_id=scene.id, sort_order=0, domain="knx")
    await scenes.delete_scene(db, scene.id)
    assert await scenes.get_action(db, action.id) is None


async def test_scene_action_crud_and_optimistic_concurrency(db: Database) -> None:
    scene = await scenes.create_scene(db, name="Look 1")
    action = await scenes.create_action(
        db,
        scene_id=scene.id,
        sort_order=0,
        domain="dmx",
        dmx_snapshot={"1": 50.0, "2": 100.0},
        dmx_fade_ms=3000,
    )
    assert action.dmx_snapshot == {"1": 50.0, "2": 100.0}
    assert action.created_at == action.updated_at

    listed = await scenes.list_actions(db, scene.id)
    assert [a.id for a in listed] == [action.id]

    updated = await scenes.update_action(db, action.id, action.updated_at, dmx_fade_ms=5000)
    assert updated.dmx_fade_ms == 5000
    with pytest.raises(ConflictError):
        await scenes.update_action(db, action.id, action.updated_at, dmx_fade_ms=1)

    await scenes.delete_action(db, action.id)
    assert await scenes.get_action(db, action.id) is None
    with pytest.raises(NotFoundError):
        await scenes.delete_action(db, action.id)


async def test_dmx_and_knx_scene_actions_accept_the_forward_referencing_columns_as_null(
    db: Database,
) -> None:
    """Deviation 2 of 003_lighting_rules_scenes.sql: mixer/hdmi REFERENCES are
    omitted until Phase 3/4 exist, but the columns themselves are usable now."""
    scene = await scenes.create_scene(db, name="Look 2")
    address = await knx.create_address(
        db, group_address="5/1/1", name="Cmd", dpt="1.001", direction="outgoing"
    )
    action = await scenes.create_action(
        db,
        scene_id=scene.id,
        sort_order=0,
        domain="knx",
        knx_address_id=address.id,
        knx_value="1",
    )
    assert action.mixer_scene_id is None
    assert action.mixer_channel_id is None
    assert action.hdmi_destination is None
    assert action.hdmi_input_id is None


# -- the snapshot integrity check (§9.7) --------------------------------------------


async def test_find_orphaned_snapshots_finds_one_inserted_behind_its_back(db: Database) -> None:
    profile = await lighting.create_fixture_profile(
        db, name="Dimmer", channel_count=1, channels=[{"offset": 0, "role": "dimmer"}]
    )
    output = await devices.create(
        db, category="lighting_output", driver_key="artnet", name="Rack 1", config={}
    )
    channel = await lighting.create_channel(
        db, name="Ch 1", type="dmx", profile_id=profile.id, device_id=output.id, address=1
    )
    scene = await scenes.create_scene(db, name="Look 3")

    # A legitimate reference: not an orphan.
    await scenes.create_action(
        db, scene_id=scene.id, sort_order=0, domain="dmx", dmx_snapshot={str(channel.id): 50.0}
    )
    assert await scenes.find_orphaned_snapshots(db) == []

    # An orphan "inserted behind its back": a snapshot naming a channel id that
    # was never created, bypassing the delete_channel guard entirely.
    orphan_action = await scenes.create_action(
        db, scene_id=scene.id, sort_order=1, domain="dmx", dmx_snapshot={"999999": 10.0}
    )

    orphans = await scenes.find_orphaned_snapshots(db)
    assert len(orphans) == 1
    assert orphans[0].scene_action_id == orphan_action.id
    assert orphans[0].scene_id == scene.id
    assert orphans[0].channel_id == 999999
