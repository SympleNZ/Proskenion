"""Fixture profiles, bars, lighting channels, groups and colour presets (§9.1, §15.9).

Channel occupancy comes from a fixture's profile, not a hard-coded type
(§9.1): a profile declares ``channel_count`` and the role of each channel.
The role vocabulary is closed (§15.9) and validated here before a profile is
stored. Two guards live in this module because both cross a foreign key SQLite
cannot enforce:

* :func:`references_to_device` lists the fixtures patched to a ``devices``
  row, for the 409 ``in_use`` body when a lighting output device with
  fixtures is deleted (``lighting_channels.device_id`` is ``ON DELETE
  RESTRICT`` — §16.1, decided in the Phase 2 slice A plan, Q2). The delete
  itself happens in :mod:`proskenion.db.crud.devices`, unchanged.
* :func:`delete_channel` checks every ``scene_actions.dmx_snapshot`` for this
  channel's id before deleting it — that JSON column references lighting
  channel ids with nothing SQLite can enforce (§9.7).
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import aiosqlite

from proskenion.db.connection import Database
from proskenion.db.crud import base
from proskenion.db.crud.refs import ConstraintError, InUseError, Reference

FIXTURE_PROFILES_TABLE = "fixture_profiles"
BARS_TABLE = "lighting_bars"
CHANNELS_TABLE = "lighting_channels"
GROUPS_TABLE = "lighting_groups"
MEMBERSHIPS_TABLE = "lighting_group_memberships"
PRESETS_TABLE = "colour_presets"

# Closed vocabulary (§15.9): the compositor must know what to do with every
# role, so an unrecognised one is refused rather than silently ignored.
ROLES = frozenset(
    {
        "dimmer",
        "red",
        "green",
        "blue",
        "white",
        "amber",
        "uv",
        "pan",
        "tilt",
        "strobe",
        "macro",
        "unused",
    }
)

CHANNEL_TYPES = frozenset({"dmx", "knx_dimmer"})


# -- fixture profiles ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FixtureChannel:
    offset: int
    role: str
    default: float


@dataclass(frozen=True, slots=True)
class FixtureProfile:
    id: int
    manufacturer: str | None
    model: str | None
    name: str
    channel_count: int
    channels: tuple[FixtureChannel, ...]
    created_at: str
    updated_at: str


def _validate_profile_channels(channel_count: int, channels: list[dict[str, Any]]) -> None:
    if channel_count < 1:
        raise ValueError(f"channel_count must be at least 1, got {channel_count}")
    if len(channels) != channel_count:
        raise ValueError(
            f"channels has {len(channels)} entries but channel_count is {channel_count}"
        )
    offsets: set[int] = set()
    for entry in channels:
        role = entry.get("role")
        if role not in ROLES:
            raise ValueError(f"unknown fixture channel role: {role!r}")
        offset = entry.get("offset")
        if not isinstance(offset, int) or isinstance(offset, bool):
            raise ValueError(f"channel offset must be an int, got {offset!r}")
        if offset in offsets:
            raise ValueError(f"duplicate channel offset: {offset}")
        offsets.add(offset)
    if offsets != set(range(channel_count)):
        raise ValueError(
            f"channel offsets must run 0..{channel_count - 1} with no gaps, got {sorted(offsets)}"
        )


def _dump_channels(channels: list[dict[str, Any]]) -> str:
    return json.dumps({"channels": channels}, separators=(",", ":"), sort_keys=True)


def _profile_from_row(row: base.Row) -> FixtureProfile:
    payload = json.loads(str(row["channels"]))
    channels = tuple(
        FixtureChannel(
            offset=int(c["offset"]), role=str(c["role"]), default=float(c.get("default", 0))
        )
        for c in payload["channels"]
    )
    return FixtureProfile(
        id=int(row["id"]),
        manufacturer=None if row["manufacturer"] is None else str(row["manufacturer"]),
        model=None if row["model"] is None else str(row["model"]),
        name=str(row["name"]),
        channel_count=int(row["channel_count"]),
        channels=channels,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


async def create_fixture_profile(
    db: Database,
    *,
    name: str,
    channel_count: int,
    channels: list[dict[str, Any]],
    manufacturer: str | None = None,
    model: str | None = None,
) -> FixtureProfile:
    _validate_profile_channels(channel_count, channels)
    now = base.now_iso()
    async with db.write() as conn:
        row_id = await base.insert(
            conn,
            FIXTURE_PROFILES_TABLE,
            {
                "manufacturer": manufacturer,
                "model": model,
                "name": name,
                "channel_count": channel_count,
                "channels": _dump_channels(channels),
                "created_at": now,
                "updated_at": now,
            },
        )
        row = await base.get(conn, FIXTURE_PROFILES_TABLE, row_id)
    if row is None:  # pragma: no cover - just inserted
        raise base.NotFoundError(FIXTURE_PROFILES_TABLE, row_id)
    return _profile_from_row(row)


async def get_fixture_profile(db: Database, profile_id: int) -> FixtureProfile | None:
    async with db.read() as conn:
        row = await base.get(conn, FIXTURE_PROFILES_TABLE, profile_id)
    return None if row is None else _profile_from_row(row)


async def list_fixture_profiles(db: Database) -> list[FixtureProfile]:
    async with db.read() as conn:
        rows = await base.list_rows(conn, FIXTURE_PROFILES_TABLE, order_by="name, id")
    return [_profile_from_row(r) for r in rows]


async def update_fixture_profile(
    db: Database,
    profile_id: int,
    expected_updated_at: str,
    *,
    name: str | None = None,
    channel_count: int | None = None,
    channels: list[dict[str, Any]] | None = None,
    manufacturer: str | None = None,
    model: str | None = None,
) -> FixtureProfile:
    if channel_count is not None or channels is not None:
        async with db.read() as conn:
            current = await base.get(conn, FIXTURE_PROFILES_TABLE, profile_id)
        if current is None:
            raise base.NotFoundError(FIXTURE_PROFILES_TABLE, profile_id)
        effective_count = (
            channel_count if channel_count is not None else int(current["channel_count"])
        )
        effective_channels = (
            channels
            if channels is not None
            else json.loads(str(current["channels"]))["channels"]
        )
        _validate_profile_channels(effective_count, effective_channels)

    values: dict[str, Any] = {}
    if name is not None:
        values["name"] = name
    if channel_count is not None:
        values["channel_count"] = channel_count
    if channels is not None:
        values["channels"] = _dump_channels(channels)
    if manufacturer is not None:
        values["manufacturer"] = manufacturer
    if model is not None:
        values["model"] = model
    async with db.write() as conn:
        row = await base.update_with_version(
            conn, FIXTURE_PROFILES_TABLE, profile_id, expected_updated_at, values
        )
    return _profile_from_row(row)


async def _references_fixture_profile(
    conn: aiosqlite.Connection, profile_id: int
) -> list[Reference]:
    rows = await base.list_rows(
        conn, CHANNELS_TABLE, where_sql="profile_id = ?", params=(profile_id,), order_by="id"
    )
    return [Reference(entity=CHANNELS_TABLE, id=int(r["id"]), name=str(r["name"])) for r in rows]


async def references_fixture_profile(db: Database, profile_id: int) -> list[Reference]:
    async with db.read() as conn:
        return await _references_fixture_profile(conn, profile_id)


async def delete_fixture_profile(db: Database, profile_id: int) -> None:
    """Delete a fixture profile. Raises :class:`InUseError` listing the
    channels patched with it (``profile_id`` is ``ON DELETE RESTRICT``)."""
    async with db.write() as conn:
        current = await base.get(conn, FIXTURE_PROFILES_TABLE, profile_id)
        if current is None:
            raise base.NotFoundError(FIXTURE_PROFILES_TABLE, profile_id)
        references = await _references_fixture_profile(conn, profile_id)
        if references:
            raise InUseError(FIXTURE_PROFILES_TABLE, profile_id, references)
        await conn.execute(f"DELETE FROM {FIXTURE_PROFILES_TABLE} WHERE id = ?", (profile_id,))


# -- lighting bars ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LightingBar:
    id: int
    name: str
    sort_order: int
    notes: str | None
    created_at: str
    updated_at: str


def _bar_from_row(row: base.Row) -> LightingBar:
    return LightingBar(
        id=int(row["id"]),
        name=str(row["name"]),
        sort_order=int(row["sort_order"]),
        notes=None if row["notes"] is None else str(row["notes"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


async def create_bar(
    db: Database, *, name: str, sort_order: int = 0, notes: str | None = None
) -> LightingBar:
    now = base.now_iso()
    async with db.write() as conn:
        row_id = await base.insert(
            conn,
            BARS_TABLE,
            {
                "name": name,
                "sort_order": sort_order,
                "notes": notes,
                "created_at": now,
                "updated_at": now,
            },
        )
        row = await base.get(conn, BARS_TABLE, row_id)
    if row is None:  # pragma: no cover - just inserted
        raise base.NotFoundError(BARS_TABLE, row_id)
    return _bar_from_row(row)


async def get_bar(db: Database, bar_id: int) -> LightingBar | None:
    async with db.read() as conn:
        row = await base.get(conn, BARS_TABLE, bar_id)
    return None if row is None else _bar_from_row(row)


async def list_bars(db: Database) -> list[LightingBar]:
    async with db.read() as conn:
        rows = await base.list_rows(conn, BARS_TABLE, order_by="sort_order, id")
    return [_bar_from_row(r) for r in rows]


async def update_bar(
    db: Database,
    bar_id: int,
    expected_updated_at: str,
    *,
    name: str | None = None,
    sort_order: int | None = None,
    notes: str | None = None,
) -> LightingBar:
    values: dict[str, Any] = {}
    if name is not None:
        values["name"] = name
    if sort_order is not None:
        values["sort_order"] = sort_order
    if notes is not None:
        values["notes"] = notes
    async with db.write() as conn:
        row = await base.update_with_version(conn, BARS_TABLE, bar_id, expected_updated_at, values)
    return _bar_from_row(row)


async def references_bar(db: Database, bar_id: int) -> list[Reference]:
    """Channels placed on this bar — informational; ``bar_id`` is ``SET NULL``."""
    async with db.read() as conn:
        rows = await base.list_rows(
            conn, CHANNELS_TABLE, where_sql="bar_id = ?", params=(bar_id,), order_by="id"
        )
    return [Reference(entity=CHANNELS_TABLE, id=int(r["id"]), name=str(r["name"])) for r in rows]


async def delete_bar(db: Database, bar_id: int) -> None:
    """Delete a bar. Never blocked — channels fall back to ``bar_id = NULL``."""
    async with db.write() as conn:
        await base.delete_or_in_use(conn, BARS_TABLE, bar_id)


# -- lighting channels -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LightingChannel:
    id: int
    name: str
    type: str
    profile_id: int | None
    device_id: int | None
    universe: int
    address: int | None
    bar_id: int | None
    position: float
    colour_r: int | None
    colour_g: int | None
    colour_b: int | None
    colour_w: int | None
    knx_command_address_id: int | None
    knx_status_address_id: int | None
    knx_switch_address_id: int | None
    fade_mode: str
    min_value: float
    max_value: float
    visible_staff: bool
    notes: str | None
    created_at: str
    updated_at: str


def _channel_from_row(row: base.Row) -> LightingChannel:
    def _opt_int(key: str) -> int | None:
        return None if row[key] is None else int(row[key])

    return LightingChannel(
        id=int(row["id"]),
        name=str(row["name"]),
        type=str(row["type"]),
        profile_id=_opt_int("profile_id"),
        device_id=_opt_int("device_id"),
        universe=int(row["universe"]),
        address=_opt_int("address"),
        bar_id=_opt_int("bar_id"),
        position=float(row["position"]),
        colour_r=_opt_int("colour_r"),
        colour_g=_opt_int("colour_g"),
        colour_b=_opt_int("colour_b"),
        colour_w=_opt_int("colour_w"),
        knx_command_address_id=_opt_int("knx_command_address_id"),
        knx_status_address_id=_opt_int("knx_status_address_id"),
        knx_switch_address_id=_opt_int("knx_switch_address_id"),
        fade_mode=str(row["fade_mode"]),
        min_value=float(row["min_value"]),
        max_value=float(row["max_value"]),
        visible_staff=bool(row["visible_staff"]),
        notes=None if row["notes"] is None else str(row["notes"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


async def create_channel(
    db: Database,
    *,
    name: str,
    type: str,  # matches the column name; the value comes from §9.1's vocabulary
    profile_id: int | None = None,
    device_id: int | None = None,
    universe: int = 1,
    address: int | None = None,
    bar_id: int | None = None,
    position: float = 0.5,
    colour_r: int | None = None,
    colour_g: int | None = None,
    colour_b: int | None = None,
    colour_w: int | None = None,
    knx_command_address_id: int | None = None,
    knx_status_address_id: int | None = None,
    knx_switch_address_id: int | None = None,
    fade_mode: str = "hardware",
    min_value: float = 0.0,
    max_value: float = 100.0,
    visible_staff: bool = True,
    notes: str | None = None,
) -> LightingChannel:
    if type not in CHANNEL_TYPES:
        raise ValueError(f"unknown lighting channel type: {type!r}")
    now = base.now_iso()
    values = {
        "name": name,
        "type": type,
        "profile_id": profile_id,
        "device_id": device_id,
        "universe": universe,
        "address": address,
        "bar_id": bar_id,
        "position": position,
        "colour_r": colour_r,
        "colour_g": colour_g,
        "colour_b": colour_b,
        "colour_w": colour_w,
        "knx_command_address_id": knx_command_address_id,
        "knx_status_address_id": knx_status_address_id,
        "knx_switch_address_id": knx_switch_address_id,
        "fade_mode": fade_mode,
        "min_value": min_value,
        "max_value": max_value,
        "visible_staff": int(visible_staff),
        "notes": notes,
        "created_at": now,
        "updated_at": now,
    }
    async with db.write() as conn:
        try:
            row_id = await base.insert(conn, CHANNELS_TABLE, values)
        except sqlite3.IntegrityError as exc:
            raise _translate_channel_integrity_error(exc) from exc
        row = await base.get(conn, CHANNELS_TABLE, row_id)
    if row is None:  # pragma: no cover - just inserted
        raise base.NotFoundError(CHANNELS_TABLE, row_id)
    return _channel_from_row(row)


def _translate_channel_integrity_error(exc: sqlite3.IntegrityError) -> Exception:
    """CHECK failures become :class:`ConstraintError`; anything else (a bad
    foreign key on insert, say) is left as the raw ``IntegrityError`` — §22.2
    asks only that the shape ``CHECK`` be named, not every constraint."""
    message = str(exc)
    if "CHECK constraint failed" in message:
        return ConstraintError("lighting_channels_shape", message)
    return exc


async def get_channel(db: Database, channel_id: int) -> LightingChannel | None:
    async with db.read() as conn:
        row = await base.get(conn, CHANNELS_TABLE, channel_id)
    return None if row is None else _channel_from_row(row)


async def list_channels(db: Database, *, device_id: int | None = None) -> list[LightingChannel]:
    async with db.read() as conn:
        if device_id is None:
            rows = await base.list_rows(conn, CHANNELS_TABLE, order_by="id")
        else:
            rows = await base.list_rows(
                conn, CHANNELS_TABLE, where_sql="device_id = ?", params=(device_id,), order_by="id"
            )
    return [_channel_from_row(r) for r in rows]


async def update_channel(
    db: Database, channel_id: int, expected_updated_at: str, **values: Any
) -> LightingChannel:
    """Edit a channel under §16.1 optimistic concurrency.

    ``values`` are column values to change; unknown columns raise
    :class:`ValueError` via the underlying insert-column validation. A shape
    ``CHECK`` violation raises :class:`~proskenion.db.crud.refs.ConstraintError`
    rather than a raw ``IntegrityError`` (§22.2).
    """
    if "visible_staff" in values and isinstance(values["visible_staff"], bool):
        values["visible_staff"] = int(values["visible_staff"])
    async with db.write() as conn:
        try:
            row = await base.update_with_version(
                conn, CHANNELS_TABLE, channel_id, expected_updated_at, values
            )
        except sqlite3.IntegrityError as exc:
            raise _translate_channel_integrity_error(exc) from exc
    return _channel_from_row(row)


async def _snapshot_references(conn: aiosqlite.Connection, channel_id: int) -> list[Reference]:
    """Scenes whose ``dmx_snapshot`` JSON still keys this channel (§9.7).

    No foreign key protects ``dmx_snapshot``; every ``dmx`` scene action's
    snapshot is checked by hand.
    """
    cursor = await conn.execute(
        "SELECT sa.id AS action_id, sa.scene_id AS scene_id, sa.dmx_snapshot AS snapshot, "
        "s.name AS scene_name "
        "FROM scene_actions sa JOIN scenes s ON s.id = sa.scene_id "
        "WHERE sa.domain = 'dmx' AND sa.dmx_snapshot IS NOT NULL"
    )
    key = str(channel_id)
    seen_scenes: dict[int, str] = {}
    for row in await cursor.fetchall():
        try:
            snapshot = json.loads(str(row["snapshot"]))
        except (TypeError, ValueError):
            continue
        if isinstance(snapshot, dict) and key in snapshot:
            seen_scenes[int(row["scene_id"])] = str(row["scene_name"])
    return [
        Reference(entity="scenes", id=sid, name=name) for sid, name in sorted(seen_scenes.items())
    ]


async def snapshot_references(db: Database, channel_id: int) -> list[Reference]:
    async with db.read() as conn:
        return await _snapshot_references(conn, channel_id)


async def delete_channel(db: Database, channel_id: int) -> None:
    """Delete a lighting channel.

    Raises :class:`~proskenion.db.crud.refs.InUseError` listing the scenes
    whose saved-look snapshot still references this channel (§9.7) before any
    row is touched. Group memberships cascade; nothing else blocks the delete
    at the database level.
    """
    async with db.write() as conn:
        current = await base.get(conn, CHANNELS_TABLE, channel_id)
        if current is None:
            raise base.NotFoundError(CHANNELS_TABLE, channel_id)
        references = await _snapshot_references(conn, channel_id)
        if references:
            raise InUseError(CHANNELS_TABLE, channel_id, references)
        await conn.execute(f"DELETE FROM {CHANNELS_TABLE} WHERE id = ?", (channel_id,))


async def references_to_device(db: Database, device_id: int) -> list[Reference]:
    """Fixtures patched to a ``devices`` row — the list for the 409 body when
    deleting a lighting output device with fixtures is refused (§16.1, Q2)."""
    async with db.read() as conn:
        rows = await base.list_rows(
            conn, CHANNELS_TABLE, where_sql="device_id = ?", params=(device_id,), order_by="id"
        )
    return [Reference(entity=CHANNELS_TABLE, id=int(r["id"]), name=str(r["name"])) for r in rows]


# -- overlap detection --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Overlap:
    """Two ``dmx`` fixtures whose address ranges share a slot (§9.1).

    Reported, not refused: patching ahead of a rewire is normal during
    commissioning.
    """

    device_id: int
    universe: int
    channel_a_id: int
    channel_a_name: str
    range_a: tuple[int, int]  # inclusive start, end
    channel_b_id: int
    channel_b_name: str
    range_b: tuple[int, int]


async def find_overlaps(db: Database, *, device_id: int | None = None) -> list[Overlap]:
    async with db.read() as conn:
        where = "type = 'dmx'"
        params: list[Any] = []
        if device_id is not None:
            where += " AND device_id = ?"
            params.append(device_id)
        rows = await base.list_rows(
            conn, CHANNELS_TABLE, where_sql=where, params=params, order_by="id"
        )
        profile_rows = await base.list_rows(conn, FIXTURE_PROFILES_TABLE)
    counts = {int(p["id"]): int(p["channel_count"]) for p in profile_rows}

    grouped: dict[tuple[int, int], list[tuple[int, str, int, int]]] = defaultdict(list)
    for r in rows:
        if r["device_id"] is None or r["address"] is None:
            continue
        profile_id = r["profile_id"]
        count = counts.get(int(profile_id), 1) if profile_id is not None else 1
        start = int(r["address"])
        end = start + count - 1
        grouped[(int(r["device_id"]), int(r["universe"]))].append(
            (int(r["id"]), str(r["name"]), start, end)
        )

    overlaps: list[Overlap] = []
    for (dev, universe), fixtures in grouped.items():
        fixtures.sort(key=lambda f: f[2])
        for i in range(len(fixtures)):
            for j in range(i + 1, len(fixtures)):
                a_id, a_name, a_start, a_end = fixtures[i]
                b_id, b_name, b_start, b_end = fixtures[j]
                if a_start <= b_end and b_start <= a_end:
                    overlaps.append(
                        Overlap(
                            device_id=dev,
                            universe=universe,
                            channel_a_id=a_id,
                            channel_a_name=a_name,
                            range_a=(a_start, a_end),
                            channel_b_id=b_id,
                            channel_b_name=b_name,
                            range_b=(b_start, b_end),
                        )
                    )
    return overlaps


# -- lighting groups and memberships --------------------------------------------------


@dataclass(frozen=True, slots=True)
class LightingGroup:
    id: int
    name: str
    colour: str
    sort_order: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class GroupMember:
    channel_id: int
    sort_order: int


def _group_from_row(row: base.Row) -> LightingGroup:
    return LightingGroup(
        id=int(row["id"]),
        name=str(row["name"]),
        colour=str(row["colour"]),
        sort_order=int(row["sort_order"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


async def create_group(
    db: Database, *, name: str, colour: str = "#2E86C1", sort_order: int = 0
) -> LightingGroup:
    now = base.now_iso()
    async with db.write() as conn:
        row_id = await base.insert(
            conn,
            GROUPS_TABLE,
            {
                "name": name,
                "colour": colour,
                "sort_order": sort_order,
                "created_at": now,
                "updated_at": now,
            },
        )
        row = await base.get(conn, GROUPS_TABLE, row_id)
    if row is None:  # pragma: no cover - just inserted
        raise base.NotFoundError(GROUPS_TABLE, row_id)
    return _group_from_row(row)


async def get_group(db: Database, group_id: int) -> LightingGroup | None:
    async with db.read() as conn:
        row = await base.get(conn, GROUPS_TABLE, group_id)
    return None if row is None else _group_from_row(row)


async def list_groups(db: Database) -> list[LightingGroup]:
    async with db.read() as conn:
        rows = await base.list_rows(conn, GROUPS_TABLE, order_by="sort_order, id")
    return [_group_from_row(r) for r in rows]


async def update_group(
    db: Database,
    group_id: int,
    expected_updated_at: str,
    *,
    name: str | None = None,
    colour: str | None = None,
    sort_order: int | None = None,
) -> LightingGroup:
    values: dict[str, Any] = {}
    if name is not None:
        values["name"] = name
    if colour is not None:
        values["colour"] = colour
    if sort_order is not None:
        values["sort_order"] = sort_order
    async with db.write() as conn:
        row = await base.update_with_version(
            conn, GROUPS_TABLE, group_id, expected_updated_at, values
        )
    return _group_from_row(row)


async def _references_group(conn: aiosqlite.Connection, group_id: int) -> list[Reference]:
    references: list[Reference] = []
    rows = await base.list_rows(
        conn, "rules", where_sql="lighting_group_id = ?", params=(group_id,), order_by="id"
    )
    references += [Reference(entity="rules", id=int(r["id"]), name=str(r["name"])) for r in rows]
    rows = await base.list_rows(
        conn, "derived_status", where_sql="lighting_group_id = ?", params=(group_id,), order_by="id"
    )
    references += [
        Reference(entity="derived_status", id=int(r["id"]), name=str(r["name"])) for r in rows
    ]
    return references


async def references_group(db: Database, group_id: int) -> list[Reference]:
    async with db.read() as conn:
        return await _references_group(conn, group_id)


async def delete_group(db: Database, group_id: int) -> None:
    """Delete a lighting group.

    Raises :class:`InUseError` listing both rules and derived-status entries
    that name it (RESTRICT on both, so one blocked reference never hides the
    other — §15.8). Memberships cascade.
    """
    async with db.write() as conn:
        current = await base.get(conn, GROUPS_TABLE, group_id)
        if current is None:
            raise base.NotFoundError(GROUPS_TABLE, group_id)
        references = await _references_group(conn, group_id)
        if references:
            raise InUseError(GROUPS_TABLE, group_id, references)
        await conn.execute(f"DELETE FROM {GROUPS_TABLE} WHERE id = ?", (group_id,))


async def get_group_members(db: Database, group_id: int) -> list[GroupMember]:
    async with db.read() as conn:
        rows = await base.list_rows(
            conn,
            MEMBERSHIPS_TABLE,
            where_sql="group_id = ?",
            params=(group_id,),
            order_by="sort_order, id",
        )
    return [
        GroupMember(channel_id=int(r["channel_id"]), sort_order=int(r["sort_order"]))
        for r in rows
    ]


async def set_group_members(
    db: Database, group_id: int, channel_ids: list[int]
) -> list[GroupMember]:
    """Replace a group's membership list, in sort order, as one transaction.

    Duplicate ids are refused: the ``UNIQUE(group_id, channel_id)`` constraint
    exists precisely so a channel cannot be a member twice.
    """
    if len(set(channel_ids)) != len(channel_ids):
        raise ValueError("a channel cannot be a member of the same group twice")
    async with db.write() as conn:
        current = await base.get(conn, GROUPS_TABLE, group_id)
        if current is None:
            raise base.NotFoundError(GROUPS_TABLE, group_id)
        await conn.execute(f"DELETE FROM {MEMBERSHIPS_TABLE} WHERE group_id = ?", (group_id,))
        for index, channel_id in enumerate(channel_ids):
            await base.insert(
                conn,
                MEMBERSHIPS_TABLE,
                {"group_id": group_id, "channel_id": channel_id, "sort_order": index},
            )
    return [GroupMember(channel_id=cid, sort_order=i) for i, cid in enumerate(channel_ids)]


# -- colour presets ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ColourPreset:
    id: int
    name: str
    r: int
    g: int
    b: int
    w: int
    sort_order: int
    created_at: str
    updated_at: str


def _preset_from_row(row: base.Row) -> ColourPreset:
    return ColourPreset(
        id=int(row["id"]),
        name=str(row["name"]),
        r=int(row["r"]),
        g=int(row["g"]),
        b=int(row["b"]),
        w=int(row["w"]),
        sort_order=int(row["sort_order"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


async def create_preset(
    db: Database, *, name: str, r: int, g: int, b: int, w: int = 0, sort_order: int = 0
) -> ColourPreset:
    now = base.now_iso()
    async with db.write() as conn:
        row_id = await base.insert(
            conn,
            PRESETS_TABLE,
            {
                "name": name,
                "r": r,
                "g": g,
                "b": b,
                "w": w,
                "sort_order": sort_order,
                "created_at": now,
                "updated_at": now,
            },
        )
        row = await base.get(conn, PRESETS_TABLE, row_id)
    if row is None:  # pragma: no cover - just inserted
        raise base.NotFoundError(PRESETS_TABLE, row_id)
    return _preset_from_row(row)


async def get_preset(db: Database, preset_id: int) -> ColourPreset | None:
    async with db.read() as conn:
        row = await base.get(conn, PRESETS_TABLE, preset_id)
    return None if row is None else _preset_from_row(row)


async def list_presets(db: Database) -> list[ColourPreset]:
    async with db.read() as conn:
        rows = await base.list_rows(conn, PRESETS_TABLE, order_by="sort_order, id")
    return [_preset_from_row(r) for r in rows]


async def update_preset(
    db: Database,
    preset_id: int,
    expected_updated_at: str,
    *,
    name: str | None = None,
    r: int | None = None,
    g: int | None = None,
    b: int | None = None,
    w: int | None = None,
    sort_order: int | None = None,
) -> ColourPreset:
    values: dict[str, Any] = {}
    if name is not None:
        values["name"] = name
    if r is not None:
        values["r"] = r
    if g is not None:
        values["g"] = g
    if b is not None:
        values["b"] = b
    if w is not None:
        values["w"] = w
    if sort_order is not None:
        values["sort_order"] = sort_order
    async with db.write() as conn:
        row = await base.update_with_version(
            conn, PRESETS_TABLE, preset_id, expected_updated_at, values
        )
    return _preset_from_row(row)


async def delete_preset(db: Database, preset_id: int) -> None:
    """Delete a colour preset. Nothing references it, so this never blocks."""
    async with db.write() as conn:
        await base.delete_or_in_use(conn, PRESETS_TABLE, preset_id)
