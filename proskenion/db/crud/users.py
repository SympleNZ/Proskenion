"""``users`` — always exactly two rows, ``admin`` and ``operator`` (§15.4)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import aiosqlite

from proskenion.db.connection import Database
from proskenion.db.crud import base

Tier = Literal["admin", "operator"]
TIERS: tuple[Tier, ...] = ("admin", "operator")

PLACEHOLDER_HASH_PREFIX = "$2b$12$PLACEHOLDER"
"""Prefix of the seed's placeholder password and PIN hashes (§15.2).

The seed writes bcrypt-shaped hashes that verify against nothing. The
first-run wizard checks :func:`is_placeholder_hash` to decide whether
credentials still need setting, and authentication must refuse any hash that
matches before calling bcrypt.
"""

TABLE = "users"


@dataclass(frozen=True, slots=True)
class User:
    id: int
    tier: str
    password: str
    token_version: int
    updated_at: str
    #: When this tier's password was last actually changed (§21.23) — distinct
    #: from ``updated_at``, which any write to the row bumps, including
    #: ``bump_token_version``. ``None`` for a row the seed placeholder has
    #: never been replaced on; the Users screen shows that as "Not recorded".
    password_changed_at: str | None = None

    @property
    def has_placeholder_password(self) -> bool:
        return is_placeholder_hash(self.password)


def is_placeholder_hash(hash_: str) -> bool:
    """True if ``hash_`` is one of the seed's placeholders rather than a real bcrypt hash."""
    return hash_.startswith(PLACEHOLDER_HASH_PREFIX)


def _from_row(row: base.Row) -> User:
    changed_at = row.get("password_changed_at")
    return User(
        id=int(row["id"]),
        tier=str(row["tier"]),
        password=str(row["password"]),
        token_version=int(row["token_version"]),
        updated_at=str(row["updated_at"]),
        password_changed_at=None if changed_at is None else str(changed_at),
    )


async def get_by_tier(db: Database, tier: str) -> User | None:
    async with db.read() as conn:
        rows = await base.list_rows(conn, TABLE, where_sql="tier = ?", params=(tier,))
    return _from_row(rows[0]) if rows else None


async def get_by_id(db: Database, user_id: int) -> User | None:
    async with db.read() as conn:
        row = await base.get(conn, TABLE, user_id)
    return None if row is None else _from_row(row)


async def get_all(db: Database) -> list[User]:
    async with db.read() as conn:
        rows = await base.list_rows(conn, TABLE, order_by="tier")
    return [_from_row(r) for r in rows]


async def has_placeholder_passwords(db: Database) -> bool:
    """True while any user still carries the seed placeholder — the wizard's check."""
    return any(u.has_placeholder_password for u in await get_all(db))


async def set_password_hash(db: Database, tier: str, password_hash: str) -> User:
    """Store a new bcrypt hash, bump ``token_version`` so existing JWTs die,
    and record ``password_changed_at`` (§21.23) — every password-change path
    (the API, the first-run wizard, ``avc-reset-password``) goes through
    :func:`proskenion.core.auth.set_staff_password`, which calls this."""
    async with db.write() as conn:
        user = await _require(conn, tier)
        row = await base.update(
            conn,
            TABLE,
            user.id,
            {
                "password": password_hash,
                "token_version": user.token_version + 1,
                "password_changed_at": base.now_iso(),
            },
        )
    return _from_row(row)


async def bump_token_version(db: Database, tier: str) -> User:
    """Invalidate every JWT issued for ``tier`` without changing the password."""
    async with db.write() as conn:
        user = await _require(conn, tier)
        row = await base.update(conn, TABLE, user.id, {"token_version": user.token_version + 1})
    return _from_row(row)


async def _require(conn: aiosqlite.Connection, tier: str) -> User:
    rows = await base.list_rows(conn, TABLE, where_sql="tier = ?", params=(tier,))
    if not rows:
        raise base.NotFoundError(TABLE, -1)
    return _from_row(rows[0])
