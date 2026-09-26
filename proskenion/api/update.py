"""Application update endpoints (contracts §5 ``/system/update*``, §16.7, §21.24).

Three verbs and a reading: upload a package for review, apply it (now or at
the next quiet moment), roll back, and ask what is going on.

The upload never touches memory (Q9)
    ``POST /system/update`` reads the request body as a stream and writes it
    to ``/data/tmp`` chunk by chunk, hashing on the way through. It does not
    use ``UploadFile``, which spools through a buffer in ``/tmp`` with a
    memory threshold: this appliance has 4 GB of RAM, a package may be 2 GB,
    and ``/tmp`` is ``PrivateTmp`` on the read-only root. Verification then
    reads the file on disk, and **nothing is extracted until it has passed**
    (contracts §3).

One route, two kinds of package (§14.1, contracts §3)
    ``POST /system/update`` takes an application package or an OS package.
    The manifest's ``type`` says which, and the upload is handed to the
    service that applies that kind -- which then verifies it with
    ``expect_type`` set, so an ``os`` package that reached the application
    path is refused by the verifier and the reverse. The peek that routes is
    unverified by construction and decides nothing but which verification
    runs.

A refusal names the rule that refused it
    Every :class:`~proskenion.core.packages.PackageError` and
    :class:`~proskenion.core.update.UpdateError` carries a stable ``rule`` and
    a sentence written for the operator. They become the ``detail`` and the
    message of a §16.1 ``validation_failed`` envelope, so §21.24's rejection
    panel says what happened rather than reconstructing it from a string.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from proskenion.api.deps import client_ip, get_db, require_admin
from proskenion.api.errors import ApiError, ErrorCode
from proskenion.api.os_upgrade import optional_os_upgrade
from proskenion.core.auth import TokenClaims
from proskenion.core.osupgrade import OsUpgradeService, SlotsUnavailable, read_os_pending
from proskenion.core.packages import PackageError, peek_manifest_type
from proskenion.core.update import (
    MAX_UPLOAD_BYTES,
    AppliedUpdate,
    QuietReport,
    UpdateError,
    manifest_json,
    read_pending,
    stage_upload,
)
from proskenion.core.update_service import UpdateService
from proskenion.db.connection import Database

router = APIRouter(prefix="/system/update", tags=["update"])


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


def get_updates(request: Request) -> UpdateService:
    service: UpdateService | None = getattr(request.app.state, "updates", None)
    if service is None:
        raise ApiError(
            ErrorCode.INTERNAL_ERROR,
            "Updates are not running",
            {"reason": "not_started"},
        )
    return service


def _refuse(exc: PackageError | UpdateError) -> ApiError:
    """Turn a refusal into the §16.1 envelope §21.24 knows how to render."""
    return ApiError(
        ErrorCode.VALIDATION_FAILED,
        exc.summary,
        {"rule": exc.rule, "reason": str(exc)},
    )


# -- upload and review (§21.24) --------------------------------------------------------


class ManifestResponse(_Payload):
    """The verified manifest, for the review card."""

    manifest: dict[str, Any]
    sha256: str
    size: int


@router.post("", response_model=ManifestResponse, dependencies=[Depends(require_admin)])
async def upload(
    request: Request,
    service: Annotated[UpdateService, Depends(get_updates)],
    os_service: Annotated[OsUpgradeService | None, Depends(optional_os_upgrade)],
) -> ManifestResponse:
    """Stream a package to ``/data/tmp``, verify it, and answer its manifest.

    The answer is what §21.24 shows for review — version, build date, minimum
    required version and the change list — and not a promise that anything has
    been installed. Nothing is extracted here at all.

    An OS package goes to the slot service instead of the application one, on
    the strength of a manifest ``type`` nothing has authenticated yet. That is
    safe because it only chooses which verification runs: each service passes
    its own ``expect_type``, so a package claiming to be one kind while being
    the other is refused whichever way round it is offered (contracts §3).
    """
    try:
        staged = await stage_upload(request.stream(), service.paths, max_bytes=MAX_UPLOAD_BYTES)
    except UpdateError as exc:
        raise _refuse(exc) from exc
    declared = await asyncio.to_thread(peek_manifest_type, staged.path)
    if declared == "os" and os_service is None:
        staged.path.unlink(missing_ok=True)
        raise _refuse(SlotsUnavailable("this appliance has no A/B root slots"))
    try:
        if os_service is not None and declared == "os":
            manifest = await os_service.accept(staged)
        else:
            manifest = await service.accept(staged)
    except (PackageError, UpdateError) as exc:
        raise _refuse(exc) from exc
    return ManifestResponse(
        manifest=manifest_json(manifest), sha256=staged.sha256, size=staged.size
    )


class DiscardResponse(_Payload):
    discarded: bool


@router.delete("", response_model=DiscardResponse, dependencies=[Depends(require_admin)])
async def discard(
    service: Annotated[UpdateService, Depends(get_updates)],
    os_service: Annotated[OsUpgradeService | None, Depends(optional_os_upgrade)],
) -> DiscardResponse:
    """§21.24's Discard: forget the package and take its upload with it.

    Both kinds. Discard is one button, and the admin who pressed it does not
    care which of the two records the upload ended up in.
    """
    discarded = await service.discard()
    if os_service is not None:
        discarded = await os_service.discard() or discarded
    return DiscardResponse(discarded=discarded)


# -- apply (§14.2, Q17) ----------------------------------------------------------------


class ApplyBody(_Payload):
    when: Literal["now", "quiet"] = "now"


class ApplyResponse(_Payload):
    state: str
    applied: dict[str, Any] | None = None
    quiet: dict[str, Any] | None = None
    trial: dict[str, Any] | None = None


@router.post("/apply", response_model=ApplyResponse, dependencies=[Depends(require_admin)])
async def apply(
    body: ApplyBody,
    request: Request,
    claims: Annotated[TokenClaims, Depends(require_admin)],
    db: Annotated[Database, Depends(get_db)],
    service: Annotated[UpdateService, Depends(get_updates)],
    os_service: Annotated[OsUpgradeService | None, Depends(optional_os_upgrade)],
) -> ApplyResponse:
    """Apply now, or arm the update for the next quiet moment (§14.2, Q17).

    Either way the package is extracted, its environment built and its
    migrations tested against a copy of the database before this answers —
    so a package that cannot work says so while somebody is here to read it.
    A failure at that point has changed nothing.

    ``when: "now"`` restarts the application, which is this process: the
    answer may well be overtaken by the restart, and §21.24 hands over to the
    reconnection screen at exactly that moment.
    """
    del db  # the audit row is written through the service's own handle
    if os_service is not None and await asyncio.to_thread(
        _os_package_waiting, service, os_service
    ):
        # §14.4: an OS upgrade is rare and deliberate, done at a desk with the
        # recovery USB to hand, and it reboots rather than restarting. "At the
        # next quiet moment" is for the application path, where the appliance
        # comes back in seconds; an unattended reboot into an operating system
        # that has never run here is the opposite of what Q17 is for, so
        # `when` does not apply to it and the upgrade goes now.
        try:
            trial = await os_service.apply(
                user_ident=claims.tier, ip_address=client_ip(request)
            )
        except (PackageError, UpdateError) as exc:
            raise _refuse(exc) from exc
        return ApplyResponse(state="os_trial", trial=trial.to_json())
    try:
        outcome = await service.apply(body.when)
    except (PackageError, UpdateError) as exc:
        raise _refuse(exc) from exc
    if isinstance(outcome, AppliedUpdate):
        await service.audit_applied(
            outcome, user_ident=claims.tier, ip_address=client_ip(request), when=body.when
        )
        return ApplyResponse(state="applied", applied=_applied_json(outcome))
    quiet: QuietReport = outcome
    return ApplyResponse(state="waiting_for_quiet", quiet=quiet.to_json())


# -- rollback (§14.3) ------------------------------------------------------------------


class RollbackBody(_Payload):
    to: str | None = None


class RollbackResponse(_Payload):
    from_version: str
    to_version: str
    snapshot: str | None


@router.post("/rollback", response_model=RollbackResponse, dependencies=[Depends(require_admin)])
async def rollback(
    body: RollbackBody,
    request: Request,
    claims: Annotated[TokenClaims, Depends(require_admin)],
    service: Annotated[UpdateService, Depends(get_updates)],
) -> RollbackResponse:
    """Repoint ``current``, restore the pre-update snapshot, restart — in that order."""
    try:
        rolled = await service.roll_back(to=body.to)
    except (PackageError, UpdateError) as exc:
        raise _refuse(exc) from exc
    await service.audit_rolled_back(
        rolled, user_ident=claims.tier, ip_address=client_ip(request)
    )
    return RollbackResponse(
        from_version=rolled.from_version,
        to_version=rolled.to_version,
        snapshot=rolled.snapshot,
    )


# -- status (§16.7) --------------------------------------------------------------------


class StatusResponse(_Payload):
    installed_version: str | None
    state: str
    error: str | None
    rule: str | None
    pending: dict[str, Any] | None
    quiet: dict[str, Any]
    previous_versions: list[str]
    history: list[dict[str, Any]]
    rolled_back: dict[str, Any] | None


@router.get("/status", response_model=StatusResponse, dependencies=[Depends(require_admin)])
async def status(service: Annotated[UpdateService, Depends(get_updates)]) -> StatusResponse:
    return StatusResponse(**service.status())


def _os_package_waiting(service: UpdateService, os_service: OsUpgradeService) -> bool:
    """Whether the package waiting to be applied is an OS one.

    The application's record is consulted too, so an OS package left over
    from a session the admin abandoned never diverts an application update
    uploaded afterwards. The more recently accepted record wins, which is the
    one the review card is showing.
    """
    pending_os = read_os_pending(os_service.paths)
    if pending_os is None:
        return False
    pending_app = read_pending(service.paths)
    return pending_app is None or pending_app.received_at <= pending_os.received_at


def _applied_json(applied: AppliedUpdate) -> dict[str, Any]:
    return {
        "from_version": applied.from_version,
        "to_version": applied.to_version,
        "snapshot": applied.snapshot,
        "at": applied.at,
    }
