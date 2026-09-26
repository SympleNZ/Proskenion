"""The nightly backup job, its failure ladder, and the app-side watcher
(spec §13.3-§13.4, §4.5, §15.3; contracts §5-§8; Q3, Q4).

Two halves, run by two different processes:

:class:`BackupJob`
    What ``python -m proskenion.tools.backup`` runs (Q3), as the ``auditorium``
    user, whether or not the application is up. One attempt builds the
    archive (:mod:`proskenion.core.backup_archive`) and writes it to every
    *available* destination (:mod:`proskenion.core.backup_destinations`);
    absent media is skipped, never a failure (§4.5). Only the local write —
    ``/srv/local``, which is never removable — is load-bearing: if it fails,
    :meth:`BackupJob.run` waits ten minutes and tries once more (§13.4), and
    a failure that survives the retry raises ``consecutive_failures``, which
    is what turns the amber banner red after three nights. Retention pruning
    (§15.3) runs every night, independent of whether tonight's archive was
    written, because it is about *old* archives, not tonight's.

    The result is persisted to ``system_state`` (domain ``"backup"``,
    key ``"status"``) — nothing here sends an email or raises a banner
    directly, because the standalone job may run while nobody is watching
    for it, and Phase 6's email needs the live SMTP configuration and an
    async SMTP client neither of which a `systemd oneshot` should carry.

:class:`BackupStatusWatcher`
    What the running application spawns instead: it polls that persisted
    state (the timer's tick is once a night; polling every 30 s costs one
    cheap read) and turns a *new* result into banners
    (``backup_failed_amber``/``backup_failed_red``) and one email per new
    failure through ``AlertSink`` (contracts §7) — the seam
    ``proskenion/core/alerts.py`` left for this task. It also answers
    :func:`network_destination_status`, which :class:`~proskenion.core.health.
    BackupMediaMonitor` reads to decide whether prolonged USB absence is
    amber (the network destination is fine) or red (both are down) — the
    escalation that module's own docstring said would "arrive with the
    backup job in Phase 6".

The monthly verification (§13.4's third failure mode — a corrupt archive) is
:func:`run_monthly_verify`, called by ``python -m proskenion.tools.verify``;
its result is a second, independent ``system_state`` key
(``"verify"``), and the watcher raises ``backup_untrusted`` from it the same
way.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal

from proskenion import __version__
from proskenion.core import retention
from proskenion.core.alerts import AlertKind, AlertSink
from proskenion.core.backup_archive import (
    ArchiveError,
    BuiltArchive,
    archive_filename,
    build_archive,
    checksum_filename,
    verify_archive,
)
from proskenion.core.backup_destinations import (
    DEFAULT_LOCAL_BACKUPS_DIR,
    DEFAULT_USB_MOUNT,
    BackupDestination,
    DestinationError,
    DestinationName,
    FilesystemDestination,
    SftpConfig,
    SftpDestination,
    SmbConfig,
    SmbDestination,
    UsbDestination,
    sftp_key_paths,
)
from proskenion.core.backup_retention import expired
from proskenion.core.events import BannerLevel
from proskenion.core.platform import BOOT_STATE_FILENAME
from proskenion.core.secrets import DEFAULT_SECRET_PATH, DeviceSecret, SecretMismatch
from proskenion.core.snapshots import snapshots_dir
from proskenion.core.state import StateStore, SystemWriter
from proskenion.core.tasks import every
from proskenion.db import migrations as db_migrations
from proskenion.db.connection import Database
from proskenion.db.crud import backup as backup_crud
from proskenion.db.crud import system_state
from proskenion.db.crud.base import AUCKLAND

log = logging.getLogger(__name__)

DOMAIN: Final = "backup"
KEY_STATUS: Final = "status"
KEY_VERIFY: Final = "verify"

#: §13.4: retry once, ten minutes later, before the amber banner.
RETRY_DELAY_S: Final = 600.0
#: §13.4: amber after the retry fails; red after this many consecutive nights.
RED_AFTER_NIGHTS: Final = 3

BACKUP_FAILED_AMBER_KEY: Final = "backup_failed_amber"
BACKUP_FAILED_RED_KEY: Final = "backup_failed_red"
BACKUP_UNTRUSTED_KEY: Final = "backup_untrusted"

WATCHER_OWNER: Final = "backup_status"
DEFAULT_WATCH_INTERVAL_S: Final = 30.0

_PASSWORD_FIELD: Final = "backup_destination_password"

Source = Literal["scheduled", "manual"]
Sleeper = Callable[[float], Awaitable[None]]


# -- the persisted shapes (system_state domain="backup") ------------------------------


@dataclass(frozen=True, slots=True)
class DestinationOutcome:
    attempted: bool
    ok: bool | None  # None only when not attempted
    reason: str | None

    def to_json(self) -> dict[str, Any]:
        return {"attempted": self.attempted, "ok": self.ok, "reason": self.reason}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> DestinationOutcome:
        return cls(
            attempted=bool(data.get("attempted")),
            ok=data.get("ok") if isinstance(data.get("ok"), bool) else None,
            reason=data.get("reason") if isinstance(data.get("reason"), str) else None,
        )

    @property
    def down(self) -> bool:
        """Not actually holding a fresh copy right now — absent, unreachable or failed."""
        return self.ok is not True


NOT_CONFIGURED: Final = "not_configured"
MEDIA_ABSENT: Final = "media_absent"

_SKIPPED_LOCAL = DestinationOutcome(False, None, "not_attempted")


@dataclass(frozen=True, slots=True)
class BackupRunStatus:
    """One nightly job's persisted outcome (contracts §8's failure handling)."""

    attempted_at: str
    source: Source
    archive_id: str | None
    job_result: Literal["success", "failed"]
    job_detail: str | None
    consecutive_failures: int
    retried: bool
    destinations: dict[DestinationName, DestinationOutcome]

    def to_json(self) -> dict[str, Any]:
        return {
            "attempted_at": self.attempted_at,
            "source": self.source,
            "archive_id": self.archive_id,
            "job_result": self.job_result,
            "job_detail": self.job_detail,
            "consecutive_failures": self.consecutive_failures,
            "retried": self.retried,
            "destinations": {k: v.to_json() for k, v in self.destinations.items()},
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> BackupRunStatus:
        source = data.get("source")
        job_result = data.get("job_result")
        destinations = {
            str(name): DestinationOutcome.from_json(value)
            for name, value in (data.get("destinations") or {}).items()
        }
        return cls(
            attempted_at=str(data.get("attempted_at", "")),
            source=source if source in ("scheduled", "manual") else "scheduled",
            archive_id=data.get("archive_id"),
            job_result=job_result if job_result in ("success", "failed") else "failed",
            job_detail=data.get("job_detail"),
            consecutive_failures=int(data.get("consecutive_failures", 0)),
            retried=bool(data.get("retried")),
            destinations=destinations,  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class VerifyStatus:
    """The monthly job's persisted outcome (§13.4)."""

    verified_at: str
    archive_id: str | None
    ok: bool
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {
            "verified_at": self.verified_at,
            "archive_id": self.archive_id,
            "ok": self.ok,
            "detail": self.detail,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> VerifyStatus:
        return cls(
            verified_at=str(data.get("verified_at", "")),
            archive_id=data.get("archive_id"),
            ok=bool(data.get("ok")),
            detail=str(data.get("detail", "")),
        )


async def _read_json(db: Database, key: str) -> dict[str, Any] | None:
    raw = await system_state.get_value(db, DOMAIN, key)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("backup %s in system_state is not valid JSON; ignored", key)
        return None
    return data if isinstance(data, dict) else None


async def _write_json(db: Database, key: str, data: dict[str, Any]) -> None:
    await system_state.set(db, DOMAIN, key, json.dumps(data), source="backup_job")


async def read_status(db: Database) -> BackupRunStatus | None:
    data = await _read_json(db, KEY_STATUS)
    return None if data is None else BackupRunStatus.from_json(data)


async def read_verify_status(db: Database) -> VerifyStatus | None:
    data = await _read_json(db, KEY_VERIFY)
    return None if data is None else VerifyStatus.from_json(data)


async def network_destination_status(db: Database) -> bool | None:
    """Whether the most recent nightly run reached the network destination.

    ``None`` when nothing has run yet, or when no network destination is
    configured at all — :class:`~proskenion.core.health.BackupMediaMonitor`
    treats that the same as "not applicable" rather than "down", so a site
    that only ever backs up to a USB stick is not paged the first time the
    stick is unplugged.
    """
    status = await read_status(db)
    if status is None:
        return None
    outcome = status.destinations.get("network")
    if outcome is None or outcome.reason == NOT_CONFIGURED:
        return None
    return outcome.ok


KEY_MEDIA_ALERT_SENT: Final = "media_alert_sent"


async def media_alert_already_sent(db: Database) -> bool:
    """Whether "both backup destinations are unavailable" has already been
    alerted for the outage in progress.

    Persisted (``system_state``), and shared by both places that can detect
    the outage — :class:`BackupStatusWatcher` here, from the nightly job's
    result, and :class:`~proskenion.core.health.BackupMediaMonitor`, from the
    48-hour live USB-absence escalation. Before this (carry-forward 6,
    phase-7 plan), each kept its own in-memory flag: an app restart mid-
    outage forgot it had already alerted and sent one more, and the two
    monitors, each unaware of the other, could each send their own "both
    destinations unavailable" email for the same outage. One persisted flag
    makes the alert single-sourced regardless of which monitor notices
    first, and survives a restart.
    """
    return await system_state.get_value(db, DOMAIN, KEY_MEDIA_ALERT_SENT) == "1"


async def mark_media_alert_sent(db: Database) -> None:
    await system_state.set(db, DOMAIN, KEY_MEDIA_ALERT_SENT, "1", source="media_alert")


async def clear_media_alert_sent(db: Database) -> None:
    """Called by whichever monitor first sees the outage end, so the next
    one raises the alert afresh."""
    await system_state.set(db, DOMAIN, KEY_MEDIA_ALERT_SENT, "0", source="media_alert")


# -- building the destinations from configuration --------------------------------------


async def load_network_destination(
    db: Database, secret: DeviceSecret, *, state_dir: Path
) -> BackupDestination | None:
    """The configured network destination, or ``None`` if unset or disabled."""
    row = await backup_crud.get_destination(db)
    if row is None or not row.enabled or not row.protocol or not row.host:
        return None
    if row.protocol == "smb":
        if not row.path or not row.username:
            return None
        password = ""
        if row.password is not None:
            try:
                password = secret.decrypt_value(_PASSWORD_FIELD, row.password)
            except SecretMismatch:
                log.error(
                    "the stored network-destination password was encrypted with a "
                    "different device secret; re-enter it in Admin -> System -> Backup"
                )
                return None
        return SmbDestination(
            SmbConfig(
                host=row.host,
                port=row.port or 445,
                share_path=row.path,
                username=row.username,
                password=password,
            )
        )
    if row.protocol == "sftp":
        if not row.path or not row.username:
            return None
        private_key, _ = sftp_key_paths(state_dir)
        if not private_key.is_file():
            log.error("no SFTP key has been generated yet; the network destination is skipped")
            return None
        return SftpDestination(
            SftpConfig(
                host=row.host,
                port=row.port or 22,
                remote_dir=row.path,
                username=row.username,
                private_key_path=private_key,
            )
        )
    return None


# -- the job ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BackupPaths:
    db_path: Path
    data_dir: Path
    state_dir: Path
    local_dir: Path = DEFAULT_LOCAL_BACKUPS_DIR
    usb_dir: Path = DEFAULT_USB_MOUNT
    staging_dir: Path | None = None  # defaults to <data_dir>/tmp/backup


def staging_dir_of(paths: BackupPaths) -> Path:
    return paths.staging_dir or (paths.data_dir / "tmp" / "backup")


class BackupJob:
    """One appliance's nightly (or manual) backup run (Q3)."""

    def __init__(
        self,
        db: Database,
        paths: BackupPaths,
        *,
        secret: DeviceSecret,
        app_version: str = __version__,
        now: Callable[[], datetime] = lambda: datetime.now(tz=AUCKLAND),
        sleep: Sleeper = asyncio.sleep,
        retry_delay_s: float = RETRY_DELAY_S,
        destinations_provider: (
            Callable[[], Awaitable[dict[DestinationName, BackupDestination]]] | None
        ) = None,
    ) -> None:
        self._db = db
        self._paths = paths
        self._secret = secret
        self._app_version = app_version
        self._now = now
        self._sleep = sleep
        self._retry_delay_s = retry_delay_s
        # Overridable so a test can substitute fake destinations without a
        # real USB mount, SMB server or SFTP server — see
        # tests/unit/core/test_backup.py. Production always uses the default.
        self._destinations_provider = destinations_provider or self._default_destinations

    async def _destinations(self) -> dict[DestinationName, BackupDestination]:
        return await self._destinations_provider()

    async def _default_destinations(self) -> dict[DestinationName, BackupDestination]:
        destinations: dict[DestinationName, BackupDestination] = {
            "local": FilesystemDestination("local", self._paths.local_dir),
            "usb": UsbDestination(self._paths.usb_dir),
        }
        network = await load_network_destination(
            self._db, self._secret, state_dir=self._paths.state_dir
        )
        if network is not None:
            destinations["network"] = network
        return destinations

    async def _schema_version(self) -> int:
        applied = await db_migrations.applied_versions(self._db)
        return max((db_migrations.version_of(n) for n in applied), default=0)

    async def _attempt(self, *, source: Source) -> BackupRunStatus:
        now = self._now()
        attempted_at = now.astimezone(AUCKLAND).isoformat(timespec="seconds")
        destinations = await self._destinations()

        try:
            built = await build_archive(
                db_path=self._paths.db_path,
                data_dir=self._paths.data_dir,
                state_dir=self._paths.state_dir,
                staging_dir=staging_dir_of(self._paths),
                schema_version=await self._schema_version(),
                app_version=self._app_version,
                now=now,
            )
        except ArchiveError as exc:
            log.error("backup archive could not be built: %s", exc)
            return BackupRunStatus(
                attempted_at=attempted_at,
                source=source,
                archive_id=None,
                job_result="failed",
                job_detail=str(exc),
                consecutive_failures=0,
                retried=False,
                destinations={
                    "local": _SKIPPED_LOCAL,
                    "usb": DestinationOutcome(False, None, "not_attempted"),
                    "network": DestinationOutcome(False, None, "not_attempted"),
                },
            )

        outcomes: dict[DestinationName, DestinationOutcome] = {}
        local = destinations["local"]
        try:
            await local.write(built.path, archive_filename(built.id))
            await local.write(built.checksum_path, checksum_filename(built.id))
            outcomes["local"] = DestinationOutcome(True, True, None)
        except DestinationError as exc:
            log.error("the local backup write failed: %s", exc)
            self._cleanup(built)
            return BackupRunStatus(
                attempted_at=attempted_at,
                source=source,
                archive_id=built.id,
                job_result="failed",
                job_detail=str(exc),
                consecutive_failures=0,
                retried=False,
                destinations={
                    "local": DestinationOutcome(True, False, str(exc)),
                    "usb": DestinationOutcome(False, None, "not_attempted"),
                    "network": DestinationOutcome(False, None, "not_attempted"),
                },
            )

        usb = destinations.get("usb")
        outcomes["usb"] = (
            await self._write_removable(usb, built, "usb")
            if usb is not None
            else DestinationOutcome(False, None, MEDIA_ABSENT)
        )
        network = destinations.get("network")
        outcomes["network"] = (
            await self._write_removable(network, built, "network")
            if network is not None
            else DestinationOutcome(False, None, NOT_CONFIGURED)
        )

        await backup_crud.record_archive(
            self._db,
            archive_id=built.id,
            created_at=built.manifest.created_at,
            source=source,
            size_bytes=built.size_bytes,
            sha256=built.sha256,
            schema_version=built.manifest.schema_version,
            app_version=built.manifest.app_version,
            local_present=True,
            usb_present=outcomes["usb"].ok is True,
            network_present=outcomes["network"].ok is True,
        )
        self._cleanup(built)

        with contextlib.suppress(Exception):
            # Retention is independent maintenance (§15.3): a failure here must
            # never turn tonight's successful archive into a reported failure.
            await self.prune(destinations)

        return BackupRunStatus(
            attempted_at=attempted_at,
            source=source,
            archive_id=built.id,
            job_result="success",
            job_detail=None,
            consecutive_failures=0,
            retried=False,
            destinations=outcomes,
        )

    @staticmethod
    def _cleanup(built: BuiltArchive) -> None:
        built.path.unlink(missing_ok=True)
        built.checksum_path.unlink(missing_ok=True)

    async def _write_removable(
        self, destination: BackupDestination, built: BuiltArchive, name: DestinationName
    ) -> DestinationOutcome:
        """USB or network: absent/unreachable is skipped, never a failure (§4.5)."""
        try:
            if not await destination.available():
                reason = MEDIA_ABSENT if name == "usb" else "unreachable"
                return DestinationOutcome(False, None, reason)
        except DestinationError as exc:
            return DestinationOutcome(False, None, str(exc))
        try:
            await destination.write(built.path, archive_filename(built.id))
            await destination.write(built.checksum_path, checksum_filename(built.id))
            return DestinationOutcome(True, True, None)
        except DestinationError as exc:
            log.warning("the %s backup write failed: %s", name, exc)
            return DestinationOutcome(True, False, str(exc))

    async def prune(self, destinations: dict[DestinationName, BackupDestination]) -> None:
        """§15.3/§13.3: drop archives past each destination's own retention window."""
        now = self._now()
        for name, destination in destinations.items():
            try:
                if not await destination.available():
                    continue  # can't reach it to delete from it right now
            except DestinationError:
                continue
            present = await backup_crud.list_archives_present(self._db, name)
            for row in expired(present, name, now=now):
                try:
                    await destination.delete(archive_filename(row.id))
                    await destination.delete(checksum_filename(row.id))
                except DestinationError as exc:
                    log.warning("could not prune %s from %s: %s", row.id, name, exc)
                    continue
                await backup_crud.set_presence(self._db, row.id, name, False)
                await backup_crud.delete_if_absent_everywhere(self._db, row.id)
                log.info("pruned expired backup %s from %s", row.id, name)

    async def run(self, *, source: Source) -> BackupRunStatus:
        """§13.4: one attempt; a scheduled run that fails waits ten minutes and
        tries once more. A manual "Back up now" does not retry — an operator
        watching it wants an answer, not a ten-minute silence."""
        previous = await read_status(self._db)
        previous_failures = previous.consecutive_failures if previous else 0

        status = await self._attempt(source=source)
        retried = False
        if status.job_result == "failed" and source == "scheduled":
            log.warning(
                "backup failed; retrying once in %.0f minutes (§13.4)",
                self._retry_delay_s / 60,
            )
            await self._sleep(self._retry_delay_s)
            status = await self._attempt(source=source)
            retried = True

        consecutive = previous_failures + 1 if status.job_result == "failed" else 0
        status = replace(status, consecutive_failures=consecutive, retried=retried)
        await _write_json(self._db, KEY_STATUS, status.to_json())
        await self.prune_retention()
        return status

    async def prune_retention(self) -> retention.PruneResult | None:
        """§15.3: "pruned during the nightly backup job" — the 90-day tables and
        the pre-change snapshots, after the archive, whatever tonight's result.

        Independent maintenance, like the archive retention above: a failure
        here is logged and never turns tonight's backup into a failure.
        """
        try:
            return await retention.prune(
                self._db,
                now=self._now(),
                snapshots_dir=snapshots_dir(self._paths.data_dir),
                boot_state=self._paths.state_dir / BOOT_STATE_FILENAME,
            )
        except Exception:
            log.exception("retention pruning failed; tonight's backup is unaffected")
            return None


# -- the monthly verification ------------------------------------------------------------


async def run_monthly_verify(
    db: Database,
    paths: BackupPaths,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(tz=AUCKLAND),
    random_choice: Callable[[list[backup_crud.ArchiveRow]], backup_crud.ArchiveRow] = random.choice,
) -> VerifyStatus:
    """A random recent archive's checksum, and a read-only integrity check (§13.4)."""
    archives = [a for a in await backup_crud.list_archives(db, limit=200) if a.any_present]
    verified_at = now().astimezone(AUCKLAND).isoformat(timespec="seconds")
    if not archives:
        status = VerifyStatus(verified_at, None, True, "no archives are held anywhere yet")
        await _write_json(db, KEY_VERIFY, status.to_json())
        return status

    chosen = random_choice(archives)
    scratch_dir = staging_dir_of(paths) / "verify"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    local_copy = scratch_dir / archive_filename(chosen.id)
    scratch_db = scratch_dir / f"{chosen.id}.db"
    local_copy.unlink(missing_ok=True)
    scratch_db.unlink(missing_ok=True)
    try:
        await _fetch_copy(db, paths, chosen, local_copy)
        result = await verify_archive(
            local_copy, expected_sha256=chosen.sha256, scratch_db_path=scratch_db
        )
        status = VerifyStatus(verified_at, chosen.id, result.ok, result.detail)
    except (ArchiveError, DestinationError) as exc:
        status = VerifyStatus(verified_at, chosen.id, False, str(exc))
    finally:
        local_copy.unlink(missing_ok=True)
        scratch_db.unlink(missing_ok=True)

    await backup_crud.mark_verified(
        db,
        chosen.id,
        verified_at=verified_at,
        untrusted=not status.ok,
        reason=None if status.ok else status.detail,
    )
    await _write_json(db, KEY_VERIFY, status.to_json())
    return status


async def _fetch_copy(
    db: Database, paths: BackupPaths, row: backup_crud.ArchiveRow, destination: Path
) -> None:
    if row.local_present:
        await FilesystemDestination("local", paths.local_dir).read(
            archive_filename(row.id), destination
        )
        return
    if row.usb_present:
        usb = UsbDestination(paths.usb_dir)
        if await usb.available():
            await usb.read(archive_filename(row.id), destination)
            return
    if row.network_present:
        network = await load_network_destination(
            db,
            DeviceSecret.load(paths.state_dir / DEFAULT_SECRET_PATH.name),
            state_dir=paths.state_dir,
        )
        if network is not None:
            await network.read(archive_filename(row.id), destination)
            return
    raise ArchiveError(f"{row.id} is not reachable from any destination right now")


# -- the app-side watcher (contracts §7's seam) -------------------------------------------

_JOB_FAILED_TEXT: Final = (
    "The backup on {at} failed after a retry: {detail} "
    "({nights} consecutive night(s) failed)."
)
_JOB_FAILED_SUBJECT: Final = "Backup failed"
_MEDIA_FAILED_SUBJECT: Final = "Both backup destinations are unavailable"
_MEDIA_FAILED_TEXT: Final = (
    "Tonight's backup could not reach the USB stick or the network destination. "
    "No offline copy has been made. Insert the backup USB or check the network "
    "destination in Admin -> System -> Backup."
)
_UNTRUSTED_SUBJECT: Final = "A backup archive failed verification"
_UNTRUSTED_TEXT: Final = "The monthly check on archive {archive_id} failed: {detail}"


class BackupStatusWatcher:
    """Turns the job's persisted state into banners and email (contracts §7).

    Polls rather than subscribes: the job that writes ``system_state`` runs
    in a different process (Q3), so there is no in-process event to listen
    for. A poll that finds nothing new (the common case, 29 nights out of
    30) costs one ``system_state`` read.
    """

    def __init__(
        self,
        db: Database,
        state: StateStore,
        alert_sink: AlertSink,
        *,
        interval_s: float = DEFAULT_WATCH_INTERVAL_S,
        sleep: Sleeper = asyncio.sleep,
        owner: str = WATCHER_OWNER,
    ) -> None:
        self._db = db
        self._alert_sink = alert_sink
        self._interval = interval_s
        self._sleep = sleep
        state.register_owner("system", owner, allow_multiple=True)
        self._writer: SystemWriter = state.system.writer(owner)
        self._seen_status_at: str | None = None
        self._seen_verify_at: str | None = None

    async def poll(self) -> None:
        status = await read_status(self._db)
        if status is not None and status.attempted_at != self._seen_status_at:
            self._seen_status_at = status.attempted_at
            await self._apply_status(status)
        verify = await read_verify_status(self._db)
        if verify is not None and verify.verified_at != self._seen_verify_at:
            self._seen_verify_at = verify.verified_at
            await self._apply_verify(verify)

    async def _apply_status(self, status: BackupRunStatus) -> None:
        if status.job_result == "failed":
            red = status.consecutive_failures >= RED_AFTER_NIGHTS
            level: BannerLevel = "red" if red else "amber"
            key = BACKUP_FAILED_RED_KEY if red else BACKUP_FAILED_AMBER_KEY
            other = BACKUP_FAILED_AMBER_KEY if red else BACKUP_FAILED_RED_KEY
            text = _JOB_FAILED_TEXT.format(
                at=status.attempted_at,
                detail=status.job_detail or "no detail given",
                nights=status.consecutive_failures,
            )
            self._writer.set_banner(key, level, text)
            self._writer.clear_banner(other)
            await self._alert_sink.send(AlertKind.BACKUP_FAILED, _JOB_FAILED_SUBJECT, text)
        else:
            self._writer.clear_banner(BACKUP_FAILED_AMBER_KEY)
            self._writer.clear_banner(BACKUP_FAILED_RED_KEY)

        # Only meaningful once the primary (local) write held — a local
        # failure is already the backup_failed ladder above, and its
        # destinations dict reports usb/network as "not attempted" rather
        # than genuinely unreachable, which must not also read as "both
        # removable destinations are down".
        local = status.destinations.get("local")
        usb = status.destinations.get("usb")
        network = status.destinations.get("network")
        both_down = (
            local is not None
            and local.ok is True
            and usb is not None
            and usb.down
            and network is not None
            and network.reason != NOT_CONFIGURED
            and network.down
        )
        if both_down and not await media_alert_already_sent(self._db):
            await self._alert_sink.send(
                AlertKind.MEDIA_FAILED, _MEDIA_FAILED_SUBJECT, _MEDIA_FAILED_TEXT
            )
            await mark_media_alert_sent(self._db)
        elif not both_down:
            await clear_media_alert_sent(self._db)

    async def _apply_verify(self, verify: VerifyStatus) -> None:
        if verify.ok:
            self._writer.clear_banner(BACKUP_UNTRUSTED_KEY)
            return
        text = _UNTRUSTED_TEXT.format(
            archive_id=verify.archive_id or "unknown", detail=verify.detail
        )
        self._writer.set_banner(BACKUP_UNTRUSTED_KEY, "red", text)
        await self._alert_sink.send(AlertKind.BACKUP_UNTRUSTED, _UNTRUSTED_SUBJECT, text)

    async def run(self) -> None:
        # proskenion.core.tasks: a failed poll costs that poll, never the watcher.
        await every(
            "the backup status watch", self.poll, interval_s=self._interval, sleep=self._sleep
        )


__all__ = [
    "BACKUP_FAILED_AMBER_KEY",
    "BACKUP_FAILED_RED_KEY",
    "BACKUP_UNTRUSTED_KEY",
    "DOMAIN",
    "MEDIA_ABSENT",
    "NOT_CONFIGURED",
    "RED_AFTER_NIGHTS",
    "RETRY_DELAY_S",
    "BackupJob",
    "BackupPaths",
    "BackupRunStatus",
    "BackupStatusWatcher",
    "DestinationOutcome",
    "VerifyStatus",
    "clear_media_alert_sent",
    "load_network_destination",
    "mark_media_alert_sent",
    "media_alert_already_sent",
    "network_destination_status",
    "read_status",
    "read_verify_status",
    "run_monthly_verify",
    "staging_dir_of",
]
