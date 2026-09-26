"""``rules`` and ``derived_status`` (§8, §15.8, §16.1, §22.2)."""

import pytest

from proskenion.db.connection import Database
from proskenion.db.crud import knx, lighting, rules, scenes
from proskenion.db.crud.base import ConflictError
from proskenion.db.crud.refs import ConstraintError


async def _address(db: Database, addr: str = "1/1/1") -> int:
    a = await knx.create_address(db, group_address=addr, name="A", dpt="1.001", direction="both")
    return a.id


async def test_run_scene_rule_is_accepted(db: Database) -> None:
    scene = await scenes.create_scene(db, name="House to half")
    rule = await rules.create_rule(
        db,
        name="Panel button 1",
        trigger_type="surface",
        action_type="run_scene",
        scene_id=scene.id,
    )
    assert rule.action_type == "run_scene" and rule.scene_id == scene.id


async def test_invalid_binding_rule_raises_typed_constraint_error(db: Database) -> None:
    address_id = await _address(db)
    group = await lighting.create_group(db, name="G")

    # action_type = lighting_group without match_type = 'any' on a knx trigger
    # trips the CHECK (§8.2).
    with pytest.raises(ConstraintError) as excinfo:
        await rules.create_rule(
            db,
            name="Bad binding",
            trigger_type="knx",
            knx_address_id=address_id,
            match_type="equal",  # wrong — must be 'any' for a binding
            action_type="lighting_group",
            lighting_group_id=group.id,
            on_level=100.0,
            off_level=0.0,
        )
    assert excinfo.value.constraint == "rules_action_shape"

    # Missing on_level/off_level trips the same CHECK.
    with pytest.raises(ConstraintError):
        await rules.create_rule(
            db,
            name="Missing levels",
            trigger_type="knx",
            knx_address_id=address_id,
            match_type="any",
            action_type="lighting_group",
            lighting_group_id=group.id,
        )


async def test_valid_binding_rule_forces_any_match_and_both_levels(db: Database) -> None:
    address_id = await _address(db, "1/1/2")
    group = await lighting.create_group(db, name="G2")
    rule = await rules.create_rule(
        db,
        name="Good binding",
        trigger_type="knx",
        knx_address_id=address_id,
        match_type="any",
        action_type="lighting_group",
        lighting_group_id=group.id,
        on_level=100.0,
        off_level=0.0,
    )
    assert rule.match_type == "any"
    assert (rule.on_level, rule.off_level) == (100.0, 0.0)


async def test_notify_rule_needs_a_message(db: Database) -> None:
    with pytest.raises(ConstraintError):
        await rules.create_rule(
            db, name="Silent notify", trigger_type="schedule", action_type="notify", message=None
        )
    ok = await rules.create_rule(
        db,
        name="Notify",
        trigger_type="schedule",
        action_type="notify",
        message="Interval started",
    )
    assert ok.message == "Interval started"


async def test_rule_update_and_optimistic_concurrency(db: Database) -> None:
    rule = await rules.create_rule(
        db, name="Notify", trigger_type="schedule", action_type="notify", message="x"
    )
    updated = await rules.update_rule(db, rule.id, rule.updated_at, name="Notify renamed")
    assert updated.name == "Notify renamed"
    with pytest.raises(ConflictError):
        await rules.update_rule(db, rule.id, rule.updated_at, name="Stale")


async def test_rule_delete_succeeds_when_unreferenced(db: Database) -> None:
    rule = await rules.create_rule(
        db, name="Notify", trigger_type="schedule", action_type="notify", message="x"
    )
    await rules.delete_rule(db, rule.id)
    assert await rules.get_rule(db, rule.id) is None


# -- derived_status ----------------------------------------------------------------


async def test_duplicate_derived_status_address_raises_typed_constraint_error(db: Database) -> None:
    address_id = await _address(db, "4/1/1")
    await rules.create_derived_status(
        db, name="First", knx_address_id=address_id, source_type="device_state"
    )
    with pytest.raises(ConstraintError) as excinfo:
        await rules.create_derived_status(
            db, name="Second", knx_address_id=address_id, source_type="device_state"
        )
    assert excinfo.value.constraint == "derived_status_knx_address_unique"


async def test_derived_status_update_and_optimistic_concurrency(db: Database) -> None:
    address_id = await _address(db, "4/1/2")
    status = await rules.create_derived_status(
        db, name="Status", knx_address_id=address_id, source_type="device_state"
    )
    updated = await rules.update_derived_status(db, status.id, status.updated_at, name="Renamed")
    assert updated.name == "Renamed"
    with pytest.raises(ConflictError):
        await rules.update_derived_status(db, status.id, status.updated_at, name="Stale")


async def test_derived_status_delete_is_never_blocked(db: Database) -> None:
    address_id = await _address(db, "4/1/3")
    status = await rules.create_derived_status(
        db, name="Status", knx_address_id=address_id, source_type="device_state"
    )
    await rules.delete_derived_status(db, status.id)
    assert await rules.get_derived_status(db, status.id) is None
