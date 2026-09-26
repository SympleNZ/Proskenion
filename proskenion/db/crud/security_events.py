"""``security_events`` — the audit trail (§15.13).

Timestamps are ISO 8601 with offset and compare as text, which is how the
index ``idx_security_ts`` orders them. Pruning is chunked (§15.1): it runs
nightly and on low disk, and must never hold the write lock for seconds.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from proskenion.db.connection import DEFAULT_DELETE_BATCH, Database
from proskenion.db.crud import base

TABLE = "security_events"

#: The most rows one query returns (the security log viewer pages through the rest).
MAX_LIMIT = 1000


@dataclass(frozen=True, slots=True)
class SecurityEvent:
    id: int
    timestamp: str
    event_type: str
    user_ident: str | None
    ip_address: str | None
    detail: str | None


def _optional(value: Any) -> str | None:
    return None if value is None else str(value)


def _from_row(row: base.Row) -> SecurityEvent:
    return SecurityEvent(
        id=int(row["id"]),
        timestamp=str(row["timestamp"]),
        event_type=str(row["event_type"]),
        user_ident=_optional(row["user_ident"]),
        ip_address=_optional(row["ip_address"]),
        detail=_optional(row["detail"]),
    )


async def insert(
    db: Database,
    event_type: str,
    *,
    user_ident: str | None = None,
    ip_address: str | None = None,
    detail: str | None = None,
    timestamp: str | None = None,
) -> int:
    """Record one event and return its id. ``timestamp`` defaults to now."""
    async with db.write() as conn:
        return await base.insert(
            conn,
            TABLE,
            {
                "timestamp": timestamp or base.now_iso(),
                "event_type": event_type,
                "user_ident": user_ident,
                "ip_address": ip_address,
                "detail": detail,
            },
        )


async def query(
    db: Database,
    *,
    since: str | None = None,
    until: str | None = None,
    event_type: str | None = None,
    event_types: Sequence[str] | None = None,
    ip_address: str | None = None,
    limit: int = 500,
    offset: int = 0,
) -> list[SecurityEvent]:
    """Events newest first, filtered by inclusive ``since``/``until``, type and address.

    ``event_type`` narrows to one exact type; ``event_types`` narrows to any of
    several (the security log viewer's "outcome" filter groups several types
    together). Both may be given at once — the intersection — though nothing
    in this codebase does.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if since is not None:
        clauses.append("timestamp >= ?")
        params.append(since)
    if until is not None:
        clauses.append("timestamp <= ?")
        params.append(until)
    if event_type is not None:
        clauses.append("event_type = ?")
        params.append(event_type)
    if event_types is not None:
        placeholders = ",".join("?" for _ in event_types)
        clauses.append(f"event_type IN ({placeholders})")
        params.extend(event_types)
    if ip_address is not None:
        clauses.append("ip_address = ?")
        params.append(ip_address)
    sql = "SELECT * FROM security_events"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY timestamp DESC, id DESC LIMIT ? OFFSET ?"
    params.extend((max(1, min(limit, MAX_LIMIT)), max(0, offset)))
    async with db.read() as conn:
        cursor = await conn.execute(sql, params)
        rows = await cursor.fetchall()
    return [_from_row(base.row_to_dict(r)) for r in rows]


async def prune_before(db: Database, cutoff: str, *, batch: int = DEFAULT_DELETE_BATCH) -> int:
    """Delete events older than ``cutoff`` in bounded batches. Returns rows deleted."""
    return await db.chunked_delete(TABLE, "timestamp < ?", (cutoff,), batch=batch)
