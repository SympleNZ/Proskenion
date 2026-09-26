"""System image endpoints (contracts §5 ``/system/images*``, §13.6, Q13).

All four routes are admin-only, matching every other `/system/*` write except
the two contracts already carve out as public (certificate download, and
``/health``). Capture and restore both block for the duration — the same
shape ``POST /system/certs/issue`` and ``POST /system/backup/run`` use —
while ``image_capture``/``image_restore`` ``progress`` frames stream over the
WebSocket in parallel (contracts §6).

The work, the checks and the order are :mod:`proskenion.core.images`; this
module is the route, the §16.1 envelope a refusal becomes, and the response
shape.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from proskenion.api.deps import client_ip, require_admin
from proskenion.api.errors import ApiError, ErrorCode
from proskenion.core.auth import TokenClaims
from proskenion.core.images import (
    CaptureInProgress,
    ImageError,
    ImageNotFound,
    ImagesService,
    NoActiveVersion,
    SlotsUnavailable,
)
from proskenion.db.crud.images import ImageRow

router = APIRouter(prefix="/system/images", tags=["images"], dependencies=[Depends(require_admin)])


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


def get_images(request: Request) -> ImagesService:
    """Built once in ``create_app``, the same as ``app.state.os_upgrade`` —
    its capture lock only means anything if the same instance answers every
    request."""
    service: ImagesService | None = getattr(request.app.state, "images", None)
    if service is None:
        raise ApiError(
            ErrorCode.INTERNAL_ERROR, "System images are not running", {"reason": "not_started"}
        )
    return service


def _refuse(exc: ImageError) -> ApiError:
    """The §16.1 envelope §21.24 renders, carrying the rule that refused it."""
    if isinstance(exc, ImageNotFound):
        return ApiError(ErrorCode.NOT_FOUND, exc.summary, {"rule": exc.rule, "reason": str(exc)})
    if isinstance(exc, CaptureInProgress | SlotsUnavailable):
        return ApiError(
            ErrorCode.DEVICE_UNAVAILABLE, exc.summary, {"rule": exc.rule, "reason": str(exc)}
        )
    return ApiError(
        ErrorCode.VALIDATION_FAILED, exc.summary, {"rule": exc.rule, "reason": str(exc)}
    )


class ImageResponse(_Payload):
    id: str
    filename: str
    created_at: str
    slot: str
    os_version: str
    #: Always null: a system image is the root filesystem only (§2.3) —
    #: the application lives on /data and is never part of a capture.
    #: web/src/admin/backup/types.ts's SystemImage carries this field
    #: (built from the contract alone, before this router existed); this
    #: is the shape it expects, kept rather than dropped so the screen
    #: needs no change here.
    app_version: str | None = None
    size_bytes: int
    sha256: str
    key_id: str | None
    local_present: bool
    usb_present: bool


def _image_response(row: ImageRow) -> ImageResponse:
    return ImageResponse(
        id=row.id,
        filename=row.filename,
        created_at=row.created_at,
        slot=row.slot,
        os_version=row.version,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        key_id=row.key_id,
        local_present=row.local_present,
        usb_present=row.usb_present,
    )


class ImageListResponse(_Payload):
    images: list[ImageResponse]


@router.get("", response_model=ImageListResponse)
async def list_images(
    service: Annotated[ImagesService, Depends(get_images)],
) -> ImageListResponse:
    """§21.24's images card: newest first."""
    return ImageListResponse(images=[_image_response(row) for row in await service.list()])


@router.post("/capture", response_model=ImageResponse)
async def capture(
    request: Request,
    claims: Annotated[TokenClaims, Depends(require_admin)],
    service: Annotated[ImagesService, Depends(get_images)],
) -> ImageResponse:
    """Capture the active slot (§13.6). Progress streams as ``image_capture``."""
    try:
        row = await service.capture(user_ident=claims.tier, ip_address=client_ip(request))
    except (NoActiveVersion, SlotsUnavailable, CaptureInProgress, ImageError) as exc:
        raise _refuse(exc) from exc
    return _image_response(row)


class RestoreResponse(_Payload):
    image_id: str
    slot: str
    restarted: bool
    os_version: str


@router.post("/{image_id}/restore", response_model=RestoreResponse)
async def restore(
    image_id: str,
    request: Request,
    claims: Annotated[TokenClaims, Depends(require_admin)],
    service: Annotated[ImagesService, Depends(get_images)],
) -> RestoreResponse:
    """Write the standby slot and boot it on trial (§14.4) — never the running
    slot. The answer may well not reach the browser; §21.24 hands over to the
    reconnection screen at exactly this point, as it does for an OS upgrade."""
    try:
        outcome = await service.restore(
            image_id, user_ident=claims.tier, ip_address=client_ip(request)
        )
    except ImageError as exc:
        raise _refuse(exc) from exc
    return RestoreResponse(**outcome)


@router.delete("/{image_id}", status_code=204)
async def delete(
    image_id: str,
    request: Request,
    claims: Annotated[TokenClaims, Depends(require_admin)],
    service: Annotated[ImagesService, Depends(get_images)],
) -> None:
    try:
        await service.delete(image_id, user_ident=claims.tier, ip_address=client_ip(request))
    except ImageError as exc:
        raise _refuse(exc) from exc
