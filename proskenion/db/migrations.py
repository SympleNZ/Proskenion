"""Migration runner (spec §15.2).

Files are ``NNN_description.sql`` in ``migrations/forward/``, each with a
counterpart in ``migrations/reverse/``. They ship inside the package and are
located relative to this module.

At startup, before any other database operation, :func:`migrate`:

1. creates ``schema_versions`` if absent;
2. lists forward migrations sorted by numeric prefix;
3. for each not yet recorded: begins a transaction, executes the script,
   inserts the ``(migration, applied_at)`` row, commits;
4. on any failure rolls back, logs, and raises :class:`MigrationFailed`. The
   caller exits with :data:`EXIT_MIGRATION_FAILED` (2) — distinct from other
   startup failures so the automatic rollback in §14.5 can identify the cause.

Schema version guard: if the highest recorded version is newer than the highest
shipped migration, the database is ahead of the code (a failed rollback or
manual intervention) and :class:`SchemaAhead` is raised. Reverse migrations
(:func:`revert_to`) are for development only; production rollback uses the
pre-update snapshot (§14.3).

Seed numbering: §15.2 names the seed ``000_seed.sql`` and says it runs first,
but the seed needs tables. Accepted deviation (phase-1 plan, Q2):
``001_schema.sql`` creates the schema and ``002_seed.sql`` inserts the seed.

Table rebuilds (§15.10): SQLite has no ``ALTER TABLE ... ADD CONSTRAINT``, so
adding or removing a foreign key means rebuilding the table — SQLite's
documented twelve-step procedure. A migration that needs this marks itself
with :data:`FK_REBUILD_MARKER` as the first line of its script; the runner
then runs it through :meth:`~proskenion.db.connection.Database.
write_no_fk_enforcement` instead of :meth:`~proskenion.db.connection.Database.
write`, and checks ``PRAGMA foreign_key_check`` before recording (or
un-recording, for a reverse script) the migration, raising
:class:`ForeignKeyViolation` — which :func:`migrate` and :func:`revert_to`
wrap in :class:`MigrationFailed` like any other failure — if the rebuild left
anything broken.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import aiosqlite

from proskenion.db.connection import Database
from proskenion.db.crud.base import now_iso

log = logging.getLogger(__name__)

EXIT_MIGRATION_FAILED = 2
"""Process exit code when a migration fails or the schema is ahead (§15.2)."""

SCHEMA_AHEAD_MESSAGE = (
    "The database schema is newer than this application version expects. "
    "A rollback or restore is required."
)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
FORWARD = "forward"
REVERSE = "reverse"

MIGRATION_NAME = re.compile(r"^(?P<version>\d{3,})_[A-Za-z0-9_]+\.sql$")

CREATE_SCHEMA_VERSIONS = """
CREATE TABLE IF NOT EXISTS schema_versions (
  migration  TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL
)
"""


class MigrationError(Exception):
    """Base class for runner failures."""


class MigrationFailed(MigrationError):
    """A migration script failed; the transaction was rolled back."""

    def __init__(self, migration: str, cause: BaseException) -> None:
        super().__init__(f"migration {migration} failed: {cause}")
        self.migration = migration


class SchemaAhead(MigrationError):
    """The database records a migration newer than any this build ships."""

    def __init__(self, recorded: int, shipped: int) -> None:
        super().__init__(SCHEMA_AHEAD_MESSAGE)
        self.recorded = recorded
        self.shipped = shipped


class ForeignKeyViolation(MigrationError):
    """``PRAGMA foreign_key_check`` found violations after a script that ran
    with foreign key enforcement disabled for a table rebuild (§15.2). The
    transaction is rolled back and nothing is recorded; the caller wraps this
    in :class:`MigrationFailed`, same as any other failed script."""

    def __init__(self, migration: str, violations: list[tuple[Any, ...]]) -> None:
        super().__init__(
            f"migration {migration} left {len(violations)} foreign key "
            f"violation(s) after the rebuild: {violations}"
        )
        self.migration = migration
        self.violations = violations


FK_REBUILD_MARKER = "-- proskenion:rebuild-without-foreign-key-enforcement"
"""First line a migration script must carry to run under
:meth:`~proskenion.db.connection.Database.write_no_fk_enforcement` instead of
:meth:`~proskenion.db.connection.Database.write` (see the module docstring).
"""


def version_of(name: str) -> int:
    """Numeric prefix of a migration filename, e.g. ``003_add_bars.sql`` → 3."""
    match = MIGRATION_NAME.match(name)
    if match is None:
        raise ValueError(f"not a migration filename: {name!r}")
    return int(match.group("version"))


def shipped_migrations(migrations_dir: Path = MIGRATIONS_DIR) -> list[Path]:
    """Forward migration files sorted by numeric prefix."""
    forward = migrations_dir / FORWARD
    if not forward.is_dir():
        return []
    files = [p for p in forward.iterdir() if p.is_file() and MIGRATION_NAME.match(p.name)]
    files.sort(key=lambda p: (version_of(p.name), p.name))
    versions = [version_of(p.name) for p in files]
    if len(set(versions)) != len(versions):
        raise MigrationError(f"duplicate migration version in {forward}")
    return files


async def ensure_schema_versions(db: Database) -> None:
    async with db.write() as conn:
        await conn.execute(CREATE_SCHEMA_VERSIONS)


async def applied_versions(db: Database) -> list[str]:
    """Recorded migration filenames, sorted by numeric prefix.

    Returns an empty list when ``schema_versions`` does not exist yet.
    """
    async with db.read() as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_versions'"
        )
        if await cursor.fetchone() is None:
            return []
        cursor = await conn.execute("SELECT migration FROM schema_versions")
        names = [str(row["migration"]) for row in await cursor.fetchall()]
    return sorted(names, key=_sort_key)


def _sort_key(name: str) -> tuple[int, str]:
    match = MIGRATION_NAME.match(name)
    return (int(match.group("version")) if match else -1, name)


def _highest(names: list[str]) -> int:
    versions = [version_of(n) for n in names if MIGRATION_NAME.match(n)]
    return max(versions, default=0)


def check_schema_ahead(applied: list[str], shipped: list[Path]) -> None:
    """Raise :class:`SchemaAhead` if the database is newer than the code."""
    recorded = _highest(applied)
    available = _highest([p.name for p in shipped])
    if recorded > available:
        raise SchemaAhead(recorded, available)


async def pending(db: Database, migrations_dir: Path = MIGRATIONS_DIR) -> list[Path]:
    """Shipped forward migrations not yet recorded, in application order."""
    applied = set(await applied_versions(db))
    return [p for p in shipped_migrations(migrations_dir) if p.name not in applied]


def _requires_fk_rebuild(script: str) -> bool:
    """Whether ``script`` must run with foreign key enforcement disabled
    around it — declared by :data:`FK_REBUILD_MARKER` as its first line."""
    stripped = script.lstrip()
    if not stripped:
        return False
    return stripped.splitlines()[0].strip() == FK_REBUILD_MARKER


async def _foreign_key_violations(conn: aiosqlite.Connection) -> list[tuple[Any, ...]]:
    cursor = await conn.execute("PRAGMA foreign_key_check")
    return [tuple(row) for row in await cursor.fetchall()]


async def _run_migration_script(
    db: Database,
    name: str,
    script: str,
    record: Callable[[aiosqlite.Connection], Awaitable[None]],
) -> None:
    """Execute one migration or reverse script as a single transactional
    unit, then run ``record`` (the ``schema_versions`` insert or delete)
    inside the same unit.

    A script marked with :data:`FK_REBUILD_MARKER` runs under
    :meth:`~proskenion.db.connection.Database.write_no_fk_enforcement`
    instead of :meth:`~proskenion.db.connection.Database.write`, and is
    followed by ``PRAGMA foreign_key_check`` before ``record`` runs — steps
    10 and 11 of SQLite's documented twelve-step table-rebuild procedure.
    Anything the check finds raises :class:`ForeignKeyViolation`, which rolls
    the unit back instead of recording the migration.
    """
    if _requires_fk_rebuild(script):
        async with db.write_no_fk_enforcement() as conn:
            await conn.executescript(script)
            violations = await _foreign_key_violations(conn)
            if violations:
                raise ForeignKeyViolation(name, violations)
            await record(conn)
    else:
        async with db.write() as conn:
            await conn.executescript(script)
            await record(conn)


async def migrate(db: Database, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply every pending forward migration. Returns the filenames applied."""
    await ensure_schema_versions(db)
    shipped = shipped_migrations(migrations_dir)
    applied = await applied_versions(db)
    check_schema_ahead(applied, shipped)

    done: list[str] = []
    recorded = set(applied)
    for path in shipped:
        if path.name in recorded:
            continue
        script = path.read_text(encoding="utf-8")
        applied_at = now_iso()

        async def _record(
            conn: aiosqlite.Connection, name: str = path.name, applied_at: str = applied_at
        ) -> None:
            await conn.execute(
                "INSERT INTO schema_versions (migration, applied_at) VALUES (?, ?)",
                (name, applied_at),
            )

        try:
            await _run_migration_script(db, path.name, script, _record)
        except Exception as exc:
            log.error("migration %s failed and was rolled back: %s", path.name, exc)
            raise MigrationFailed(path.name, exc) from exc
        log.info("applied migration %s", path.name)
        done.append(path.name)
    return done


async def revert_to(db: Database, version: int, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Development only: apply reverse scripts until ``version`` is the highest.

    Every recorded migration with a numeric prefix above ``version`` is
    reverted in descending order, each in its own transaction that also removes
    its ``schema_versions`` row. ``version=0`` reverts everything. Returns the
    filenames reverted.
    """
    applied = await applied_versions(db)
    to_revert = [n for n in reversed(applied) if version_of(n) > version]
    done: list[str] = []
    for name in to_revert:
        path = migrations_dir / REVERSE / name
        if not path.is_file():
            raise MigrationFailed(name, FileNotFoundError(f"no reverse script at {path}"))
        script = path.read_text(encoding="utf-8")

        async def _unrecord(conn: aiosqlite.Connection, name: str = name) -> None:
            await conn.execute("DELETE FROM schema_versions WHERE migration = ?", (name,))

        try:
            await _run_migration_script(db, name, script, _unrecord)
        except Exception as exc:
            log.error("reverse migration %s failed and was rolled back: %s", name, exc)
            raise MigrationFailed(name, exc) from exc
        log.info("reverted migration %s", name)
        done.append(name)
    return done


async def dry_run(path: Path, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply every pending migration to ``path`` and report what ran (§14.2).

    ``path`` is a **copy** of the live database. The update process takes the
    copy with SQLite's online backup API and runs this against it in the new
    version's environment, because the runner that will run at startup is the
    only one whose opinion matters. A failure here means the update stops
    before anything has been changed; it never touches the live database.
    """
    db = Database()
    await db.open(path)
    try:
        return await migrate(db, migrations_dir)
    finally:
        await db.close()


def main(argv: list[str] | None = None) -> int:
    """``python -m proskenion.db.migrations --check <database>``.

    Exit codes match a startup's: 0 clean, :data:`EXIT_MIGRATION_FAILED` for a
    migration failure or a schema ahead of the code (§15.2), 1 for anything
    else. The result is printed as one JSON object so the caller reports the
    reason rather than a shrug.
    """
    import argparse
    import asyncio
    import json
    import sys

    parser = argparse.ArgumentParser(prog="proskenion.db.migrations")
    parser.add_argument(
        "--check",
        required=True,
        type=Path,
        metavar="DATABASE",
        help="apply this database's pending migrations; it must be a copy",
    )
    options = parser.parse_args(argv)
    try:
        applied = asyncio.run(dry_run(options.check))
    except MigrationError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr, flush=True)
        return EXIT_MIGRATION_FAILED
    except Exception as exc:  # noqa: BLE001 - the caller wants the reason, not a traceback
        print(json.dumps({"ok": False, "error": repr(exc)}), file=sys.stderr, flush=True)
        return 1
    print(json.dumps({"ok": True, "applied": applied}), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - a subprocess entry point
    raise SystemExit(main())
