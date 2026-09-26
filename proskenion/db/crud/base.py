"""Helpers shared by the entity CRUD modules.

Every helper takes an open connection — a write helper expects the connection
yielded by ``Database.write()`` so that several operations can share one
transactional unit (§15.1); read helpers accept either.

The exceptions here are plain Python. The API layer maps them to the §16.1
error vocabulary: :class:`ConflictError` → 409 ``conflict`` with the current
record in ``detail.current``; :class:`InUseError` → ``in_use``;
:class:`NotFoundError` → ``not_found``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite

from proskenion.db.connection import validate_identifier

AUCKLAND = ZoneInfo("Pacific/Auckland")

Row = dict[str, Any]
"""A table row as a plain dictionary keyed by column name."""


class CrudError(Exception):
    """Base class for the CRUD layer's own exceptions."""


class NotFoundError(CrudError):
    """No row with that id."""

    def __init__(self, table: str, row_id: int) -> None:
        super().__init__(f"{table} {row_id} not found")
        self.table = table
        self.row_id = row_id


class ConflictError(CrudError):
    """Optimistic-concurrency mismatch (§16.1). ``current`` is the row as stored."""

    def __init__(self, table: str, row_id: int, current: Row) -> None:
        super().__init__(f"{table} {row_id} was modified since it was read")
        self.table = table
        self.row_id = row_id
        self.current = current


class InUseError(CrudError):
    """A delete was blocked by ``ON DELETE RESTRICT`` (§15.1, §21.22)."""

    def __init__(self, table: str, row_id: int) -> None:
        super().__init__(f"{table} {row_id} is in use and cannot be deleted")
        self.table = table
        self.row_id = row_id


def now_iso() -> str:
    """Current time in Pacific/Auckland as ISO 8601 with offset (§4.9).

    Microsecond precision, e.g. ``2026-09-04T14:30:00.123456+12:00``. Whole
    seconds are not enough for ``updated_at`` to act as a version stamp: two
    writes within one second would be indistinguishable to the §16.1 check.
    """
    return datetime.now(tz=AUCKLAND).isoformat(timespec="microseconds")


def next_version_stamp(previous: str | None) -> str:
    """A ``now_iso()`` value guaranteed to differ from ``previous``.

    Clocks are not monotonic and two writes can land within the same
    microsecond, so the stamp is nudged forward when it would otherwise repeat.
    """
    stamp = now_iso()
    if previous is None or stamp != previous:
        return stamp
    bumped = datetime.fromisoformat(previous) + timedelta(microseconds=1)
    return bumped.astimezone(AUCKLAND).isoformat(timespec="microseconds")


def row_to_dict(row: sqlite3.Row) -> Row:
    return {key: row[key] for key in row.keys()}


def _columns(values: Mapping[str, Any]) -> list[str]:
    columns = [validate_identifier(column) for column in values]
    if not columns:
        raise ValueError("no columns given")
    return columns


async def insert(conn: aiosqlite.Connection, table: str, values: Mapping[str, Any]) -> int:
    """Insert one row and return its id."""
    validate_identifier(table)
    columns = _columns(values)
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
    cursor = await conn.execute(sql, [values[c] for c in columns])
    if cursor.lastrowid is None:  # pragma: no cover - INSERT always sets it
        raise RuntimeError("insert returned no rowid")
    return int(cursor.lastrowid)


async def get(conn: aiosqlite.Connection, table: str, row_id: int) -> Row | None:
    """Fetch one row by id, or ``None``."""
    validate_identifier(table)
    cursor = await conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,))
    row = await cursor.fetchone()
    return None if row is None else row_to_dict(row)


async def list_rows(
    conn: aiosqlite.Connection,
    table: str,
    *,
    where_sql: str | None = None,
    params: Sequence[Any] = (),
    order_by: str = "id",
) -> list[Row]:
    """Fetch rows, optionally filtered by a trusted SQL fragment."""
    validate_identifier(table)
    for term in order_by.split(","):
        parts = term.split()
        validate_identifier(parts[0])
        if len(parts) > 2 or (len(parts) == 2 and parts[1].upper() not in ("ASC", "DESC")):
            raise ValueError(f"bad order_by term: {term!r}")
    sql = f"SELECT * FROM {table}"
    if where_sql:
        sql += f" WHERE ({where_sql})"
    sql += f" ORDER BY {order_by}"
    cursor = await conn.execute(sql, list(params))
    return [row_to_dict(row) for row in await cursor.fetchall()]


async def update_with_version(
    conn: aiosqlite.Connection,
    table: str,
    row_id: int,
    expected_updated_at: str,
    values: Mapping[str, Any],
) -> Row:
    """Update a row only if its ``updated_at`` still equals what the client read.

    Implements §16.1 optimistic concurrency. Must run inside a write unit so
    the read-compare-write is atomic. Raises :class:`NotFoundError` when the
    row is missing and :class:`ConflictError` (carrying the current row) when
    ``updated_at`` differs. ``values`` must not contain ``updated_at``; it is
    stamped here. Returns the row as it is after the update.
    """
    validate_identifier(table)
    if "updated_at" in values:
        raise ValueError("updated_at is stamped by update_with_version")
    current = await get(conn, table, row_id)
    if current is None:
        raise NotFoundError(table, row_id)
    if current["updated_at"] != expected_updated_at:
        raise ConflictError(table, row_id, current)

    stamped = {**values, "updated_at": next_version_stamp(current["updated_at"])}
    columns = _columns(stamped)
    assignments = ", ".join(f"{c} = ?" for c in columns)
    cursor = await conn.execute(
        f"UPDATE {table} SET {assignments} WHERE id = ? AND updated_at = ?",
        [*(stamped[c] for c in columns), row_id, expected_updated_at],
    )
    if cursor.rowcount != 1:  # pragma: no cover - impossible under the write lock
        raise ConflictError(table, row_id, current)
    updated = await get(conn, table, row_id)
    if updated is None:  # pragma: no cover - the row was just updated
        raise NotFoundError(table, row_id)
    return updated


async def update(
    conn: aiosqlite.Connection, table: str, row_id: int, values: Mapping[str, Any]
) -> Row:
    """Update a row without a version check, stamping ``updated_at``.

    For writes that are not client edits of a configuration entity — a
    password change, a token-version bump — where §16.1 does not apply.
    """
    validate_identifier(table)
    current = await get(conn, table, row_id)
    if current is None:
        raise NotFoundError(table, row_id)
    stamped = {**values, "updated_at": next_version_stamp(current.get("updated_at"))}
    columns = _columns(stamped)
    assignments = ", ".join(f"{c} = ?" for c in columns)
    await conn.execute(
        f"UPDATE {table} SET {assignments} WHERE id = ?",
        [*(stamped[c] for c in columns), row_id],
    )
    updated = await get(conn, table, row_id)
    if updated is None:  # pragma: no cover - the row was just updated
        raise NotFoundError(table, row_id)
    return updated


async def delete_or_in_use(conn: aiosqlite.Connection, table: str, row_id: int) -> None:
    """Delete a row; raise :class:`InUseError` if ``ON DELETE RESTRICT`` blocks it.

    Raises :class:`NotFoundError` when nothing was deleted. Must run inside a
    write unit; a blocked delete leaves the transaction usable.
    """
    validate_identifier(table)
    try:
        cursor = await conn.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
    except sqlite3.IntegrityError as exc:
        if "FOREIGN KEY" in str(exc).upper():
            raise InUseError(table, row_id) from exc
        raise
    if cursor.rowcount == 0:
        raise NotFoundError(table, row_id)
