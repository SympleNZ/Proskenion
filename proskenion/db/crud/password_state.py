"""``password_state`` — one row (id 1), like ``hirer_config`` (§15.4).

Whether the admin and operator passwords are currently the same cannot be
read off ``users.password``: two bcrypt hashes of an identical password never
compare equal, since each carries its own random salt. So it is computed
once, at the moment either password changes — checking the new plaintext
against the *other* tier's stored hash, the only point the plaintext is ever
available (:func:`proskenion.core.auth.set_staff_password`) — and the answer
is cached here, so the admin-only status endpoint (``GET
/auth/password-status``, §21.23) never has to touch a plaintext or a hash
again.
"""

from __future__ import annotations

from dataclasses import dataclass

from proskenion.db.connection import Database
from proskenion.db.crud import base

TABLE = "password_state"
ROW_ID = 1


@dataclass(frozen=True, slots=True)
class PasswordState:
    identical: bool
    updated_at: str


def _from_row(row: base.Row) -> PasswordState:
    return PasswordState(identical=bool(row["identical"]), updated_at=str(row["updated_at"]))


async def get(db: Database) -> PasswordState:
    async with db.read() as conn:
        row = await base.get(conn, TABLE, ROW_ID)
    if row is None:
        raise base.NotFoundError(TABLE, ROW_ID)
    return _from_row(row)


async def set_identical(db: Database, identical: bool) -> PasswordState:
    """Recorded every time a staff password changes, by
    :func:`proskenion.core.auth.set_staff_password`."""
    async with db.write() as conn:
        current = await base.get(conn, TABLE, ROW_ID)
        if current is None:
            raise base.NotFoundError(TABLE, ROW_ID)
        row = await base.update(conn, TABLE, ROW_ID, {"identical": int(identical)})
    return _from_row(row)
