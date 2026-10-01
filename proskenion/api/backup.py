"""Backup endpoints (contracts §5 ``/system/backup/*``, §21.24).

Every route is admin-only (contracts §5's default). ``POST /system/backup/run``
blocks for the duration of "Back up now" — the same shape as
``POST /system/certs/issue`` — while ``backup_run`` ``progress`` frames stream
over the WebSocket in parallel (contracts §6); the request itself carries
only the finished status. ``POST /system/backup/verify`` runs the monthly
check in-process: unlike a backup, reading an archive back needs no
privilege the application does not already have.

The network destination's password is write-only, the same convention as
every other §6.10 credential (see ``GET``/``PUT /system/email`` in
:mod:`proskenion.api.system`): ``GET`` answers ``password_set`` rather than
the password, and an absent or empty ``password`` on ``PUT`` means "leave
the stored one unchanged". There is nothing to store for SFTP — it
authenticates with the key pair this device generated
(:func:`~proskenion.core.backup_destinations.generate_sftp_keypair_if_missing`),
whose public half ``GET /system/backup/sftp-key`` serves.

``POST /system/backup/restore`` takes either shape contracts §5 gives it,
told apart by the content type:

* a **streamed upload** — any body that is not JSON. It is read as a stream
  and written to ``/data/tmp`` chunk by chunk (Q9), never through
  ``UploadFile``, which spools through a buffer with a memory threshold on a
  machine with 4 GB of RAM and a read-only root. An operator who also has the
  archive's ``.sha256`` sidecar passes its digest as ``?sha256=``.
* a **JSON body** naming an archive this appliance already holds
  (``{"archive_id": …}``, optionally ``{"destination": "local"|"usb"|
  "network"}``), or a pre-restore snapshot to go back to (``{"snapshot": …}``).

The work, the checks and the order are :mod:`proskenion.core.backup_restore`;
this module is the route, the §16.1 envelope each refusal becomes, and the
answer. A refusal names the check that refused it — ``detail.rule`` — so
§21.24 says which one failed rather than reconstructing it from a sentence.
A restore that succeeds closes the database and restarts the appliance, so
nothing here touches ``db`` after the service returns: the audit row and the
restore record are written by the service, into the database it restored.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from starlette.background import BackgroundTask

from proskenion.api.deps import client_ip, get_config, get_db, get_helper, require_admin
from proskenion.api.errors import ApiError, ErrorCode
from proskenion.api.snapshots import SNAPSHOT_FAILED_MESSAGE
from proskenion.config import Config
from proskenion.core import backup as backup_core
from proskenion.core import backup_restore as restore_core
from proskenion.core import system_config
from proskenion.core.auth import TokenClaims, record_event
from proskenion.core.backup_archive import archive_filename
from proskenion.core.backup_destinations import (
    RETENTION_DAYS,
    UsbDestination,
    generate_sftp_keypair_if_missing,
    is_mounted,
    sftp_public_key_text,
)
from proskenion.core.broadcast import progress_message
from proskenion.core.helper import HelperClient, HelperError
from proskenion.core.secrets import (
    DEFAULT_SECRET_PATH,
    DeviceSecret,
    generate_secret_if_missing,
)
from proskenion.core.snapshots import SnapshotFailed, list_snapshots, read_sidecar, snapshots_dir
from proskenion.db.connection import Database
from proskenion.db.crud import backup as backup_crud
from proskenion.db.crud import users as users_crud

log = logging.getLogger(__name__)

router = APIRouter(prefix="/system/backup", tags=["backup"], dependencies=[Depends(require_admin)])

_PASSWORD_FIELD = "backup_destination_password"


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _device_secret(config: Config) -> DeviceSecret:
    path = config.app.state_dir / DEFAULT_SECRET_PATH.name
    generate_secret_if_missing(path)
    return DeviceSecret.load(path)


def get_backup_paths(request: Request) -> backup_core.BackupPaths:
    """Where archives, the SFTP key and staging live (contracts §8).

    A dataclass on ``app.state`` — the same pattern as ``get_certs`` in
    :mod:`proskenion.api.certs` — built once in :func:`~proskenion.api.app.create_app`
    so a test can point local/USB at a ``tmp_path`` instead of the real
    ``/srv/local`` and ``/mnt/backup`` mounts.
    """
    paths: backup_core.BackupPaths | None = getattr(request.app.state, "backup_paths", None)
    if paths is None:
        raise ApiError(
            ErrorCode.INTERNAL_ERROR, "Backup paths are not configured", {"reason": "not_started"}
        )
    return paths


# -- backup restore (contracts §5 "POST /system/backup/restore", §13.2, Q15) -------------


class NetworkDifferenceResponse(_Payload):
    """One ``system.json`` setting the archive disagrees with, **not applied**."""

    key: str
    current: Any = None
    archived: Any = None


class RestoreResponse(_Payload):
    """What the restore did, what it deliberately left alone, and what the
    operator still has to do about it."""

    at: str
    source: Literal["upload", "local", "usb", "network", "snapshot"]
    archive_id: str | None
    created_at: str | None
    schema_version: int | None
    app_version: str | None
    sha256: str | None
    #: False only for an upload that arrived without its ``.sha256`` sidecar;
    #: the manifest's own digest of the database is checked either way.
    checksum_verified: bool
    #: The pre-restore snapshot, so this restore is itself reversible (§21.24):
    #: pass it back as ``{"snapshot": …}`` to undo.
    snapshot: str
    baselines_snapshot: str | None
    replaced: list[str]
    not_applied: list[str]
    #: What the next start will migrate the restored database forward by (B33).
    migrations_pending: list[str]
    network_differences: list[NetworkDifferenceResponse]
    device_passwords_require_reentry: bool
    devices_needing_passwords: list[str]
    #: Settings outside the device table whose passwords will not decrypt
    #: here (the email relay, the network backup destination).
    settings_needing_passwords: list[str]
    restarted: bool
    #: The certificate nginx serves is now a different one. A browser that
    #: accepted the old one refuses it until the page is reloaded and the new
    #: one accepted — and on an address it does not name, never.
    certificate_replaced: bool
    #: What the certificate now served is valid for (names and addresses).
    certificate_names: list[str]
    #: When the appliance first started on the restored database; ``None``
    #: until it has (the response to the restore itself always says ``None``).
    restarted_at: str | None
    acknowledged_at: str | None


class LastRestoreResponse(RestoreResponse):
    """The restore record as the Backup screen shows it after the restart,
    with what still needs attention worked out from the live database."""

    #: Of ``devices_needing_passwords``, those whose password still does not
    #: decrypt on this appliance — re-entering one takes it off this list.
    devices_still_needing_passwords: list[str]
    settings_still_needing_passwords: list[str]


def _restore_response(result: restore_core.RestoreResult) -> RestoreResponse:
    return RestoreResponse(
        at=result.at,
        source=result.source,
        archive_id=result.archive_id,
        created_at=result.created_at,
        schema_version=result.schema_version,
        app_version=result.app_version,
        sha256=result.sha256,
        checksum_verified=result.checksum_verified,
        snapshot=result.snapshot,
        baselines_snapshot=result.baselines_snapshot,
        replaced=list(result.replaced),
        not_applied=list(result.not_applied),
        migrations_pending=list(result.migrations_pending),
        network_differences=[
            NetworkDifferenceResponse(key=d.key, current=d.current, archived=d.archived)
            for d in result.network_differences
        ],
        device_passwords_require_reentry=result.device_passwords_require_reentry,
        devices_needing_passwords=list(result.devices_needing_passwords),
        settings_needing_passwords=list(result.settings_needing_passwords),
        restarted=result.restarted,
        certificate_replaced=result.certificate_replaced,
        certificate_names=list(result.certificate_names),
        restarted_at=result.restarted_at,
        acknowledged_at=result.acknowledged_at,
    )


async def _last_restore_response(
    result: restore_core.RestoreResult, db: Database, secret: DeviceSecret
) -> LastRestoreResponse:
    devices_now, settings_now = await restore_core.still_needing_passwords(db, secret, result)
    return LastRestoreResponse(
        **_restore_response(result).model_dump(),
        devices_still_needing_passwords=list(devices_now),
        settings_still_needing_passwords=list(settings_now),
    )


class RestoreRequest(BaseModel):
    """The JSON shape: an archive this appliance holds, or a snapshot."""

    model_config = ConfigDict(extra="forbid")

    archive_id: str | None = Field(default=None, max_length=128)
    destination: Literal["local", "usb", "network"] | None = None
    snapshot: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _one_source(self) -> RestoreRequest:
        if bool(self.archive_id) == bool(self.snapshot):
            raise ValueError("give exactly one of archive_id or snapshot")
        if self.snapshot and self.destination:
            raise ValueError("a snapshot is held locally and has no destination")
        return self


def get_restore(
    request: Request, db: Database, config: Config, paths: backup_core.BackupPaths
) -> restore_core.RestoreService:
    """Build the service over whatever the application is currently running.

    Per request rather than in the lifespan, the same reasoning
    :func:`proskenion.api.baseline.get_baseline` gives: it holds nothing
    between calls, and a subsystem that is not running is simply not used.
    """
    broadcaster = getattr(request.app.state, "broadcaster", None)

    def _progress(operation: str, step: int, of: int, message: str) -> None:
        if broadcaster is not None:
            broadcaster.publish(progress_message(operation, step, of, message))

    return restore_core.RestoreService(
        db,
        restore_core.RestorePaths.for_appliance(
            database=paths.db_path,
            data_dir=paths.data_dir,
            state_dir=paths.state_dir,
            local_dir=paths.local_dir,
            usb_dir=paths.usb_dir,
        ),
        secret=_device_secret(config),
        helper=getattr(request.app.state, "helper", None),
        progress=_progress,
        scenes=getattr(request.app.state, "scene_engine", None),
    )


def _refuse(exc: restore_core.RestoreRefused) -> ApiError:
    """Turn one refused check into the §16.1 envelope §21.24 renders.

    ``detail.rule`` is the check that refused — ``checksum``, ``member``,
    ``schema_ahead`` — so the screen can say which, and the message is the
    sentence the refusal carries rather than one reconstructed here.
    """
    if isinstance(exc, restore_core.NoSuchSnapshot):
        return ApiError(ErrorCode.NOT_FOUND, exc.summary, {"rule": exc.rule, "reason": str(exc)})
    if isinstance(exc, restore_core.ArchiveUnreachable | restore_core.RestartRefused):
        return ApiError(
            ErrorCode.DEVICE_UNAVAILABLE, exc.summary, {"rule": exc.rule, "reason": str(exc)}
        )
    return ApiError(
        ErrorCode.VALIDATION_FAILED, exc.summary, {"rule": exc.rule, "reason": str(exc)}
    )


async def _restore_source(
    request: Request, paths: restore_core.RestorePaths
) -> restore_core.RestoreSource:
    """Read whichever of the two shapes contracts §5 allows arrived.

    The upload is the default, and it is streamed: a body that is not JSON is
    an archive, and it goes to ``/data/tmp`` a chunk at a time (Q9). Only the
    JSON shape is small enough to read whole.
    """
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip() == "application/json":
        raw = await request.body()
        try:
            body = RestoreRequest.model_validate(json.loads(raw or b"{}"))
        except (ValueError, ValidationError) as exc:
            raise ApiError(
                ErrorCode.VALIDATION_FAILED,
                "The restore request could not be read",
                {"reason": str(exc)},
            ) from exc
        if body.snapshot:
            return restore_core.SnapshotSource(name=body.snapshot)
        return restore_core.NamedSource(
            archive_id=body.archive_id or "", destination=body.destination
        )
    declared = request.query_params.get("sha256")
    staged = await restore_core.stage_archive_upload(request.stream(), paths.tmp_dir)
    return restore_core.UploadSource(staged=staged, declared_sha256=declared)


@router.post("/restore", response_model=RestoreResponse)
async def restore(
    request: Request,
    claims: Annotated[TokenClaims, Depends(require_admin)],
    db: Annotated[Database, Depends(get_db)],
    config: Annotated[Config, Depends(get_config)],
    paths: Annotated[backup_core.BackupPaths, Depends(get_backup_paths)],
) -> RestoreResponse:
    """Restore the database, the baselines and the certificates (Q15).

    Every check runs before anything is replaced, and each refusal names
    itself. A restore that gets past them takes a pre-restore snapshot, swaps
    the files and restarts the appliance through the helper — so this
    response is the last thing this process says.
    """
    service = get_restore(request, db, config, paths)
    try:
        source = await _restore_source(request, service.paths)
    except restore_core.RestoreRefused as exc:
        # An upload that was too large or carried nothing at all: it is
        # refused where it is read, and it took its own file with it.
        raise _refuse(exc) from exc
    try:
        result = await service.restore(
            source,
            actor=restore_core.Actor(ident=claims.tier, ip_address=client_ip(request)),
        )
    except restore_core.RestoreRefused as exc:
        raise _refuse(exc) from exc
    except SnapshotFailed as exc:
        raise ApiError(
            ErrorCode.INTERNAL_ERROR,
            SNAPSHOT_FAILED_MESSAGE,
            {"reason": "snapshot_failed", "action": "backup restore", "detail": str(exc)},
        ) from exc
    finally:
        # The upload was written here and is not wanted once it has been read,
        # whether it was restored or refused: /data/tmp is not where a
        # rejected archive should sit waiting to be picked up by anything.
        if isinstance(source, restore_core.UploadSource):
            await asyncio.to_thread(source.staged.path.unlink, True)  # missing_ok
    return _restore_response(result)


# -- status -----------------------------------------------------------------------------


class DestinationStatusResponse(_Payload):
    attempted: bool
    ok: bool | None
    reason: str | None


class BackupRunStatusResponse(_Payload):
    attempted_at: str
    source: Literal["scheduled", "manual"]
    archive_id: str | None
    result: Literal["success", "failed"]
    detail: str | None
    consecutive_failures: int
    retried: bool
    destinations: dict[str, DestinationStatusResponse]


class BackupVerifyStatusResponse(_Payload):
    verified_at: str
    archive_id: str | None
    ok: bool
    detail: str
    #: What the check found (§13.4): only "untrusted" says the archive is bad;
    #: "missing" and "unreachable" mean no copy could be read at all.
    outcome: Literal["verified", "untrusted", "missing", "unreachable", "none"]
    #: Which destination the checked copy was read from, when one was.
    destination: str | None


class BackupStatusResponse(_Payload):
    last_run: BackupRunStatusResponse | None
    last_verify: BackupVerifyStatusResponse | None
    #: The restore this appliance came back from, if it came back from one.
    #: Read from the restored database, so it is how "re-enter device
    #: passwords" (§6.10, §13.2) survives the restart a restore ends with —
    #: contracts §6's banner vocabulary is closed and has no key for it.
    #: Shown on the Backup screen until acknowledged or superseded.
    last_restore: LastRestoreResponse | None
    usb_present: bool
    retention_days: dict[str, int]


def _run_response(status: backup_core.BackupRunStatus) -> BackupRunStatusResponse:
    return BackupRunStatusResponse(
        attempted_at=status.attempted_at,
        source=status.source,
        archive_id=status.archive_id,
        result=status.job_result,
        detail=status.job_detail,
        consecutive_failures=status.consecutive_failures,
        retried=status.retried,
        destinations={
            name: DestinationStatusResponse(
                attempted=outcome.attempted, ok=outcome.ok, reason=outcome.reason
            )
            for name, outcome in status.destinations.items()
        },
    )


def _verify_response(status: backup_core.VerifyStatus) -> BackupVerifyStatusResponse:
    return BackupVerifyStatusResponse(
        verified_at=status.verified_at,
        archive_id=status.archive_id,
        ok=status.ok,
        detail=status.detail,
        outcome=status.outcome,
        destination=status.destination,
    )


@router.get("/status", response_model=BackupStatusResponse)
async def get_status(
    db: Annotated[Database, Depends(get_db)],
    config: Annotated[Config, Depends(get_config)],
    paths: Annotated[backup_core.BackupPaths, Depends(get_backup_paths)],
) -> BackupStatusResponse:
    last_run = await backup_core.read_status(db)
    last_verify = await backup_core.read_verify_status(db)
    last_restore = await restore_core.read_restore_record(db)
    usb_present = await asyncio.to_thread(is_mounted, paths.usb_dir)
    return BackupStatusResponse(
        last_run=None if last_run is None else _run_response(last_run),
        last_verify=None if last_verify is None else _verify_response(last_verify),
        last_restore=(
            None
            if last_restore is None
            else await _last_restore_response(last_restore, db, _device_secret(config))
        ),
        usb_present=usb_present,
        retention_days={str(k): v for k, v in RETENTION_DAYS.items()},
    )


@router.post("/restore/acknowledge", response_model=LastRestoreResponse)
async def acknowledge_restore(
    db: Annotated[Database, Depends(get_db)],
    config: Annotated[Config, Depends(get_config)],
) -> LastRestoreResponse:
    """Dismiss the last restore from the Backup screen (§21.24). The record
    stays until the next restore supersedes it; only its display ends."""
    record = await restore_core.acknowledge_restore(db)
    if record is None:
        raise ApiError(ErrorCode.NOT_FOUND, "No restore has been recorded on this appliance")
    return await _last_restore_response(record, db, _device_secret(config))


# -- run now (contracts §2 "backup-now") -------------------------------------------------


@router.post("/run", response_model=BackupRunStatusResponse)
async def run_now(
    request: Request,
    claims: Annotated[TokenClaims, Depends(require_admin)],
    db: Annotated[Database, Depends(get_db)],
) -> BackupRunStatusResponse:
    """"Back up now" (Q3): through the helper, which runs the same job as the
    nightly timer. Progress arrives as ``backup_run`` ``progress`` frames."""
    helper = get_helper(request)
    try:
        await helper.run("backup-now")
    except HelperError as exc:
        raise ApiError(
            ErrorCode.DEVICE_UNAVAILABLE, "The backup could not be started", {"detail": str(exc)}
        ) from exc
    status = await backup_core.read_status(db)
    if status is None:  # pragma: no cover - the helper always leaves a status
        raise ApiError(ErrorCode.INTERNAL_ERROR, "The backup finished but left no status")
    return _run_response(status)


# -- verify (contracts §5 "POST /system/backup/verify") ----------------------------------


@router.post("/verify", response_model=BackupVerifyStatusResponse)
async def verify_now(
    db: Annotated[Database, Depends(get_db)],
    paths: Annotated[backup_core.BackupPaths, Depends(get_backup_paths)],
    archive_id: str | None = None,
) -> BackupVerifyStatusResponse:
    """The monthly check, run on demand. Needs no privilege: reading an
    archive back is something the application can already do.

    ``?archive_id=`` checks that archive rather than a random one — how an
    archive wrongly marked untrusted is cleared without waiting for chance.
    """
    try:
        status = await backup_core.run_monthly_verify(db, paths, archive_id=archive_id)
    except LookupError as exc:
        raise ApiError(ErrorCode.NOT_FOUND, "No such backup archive") from exc
    return _verify_response(status)


# -- history and download ------------------------------------------------------------


class ArchiveSummaryResponse(_Payload):
    id: str
    created_at: str
    source: Literal["scheduled", "manual"]
    size_bytes: int
    sha256: str
    schema_version: int
    app_version: str
    local_present: bool
    usb_present: bool
    network_present: bool
    verified_at: str | None
    untrusted: bool
    untrusted_reason: str | None
    #: The backup run's own read-back check of the copies it wrote, distinct
    #: from the monthly check above. ``None`` for an older archive.
    checked_at: str | None
    checked_destinations: list[str]


class BackupHistoryResponse(_Payload):
    archives: list[ArchiveSummaryResponse]


def _summary(row: backup_crud.ArchiveRow) -> ArchiveSummaryResponse:
    return ArchiveSummaryResponse(
        id=row.id,
        created_at=row.created_at,
        source=row.source,  # type: ignore[arg-type]  # DB-validated at write time
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        schema_version=row.schema_version,
        app_version=row.app_version,
        local_present=row.local_present,
        usb_present=row.usb_present,
        network_present=row.network_present,
        verified_at=row.verified_at,
        untrusted=row.untrusted,
        untrusted_reason=row.untrusted_reason,
        checked_at=row.checked_at,
        checked_destinations=list(row.checked_destinations),
    )


@router.get("/history", response_model=BackupHistoryResponse)
async def history(db: Annotated[Database, Depends(get_db)]) -> BackupHistoryResponse:
    rows = await backup_crud.list_archives(db)
    return BackupHistoryResponse(archives=[_summary(r) for r in rows])


@router.get("/{archive_id}/download")
async def download(
    archive_id: str,
    db: Annotated[Database, Depends(get_db)],
    paths: Annotated[backup_core.BackupPaths, Depends(get_backup_paths)],
    config: Annotated[Config, Depends(get_config)],
) -> Response:
    row = await backup_crud.get_archive(db, archive_id)
    if row is None:
        raise ApiError(ErrorCode.NOT_FOUND, "No such backup archive")
    filename = archive_filename(row.id)

    if row.local_present:
        source_path = paths.local_dir / filename
        if source_path.is_file():
            return FileResponse(source_path, filename=filename, media_type="application/zstd")

    downloads_dir = backup_core.staging_dir_of(paths) / "downloads"
    await asyncio.to_thread(downloads_dir.mkdir, parents=True, exist_ok=True)
    target = downloads_dir / filename
    if row.usb_present:
        usb = UsbDestination(paths.usb_dir)
        if await usb.available():
            await usb.read(filename, target)
            return FileResponse(
                target,
                filename=filename,
                media_type="application/zstd",
                background=BackgroundTask(target.unlink, missing_ok=True),
            )
    if row.network_present:
        secret = _device_secret(config)
        network = await backup_core.load_network_destination(db, secret, state_dir=paths.state_dir)
        if network is not None:
            await network.read(filename, target)
            return FileResponse(
                target,
                filename=filename,
                media_type="application/zstd",
                background=BackgroundTask(target.unlink, missing_ok=True),
            )
    raise ApiError(
        ErrorCode.DEVICE_UNAVAILABLE,
        "This archive is not reachable from any destination right now",
        {"archive_id": archive_id},
    )


# -- snapshots (§18 Phase 7, §2.3) -------------------------------------------------------


class SnapshotListItem(_Payload):
    """One pre-change/-restore/-update snapshot, newest first.

    ``reason``, ``actor``, ``ip_address``, ``taken_at``, ``app_version`` and
    ``duration_ms`` come from the sidecar :mod:`proskenion.core.snapshots`
    writes beside every snapshot it takes — ``None`` for an older
    ``pre-update-``/``pre-restore-`` file from before that sidecar existed,
    which is still listed (the file itself, and its size, are always real),
    just with less to say about it.
    """

    name: str
    reason: str | None
    actor: str | None
    ip_address: str | None
    taken_at: str | None
    app_version: str | None
    size_bytes: int
    duration_ms: float | None


class SnapshotListResponse(_Payload):
    snapshots: list[SnapshotListItem]


def _snapshot_list_item(path: Path, sidecar: dict[str, Any] | None, size: int) -> SnapshotListItem:
    if sidecar is None:
        return SnapshotListItem(
            name=path.name,
            reason=None,
            actor=None,
            ip_address=None,
            taken_at=None,
            app_version=None,
            size_bytes=size,
            duration_ms=None,
        )
    duration = sidecar.get("duration_ms")
    return SnapshotListItem(
        name=str(sidecar.get("snapshot") or path.name),
        reason=_str_or_none(sidecar.get("reason")),
        actor=_str_or_none(sidecar.get("actor")),
        ip_address=_str_or_none(sidecar.get("ip_address")),
        taken_at=_str_or_none(sidecar.get("taken_at")),
        app_version=_str_or_none(sidecar.get("app_version")),
        size_bytes=size,
        duration_ms=float(duration) if isinstance(duration, int | float) else None,
    )


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _file_size(path: Path) -> int:
    return path.stat().st_size


@router.get("/snapshots", response_model=SnapshotListResponse)
async def list_backup_snapshots(
    paths: Annotated[backup_core.BackupPaths, Depends(get_backup_paths)],
) -> SnapshotListResponse:
    """Every pre-change, pre-restore and pre-update snapshot, newest first
    (§18 Phase 7) — the Backup screen's Snapshots list, and what
    ``POST /system/backup/restore``'s ``{"snapshot": …}`` accepts by name.
    """
    directory = snapshots_dir(paths.data_dir)
    files = await asyncio.to_thread(list_snapshots, directory)
    items: list[SnapshotListItem] = []
    for path in files:
        sidecar = await asyncio.to_thread(read_sidecar, path)
        try:
            size = await asyncio.to_thread(_file_size, path)
        except OSError:
            size = 0
        items.append(_snapshot_list_item(path, sidecar, size))
    return SnapshotListResponse(snapshots=items)


# -- destinations (contracts §5, §3) -----------------------------------------------------


class LocalDestinationInfo(_Payload):
    path: str
    retention_days: int


class UsbDestinationInfo(_Payload):
    path: str
    retention_days: int
    present: bool


class NetworkDestinationInfo(_Payload):
    protocol: Literal["smb", "sftp"] | None
    host: str | None
    port: int | None
    path: str | None
    username: str | None
    password_set: bool
    enabled: bool
    retention_days: int
    updated_at: str | None


class BackupDestinationsResponse(_Payload):
    local: LocalDestinationInfo
    usb: UsbDestinationInfo
    network: NetworkDestinationInfo


class NetworkDestinationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol: Literal["smb", "sftp"] | None = None
    host: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    path: str | None = Field(default=None, max_length=1024)
    username: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, max_length=255)
    enabled: bool = False

    @model_validator(mode="after")
    def _check_required(self) -> NetworkDestinationUpdate:
        if self.protocol is None:
            return self
        if not self.host or not self.path or not self.username:
            raise ValueError("host, path and username are required when a protocol is set")
        if self.protocol == "smb" and not self.password:
            # A password may already be stored from a previous save; the
            # endpoint fills it back in, so this only refuses a *first* save
            # with no password at all — smbclient authenticates with one.
            pass
        return self


async def _usb_info(paths: backup_core.BackupPaths) -> UsbDestinationInfo:
    present = await asyncio.to_thread(is_mounted, paths.usb_dir)
    return UsbDestinationInfo(
        path=str(paths.usb_dir), retention_days=RETENTION_DAYS["usb"], present=present
    )


async def _network_info(db: Database) -> NetworkDestinationInfo:
    row = await backup_crud.get_destination(db)
    if row is None:
        return NetworkDestinationInfo(
            protocol=None,
            host=None,
            port=None,
            path=None,
            username=None,
            password_set=False,
            enabled=False,
            retention_days=RETENTION_DAYS["network"],
            updated_at=None,
        )
    return NetworkDestinationInfo(
        protocol=row.protocol,  # type: ignore[arg-type]  # DB-validated at write time
        host=row.host,
        port=row.port,
        path=row.path,
        username=row.username,
        password_set=row.password is not None,
        enabled=row.enabled,
        retention_days=RETENTION_DAYS["network"],
        updated_at=row.updated_at,
    )


@router.get("/destinations", response_model=BackupDestinationsResponse)
async def get_destinations(
    db: Annotated[Database, Depends(get_db)],
    paths: Annotated[backup_core.BackupPaths, Depends(get_backup_paths)],
) -> BackupDestinationsResponse:
    return BackupDestinationsResponse(
        local=LocalDestinationInfo(
            path=str(paths.local_dir), retention_days=RETENTION_DAYS["local"]
        ),
        usb=await _usb_info(paths),
        network=await _network_info(db),
    )


async def _actor_id(db: Database, claims: TokenClaims) -> int | None:
    user = await users_crud.get_by_tier(db, claims.tier)
    return None if user is None else user.id


async def _sync_backup_firewall(
    paths: backup_core.BackupPaths, helper: HelperClient, body: NetworkDestinationUpdate
) -> None:
    """Mirror the network destination into ``system.json`` and ask the
    helper to re-render the firewall from it (contracts §4) — the same
    "the rule follows the saved row" reasoning
    :func:`proskenion.api.devices._sync_firewall` applies to the device
    table. ``auditorium-config-apply``'s ``_ip()`` only opens a rule for a
    literal IP address, not a hostname (unlike the SMTP relay, which it
    resolves, Q22) — a hostname destination is still saved and reachable
    over SMB/SFTP, it just has no firewall rule until that gap is closed.

    Best-effort, like the device mirror: a filesystem or helper hiccup here
    is logged, not turned into a failed save.
    """
    try:
        doc = await asyncio.to_thread(system_config.read, paths.data_dir)
        network_doc = system_config.backup_destination_network(
            doc, body.protocol, body.host, body.enabled
        )
        await asyncio.to_thread(system_config.merge, paths.data_dir, {"network": network_doc})
        await helper.submit("apply-network")
    except OSError as exc:
        log.warning("could not mirror the backup destination into the firewall: %s", exc)


@router.put("/destinations", response_model=BackupDestinationsResponse)
async def put_destinations(
    body: NetworkDestinationUpdate,
    request: Request,
    claims: Annotated[TokenClaims, Depends(require_admin)],
    db: Annotated[Database, Depends(get_db)],
    config: Annotated[Config, Depends(get_config)],
    paths: Annotated[backup_core.BackupPaths, Depends(get_backup_paths)],
) -> BackupDestinationsResponse:
    secret = _device_secret(config)
    existing = await backup_crud.get_destination(db)
    password_stored = existing.password if existing is not None else None
    if body.password:
        password_stored = secret.encrypt_value(_PASSWORD_FIELD, body.password)
    elif body.protocol != "smb":
        password_stored = None  # SFTP never stores a password (key auth)

    if body.protocol == "sftp":
        # The device's own key pair, generated on first need — nothing to
        # store here, and never a password.
        generate_sftp_keypair_if_missing(paths.state_dir)
        password_stored = None

    await backup_crud.upsert_destination(
        db,
        protocol=body.protocol,
        host=body.host,
        port=body.port,
        path=body.path,
        username=body.username,
        password=password_stored,
        enabled=body.enabled,
        updated_by=await _actor_id(db, claims),
    )
    await _sync_backup_firewall(paths, get_helper(request), body)
    await record_event(
        db,
        "config_changed",
        user_ident=claims.tier,
        ip_address=client_ip(request),
        detail={
            "setting": "backup_destination",
            "protocol": body.protocol,
            "host": body.host,
            "enabled": body.enabled,
            "password_changed": bool(body.password),
        },
    )
    return BackupDestinationsResponse(
        local=LocalDestinationInfo(
            path=str(paths.local_dir), retention_days=RETENTION_DAYS["local"]
        ),
        usb=await _usb_info(paths),
        network=await _network_info(db),
    )


# -- the SFTP public key (contracts §5) ---------------------------------------------------


@router.get("/sftp-key")
async def sftp_key(config: Annotated[Config, Depends(get_config)]) -> Response:
    text = await asyncio.to_thread(sftp_public_key_text, config.app.state_dir)
    return Response(content=text, media_type="text/plain")


__all__ = ["router"]
