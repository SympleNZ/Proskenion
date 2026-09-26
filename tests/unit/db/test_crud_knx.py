"""``knx_device_groups`` and ``knx_group_addresses`` (§15.7, §16.1, §21.22)."""

import pytest

from proskenion.db.connection import Database
from proskenion.db.crud import knx, lighting, rules
from proskenion.db.crud.base import ConflictError, NotFoundError
from proskenion.db.crud.refs import InUseError


async def test_device_group_crud_and_addresses_fall_back_to_null_on_delete(db: Database) -> None:
    group = await knx.create_device_group(db, name="Foyer KNX", location="Plant room")
    assert group.id > 0
    assert group.created_at == group.updated_at

    address = await knx.create_address(
        db, group_address="1/1/1", name="House lights", dpt="1.001", direction="both",
        device_id=group.id,
    )
    assert address.device_id == group.id

    refs = await knx.references_device_group(db, group.id)
    assert [r.entity for r in refs] == ["knx_group_addresses"]
    assert refs[0].id == address.id

    # SET NULL, not RESTRICT: the group deletes even though an address names it.
    await knx.delete_device_group(db, group.id)
    assert await knx.get_device_group(db, group.id) is None
    after = await knx.get_address(db, address.id)
    assert after is not None and after.device_id is None


async def test_device_group_update_uses_optimistic_concurrency(db: Database) -> None:
    group = await knx.create_device_group(db, name="A")
    updated = await knx.update_device_group(db, group.id, group.updated_at, name="B")
    assert updated.name == "B" and updated.updated_at != group.updated_at
    with pytest.raises(ConflictError):
        await knx.update_device_group(db, group.id, group.updated_at, name="C")


async def test_address_crud_and_bad_direction_rejected(db: Database) -> None:
    address = await knx.create_address(
        db, group_address="1/2/3", name="Test", dpt="5.001", direction="incoming"
    )
    assert address.is_heartbeat is False
    with pytest.raises(ValueError):
        await knx.create_address(
            db, group_address="1/2/4", name="Bad", dpt="5.001", direction="sideways"
        )
    with pytest.raises(ValueError):
        await knx.update_address(db, address.id, address.updated_at, direction="sideways")

    updated = await knx.update_address(db, address.id, address.updated_at, name="Renamed")
    assert updated.name == "Renamed"
    with pytest.raises(ConflictError) as excinfo:
        await knx.update_address(db, address.id, address.updated_at, name="Stale")
    assert excinfo.value.current["name"] == "Renamed"

    with pytest.raises(NotFoundError):
        await knx.delete_address(db, 99999)


async def test_address_delete_restrict_lists_the_rule_using_it(db: Database) -> None:
    address = await knx.create_address(
        db, group_address="1/9/9", name="Binding address", dpt="1.001", direction="both"
    )
    group = await lighting.create_group(db, name="Aisle lights")
    rule = await rules.create_rule(
        db,
        name="Aisle binding",
        trigger_type="knx",
        knx_address_id=address.id,
        match_type="any",
        action_type="lighting_group",
        lighting_group_id=group.id,
        on_level=100.0,
        off_level=0.0,
    )

    with pytest.raises(InUseError) as excinfo:
        await knx.delete_address(db, address.id)
    assert excinfo.value.table == "knx_group_addresses"
    refs = excinfo.value.references
    assert ("rules", rule.id, rule.name) in [(r.entity, r.id, r.name) for r in refs]

    # The address survives the blocked delete.
    assert await knx.get_address(db, address.id) is not None

    # Once the rule is gone, the address can be deleted.
    await rules.delete_rule(db, rule.id)
    await knx.delete_address(db, address.id)
    assert await knx.get_address(db, address.id) is None


async def test_address_references_cover_lighting_channels_and_derived_status(db: Database) -> None:
    command = await knx.create_address(
        db, group_address="2/1/1", name="Command", dpt="5.001", direction="outgoing"
    )
    channel = await lighting.create_channel(
        db, name="Dimmer 1", type="knx_dimmer", knx_command_address_id=command.id
    )
    status_addr = await knx.create_address(
        db, group_address="2/1/2", name="Status", dpt="1.001", direction="incoming"
    )
    await rules.create_derived_status(
        db, name="All at zero", knx_address_id=status_addr.id, source_type="external_control"
    )

    with pytest.raises(InUseError) as excinfo:
        await knx.delete_address(db, command.id)
    assert any(
        r.entity == "lighting_channels" and r.id == channel.id for r in excinfo.value.references
    )

    with pytest.raises(InUseError) as excinfo:
        await knx.delete_address(db, status_addr.id)
    assert any(r.entity == "derived_status" for r in excinfo.value.references)
