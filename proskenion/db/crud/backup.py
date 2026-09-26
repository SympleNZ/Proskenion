"""``backup_archives`` and ``backup_destination`` (contracts §5, §8).

Two independent tables, one module because both belong to the same screen:

``backup_archives``
    One row per archive the nightly job or a manual "Back up now" built —
    :func:`record_archive` inserts it once, and :func:`mark_present`/
    :func:`mark_absent` flip the per-destination flags as retention prunes a
    copy or a write to a destination fails. :func:`mark_verified` is the
    monthly job's write (§13.4): a verification failure marks the archive
    ``untrusted`` with a reason, which the Backup screen shows and a restore
    refuses to offer.

``backup_destination``
    Exactly one row, like ``email_config`` and ``hirer_config`` — absence is
    "no network destination configured", not an error, so :func:`get_destination`
    returns ``None`` rather than raising. The password is stored the way
    every other §6.10 credential is: the JSON ``{"enc": "..."}`` form,
    encrypted with the device secret by the caller — this module never
    encrypts or decrypts it, so a bug here cannot leak a plain password into
    a log line.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from proskenion.db.connection import Database
from proskenion.db.crud import base

ARCHIVES_TABLE = "backup_archives"
DESTINATION_TABLE = "backup_destination"
DESTINATION_ROW_ID = 1


# -- archives -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArchiveRow:
    id: str
    created_at: str
    source: str  # 'scheduled'|'manual'
    size_bytes: int
    sha256: str
    schema_version: int
    app_version: str
    local_present: bool
    usb_present: bool
    network_present: bool
    verified_at: str | None
    untrusted: bool
    untrusted_reason: str | None

    @property
    def any_present(self) -> bool:
        return self.local_present or self.usb_present or self.network_present


def _archive_from_row(row: base.Row) -> ArchiveRow:
    return ArchiveRow(
        id=str(row["id"]),
        created_at=str(row["created_at"]),
        source=str(row["source"]),
        size_bytes=int(row["size_bytes"]),
        sha256=str(row["sha256"]),
        schema_version=int(row["schema_version"]),
        app_version=str(row["app_version"]),
        local_present=bool(row["local_present"]),
        usb_present=bool(row["usb_present"]),
        network_present=bool(row["network_present"]),
        verified_at=row.get("verified_at"),
        untrusted=bool(row["untrusted"]),
        untrusted_reason=row.get("untrusted_reason"),
    )


async def record_archive(
    db: Database,
    *,
    archive_id: str,
    created_at: str,
    source: str,
    size_bytes: int,
    sha256: str,
    schema_version: int,
    app_version: str,
    local_present: bool,
    usb_present: bool,
    network_present: bool,
) -> ArchiveRow:
    """Record one archive. ``archive_id`` is the primary key (contracts §8's
    minute-granularity filename): an upsert rather than a plain insert, so
    two archives that genuinely land in the same minute — a manual "Back up
    now" moments after the nightly run, most plausibly — replace each other
    the same way their identically-named file on disk already does, instead
    of raising a constraint error the caller has to handle specially.
    """
    async with db.write() as conn:
        await conn.execute(
            f"""
            INSERT INTO {ARCHIVES_TABLE}
                (id, created_at, source, size_bytes, sha256, schema_version, app_version,
                 local_present, usb_present, network_present)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                created_at = excluded.created_at,
                source = excluded.source,
                size_bytes = excluded.size_bytes,
                sha256 = excluded.sha256,
                schema_version = excluded.schema_version,
                app_version = excluded.app_version,
                local_present = excluded.local_present,
                usb_present = excluded.usb_present,
                network_present = excluded.network_present,
                verified_at = NULL,
                untrusted = 0,
                untrusted_reason = NULL
            """,
            (
                archive_id,
                created_at,
                source,
                size_bytes,
                sha256,
                schema_version,
                app_version,
                int(local_present),
                int(usb_present),
                int(network_present),
            ),
        )
        cursor = await conn.execute(
            f"SELECT * FROM {ARCHIVES_TABLE} WHERE id = ?", (archive_id,)
        )
        row = await cursor.fetchone()
    if row is None:  # pragma: no cover - just inserted
        raise RuntimeError(f"backup archive {archive_id!r} was not recorded")
    return _archive_from_row(base.row_to_dict(row))


async def get_archive(db: Database, archive_id: str) -> ArchiveRow | None:
    async with db.read() as conn:
        cursor = await conn.execute(f"SELECT * FROM {ARCHIVES_TABLE} WHERE id = ?", (archive_id,))
        row = await cursor.fetchone()
    return None if row is None else _archive_from_row(base.row_to_dict(row))


async def list_archives(db: Database, *, limit: int = 100) -> list[ArchiveRow]:
    """Newest first — the shape ``GET /system/backup/history`` serves."""
    async with db.read() as conn:
        cursor = await conn.execute(
            f"SELECT * FROM {ARCHIVES_TABLE} ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
    return [_archive_from_row(base.row_to_dict(r)) for r in rows]


async def list_archives_present(db: Database, destination: str) -> list[ArchiveRow]:
    """Archives currently marked present at ``destination`` ('local'|'usb'|'network'),
    oldest first — the order retention pruning and eviction walk."""
    column = _destination_column(destination)
    async with db.read() as conn:
        cursor = await conn.execute(
            f"SELECT * FROM {ARCHIVES_TABLE} WHERE {column} = 1 ORDER BY created_at ASC"
        )
        rows = await cursor.fetchall()
    return [_archive_from_row(base.row_to_dict(r)) for r in rows]


def _destination_column(destination: str) -> str:
    column = {"local": "local_present", "usb": "usb_present", "network": "network_present"}.get(
        destination
    )
    if column is None:
        raise ValueError(f"unknown destination {destination!r}")
    return column


async def set_presence(db: Database, archive_id: str, destination: str, present: bool) -> None:
    """Flip one destination's presence flag — retention pruning and eviction call this."""
    column = _destination_column(destination)
    async with db.write() as conn:
        await conn.execute(
            f"UPDATE {ARCHIVES_TABLE} SET {column} = ? WHERE id = ?",
            (int(present), archive_id),
        )


async def delete_if_absent_everywhere(db: Database, archive_id: str) -> None:
    """Drop the row once every presence flag is false — nothing holds it any more."""
    async with db.write() as conn:
        await conn.execute(
            f"""
            DELETE FROM {ARCHIVES_TABLE}
            WHERE id = ? AND local_present = 0 AND usb_present = 0 AND network_present = 0
            """,
            (archive_id,),
        )


async def mark_verified(
    db: Database, archive_id: str, *, verified_at: str, untrusted: bool, reason: str | None
) -> None:
    """The monthly job's write: when it fails, ``reason`` is shown and the
    archive is never offered by a restore (§13.4)."""
    async with db.write() as conn:
        await conn.execute(
            f"""
            UPDATE {ARCHIVES_TABLE}
            SET verified_at = ?, untrusted = ?, untrusted_reason = ?
            WHERE id = ?
            """,
            (verified_at, int(untrusted), reason, archive_id),
        )


# -- the network destination ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DestinationRow:
    protocol: str | None  # 'smb'|'sftp', None = not configured
    host: str | None
    port: int | None
    path: str | None
    username: str | None
    password: dict[str, str] | None  # the encrypted {"enc": ...} form, SMB only
    enabled: bool
    updated_at: str | None
    updated_by: int | None


def _destination_from_row(row: base.Row) -> DestinationRow:
    password_raw = row.get("password")
    password: dict[str, str] | None = json.loads(password_raw) if password_raw else None
    port = row.get("port")
    updated_by = row.get("updated_by")
    return DestinationRow(
        protocol=row.get("protocol"),
        host=row.get("host"),
        port=None if port is None else int(port),
        path=row.get("path"),
        username=row.get("username"),
        password=password,
        enabled=bool(row.get("enabled")),
        updated_at=row.get("updated_at"),
        updated_by=None if updated_by is None else int(updated_by),
    )


async def get_destination(db: Database) -> DestinationRow | None:
    """The one row, or ``None`` when no network destination has ever been saved."""
    async with db.read() as conn:
        row = await base.get(conn, DESTINATION_TABLE, DESTINATION_ROW_ID)
    return None if row is None else _destination_from_row(row)


async def upsert_destination(
    db: Database,
    *,
    protocol: str | None,
    host: str | None,
    port: int | None,
    path: str | None,
    username: str | None,
    password: dict[str, str] | None,
    enabled: bool,
    updated_by: int | None,
) -> DestinationRow:
    """Replace the one row whole (the same "one settings form" shape as email_config)."""
    async with db.write() as conn:
        await conn.execute(
            f"""
            INSERT INTO {DESTINATION_TABLE}
                (id, protocol, host, port, path, username, password, enabled,
                 updated_at, updated_by)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                protocol = excluded.protocol,
                host = excluded.host,
                port = excluded.port,
                path = excluded.path,
                username = excluded.username,
                password = excluded.password,
                enabled = excluded.enabled,
                updated_at = excluded.updated_at,
                updated_by = excluded.updated_by
            """,
            (
                protocol,
                host,
                port,
                path,
                username,
                None if password is None else json.dumps(password),
                int(enabled),
                base.now_iso(),
                updated_by,
            ),
        )
        row = await base.get(conn, DESTINATION_TABLE, DESTINATION_ROW_ID)
    if row is None:  # pragma: no cover - the upsert above always leaves a row
        raise RuntimeError("backup_destination upsert left no row")
    return _destination_from_row(row)
