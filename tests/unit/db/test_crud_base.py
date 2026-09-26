"""CRUD base helpers: timestamps, optimistic concurrency, delete protection, constraints."""

import re
import sqlite3
from datetime import datetime

import pytest

from proskenion.db.connection import Database
from proskenion.db.crud import base
from proskenion.db.crud.base import (
    ConflictError,
    InUseError,
    NotFoundError,
    next_version_stamp,
    now_iso,
)

ISO_WITH_OFFSET = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+1[23]:00$")


def test_now_iso_is_auckland_with_offset() -> None:
    stamp = now_iso()
    assert ISO_WITH_OFFSET.match(stamp), stamp
    parsed = datetime.fromisoformat(stamp)
    assert parsed.utcoffset() is not None


def test_next_version_stamp_never_repeats() -> None:
    previous = now_iso()
    assert next_version_stamp(None) != ""
    bumped = next_version_stamp(previous)
    assert bumped != previous
    assert datetime.fromisoformat(bumped) > datetime.fromisoformat(previous) or bumped > previous


async def test_insert_get_list(db: Database) -> None:
    async with db.write() as conn:
        new_id = await base.insert(conn, "lighting_bars", {"name": "Bar 2", "sort_order": 1})
        row = await base.get(conn, "lighting_bars", new_id)
    assert row == {
        "id": new_id,
        "name": "Bar 2",
        "sort_order": 1,
        "notes": None,
        # lighting_bars gained created_at/updated_at in 003_lighting_rules_scenes.sql
        # (§16.1 optimistic concurrency); the ALTER TABLE's DEFAULT fills them in
        # when, as here, a raw insert omits them.
        "created_at": "2026-01-01T00:00:00+13:00",
        "updated_at": "2026-01-01T00:00:00+13:00",
    }
    async with db.read() as conn:
        rows = await base.list_rows(conn, "lighting_bars", order_by="sort_order DESC")
        assert [r["name"] for r in rows] == ["Bar 2", "Proscenium"]
        rows = await base.list_rows(conn, "lighting_bars", where_sql="sort_order = ?", params=(0,))
        assert [r["name"] for r in rows] == ["Proscenium"]
        assert await base.get(conn, "lighting_bars", 999) is None
        with pytest.raises(ValueError):
            await base.list_rows(conn, "lighting_bars", order_by="id; DROP TABLE users")


async def test_update_with_version_accepts_the_current_stamp(db: Database) -> None:
    async with db.read() as conn:
        before = await base.get(conn, "users", 1)
    assert before is not None
    async with db.write() as conn:
        after = await base.update_with_version(
            conn, "users", 1, before["updated_at"], {"token_version": 5}
        )
    assert after["token_version"] == 5
    assert after["updated_at"] != before["updated_at"]


async def test_stale_updated_at_raises_conflict_with_current_row(db: Database) -> None:
    async with db.read() as conn:
        original = await base.get(conn, "users", 1)
    assert original is not None
    async with db.write() as conn:
        current = await base.update_with_version(
            conn, "users", 1, original["updated_at"], {"token_version": 1}
        )
    with pytest.raises(ConflictError) as excinfo:
        async with db.write() as conn:
            await base.update_with_version(
                conn, "users", 1, original["updated_at"], {"token_version": 2}
            )
    assert excinfo.value.current == current
    assert excinfo.value.current["token_version"] == 1
    assert (excinfo.value.table, excinfo.value.row_id) == ("users", 1)
    async with db.read() as conn:
        assert await base.get(conn, "users", 1) == current, "the stale write must not land"


async def test_update_with_version_missing_row_and_reserved_column(db: Database) -> None:
    with pytest.raises(NotFoundError):
        async with db.write() as conn:
            await base.update_with_version(conn, "users", 99, "x", {"token_version": 1})
    with pytest.raises(ValueError):
        async with db.write() as conn:
            await base.update_with_version(conn, "users", 1, "x", {"updated_at": "y"})


async def test_hirer_config_cannot_hold_a_second_row(db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        async with db.write() as conn:
            await base.insert(conn, "hirer_config", {"id": 2, "pin": "x", "updated_at": now_iso()})
    with pytest.raises(sqlite3.IntegrityError):
        async with db.write() as conn:
            await base.insert(conn, "hirer_config", {"pin": "x", "updated_at": now_iso()})
    async with db.read() as conn:
        assert len(await base.list_rows(conn, "hirer_config")) == 1


async def test_foreign_keys_are_enforced(db: Database) -> None:
    async with db.read() as conn:
        current = await base.get(conn, "hirer_config", 1)
    assert current is not None
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        async with db.write() as conn:
            await base.update_with_version(
                conn, "hirer_config", 1, current["updated_at"], {"updated_by": 4242}
            )


async def test_on_delete_set_null_clears_updated_by(db: Database) -> None:
    with pytest.raises(_Undo):
        async with db.write() as conn:
            await conn.execute("UPDATE hirer_config SET updated_by = 2 WHERE id = 1")
            await conn.execute("DELETE FROM users WHERE id = 2")
            row = await base.get(conn, "hirer_config", 1)
            assert row is not None and row["updated_by"] is None
            raise _Undo  # roll the unit back so the seed stays intact
    async with db.read() as conn:
        assert await base.get(conn, "users", 2) is not None, "the unit rolled back"


class _Undo(Exception):
    pass


async def test_on_delete_restrict_surfaces_as_in_use(db: Database) -> None:
    # No Phase 1 table carries ON DELETE RESTRICT yet; a scratch table stands in
    # for the KNX addresses and desk scenes that will (§15.1).
    async with db.write() as conn:
        await conn.execute(
            "CREATE TABLE scratch_refs ("
            " id INTEGER PRIMARY KEY,"
            " bar_id INTEGER NOT NULL REFERENCES lighting_bars(id) ON DELETE RESTRICT)"
        )
        await conn.execute("INSERT INTO scratch_refs (bar_id) VALUES (1)")
    with pytest.raises(InUseError) as excinfo:
        async with db.write() as conn:
            await base.delete_or_in_use(conn, "lighting_bars", 1)
    assert (excinfo.value.table, excinfo.value.row_id) == ("lighting_bars", 1)
    async with db.read() as conn:
        assert await base.get(conn, "lighting_bars", 1) is not None
    async with db.write() as conn:
        await conn.execute("DELETE FROM scratch_refs")
        await base.delete_or_in_use(conn, "lighting_bars", 1)
    with pytest.raises(NotFoundError):
        async with db.write() as conn:
            await base.delete_or_in_use(conn, "lighting_bars", 1)
