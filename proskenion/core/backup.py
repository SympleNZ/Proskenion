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

Two additions after the CM5's first monthly check (1 October 2026) reported a
merely *missing* archive as corrupt:

* **The index is reconciled with the destinations**
  (:func:`reconcile_presence`) at the start of every run, every
  verification and the first start after a restore: the ``*_present`` flags
  follow what each reachable destination actually lists, and an archive file
  with no row is adopted when its ``.sha256`` sidecar vouches for it. The
  verification tells "missing" and "unreachable" apart from "corrupt"
  (:data:`VerifyOutcome`); only the last marks an archive untrusted.
* **Every run checks its own copies** (``BackupJob._write_checked``): each
  copy is read back through its destination and compared with the archive
  built, and the built archive itself passes the monthly check's integrity
  test before it is distributed. The result is recorded per row
  (``checked_at``/``checked_destinations``, migration 012), separately from
  the monthly check.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal

from proskenion import __version__
from proskenion.core import retention
from proskenion.core.alerts import AlertKind, AlertSink
from proskenion.core.backup_archive import (
    ARCHIVE_PREFIX,
    ARCHIVE_SUFFIX,
    ArchiveError,
    BuiltArchive,
    archive_filename,
    build_archive,
    checksum_filename,
    hash_archive,
    read_manifest,
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
from proskenion.core.secrets import (
    DEFAULT_SECRET_PATH,
    DeviceSecret,
    SecretMismatch,
    SecretUnavailable,
)
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


#: What one verification run found (§13.4). Only ``"untrusted"`` says anything
#: about an archive's bytes — it is the one outcome from a copy that was
#: actually read and failed its checksum or integrity check:
#:
#: ``"verified"``     a copy was read and passed both checks;
#: ``"untrusted"``    a copy was read and failed one — the archive is marked;
#: ``"missing"``      every destination the index said held it was reachable
#:                    and none has the file (the index was stale — the CM5,
#:                    1 October 2026); its flags are cleared, nothing is marked;
#: ``"unreachable"``  a destination that may hold it could not be reached (USB
#:                    out, network down), so it could not be checked at all;
#: ``"none"``         no archive is held anywhere yet.
VerifyOutcome = Literal["verified", "untrusted", "missing", "unreachable", "none"]
_VERIFY_OUTCOMES: Final = ("verified", "untrusted", "missing", "unreachable", "none")


@dataclass(frozen=True, slots=True)
class VerifyStatus:
    """The monthly job's persisted outcome (§13.4)."""

    verified_at: str
    archive_id: str | None
    ok: bool
    detail: str
    outcome: VerifyOutcome = "verified"
    #: Which destination the checked copy was read from, when one was.
    destination: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "verified_at": self.verified_at,
            "archive_id": self.archive_id,
            "ok": self.ok,
            "detail": self.detail,
            "outcome": self.outcome,
            "destination": self.destination,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> VerifyStatus:
        ok = bool(data.get("ok"))
        raw_outcome = data.get("outcome")
        # A status persisted before outcomes existed was only ever a pass or
        # an "untrusted" (the old code marked even a missing file untrusted).
        outcome: VerifyOutcome = (
            raw_outcome
            if raw_outcome in _VERIFY_OUTCOMES
            else ("verified" if ok else "untrusted")
        )
        destination = data.get("destination")
        return cls(
            verified_at=str(data.get("verified_at", "")),
            archive_id=data.get("archive_id"),
            ok=ok,
            detail=str(data.get("detail", "")),
            outcome=outcome,
            destination=destination if isinstance(destination, str) else None,
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


async def default_destinations(
    db: Database, paths: BackupPaths, secret: DeviceSecret | None
) -> dict[DestinationName, BackupDestination]:
    """Local, the USB stick and — when one is configured and ``secret`` can
    decrypt its credentials — the network destination. Presence is not
    checked here; every caller asks ``available()`` itself."""
    destinations: dict[DestinationName, BackupDestination] = {
        "local": FilesystemDestination("local", paths.local_dir),
        "usb": UsbDestination(paths.usb_dir),
    }
    if secret is not None:
        network = await load_network_destination(db, secret, state_dir=paths.state_dir)
        if network is not None:
            destinations["network"] = network
    return destinations


def _load_secret(paths: BackupPaths) -> DeviceSecret | None:
    try:
        return DeviceSecret.load(paths.state_dir / DEFAULT_SECRET_PATH.name)
    except SecretUnavailable as exc:
        log.warning("no device secret, so the network destination is skipped: %s", exc)
        return None


# -- reconciling the index with what the destinations actually hold ---------------------

_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_ARCHIVE_ID_RE: Final = re.compile(rf"^{re.escape(ARCHIVE_PREFIX)}(\d{{8}}-\d{{4}})$")
_DESTINATION_ORDER: Final[tuple[DestinationName, ...]] = ("local", "usb", "network")

#: What an adopted archive records when its manifest cannot be read. Only
#: reachable for a file whose sidecar matched but whose first member is not a
#: readable manifest — a corrupt build — and such a row is marked untrusted,
#: so a restore never offers it and nothing ever reads these two values as
#: real: 0 is below every shipped migration, "unknown" is no version string.
_UNKNOWN_SCHEMA_VERSION: Final = 0
_UNKNOWN_APP_VERSION: Final = "unknown"


def _present_at(row: backup_crud.ArchiveRow, name: DestinationName) -> bool:
    if name == "local":
        return row.local_present
    if name == "usb":
        return row.usb_present
    return row.network_present


def archive_id_from_filename(filename: str) -> str | None:
    """``auditorium-YYYYMMDD-HHMM`` from ``auditorium-YYYYMMDD-HHMM.tar.zst``, or
    ``None`` for anything else (a sidecar, a ``.tmp``, an image, a stray file)."""
    if not filename.endswith(ARCHIVE_SUFFIX):
        return None
    the_id = filename[: -len(ARCHIVE_SUFFIX)]
    return the_id if _ARCHIVE_ID_RE.match(the_id) else None


def created_at_from_archive_id(the_id: str) -> str | None:
    """The id's own minute, in Pacific/Auckland, as ISO 8601 with offset (§4.9)."""
    match = _ARCHIVE_ID_RE.match(the_id)
    if match is None:
        return None
    try:
        naive = datetime.strptime(match.group(1), "%Y%m%d-%H%M")
    except ValueError:
        return None
    return naive.replace(tzinfo=AUCKLAND).isoformat(timespec="seconds")


def _parse_sidecar(text: str) -> str | None:
    """The digest from ``<sha256>  <filename>`` (``sha256sum`` form)."""
    parts = text.strip().split()
    if not parts:
        return None
    digest = parts[0].lower()
    return digest if _SHA256_RE.match(digest) else None


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """What :func:`reconcile_presence` changed."""

    #: Destinations that were reachable and listed — the only ones whose
    #: flags were touched. An unreachable destination keeps its flags.
    reachable: frozenset[DestinationName]
    cleared: tuple[tuple[str, DestinationName], ...]  # flag set, file absent
    set: tuple[tuple[str, DestinationName], ...]  # flag clear, file present
    adopted: tuple[str, ...]  # files with no row, sidecar-verified, now recorded
    refused: tuple[tuple[str, str], ...]  # (archive id, why it was not adopted)
    removed: tuple[str, ...]  # rows left present nowhere, deleted like a pruned one


async def reconcile_presence(
    db: Database,
    destinations: dict[DestinationName, BackupDestination],
    *,
    scratch_dir: Path,
    now: Callable[[], datetime] = lambda: datetime.now(tz=AUCKLAND),
) -> ReconcileResult:
    """Bring ``backup_archives``' ``*_present`` flags into line with what each
    *reachable* destination actually holds (§13.3, §13.4).

    The flags are otherwise only ever written by the job that wrote a copy
    and by retention that deleted one, so anything else that changes a
    destination — a re-imaged CM5 recreating ``/srv/local`` empty while
    ``/data`` came back from an older archive (28 September 2026), a stick
    swapped for another, a NAS share restored — leaves them lying, and the
    monthly check then reads "not there" as "corrupt". For each destination
    that is available and can be listed:

    * a row flagged present whose file is not listed there is cleared;
    * a row not flagged present whose file is listed there is set;
    * an archive file with **no row at all** is *adopted* — but only when
      its ``.sha256`` sidecar is beside it and the file hashes to it (see
      :func:`_adopt`); otherwise it is left alone and reported as refused.

    A destination that is absent or cannot be listed is skipped entirely:
    unreachable is not "missing" (§4.5), so its flags are kept. A row that
    ends up present nowhere is deleted, exactly as retention does once the
    last copy of an archive is pruned.
    """
    reachable: dict[DestinationName, set[str]] = {}
    for name in _DESTINATION_ORDER:
        destination = destinations.get(name)
        if destination is None:
            continue
        try:
            if not await destination.available():
                continue
            listing = await destination.list_names()
        except DestinationError as exc:
            log.info("could not list the %s backup destination to reconcile it: %s", name, exc)
            continue
        reachable[name] = set(listing)

    rows = {row.id: row for row in await backup_crud.all_archives(db)}
    cleared: list[tuple[str, DestinationName]] = []
    set_: list[tuple[str, DestinationName]] = []
    orphans: dict[str, list[DestinationName]] = {}
    for name, names in reachable.items():
        held = {i for n in names if (i := archive_id_from_filename(n)) is not None}
        for row in rows.values():
            present = row.id in held
            if present != _present_at(row, name):
                await backup_crud.set_presence(db, row.id, name, present)
                (set_ if present else cleared).append((row.id, name))
        for orphan in sorted(held - rows.keys()):
            orphans.setdefault(orphan, []).append(name)

    adopted: list[str] = []
    refused: list[tuple[str, str]] = []
    for orphan, holders in orphans.items():
        reason = await _adopt(
            db, orphan, holders, destinations, reachable, scratch_dir=scratch_dir, now=now
        )
        if reason is None:
            adopted.append(orphan)
        else:
            refused.append((orphan, reason))
            log.warning("backup archive %s has no index row; not adopted: %s", orphan, reason)

    removed: list[str] = []
    for the_id in dict.fromkeys(i for i, _ in cleared):
        current = await backup_crud.get_archive(db, the_id)
        if current is not None and not current.any_present:
            await backup_crud.delete_if_absent_everywhere(db, the_id)
            removed.append(the_id)

    result = ReconcileResult(
        reachable=frozenset(reachable),
        cleared=tuple(cleared),
        set=tuple(set_),
        adopted=tuple(adopted),
        refused=tuple(refused),
        removed=tuple(removed),
    )
    if cleared or set_ or adopted or refused:
        log.info(
            "reconciled the backup index with its destinations",
            extra={
                "reachable": sorted(result.reachable),
                "cleared": [f"{i}@{n}" for i, n in cleared],
                "set": [f"{i}@{n}" for i, n in set_],
                "adopted": list(adopted),
                "refused": [i for i, _ in refused],
                "removed": removed,
            },
        )
    return result


async def _adopt(
    db: Database,
    the_id: str,
    holders: list[DestinationName],
    destinations: dict[DestinationName, BackupDestination],
    listings: dict[DestinationName, set[str]],
    *,
    scratch_dir: Path,
    now: Callable[[], datetime],
) -> str | None:
    """Record an archive file that has no row. Returns ``None`` when adopted,
    otherwise why not.

    **The rule: a sidecar must vouch for it.** The ``.tar.zst.sha256`` beside
    the file was written by this appliance when it built the archive
    (:mod:`proskenion.core.backup_archive`); a file that hashes to it is the
    file that was built. Without one, or with one it does not match, the
    file is left exactly where it is and never enters the index — nothing is
    deleted on a guess, and nothing unvouched-for is ever offered to a
    restore.

    **What the row records:** the id from the filename; ``created_at`` from
    the archive's own ``manifest.json`` (the value the job would have
    recorded), else from the id's minute in Pacific/Auckland; the size and
    SHA-256 of the verified copy; ``schema_version`` and ``app_version`` from
    the manifest — cheap, because it is the tar's first member and is read
    without extracting anything else. ``source`` is ``"scheduled"``: the
    archive's origin is not recorded anywhere in it, the column (and the
    history API) only knows ``scheduled``/``manual``, and nearly every
    archive is a nightly one. Presence is set for every reachable
    destination that lists the file.

    A file whose sidecar matches but whose manifest cannot be read is
    adopted *untrusted*, with the reason, so retention can still prune it
    and a restore still refuses it.
    """
    filename = archive_filename(the_id)
    sidecar_name = checksum_filename(the_id)
    last_reason = "no destination holding it has a .sha256 sidecar beside it"
    await asyncio.to_thread(scratch_dir.mkdir, parents=True, exist_ok=True)
    for name in holders:
        if sidecar_name not in listings[name]:
            continue
        destination = destinations[name]
        local_copy = scratch_dir / filename
        local_sidecar = scratch_dir / sidecar_name
        try:
            await destination.read(sidecar_name, local_sidecar)
            expected = _parse_sidecar(await asyncio.to_thread(local_sidecar.read_text, "utf-8"))
            if expected is None:
                last_reason = f"its sidecar at {name} is not a SHA-256 digest"
                continue
            await destination.read(filename, local_copy)
            actual = await hash_archive(local_copy)
            if actual != expected:
                last_reason = (
                    f"the copy at {name} does not match its sidecar "
                    f"(expected {expected}, got {actual})"
                )
                continue
            size_bytes = (await asyncio.to_thread(local_copy.stat)).st_size
            manifest_error: str | None = None
            try:
                manifest = await read_manifest(local_copy)
                created_at = manifest.created_at
                schema_version = manifest.schema_version
                app_version = manifest.app_version
            except Exception as exc:  # zstd, tar or JSON: any unreadable manifest
                manifest_error = f"adopted from {name}, but its manifest could not be read: {exc}"
                created_at = created_at_from_archive_id(the_id) or now().isoformat(
                    timespec="seconds"
                )
                schema_version = _UNKNOWN_SCHEMA_VERSION
                app_version = _UNKNOWN_APP_VERSION
        except (DestinationError, OSError, UnicodeDecodeError) as exc:
            last_reason = f"could not read it from {name}: {exc}"
            continue
        finally:
            local_copy.unlink(missing_ok=True)
            local_sidecar.unlink(missing_ok=True)

        await backup_crud.record_archive(
            db,
            archive_id=the_id,
            created_at=created_at,
            source="scheduled",
            size_bytes=size_bytes,
            sha256=actual,
            schema_version=schema_version,
            app_version=app_version,
            local_present=filename in listings.get("local", set()),
            usb_present=filename in listings.get("usb", set()),
            network_present=filename in listings.get("network", set()),
        )
        if manifest_error is not None:
            await backup_crud.mark_verified(
                db,
                the_id,
                verified_at=now().astimezone(AUCKLAND).isoformat(timespec="seconds"),
                untrusted=True,
                reason=manifest_error,
            )
        log.info("adopted backup archive %s from %s (sidecar-verified)", the_id, name)
        return None
    return last_reason


async def reconcile_after_restore(db: Database, paths: BackupPaths) -> None:
    """The first start after a restore: the restored database's archive index
    describes the destinations as they were when that archive was built, not
    as they are now, so bring it into line straight away rather than waiting
    for tonight's job. Never raises — the nightly job reconciles again anyway."""
    try:
        destinations = await default_destinations(db, paths, _load_secret(paths))
        await reconcile_presence(
            db, destinations, scratch_dir=staging_dir_of(paths) / "reconcile"
        )
    except Exception:
        log.exception("could not reconcile the backup index after the restore")


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
        return await default_destinations(self._db, self._paths, self._secret)

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
            # Once per run, before a byte is distributed: the same checksum
            # and read-only integrity check the monthly job makes (§13.4),
            # so an archive that is structurally bad fails the backup itself
            # rather than being discovered weeks later.
            await self._check_built(built)
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
            await self._write_checked(local, built, "local")
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
            checked_at=self._now().astimezone(AUCKLAND).isoformat(timespec="seconds"),
            checked_destinations=tuple(n for n, o in outcomes.items() if o.ok is True),
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

    async def _check_built(self, built: BuiltArchive) -> None:
        """The freshly built archive's checksum and ``PRAGMA integrity_check``.

        Raises :class:`ArchiveError` (after removing the build) when it fails,
        which the caller reports exactly like a build that did not complete.
        """
        scratch_db = staging_dir_of(self._paths) / "check" / f"{built.id}.db"
        try:
            await asyncio.to_thread(scratch_db.parent.mkdir, parents=True, exist_ok=True)
            result = await verify_archive(
                built.path, expected_sha256=built.sha256, scratch_db_path=scratch_db
            )
        finally:
            scratch_db.unlink(missing_ok=True)
        if not result.ok:
            self._cleanup(built)
            raise ArchiveError(f"the archive just built failed its own check: {result.detail}")

    async def _write_checked(
        self, destination: BackupDestination, built: BuiltArchive, name: DestinationName
    ) -> None:
        """Write the archive and its sidecar, then read the archive back
        through the destination's own ``read`` and compare its SHA-256 with
        the one built. A copy that does not read back identical is removed
        (so the index reconcile never meets it as an orphan) and the write
        raises :class:`DestinationError` — the destination's write failed.

        For local and the USB stick the read-back may be served from the page
        cache rather than the medium itself; what it proves is that the
        filesystem holds the right bytes under the right name, and the
        monthly check (§13.4) is what catches later decay on the medium.
        """
        filename = archive_filename(built.id)
        await destination.write(built.path, filename)
        await destination.write(built.checksum_path, checksum_filename(built.id))
        readback = staging_dir_of(self._paths) / "readback" / f"{name}-{filename}"
        try:
            await asyncio.to_thread(readback.parent.mkdir, parents=True, exist_ok=True)
            await destination.read(filename, readback)
            actual = await hash_archive(readback)
        except (OSError, DestinationError) as exc:
            reason = f"the copy written to {name} could not be read back: {exc}"
            actual = None
        else:
            reason = (
                f"the copy written to {name} does not read back as written "
                f"(expected {built.sha256}, got {actual})"
            )
        finally:
            readback.unlink(missing_ok=True)
        if actual == built.sha256:
            return
        with contextlib.suppress(DestinationError):
            await destination.delete(filename)
            await destination.delete(checksum_filename(built.id))
        raise DestinationError(reason)

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
            await self._write_checked(destination, built, name)
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

        await self.reconcile()

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

    async def reconcile(self) -> ReconcileResult | None:
        """The index against what the destinations hold, before tonight's
        archive — so retention below prunes from flags that are true, and an
        orphan a previous index never recorded is adopted and aged out like
        any other. Maintenance: a failure is logged, never tonight's result."""
        try:
            return await reconcile_presence(
                self._db,
                await self._destinations(),
                scratch_dir=staging_dir_of(self._paths) / "reconcile",
                now=self._now,
            )
        except Exception:
            log.exception("reconciling the backup index failed; tonight's backup is unaffected")
            return None

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

DestinationsProvider = Callable[[], Awaitable[dict[DestinationName, BackupDestination]]]


async def run_monthly_verify(
    db: Database,
    paths: BackupPaths,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(tz=AUCKLAND),
    random_choice: Callable[[list[backup_crud.ArchiveRow]], backup_crud.ArchiveRow] = random.choice,
    archive_id: str | None = None,
    destinations_provider: DestinationsProvider | None = None,
) -> VerifyStatus:
    """A random recent archive's checksum, and a read-only integrity check (§13.4).

    The index is reconciled with the destinations first
    (:func:`reconcile_presence`), and the archive is chosen from those held
    by a destination that was just listed, so a stale row is unlikely to be
    picked at all. ``archive_id`` checks that one archive instead of a
    random one (``python -m proskenion.tools.verify --archive``); an id
    with no row raises :class:`LookupError`.

    Only a copy that was actually read and failed its checksum or integrity
    check marks the archive untrusted. A destination flagged present that is
    reachable but does not have the file has its flag cleared and the next
    one is tried (local, USB, network); one that cannot be reached keeps its
    flag. See :data:`VerifyOutcome`.
    """
    if destinations_provider is not None:
        destinations = await destinations_provider()
    else:
        destinations = await default_destinations(db, paths, _load_secret(paths))
    verified_at = now().astimezone(AUCKLAND).isoformat(timespec="seconds")
    scratch_dir = staging_dir_of(paths) / "verify"
    await asyncio.to_thread(scratch_dir.mkdir, parents=True, exist_ok=True)

    reachable: frozenset[DestinationName] = frozenset()
    try:
        reconciled = await reconcile_presence(
            db, destinations, scratch_dir=scratch_dir / "reconcile", now=now
        )
        reachable = reconciled.reachable
    except Exception:
        log.exception("reconciling the backup index before verification failed")

    if archive_id is not None:
        requested = await backup_crud.get_archive(db, archive_id)
        if requested is None:
            raise LookupError(f"no backup archive {archive_id!r} is recorded")
        chosen = requested
    else:
        archives = [a for a in await backup_crud.list_archives(db, limit=200) if a.any_present]
        if not archives:
            status = VerifyStatus(
                verified_at, None, True, "no archives are held anywhere yet", outcome="none"
            )
            await _write_json(db, KEY_VERIFY, status.to_json())
            return status
        confirmed = [a for a in archives if any(_present_at(a, n) for n in reachable)]
        chosen = random_choice(confirmed or archives)

    local_copy = scratch_dir / archive_filename(chosen.id)
    scratch_db = scratch_dir / f"{chosen.id}.db"
    local_copy.unlink(missing_ok=True)
    scratch_db.unlink(missing_ok=True)
    try:
        fetched = await _fetch_copy(db, destinations, chosen, local_copy)
        if fetched.source is None:
            status = await _not_checked(db, chosen, fetched, verified_at)
            await _write_json(db, KEY_VERIFY, status.to_json())
            return status
        try:
            result = await verify_archive(
                local_copy, expected_sha256=chosen.sha256, scratch_db_path=scratch_db
            )
            ok, detail = result.ok, result.detail
        except ArchiveError as exc:
            ok, detail = False, str(exc)
    finally:
        local_copy.unlink(missing_ok=True)
        scratch_db.unlink(missing_ok=True)

    status = VerifyStatus(
        verified_at,
        chosen.id,
        ok,
        detail,
        outcome="verified" if ok else "untrusted",
        destination=fetched.source,
    )
    # A pass clears any earlier mark, including the false one the old code
    # wrote for a file that was merely missing (the CM5, 1 October 2026).
    await backup_crud.mark_verified(
        db,
        chosen.id,
        verified_at=verified_at,
        untrusted=not ok,
        reason=None if ok else f"{detail} (the copy at {fetched.source})",
    )
    await _write_json(db, KEY_VERIFY, status.to_json())
    return status


@dataclass(frozen=True, slots=True)
class _Fetched:
    source: DestinationName | None  # where the copy was read from; None = nowhere
    missing: tuple[DestinationName, ...]  # reachable, flagged present, no file: flag cleared
    unreachable: tuple[tuple[DestinationName, str], ...]  # flagged present, could not look


async def _fetch_copy(
    db: Database,
    destinations: dict[DestinationName, BackupDestination],
    row: backup_crud.ArchiveRow,
    target: Path,
) -> _Fetched:
    """The archive's first readable copy, trying local, then USB, then network.

    Each destination the row says holds it is asked whether it is available
    and then listed: a listing without the file clears that flag (the
    destination was reachable, so "not there" is a fact, not a glitch); an
    unavailable destination, or one whose listing or read fails, is skipped
    with its flag kept.
    """
    filename = archive_filename(row.id)
    missing: list[DestinationName] = []
    unreachable: list[tuple[DestinationName, str]] = []
    for name in _DESTINATION_ORDER:
        if not _present_at(row, name):
            continue
        destination = destinations.get(name)
        if destination is None:
            unreachable.append((name, "not configured"))
            continue
        try:
            if not await destination.available():
                unreachable.append((name, MEDIA_ABSENT if name == "usb" else "unreachable"))
                continue
            names = await destination.list_names()
        except DestinationError as exc:
            unreachable.append((name, str(exc)))
            continue
        if filename not in names:
            log.warning(
                "backup archive %s is flagged present at %s but is not there; flag cleared",
                row.id,
                name,
            )
            await backup_crud.set_presence(db, row.id, name, False)
            missing.append(name)
            continue
        try:
            await destination.read(filename, target)
        except DestinationError as exc:
            log.warning("could not read %s from %s: %s", filename, name, exc)
            unreachable.append((name, str(exc)))
            continue
        return _Fetched(name, tuple(missing), tuple(unreachable))
    return _Fetched(None, tuple(missing), tuple(unreachable))


async def _not_checked(
    db: Database, row: backup_crud.ArchiveRow, fetched: _Fetched, verified_at: str
) -> VerifyStatus:
    """No copy could be read. Never marks the archive untrusted: nothing
    about its bytes is known."""
    if fetched.unreachable:
        where = ", ".join(f"{name} ({why})" for name, why in fetched.unreachable)
        detail = f"{row.id} could not be checked: no destination holding it is reachable ({where})"
        log.warning("%s", detail)
        return VerifyStatus(verified_at, row.id, False, detail, outcome="unreachable")
    looked = ", ".join(fetched.missing) or "none, the index held it nowhere"
    detail = (
        f"{archive_filename(row.id)} is not present at any destination "
        f"(looked at: {looked}); it has been removed from the backup index"
    )
    log.error("%s", detail)
    await backup_crud.delete_if_absent_everywhere(db, row.id)
    return VerifyStatus(verified_at, row.id, False, detail, outcome="missing")


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
_MISSING_SUBJECT: Final = "A backup archive is missing from every destination"
_MISSING_TEXT: Final = (
    "The monthly check chose archive {archive_id}, but none of the places the "
    "backup index said held it still has the file: {detail}. Nothing was found "
    "to be corrupt. The archives that remain, and where each is held, are listed "
    "in Admin -> System -> Backup."
)


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
            # Any pass clears the banner: the archive it named has either
            # been re-checked and cleared, or been superseded by a good one.
            self._writer.clear_banner(BACKUP_UNTRUSTED_KEY)
            return
        if verify.outcome == "unreachable":
            # Nothing is known about the archive's bytes, and absent media
            # already has its own alarm (BackupMediaMonitor): no banner, no email.
            return
        if verify.outcome == "missing":
            text = _MISSING_TEXT.format(
                archive_id=verify.archive_id or "unknown", detail=verify.detail
            )
            await self._alert_sink.send(AlertKind.BACKUP_MISSING, _MISSING_SUBJECT, text)
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
    "ReconcileResult",
    "VerifyOutcome",
    "VerifyStatus",
    "archive_id_from_filename",
    "clear_media_alert_sent",
    "created_at_from_archive_id",
    "default_destinations",
    "load_network_destination",
    "mark_media_alert_sent",
    "media_alert_already_sent",
    "network_destination_status",
    "read_status",
    "read_verify_status",
    "reconcile_after_restore",
    "reconcile_presence",
    "run_monthly_verify",
    "staging_dir_of",
]
