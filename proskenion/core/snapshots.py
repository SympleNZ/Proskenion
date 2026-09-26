"""Pre-change database snapshots: one way to take them, one policy to keep them
(spec §2.3, §7.1, §13.5, §14.2, §15.3, §11.3, §18 Phase 7).

§18 asks for "pre-change snapshots on every destructive admin action", and
§2.3 gives them one home: ``/data/backups/snapshots``, "pre-change database
snapshots (retain 10)". Four things take one, and all four take it here:

* a destructive admin request (a delete, a KNX import) — ``pre-change-``;
* a venue baseline restore (§13.5) — ``pre-change-`` as well, beside the
  baseline-format copy the Backup screen offers as its own undo;
* a backup restore (§13.2, §21.24) — ``pre-restore-``, which the restore
  record names and the Backup screen passes back to undo it;
* an application update (§14.2) — ``pre-update-<version>``, which rollback
  finds by the version it replaced.

The prefixes are kept because something reads each of them by name; the
mechanism and the retention are not per-prefix any more.

How a snapshot is taken
-----------------------
``VACUUM INTO`` a file beside the target, then a rename, so nothing ever sees
a half-written snapshot. ``VACUUM INTO`` reads one committed state of the
database — under WAL a reader is never blocked by the writer and never
blocks it — so the copy is consistent without holding the write lock. It is
also compact: free pages are not copied. Beside the ``.db`` goes a ``.json``
sidecar with the reason, the actor, the time and the application version, so
a list of snapshots can say what each one is from.

The in-application path, :func:`take_pre_change_snapshot`, runs the
``VACUUM INTO`` on one of the database's own read connections: aiosqlite runs
it on that connection's thread, so the event loop keeps serving faders while
it happens. The standalone path, :func:`take_snapshot_sync`, opens a
read-only connection of its own; it is what the updater uses, because
:mod:`proskenion.core.update` must run from a plain interpreter with nothing
but the standard library.

When a snapshot cannot be taken, :class:`SnapshotFailed` is raised and the
action that asked for it does not happen. The spec does not say what to do
when the snapshot fails; refusing is what keeps "every destructive action is
reversible" true, and an admin who is told "nothing was changed" can free
space and try again, where one whose delete went through without a way back
cannot undo it.

Retention (§15.3, §11.3)
------------------------
Ten most recent, five under disk pressure, across **every** prefix: §15.3
has one row for pre-change snapshots, not one per caller. Pruning happens in
:func:`proskenion.core.retention.prune` — "during the nightly backup job, or
earlier under disk pressure" — and nowhere else, so taking a snapshot never
deletes one.

Never pruned, whatever their age:

* the snapshot the update record in ``boot-state.json`` names, and the newest
  ``pre-update-`` snapshot, which rollback falls back to (§14.3);
* the snapshot the last backup restore's record names — the Backup screen's
  "undo" (§21.24);
* anything a caller names as in use, such as the snapshot a restore is
  reading at that moment.

Venue baselines live in ``/data/config/baselines``, not here, and this module
never touches that directory (§15.3: "never auto-pruned").
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import sqlite3
import time
import uuid
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import ZoneInfo

from proskenion import __version__

if TYPE_CHECKING:
    from proskenion.db.connection import Database

log = logging.getLogger(__name__)

#: §2.3: ``/data/backups/snapshots``, relative to the data directory.
SNAPSHOTS_SUBDIR: Final = Path("backups") / "snapshots"

#: What a destructive admin action takes.
PRE_CHANGE_PREFIX: Final = "pre-change-"
#: What a backup restore takes; the restore record names it (§21.24).
PRE_RESTORE_PREFIX: Final = "pre-restore-"
#: What an update takes; rollback finds it by version (§14.3).
PRE_UPDATE_PREFIX: Final = "pre-update-"
PREFIXES: Final[tuple[str, ...]] = (PRE_CHANGE_PREFIX, PRE_RESTORE_PREFIX, PRE_UPDATE_PREFIX)

SNAPSHOT_SUFFIX: Final = ".db"
SIDECAR_SUFFIX: Final = ".json"
#: A backup restore also puts the baselines directory aside, under this suffix.
BASELINES_SUFFIX: Final = "-baselines"

#: ``YYYYMMDD-HHMMSS``, the stamp restore snapshots have always carried.
STAMP_FORMAT: Final = "%Y%m%d-%H%M%S"
AUCKLAND: Final = ZoneInfo("Pacific/Auckland")

#: ``system_state`` domain and key of the backup restore record, whose
#: ``snapshot`` is the Backup screen's undo. The same pair as
#: :data:`proskenion.core.backup_restore.DOMAIN` and ``KEY_RESTORE``, repeated
#: so this module stays importable without the restore machinery.
RESTORE_RECORD: Final[tuple[str, str]] = ("backup", "restore")


class SnapshotFailed(Exception):
    """A snapshot could not be taken, so the change that needed it must not run."""

    rule = "snapshot"
    summary = "A pre-change snapshot could not be taken, so nothing was changed."


@dataclass(frozen=True, slots=True)
class SnapshotInfo:
    """One snapshot and what its sidecar says about it."""

    name: str
    path: Path
    reason: str
    actor: str | None
    ip_address: str | None
    taken_at: str
    app_version: str
    size_bytes: int
    duration_ms: float

    def to_json(self) -> dict[str, Any]:
        return {
            "snapshot": self.name,
            "reason": self.reason,
            "actor": self.actor,
            "ip_address": self.ip_address,
            "taken_at": self.taken_at,
            "app_version": self.app_version,
            "size_bytes": self.size_bytes,
            "duration_ms": self.duration_ms,
        }


def snapshots_dir(data_dir: Path | str) -> Path:
    """``<data_dir>/backups/snapshots`` (§2.3)."""
    return Path(data_dir) / SNAPSHOTS_SUBDIR


def sidecar_of(snapshot: Path) -> Path:
    return snapshot.with_name(snapshot.name.removesuffix(SNAPSHOT_SUFFIX) + SIDECAR_SUFFIX)


def baselines_of(snapshot: Path) -> Path:
    return snapshot.with_name(snapshot.name.removesuffix(SNAPSHOT_SUFFIX) + BASELINES_SUFFIX)


def is_snapshot_name(name: str) -> bool:
    """A bare file name one of the four callers could have written."""
    return (
        name.startswith(PREFIXES)
        and name.endswith(SNAPSHOT_SUFFIX)
        and "/" not in name
        and "\\" not in name
        and ".." not in name
    )


def stamp(now: datetime) -> str:
    return now.astimezone(AUCKLAND).strftime(STAMP_FORMAT)


def reserve_path(directory: Path, prefix: str, when: str) -> Path:
    """Claim ``<prefix><when>.db``, or the first ``-2``, ``-3``… nobody holds.

    Two destructive requests inside one second share a stamp, and the second
    must not replace what the first put aside. The name is claimed by
    creating the file exclusively, so two requests racing for it cannot both
    win; the empty file is replaced by the snapshot, or removed if taking it
    fails.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(1, 1000):
        suffix = "" if index == 1 else f"-{index}"
        candidate = directory / f"{prefix}{when}{suffix}{SNAPSHOT_SUFFIX}"
        try:
            with open(candidate, "x"):
                pass
        except FileExistsError:
            continue
        return candidate
    raise SnapshotFailed(f"cannot find an unused snapshot name beside {prefix}{when}")


def _now() -> datetime:
    return datetime.now(tz=AUCKLAND)


def _staging(target: Path) -> Path:
    """A hidden name of its own beside ``target``, which no listing matches."""
    return target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")


def _finish(
    staged: Path,
    target: Path,
    *,
    reason: str,
    actor: str | None,
    ip_address: str | None,
    app_version: str,
    taken_at: datetime,
    started: float,
) -> SnapshotInfo:
    """Rename the staged copy into place and write its sidecar."""
    os.replace(staged, target)
    info = SnapshotInfo(
        name=target.name,
        path=target,
        reason=reason,
        actor=actor,
        ip_address=ip_address,
        taken_at=taken_at.astimezone(AUCKLAND).isoformat(timespec="seconds"),
        app_version=app_version,
        size_bytes=target.stat().st_size,
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )
    sidecar = sidecar_of(target)
    tmp = _staging(sidecar)
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(info.to_json(), handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, sidecar)
    return info


def vacuum_into_sync(source: str, uri: bool, target: Path) -> None:
    """``VACUUM INTO`` ``target`` on a read-only connection opened for it alone.

    Not on a pooled reader: ``VACUUM INTO`` refuses to run on a connection
    with any statement still in progress, and a pooled connection cannot
    promise it has none. A private in-memory database (tests) is reached by
    its shared-cache URI, which cannot also say ``mode=ro``; every file is
    opened read-only. ``VACUUM INTO`` refuses to overwrite, so ``target``
    must not exist.
    """
    address = source if uri else f"file:{source}?mode=ro"
    with contextlib.closing(sqlite3.connect(address, uri=True)) as conn:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("VACUUM INTO ?", (str(target),))


async def vacuum_into(db: Database, target: Path) -> None:
    """:func:`vacuum_into_sync` for an open :class:`~proskenion.db.connection.Database`,
    off the event loop."""
    source, uri = db.location
    await asyncio.to_thread(vacuum_into_sync, source, uri, target)


def take_snapshot_sync(
    database: Path,
    target: Path,
    *,
    reason: str,
    actor: str | None = None,
    ip_address: str | None = None,
    app_version: str = __version__,
    now: Callable[[], datetime] = _now,
) -> SnapshotInfo | None:
    """``VACUUM INTO`` ``target`` from a read-only connection of its own.

    For the callers that have no :class:`~proskenion.db.connection.Database`:
    the updater, which runs from a plain interpreter, and a restore whose
    database is about to be replaced. Returns ``None`` when there is no
    database yet — a first install — and raises :class:`SnapshotFailed` when
    there is one and it could not be copied. An existing ``target`` is
    replaced: an update re-applied to the same version snapshots again.
    """
    if not database.exists():
        return None
    started = time.perf_counter()
    taken_at = now()
    staged = _staging(target)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        vacuum_into_sync(str(database), False, staged)
        return _finish(
            staged,
            target,
            reason=reason,
            actor=actor,
            ip_address=ip_address,
            app_version=app_version,
            taken_at=taken_at,
            started=started,
        )
    except (sqlite3.Error, OSError) as exc:
        staged.unlink(missing_ok=True)
        raise SnapshotFailed(f"could not snapshot {database} to {target.name}: {exc}") from exc


async def take_pre_change_snapshot(
    db: Database,
    directory: Path,
    *,
    reason: str,
    actor: str | None = None,
    ip_address: str | None = None,
    prefix: str = PRE_CHANGE_PREFIX,
    app_version: str = __version__,
    now: Callable[[], datetime] = _now,
) -> SnapshotInfo:
    """Snapshot the live database before a destructive change (§18 Phase 7).

    Call it immediately before the write it protects, and let
    :class:`SnapshotFailed` propagate: the change must not run without it.
    """
    started = time.perf_counter()
    taken_at = now()
    target: Path | None = None
    staged: Path | None = None
    try:
        target = await asyncio.to_thread(reserve_path, directory, prefix, stamp(taken_at))
        staged = _staging(target)
        await vacuum_into(db, staged)
        info = await asyncio.to_thread(
            _finish,
            staged,
            target,
            reason=reason,
            actor=actor,
            ip_address=ip_address,
            app_version=app_version,
            taken_at=taken_at,
            started=started,
        )
    except SnapshotFailed:
        raise
    except (sqlite3.Error, OSError) as exc:
        for leftover in (staged, target):
            if leftover is not None:
                with contextlib.suppress(OSError):
                    leftover.unlink(missing_ok=True)
        raise SnapshotFailed(f"could not take a pre-change snapshot for {reason}: {exc}") from exc
    log.info(
        "pre-change snapshot taken",
        extra={
            "snapshot": info.name,
            "reason": reason,
            "actor": actor,
            "size_bytes": info.size_bytes,
            "duration_ms": info.duration_ms,
        },
    )
    return info


# -- reading back ------------------------------------------------------------------------


def list_snapshots(directory: Path) -> list[Path]:
    """Every snapshot of every prefix, newest first."""
    if not directory.is_dir():
        return []
    found = [p for p in directory.iterdir() if p.is_file() and is_snapshot_name(p.name)]
    return sorted(found, key=lambda p: (p.stat().st_mtime, p.name), reverse=True)


def read_sidecar(snapshot: Path) -> dict[str, Any] | None:
    """What the sidecar says, or ``None`` for a snapshot taken before sidecars."""
    try:
        data = json.loads(sidecar_of(snapshot).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# -- what must not be pruned ----------------------------------------------------------------


def _name_of(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return Path(value).name


def update_relies_on(directory: Path, boot_state: Path | None) -> set[str]:
    """The snapshots a rollback could restore (§14.3).

    The one the update record names, and the newest ``pre-update-`` snapshot,
    which :func:`proskenion.core.update._rollback_snapshot` falls back to.
    """
    names: set[str] = set()
    newest = next(
        (p for p in list_snapshots(directory) if p.name.startswith(PRE_UPDATE_PREFIX)), None
    )
    if newest is not None:
        names.add(newest.name)
    if boot_state is not None:
        try:
            document = json.loads(boot_state.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            document = None
        if isinstance(document, dict):
            update = document.get("update")
            if isinstance(update, dict):
                recorded = _name_of(update.get("snapshot"))
                if recorded is not None:
                    names.add(recorded)
    return names


def restore_record_snapshot(raw: str | None) -> str | None:
    """The ``snapshot`` a persisted backup restore record names."""
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return _name_of(data.get("snapshot")) if isinstance(data, dict) else None


def _remove(snapshot: Path) -> bool:
    try:
        snapshot.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("could not prune snapshot %s: %s", snapshot.name, exc)
        return False
    sidecar_of(snapshot).unlink(missing_ok=True)
    shutil.rmtree(baselines_of(snapshot), ignore_errors=True)
    return True


def prune_sync(directory: Path, *, keep: int, protect: Iterable[str] = ()) -> list[Path]:
    """Keep the ``keep`` newest snapshots, and every protected one (§15.3).

    A protected snapshot counts towards ``keep`` when it is among the newest,
    and is kept in addition when it is older. Each snapshot goes with its
    sidecar and, for a restore's, the baselines put aside beside it. Returns
    what was removed.
    """
    protected: Collection[str] = frozenset(protect)
    removed: list[Path] = []
    for path in list_snapshots(directory)[keep:]:
        if path.name in protected:
            continue
        if _remove(path):
            removed.append(path)
    return removed


__all__ = [
    "BASELINES_SUFFIX",
    "PREFIXES",
    "PRE_CHANGE_PREFIX",
    "PRE_RESTORE_PREFIX",
    "PRE_UPDATE_PREFIX",
    "RESTORE_RECORD",
    "SIDECAR_SUFFIX",
    "SNAPSHOTS_SUBDIR",
    "SNAPSHOT_SUFFIX",
    "SnapshotFailed",
    "SnapshotInfo",
    "baselines_of",
    "is_snapshot_name",
    "list_snapshots",
    "prune_sync",
    "read_sidecar",
    "restore_record_snapshot",
    "sidecar_of",
    "snapshots_dir",
    "stamp",
    "take_pre_change_snapshot",
    "take_snapshot_sync",
    "vacuum_into",
    "vacuum_into_sync",
    "reserve_path",
    "update_relies_on",
]
