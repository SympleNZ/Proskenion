"""``matrix_inputs``, ``matrix_outputs``, ``video_destinations`` and
``video_destination_outputs`` (§15.10, §16.1, §21.22)."""

import sqlite3

import pytest

from proskenion.db.connection import Database
from proskenion.db.crud import devices, scenes, video
from proskenion.db.crud.base import ConflictError, NotFoundError
from proskenion.db.crud.base import InUseError as BaseInUseError
from proskenion.db.crud.refs import ConstraintError, InUseError


async def _matrix_device(db: Database, name: str = "LKV422") -> devices.Device:
    return await devices.create(
        db, category="video_matrix", driver_key="lkv422", name=name, config={}
    )


# -- matrix inputs -------------------------------------------------------------


async def test_input_crud_and_optimistic_concurrency(db: Database) -> None:
    device = await _matrix_device(db)
    created = await video.create_input(db, device_id=device.id, driver_ref="1", name="Laptop")
    assert created.id > 0
    assert created.created_at == created.updated_at
    assert created.description is None

    fetched = await video.get_input(db, created.id)
    assert fetched == created

    updated = await video.update_input(db, created.id, created.updated_at, name="Laptop (podium)")
    assert updated.name == "Laptop (podium)" and updated.updated_at != created.updated_at
    with pytest.raises(ConflictError) as excinfo:
        await video.update_input(db, created.id, created.updated_at, name="Stale")
    assert excinfo.value.current["name"] == "Laptop (podium)"

    await video.delete_input(db, created.id)
    assert await video.get_input(db, created.id) is None
    with pytest.raises(NotFoundError):
        await video.delete_input(db, created.id)


async def test_input_device_and_driver_ref_is_unique(db: Database) -> None:
    device = await _matrix_device(db)
    await video.create_input(db, device_id=device.id, driver_ref="1", name="Laptop")
    with pytest.raises(ConstraintError):
        await video.create_input(db, device_id=device.id, driver_ref="1", name="Duplicate")

    other = await video.create_input(db, device_id=device.id, driver_ref="2", name="Blu-ray")
    with pytest.raises(ConstraintError):
        await video.update_input(db, other.id, other.updated_at, driver_ref="1")


async def test_get_input_by_driver_ref(db: Database) -> None:
    device = await _matrix_device(db)
    other_device = await _matrix_device(db, name="Second matrix")
    laptop = await video.create_input(db, device_id=device.id, driver_ref="1", name="Laptop")
    await video.create_input(db, device_id=other_device.id, driver_ref="1", name="Unrelated")

    found = await video.get_input_by_driver_ref(db, device.id, "1")
    assert found == laptop
    assert await video.get_input_by_driver_ref(db, device.id, "9") is None


async def test_list_inputs_by_device_and_sort_order(db: Database) -> None:
    device = await _matrix_device(db)
    other_device = await _matrix_device(db, name="Second matrix")
    second = await video.create_input(
        db, device_id=device.id, driver_ref="2", name="B", sort_order=1
    )
    first = await video.create_input(
        db, device_id=device.id, driver_ref="1", name="A", sort_order=0
    )
    await video.create_input(db, device_id=other_device.id, driver_ref="1", name="Elsewhere")

    listed = await video.list_inputs(db, device_id=device.id)
    assert [i.id for i in listed] == [first.id, second.id]


async def test_deleting_an_input_used_by_a_scene_action_is_refused(db: Database) -> None:
    device = await _matrix_device(db)
    matrix_input = await video.create_input(db, device_id=device.id, driver_ref="1", name="Laptop")
    destination = await video.create_destination(db, device_id=device.id, name="The room")
    scene = await scenes.create_scene(db, name="Performance Start")
    await scenes.create_action(
        db,
        scene_id=scene.id,
        sort_order=0,
        domain="hdmi_source",
        hdmi_destination=destination.id,
        hdmi_input_id=matrix_input.id,
    )

    with pytest.raises(InUseError) as excinfo:
        await video.delete_input(db, matrix_input.id)
    assert excinfo.value.table == "matrix_inputs"
    assert [(r.entity, r.name) for r in excinfo.value.references] == [("scene_actions", scene.name)]
    assert await video.get_input(db, matrix_input.id) is not None


async def test_deleting_an_input_used_as_a_default_sets_it_null_not_blocked(db: Database) -> None:
    device = await _matrix_device(db)
    matrix_input = await video.create_input(db, device_id=device.id, driver_ref="1", name="Laptop")
    destination = await video.create_destination(
        db, device_id=device.id, name="The room", default_input_id=matrix_input.id
    )

    await video.delete_input(db, matrix_input.id)
    assert await video.get_input(db, matrix_input.id) is None
    after = await video.get_destination(db, destination.id)
    assert after is not None and after.default_input_id is None


# -- matrix outputs -------------------------------------------------------------


async def test_output_crud_and_optimistic_concurrency(db: Database) -> None:
    device = await _matrix_device(db)
    created = await video.create_output(
        db, device_id=device.id, driver_ref="1", name="Foyer screen"
    )
    fetched = await video.get_output(db, created.id)
    assert fetched == created

    updated = await video.update_output(
        db, created.id, created.updated_at, description="Above the bar"
    )
    assert updated.description == "Above the bar"
    with pytest.raises(ConflictError):
        await video.update_output(db, created.id, created.updated_at, description="Stale")


async def test_output_device_and_driver_ref_is_unique(db: Database) -> None:
    device = await _matrix_device(db)
    await video.create_output(db, device_id=device.id, driver_ref="1", name="A")
    with pytest.raises(ConstraintError):
        await video.create_output(db, device_id=device.id, driver_ref="1", name="B")


async def test_deleting_an_output_removes_it_from_its_destination_not_blocked(db: Database) -> None:
    device = await _matrix_device(db)
    output = await video.create_output(db, device_id=device.id, driver_ref="1", name="Main")
    destination = await video.create_destination(db, device_id=device.id, name="The room")
    await video.set_destination_outputs(db, destination.id, [output.id])

    await video.delete_output(db, output.id)
    assert await video.get_output(db, output.id) is None
    assert await video.get_destination_outputs(db, destination.id) == []


# -- video destinations ----------------------------------------------------------


async def test_destination_crud_and_optimistic_concurrency(db: Database) -> None:
    device = await _matrix_device(db)
    created = await video.create_destination(db, device_id=device.id, name="The room")
    assert created.default_input_id is None

    matrix_input = await video.create_input(db, device_id=device.id, driver_ref="1", name="Laptop")
    updated = await video.update_destination(
        db, created.id, created.updated_at, default_input_id=matrix_input.id
    )
    assert updated.default_input_id == matrix_input.id
    with pytest.raises(ConflictError):
        await video.update_destination(db, created.id, created.updated_at, name="Stale")

    # Explicit None clears the default, distinct from omitting the key.
    cleared = await video.update_destination(
        db, created.id, updated.updated_at, default_input_id=None
    )
    assert cleared.default_input_id is None


async def test_deleting_a_destination_used_by_a_scene_action_is_refused(db: Database) -> None:
    device = await _matrix_device(db)
    destination = await video.create_destination(db, device_id=device.id, name="The room")
    matrix_input = await video.create_input(db, device_id=device.id, driver_ref="1", name="Laptop")
    scene = await scenes.create_scene(db, name="Interval")
    await scenes.create_action(
        db,
        scene_id=scene.id,
        sort_order=0,
        domain="hdmi_source",
        hdmi_destination=destination.id,
        hdmi_input_id=matrix_input.id,
    )

    with pytest.raises(InUseError) as excinfo:
        await video.delete_destination(db, destination.id)
    assert [(r.entity, r.name) for r in excinfo.value.references] == [("scene_actions", scene.name)]

    assert await video.get_destination(db, destination.id) is not None


async def test_destination_outputs_replace_in_order_and_reject_duplicates(db: Database) -> None:
    device = await _matrix_device(db)
    destination = await video.create_destination(db, device_id=device.id, name="The room")
    hdmi_a = await video.create_output(db, device_id=device.id, driver_ref="1", name="A")
    hdmi_b = await video.create_output(db, device_id=device.id, driver_ref="2", name="B")

    assert await video.get_destination_outputs(db, destination.id) == []

    with pytest.raises(ValueError):
        await video.set_destination_outputs(db, destination.id, [hdmi_a.id, hdmi_a.id])

    result = await video.set_destination_outputs(db, destination.id, [hdmi_b.id, hdmi_a.id])
    assert [o.output_id for o in result] == [hdmi_b.id, hdmi_a.id]
    listed = await video.get_destination_outputs(db, destination.id)
    assert [o.output_id for o in listed] == [hdmi_b.id, hdmi_a.id]
    assert [o.sort_order for o in listed] == [0, 1]

    # Replacing again drops the previous assignment entirely.
    replaced = await video.set_destination_outputs(db, destination.id, [hdmi_a.id])
    assert [o.output_id for o in replaced] == [hdmi_a.id]
    after = await video.get_destination_outputs(db, destination.id)
    assert [o.output_id for o in after] == [hdmi_a.id]


async def test_destination_output_database_constraint(db: Database) -> None:
    """``UNIQUE(destination_id, output_id)`` itself, independent of
    :func:`~proskenion.db.crud.video.set_destination_outputs`'s own
    duplicate check (which only guards one call's list against itself) —
    the same distinction ``test_input_device_and_driver_ref_is_unique``
    draws between the client-side check and the table constraint."""
    device = await _matrix_device(db)
    destination = await video.create_destination(db, device_id=device.id, name="The room")
    output = await video.create_output(db, device_id=device.id, driver_ref="1", name="Main")
    await video.set_destination_outputs(db, destination.id, [output.id])

    with pytest.raises(sqlite3.IntegrityError):
        async with db.write() as conn:
            await conn.execute(
                "INSERT INTO video_destination_outputs (destination_id, output_id, sort_order) "
                "VALUES (?, ?, 1)",
                (destination.id, output.id),
            )
    # The original assignment survives the failed duplicate insert.
    assert [o.output_id for o in await video.get_destination_outputs(db, destination.id)] == [
        output.id
    ]

    # The same output may still belong to a different destination — the
    # constraint is on the pair, not the output alone.
    other = await video.create_destination(db, device_id=device.id, name="Foyer")
    await video.set_destination_outputs(db, other.id, [output.id])
    assert [o.output_id for o in await video.get_destination_outputs(db, other.id)] == [output.id]


# -- cascade from a device (§15.10) --------------------------------------------


async def test_deleting_a_device_cascades_to_its_inputs_outputs_and_destinations(
    db: Database,
) -> None:
    device = await _matrix_device(db)
    matrix_input = await video.create_input(db, device_id=device.id, driver_ref="1", name="Laptop")
    output = await video.create_output(db, device_id=device.id, driver_ref="1", name="Main")
    destination = await video.create_destination(db, device_id=device.id, name="The room")
    await video.set_destination_outputs(db, destination.id, [output.id])

    await devices.delete(db, device.id)

    assert await video.get_input(db, matrix_input.id) is None
    assert await video.get_output(db, output.id) is None
    assert await video.get_destination(db, destination.id) is None
    assert await video.get_destination_outputs(db, destination.id) == []


async def test_deleting_a_device_still_used_by_a_scene_action_is_refused(db: Database) -> None:
    """A scene action's RESTRICT on the input/destination blocks the cascade
    from the device that owns them, the same as any other blocked delete."""
    device = await _matrix_device(db)
    matrix_input = await video.create_input(db, device_id=device.id, driver_ref="1", name="Laptop")
    destination = await video.create_destination(db, device_id=device.id, name="The room")
    scene = await scenes.create_scene(db, name="Performance Start")
    await scenes.create_action(
        db,
        scene_id=scene.id,
        sort_order=0,
        domain="hdmi_source",
        hdmi_destination=destination.id,
        hdmi_input_id=matrix_input.id,
    )

    with pytest.raises(BaseInUseError):
        await devices.delete(db, device.id)
    assert await video.get_input(db, matrix_input.id) is not None
