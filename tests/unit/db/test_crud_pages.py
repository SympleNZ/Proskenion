"""``pages``, ``page_items``, ``page_buttons`` and ``hirer_pages`` (§15.12,
§21.9, §16.1, §22.2, migration 006)."""

import pytest

from proskenion.db.connection import Database
from proskenion.db.crud import devices, knx, lighting, mixer, pages, rules, scenes
from proskenion.db.crud.base import ConflictError, NotFoundError
from proskenion.db.crud.pages import (
    DefaultPageError,
    PageButtonInput,
    PageItemInput,
)
from proskenion.db.crud.refs import ConstraintError, InUseError


async def _rule(db: Database, name: str = "Panel button") -> int:
    scene = await scenes.create_scene(db, name=f"{name} scene")
    rule = await rules.create_rule(
        db, name=name, trigger_type="surface", action_type="run_scene", scene_id=scene.id
    )
    return rule.id


async def _mixer_device(db: Database) -> int:
    device = await devices.create(db, category="mixer", driver_key="cq20b", name="Desk", config={})
    return device.id


# -- create, list, get ------------------------------------------------------------


async def test_create_page_starts_empty(db: Database) -> None:
    page = await pages.create_page(db, name="Performance")
    assert page.name == "Performance" and not page.is_default
    fetched = await pages.get_page(db, page.id)
    assert fetched is not None
    assert fetched.page == page
    assert fetched.items == ()


async def test_list_pages_orders_by_sort_order(db: Database) -> None:
    b = await pages.create_page(db, name="B", sort_order=1)
    a = await pages.create_page(db, name="A", sort_order=0)
    listed = await pages.list_pages(db)
    ids = [p.id for p in listed]
    assert ids.index(a.id) < ids.index(b.id)


async def test_get_page_missing_returns_none(db: Database) -> None:
    assert await pages.get_page(db, 999999) is None


# -- whole-page replace -------------------------------------------------------------


async def test_replace_page_writes_items_and_buttons_in_sort_order(db: Database) -> None:
    device_id = await _mixer_device(db)
    channel = await mixer.create_channel(db, device_id=device_id, name="Wireless 1")
    group = await lighting.create_group(db, name="Wash")
    rule_id = await _rule(db)

    page = await pages.create_page(db, name="Performance")
    items = [
        PageItemInput(kind="channel", channel_id=channel.id),
        PageItemInput(kind="group_master", group_id=group.id, expanded=True),
        PageItemInput(
            kind="panel",
            panel_title="Room",
            panel_width=3,
            buttons=(
                PageButtonInput(col=0, row=0, label="House up", rule_id=rule_id),
                # Q5: row has no three-row limit.
                PageButtonInput(col=1, row=4, label="House down", rule_id=rule_id, confirm=True),
            ),
        ),
    ]
    replaced = await pages.replace_page(
        db, page.id, page.updated_at, name="Performance", sort_order=2, items=items
    )
    assert replaced.page.name == "Performance"
    assert replaced.page.sort_order == 2
    assert replaced.page.updated_at != page.updated_at

    kinds = [i.kind for i in replaced.items]
    assert kinds == ["channel", "group_master", "panel"]
    assert [i.sort_order for i in replaced.items] == [0, 1, 2]

    channel_item, group_item, panel_item = replaced.items
    assert channel_item.channel_id == channel.id
    assert group_item.group_id == group.id and group_item.expanded is True
    assert panel_item.panel_width == 3
    assert [(b.col, b.row, b.label) for b in panel_item.buttons] == [
        (0, 0, "House up"),
        (1, 4, "House down"),
    ]
    assert panel_item.buttons[1].confirm is True

    # Persisted, not just returned.
    reloaded = await pages.get_page(db, page.id)
    assert reloaded == replaced


async def test_replace_page_is_wholesale_not_a_merge(db: Database) -> None:
    device_id = await _mixer_device(db)
    channel = await mixer.create_channel(db, device_id=device_id, name="Wireless 1")
    page = await pages.create_page(db, name="P")
    once = await pages.replace_page(
        db,
        page.id,
        page.updated_at,
        name="P",
        sort_order=0,
        items=[PageItemInput(kind="channel", channel_id=channel.id)],
    )
    twice = await pages.replace_page(
        db, page.id, once.page.updated_at, name="P", sort_order=0, items=[]
    )
    assert twice.items == ()


async def test_replace_page_conflict_on_stale_version(db: Database) -> None:
    page = await pages.create_page(db, name="P")
    await pages.replace_page(db, page.id, page.updated_at, name="P renamed", sort_order=0, items=[])
    with pytest.raises(ConflictError):
        await pages.replace_page(db, page.id, page.updated_at, name="Stale", sort_order=0, items=[])


async def test_replace_page_not_found(db: Database) -> None:
    with pytest.raises(NotFoundError):
        await pages.replace_page(db, 999999, "x", name="P", sort_order=0, items=[])


async def test_replace_page_refuses_the_default_page(db: Database) -> None:
    default_page = await pages.regenerate_default_page(db)
    with pytest.raises(DefaultPageError):
        await pages.replace_page(
            db, default_page.page.id, default_page.page.updated_at, name="Hacked",
            sort_order=0, items=[],
        )


async def test_replace_page_rejects_a_malshaped_item(db: Database) -> None:
    device_id = await _mixer_device(db)
    channel = await mixer.create_channel(db, device_id=device_id, name="Wireless 1")
    address = await knx.create_address(
        db, group_address="6/1/1", name="House", dpt="1.001", direction="outgoing"
    )
    lighting_channel = await lighting.create_channel(
        db, name="House", type="knx_dimmer", knx_command_address_id=address.id
    )
    page = await pages.create_page(db, name="P")
    # A 'channel' item with both channel_id and lighting_channel_id set — both
    # real rows — trips page_items' CHECK (exactly one of the two), not a
    # foreign key.
    with pytest.raises(ConstraintError) as excinfo:
        await pages.replace_page(
            db,
            page.id,
            page.updated_at,
            name="P",
            sort_order=0,
            items=[
                PageItemInput(
                    kind="channel",
                    channel_id=channel.id,
                    lighting_channel_id=lighting_channel.id,
                )
            ],
        )
    assert excinfo.value.constraint == "page_items_shape"


async def test_replace_page_rejects_a_button_out_of_panel_width(db: Database) -> None:
    rule_id = await _rule(db)
    page = await pages.create_page(db, name="P")
    with pytest.raises(ValueError):
        await pages.replace_page(
            db,
            page.id,
            page.updated_at,
            name="P",
            sort_order=0,
            items=[
                PageItemInput(
                    kind="panel",
                    panel_title="Room",
                    panel_width=2,
                    buttons=(PageButtonInput(col=2, row=0, label="Off the edge", rule_id=rule_id),),
                )
            ],
        )


async def test_replace_page_rejects_a_duplicate_button_position(db: Database) -> None:
    rule_id = await _rule(db)
    page = await pages.create_page(db, name="P")
    with pytest.raises(ConstraintError) as excinfo:
        await pages.replace_page(
            db,
            page.id,
            page.updated_at,
            name="P",
            sort_order=0,
            items=[
                PageItemInput(
                    kind="panel",
                    panel_title="Room",
                    panel_width=2,
                    buttons=(
                        PageButtonInput(col=0, row=0, label="A", rule_id=rule_id),
                        PageButtonInput(col=0, row=0, label="B", rule_id=rule_id),
                    ),
                )
            ],
        )
    assert excinfo.value.constraint == "page_buttons_position_unique"


async def test_replace_page_rejects_an_unknown_rule(db: Database) -> None:
    page = await pages.create_page(db, name="P")
    with pytest.raises(ConstraintError) as excinfo:
        await pages.replace_page(
            db,
            page.id,
            page.updated_at,
            name="P",
            sort_order=0,
            items=[
                PageItemInput(
                    kind="panel",
                    panel_title="Room",
                    panel_width=1,
                    buttons=(PageButtonInput(col=0, row=0, label="Ghost", rule_id=999999),),
                )
            ],
        )
    assert excinfo.value.constraint == "page_reference"


# -- delete -------------------------------------------------------------------------


async def test_delete_page_cascades_items_and_buttons(db: Database) -> None:
    rule_id = await _rule(db)
    page = await pages.create_page(db, name="P")
    replaced = await pages.replace_page(
        db,
        page.id,
        page.updated_at,
        name="P",
        sort_order=0,
        items=[
            PageItemInput(
                kind="panel",
                panel_title="Room",
                panel_width=1,
                buttons=(PageButtonInput(col=0, row=0, label="A", rule_id=rule_id),),
            )
        ],
    )
    item_id = replaced.items[0].id
    button_id = replaced.items[0].buttons[0].id

    await pages.delete_page(db, page.id)
    assert await pages.get_page(db, page.id) is None
    async with db.read() as conn:
        item_row = await conn.execute("SELECT 1 FROM page_items WHERE id = ?", (item_id,))
        assert await item_row.fetchone() is None
        button_row = await conn.execute("SELECT 1 FROM page_buttons WHERE id = ?", (button_id,))
        assert await button_row.fetchone() is None


async def test_delete_page_refuses_the_default_page(db: Database) -> None:
    default_page = await pages.regenerate_default_page(db)
    with pytest.raises(DefaultPageError):
        await pages.delete_page(db, default_page.page.id)


async def test_delete_page_not_found(db: Database) -> None:
    with pytest.raises(NotFoundError):
        await pages.delete_page(db, 999999)


# -- hirer page assignment ------------------------------------------------------------


async def test_hirer_pages_round_trip_in_page_sort_order(db: Database) -> None:
    b = await pages.create_page(db, name="B", sort_order=1)
    a = await pages.create_page(db, name="A", sort_order=0)
    assigned = await pages.replace_hirer_pages(db, [b.id, a.id])
    assert set(assigned) == {a.id, b.id}
    assert await pages.list_hirer_page_ids(db) == [a.id, b.id]


async def test_hirer_pages_replace_is_wholesale(db: Database) -> None:
    a = await pages.create_page(db, name="A")
    b = await pages.create_page(db, name="B")
    await pages.replace_hirer_pages(db, [a.id])
    await pages.replace_hirer_pages(db, [b.id])
    assert await pages.list_hirer_page_ids(db) == [b.id]


async def test_hirer_pages_refuses_the_default_page(db: Database) -> None:
    default_page = await pages.regenerate_default_page(db)
    with pytest.raises(DefaultPageError):
        await pages.replace_hirer_pages(db, [default_page.page.id])


async def test_hirer_pages_refuses_an_unknown_page(db: Database) -> None:
    with pytest.raises(NotFoundError):
        await pages.replace_hirer_pages(db, [999999])


async def test_hirer_pages_refuses_a_duplicate(db: Database) -> None:
    page = await pages.create_page(db, name="P")
    with pytest.raises(ValueError):
        await pages.replace_hirer_pages(db, [page.id, page.id])


async def test_deleting_an_assigned_page_clears_its_hirer_pages_row(db: Database) -> None:
    page = await pages.create_page(db, name="P")
    await pages.replace_hirer_pages(db, [page.id])
    await pages.delete_page(db, page.id)
    assert await pages.list_hirer_page_ids(db) == []


# -- default page generation ---------------------------------------------------------


async def test_default_page_orders_main_then_outputs_then_inputs_then_groups(
    db: Database,
) -> None:
    device_id = await _mixer_device(db)
    wireless = await mixer.create_channel(
        db, device_id=device_id, name="Wireless 1", channel_kind="input", sort_order=2
    )
    foh = await mixer.create_channel(
        db, device_id=device_id, name="FOH", channel_kind="output", sort_order=1
    )
    main = await mixer.create_channel(
        db, device_id=device_id, name="Main", channel_kind="main", sort_order=0
    )
    group = await lighting.create_group(db, name="Wash")

    default_page = await pages.regenerate_default_page(db)
    assert default_page.page.is_default is True
    kinds_and_refs = [
        (i.kind, i.channel_id, i.group_id) for i in default_page.items
    ]
    assert kinds_and_refs == [
        ("channel", main.id, None),
        ("channel", foh.id, None),
        ("channel", wireless.id, None),
        ("group_master", None, group.id),
    ]


async def test_default_page_excludes_staff_invisible_channels(db: Database) -> None:
    device_id = await _mixer_device(db)
    await mixer.create_channel(
        db, device_id=device_id, name="Hidden", visible_staff=False
    )
    default_page = await pages.regenerate_default_page(db)
    assert default_page.items == ()


async def test_default_page_regeneration_is_idempotent_and_never_duplicated(
    db: Database,
) -> None:
    device_id = await _mixer_device(db)
    await mixer.create_channel(db, device_id=device_id, name="Wireless 1")
    first = await pages.regenerate_default_page(db)
    second = await pages.regenerate_default_page(db)
    assert first.page.id == second.page.id
    assert [(i.kind, i.channel_id, i.group_id) for i in first.items] == [
        (i.kind, i.channel_id, i.group_id) for i in second.items
    ]
    default_pages = [p for p in await pages.list_pages(db) if p.is_default]
    assert len(default_pages) == 1


async def test_default_page_is_never_assignable_even_by_id(db: Database) -> None:
    default_page = await pages.regenerate_default_page(db)
    other = await pages.create_page(db, name="Other")
    with pytest.raises(DefaultPageError):
        await pages.replace_hirer_pages(db, [other.id, default_page.page.id])
    # Nothing was assigned — the whole call is refused, not a partial write.
    assert await pages.list_hirer_page_ids(db) == []


# -- reference lists: a rule bound to a button, a derived status used as a lamp -------


async def test_rule_delete_is_blocked_by_a_page_button(db: Database) -> None:
    rule_id = await _rule(db, "Button rule")
    page = await pages.create_page(db, name="P")
    replaced = await pages.replace_page(
        db,
        page.id,
        page.updated_at,
        name="P",
        sort_order=0,
        items=[
            PageItemInput(
                kind="panel",
                panel_title="Room",
                panel_width=1,
                buttons=(PageButtonInput(col=0, row=0, label="Go", rule_id=rule_id),),
            )
        ],
    )
    button_id = replaced.items[0].buttons[0].id

    with pytest.raises(InUseError) as excinfo:
        await rules.delete_rule(db, rule_id)
    refs = await rules.references_rule(db, rule_id)
    assert [(r.entity, r.id, r.name) for r in refs] == [("page_buttons", button_id, "Go")]
    assert [(r.entity, r.id, r.name) for r in excinfo.value.references] == [
        ("page_buttons", button_id, "Go")
    ]

    # Once the button is gone, the rule deletes cleanly.
    await pages.delete_page(db, page.id)
    await rules.delete_rule(db, rule_id)
    assert await rules.get_rule(db, rule_id) is None


async def test_derived_status_delete_is_not_blocked_but_is_reported(db: Database) -> None:
    address = await knx.create_address(
        db, group_address="5/1/1", name="House", dpt="1.001", direction="outgoing"
    )
    status = await rules.create_derived_status(
        db, name="House", knx_address_id=address.id, source_type="device_state"
    )
    rule_id = await _rule(db, "Lamped button rule")
    page = await pages.create_page(db, name="P")
    replaced = await pages.replace_page(
        db,
        page.id,
        page.updated_at,
        name="P",
        sort_order=0,
        items=[
            PageItemInput(
                kind="panel",
                panel_title="Room",
                panel_width=1,
                buttons=(
                    PageButtonInput(
                        col=0, row=0, label="House up", rule_id=rule_id, state_id=status.id
                    ),
                ),
            )
        ],
    )
    button_id = replaced.items[0].buttons[0].id

    refs = await rules.references_derived_status(db, status.id)
    assert [(r.entity, r.id, r.name) for r in refs] == [("page_buttons", button_id, "House up")]

    await rules.delete_derived_status(db, status.id)
    assert await rules.get_derived_status(db, status.id) is None

    reloaded = await pages.get_page(db, page.id)
    assert reloaded is not None
    assert reloaded.items[0].buttons[0].state_id is None


async def test_derived_status_accepts_a_null_address(db: Database) -> None:
    status = await rules.create_derived_status(
        db, name="House at 100%", source_type="device_state"
    )
    assert status.knx_address_id is None
    reloaded = await rules.get_derived_status(db, status.id)
    assert reloaded is not None and reloaded.knx_address_id is None


async def test_derived_status_null_address_is_still_unique_only_among_non_null(
    db: Database,
) -> None:
    first = await rules.create_derived_status(db, name="A", source_type="device_state")
    second = await rules.create_derived_status(db, name="B", source_type="device_state")
    assert first.knx_address_id is None and second.knx_address_id is None


# -- mixer_desk_scene_observed (Phase 5 contracts, "GET /hirer/conflicts") ------------


async def test_observed_levels_round_trip_and_replace_wholesale(db: Database) -> None:
    device_id = await _mixer_device(db)
    channel_a = await mixer.create_channel(db, device_id=device_id, name="A")
    channel_b = await mixer.create_channel(db, device_id=device_id, name="B")
    desk_scene = await mixer.create_desk_scene(db, device_id=device_id, scene_ref="1", name="Band")

    assert await mixer.get_observed_levels(db, desk_scene.id) == []

    first = await mixer.replace_observed_levels(
        db, desk_scene.id, {channel_a.id: -6.0, channel_b.id: None}
    )
    by_channel = {level.channel_id: level for level in first}
    assert by_channel[channel_a.id].db == -6.0
    assert by_channel[channel_b.id].db is None

    second = await mixer.replace_observed_levels(db, desk_scene.id, {channel_a.id: -3.0})
    assert [level.channel_id for level in second] == [channel_a.id]
    assert second[0].db == -3.0

    fetched = await mixer.get_observed_levels(db, desk_scene.id)
    assert fetched == second


async def test_observed_levels_not_found_desk_scene(db: Database) -> None:
    with pytest.raises(NotFoundError):
        await mixer.replace_observed_levels(db, 999999, {})


async def test_observed_levels_cascade_on_desk_scene_delete(db: Database) -> None:
    device_id = await _mixer_device(db)
    channel = await mixer.create_channel(db, device_id=device_id, name="A")
    desk_scene = await mixer.create_desk_scene(db, device_id=device_id, scene_ref="1", name="Band")
    await mixer.replace_observed_levels(db, desk_scene.id, {channel.id: -6.0})
    await mixer.delete_desk_scene(db, desk_scene.id)
    async with db.read() as conn:
        row = await conn.execute(
            "SELECT COUNT(*) FROM mixer_desk_scene_observed WHERE desk_scene_id = ?",
            (desk_scene.id,),
        )
        count = await row.fetchone()
    assert count is not None and count[0] == 0


async def test_observed_levels_cascade_on_channel_delete(db: Database) -> None:
    device_id = await _mixer_device(db)
    channel = await mixer.create_channel(db, device_id=device_id, name="A")
    desk_scene = await mixer.create_desk_scene(db, device_id=device_id, scene_ref="1", name="Band")
    await mixer.replace_observed_levels(db, desk_scene.id, {channel.id: -6.0})
    await mixer.delete_channel(db, channel.id)
    async with db.read() as conn:
        row = await conn.execute(
            "SELECT COUNT(*) FROM mixer_desk_scene_observed WHERE channel_id = ?",
            (channel.id,),
        )
        count = await row.fetchone()
    assert count is not None and count[0] == 0
