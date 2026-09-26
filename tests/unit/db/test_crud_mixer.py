"""``mixer_channels``, ``mixer_channel_refs`` and ``mixer_desk_scenes`` (§15.6,
§7.3, §13.5, §16.1)."""

import sqlite3

import pytest

from proskenion.db.connection import Database
from proskenion.db.crud import devices, mixer, scenes
from proskenion.db.crud.base import ConflictError, NotFoundError
from proskenion.db.crud.refs import ConstraintError, InUseError


async def _mixer_device(db: Database, name: str = "CQ-20B") -> devices.Device:
    return await devices.create(db, category="mixer", driver_key="cq20b", name=name, config={})


# -- mixer channels --------------------------------------------------------------


async def test_channel_crud_and_optimistic_concurrency(db: Database) -> None:
    device = await _mixer_device(db)
    created = await mixer.create_channel(db, device_id=device.id, name="Mic 1")
    assert created.channel_kind == "input"
    assert created.created_at == created.updated_at
    assert created.short_name is None
    assert created.hirer_max_db is None
    assert created.unmapped is False
    assert created.tracked is True
    assert created.sort_order == 0

    fetched = await mixer.get_channel(db, created.id)
    assert fetched == created

    updated = await mixer.update_channel(db, created.id, created.updated_at, name="Mic 1 (podium)")
    assert updated.name == "Mic 1 (podium)" and updated.updated_at != created.updated_at
    with pytest.raises(ConflictError) as excinfo:
        await mixer.update_channel(db, created.id, created.updated_at, name="Stale")
    assert excinfo.value.current["name"] == "Mic 1 (podium)"


async def test_bad_channel_kind_is_refused_on_create_and_update(db: Database) -> None:
    device = await _mixer_device(db)
    with pytest.raises(ValueError):
        await mixer.create_channel(db, device_id=device.id, name="Bad kind", channel_kind="dj")

    channel = await mixer.create_channel(db, device_id=device.id, name="Mic 1")
    with pytest.raises(ValueError):
        await mixer.update_channel(db, channel.id, channel.updated_at, channel_kind="dj")


async def test_list_channels_by_device_and_sort_order(db: Database) -> None:
    device = await _mixer_device(db)
    other_device = await _mixer_device(db, name="Second desk")
    second = await mixer.create_channel(db, device_id=device.id, name="Mic 2", sort_order=1)
    first = await mixer.create_channel(db, device_id=device.id, name="Mic 1", sort_order=0)
    await mixer.create_channel(db, device_id=other_device.id, name="Elsewhere")

    listed = await mixer.list_channels(db, device_id=device.id)
    assert [c.id for c in listed] == [first.id, second.id]
    assert len(await mixer.list_channels(db)) == 3


# -- ganged driver references ------------------------------------------------------


async def test_channel_refs_replace_in_order_and_reject_duplicates(db: Database) -> None:
    device = await _mixer_device(db)
    channel = await mixer.create_channel(db, device_id=device.id, name="Out 1/2")

    assert await mixer.get_channel_refs(db, channel.id) == []

    with pytest.raises(ValueError):
        await mixer.set_channel_refs(db, channel.id, ["out1", "out1"])

    result = await mixer.set_channel_refs(db, channel.id, ["out2", "out1"])
    assert [r.driver_ref for r in result] == ["out2", "out1"]
    assert [r.sort_order for r in result] == [0, 1]
    listed = await mixer.get_channel_refs(db, channel.id)
    assert [r.driver_ref for r in listed] == ["out2", "out1"]

    # Replacing again drops the previous assignment entirely, and the first
    # is authoritative for display (§5.5 *Ganged channels*).
    replaced = await mixer.set_channel_refs(db, channel.id, ["ip1"])
    assert [r.driver_ref for r in replaced] == ["ip1"]
    after = await mixer.get_channel_refs(db, channel.id)
    assert [r.driver_ref for r in after] == ["ip1"]

    with pytest.raises(NotFoundError):
        await mixer.set_channel_refs(db, 999999, ["ip1"])


async def test_channel_refs_database_constraint(db: Database) -> None:
    """``UNIQUE(channel_id, driver_ref)`` itself, independent of
    :func:`~proskenion.db.crud.mixer.set_channel_refs`'s own duplicate check
    (which only guards one call's list against itself), the same
    distinction video's equivalent test draws."""
    device = await _mixer_device(db)
    channel = await mixer.create_channel(db, device_id=device.id, name="Mic 1")
    await mixer.set_channel_refs(db, channel.id, ["ip1"])

    with pytest.raises(sqlite3.IntegrityError):
        async with db.write() as conn:
            await conn.execute(
                "INSERT INTO mixer_channel_refs (channel_id, driver_ref, sort_order) "
                "VALUES (?, ?, 1)",
                (channel.id, "ip1"),
            )
    assert [r.driver_ref for r in await mixer.get_channel_refs(db, channel.id)] == ["ip1"]


async def test_get_channel_by_driver_ref(db: Database) -> None:
    device = await _mixer_device(db)
    other_device = await _mixer_device(db, name="Second desk")
    mic1 = await mixer.create_channel(db, device_id=device.id, name="Mic 1")
    await mixer.set_channel_refs(db, mic1.id, ["ip1"])
    await mixer.create_channel(db, device_id=other_device.id, name="Elsewhere")

    found = await mixer.get_channel_by_driver_ref(db, device.id, "ip1")
    assert found == mic1
    assert await mixer.get_channel_by_driver_ref(db, device.id, "ip9") is None
    assert await mixer.get_channel_by_driver_ref(db, other_device.id, "ip1") is None


async def test_list_channels_with_refs(db: Database) -> None:
    device = await _mixer_device(db)
    mic1 = await mixer.create_channel(db, device_id=device.id, name="Mic 1", sort_order=0)
    out12 = await mixer.create_channel(db, device_id=device.id, name="Out 1/2", sort_order=1)
    unpatched = await mixer.create_channel(db, device_id=device.id, name="Spare", sort_order=2)
    await mixer.set_channel_refs(db, mic1.id, ["ip1"])
    await mixer.set_channel_refs(db, out12.id, ["out1", "out2"])

    bundled = await mixer.list_channels_with_refs(db, device.id)
    assert [b.channel.id for b in bundled] == [mic1.id, out12.id, unpatched.id]
    assert [r.driver_ref for r in bundled[0].refs] == ["ip1"]
    assert [r.driver_ref for r in bundled[1].refs] == ["out1", "out2"]
    assert bundled[2].refs == []

    assert await mixer.list_channels_with_refs(db, 999999) == []


# -- Main channel (§7.3) ------------------------------------------------------------


async def test_main_channel_cannot_be_deleted(db: Database) -> None:
    device = await _mixer_device(db)
    main = await mixer.create_channel(db, device_id=device.id, name="Main LR", channel_kind="main")

    with pytest.raises(ConstraintError) as excinfo:
        await mixer.delete_channel(db, main.id)
    assert excinfo.value.constraint == "mixer_channels_main_immutable"
    assert await mixer.get_channel(db, main.id) is not None


async def test_only_one_main_channel_per_device(db: Database) -> None:
    device = await _mixer_device(db)
    other_device = await _mixer_device(db, name="Second desk")
    await mixer.create_channel(db, device_id=device.id, name="Main LR", channel_kind="main")

    with pytest.raises(ConstraintError) as excinfo:
        await mixer.create_channel(db, device_id=device.id, name="Main LR 2", channel_kind="main")
    assert excinfo.value.constraint == "mixer_channels_one_main_per_device"

    # A different device may have its own Main channel.
    await mixer.create_channel(db, device_id=other_device.id, name="Main LR", channel_kind="main")


async def test_promoting_a_channel_to_main_is_also_guarded(db: Database) -> None:
    device = await _mixer_device(db)
    await mixer.create_channel(db, device_id=device.id, name="Main LR", channel_kind="main")
    other = await mixer.create_channel(db, device_id=device.id, name="Mic 1")

    with pytest.raises(ConstraintError) as excinfo:
        await mixer.update_channel(db, other.id, other.updated_at, channel_kind="main")
    assert excinfo.value.constraint == "mixer_channels_one_main_per_device"


async def test_main_cannot_be_demoted_and_so_deleted(db: Database) -> None:
    device = await _mixer_device(db)
    main = await mixer.create_channel(db, device_id=device.id, name="Main LR", channel_kind="main")

    with pytest.raises(ConstraintError) as excinfo:
        await mixer.update_channel(db, main.id, main.updated_at, channel_kind="input")
    assert excinfo.value.constraint == "mixer_channels_main_immutable"
    # Renaming Main is still allowed (§7.3).
    renamed = await mixer.update_channel(db, main.id, main.updated_at, name="PA")
    assert renamed.name == "PA" and renamed.channel_kind == "main"


async def test_deleting_a_channel_used_by_a_scene_action_is_refused(db: Database) -> None:
    device = await _mixer_device(db)
    channel = await mixer.create_channel(db, device_id=device.id, name="Mic 1")
    scene = await scenes.create_scene(db, name="Lecture")
    await scenes.create_action(
        db, scene_id=scene.id, sort_order=0, domain="mixer_fader", mixer_channel_id=channel.id
    )

    with pytest.raises(InUseError) as excinfo:
        await mixer.delete_channel(db, channel.id)
    assert excinfo.value.table == "mixer_channels"
    assert [(r.entity, r.name) for r in excinfo.value.references] == [("scene_actions", scene.name)]
    assert await mixer.get_channel(db, channel.id) is not None


async def test_deleting_a_device_cascades_to_channels_and_refs(db: Database) -> None:
    device = await _mixer_device(db)
    channel = await mixer.create_channel(db, device_id=device.id, name="Mic 1")
    await mixer.set_channel_refs(db, channel.id, ["ip1"])

    await devices.delete(db, device.id)

    assert await mixer.get_channel(db, channel.id) is None
    assert await mixer.get_channel_refs(db, channel.id) == []


# -- desk scene library and the Venue Default (§13.5) -------------------------------


async def test_desk_scene_crud_and_optimistic_concurrency(db: Database) -> None:
    device = await _mixer_device(db)
    created = await mixer.create_desk_scene(db, device_id=device.id, scene_ref="3", name="Lecture")
    assert created.is_venue_default is False
    assert created.created_at == created.updated_at

    fetched = await mixer.get_desk_scene(db, created.id)
    assert fetched == created

    updated = await mixer.update_desk_scene(
        db, created.id, created.updated_at, notes="Updated March 2026"
    )
    assert updated.notes == "Updated March 2026"
    with pytest.raises(ConflictError):
        await mixer.update_desk_scene(db, created.id, created.updated_at, notes="Stale")


async def test_desk_scene_device_and_scene_ref_is_unique(db: Database) -> None:
    device = await _mixer_device(db)
    await mixer.create_desk_scene(db, device_id=device.id, scene_ref="3", name="Lecture")
    with pytest.raises(ConstraintError):
        await mixer.create_desk_scene(db, device_id=device.id, scene_ref="3", name="Duplicate")

    other = await mixer.create_desk_scene(db, device_id=device.id, scene_ref="4", name="Concert")
    with pytest.raises(ConstraintError):
        await mixer.update_desk_scene(db, other.id, other.updated_at, scene_ref="3")


async def test_creating_a_venue_default_clears_the_previous_one_in_one_transaction(
    db: Database,
) -> None:
    device = await _mixer_device(db)
    first = await mixer.create_desk_scene(
        db, device_id=device.id, scene_ref="1", name="Venue Default", is_venue_default=True
    )
    assert first.is_venue_default is True
    assert (await mixer.get_venue_default(db, device.id)) == first

    second = await mixer.create_desk_scene(
        db, device_id=device.id, scene_ref="2", name="New default", is_venue_default=True
    )
    assert second.is_venue_default is True

    after = await mixer.get_desk_scene(db, first.id)
    assert after is not None and after.is_venue_default is False
    assert (await mixer.get_venue_default(db, device.id)) == second


async def test_switching_the_venue_default_by_update_clears_the_previous_one(db: Database) -> None:
    device = await _mixer_device(db)
    first = await mixer.create_desk_scene(
        db, device_id=device.id, scene_ref="1", name="A", is_venue_default=True
    )
    second = await mixer.create_desk_scene(db, device_id=device.id, scene_ref="2", name="B")

    switched = await mixer.update_desk_scene(
        db, second.id, second.updated_at, is_venue_default=True
    )
    assert switched.is_venue_default is True

    after_first = await mixer.get_desk_scene(db, first.id)
    assert after_first is not None and after_first.is_venue_default is False
    assert (await mixer.get_venue_default(db, device.id)) == switched

    # Different devices designate their Venue Default independently.
    other_device = await _mixer_device(db, name="Second desk")
    assert await mixer.get_venue_default(db, other_device.id) is None
    other_default = await mixer.create_desk_scene(
        db, device_id=other_device.id, scene_ref="1", name="Its own default", is_venue_default=True
    )
    assert after_first.is_venue_default is False
    assert (await mixer.get_venue_default(db, other_device.id)) == other_default
    # The first device's designation is unaffected by the second device's.
    assert (await mixer.get_venue_default(db, device.id)) == switched


async def test_partial_unique_index_blocks_a_second_venue_default_directly(db: Database) -> None:
    """``idx_mixer_desk_scenes_one_venue_default`` itself, independent of the
    CRUD-level clearing :func:`~proskenion.db.crud.mixer.create_desk_scene`
    and :func:`~proskenion.db.crud.mixer.update_desk_scene` do — proves the
    invariant is a real schema constraint, not only application discipline,
    the same distinction :func:`test_channel_refs_database_constraint` and
    video's equivalent test draw for their own unique constraints."""
    device = await _mixer_device(db)
    await mixer.create_desk_scene(
        db, device_id=device.id, scene_ref="1", name="A", is_venue_default=True
    )
    second = await mixer.create_desk_scene(db, device_id=device.id, scene_ref="2", name="B")

    with pytest.raises(sqlite3.IntegrityError):
        async with db.write() as conn:
            await conn.execute(
                "UPDATE mixer_desk_scenes SET is_venue_default = 1 WHERE id = ?", (second.id,)
            )


async def test_no_venue_default_until_one_is_designated(db: Database) -> None:
    device = await _mixer_device(db)
    assert await mixer.get_venue_default(db, device.id) is None
    await mixer.create_desk_scene(db, device_id=device.id, scene_ref="1", name="Lecture")
    assert await mixer.get_venue_default(db, device.id) is None


async def test_deleting_a_desk_scene_used_by_a_scene_action_is_refused(db: Database) -> None:
    device = await _mixer_device(db)
    desk_scene = await mixer.create_desk_scene(
        db, device_id=device.id, scene_ref="3", name="Lecture"
    )
    scene = await scenes.create_scene(db, name="Restore Venue Default")
    await scenes.create_action(
        db, scene_id=scene.id, sort_order=0, domain="mixer_recall", mixer_scene_id=desk_scene.id
    )

    with pytest.raises(InUseError) as excinfo:
        await mixer.delete_desk_scene(db, desk_scene.id)
    assert [(r.entity, r.name) for r in excinfo.value.references] == [("scene_actions", scene.name)]
    assert await mixer.get_desk_scene(db, desk_scene.id) is not None


async def test_deleting_a_device_cascades_to_desk_scenes(db: Database) -> None:
    device = await _mixer_device(db)
    desk_scene = await mixer.create_desk_scene(
        db, device_id=device.id, scene_ref="3", name="Lecture"
    )

    await devices.delete(db, device.id)

    assert await mixer.get_desk_scene(db, desk_scene.id) is None
