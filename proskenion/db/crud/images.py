"""``system_images`` — the captured system image index (contracts §5, §8).

The same shape :mod:`proskenion.db.crud.backup` uses for archives, and for
the same reason: :func:`record_image` inserts a row once, per capture, and
:func:`set_presence` flips a destination's flag as retention prunes a copy or
the USB evicts one for space (:mod:`proskenion.core.backup_destinations`).
There is no network destination for images (Q4) — only
``local_present`` and ``usb_present``.
"""

from __future__ import annotations

from dataclasses import dataclass

from proskenion.db.connection import Database
from proskenion.db.crud import base

IMAGES_TABLE = "system_images"


@dataclass(frozen=True, slots=True)
class ImageRow:
    id: str
    filename: str
    created_at: str
    slot: str
    version: str
    size_bytes: int
    sha256: str
    key_id: str | None
    local_present: bool
    usb_present: bool

    @property
    def any_present(self) -> bool:
        return self.local_present or self.usb_present


def _image_from_row(row: base.Row) -> ImageRow:
    return ImageRow(
        id=str(row["id"]),
        filename=str(row["filename"]),
        created_at=str(row["created_at"]),
        slot=str(row["slot"]),
        version=str(row["version"]),
        size_bytes=int(row["size_bytes"]),
        sha256=str(row["sha256"]),
        key_id=row.get("key_id"),
        local_present=bool(row["local_present"]),
        usb_present=bool(row["usb_present"]),
    )


async def record_image(
    db: Database,
    *,
    image_id: str,
    filename: str,
    created_at: str,
    slot: str,
    version: str,
    size_bytes: int,
    sha256: str,
    key_id: str | None,
    local_present: bool,
    usb_present: bool,
) -> ImageRow:
    """Record one captured image. Upsert, like :func:`backup.record_archive`,
    so a re-capture that happens to land on the same id (same version, same
    second) replaces the row the way it replaces the file on disk."""
    async with db.write() as conn:
        await conn.execute(
            f"""
            INSERT INTO {IMAGES_TABLE}
                (id, filename, created_at, slot, version, size_bytes, sha256, key_id,
                 local_present, usb_present)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                filename = excluded.filename,
                created_at = excluded.created_at,
                slot = excluded.slot,
                version = excluded.version,
                size_bytes = excluded.size_bytes,
                sha256 = excluded.sha256,
                key_id = excluded.key_id,
                local_present = excluded.local_present,
                usb_present = excluded.usb_present
            """,
            (
                image_id,
                filename,
                created_at,
                slot,
                version,
                size_bytes,
                sha256,
                key_id,
                int(local_present),
                int(usb_present),
            ),
        )
        cursor = await conn.execute(f"SELECT * FROM {IMAGES_TABLE} WHERE id = ?", (image_id,))
        row = await cursor.fetchone()
    if row is None:  # pragma: no cover - just inserted
        raise RuntimeError(f"system image {image_id!r} was not recorded")
    return _image_from_row(base.row_to_dict(row))


async def get_image(db: Database, image_id: str) -> ImageRow | None:
    async with db.read() as conn:
        cursor = await conn.execute(f"SELECT * FROM {IMAGES_TABLE} WHERE id = ?", (image_id,))
        row = await cursor.fetchone()
    return None if row is None else _image_from_row(base.row_to_dict(row))


async def list_images(db: Database, *, limit: int = 100) -> list[ImageRow]:
    """Newest first — the shape ``GET /system/images`` serves."""
    async with db.read() as conn:
        cursor = await conn.execute(
            f"SELECT * FROM {IMAGES_TABLE} ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
    return [_image_from_row(base.row_to_dict(r)) for r in rows]


async def list_images_present(db: Database, destination: str) -> list[ImageRow]:
    """Images currently marked present at ``destination`` ('local'|'usb'),
    oldest first — the order retention walks (Q4: newest 3 local, 2 USB)."""
    column = _destination_column(destination)
    async with db.read() as conn:
        cursor = await conn.execute(
            f"SELECT * FROM {IMAGES_TABLE} WHERE {column} = 1 ORDER BY created_at ASC"
        )
        rows = await cursor.fetchall()
    return [_image_from_row(base.row_to_dict(r)) for r in rows]


def _destination_column(destination: str) -> str:
    column = {"local": "local_present", "usb": "usb_present"}.get(destination)
    if column is None:
        raise ValueError(f"unknown image destination {destination!r}")
    return column


async def set_presence(db: Database, image_id: str, destination: str, present: bool) -> None:
    column = _destination_column(destination)
    async with db.write() as conn:
        await conn.execute(
            f"UPDATE {IMAGES_TABLE} SET {column} = ? WHERE id = ?",
            (int(present), image_id),
        )


async def delete_if_absent_everywhere(db: Database, image_id: str) -> None:
    async with db.write() as conn:
        await conn.execute(
            f"""
            DELETE FROM {IMAGES_TABLE}
            WHERE id = ? AND local_present = 0 AND usb_present = 0
            """,
            (image_id,),
        )
