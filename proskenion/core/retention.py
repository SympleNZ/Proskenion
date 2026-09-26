"""Retention pruning (spec §15.3, §11.3).

§15.3 sets the normal retention: ninety days for ``scene_execution_log``,
``rule_execution_log`` and ``security_events``, ninety days for the file logs
in ``/data/logs`` (logrotate's job, §4.10), and the ten most recent pre-change
snapshots. §11.3 halves those to thirty days, and five snapshots, once ``/data``
falls below 500 MB free.

Deletes go through :meth:`proskenion.db.connection.Database.chunked_delete`,
which deletes by rowid range in bounded batches and releases the write lock
between them (§15.1). Pruning runs when the system is already under stress, so
it must never hold the lock long enough to stall a fader move.

When it runs
------------
§15.3 says "during the nightly backup job, or earlier under disk pressure".
:meth:`proskenion.core.backup.BackupJob.run` calls :func:`prune` after every
nightly run, and the health poller calls it with ``under_pressure=True`` when
free space crosses §11.3's threshold. Both pass the snapshots directory, so
the pre-change snapshots are pruned by the same call as the tables.

Phase 1 ships one of the three tables. ``scene_execution_log`` and
``rule_execution_log`` arrive with the scene engine and the rule engine in
Phase 2; their entries are declared in :data:`TABLES` and skipped until the
table exists, so Phase 2 gets its retention by creating the table.

One row of §11.3 is deliberately not here: the application log files in
``/data/logs`` are logrotate's (§4.10). The pre-change snapshots are: ten
kept, five under pressure (:func:`snapshots_kept`), across every kind of
snapshot in ``/data/backups/snapshots``, never removing one a rollback or the
Backup screen's undo still relies on (:mod:`proskenion.core.snapshots`).
Venue baselines live elsewhere and are never pruned.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from proskenion.core import snapshots
from proskenion.db.connection import DEFAULT_DELETE_BATCH, Database
from proskenion.db.crud import system_state
from proskenion.db.crud.base import AUCKLAND

log = logging.getLogger(__name__)

#: §15.3's normal retention, in days.
RETENTION_DAYS = 90
#: §11.3's reduced retention under disk pressure, in days.
PRESSURE_RETENTION_DAYS = 30
#: §15.3 and §11.3 on pre-change snapshots: ten normally, five under pressure.
SNAPSHOT_KEEP = 10
PRESSURE_SNAPSHOT_KEEP = 5


@dataclass(frozen=True, slots=True)
class Table:
    """One table pruned by timestamp, and the phase that creates it."""

    name: str
    #: The column holding the ISO 8601 timestamp; text comparison is the order.
    column: str
    phase: str


#: Every table §15.3 prunes by age. Order is the order they are pruned in.
TABLES: tuple[Table, ...] = (
    Table("security_events", "timestamp", "Phase 1"),
    Table("scene_execution_log", "started_at", "Phase 2 — scene engine"),
    Table("rule_execution_log", "fired_at", "Phase 2 — rule engine"),
)


@dataclass(frozen=True, slots=True)
class PruneResult:
    """What one prune deleted, by table."""

    cutoff: str
    under_pressure: bool
    deleted: Mapping[str, int] = field(default_factory=dict)
    skipped: tuple[str, ...] = ()
    #: The pre-change snapshots removed, by file name.
    snapshots_removed: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return sum(self.deleted.values())


def retention_days(*, under_pressure: bool = False) -> int:
    """§15.3's ninety days, or §11.3's thirty when ``/data`` is under pressure."""
    return PRESSURE_RETENTION_DAYS if under_pressure else RETENTION_DAYS


def snapshots_kept(*, under_pressure: bool = False) -> int:
    """§15.3's ten pre-change snapshots, or §11.3's five under pressure."""
    return PRESSURE_SNAPSHOT_KEEP if under_pressure else SNAPSHOT_KEEP


def cutoff_iso(days: int, *, now: datetime | None = None) -> str:
    """The ISO 8601 timestamp ``days`` before now, in Pacific/Auckland (§4.9).

    Stored timestamps are ISO 8601 with offset and compare as text, which is
    how ``idx_security_ts`` orders them, so a text cutoff is a correct bound.
    """
    moment = (now or datetime.now(tz=AUCKLAND)) - timedelta(days=days)
    return moment.astimezone(AUCKLAND).isoformat(timespec="microseconds")


async def table_exists(db: Database, name: str) -> bool:
    async with db.read() as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        )
        return await cursor.fetchone() is not None


async def prune_table(
    db: Database,
    table: Table,
    cutoff: str,
    *,
    batch: int = DEFAULT_DELETE_BATCH,
) -> int:
    """Delete rows of ``table`` older than ``cutoff``, in batches (§15.1)."""
    return await db.chunked_delete(
        table.name, f"{table.column} < ?", (cutoff,), batch=batch
    )


async def prune_security_events(
    db: Database,
    *,
    under_pressure: bool = False,
    now: datetime | None = None,
    batch: int = DEFAULT_DELETE_BATCH,
) -> int:
    """Prune the audit trail alone — ninety days, thirty under pressure."""
    cutoff = cutoff_iso(retention_days(under_pressure=under_pressure), now=now)
    return await prune_table(db, TABLES[0], cutoff, batch=batch)


async def snapshots_relied_on(
    db: Database, directory: Path, *, boot_state: Path | None = None
) -> set[str]:
    """Every snapshot something could still restore: never pruned.

    The update's rollback snapshot (``boot-state.json`` and the newest
    ``pre-update-``, §14.3) and the last backup restore's undo (§21.24).
    """
    names = await asyncio.to_thread(snapshots.update_relies_on, directory, boot_state)
    if await table_exists(db, "system_state"):
        raw = await system_state.get_value(db, *snapshots.RESTORE_RECORD)
        restore = snapshots.restore_record_snapshot(raw)
        if restore is not None:
            names.add(restore)
    return names


async def prune_snapshots(
    db: Database,
    directory: Path,
    *,
    under_pressure: bool = False,
    boot_state: Path | None = None,
    protect: Iterable[str] = (),
) -> list[Path]:
    """Keep ten pre-change snapshots, five under pressure, and every one in use."""
    protected = await snapshots_relied_on(db, directory, boot_state=boot_state)
    protected.update(protect)
    return await asyncio.to_thread(
        snapshots.prune_sync,
        directory,
        keep=snapshots_kept(under_pressure=under_pressure),
        protect=protected,
    )


async def prune(
    db: Database,
    *,
    under_pressure: bool = False,
    now: datetime | None = None,
    batch: int = DEFAULT_DELETE_BATCH,
    snapshots_dir: Path | None = None,
    boot_state: Path | None = None,
) -> PruneResult:
    """Prune every §15.3 table that exists, and the pre-change snapshots.
    Nightly, or under §11.3 pressure.

    A table a later phase has not created yet is skipped, not an error: the
    retention policy is declared once, here, and each phase inherits it by
    creating its table. The snapshots are pruned when ``snapshots_dir`` is
    given; ``boot_state`` is where the update record that protects a rollback
    snapshot is read from.
    """
    days = retention_days(under_pressure=under_pressure)
    cutoff = cutoff_iso(days, now=now)
    deleted: dict[str, int] = {}
    skipped: list[str] = []
    for table in TABLES:
        if not await table_exists(db, table.name):
            skipped.append(table.name)
            continue
        deleted[table.name] = await prune_table(db, table, cutoff, batch=batch)
    removed: list[Path] = []
    if snapshots_dir is not None:
        removed = await prune_snapshots(
            db, snapshots_dir, under_pressure=under_pressure, boot_state=boot_state
        )
    result = PruneResult(
        cutoff, under_pressure, deleted, tuple(skipped), tuple(p.name for p in removed)
    )
    log.info(
        "retention prune complete",
        extra={
            "days": days,
            "cutoff": cutoff,
            "under_pressure": under_pressure,
            "deleted": dict(deleted),
            "skipped": skipped,
            "snapshots_removed": list(result.snapshots_removed),
        },
    )
    return result
