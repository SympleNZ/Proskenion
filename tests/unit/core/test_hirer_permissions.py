"""The hirer permission resolver and the hirer's filtered view of the socket.

Spec §15.4 (pages decide what, ceilings how far), §6.7 (enforcement reads a
cache rebuilt on an explicit invalidation event; the live-effect table),
§5.6 and B39 (one writer of ``state.hirer``), §16.8 (the frames) and §21.9
(group trays). The phase-5 plan fixes Q2 (scenes and desk scenes derived
from buttons), Q3 (the three switches) and Q4 as amended (inputs, and Main
when it is on an assigned page; never another output), and the phase-5
contracts fix the snapshot's interface and the hirer frame shapes.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from proskenion.config import Config
from proskenion.core import hirer_permissions
from proskenion.core.auth import hash_secret
from proskenion.core.broadcast import RESYNC_SOURCE, Broadcaster, Connection, Message
from proskenion.core.bus import EventBus
from proskenion.core.events import (
    Event,
    HirerConfigChanged,
    HirerPermissionsChanged,
    LightingConfigChanged,
    MixerConfigChanged,
    PagesChanged,
    RulesConfigChanged,
    SceneConfigChanged,
)
from proskenion.core.hirer_access import ACCESS_SIGNAL_FILENAME, OWNER, HirerAccess
from proskenion.core.hirer_permissions import (
    NO_PERMISSIONS,
    HirerConfiguration,
    HirerPermissionResolver,
    HirerPermissions,
    diff,
    resolve,
)
from proskenion.core.state import OwnershipError, StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import hirer as hirer_crud
from proskenion.db.crud import knx as knx_crud
from proskenion.db.crud import lighting as lighting_crud
from proskenion.db.crud import mixer as mixer_crud
from proskenion.db.crud import pages as pages_crud
from proskenion.db.crud import rules as rules_crud
from proskenion.db.crud import scenes as scenes_crud
from proskenion.db.crud.mixer import MixerChannel
from proskenion.db.crud.pages import (
    Page,
    PageButton,
    PageButtonInput,
    PageItem,
    PageItemInput,
    PageWithItems,
)

WAIT_S = 5.0
REAL_PIN = hash_secret("246810", rounds=4)
STAMP = "2026-09-19T19:00:00+12:00"


async def until(condition: Callable[[], bool]) -> None:
    """Wait for ``condition`` against a deadline — never a fixed sleep."""
    async with asyncio.timeout(WAIT_S):
        while not condition():  # noqa: ASYNC110 - conditions span bus consumer tasks
            await asyncio.sleep(0.001)


async def drain(connection: Connection) -> list[Message]:
    """Everything queued for ``connection`` right now. Queueing is synchronous."""
    count = connection.queued
    messages: list[Message] = []
    if count == 0:
        return messages
    async for message in connection.messages():
        messages.append(message)
        if len(messages) == count:
            break
    return messages


# -- building configurations for the pure rules ------------------------------------


def _page(page_id: int, *items: PageItem, default: bool = False) -> PageWithItems:
    page = Page(page_id, f"Page {page_id}", page_id, default, STAMP, STAMP)
    return PageWithItems(page=page, items=tuple(replace(i, page_id=page_id) for i in items))


def _item(kind: str, **values: Any) -> PageItem:
    fields: dict[str, Any] = {
        "id": 0,
        "page_id": 0,
        "sort_order": 0,
        "kind": kind,
        "channel_id": None,
        "lighting_channel_id": None,
        "group_id": None,
        "expanded": False,
        "panel_title": None,
        "panel_width": None,
    }
    fields.update(values)
    return PageItem(**fields)


def mixer_item(channel_id: int) -> PageItem:
    return _item("channel", channel_id=channel_id)


def light_item(channel_id: int) -> PageItem:
    return _item("channel", lighting_channel_id=channel_id)


def group_item(group_id: int) -> PageItem:
    return _item("group_master", group_id=group_id)


def panel(*buttons: tuple[int, int, int | None]) -> PageItem:
    """``buttons`` as ``(button_id, rule_id, state_id)``."""
    return _item(
        "panel",
        panel_title="Room",
        panel_width=4,
        buttons=tuple(
            PageButton(bid, 0, n % 4, n // 4, f"B{bid}", rule_id, state_id, None, False)
            for n, (bid, rule_id, state_id) in enumerate(buttons)
        ),
    )


def channel(channel_id: int, kind: str = "input", ceiling: float | None = None) -> MixerChannel:
    return MixerChannel(
        id=channel_id,
        device_id=1,
        channel_kind=kind,
        name=f"Ch {channel_id}",
        short_name=None,
        notes=None,
        unmapped=False,
        visible_staff=True,
        hirer_max_db=ceiling,
        show_pan=False,
        tracked=True,
        sort_order=channel_id,
        created_at=STAMP,
        updated_at=STAMP,
    )


#: 1-4 inputs (3 with a ceiling), 5 Main, 6 an output, 7 an FX return.
CHANNELS: Mapping[int, MixerChannel] = {
    1: channel(1, ceiling=-6.0),
    2: channel(2),
    3: channel(3, ceiling=-3.0),
    4: channel(4),
    5: channel(5, "main", ceiling=0.0),
    6: channel(6, "output"),
    7: channel(7, "fx_return"),
}
#: Group 20 holds 101-103, group 21 holds 103-104, group 22 holds 105.
GROUPS: Mapping[int, tuple[int, ...]] = {20: (101, 102, 103), 21: (103, 104), 22: (105,)}


def config(
    *pages: PageWithItems,
    lighting: bool = True,
    individual: bool = True,
    colour: bool = True,
    rule_scenes: Mapping[int, int] | None = None,
    scene_recalls: Mapping[int, tuple[tuple[int | None, int | None], ...]] | None = None,
    venue_defaults: Mapping[int, int] | None = None,
) -> HirerConfiguration:
    return HirerConfiguration(
        pages=pages,
        lighting_enabled=lighting,
        individual_fixtures=individual,
        colour_enabled=colour,
        mixer_channels=CHANNELS,
        group_members=GROUPS,
        rule_scenes=rule_scenes or {},
        scene_recalls=scene_recalls or {},
        venue_defaults=venue_defaults or {},
    )


# -- the snapshot's rules ------------------------------------------------------------


def test_the_default_snapshot_reaches_nothing() -> None:
    assert NO_PERMISSIONS.enabled is False
    assert NO_PERMISSIONS.pages == ()
    assert not NO_PERMISSIONS.mixer_reachable(1)
    assert not NO_PERMISSIONS.lighting_reachable(101)
    assert not NO_PERMISSIONS.group_reachable(20)
    assert NO_PERMISSIONS.lamp_ids == frozenset()


def test_inputs_on_an_assigned_page_are_reachable_with_their_ceilings() -> None:
    p = resolve(config(_page(1, mixer_item(1), mixer_item(2))))
    assert p.mixer_reachable(1) and p.mixer_reachable(2)
    assert not p.mixer_reachable(3)
    assert p.ceiling_db(1) == -6.0
    assert p.ceiling_db(2) is None  # no ceiling
    assert p.pages == (1,)


def test_main_is_reachable_only_when_on_an_assigned_page_and_its_ceiling_applies() -> None:
    """Q4 as amended."""
    without = resolve(config(_page(1, mixer_item(1))))
    assert not without.mixer_reachable(5) and not without.main_reachable

    with_main = resolve(config(_page(1, mixer_item(1), mixer_item(5))))
    assert with_main.mixer_reachable(5) and with_main.main_reachable
    assert with_main.ceiling_db(5) == 0.0


def test_no_other_output_is_ever_reachable_even_on_an_assigned_page() -> None:
    """Q4: an output, or anything that is neither an input nor Main, never."""
    p = resolve(config(_page(1, mixer_item(6), mixer_item(7), mixer_item(1))))
    assert not p.mixer_reachable(6)
    assert not p.mixer_reachable(7)
    assert p.mixer_channels == frozenset({1})


def test_a_ceiling_applies_whichever_page_reached_the_channel() -> None:
    p = resolve(config(_page(1, mixer_item(3)), _page(2, mixer_item(3))))
    assert p.ceiling_db(3) == -3.0
    assert p.pages == (1, 2)


def test_the_default_page_is_never_reachable() -> None:
    p = resolve(
        config(
            _page(9, mixer_item(1), light_item(101), panel((40, 7, 4)), default=True),
            _page(1, mixer_item(2)),
        )
    )
    assert p.pages == (1,)
    assert not p.mixer_reachable(1)
    assert not p.lighting_reachable(101)
    assert not p.button_reachable(9, 40)
    assert p.lamp_ids == frozenset()


def test_lighting_off_reaches_nothing_lighting() -> None:
    """Q3: lighting off means no channel and no group, placed or not."""
    p = resolve(config(_page(1, light_item(101), group_item(21), mixer_item(1)), lighting=False))
    assert p.lighting_channels == frozenset()
    assert not p.lighting_reachable(101)
    assert not p.lighting_writable(101)
    assert not p.group_reachable(21)
    # Mixer reach is untouched by the lighting switch.
    assert p.mixer_reachable(1)


def test_a_group_master_reaches_its_members_and_they_are_writable_with_individual_fixtures() -> (
    None
):
    p = resolve(config(_page(1, group_item(20)), individual=True))
    assert p.group_reachable(20)
    assert {c for c in (101, 102, 103, 104) if p.lighting_reachable(c)} == {101, 102, 103}
    assert all(p.lighting_writable(c) for c in (101, 102, 103))


def test_individual_fixtures_off_leaves_a_trays_members_readable_but_not_writable() -> None:
    """Q3: the tray is shown read-only and only the master moves."""
    p = resolve(config(_page(1, group_item(20), light_item(105)), individual=False))
    assert p.group_reachable(20)
    assert all(p.lighting_reachable(c) for c in (101, 102, 103))
    assert not any(p.lighting_writable(c) for c in (101, 102, 103))
    # A channel placed directly stays writable.
    assert p.lighting_reachable(105) and p.lighting_writable(105)


def test_a_channel_placed_directly_and_in_a_placed_group_is_writable() -> None:
    p = resolve(config(_page(1, group_item(20), light_item(101)), individual=False))
    assert p.lighting_writable(101)
    assert not p.lighting_writable(102)


def test_a_lighting_channel_alone_is_not_its_groups_master() -> None:
    p = resolve(config(_page(1, light_item(103))))
    assert p.lighting_reachable(103)
    assert not p.group_reachable(20) and not p.group_reachable(21)


def test_colour_follows_colour_enabled() -> None:
    assert resolve(config(_page(1, light_item(101)), colour=True)).colour_allowed
    assert not resolve(config(_page(1, light_item(101)), colour=False)).colour_allowed


def test_buttons_and_their_lamps_are_reachable_on_their_own_page_only() -> None:
    p = resolve(config(_page(1, panel((40, 7, 4), (41, 8, None))), _page(2, mixer_item(1))))
    assert p.button_reachable(1, 40) and p.button_reachable(1, 41)
    assert not p.button_reachable(2, 40)
    assert not p.button_reachable(1, 99)
    assert p.lamp_ids == frozenset({4})
    assert p.rules == frozenset({7, 8})


def test_buttons_stay_reachable_with_lighting_off() -> None:
    """A button fires a rule, which bypasses the permission model by design (§15.4)."""
    p = resolve(config(_page(1, panel((40, 7, 4))), lighting=False))
    assert p.button_reachable(1, 40)
    assert p.lamp_ids == frozenset({4})


def test_scenes_and_desk_scenes_derive_from_buttons_through_rules() -> None:
    """Q2: button → rule → scene → mixer_recall."""
    p = resolve(
        config(
            _page(1, panel((40, 7, None), (41, 8, None), (42, 9, None))),
            # 7 runs scene 70; 8 runs scene 80; 9 is a lighting_group rule (no scene).
            rule_scenes={7: 70, 8: 80, 10: 100},
            scene_recalls={70: ((300, 1),), 80: ((301, None), (302, 1)), 100: ((303, 1),)},
        )
    )
    assert p.scenes == frozenset({70, 80})
    assert p.desk_scenes == frozenset({300, 301, 302})


def test_a_null_mixer_recall_counts_as_the_devices_venue_default() -> None:
    """Q2: "Restore Venue Default" (§13.5) is a desk scene a hirer's button reaches."""
    device_named = resolve(
        config(
            _page(1, panel((40, 7, None))),
            rule_scenes={7: 70},
            scene_recalls={70: ((None, 1),)},
            venue_defaults={1: 310, 2: 320},
        )
    )
    assert device_named.desk_scenes == frozenset({310})

    only_device = resolve(
        config(
            _page(1, panel((40, 7, None))),
            rule_scenes={7: 70},
            scene_recalls={70: ((None, None),)},
            venue_defaults={1: 310},
        )
    )
    assert only_device.desk_scenes == frozenset({310})

    no_default = resolve(
        config(
            _page(1, panel((40, 7, None))),
            rule_scenes={7: 70},
            scene_recalls={70: ((None, 1),)},
        )
    )
    assert no_default.desk_scenes == frozenset()


def test_the_snapshot_is_immutable() -> None:
    p = resolve(config(_page(1, mixer_item(1))))
    with pytest.raises(AttributeError):
        p.pages = (2,)  # type: ignore[misc]
    with pytest.raises(TypeError):
        p.mixer_ceilings[1] = 10.0  # type: ignore[index]


# -- the diff ------------------------------------------------------------------------


def test_the_diff_reports_removed_reach_lowered_ceilings_and_disabling() -> None:
    before = replace(
        resolve(config(_page(1, mixer_item(1), mixer_item(2), mixer_item(3), light_item(101)))),
        enabled=True,
    )
    lowered_one = {**CHANNELS, 3: channel(3, ceiling=-12.0), 2: channel(2, ceiling=-1.0)}
    after_config = replace(
        config(_page(1, mixer_item(2), mixer_item(3), mixer_item(4), mixer_item(5))),
        mixer_channels=lowered_one,
    )
    after = resolve(after_config)  # enabled defaults to False

    change = diff(before, after)
    assert change is not None
    assert change.removed_mixer == frozenset({1})
    assert change.removed_lighting == frozenset({101})
    # 3 lowered; 2 gained a ceiling where it had none; 5 is newly reachable with
    # one. 4 is newly reachable with none, so it holds a hirer to nothing.
    assert dict(change.lowered_ceilings) == {2: -1.0, 3: -12.0, 5: 0.0}
    assert change.disabled is True


def test_a_raised_ceiling_is_not_lowered() -> None:
    before = resolve(config(_page(1, mixer_item(3))))
    after = resolve(
        replace(config(_page(1, mixer_item(3))), mixer_channels={3: channel(3, ceiling=0.0)})
    )
    assert diff(before, after) is None


def test_a_change_that_only_adds_reach_has_no_diff() -> None:
    before = resolve(config(_page(1, mixer_item(2))))
    after = resolve(config(_page(1, mixer_item(2), mixer_item(4), light_item(101))))
    assert diff(before, after) is None


def test_enabling_is_not_disabling() -> None:
    before = NO_PERMISSIONS
    assert diff(before, replace(before, enabled=True)) is None


# -- the database-backed resolver ------------------------------------------------------


@dataclass
class World:
    db: Database
    bus: EventBus
    state: StateStore
    broadcaster: Broadcaster
    access: HirerAccess
    resolver: HirerPermissionResolver
    ids: dict[str, int]


async def _seed(db: Database) -> dict[str, int]:
    """A desk, lighting, a scene that restores the Venue Default, and one assigned page."""
    ids: dict[str, int] = {}
    desk = await devices_crud.create(
        db, category="mixer", driver_key="stub", name="Desk", config={}
    )
    ids["desk"] = desk.id
    for key, kind, ceiling in (
        ("in1", "input", -6.0),
        ("in2", "input", None),
        ("main", "main", 0.0),
        ("out1", "output", None),
    ):
        created = await mixer_crud.create_channel(
            db, device_id=desk.id, channel_kind=kind, name=key, hirer_max_db=ceiling
        )
        ids[key] = created.id
    for n, key in enumerate(("l1", "l2", "l3"), start=1):
        address = await knx_crud.create_address(
            db, group_address=f"1/0/{n}", name=key, dpt="5.001", direction="outgoing"
        )
        light = await lighting_crud.create_channel(
            db, name=key, type="knx_dimmer", knx_command_address_id=address.id
        )
        ids[key] = light.id
    wash = await lighting_crud.create_group(db, name="Wash")
    ids["wash"] = wash.id
    await lighting_crud.set_group_members(db, wash.id, [ids["l1"], ids["l2"]])
    default = await mixer_crud.create_desk_scene(
        db, device_id=desk.id, scene_ref="1", name="Venue Default", is_venue_default=True
    )
    band = await mixer_crud.create_desk_scene(db, device_id=desk.id, scene_ref="2", name="Band")
    ids["default_scene"], ids["band_scene"] = default.id, band.id
    restore = await scenes_crud.create_scene(db, name="Restore")
    await scenes_crud.create_action(db, scene_id=restore.id, sort_order=0, domain="mixer_recall")
    ids["restore"] = restore.id
    other = await scenes_crud.create_scene(db, name="Other")
    ids["other"] = other.id
    rule = await rules_crud.create_rule(
        db, name="Reset", trigger_type="surface", action_type="run_scene", scene_id=restore.id
    )
    ids["rule"] = rule.id
    lamp = await rules_crud.create_derived_status(db, name="House", source_type="device_state")
    ids["lamp"] = lamp.id
    page = await pages_crud.create_page(db, name="Performance", sort_order=1)
    ids["page"] = page.id
    await _set_items(
        db,
        page.id,
        [
            PageItemInput(kind="channel", channel_id=ids["in1"]),
            PageItemInput(kind="channel", channel_id=ids["main"]),
            PageItemInput(kind="channel", channel_id=ids["out1"]),
            PageItemInput(kind="group_master", group_id=wash.id),
            PageItemInput(
                kind="panel",
                panel_title="Room",
                panel_width=2,
                buttons=(
                    PageButtonInput(col=0, row=0, label="Reset", rule_id=rule.id, state_id=lamp.id),
                ),
            ),
        ],
    )
    await pages_crud.replace_hirer_pages(db, [page.id])
    config_row = await hirer_crud.get(db)
    await hirer_crud.update(
        db,
        {"lighting_enabled": True, "individual_fixtures": False, "colour_enabled": True},
        expected_updated_at=config_row.updated_at,
        updated_by=None,
    )
    return ids


async def _set_items(db: Database, page_id: int, items: list[PageItemInput]) -> None:
    page = await pages_crud.get_page(db, page_id)
    assert page is not None
    await pages_crud.replace_page(
        db,
        page_id,
        page.page.updated_at,
        name=page.page.name,
        sort_order=page.page.sort_order,
        items=items,
    )


def _button_id(page: PageWithItems) -> int:
    return next(b.id for item in page.items for b in item.buttons)


@pytest.fixture
async def world(db: Database, dev_config: Config, tmp_path: Path) -> AsyncIterator[World]:
    ids = await _seed(db)
    page = await pages_crud.get_page(db, ids["page"])
    assert page is not None
    ids["button"] = _button_id(page)
    bus = EventBus()
    await bus.start()
    state = StateStore(dev_config, bus)
    broadcaster = Broadcaster(state, bus)
    await broadcaster.start()
    access = HirerAccess(state, broadcaster, signal_path=tmp_path / ACCESS_SIGNAL_FILENAME)
    await access.load(db)
    resolver = HirerPermissionResolver(access, bus, broadcaster)
    await resolver.start(db)
    try:
        yield World(db, bus, state, broadcaster, access, resolver, ids)
    finally:
        await resolver.stop()
        await broadcaster.stop()
        await bus.stop()


async def test_the_resolver_builds_the_snapshot_from_the_database(world: World) -> None:
    p = world.state.hirer.permissions
    ids = world.ids
    assert p is world.resolver.permissions is world.access.permissions
    assert p.pages == (ids["page"],)
    assert p.mixer_channels == frozenset({ids["in1"], ids["main"]})
    assert not p.mixer_reachable(ids["out1"])
    assert p.ceiling_db(ids["in1"]) == -6.0 and p.ceiling_db(ids["main"]) == 0.0
    assert p.main_reachable
    assert p.lighting_channels == frozenset({ids["l1"], ids["l2"]})
    assert p.group_reachable(ids["wash"])
    assert not p.lighting_writable(ids["l1"])  # individual fixtures off
    assert p.button_reachable(ids["page"], ids["button"])
    assert p.lamp_ids == frozenset({ids["lamp"]})
    assert p.scenes == frozenset({ids["restore"]})
    # The scene's recall has no desk scene: Restore Venue Default (§13.5).
    assert p.desk_scenes == frozenset({ids["default_scene"]})


async def test_the_state_mirror_is_written_in_the_same_batch(world: World) -> None:
    ids = world.ids
    assert world.state.hirer.get("pages") == [ids["page"]]
    assert world.state.hirer.get("permitted_channels") == {
        str(ids["in1"]): -6.0,
        str(ids["main"]): 0.0,
    }


async def test_the_resolver_never_writes_state_hirer_itself(world: World) -> None:
    """B39: one owner, HirerAccess; a second writer is refused in development."""
    assert world.state.owners("hirer") == frozenset({OWNER})
    with pytest.raises(OwnershipError):
        world.state.hirer.writer("hirer_permissions")


async def test_the_snapshot_carries_the_access_hirer_access_publishes(world: World) -> None:
    await hirer_crud.set_pin_hash(world.db, REAL_PIN, updated_by=None)
    await world.access.set_enabled(world.db, True, actor="admin", ip_address=None)
    p = world.state.hirer.permissions
    assert p.enabled is True
    assert p.token_version == world.access.token_version
    # Reach survived the access change: it was stamped onto the same snapshot.
    assert p.mixer_reachable(world.ids["in1"])


async def _change_and_emit(world: World, event: Event) -> None:
    db, ids = world.db, world.ids
    match event:
        case HirerConfigChanged():
            row = await hirer_crud.get(db)
            await hirer_crud.update(
                db, {"lighting_enabled": False}, expected_updated_at=row.updated_at, updated_by=None
            )
        case PagesChanged():
            await _set_items(
                db, ids["page"], [PageItemInput(kind="channel", channel_id=ids["in2"])]
            )
        case MixerConfigChanged():
            current = await mixer_crud.get_channel(db, ids["in1"])
            assert current is not None
            await mixer_crud.update_channel(db, ids["in1"], current.updated_at, hirer_max_db=-20.0)
        case LightingConfigChanged():
            await lighting_crud.set_group_members(db, ids["wash"], [ids["l3"]])
        case RulesConfigChanged():
            rule = await rules_crud.get_rule(db, ids["rule"])
            assert rule is not None
            await rules_crud.update_rule(db, ids["rule"], rule.updated_at, scene_id=ids["other"])
        case SceneConfigChanged():
            await scenes_crud.create_action(
                db,
                scene_id=ids["restore"],
                sort_order=1,
                domain="mixer_recall",
                mixer_scene_id=ids["band_scene"],
            )
    world.bus.emit(event)


def _expectation(event: Event, ids: dict[str, int]) -> Callable[[HirerPermissions], bool]:
    match event:
        case HirerConfigChanged():
            return lambda p: not p.lighting_enabled and p.lighting_channels == frozenset()
        case PagesChanged():
            return lambda p: p.mixer_channels == frozenset({ids["in2"]})
        case MixerConfigChanged():
            return lambda p: p.ceiling_db(ids["in1"]) == -20.0
        case LightingConfigChanged():
            return lambda p: p.lighting_channels == frozenset({ids["l3"]})
        case RulesConfigChanged():
            return lambda p: p.scenes == frozenset({ids["other"]}) and p.desk_scenes == frozenset()
        case SceneConfigChanged():
            return lambda p: p.desk_scenes == frozenset({ids["default_scene"], ids["band_scene"]})
    raise AssertionError(event)


@pytest.mark.parametrize(
    "event",
    [
        HirerConfigChanged(reason="test"),
        PagesChanged(reason="test"),
        MixerConfigChanged(reason="test"),
        LightingConfigChanged(reason="test"),
        RulesConfigChanged(reason="test"),
        SceneConfigChanged(reason="test"),
    ],
    ids=lambda e: e.TYPE,
)
async def test_each_configuration_event_rebuilds_and_swaps_the_snapshot_whole(
    world: World, event: Event
) -> None:
    before = world.state.hirer.permissions
    rebuilds = world.resolver.rebuilds
    await _change_and_emit(world, event)
    await until(lambda: world.resolver.rebuilds > rebuilds)

    after = world.state.hirer.permissions
    assert after is not before
    assert _expectation(event, world.ids)(after)
    # The old snapshot is untouched: a reader holding it saw one whole state.
    assert before.mixer_channels == frozenset({world.ids["in1"], world.ids["main"]})
    assert world.state.hirer.get("permitted_channels") == after.permitted_channels()


async def test_a_failed_rebuild_keeps_the_snapshot_in_force(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = world.state.hirer.permissions

    async def broken(db: Database) -> HirerConfiguration:
        raise RuntimeError("database gone")

    monkeypatch.setattr(hirer_permissions, "load_configuration", broken)
    assert await world.resolver.rebuild() is before
    assert world.state.hirer.permissions is before


async def test_a_rebuild_never_carries_access_back_over_a_kill_switch(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The switch lands while a rebuild is reading; the published snapshot keeps it."""
    await hirer_crud.set_pin_hash(world.db, REAL_PIN, updated_by=None)
    await world.access.set_enabled(world.db, True, actor="admin", ip_address=None)
    reading = asyncio.Event()
    release = asyncio.Event()
    real = hirer_permissions.load_configuration

    async def slow(db: Database) -> HirerConfiguration:
        loaded = await real(db)
        reading.set()
        await release.wait()
        return loaded

    monkeypatch.setattr(hirer_permissions, "load_configuration", slow)
    rebuild = asyncio.create_task(world.resolver.rebuild())
    await reading.wait()
    await world.access.set_enabled(world.db, False, actor="admin", ip_address=None)
    release.set()
    published = await rebuild
    assert published.enabled is False
    assert world.state.hirer.permissions.enabled is False
    assert world.state.hirer.permissions.token_version == world.access.token_version


async def test_concurrent_rebuilds_are_serialised_and_the_last_reflects_the_last_write(
    world: World,
) -> None:
    await _set_items(world.db, world.ids["page"], [])
    first = asyncio.create_task(world.resolver.rebuild())
    await _set_items(
        world.db, world.ids["page"], [PageItemInput(kind="channel", channel_id=world.ids["in2"])]
    )
    second = asyncio.create_task(world.resolver.rebuild())
    await asyncio.gather(first, second)
    assert world.state.hirer.permissions.mixer_channels == frozenset({world.ids["in2"]})


async def test_the_change_event_carries_the_diff(world: World) -> None:
    received: list[HirerPermissionsChanged] = []

    async def record(event: HirerPermissionsChanged) -> None:
        received.append(event)

    world.resolver.on_change(record, name="test.recorder")
    ids = world.ids
    current = await mixer_crud.get_channel(world.db, ids["main"])
    assert current is not None
    await mixer_crud.update_channel(world.db, ids["main"], current.updated_at, hirer_max_db=-10.0)
    await _set_items(
        world.db,
        ids["page"],
        [
            PageItemInput(kind="channel", channel_id=ids["main"]),
            PageItemInput(kind="group_master", group_id=ids["wash"]),
        ],
    )
    await world.resolver.rebuild()
    await until(lambda: len(received) == 1)
    change = received[0]
    assert change.removed_mixer == frozenset({ids["in1"]})
    assert change.removed_lighting == frozenset()
    assert dict(change.lowered_ceilings) == {ids["main"]: -10.0}
    assert change.disabled is False

    await hirer_crud.set_pin_hash(world.db, REAL_PIN, updated_by=None)
    await world.access.set_enabled(world.db, True, actor="admin", ip_address=None)
    await world.access.set_enabled(world.db, False, actor="admin", ip_address=None)
    await until(lambda: len(received) == 2)
    assert received[1].disabled is True
    assert received[1].removed_mixer == frozenset()


async def test_deleting_an_assigned_page_rebuilds_to_nothing(world: World) -> None:
    await pages_crud.delete_page(world.db, world.ids["page"])
    world.bus.emit(PagesChanged(reason="deleted"))
    await until(lambda: world.state.hirer.permissions.pages == ())
    p = world.state.hirer.permissions
    assert p.mixer_channels == frozenset() and p.lighting_channels == frozenset()
    assert p.lamp_ids == frozenset() and p.desk_scenes == frozenset()


# -- per-connection filtering ---------------------------------------------------------


def _mixer_entry(db: float) -> dict[str, Any]:
    return {"db": db, "muted": False}


def _write_mixer(state: StateStore, ids: dict[str, int], db: float) -> None:
    writer = state.mixer.writer("mixer_service")
    writer.set("main", _mixer_entry(db))
    writer.set_item("inputs", ids["in1"], _mixer_entry(db))
    writer.set_item("inputs", ids["in2"], _mixer_entry(db))
    writer.set_item("outputs", ids["out1"], _mixer_entry(db))
    for key in ("in1", "in2", "main", "out1"):
        writer.set_item("meters", ids[key], [db])


def _write_lighting(state: StateStore, ids: dict[str, int], level: float) -> None:
    writer = state.lighting.writer("fade_engine")
    for key in ("l1", "l2", "l3"):
        writer.set_item("levels", ids[key], level)
        writer.set_item("colour", ids[key], {"r": 255, "g": 0, "b": 0})
    writer.set_item("binding_states", ids["rule"], True)
    writer.set("master", 80.0)


def _by_type(messages: list[Message]) -> dict[str, Message]:
    return {m["type"]: m for m in messages}


async def test_mixer_frames_carry_only_reachable_channels_and_staff_see_everything(
    world: World,
) -> None:
    ids = world.ids
    hirer = world.broadcaster.connect(tier="hirer", session_id="s", domains=["mixer"])
    staff = world.broadcaster.connect(tier="operator", domains=["mixer"])
    _write_mixer(world.state, ids, -5.0)
    world.broadcaster.tick()

    seen = _by_type(await drain(hirer))
    assert seen["mixer_state"]["inputs"] == {str(ids["in1"]): _mixer_entry(-5.0)}
    assert seen["mixer_state"]["main"] == _mixer_entry(-5.0)  # Main is on the page
    assert "outputs" not in seen["mixer_state"]
    assert set(seen["mixer_meters"]["channels"]) == {str(ids["in1"]), str(ids["main"])}

    everything = _by_type(await drain(staff))
    assert set(everything["mixer_state"]["inputs"]) == {str(ids["in1"]), str(ids["in2"])}
    assert set(everything["mixer_state"]["outputs"]) == {str(ids["out1"])}
    assert set(everything["mixer_meters"]["channels"]) == {
        str(ids[k]) for k in ("in1", "in2", "main", "out1")
    }


async def test_main_is_null_in_a_hirer_snapshot_unless_reachable(world: World) -> None:
    ids = world.ids
    _write_mixer(world.state, ids, -5.0)
    world.broadcaster.tick()
    await _set_items(world.db, ids["page"], [PageItemInput(kind="channel", channel_id=ids["in2"])])
    await world.resolver.rebuild()
    hirer = world.broadcaster.connect(tier="hirer", session_id="s", domains=["mixer"])
    snapshot = _by_type(world.broadcaster.snapshot(["mixer"], connection=hirer))
    assert snapshot["mixer_state"] == {
        "type": "mixer_state",
        "main": None,
        "outputs": {},
        "inputs": {str(ids["in2"]): _mixer_entry(-5.0)},
        "source": RESYNC_SOURCE,
    }
    # The fresh meter catch-up is filtered the same way: only in2 reachable.
    assert snapshot["mixer_meters"]["channels"] == {str(ids["in2"]): [-5.0]}
    # A partial frame that changes only Main is not sent at all.
    world.state.mixer.writer("mixer_service").set("main", _mixer_entry(-9.0))
    world.broadcaster.tick()
    assert await drain(hirer) == []


async def test_lighting_frames_carry_reachable_channels_and_master(
    world: World,
) -> None:
    ids = world.ids
    hirer = world.broadcaster.connect(tier="hirer", session_id="s", domains=["lighting"])
    staff = world.broadcaster.connect(tier="operator", domains=["lighting"])
    _write_lighting(world.state, ids, 40.0)
    world.broadcaster.tick()

    [frame] = await drain(hirer)
    assert set(frame["channels"]) == {str(ids["l1"]), str(ids["l2"])}
    assert frame["channels"][str(ids["l1"])] == {"level": 40.0, "r": 255, "g": 0, "b": 0}
    assert "groups" not in frame  # a group fader shows its members' levels
    assert frame["master"] == 80.0
    assert "bindings" not in frame

    [everything] = await drain(staff)
    assert set(everything["channels"]) == {str(ids[k]) for k in ("l1", "l2", "l3")}
    assert "groups" not in everything
    assert everything["bindings"] == {str(ids["rule"]): True}


async def test_a_channel_placed_alone_travels_without_its_groups_master(
    world: World,
) -> None:
    """A channel placed alone reaches the hirer; its group, unplaced, has no section."""
    ids = world.ids
    await _set_items(
        world.db, ids["page"], [PageItemInput(kind="channel", lighting_channel_id=ids["l1"])]
    )
    await world.resolver.rebuild()
    assert not world.state.hirer.permissions.group_reachable(ids["wash"])
    hirer = world.broadcaster.connect(tier="hirer", session_id="s", domains=["lighting"])
    _write_lighting(world.state, ids, 40.0)
    world.broadcaster.tick()
    [frame] = await drain(hirer)
    assert set(frame["channels"]) == {str(ids["l1"])}
    assert "groups" not in frame  # a group fader shows its members' levels


async def test_lighting_off_sends_a_hirer_no_lighting_at_all(world: World) -> None:
    row = await hirer_crud.get(world.db)
    await hirer_crud.update(
        world.db, {"lighting_enabled": False}, expected_updated_at=row.updated_at, updated_by=None
    )
    await world.resolver.rebuild()
    hirer = world.broadcaster.connect(tier="hirer", session_id="s", domains=["lighting"])
    _write_lighting(world.state, world.ids, 40.0)
    world.broadcaster.tick()
    assert await drain(hirer) == []
    assert world.broadcaster.snapshot(["lighting"], connection=hirer) == []


async def test_a_hirer_subscribe_is_silently_narrowed(world: World) -> None:
    hirer = world.broadcaster.connect(
        tier="hirer",
        session_id="s",
        domains=["mixer", "lighting", "devices", "system", "timer", "scenes", "projector", "hdmi"],
    )
    assert hirer.domains == frozenset({"mixer", "lighting", "devices"})


async def test_the_status_frame_is_limited_to_the_hirers_lamps(world: World) -> None:
    """``status`` is a real broadcast domain now; this still builds
    the frame by hand rather than driving the derived-status engine, since
    this suite is about the hirer filter, not lamp evaluation."""
    lamp = world.ids["lamp"]
    hirer = world.broadcaster.connect(tier="hirer", session_id="s")
    staff = world.broadcaster.connect(tier="operator")
    hirer.set_domains({"status"})
    staff.set_domains({"status"})
    lamps = {
        str(lamp): {"on": True, "transitioning": False},
        "9999": {"on": False, "transitioning": True},
    }
    world.broadcaster.publish({"type": "status", "lamps": lamps}, domain="status")
    assert await drain(hirer) == [
        {"type": "status", "lamps": {str(lamp): {"on": True, "transitioning": False}}}
    ]
    assert (await drain(staff))[0]["lamps"] == lamps

    world.broadcaster.publish(
        {"type": "status", "lamps": {"9999": {"on": True, "transitioning": False}}},
        domain="status",
    )
    assert await drain(hirer) == []


async def test_device_status_reaches_a_hirer_unchanged(world: World) -> None:
    hirer = world.broadcaster.connect(tier="hirer", session_id="s", domains=["devices"])
    world.state.devices.writer("probe").set_status("mixer", "error", kind="device")
    await until(lambda: hirer.queued > 0)
    assert await drain(hirer) == [{"type": "device_status", "device": "mixer", "status": "error"}]


async def test_an_unknown_tier_is_sent_nothing(world: World) -> None:
    stranger = world.broadcaster.connect(tier="guest", domains=["mixer", "devices"])
    assert stranger.domains == frozenset()
    stranger.set_domains({"mixer"})
    _write_mixer(world.state, world.ids, -1.0)
    world.broadcaster.tick()
    assert await drain(stranger) == []


# -- live effect: the next broadcast, with no re-login (§6.7) ---------------------------


async def test_a_removed_channel_stops_and_an_added_one_arrives_on_the_next_broadcast(
    world: World,
) -> None:
    ids = world.ids
    _write_mixer(world.state, ids, -5.0)
    world.broadcaster.tick()
    hirer = world.broadcaster.connect(tier="hirer", session_id="s", domains=["mixer", "devices"])
    rebuilds = world.resolver.rebuilds

    # The admin moves the page from input 1 to input 2.
    await _set_items(world.db, ids["page"], [PageItemInput(kind="channel", channel_id=ids["in2"])])
    world.bus.emit(PagesChanged(reason="edited"))
    await until(lambda: world.resolver.rebuilds > rebuilds)

    # A filtered resync of the affected domain, at once: the added channel's
    # value arrives, and the removed one is absent — its meter too, fresh
    # from live state rather than replayed (§16.8, B58).
    seen = _by_type(await drain(hirer))
    assert seen["mixer_state"] == {
        "type": "mixer_state",
        "main": None,
        "outputs": {},
        "inputs": {str(ids["in2"]): _mixer_entry(-5.0)},
        "source": RESYNC_SOURCE,
    }
    assert seen["mixer_meters"]["channels"] == {str(ids["in2"]): [-5.0]}

    writer = world.state.mixer.writer("mixer_service")
    writer.set_item("inputs", ids["in1"], _mixer_entry(-1.0))
    world.broadcaster.tick()
    assert await drain(hirer) == []

    writer.set_item("inputs", ids["in2"], _mixer_entry(-2.0))
    world.broadcaster.tick()
    [frame] = await drain(hirer)
    assert frame["inputs"] == {str(ids["in2"]): _mixer_entry(-2.0)}


async def test_the_resync_covers_only_the_domains_that_changed_and_skips_staff(
    world: World,
) -> None:
    ids = world.ids
    hirer = world.broadcaster.connect(
        tier="hirer", session_id="s", domains=["mixer", "lighting", "devices"]
    )
    staff = world.broadcaster.connect(tier="operator", domains=["mixer", "lighting"])
    backgrounded = world.broadcaster.connect(tier="hirer", session_id="t", domains=["lighting"])
    backgrounded.background()

    await lighting_crud.set_group_members(world.db, ids["wash"], [ids["l1"], ids["l2"], ids["l3"]])
    await world.resolver.rebuild()

    messages = await drain(hirer)
    assert [m["type"] for m in messages] == ["lighting_state"]
    assert set(messages[0]["channels"]) == set()  # nothing written yet; shape only
    assert await drain(staff) == []
    assert await drain(backgrounded) == []


async def test_a_rebuild_that_changes_nothing_sends_nothing(world: World) -> None:
    hirer = world.broadcaster.connect(tier="hirer", session_id="s", domains=["mixer", "lighting"])
    await world.resolver.rebuild()
    assert await drain(hirer) == []


async def test_an_assignment_change_tells_each_hirer_its_own_pages_after_the_swap(
    world: World,
) -> None:
    """A hirer whose assigned pages change is sent ``pages_changed`` naming
    the pages the new snapshot holds — never the ones it replaced — and
    staff are not sent this copy (theirs is the pages broadcaster's)."""
    hirer = world.broadcaster.connect(tier="hirer", session_id="s", domains=["devices"])
    staff = world.broadcaster.connect(tier="operator", domains=["devices"])
    await pages_crud.delete_page(world.db, world.ids["page"])
    await world.resolver.rebuild(reason="page_deleted")
    changed = [m for m in await drain(hirer) if m["type"] == "pages_changed"]
    assert changed == [{"type": "pages_changed", "page_ids": []}]
    assert [m for m in await drain(staff) if m["type"] == "pages_changed"] == []


# -- property: a hirer never receives an id outside its snapshot ----------------------


def _assert_within(message: Message, p: HirerPermissions) -> None:
    kind = message["type"]
    if kind == "mixer_state":
        for section in ("inputs", "outputs"):
            assert {int(k) for k in message.get(section, {})} <= p.mixer_channels, message
        if message.get("main") is not None:
            assert p.main_reachable, message
    elif kind == "mixer_meters":
        assert {int(k) for k in message["channels"]} <= p.mixer_channels, message
    elif kind == "lighting_state":
        assert p.lighting_enabled, message
        assert {int(k) for k in message.get("channels", {})} <= p.lighting_channels, message
        assert {int(k) for k in (message.get("observed") or {})} <= p.lighting_channels, message
        assert "groups" not in message, message
        assert "bindings" not in message, message
    elif kind == "status":
        assert {int(k) for k in message["lamps"]} <= p.lamp_ids, message
    else:
        assert kind in {"device_status", "external_control"}, message


def _carried_ids(message: Message) -> int:
    sections = ("inputs", "outputs", "channels", "observed", "lamps")
    return sum(len(message.get(section) or {}) for section in sections)


def _random_configuration(rng: random.Random) -> HirerConfiguration:
    pages: list[PageWithItems] = []
    for page_id in rng.sample(range(1, 6), rng.randint(0, 3)):
        items: list[PageItem] = []
        for _ in range(rng.randint(0, 6)):
            roll = rng.random()
            if roll < 0.35:
                items.append(mixer_item(rng.choice(list(CHANNELS))))
            elif roll < 0.6:
                items.append(light_item(rng.randint(101, 106)))
            elif roll < 0.8:
                items.append(group_item(rng.choice([*GROUPS, 23])))
            else:
                items.append(
                    panel(
                        *(
                            (rng.randint(1, 50), rng.randint(1, 5), rng.choice([None, 1, 2, 3]))
                            for _ in range(rng.randint(0, 3))
                        )
                    )
                )
        pages.append(_page(page_id, *items, default=rng.random() < 0.15))
    ceilings = {
        cid: replace(ch, hirer_max_db=rng.choice([None, -20.0, -6.0, 0.0]))
        for cid, ch in CHANNELS.items()
    }
    return replace(
        config(
            *pages,
            lighting=rng.random() < 0.7,
            individual=rng.random() < 0.5,
            colour=rng.random() < 0.5,
        ),
        mixer_channels=ceilings,
    )


def _random_state_change(rng: random.Random, state: StateStore, broadcaster: Broadcaster) -> None:
    mixer = state.mixer.writer("mixer_service")
    lighting = state.lighting.writer("fade_engine")
    db = round(rng.uniform(-60.0, 10.0), 1)
    roll = rng.random()
    if roll < 0.2:
        mixer.set_item("inputs", rng.choice([1, 2, 3, 4, 7]), _mixer_entry(db))
    elif roll < 0.3:
        mixer.set_item("outputs", 6, _mixer_entry(db))
    elif roll < 0.4:
        mixer.set("main", _mixer_entry(db))
    elif roll < 0.5:
        mixer.set_item("meters", rng.randint(1, 7), [db])
    elif roll < 0.65:
        lighting.set_item("levels", rng.randint(101, 106), round(rng.uniform(0, 100), 1))
    elif roll < 0.7:
        lighting.set_item("colour", rng.randint(101, 106), {"r": rng.randint(0, 255)})
    elif roll < 0.75:
        lighting.set_item("levels", rng.choice([101, 102]), round(rng.uniform(0, 100), 1))
    elif roll < 0.8:
        lighting.set_item("observed", rng.randint(101, 106), round(rng.uniform(0, 100), 1))
    elif roll < 0.85:
        lighting.set_item("binding_states", rng.randint(1, 5), rng.random() < 0.5)
    elif roll < 0.9:
        lighting.set("master", round(rng.uniform(0, 100), 1))
    else:
        lamps = {str(rng.randint(1, 4)): {"on": rng.random() < 0.5, "transitioning": False}}
        broadcaster.publish({"type": "status", "lamps": lamps}, domain="status")


@pytest.mark.parametrize("seed", range(12))
async def test_a_hirer_never_receives_an_id_outside_its_snapshot(
    seed: int, dev_config: Config, tmp_path: Path
) -> None:
    rng = random.Random(seed)
    bus = EventBus()
    await bus.start()
    state = StateStore(dev_config, bus)
    broadcaster = Broadcaster(state, bus)
    access = HirerAccess(state, broadcaster, signal_path=tmp_path / ACCESS_SIGNAL_FILENAME)
    try:
        hirers = [
            broadcaster.connect(
                tier="hirer",
                session_id=f"s{n}",
                domains=["mixer", "lighting", "devices", "system", "timer"],
            )
            for n in range(2)
        ]
        for connection in hirers:
            connection.set_domains(connection.domains | {"status"})
        carried = 0
        for _step in range(60):
            if rng.random() < 0.15:
                before = access.permissions
                published = access.publish_permissions(resolve(_random_configuration(rng)))
                broadcaster.resync_tier(
                    "hirer", hirer_permissions.changed_domains(before, published)
                )
            elif rng.random() < 0.1:
                for connection in hirers:
                    for message in broadcaster.snapshot(connection.domains, connection=connection):
                        connection.send(message)
            else:
                for _ in range(rng.randint(1, 4)):
                    _random_state_change(rng, state, broadcaster)
                broadcaster.tick()
            current = access.permissions
            for connection in hirers:
                for message in await drain(connection):
                    _assert_within(message, current)
                    carried += _carried_ids(message)
        # Not vacuous: reachable ids did reach the hirers along the way.
        assert carried > 0
    finally:
        await broadcaster.stop()
        await bus.stop()
