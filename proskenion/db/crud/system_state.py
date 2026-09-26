"""``system_state`` — persisted runtime state keyed by ``(domain, key)`` (§15.13).

Values are text; callers serialise whatever they store (JSON for anything
structured). :func:`set_many` is what the state persister calls once per
500 ms tick with every value that changed in that window — one transaction,
not one per value (§15.1).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from proskenion.db.connection import Database
from proskenion.db.crud import base

TABLE = "system_state"

UPSERT = """
INSERT INTO system_state (domain, key, value, updated_at, source)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(domain, key) DO UPDATE SET
  value = excluded.value,
  updated_at = excluded.updated_at,
  source = excluded.source
"""


@dataclass(frozen=True, slots=True)
class StateRow:
    domain: str
    key: str
    value: str
    updated_at: str
    source: str | None


def _from_row(row: base.Row) -> StateRow:
    return StateRow(
        domain=str(row["domain"]),
        key=str(row["key"]),
        value=str(row["value"]),
        updated_at=str(row["updated_at"]),
        source=None if row["source"] is None else str(row["source"]),
    )


async def get(db: Database, domain: str, key: str) -> StateRow | None:
    async with db.read() as conn:
        rows = await base.list_rows(
            conn, TABLE, where_sql="domain = ? AND key = ?", params=(domain, key)
        )
    return _from_row(rows[0]) if rows else None


async def get_value(db: Database, domain: str, key: str) -> str | None:
    row = await get(db, domain, key)
    return None if row is None else row.value


async def get_domain(db: Database, domain: str) -> dict[str, str]:
    """Every key in ``domain`` as ``{key: value}``."""
    async with db.read() as conn:
        rows = await base.list_rows(
            conn, TABLE, where_sql="domain = ?", params=(domain,), order_by="key"
        )
    return {str(r["key"]): str(r["value"]) for r in rows}


async def set(
    db: Database, domain: str, key: str, value: str, *, source: str | None = None
) -> None:
    """Upsert one value in its own transaction."""
    await set_many(db, [(domain, key, value)], source=source)


async def set_many(
    db: Database, items: Iterable[tuple[str, str, str]], *, source: str | None = None
) -> int:
    """Upsert ``(domain, key, value)`` triples in one transaction.

    Returns the number of rows written. An empty iterable writes nothing and
    takes no lock.
    """
    rows = list(items)
    if not rows:
        return 0
    now = base.now_iso()
    async with db.write() as conn:
        await conn.executemany(UPSERT, [(d, k, v, now, source) for d, k, v in rows])
    return len(rows)


async def delete_domain(db: Database, domain: str) -> int:
    """Remove every key in ``domain``. Returns the number of rows deleted."""
    async with db.write() as conn:
        cursor = await conn.execute("DELETE FROM system_state WHERE domain = ?", (domain,))
    return cursor.rowcount
