"""``knx_device_groups`` and ``knx_group_addresses`` (§15.7).

KNX is a subsystem, not a driver category (§5.5, CONVENTIONS.md); a device
group has no relationship to ``devices``. ``knx_group_addresses.device_id`` is
``ON DELETE SET NULL`` — a dangling reference is acceptable there — while the
address itself is referenced ``ON DELETE RESTRICT`` from rules, derived
status and lighting channels: deleting an address in use must be refused
with what uses it (§15.1, §21.22).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

import aiosqlite

from proskenion.db.connection import Database
from proskenion.db.crud import base
from proskenion.db.crud.refs import InUseError, Reference

DEVICE_GROUPS_TABLE = "knx_device_groups"
ADDRESSES_TABLE = "knx_group_addresses"

DIRECTIONS = frozenset({"incoming", "outgoing", "both"})


@dataclass(frozen=True, slots=True)
class KnxDeviceGroup:
    id: int
    name: str
    description: str | None
    location: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class KnxGroupAddress:
    id: int
    group_address: str
    name: str
    description: str | None
    dpt: str
    direction: str
    device_id: int | None
    is_heartbeat: bool
    notes: str | None
    created_at: str
    updated_at: str


def _group_from_row(row: base.Row) -> KnxDeviceGroup:
    return KnxDeviceGroup(
        id=int(row["id"]),
        name=str(row["name"]),
        description=None if row["description"] is None else str(row["description"]),
        location=None if row["location"] is None else str(row["location"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _address_from_row(row: base.Row) -> KnxGroupAddress:
    return KnxGroupAddress(
        id=int(row["id"]),
        group_address=str(row["group_address"]),
        name=str(row["name"]),
        description=None if row["description"] is None else str(row["description"]),
        dpt=str(row["dpt"]),
        direction=str(row["direction"]),
        device_id=None if row["device_id"] is None else int(row["device_id"]),
        is_heartbeat=bool(row["is_heartbeat"]),
        notes=None if row["notes"] is None else str(row["notes"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _check_direction(direction: str) -> None:
    if direction not in DIRECTIONS:
        raise ValueError(f"unknown KNX address direction: {direction!r}")


# -- device groups -----------------------------------------------------------


async def create_device_group(
    db: Database, *, name: str, description: str | None = None, location: str | None = None
) -> KnxDeviceGroup:
    now = base.now_iso()
    async with db.write() as conn:
        row_id = await base.insert(
            conn,
            DEVICE_GROUPS_TABLE,
            {
                "name": name,
                "description": description,
                "location": location,
                "created_at": now,
                "updated_at": now,
            },
        )
        row = await base.get(conn, DEVICE_GROUPS_TABLE, row_id)
    if row is None:  # pragma: no cover - just inserted
        raise base.NotFoundError(DEVICE_GROUPS_TABLE, row_id)
    return _group_from_row(row)


async def get_device_group(db: Database, group_id: int) -> KnxDeviceGroup | None:
    async with db.read() as conn:
        row = await base.get(conn, DEVICE_GROUPS_TABLE, group_id)
    return None if row is None else _group_from_row(row)


async def list_device_groups(db: Database) -> list[KnxDeviceGroup]:
    async with db.read() as conn:
        rows = await base.list_rows(conn, DEVICE_GROUPS_TABLE, order_by="name, id")
    return [_group_from_row(r) for r in rows]


async def update_device_group(
    db: Database,
    group_id: int,
    expected_updated_at: str,
    *,
    name: str | None = None,
    description: str | None = None,
    location: str | None = None,
) -> KnxDeviceGroup:
    values: dict[str, Any] = {}
    if name is not None:
        values["name"] = name
    if description is not None:
        values["description"] = description
    if location is not None:
        values["location"] = location
    async with db.write() as conn:
        row = await base.update_with_version(
            conn, DEVICE_GROUPS_TABLE, group_id, expected_updated_at, values
        )
    return _group_from_row(row)


async def _references_device_group(conn: aiosqlite.Connection, group_id: int) -> list[Reference]:
    rows = await base.list_rows(
        conn, ADDRESSES_TABLE, where_sql="device_id = ?", params=(group_id,), order_by="id"
    )
    return [Reference(entity=ADDRESSES_TABLE, id=int(r["id"]), name=str(r["name"])) for r in rows]


async def references_device_group(db: Database, group_id: int) -> list[Reference]:
    """Group addresses that name this device group — informational only.

    ``device_id`` is ``ON DELETE SET NULL``, so deleting the group is never
    blocked; this is what ``GET /knx/device-groups/{id}/references`` shows.
    """
    async with db.read() as conn:
        return await _references_device_group(conn, group_id)


async def delete_device_group(db: Database, group_id: int) -> None:
    """Delete a device group. Never blocked — addresses fall back to ``NULL``."""
    async with db.write() as conn:
        await base.delete_or_in_use(conn, DEVICE_GROUPS_TABLE, group_id)


# -- group addresses ----------------------------------------------------------


async def create_address(
    db: Database,
    *,
    group_address: str,
    name: str,
    dpt: str,
    direction: str,
    description: str | None = None,
    device_id: int | None = None,
    is_heartbeat: bool = False,
    notes: str | None = None,
) -> KnxGroupAddress:
    _check_direction(direction)
    now = base.now_iso()
    async with db.write() as conn:
        row_id = await base.insert(
            conn,
            ADDRESSES_TABLE,
            {
                "group_address": group_address,
                "name": name,
                "description": description,
                "dpt": dpt,
                "direction": direction,
                "device_id": device_id,
                "is_heartbeat": int(is_heartbeat),
                "notes": notes,
                "created_at": now,
                "updated_at": now,
            },
        )
        row = await base.get(conn, ADDRESSES_TABLE, row_id)
    if row is None:  # pragma: no cover - just inserted
        raise base.NotFoundError(ADDRESSES_TABLE, row_id)
    return _address_from_row(row)


async def get_address(db: Database, address_id: int) -> KnxGroupAddress | None:
    async with db.read() as conn:
        row = await base.get(conn, ADDRESSES_TABLE, address_id)
    return None if row is None else _address_from_row(row)


async def list_addresses(db: Database) -> list[KnxGroupAddress]:
    async with db.read() as conn:
        rows = await base.list_rows(conn, ADDRESSES_TABLE, order_by="group_address")
    return [_address_from_row(r) for r in rows]


async def update_address(
    db: Database,
    address_id: int,
    expected_updated_at: str,
    *,
    group_address: str | None = None,
    name: str | None = None,
    description: str | None = None,
    dpt: str | None = None,
    direction: str | None = None,
    device_id: int | None = None,
    is_heartbeat: bool | None = None,
    notes: str | None = None,
) -> KnxGroupAddress:
    if direction is not None:
        _check_direction(direction)
    values: dict[str, Any] = {}
    if group_address is not None:
        values["group_address"] = group_address
    if name is not None:
        values["name"] = name
    if description is not None:
        values["description"] = description
    if dpt is not None:
        values["dpt"] = dpt
    if direction is not None:
        values["direction"] = direction
    if device_id is not None:
        values["device_id"] = device_id
    if is_heartbeat is not None:
        values["is_heartbeat"] = int(is_heartbeat)
    if notes is not None:
        values["notes"] = notes
    async with db.write() as conn:
        row = await base.update_with_version(
            conn, ADDRESSES_TABLE, address_id, expected_updated_at, values
        )
    return _address_from_row(row)


async def _references_address(conn: aiosqlite.Connection, address_id: int) -> list[Reference]:
    references: list[Reference] = []

    rows = await base.list_rows(
        conn, "rules", where_sql="knx_address_id = ?", params=(address_id,), order_by="id"
    )
    references += [Reference(entity="rules", id=int(r["id"]), name=str(r["name"])) for r in rows]

    rows = await base.list_rows(
        conn, "derived_status", where_sql="knx_address_id = ?", params=(address_id,), order_by="id"
    )
    references += [
        Reference(entity="derived_status", id=int(r["id"]), name=str(r["name"])) for r in rows
    ]

    rows = await base.list_rows(
        conn,
        "lighting_channels",
        where_sql=(
            "knx_command_address_id = ? OR knx_status_address_id = ? OR knx_switch_address_id = ?"
        ),
        params=(address_id, address_id, address_id),
        order_by="id",
    )
    references += [
        Reference(entity="lighting_channels", id=int(r["id"]), name=str(r["name"])) for r in rows
    ]

    cursor = await conn.execute(
        "SELECT sa.id AS id, s.name AS name FROM scene_actions sa "
        "JOIN scenes s ON s.id = sa.scene_id WHERE sa.knx_address_id = ? ORDER BY sa.id",
        (address_id,),
    )
    references += [
        Reference(entity="scene_actions", id=int(r["id"]), name=str(r["name"]))
        for r in await cursor.fetchall()
    ]

    return references


async def references_address(db: Database, address_id: int) -> list[Reference]:
    async with db.read() as conn:
        return await _references_address(conn, address_id)


async def delete_address(db: Database, address_id: int) -> None:
    """Delete a group address. Raises :class:`InUseError` if anything still
    references it (§15.1 ``ON DELETE RESTRICT``, §21.22)."""
    async with db.write() as conn:
        current = await base.get(conn, ADDRESSES_TABLE, address_id)
        if current is None:
            raise base.NotFoundError(ADDRESSES_TABLE, address_id)
        references = await _references_address(conn, address_id)
        if references:
            raise InUseError(ADDRESSES_TABLE, address_id, references)
        try:
            await conn.execute(f"DELETE FROM {ADDRESSES_TABLE} WHERE id = ?", (address_id,))
        except sqlite3.IntegrityError as exc:
            if "FOREIGN KEY" in str(exc).upper():
                raise InUseError(ADDRESSES_TABLE, address_id, references) from exc
            raise
