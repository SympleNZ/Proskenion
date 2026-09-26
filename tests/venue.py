"""A commissioned venue, written straight into the database.

The baseline tests (spec §13.5, §22.4) need a room with something in *every*
captured area at once — scenes and their actions, lighting, KNX, rules and
derived statuses, mixer channels and desk scenes, video destinations, pages
and hirer permissions — and then need to drift each of them and put them
back. Building that through the API would mean commissioning three devices
and walking a dozen screens for a test whose subject is none of those
things, so the rows go in directly, in foreign-key order, with enforcement
on.

Three devices (ids :data:`LIGHTING_DEVICE`, :data:`MIXER_DEVICE` and
:data:`MATRIX_DEVICE`) are created too. They are deliberately *not* part of
a baseline (§13.5), which is exactly why a test needs them: what a restore
must leave alone, and what a missing one must make it refuse.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from proskenion.db.connection import Database
from proskenion.db.crud.base import Row

#: Stamped on every row here, so a diff never turns on when a test ran.
STAMP: Final = "2026-01-01T00:00:00+13:00"

LIGHTING_DEVICE: Final = 1
MIXER_DEVICE: Final = 2
MATRIX_DEVICE: Final = 3

#: The seeded single-channel dimmer profile (§15.2's seed).
DIMMER_PROFILE: Final = 1

#: Mixer channels the venue's one hirer page places, with their ceilings.
WIRELESS_1: Final = 1
LECTERN: Final = 2
MAIN: Final = 3

#: The venue's one hirer page, its items and its button. Numbered well clear
#: of 1 because a running application has already generated the default page
#: (§15.12) and taken the low ids for it.
PAGE: Final = 100
WIRELESS_ITEM: Final = 100
LECTERN_ITEM: Final = 101
MAIN_ITEM: Final = 102
GROUP_ITEM: Final = 103
LIGHTING_ITEM: Final = 104
PANEL_ITEM: Final = 105
BUTTON: Final = 100

#: The venue as commissioned: table → rows, parents first.
VENUE: Final[tuple[tuple[str, tuple[dict[str, Any], ...]], ...]] = (
    (
        "devices",
        (
            {
                "id": LIGHTING_DEVICE,
                "category": "lighting_output",
                "driver_key": "artnet",
                "name": "Stage DMX",
                "config": '{"host": "10.2.30.60"}',
            },
            {
                "id": MIXER_DEVICE,
                "category": "mixer",
                "driver_key": "cq20b",
                "name": "CQ-20B",
                "config": '{"host": "10.2.30.61"}',
            },
            {
                "id": MATRIX_DEVICE,
                "category": "video_matrix",
                "driver_key": "lkv422",
                "name": "Matrix",
                "config": '{"device_path": "/dev/serial/by-id/matrix"}',
            },
        ),
    ),
    (
        "fixture_profiles",
        (
            {
                "id": 10,
                "manufacturer": "Chauvet",
                "model": "Ovation",
                "name": "House LED",
                "channel_count": 1,
                "channels": '{"channels": [{"offset": 0, "role": "dimmer", "default": 0}]}',
            },
        ),
    ),
    ("lighting_bars", ({"id": 2, "name": "Cyc bar", "sort_order": 1, "notes": None},)),
    (
        "knx_device_groups",
        ({"id": 1, "name": "Wall panel", "description": "Stage left", "location": "Stage"},),
    ),
    (
        "knx_group_addresses",
        (
            {
                "id": 1,
                "group_address": "1/0/1",
                "name": "Bank command",
                "description": None,
                "dpt": "1.001",
                "direction": "both",
                "device_id": 1,
                "is_heartbeat": 0,
                "notes": None,
            },
            {
                "id": 2,
                "group_address": "1/0/2",
                "name": "Bank status",
                "description": None,
                "dpt": "1.001",
                "direction": "outgoing",
                "device_id": 1,
                "is_heartbeat": 0,
                "notes": None,
            },
            {
                "id": 3,
                "group_address": "2/1/1",
                "name": "House dimmer",
                "description": None,
                "dpt": "5.001",
                "direction": "both",
                "device_id": None,
                "is_heartbeat": 0,
                "notes": None,
            },
        ),
    ),
    (
        "lighting_channels",
        (
            {
                "id": 1,
                "name": "Warm 1",
                "type": "dmx",
                "profile_id": DIMMER_PROFILE,
                "device_id": LIGHTING_DEVICE,
                "universe": 1,
                "address": 1,
                "bar_id": 1,
                "position": 0.3,
            },
            {
                "id": 2,
                "name": "Warm 2",
                "type": "dmx",
                "profile_id": DIMMER_PROFILE,
                "device_id": LIGHTING_DEVICE,
                "universe": 1,
                "address": 2,
                "bar_id": 1,
                "position": 0.7,
            },
            {
                "id": 3,
                "name": "House",
                "type": "knx_dimmer",
                "knx_command_address_id": 3,
                "bar_id": None,
                "position": 0.5,
            },
        ),
    ),
    ("lighting_groups", ({"id": 1, "name": "Bank", "colour": "#2E86C1", "sort_order": 0},)),
    (
        "lighting_group_memberships",
        (
            {"id": 1, "group_id": 1, "channel_id": 1, "sort_order": 0},
            {"id": 2, "group_id": 1, "channel_id": 2, "sort_order": 1},
        ),
    ),
    (
        "colour_presets",
        ({"id": 1, "name": "Warm white", "r": 255, "g": 180, "b": 120, "w": 0, "sort_order": 0},),
    ),
    (
        "scenes",
        (
            {
                "id": 1,
                "name": "Restore Venue Default",
                "description": "§13.5's one button",
                "protected": 1,
                "visible_operator": 1,
                "sort_order": 0,
            },
            {"id": 2, "name": "Performance Start", "sort_order": 1},
        ),
    ),
    (
        "matrix_inputs",
        (
            {"id": 1, "device_id": MATRIX_DEVICE, "driver_ref": "1", "name": "Laptop"},
            {"id": 2, "device_id": MATRIX_DEVICE, "driver_ref": "2", "name": "Document camera"},
        ),
    ),
    (
        "matrix_outputs",
        (
            {"id": 1, "device_id": MATRIX_DEVICE, "driver_ref": "1", "name": "Projector"},
            {"id": 2, "device_id": MATRIX_DEVICE, "driver_ref": "2", "name": "Foyer screen"},
        ),
    ),
    (
        "video_destinations",
        (
            {
                "id": 1,
                "device_id": MATRIX_DEVICE,
                "name": "The room",
                "default_input_id": 1,
                "sort_order": 0,
            },
        ),
    ),
    (
        "video_destination_outputs",
        ({"id": 1, "destination_id": 1, "output_id": 1, "sort_order": 0},),
    ),
    (
        "mixer_channels",
        (
            {
                "id": WIRELESS_1,
                "device_id": MIXER_DEVICE,
                "channel_kind": "input",
                "name": "Wireless Mic 1",
                "hirer_max_db": -6.0,
                "sort_order": 0,
            },
            {
                "id": LECTERN,
                "device_id": MIXER_DEVICE,
                "channel_kind": "input",
                "name": "Lectern",
                "hirer_max_db": -10.0,
                "sort_order": 1,
            },
            {
                "id": MAIN,
                "device_id": MIXER_DEVICE,
                "channel_kind": "main",
                "name": "Main",
                "hirer_max_db": -4.0,
                "sort_order": 2,
            },
        ),
    ),
    (
        "mixer_channel_refs",
        (
            {"id": 1, "channel_id": WIRELESS_1, "driver_ref": "ch1", "sort_order": 0},
            {"id": 2, "channel_id": LECTERN, "driver_ref": "ch2", "sort_order": 0},
            {"id": 3, "channel_id": MAIN, "driver_ref": "main", "sort_order": 0},
        ),
    ),
    (
        "mixer_desk_scenes",
        (
            {
                "id": 1,
                "device_id": MIXER_DEVICE,
                "scene_ref": "1",
                "name": "Venue Default",
                "is_venue_default": 1,
                "sort_order": 0,
            },
            {
                "id": 2,
                "device_id": MIXER_DEVICE,
                "scene_ref": "2",
                "name": "Performance",
                "is_venue_default": 0,
                "sort_order": 1,
            },
        ),
    ),
    (
        "rules",
        (
            {
                "id": 1,
                "name": "Bank on",
                "trigger_type": "knx",
                "knx_address_id": 1,
                "match_type": "any",
                "action_type": "lighting_group",
                "lighting_group_id": 1,
                "on_level": 80.0,
                "off_level": 0.0,
                "sort_order": 0,
            },
            {
                "id": 2,
                "name": "Start show",
                "trigger_type": "surface",
                "match_type": "equal",
                "action_type": "run_scene",
                "scene_id": 2,
                "sort_order": 1,
            },
        ),
    ),
    (
        "derived_status",
        (
            {
                "id": 1,
                "name": "Bank indicator",
                "knx_address_id": 2,
                "source_type": "lighting_group_all_at",
                "lighting_group_id": 1,
                "compare_level": 80.0,
            },
        ),
    ),
    (
        "scene_actions",
        (
            {
                "id": 1,
                "scene_id": 1,
                "sort_order": 0,
                "delay_ms": 0,
                "domain": "mixer_recall",
                "mixer_scene_id": 1,
            },
            {
                "id": 2,
                "scene_id": 1,
                "sort_order": 1,
                "delay_ms": 0,
                "domain": "hdmi_source",
                "hdmi_destination": 1,
                "hdmi_input_id": 1,
            },
            {
                "id": 3,
                "scene_id": 2,
                "sort_order": 0,
                "delay_ms": 0,
                "domain": "mixer_recall",
                "mixer_scene_id": 2,
            },
            {
                "id": 4,
                "scene_id": 2,
                "sort_order": 1,
                "delay_ms": 500,
                "domain": "mixer_fader",
                "mixer_channel_id": LECTERN,
                "mixer_db": -3.0,
            },
        ),
    ),
    ("pages", ({"id": PAGE, "name": "Performance", "sort_order": 1, "is_default": 0},)),
    (
        "page_items",
        (
            {
                "id": WIRELESS_ITEM,
                "page_id": PAGE,
                "sort_order": 0,
                "kind": "channel",
                "channel_id": WIRELESS_1,
            },
            {
                "id": LECTERN_ITEM,
                "page_id": PAGE,
                "sort_order": 1,
                "kind": "channel",
                "channel_id": LECTERN,
            },
            {
                "id": MAIN_ITEM,
                "page_id": PAGE,
                "sort_order": 2,
                "kind": "channel",
                "channel_id": MAIN,
            },
            {
                "id": GROUP_ITEM,
                "page_id": PAGE,
                "sort_order": 3,
                "kind": "group_master",
                "group_id": 1,
            },
            {
                "id": LIGHTING_ITEM,
                "page_id": PAGE,
                "sort_order": 4,
                "kind": "channel",
                "lighting_channel_id": 1,
            },
            {
                "id": PANEL_ITEM,
                "page_id": PAGE,
                "sort_order": 5,
                "kind": "panel",
                "panel_title": "Scenes",
                "panel_width": 2,
            },
        ),
    ),
    (
        "page_buttons",
        (
            {
                "id": BUTTON,
                "item_id": PANEL_ITEM,
                "col": 0,
                "row": 0,
                "label": "Start show",
                "rule_id": 2,
                "state_id": 1,
                "confirm": 0,
            },
        ),
    ),
    ("hirer_pages", ({"id": 1, "page_id": PAGE},)),
)

#: ``hirer_config``'s three captured permission switches, as commissioned.
HIRER_PERMISSIONS: Final[Mapping[str, int]] = {
    "lighting_enabled": 1,
    "individual_fixtures": 0,
    "colour_enabled": 1,
}


async def _columns(db: Database, table: str) -> set[str]:
    async with db.read() as conn:
        cursor = await conn.execute(f"PRAGMA table_info({table})")
        return {str(row["name"]) for row in await cursor.fetchall()}


async def insert_rows(db: Database, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
    """Insert ``rows`` into ``table``, stamping the timestamp columns it has."""
    columns = await _columns(db, table)
    stamps = {c: STAMP for c in ("created_at", "updated_at") if c in columns}
    async with db.write() as conn:
        for row in rows:
            values: Row = {**row, **stamps}
            names = list(values)
            placeholders = ", ".join("?" for _ in names)
            await conn.execute(
                f"INSERT INTO {table} ({', '.join(names)}) VALUES ({placeholders})",
                [values[name] for name in names],
            )


async def commission(db: Database) -> None:
    """Write the venue: three devices, then a row in every captured area."""
    for table, rows in VENUE:
        await insert_rows(db, table, rows)
    async with db.write() as conn:
        assignments = ", ".join(f"{column} = ?" for column in HIRER_PERMISSIONS)
        await conn.execute(
            f"UPDATE hirer_config SET {assignments}, updated_at = ? WHERE id = 1",
            [*HIRER_PERMISSIONS.values(), STAMP],
        )


async def count(db: Database, table: str, where: str = "1") -> int:
    async with db.read() as conn:
        cursor = await conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}")
        row = await cursor.fetchone()
        return 0 if row is None else int(row["n"])


async def rows_of(db: Database, table: str) -> list[Row]:
    async with db.read() as conn:
        cursor = await conn.execute(f"SELECT * FROM {table} ORDER BY id")
        return [{key: row[key] for key in row.keys()} for row in await cursor.fetchall()]


async def one(db: Database, sql: str, params: Sequence[Any] = ()) -> Any:
    async with db.read() as conn:
        cursor = await conn.execute(sql, list(params))
        row = await cursor.fetchone()
        return None if row is None else row[0]
