"""``email_config`` — the SMTP relay for §11.4 alerts and Admin -> System -> Email.

Exactly one row, id 1, but unlike ``hirer_config`` or ``users`` it is never
seeded: absence *is* "no relay configured" (contracts §7's
``email_unconfigured`` banner), so :func:`get` returns ``None`` rather than
raising, and the row is created for the first time by the first
``PUT /system/email`` (:func:`upsert`'s ``ON CONFLICT`` upsert), not by a
migration.

The password is stored the way every other §6.10 credential is: the JSON
form ``{"enc": "..."}``, encrypted with the device secret. This module never
decrypts it — only :mod:`proskenion.core.email`, and only after the caller
hands it the secret — so a bug here cannot leak a plain-text password into a
log line.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from proskenion.db.connection import Database
from proskenion.db.crud import base

TABLE = "email_config"
ROW_ID = 1


@dataclass(frozen=True, slots=True)
class EmailConfigRow:
    host: str | None
    port: int | None
    tls_mode: str
    username: str | None
    password: dict[str, str] | None  # the encrypted {"enc": ...} form, or None
    sender: str | None
    recipient: str | None
    updated_at: str
    updated_by: int | None


def _from_row(row: base.Row) -> EmailConfigRow:
    password_raw = row.get("password")
    password: dict[str, str] | None = json.loads(password_raw) if password_raw else None
    port = row.get("port")
    updated_by = row.get("updated_by")
    return EmailConfigRow(
        host=row.get("host"),
        port=None if port is None else int(port),
        tls_mode=str(row["tls_mode"]),
        username=row.get("username"),
        password=password,
        sender=row.get("sender"),
        recipient=row.get("recipient"),
        updated_at=str(row["updated_at"]),
        updated_by=None if updated_by is None else int(updated_by),
    )


async def get(db: Database) -> EmailConfigRow | None:
    """The one row, or ``None`` when nothing has ever been saved."""
    async with db.read() as conn:
        row = await base.get(conn, TABLE, ROW_ID)
    return None if row is None else _from_row(row)


async def upsert(
    db: Database,
    *,
    host: str,
    port: int,
    tls_mode: str,
    username: str | None,
    password: dict[str, str] | None,
    sender: str,
    recipient: str,
    updated_by: int | None,
) -> EmailConfigRow:
    """Replace the one row whole.

    No optimistic-concurrency version stamp: §16.1's ``updated_at`` check
    defends a list a client reads and diffs against a concurrent edit; this
    is a single admin-only settings form with one row, and the API layer
    reads it fresh on every ``GET`` (§10's "absent or sentinel password on
    PUT means unchanged" is the only staleness the form has to defend, and
    that is handled by the caller before this is reached).
    """
    async with db.write() as conn:
        await conn.execute(
            """
            INSERT INTO email_config
                (id, host, port, tls_mode, username, password, sender, recipient,
                 updated_at, updated_by)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                host = excluded.host,
                port = excluded.port,
                tls_mode = excluded.tls_mode,
                username = excluded.username,
                password = excluded.password,
                sender = excluded.sender,
                recipient = excluded.recipient,
                updated_at = excluded.updated_at,
                updated_by = excluded.updated_by
            """,
            (
                host,
                port,
                tls_mode,
                username,
                None if password is None else json.dumps(password),
                sender,
                recipient,
                base.now_iso(),
                updated_by,
            ),
        )
        row = await base.get(conn, TABLE, ROW_ID)
    if row is None:  # pragma: no cover - the upsert above always leaves a row
        raise RuntimeError("email_config upsert left no row")
    return _from_row(row)


async def clear(db: Database) -> None:
    """Remove the one row: absence is "no relay configured" (see the module
    docstring). Already absent is not an error."""
    async with db.write() as conn:
        await conn.execute("DELETE FROM email_config WHERE id = ?", (ROW_ID,))
