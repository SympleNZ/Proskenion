"""``scene_execution_log`` (§8.16, §15.8) — every execution, with per-action results.

A row is written when a run starts, with ``completed_at`` and ``result``
empty, and completed when the run settles. An application that stops
mid-scene therefore leaves an honest, incomplete row rather than none.
Retained 90 days by :mod:`proskenion.core.retention`.

Times are ISO 8601 with offset (§4.9). Pacific/Auckland's offset changes
twice a year, so date-range filters compare with SQLite's ``julianday()``,
which honours the offset, rather than as text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from proskenion.db.connection import Database
from proskenion.db.crud import base

TABLE = "scene_execution_log"

#: The most rows one query returns (§21's log screen pages through the rest).
MAX_LIMIT = 1000


@dataclass(frozen=True, slots=True)
class LogEntry:
    id: int
    scene_id: int | None
    triggered_by: str
    started_at: str
    completed_at: str | None
    result: str | None
    action_results: list[dict[str, Any]]


def _from_row(row: base.Row) -> LogEntry:
    raw = row["action_results"]
    try:
        results = json.loads(str(raw)) if raw is not None else []
    except ValueError:
        results = []
    return LogEntry(
        id=int(row["id"]),
        scene_id=None if row["scene_id"] is None else int(row["scene_id"]),
        triggered_by=str(row["triggered_by"]),
        started_at=str(row["started_at"]),
        completed_at=None if row["completed_at"] is None else str(row["completed_at"]),
        result=None if row["result"] is None else str(row["result"]),
        action_results=results if isinstance(results, list) else [],
    )


async def record_start(db: Database, *, scene_id: int, triggered_by: str, started_at: str) -> int:
    """The row for a run that has just started. Returns its id."""
    async with db.write() as conn:
        return await base.insert(
            conn,
            TABLE,
            {"scene_id": scene_id, "triggered_by": triggered_by, "started_at": started_at},
        )


async def record_completion(
    db: Database,
    log_id: int,
    *,
    completed_at: str,
    result: str,
    action_results: list[dict[str, Any]],
) -> None:
    """Complete a run's row: when it settled, its result and every action's outcome."""
    async with db.write() as conn:
        await conn.execute(
            f"UPDATE {TABLE} SET completed_at = ?, result = ?, action_results = ? WHERE id = ?",
            (completed_at, result, json.dumps(action_results, ensure_ascii=False), log_id),
        )


async def get(db: Database, log_id: int) -> LogEntry | None:
    async with db.read() as conn:
        row = await base.get(conn, TABLE, log_id)
    return None if row is None else _from_row(row)


async def query(
    db: Database,
    *,
    scene_id: int | None = None,
    since: str | None = None,
    until: str | None = None,
    result: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[LogEntry]:
    """Newest first. ``since`` and ``until`` are ISO 8601 instants, inclusive."""
    clauses: list[str] = []
    params: list[object] = []
    if scene_id is not None:
        clauses.append("scene_id = ?")
        params.append(scene_id)
    if since is not None:
        clauses.append("julianday(started_at) >= julianday(?)")
        params.append(since)
    if until is not None:
        clauses.append("julianday(started_at) <= julianday(?)")
        params.append(until)
    if result is not None:
        clauses.append("result = ?")
        params.append(result)
    sql = f"SELECT * FROM {TABLE}"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY julianday(started_at) DESC, id DESC LIMIT ? OFFSET ?"
    params.extend((max(1, min(limit, MAX_LIMIT)), max(0, offset)))
    async with db.read() as conn:
        cursor = await conn.execute(sql, params)
        rows = await cursor.fetchall()
    return [_from_row(base.row_to_dict(row)) for row in rows]


async def latest_per_scene(db: Database) -> dict[int, LogEntry]:
    """Each scene's most recent run — what a scene card's "last ran" shows (§21.10)."""
    async with db.read() as conn:
        cursor = await conn.execute(
            f"SELECT * FROM {TABLE} WHERE id IN "
            f"(SELECT MAX(id) FROM {TABLE} WHERE scene_id IS NOT NULL GROUP BY scene_id)"
        )
        rows = await cursor.fetchall()
    entries = [_from_row(base.row_to_dict(row)) for row in rows]
    return {entry.scene_id: entry for entry in entries if entry.scene_id is not None}


__all__ = [
    "MAX_LIMIT",
    "TABLE",
    "LogEntry",
    "get",
    "latest_per_scene",
    "query",
    "record_completion",
    "record_start",
]
