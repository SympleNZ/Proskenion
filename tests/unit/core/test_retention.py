"""Retention pruning (spec §15.3, §11.3, §15.1).

The point of these tests is not that rows disappear — it is that they
disappear ninety days late, thirty days late under pressure, and in batches
that give the write lock back in between.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from proskenion.core import retention, snapshots
from proskenion.db.connection import Database
from proskenion.db.crud import security_events, system_state
from proskenion.db.crud.base import AUCKLAND

NOW = datetime(2026, 9, 10, 19, 42, tzinfo=AUCKLAND)


def days_ago(days: float) -> str:
    return (NOW - timedelta(days=days)).isoformat(timespec="microseconds")


async def add_events(db: Database, ages_in_days: list[float]) -> None:
    for age in ages_in_days:
        await security_events.insert(db, "login_failed", timestamp=days_ago(age))


async def remaining(db: Database) -> int:
    async with db.read() as conn:
        cursor = await conn.execute("SELECT count(*) FROM security_events")
        row = await cursor.fetchone()
        return 0 if row is None else int(row[0])


# -- thresholds ---------------------------------------------------------------


def test_retention_thresholds_match_the_spec() -> None:
    assert retention.retention_days() == 90  # §15.3
    assert retention.retention_days(under_pressure=True) == 30  # §11.3
    assert retention.snapshots_kept() == 10
    assert retention.snapshots_kept(under_pressure=True) == 5


def test_cutoff_is_iso_8601_with_offset() -> None:
    cutoff = retention.cutoff_iso(90, now=NOW)
    assert cutoff.startswith("2026-06-12T")
    assert cutoff.endswith("+12:00")


# -- pruning ------------------------------------------------------------------


async def test_security_events_older_than_ninety_days_are_pruned(db: Database) -> None:
    await add_events(db, [200.0, 91.0, 89.0, 1.0])
    deleted = await retention.prune_security_events(db, now=NOW)
    assert deleted == 2
    assert await remaining(db) == 2
    kept = await security_events.query(db)
    assert all(event.timestamp > retention.cutoff_iso(90, now=NOW) for event in kept)


async def test_disk_pressure_prunes_to_thirty_days(db: Database) -> None:
    await add_events(db, [89.0, 31.0, 29.0])
    deleted = await retention.prune_security_events(db, under_pressure=True, now=NOW)
    assert deleted == 2
    assert await remaining(db) == 1


async def test_prune_reports_every_table_now_phase_2_has_created_them(
    db: Database,
) -> None:
    # retention.py's TABLES entry for scene_execution_log and rule_execution_log
    # predates their table: "declared now so Phase 2 inherits the policy by
    # creating the table." 003_lighting_rules_scenes.sql now creates both, so
    # prune() finds them (empty) rather than skipping them.
    await add_events(db, [200.0, 1.0])
    result = await retention.prune(db, now=NOW)
    assert result.deleted == {
        "security_events": 1,
        "scene_execution_log": 0,
        "rule_execution_log": 0,
    }
    assert result.total == 1
    assert result.under_pressure is False
    assert result.skipped == ()


async def test_nothing_to_prune_is_not_an_error(db: Database) -> None:
    await add_events(db, [1.0, 2.0])
    result = await retention.prune(db, now=NOW)
    assert result.total == 0
    assert await remaining(db) == 2


# -- §15.1: the write lock is released between batches -------------------------


async def test_chunked_delete_yields_the_write_lock_between_batches(db: Database) -> None:
    await add_events(db, [100.0 + n for n in range(20)])
    interleaved: list[str] = []

    async def control_traffic() -> None:
        """A write that must not have to wait out the whole prune (§15.1)."""
        for _ in range(4):
            async with db.write() as conn:
                await conn.execute(
                    "INSERT INTO system_state (domain, key, value, updated_at, source) "
                    "VALUES ('timer', ?, '1', '2026-09-10T19:42:00+12:00', 'test')",
                    (f"probe-{len(interleaved)}",),
                )
            interleaved.append("write")
            await asyncio.sleep(0)

    async def prune() -> None:
        interleaved.append("prune-start")
        await retention.prune_security_events(db, now=NOW, batch=2)
        interleaved.append("prune-end")

    await asyncio.gather(prune(), control_traffic())

    assert await remaining(db) == 0
    # The control writes landed while the prune was still running: the lock was
    # given back between batches rather than held for the whole delete.
    assert interleaved.index("write") < interleaved.index("prune-end")
    assert interleaved.count("write") == 4


async def test_prune_uses_batches_rather_than_one_statement(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    await add_events(db, [100.0 + n for n in range(10)])
    batches = 0
    original = Database.chunked_delete

    async def counting(self: Database, *args: object, **kwargs: object) -> int:
        nonlocal batches
        batches += 1
        return await original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Database, "chunked_delete", counting)
    deleted = await retention.prune_security_events(db, now=NOW, batch=3)
    assert deleted == 10
    assert batches == 1  # one call, which itself deletes in batches of three
    assert await remaining(db) == 0


# -- pre-change snapshots (§15.3: ten most recent; §11.3: five under pressure) --


def hold_snapshots(directory: Path, names: list[str]) -> None:
    """Snapshot files, oldest first, each with its sidecar."""
    directory.mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(names):
        path = directory / name
        path.write_bytes(b"x")
        path.with_name(path.stem + ".json").write_text("{}", encoding="utf-8")
        os.utime(path, (1_000 + index, 1_000 + index))


def held(directory: Path) -> set[str]:
    return {p.name for p in snapshots.list_snapshots(directory)}


def twelve_pre_change() -> list[str]:
    return [f"pre-change-202609{n:02d}-000000.db" for n in range(1, 13)]


async def test_prune_keeps_the_ten_most_recent_snapshots(db: Database, tmp_path: Path) -> None:
    directory = tmp_path / "backups" / "snapshots"
    names = twelve_pre_change()
    hold_snapshots(directory, names)

    result = await retention.prune(db, now=NOW, snapshots_dir=directory)

    assert held(directory) == set(names[2:])
    assert set(result.snapshots_removed) == set(names[:2])


async def test_under_pressure_only_five_are_kept(db: Database, tmp_path: Path) -> None:
    directory = tmp_path / "backups" / "snapshots"
    names = twelve_pre_change()
    hold_snapshots(directory, names)

    await retention.prune(db, now=NOW, under_pressure=True, snapshots_dir=directory)

    assert held(directory) == set(names[7:])


async def test_the_snapshots_a_rollback_and_an_undo_rely_on_are_never_pruned(
    db: Database, tmp_path: Path
) -> None:
    """Oldest of all, and still kept: the update record's snapshot, the newest
    pre-update one rollback falls back to, and the last restore's undo."""
    directory = tmp_path / "backups" / "snapshots"
    relied_on = [
        "pre-update-v1.1.0.db",
        "pre-update-v1.2.0.db",
        "pre-restore-20260801-000000.db",
    ]
    hold_snapshots(directory, [*relied_on, "pre-change-20260802-000000.db", *twelve_pre_change()])
    boot_state = tmp_path / "boot-state.json"
    boot_state.write_text(
        json.dumps({"update": {"to": "v1.3.0", "snapshot": str(directory / relied_on[0])}}),
        encoding="utf-8",
    )
    await system_state.set(
        db, *snapshots.RESTORE_RECORD, json.dumps({"snapshot": relied_on[2]}), source="test"
    )

    await retention.prune(
        db, now=NOW, under_pressure=True, snapshots_dir=directory, boot_state=boot_state
    )

    remaining_names = held(directory)
    assert set(relied_on) <= remaining_names
    assert "pre-change-20260802-000000.db" not in remaining_names
    assert len(remaining_names) == 5 + len(relied_on)


async def test_venue_baselines_are_never_touched(db: Database, tmp_path: Path) -> None:
    """§15.3: "Venue baselines — never auto-pruned"."""
    directory = tmp_path / "backups" / "snapshots"
    baselines = tmp_path / "config" / "baselines"
    baselines.mkdir(parents=True)
    for name in ("current.sqlite", "baseline-20250101-0900.sqlite", "pre-restore-x.sqlite"):
        (baselines / name).write_bytes(b"baseline")
    hold_snapshots(directory, twelve_pre_change())

    await retention.prune(db, now=NOW, under_pressure=True, snapshots_dir=directory)

    assert len(list(baselines.iterdir())) == 3


def test_the_restore_record_key_is_the_one_the_restore_writes() -> None:
    from proskenion.core import backup_restore

    assert snapshots.RESTORE_RECORD == (backup_restore.DOMAIN, backup_restore.KEY_RESTORE)


async def test_without_a_snapshots_directory_no_snapshot_is_touched(
    db: Database, tmp_path: Path
) -> None:
    directory = tmp_path / "backups" / "snapshots"
    hold_snapshots(directory, twelve_pre_change())
    result = await retention.prune(db, now=NOW)
    assert result.snapshots_removed == ()
    assert len(held(directory)) == 12
