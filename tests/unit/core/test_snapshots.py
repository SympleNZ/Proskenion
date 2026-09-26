"""The one pre-change snapshot helper (§18 Phase 7, §15.3, §2.3).

What matters: a snapshot is a whole, openable database with a sidecar saying
why it was taken; one that cannot be taken raises rather than being skipped;
two taken in one second never share a name; and it is quick enough to run
inline before a request's write on a database far larger than today's.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

import pytest

from proskenion import __version__
from proskenion.core import snapshots
from proskenion.db.connection import Database
from proskenion.db.crud import system_state
from proskenion.db.crud.base import AUCKLAND
from proskenion.db.migrations import migrate

NOW = datetime(2026, 9, 25, 14, 30, 5, tzinfo=AUCKLAND)


@pytest.fixture
async def file_db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database()
    await database.open(tmp_path / "auditorium.db")
    try:
        await migrate(database)
        yield database
    finally:
        await database.close()


def _value_in(snapshot: Path, domain: str, key: str) -> str | None:
    with sqlite3.connect(snapshot) as conn:
        row = conn.execute(
            "SELECT value FROM system_state WHERE domain = ? AND key = ?", (domain, key)
        ).fetchone()
    return None if row is None else str(row[0])


async def test_a_snapshot_is_the_database_as_it_was_with_a_sidecar(
    file_db: Database, tmp_path: Path
) -> None:
    await system_state.set(file_db, "venue", "name", "before")
    directory = tmp_path / "snapshots"

    info = await snapshots.take_pre_change_snapshot(
        file_db,
        directory,
        reason="delete scene 3",
        actor="admin",
        ip_address="10.2.30.10",
        now=lambda: NOW,
    )
    await system_state.set(file_db, "venue", "name", "after")

    assert info.name == "pre-change-20260925-143005.db"
    assert info.path == directory / info.name
    assert _value_in(info.path, "venue", "name") == "before"
    with sqlite3.connect(info.path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    sidecar = json.loads((directory / "pre-change-20260925-143005.json").read_text("utf-8"))
    assert sidecar == {
        "snapshot": info.name,
        "reason": "delete scene 3",
        "actor": "admin",
        "ip_address": "10.2.30.10",
        "taken_at": "2026-09-25T14:30:05+12:00",
        "app_version": __version__,
        "size_bytes": info.path.stat().st_size,
        "duration_ms": info.duration_ms,
    }
    assert snapshots.read_sidecar(info.path) == sidecar
    assert [p.name for p in directory.iterdir() if p.name.startswith(".")] == []


async def test_two_snapshots_in_one_second_never_share_a_name(
    file_db: Database, tmp_path: Path
) -> None:
    directory = tmp_path / "snapshots"
    taken = await asyncio.gather(
        *(
            snapshots.take_pre_change_snapshot(
                file_db, directory, reason=f"delete {n}", now=lambda: NOW
            )
            for n in range(3)
        )
    )
    names = sorted(info.name for info in taken)
    assert names == [
        "pre-change-20260925-143005-2.db",
        "pre-change-20260925-143005-3.db",
        "pre-change-20260925-143005.db",
    ]
    assert all(info.path.stat().st_size > 0 for info in taken)


async def test_a_snapshot_that_cannot_be_taken_raises_and_leaves_nothing(
    file_db: Database, tmp_path: Path
) -> None:
    """The caller must be able to refuse its change: nothing is skipped silently."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("a file where the snapshots directory should be", encoding="utf-8")

    with pytest.raises(snapshots.SnapshotFailed):
        await snapshots.take_pre_change_snapshot(
            file_db, blocker / "snapshots", reason="delete scene 3"
        )


async def test_a_failed_vacuum_removes_the_name_it_claimed(
    file_db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "snapshots"

    def fail(*_: object, **__: object) -> snapshots.SnapshotInfo:
        raise OSError("No space left on device")

    monkeypatch.setattr(snapshots, "_finish", fail)
    with pytest.raises(snapshots.SnapshotFailed, match="No space left"):
        await snapshots.take_pre_change_snapshot(file_db, directory, reason="delete scene 3")
    assert snapshots.list_snapshots(directory) == []


def test_the_standalone_path_has_nothing_to_snapshot_on_a_first_install(
    tmp_path: Path,
) -> None:
    target = tmp_path / "pre-update-v1.0.0.db"
    assert snapshots.take_snapshot_sync(tmp_path / "absent.db", target, reason="x") is None
    assert not target.exists()


def test_only_names_one_of_the_four_callers_could_write_are_snapshots() -> None:
    for name in (
        "pre-change-20260925-143005.db",
        "pre-restore-20260925-143005-2.db",
        "pre-update-v1.3.0.db",
    ):
        assert snapshots.is_snapshot_name(name)
    for name in (
        "auditorium.db",
        "pre-change-20260925-143005.json",
        "../pre-change-x.db",
        "pre-update-..db",
        "pre-change-a/b.db",
        ".pre-change-x.db.1234.tmp",
    ):
        assert not snapshots.is_snapshot_name(name)


# -- retention: the helper's half -------------------------------------------------


def _held(directory: Path, names: list[str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(names):
        path = directory / name
        path.write_bytes(b"x")
        path.with_name(path.stem + ".json").write_text("{}", encoding="utf-8")
        os.utime(path, (1_000 + index, 1_000 + index))


def test_prune_keeps_the_newest_across_every_prefix(tmp_path: Path) -> None:
    names = [
        *(f"pre-change-2026090{n}-000000.db" for n in range(1, 8)),
        "pre-restore-20260910-000000.db",
        "pre-update-v1.3.0.db",
        *(f"pre-change-2026091{n}-000000.db" for n in range(1, 4)),
    ]
    _held(tmp_path, names)
    (tmp_path / "pre-restore-20260910-000000-baselines").mkdir()

    removed = snapshots.prune_sync(tmp_path, keep=10)

    assert sorted(p.name for p in removed) == [
        "pre-change-20260901-000000.db",
        "pre-change-20260902-000000.db",
    ]
    assert not (tmp_path / "pre-change-20260901-000000.json").exists()
    assert len(snapshots.list_snapshots(tmp_path)) == 10

    removed = snapshots.prune_sync(tmp_path, keep=5)
    assert len(snapshots.list_snapshots(tmp_path)) == 5
    assert (tmp_path / "pre-restore-20260910-000000-baselines").is_dir()
    assert {p.name for p in snapshots.list_snapshots(tmp_path)} == {
        "pre-restore-20260910-000000.db",
        "pre-update-v1.3.0.db",
        "pre-change-20260911-000000.db",
        "pre-change-20260912-000000.db",
        "pre-change-20260913-000000.db",
    }


def test_prune_never_removes_a_protected_snapshot(tmp_path: Path) -> None:
    names = [f"pre-change-202609{n:02d}-000000.db" for n in range(1, 13)]
    _held(tmp_path, names)
    oldest = names[0]

    snapshots.prune_sync(tmp_path, keep=5, protect={oldest})

    held = {p.name for p in snapshots.list_snapshots(tmp_path)}
    assert oldest in held
    assert len(held) == 6


def test_what_an_update_relies_on(tmp_path: Path) -> None:
    _held(
        tmp_path,
        ["pre-update-v1.1.0.db", "pre-update-v1.2.0.db", "pre-change-20260925-000000.db"],
    )
    boot_state = tmp_path / "boot-state.json"
    recorded = str(tmp_path / "pre-update-v1.1.0.db")
    boot_state.write_text(
        json.dumps({"update": {"to": "v1.2.0", "snapshot": recorded}}), encoding="utf-8"
    )
    assert snapshots.update_relies_on(tmp_path, boot_state) == {
        "pre-update-v1.1.0.db",
        "pre-update-v1.2.0.db",
    }
    assert snapshots.update_relies_on(tmp_path, tmp_path / "absent.json") == {
        "pre-update-v1.2.0.db"
    }


# -- fast enough to run inline ----------------------------------------------------


async def test_a_snapshot_of_a_forty_megabyte_database_is_quick(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CM5's database is about 300 KB today; plan for tens of megabytes.

    The bound is loose on purpose — this runs on developer machines and CI —
    but a regression to something that holds a request for seconds fails it.
    The measured time is printed for the record.
    """
    path = tmp_path / "large.db"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("CREATE TABLE filler (id INTEGER PRIMARY KEY, ts TEXT, detail TEXT)")
        rows = (40 * 1024 * 1024) // 260
        conn.executemany(
            "INSERT INTO filler (ts, detail) VALUES (?, ?)",
            ((f"2026-09-{n % 28 + 1:02d}", "x" * 200) for n in range(rows)),
        )
    database = Database()
    await database.open(path)
    try:
        started = time.perf_counter()
        info = await snapshots.take_pre_change_snapshot(
            database, tmp_path / "snapshots", reason="timing"
        )
        elapsed = time.perf_counter() - started
    finally:
        await database.close()
    with capsys.disabled():
        print(
            f"\n    pre-change snapshot of {info.size_bytes / 1_048_576:.1f} MB: "
            f"{elapsed * 1000:.0f} ms"
        )
    assert info.size_bytes > 35 * 1024 * 1024
    assert elapsed < 5.0
