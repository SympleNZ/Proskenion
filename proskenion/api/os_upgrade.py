"""The A/B root slots, from the admin interface (contracts §5, §14.4, §21.24).

Two routes, and neither of them is where an OS upgrade begins. Uploading is
``POST /system/update``, which takes both kinds of package and routes on the
manifest's ``type`` (§14.1: one format, one verification path, two payloads);
applying is ``POST /system/update/apply``, which sends an OS package down the
slot path instead of the application path. What is here is the reading — what
is in each slot and where the trial has got to — and the one button that
cannot be expressed as an update: go back to the other slot.

Both are admin. A slot change reboots the appliance into a different
operating system, which is not something an operator holds the authority for,
and the reading names versions and partition state that an operator has no
use for either.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from proskenion.api.deps import client_ip, require_admin
from proskenion.api.errors import ApiError, ErrorCode
from proskenion.core.auth import TokenClaims
from proskenion.core.osupgrade import OsUpgradeService
from proskenion.core.packages import PackageError
from proskenion.core.update import UpdateError

router = APIRouter(prefix="/system/os", tags=["update"])


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


def optional_os_upgrade(request: Request) -> OsUpgradeService | None:
    """The slot service if this appliance has one, and ``None`` otherwise.

    ``/system/update`` depends on it this way rather than strictly: the
    application update path has to keep working on a machine with no A/B
    slots — a development host, a port whose bootloader support is still in
    ``porting.md`` — and an application package has nothing to do with slots.
    Only a package that says it is an OS package needs the service to exist,
    and that one is refused with a reason rather than a 500.
    """
    service: OsUpgradeService | None = getattr(request.app.state, "os_upgrade", None)
    return service


def get_os_upgrade(request: Request) -> OsUpgradeService:
    service = optional_os_upgrade(request)
    if service is None:
        raise ApiError(
            ErrorCode.INTERNAL_ERROR,
            "OS upgrades are not running",
            {"reason": "not_started"},
        )
    return service


def refuse(exc: PackageError | UpdateError) -> ApiError:
    """The §16.1 envelope §21.24 renders, carrying the rule that refused it."""
    return ApiError(
        ErrorCode.VALIDATION_FAILED,
        exc.summary,
        {"rule": exc.rule, "reason": str(exc)},
    )


class OsStatusResponse(_Payload):
    """§21.24's OS card: the slots, their versions, and the trial."""

    active_slot: str | None
    standby_slot: str | None
    active_version: str | None
    standby_version: str | None
    last_known_good: str | None
    staged: str | None
    trial: dict[str, Any] | None
    pending: dict[str, Any] | None


@router.get("", response_model=OsStatusResponse, dependencies=[Depends(require_admin)])
async def status(
    service: Annotated[OsUpgradeService, Depends(get_os_upgrade)],
) -> OsStatusResponse:
    """Which slot is running, what the other one holds, and the trial's deadline."""
    return OsStatusResponse(**await service.status())


class OsRollbackResponse(_Payload):
    slot: str
    version: str | None
    mode: str


@router.post("/rollback", response_model=OsRollbackResponse, dependencies=[Depends(require_admin)])
async def rollback(
    request: Request,
    claims: Annotated[TokenClaims, Depends(require_admin)],
    service: Annotated[OsUpgradeService, Depends(get_os_upgrade)],
) -> OsRollbackResponse:
    """Go back to the other root slot (§14.4).

    The answer is written before the machine goes away and may well not reach
    the browser; §21.24 hands over to the reconnection screen at exactly this
    point, as it does for an apply.
    """
    try:
        outcome = await service.roll_back(
            user_ident=claims.tier, ip_address=client_ip(request)
        )
    except (PackageError, UpdateError) as exc:
        raise refuse(exc) from exc
    return OsRollbackResponse(**outcome)
