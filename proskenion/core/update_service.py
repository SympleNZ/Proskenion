"""The update engine wired into the running application (§14.2–§14.5).

:mod:`proskenion.core.update` is deliberately standalone — it is the code
that replaces the application, so it depends on nothing the application
brings. This module is the other half: it gives that engine the state store's
banners, the alert sink, the audit trail and the progress channel, and it
owns the two things that only make sense while the application is running —
**the quiet-moment watch** and **the report of an automatic rollback**.

The quiet moment (Q17)
    "At the next quiet moment" means all four of: hirer access disabled, no
    scene running, no staff or hirer socket for ten minutes, and outside
    02:30–03:30. It is checked once a minute, with the ``update_ready``
    banner up in the meantime, and applied at the first minute all four hold.
    The idle clock runs from the moment the last socket closed rather than
    from the last message, because a page left open on the booth tablet is a
    person who may come back to it.

The rollback report (§14.5)
    ``auditorium-update-rollback`` runs as root, unattended, while this
    process is not running. It repoints the symlink, restores the snapshot
    and leaves a ``rollback`` record in ``boot-state.json``. Sending the
    high-priority email and raising the banner is this side's job, at the
    next start, because email configuration lives in the database and the
    banner needs a socket to arrive on. The record is cleared once both have
    been done, which is what makes the alert fire once rather than at every
    start from then on.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal

from proskenion.core.alerts import AlertKind, AlertSink
from proskenion.core.auth import record_event
from proskenion.core.broadcast import Broadcaster, progress_message
from proskenion.core.packages import Manifest, PackageError
from proskenion.core.state import StateStore, SystemWriter
from proskenion.core.tasks import every
from proskenion.core.update import (
    QUIET_INTERVAL_S,
    VERIFY_OPERATION,
    AppliedUpdate,
    NoPendingUpdate,
    PendingUpdate,
    PreparedUpdate,
    QuietReport,
    RolledBack,
    StagedUpload,
    UpdateError,
    UpdatePaths,
    UpdateRunner,
    clear_pending,
    discard_stale_uploads,
    evaluate_quiet,
    installed_version,
    manifest_json,
    previous_versions,
    read_pending,
    write_pending,
)
from proskenion.db.connection import Database
from proskenion.logging import LOCAL_TIMEZONE

log = logging.getLogger(__name__)

#: The state domain owner (B39). Shared with the other ``system`` writers.
UPDATES_OWNER: Final = "updates"

#: contracts §6's banner keys for this task.
UPDATE_READY_BANNER: Final = "update_ready"
UPDATE_ROLLED_BACK_BANNER: Final = "update_rolled_back"

_READY_TEXT: Final = (
    "An update is ready and will be applied at the next quiet moment. "
    "Apply now from Admin → System → Updates."
)
#: §14.5's wording, unchanged: it is the one sentence the admin reads after a
#: night they were not here for.
_ROLLED_BACK_TEXT: Final = (
    "An update failed to start and was rolled back automatically. "
    "Review the logs before retrying."
)

#: What the update is doing, for ``GET /system/update/status`` and §21.24.
ApplyState = Literal["idle", "preparing", "waiting_for_quiet", "applying", "failed"]

When = Literal["now", "quiet"]


class ApplyInProgress(UpdateError):
    """An apply was asked for while one is already running or armed."""

    rule = "in_progress"
    summary = "An update is already being applied."


@dataclass
class _Report:
    """The last thing that happened, for the status endpoint."""

    state: ApplyState = "idle"
    error: str | None = None
    rule: str | None = None
    applied: AppliedUpdate | None = None
    rolled_back: RolledBack | None = None
    quiet: QuietReport | None = None
    prepared: PreparedUpdate | None = None
    history: list[dict[str, Any]] = field(default_factory=list)


class UpdateService:
    """Owns the upload, the apply, the quiet watch and the rollback report."""

    def __init__(
        self,
        state: StateStore,
        db: Database,
        broadcaster: Broadcaster,
        paths: UpdatePaths,
        *,
        alert_sink: AlertSink | None = None,
        helper: Any = None,
        anchors_dir: Path | None = None,
        scenes_running: Callable[[], int] = lambda: 0,
        now: Callable[[], datetime] = lambda: datetime.now(tz=LOCAL_TIMEZONE),
        clock: Callable[[], float] = time.monotonic,
        interval_s: float = QUIET_INTERVAL_S,
        owner: str = UPDATES_OWNER,
    ) -> None:
        state.register_owner("system", owner, allow_multiple=True)
        self._state = state
        self._writer: SystemWriter = state.system.writer(owner)
        self._db = db
        self._broadcaster = broadcaster
        self._alerts = alert_sink
        self._scenes_running = scenes_running
        self._now = now
        self._clock = clock
        self._interval = interval_s
        self.paths = paths
        self.runner = UpdateRunner(
            paths,
            helper=helper,
            anchors_dir=anchors_dir,
            progress=self._report_progress,
        )
        self._lock = asyncio.Lock()
        self._report = _Report()
        self._watch: asyncio.Task[None] | None = None
        # Starting the idle clock at now rather than at zero is what stops a
        # restart from looking like ten minutes of quiet that never happened.
        self._last_connection = self._clock()

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Report an automatic rollback, tidy /data/tmp, and start the watch."""
        await self.report_rollback()
        await asyncio.to_thread(discard_stale_uploads, self.paths)
        self._publish_pending_flag()
        if self._watch is None:
            self._watch = asyncio.create_task(self._watch_for_quiet(), name="update-quiet-watch")

    async def stop(self) -> None:
        watch, self._watch = self._watch, None
        if watch is not None:
            watch.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watch

    # -- §14.5's half that needs a running application ---------------------

    async def report_rollback(self) -> RolledBack | None:
        """Raise the banner, send the high-priority email, clear the record.

        In that order, and the clear happens whatever the email did: a relay
        that is unreachable must not turn one failed update into an alert at
        every start from now until someone notices.
        """
        store = self.paths.store()
        boot = await store.read()
        record = boot.rollback
        if not record:
            return None
        failed = _text(record.get("failed_version")) or "the new version"
        restored = _text(record.get("restored_version")) or "the previous version"
        rolled = RolledBack(
            from_version=failed,
            to_version=restored,
            snapshot=_text(record.get("snapshot")),
            at=_text(record.get("at")) or "",
        )
        self._writer.set_banner(UPDATE_ROLLED_BACK_BANNER, "red", _ROLLED_BACK_TEXT)
        try:
            if self._alerts is not None:
                await self._alerts.send(
                    AlertKind.ROLLBACK,
                    f"Auditorium: update {failed} failed and was rolled back",
                    _rollback_email(record, failed, restored),
                    priority="high",
                )
        except Exception:  # an alert that fails must not strand the record
            log.exception("the rollback alert could not be sent")
        finally:
            await store.clear("rollback")
        with contextlib.suppress(Exception):
            await record_event(
                self._db,
                "update_auto_rollback",
                user_ident=None,
                ip_address=None,
                detail={
                    "failed_version": failed,
                    "restored_version": restored,
                    "snapshot": rolled.snapshot,
                    "at": rolled.at,
                    "reason": _reason_summary(record.get("reason")),
                },
            )
        self._report.rolled_back = rolled
        log.warning(
            "an update was rolled back automatically",
            extra={"failed_version": failed, "restored_version": restored},
        )
        return rolled

    # -- the upload (§21.24, Q9) -------------------------------------------

    async def accept(self, staged: StagedUpload) -> Manifest:
        """Verify a staged upload and hold it for review. Nothing is extracted.

        A package that is refused takes its upload with it: an unverified two
        gigabyte file has no reason to stay on a partition the database also
        lives on.
        """
        self._progress(1, 2, "Verifying the package")
        try:
            manifest = await self.runner.verify_from(staged)
        except (PackageError, UpdateError):
            staged.path.unlink(missing_ok=True)
            raise
        pending = PendingUpdate(
            package=staged.path,
            version=manifest.version,
            sha256=staged.sha256,
            size=staged.size,
            received_at=self.runner.now(),
            manifest=manifest_json(manifest),
        )
        await asyncio.to_thread(write_pending, self.paths, pending)
        self._report.state = "idle"
        self._report.error = None
        self._report.rule = None
        self._publish_pending_flag()
        self._progress(2, 2, f"{manifest.version} verified")
        return manifest

    async def discard(self) -> bool:
        """Forget a verified package the admin decided against (§21.24)."""
        if read_pending(self.paths) is None:
            return False
        await self.disarm()
        await asyncio.to_thread(clear_pending, self.paths)
        self._publish_pending_flag()
        return True

    # -- the apply ---------------------------------------------------------

    async def apply(self, when: When) -> AppliedUpdate | QuietReport:
        """Prepare, then either commit now or wait for the first quiet minute.

        Preparation — extract, build the environment, dry-run the migrations —
        happens either way and happens now, because a package whose migrations
        fail should say so while the admin is still standing there rather than
        at three in the morning. Nothing it does is committed: a failure
        leaves the appliance exactly as it was.
        """
        async with self._lock:
            if self._report.state in ("preparing", "applying"):
                raise ApplyInProgress(f"an update is already {self._report.state}")
            pending = await asyncio.to_thread(read_pending, self.paths)
            if pending is None:
                raise NoPendingUpdate("no verified package is waiting")
            self._report.state = "preparing"
            self._report.error = None
            self._report.rule = None
        try:
            prepared = await self.runner.prepare(pending)
        except (PackageError, UpdateError) as exc:
            self._fail(exc)
            raise
        self._report.prepared = prepared
        if when == "quiet":
            self._report.state = "waiting_for_quiet"
            self._writer.set_banner(UPDATE_READY_BANNER, "info", _READY_TEXT)
            report = self._quiet_now()
            self._report.quiet = report
            log.info(
                "update armed for the next quiet moment",
                extra={"version": prepared.version, "blocking": report.blocking()},
            )
            return report
        self._report.state = "applying"
        return await self._commit(prepared)

    async def _commit(self, prepared: PreparedUpdate) -> AppliedUpdate:
        try:
            applied = await self.runner.commit(
                prepared, before_snapshot=self._settle_database
            )
        except (PackageError, UpdateError) as exc:
            self._fail(exc)
            raise
        self._writer.clear_banner(UPDATE_READY_BANNER)
        self._writer.clear_banner(UPDATE_ROLLED_BACK_BANNER)
        await asyncio.to_thread(clear_pending, self.paths)
        self._publish_pending_flag()
        self._report.state = "idle"
        self._report.applied = applied
        self._report.prepared = None
        self._report.rolled_back = None
        self._record_history("applied", applied.from_version, applied.to_version, applied.at)
        return applied

    async def disarm(self) -> bool:
        """Stand down a quiet-moment apply. Returns whether one was armed."""
        if self._report.state != "waiting_for_quiet":
            return False
        self._report.state = "idle"
        self._report.prepared = None
        self._writer.clear_banner(UPDATE_READY_BANNER)
        return True

    # -- §14.3, by hand ----------------------------------------------------

    async def roll_back(self, *, to: str | None = None) -> RolledBack:
        """Repoint the symlink, restore the snapshot, restart (§14.3)."""
        async with self._lock:
            if self._report.state in ("preparing", "applying"):
                raise ApplyInProgress(f"an update is already {self._report.state}")
        rolled = await self.runner.roll_back(to=to, before_restore=self._close_database)
        self._writer.clear_banner(UPDATE_READY_BANNER)
        self._report.rolled_back = rolled
        self._record_history("rolled_back", rolled.from_version, rolled.to_version, rolled.at)
        return rolled

    # -- the quiet watch (Q17) ---------------------------------------------

    def _quiet_now(self) -> QuietReport:
        return evaluate_quiet(
            hirer_enabled=self._state.hirer.enabled,
            scenes_running=self._scenes_running(),
            seconds_since_connection=self._clock() - self._last_connection,
            when=self._now(),
        )

    def quiet(self) -> QuietReport:
        """Q17's four conditions as they stand right now."""
        report = self._quiet_now()
        self._report.quiet = report
        return report

    async def tick(self) -> QuietReport:
        """One minute's worth of watching: note the sockets, then decide.

        Separated from the loop so a test drives it a minute at a time
        without waiting for one.
        """
        if self._broadcaster.connection_count > 0:
            self._last_connection = self._clock()
        report = self.quiet()
        if self._report.state != "waiting_for_quiet" or not report.quiet:
            return report
        prepared = self._report.prepared
        if prepared is None:  # pragma: no cover - armed implies prepared
            await self.disarm()
            return report
        self._report.state = "applying"
        log.info("the appliance is quiet; applying", extra={"version": prepared.version})
        try:
            await self._commit(prepared)
        except (PackageError, UpdateError):
            # Already recorded on the report and logged by _fail; the banner
            # stays down and the admin finds the reason on the Updates screen.
            log.exception("the quiet-moment apply failed")
        return report

    async def _watch_for_quiet(self) -> None:
        # proskenion.core.tasks: one bad minute costs that minute, never the watch.
        await every(
            "the quiet-moment check", self.tick, interval_s=self._interval, delay_first=True
        )

    # -- what the status endpoint reads ------------------------------------

    def status(self) -> dict[str, Any]:
        """§21.24's card: what is installed, what is waiting, what is possible.

        ``rolled_back`` is the detail behind the fixed ``update_rolled_back``
        banner text (§14.5's sentence is exact and never gains a version
        number): which version failed and which is running now, for the
        screen to say plainly rather than leaving "why did this happen" to a
        log file. It answers the last rollback this process knows about,
        manual or automatic, and clears when the next successful apply clears
        the banner alongside it.
        """
        pending = read_pending(self.paths)
        return {
            "installed_version": installed_version(self.paths),
            "state": self._report.state,
            "error": self._report.error,
            "rule": self._report.rule,
            "pending": None
            if pending is None
            else {
                "version": pending.version,
                "sha256": pending.sha256,
                "size": pending.size,
                "received_at": pending.received_at,
                "manifest": dict(pending.manifest),
                "prepared": self._report.prepared is not None,
            },
            "quiet": (self._report.quiet or self.quiet()).to_json(),
            "previous_versions": previous_versions(self.paths),
            "history": list(self._report.history),
            "rolled_back": None
            if self._report.rolled_back is None
            else {
                "from_version": self._report.rolled_back.from_version,
                "to_version": self._report.rolled_back.to_version,
                "snapshot": self._report.rolled_back.snapshot,
                "at": self._report.rolled_back.at,
            },
        }

    # -- plumbing ----------------------------------------------------------

    async def _settle_database(self) -> None:
        """Checkpoint before the snapshot, so the copy carries the whole story.

        Under WAL the newest transactions live in ``-wal`` until a checkpoint
        folds them in. The online backup API reads them, but truncating first
        keeps the snapshot small and the restore simple.
        """
        with contextlib.suppress(Exception):
            await self._db.checkpoint(truncate=True)

    async def _close_database(self) -> None:
        """Close the database before its file is replaced (§14.3).

        SQLite names its write-ahead log after the database's path, so a
        connection left open across the replacement would be writing a log
        that belongs to a file that is no longer there. The application is
        being restarted in the next breath, so there is nothing left for this
        process to do with it.
        """
        with contextlib.suppress(Exception):
            await self._db.checkpoint(truncate=True)
        await self._db.close()

    def _report_progress(self, operation: str, step: int, of: int, message: str) -> None:
        self._broadcaster.publish(progress_message(operation, step, of, message))

    def _progress(self, step: int, of: int, message: str) -> None:
        self._broadcaster.publish(progress_message(VERIFY_OPERATION, step, of, message))

    def _publish_pending_flag(self) -> None:
        self._writer.set("update_pending", read_pending(self.paths) is not None)

    def _fail(self, exc: Exception) -> None:
        self._report.state = "failed"
        self._report.error = str(exc)
        self._report.rule = getattr(exc, "rule", None)
        self._report.prepared = None
        self._writer.clear_banner(UPDATE_READY_BANNER)
        log.error("update refused", extra={"rule": self._report.rule, "error": str(exc)})

    def _record_history(self, action: str, from_version: str | None, to: str, at: str) -> None:
        self._report.history.insert(
            0, {"action": action, "from": from_version, "to": to, "at": at}
        )
        del self._report.history[20:]

    # -- the audit rows (§6.14) --------------------------------------------

    async def audit_applied(
        self,
        applied: AppliedUpdate,
        *,
        user_ident: str | None,
        ip_address: str | None,
        action: str = "apply",
        when: str | None = None,
    ) -> None:
        await record_event(
            self._db,
            "update_applied",
            user_ident=user_ident,
            ip_address=ip_address,
            detail={
                "action": action,
                "from": applied.from_version,
                "to": applied.to_version,
                "snapshot": applied.snapshot,
                "when": when,
            },
        )

    async def audit_rolled_back(
        self, rolled: RolledBack, *, user_ident: str | None, ip_address: str | None
    ) -> None:
        """A rollback an admin asked for, recorded in the database that survives it.

        §6.14's vocabulary is closed and has one entry for the unattended path
        (``update_auto_rollback``) and one for a version being put into
        service (``update_applied``). A rollback done by hand is the second of
        those, with ``action`` saying which direction it went.

        It is written **after** the restore and through a short-lived
        connection of its own, which is the one place the appliance's
        single-writer rule (§15.1) does not apply: the restore has just
        replaced the file the application's write connection was holding, that
        connection has been closed, and this process is being restarted in the
        next breath. Writing the row before the restore would put it in the
        database the restore then discards (§14.3), so the one audit row about
        the rollback would be the one thing the rollback destroyed.
        """
        await asyncio.to_thread(
            _insert_event_offline,
            self.paths.database,
            "update_applied",
            user_ident,
            ip_address,
            {
                "action": "rollback",
                "from": rolled.from_version,
                "to": rolled.to_version,
                "snapshot": rolled.snapshot,
            },
        )


def _insert_event_offline(
    database: Path,
    event_type: str,
    user_ident: str | None,
    ip_address: str | None,
    detail: Mapping[str, Any],
) -> None:
    """Append one ``security_events`` row to a database file nothing else has open."""
    with contextlib.closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "INSERT INTO security_events (timestamp, event_type, user_ident, ip_address, detail)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                datetime.now(tz=LOCAL_TIMEZONE).isoformat(timespec="seconds"),
                event_type,
                user_ident,
                ip_address,
                json.dumps(dict(detail), sort_keys=True),
            ),
        )
        connection.commit()


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _reason_summary(reason: object) -> dict[str, Any] | None:
    """The rollback script's reason, trimmed to what an audit row should hold.

    The full journal tail belongs in the log file the script already wrote it
    to; a security event carries the exit status and the classification, which
    is what "why did this roll back" is usually answered with.
    """
    if not isinstance(reason, Mapping):
        return None
    keys = ("Result", "ExecMainStatus", "NRestarts", "classified")
    return {key: reason[key] for key in keys if key in reason}


def _rollback_email(record: Mapping[str, Any], failed: str, restored: str) -> str:
    reason = _reason_summary(record.get("reason")) or {}
    lines = [
        f"The auditorium controller could not start version {failed}.",
        "",
        f"It was rolled back automatically to {restored} and is running again.",
        f"The pre-update database snapshot was restored: {record.get('snapshot') or 'none'}.",
        "",
        "Review the logs before retrying the update.",
    ]
    if reason:
        lines += ["", "Service result:"]
        lines += [f"  {key}: {value}" for key, value in reason.items()]
    at = _text(record.get("at"))
    if at:
        lines += ["", f"Rolled back at {at}."]
    return "\n".join(lines)
