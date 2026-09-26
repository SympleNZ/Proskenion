"""``devices`` — driver instances (§15.5, §5.5).

``config`` is stored as JSON whose shape the driver's ``CONFIG_SCHEMA``
declares; this module serialises and deserialises it and knows nothing about
its contents. Fields marked encrypted are handled per §6.10 by the caller
before they reach here. Edits go through §16.1 optimistic concurrency.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from proskenion.db.connection import Database
from proskenion.db.crud import base

TABLE = "devices"

Config = dict[str, Any]


@dataclass(frozen=True, slots=True)
class Device:
    id: int
    category: str
    driver_key: str
    name: str
    enabled: bool
    config: Config
    created_at: str
    updated_at: str


def _from_row(row: base.Row) -> Device:
    config = json.loads(str(row["config"]))
    if not isinstance(config, dict):
        raise ValueError(f"devices {row['id']}: config is not a JSON object")
    return Device(
        id=int(row["id"]),
        category=str(row["category"]),
        driver_key=str(row["driver_key"]),
        name=str(row["name"]),
        enabled=bool(row["enabled"]),
        config=config,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _dump(config: Mapping[str, Any]) -> str:
    return json.dumps(dict(config), separators=(",", ":"), sort_keys=True)


async def create(
    db: Database,
    *,
    category: str,
    driver_key: str,
    name: str,
    config: Mapping[str, Any],
    enabled: bool = True,
) -> Device:
    now = base.now_iso()
    async with db.write() as conn:
        device_id = await base.insert(
            conn,
            TABLE,
            {
                "category": category,
                "driver_key": driver_key,
                "name": name,
                "enabled": int(enabled),
                "config": _dump(config),
                "created_at": now,
                "updated_at": now,
            },
        )
        row = await base.get(conn, TABLE, device_id)
    if row is None:  # pragma: no cover - just inserted
        raise base.NotFoundError(TABLE, device_id)
    return _from_row(row)


async def get(db: Database, device_id: int) -> Device | None:
    async with db.read() as conn:
        row = await base.get(conn, TABLE, device_id)
    return None if row is None else _from_row(row)


async def list_all(db: Database, *, category: str | None = None) -> list[Device]:
    async with db.read() as conn:
        if category is None:
            rows = await base.list_rows(conn, TABLE, order_by="category, name, id")
        else:
            rows = await base.list_rows(
                conn, TABLE, where_sql="category = ?", params=(category,), order_by="name, id"
            )
    return [_from_row(r) for r in rows]


async def update(
    db: Database,
    device_id: int,
    expected_updated_at: str,
    *,
    name: str | None = None,
    driver_key: str | None = None,
    enabled: bool | None = None,
    config: Mapping[str, Any] | None = None,
) -> Device:
    """Edit a device under §16.1 optimistic concurrency.

    Raises :class:`~proskenion.db.crud.base.ConflictError` when
    ``expected_updated_at`` is stale. ``category`` is fixed at creation: a
    device does not change what it is.
    """
    values: dict[str, Any] = {}
    if name is not None:
        values["name"] = name
    if driver_key is not None:
        values["driver_key"] = driver_key
    if enabled is not None:
        values["enabled"] = int(enabled)
    if config is not None:
        values["config"] = _dump(config)
    async with db.write() as conn:
        row = await base.update_with_version(conn, TABLE, device_id, expected_updated_at, values)
    return _from_row(row)


async def delete(db: Database, device_id: int) -> None:
    """Delete a device. Raises ``InUseError`` if a RESTRICT reference blocks it."""
    async with db.write() as conn:
        await base.delete_or_in_use(conn, TABLE, device_id)
