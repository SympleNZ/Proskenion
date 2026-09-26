"""A logical fingerprint of the venue's database, taken before and after the soak.

The soak never opens the venue's database — the soak instance runs on its own
— and this is how that is shown rather than asserted. ``cm5.sh`` takes a
fingerprint with the service stopped, before the soak and again after it,
and compares them table by table.

Rows, not bytes: the file's bytes can change without its content changing
(a checkpoint, a vacuum), and the nightly backup job, which keeps running
through the soak (``auditorium-backup.timer``), does legitimately record its
run in two tables. Those two are named in :data:`EXPECTED_TO_CHANGE` and
reported, not failed.

The database is opened read-only and ``immutable`` — nothing is written, not
even a ``-shm`` file — which is only safe while nothing else has it open:
``cm5.sh`` stops the service first.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Final

#: The nightly backup job's own record (§13.4): allowed to change during a soak.
EXPECTED_TO_CHANGE: Final = frozenset({"system_state", "backup_archives"})


def fingerprint(database: Path) -> dict[str, dict[str, object]]:
    uri = f"file:{database.as_posix()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        result: dict[str, dict[str, object]] = {}
        for table in tables:
            digest = hashlib.sha256()
            count = 0
            quoted = '"' + table.replace('"', '""') + '"'
            for row in connection.execute(f"SELECT * FROM {quoted} ORDER BY rowid"):
                digest.update(repr(row).encode("utf-8"))
                count += 1
            result[table] = {"rows": count, "sha256": digest.hexdigest()}
        return result
    finally:
        connection.close()


def compare(
    before: dict[str, dict[str, object]], after: dict[str, dict[str, object]]
) -> dict[str, list[str]]:
    """``{"changed": [...], "expected": [...]}`` — tables that differ, split by whether they may."""
    differing = sorted(
        name for name in set(before) | set(after) if before.get(name) != after.get(name)
    )
    return {
        "changed": [n for n in differing if n not in EXPECTED_TO_CHANGE],
        "expected": [n for n in differing if n in EXPECTED_TO_CHANGE],
    }


def load(path: Path) -> dict[str, dict[str, object]]:
    data: dict[str, dict[str, object]] = json.loads(path.read_text(encoding="utf-8"))
    return data
