"""The venue database's before-and-after fingerprint (tests/soak/fingerprint.py)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tests.soak import fingerprint


def _write(path: Path, *statements: str) -> None:
    connection = sqlite3.connect(path)
    try:
        for statement in statements:
            connection.execute(statement)
        connection.commit()
    finally:
        connection.close()


def _database(path: Path) -> None:
    _write(
        path,
        "CREATE TABLE devices (id INTEGER PRIMARY KEY, name TEXT)",
        "CREATE TABLE system_state (key TEXT, value TEXT)",
        "INSERT INTO devices (name) VALUES ('CQ-20B'), ('eDMX8')",
    )


def test_an_untouched_database_compares_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "auditorium.db"
    _database(path)
    before = fingerprint.fingerprint(path)
    assert before["devices"]["rows"] == 2
    assert fingerprint.compare(before, fingerprint.fingerprint(path)) == {
        "changed": [],
        "expected": [],
    }


def test_the_backup_jobs_own_tables_may_change_and_nothing_else_may(tmp_path: Path) -> None:
    path = tmp_path / "auditorium.db"
    _database(path)
    before = fingerprint.fingerprint(path)
    _write(path, "INSERT INTO system_state VALUES ('backup.last', 'ok')")
    assert fingerprint.compare(before, fingerprint.fingerprint(path)) == {
        "changed": [],
        "expected": ["system_state"],
    }
    _write(path, "UPDATE devices SET name = 'soak' WHERE id = 1")
    assert fingerprint.compare(before, fingerprint.fingerprint(path))["changed"] == ["devices"]
