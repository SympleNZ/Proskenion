"""``system_images`` (contracts §5, §8, §13.6, Q13)."""

from __future__ import annotations

from proskenion.db.connection import Database
from proskenion.db.crud import images as images_crud


async def _image(db: Database, image_id: str, **overrides: object) -> images_crud.ImageRow:
    defaults: dict[str, object] = {
        "filename": f"{image_id}.img.gz",
        "created_at": "2026-09-20T14:30:00+12:00",
        "slot": "a",
        "version": "v1.3.0",
        "size_bytes": 1234,
        "sha256": "a" * 64,
        "key_id": "deadbeef",
        "local_present": True,
        "usb_present": False,
    }
    defaults.update(overrides)
    return await images_crud.record_image(db, image_id=image_id, **defaults)  # type: ignore[arg-type]


async def test_record_and_get_image(db: Database) -> None:
    row = await _image(db, "auditorium-v1.3.0-20260920-143000")
    assert row.id == "auditorium-v1.3.0-20260920-143000"
    assert row.filename == "auditorium-v1.3.0-20260920-143000.img.gz"
    assert row.local_present is True
    assert row.usb_present is False

    fetched = await images_crud.get_image(db, row.id)
    assert fetched == row
    assert await images_crud.get_image(db, "no-such-image") is None


async def test_record_image_upserts_by_id(db: Database) -> None:
    """A re-capture landing on the same id replaces the row, the way it
    replaces the file on disk (mirrors backup_crud.record_archive)."""
    await _image(db, "same-id", size_bytes=100)
    replaced = await _image(db, "same-id", size_bytes=200)
    assert replaced.size_bytes == 200
    fetched = await images_crud.get_image(db, "same-id")
    assert fetched is not None
    assert fetched.size_bytes == 200


async def test_list_images_is_newest_first(db: Database) -> None:
    await _image(db, "oldest", created_at="2026-09-18T00:00:00+12:00")
    await _image(db, "newest", created_at="2026-09-20T00:00:00+12:00")
    await _image(db, "middle", created_at="2026-09-19T00:00:00+12:00")

    rows = await images_crud.list_images(db)
    assert [r.id for r in rows] == ["newest", "middle", "oldest"]


async def test_list_images_present_filters_by_destination_oldest_first(db: Database) -> None:
    await _image(
        db, "local-only", created_at="2026-09-20T00:00:00+12:00",
        local_present=True, usb_present=False,
    )
    await _image(
        db, "both-older", created_at="2026-09-19T00:00:00+12:00",
        local_present=True, usb_present=True,
    )

    local_ids = [r.id for r in await images_crud.list_images_present(db, "local")]
    usb_ids = [r.id for r in await images_crud.list_images_present(db, "usb")]
    assert local_ids == ["both-older", "local-only"]  # oldest first
    assert usb_ids == ["both-older"]


async def test_set_presence_flips_one_flag(db: Database) -> None:
    row = await _image(db, "flip-me", local_present=True, usb_present=False)
    await images_crud.set_presence(db, row.id, "usb", True)
    fetched = await images_crud.get_image(db, row.id)
    assert fetched is not None
    assert fetched.local_present is True
    assert fetched.usb_present is True

    await images_crud.set_presence(db, row.id, "local", False)
    fetched = await images_crud.get_image(db, row.id)
    assert fetched is not None
    assert fetched.local_present is False
    assert fetched.usb_present is True


async def test_delete_if_absent_everywhere(db: Database) -> None:
    row = await _image(db, "cleanup", local_present=True, usb_present=True)

    await images_crud.set_presence(db, row.id, "local", False)
    await images_crud.delete_if_absent_everywhere(db, row.id)
    assert await images_crud.get_image(db, row.id) is not None, "still present on usb"

    await images_crud.set_presence(db, row.id, "usb", False)
    await images_crud.delete_if_absent_everywhere(db, row.id)
    assert await images_crud.get_image(db, row.id) is None
