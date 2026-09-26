"""``backup_archives`` and ``backup_destination`` (contracts §5, §8)."""

from __future__ import annotations

from proskenion.db.connection import Database
from proskenion.db.crud import backup as backup_crud


async def _archive(db: Database, archive_id: str, **overrides: object) -> backup_crud.ArchiveRow:
    defaults: dict[str, object] = {
        "created_at": "2026-09-20T03:00:00+12:00",
        "source": "scheduled",
        "size_bytes": 1234,
        "sha256": "a" * 64,
        "schema_version": 8,
        "app_version": "0.1.0",
        "local_present": True,
        "usb_present": False,
        "network_present": False,
    }
    defaults.update(overrides)
    return await backup_crud.record_archive(db, archive_id=archive_id, **defaults)  # type: ignore[arg-type]


async def test_record_and_get_archive(db: Database) -> None:
    row = await _archive(db, "auditorium-20260920-0300")
    assert row.id == "auditorium-20260920-0300"
    assert row.local_present is True
    assert row.usb_present is False
    assert row.untrusted is False
    assert row.verified_at is None

    fetched = await backup_crud.get_archive(db, "auditorium-20260920-0300")
    assert fetched == row
    assert await backup_crud.get_archive(db, "no-such-archive") is None


async def test_list_archives_is_newest_first(db: Database) -> None:
    await _archive(db, "auditorium-20260918-0300", created_at="2026-09-18T03:00:00+12:00")
    await _archive(db, "auditorium-20260920-0300", created_at="2026-09-20T03:00:00+12:00")
    await _archive(db, "auditorium-20260919-0300", created_at="2026-09-19T03:00:00+12:00")

    rows = await backup_crud.list_archives(db)
    assert [r.id for r in rows] == [
        "auditorium-20260920-0300",
        "auditorium-20260919-0300",
        "auditorium-20260918-0300",
    ]


async def test_list_archives_present_filters_by_destination(db: Database) -> None:
    await _archive(db, "local-only", local_present=True, usb_present=False)
    await _archive(db, "both", local_present=True, usb_present=True)

    local_rows = {r.id for r in await backup_crud.list_archives_present(db, "local")}
    usb_rows = {r.id for r in await backup_crud.list_archives_present(db, "usb")}
    assert local_rows == {"local-only", "both"}
    assert usb_rows == {"both"}


async def test_set_presence_and_delete_if_absent_everywhere(db: Database) -> None:
    await _archive(db, "gone-soon", local_present=True, usb_present=True, network_present=False)

    await backup_crud.set_presence(db, "gone-soon", "usb", False)
    row = await backup_crud.get_archive(db, "gone-soon")
    assert row is not None
    assert row.usb_present is False
    assert row.local_present is True  # untouched

    # Still present locally: the row survives.
    await backup_crud.delete_if_absent_everywhere(db, "gone-soon")
    assert await backup_crud.get_archive(db, "gone-soon") is not None

    await backup_crud.set_presence(db, "gone-soon", "local", False)
    await backup_crud.delete_if_absent_everywhere(db, "gone-soon")
    assert await backup_crud.get_archive(db, "gone-soon") is None


async def test_mark_verified_records_untrusted_and_reason(db: Database) -> None:
    await _archive(db, "checked")
    await backup_crud.mark_verified(
        db,
        "checked",
        verified_at="2026-10-01T03:30:00+12:00",
        untrusted=True,
        reason="bad checksum",
    )
    row = await backup_crud.get_archive(db, "checked")
    assert row is not None
    assert row.verified_at == "2026-10-01T03:30:00+12:00"
    assert row.untrusted is True
    assert row.untrusted_reason == "bad checksum"


async def test_destination_absent_by_default(db: Database) -> None:
    assert await backup_crud.get_destination(db) is None


async def test_upsert_destination_replaces_the_one_row(db: Database) -> None:
    await backup_crud.upsert_destination(
        db,
        protocol="sftp",
        host="nas.school.nz",
        port=22,
        path="/backups",
        username="proskenion",
        password=None,
        enabled=True,
        updated_by=None,
    )
    row = await backup_crud.get_destination(db)
    assert row is not None
    assert row.protocol == "sftp"
    assert row.host == "nas.school.nz"
    assert row.password is None
    assert row.enabled is True

    await backup_crud.upsert_destination(
        db,
        protocol="smb",
        host="nas2.school.nz",
        port=445,
        path="backups",
        username="proskenion",
        password={"enc": "ciphertext"},
        enabled=False,
        updated_by=None,
    )
    replaced = await backup_crud.get_destination(db)
    assert replaced is not None
    assert replaced.protocol == "smb"
    assert replaced.host == "nas2.school.nz"
    assert replaced.password == {"enc": "ciphertext"}
    assert replaced.enabled is False
