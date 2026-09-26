"""Venue baseline endpoints (contracts §5 ``/system/baseline*``, §13.5, §21.24).

The work is :mod:`proskenion.core.baseline`; this module is the §21.24 card,
the diff and the three buttons on it. All three are admin: capturing is
"deliberate and admin-only" (§13.5), and comparing exposes the whole
configuration.

The service is built per request from ``app.state``, rather than in the
lifespan, because it holds nothing between calls — the files under
``/data/config/baselines`` are the state. Whatever subsystems are running at
the time are handed to it; one that is not running is simply not rebuilt,
which is what a restore on a bare database needs.

``POST /system/baseline/restore`` reports its six steps as ``progress``
frames with the operation ``baseline_restore`` (contracts §6) and answers
with what it did once the transaction has committed and the live state has
been rebuilt.

Beyond the contract, both ``compare`` and ``restore`` accept an optional
``file`` naming one of the dated copies ``GET /system/baseline`` lists.
§13.5 requires a restore to be reversible from its own pre-restore snapshot,
and a snapshot that nothing can restore would not make it so.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from proskenion.api.deps import client_ip, get_db, require_admin
from proskenion.api.errors import ApiError, ErrorCode
from proskenion.api.snapshots import PreChangeSnapshot
from proskenion.config import Config
from proskenion.core.auth import TokenClaims, record_event
from proskenion.core.baseline import (
    CURRENT_FILENAME,
    AreaDiff,
    BaselineDiff,
    BaselineError,
    BaselineInfo,
    BaselineService,
    BaselineStore,
    LiveSystem,
    MissingDevicesError,
    NoBaselineError,
    RestoreResult,
    RowChange,
)
from proskenion.db.connection import Database
from proskenion.db.migrations import SCHEMA_AHEAD_MESSAGE, SchemaAhead
from proskenion.scene.engine import ScenesStillRunningError

router = APIRouter(prefix="/system/baseline", tags=["baseline"])


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


def get_baseline(request: Request) -> BaselineService:
    """Build the service over whatever the application is currently running."""
    state = request.app.state
    db: Database | None = state.db
    if db is None:
        raise ApiError(
            ErrorCode.INTERNAL_ERROR, "The database is not open", {"reason": "not_started"}
        )
    config: Config = state.config
    rules = getattr(state, "rules", None)
    return BaselineService(
        db,
        BaselineStore(config.app.data_dir),
        live=LiveSystem(
            scenes=getattr(state, "scene_engine", None),
            knx_registry=getattr(state, "knx_registry", None),
            lighting=getattr(state, "lighting", None),
            rules=rules,
            pages=getattr(state, "default_pages", None),
            hirer_permissions=getattr(state, "hirer_permissions", None),
            ceilings=getattr(state, "ceilings", None),
            bus=getattr(state, "bus", None),
            broadcaster=getattr(state, "broadcaster", None),
        ),
    )


# -- responses -------------------------------------------------------------------


class BaselineCard(_Payload):
    """One baseline file, as §21.24's card shows it."""

    name: str
    captured_at: str
    captured_by: str | None
    schema_version: str | None
    app_version: str | None
    size_bytes: int
    contents: dict[str, int]


class BaselineStateResponse(_Payload):
    """``GET /system/baseline`` — the current baseline and its dated copies (Q14)."""

    current: BaselineCard | None
    copies: list[BaselineCard]


class FieldChangeJson(_Payload):
    field: str
    before: Any
    after: Any


class RowChangeJson(_Payload):
    area: str
    entity: str
    id: int
    name: str
    change: Literal["added", "removed", "changed"]
    fields: list[FieldChangeJson]
    before: dict[str, Any] | None
    after: dict[str, Any] | None


class AreaDiffJson(_Payload):
    area: str
    changes: list[RowChangeJson]


class CompareResponse(_Payload):
    """``GET /system/baseline/compare`` — §21.24's "Changes since the baseline"."""

    baseline: BaselineCard
    areas: list[AreaDiffJson]
    #: Migrations applied to the copy before comparing (Q14); empty when none.
    migrated: list[str]
    changes: int


class RestoreBody(_Payload):
    """Which baseline to restore; the current one by default."""

    file: str | None = Field(default=None, max_length=255)


class RestoreResponse(_Payload):
    """``POST /system/baseline/restore`` — what was applied, and what moved."""

    baseline: BaselineCard
    #: The pre-restore snapshot, so this restore is itself reversible (§13.5).
    snapshot: str
    migrated: list[str]
    restored: dict[str, int]
    #: Mixer channels pulled down to a lowered hirer ceiling (§6.7, Q8a).
    pulled_down: dict[str, float]


def _card(info: BaselineInfo) -> BaselineCard:
    return BaselineCard(
        name=info.name,
        captured_at=info.captured_at,
        captured_by=info.captured_by,
        schema_version=info.schema_version,
        app_version=info.app_version,
        size_bytes=info.size_bytes,
        contents=dict(info.contents),
    )


def _row(change: RowChange) -> RowChangeJson:
    return RowChangeJson(
        area=change.area,
        entity=change.entity,
        id=change.row_id,
        name=change.name,
        change=change.change,
        fields=[
            FieldChangeJson(field=f.field, before=f.before, after=f.after) for f in change.fields
        ],
        before=None if change.before is None else dict(change.before),
        after=None if change.after is None else dict(change.after),
    )


def _area(area: AreaDiff) -> AreaDiffJson:
    return AreaDiffJson(area=area.area, changes=[_row(c) for c in area.changes])


def _compare_response(diff: BaselineDiff) -> CompareResponse:
    return CompareResponse(
        baseline=_card(diff.baseline),
        areas=[_area(area) for area in diff.areas],
        migrated=list(diff.migrated),
        changes=diff.count,
    )


def _restore_response(result: RestoreResult) -> RestoreResponse:
    return RestoreResponse(
        baseline=_card(result.baseline),
        snapshot=result.snapshot,
        migrated=list(result.migrated),
        restored=dict(result.restored),
        pulled_down={str(k): v for k, v in sorted(result.pulled_down.items())},
    )


def _not_found(exc: NoBaselineError) -> ApiError:
    return ApiError(
        ErrorCode.NOT_FOUND,
        "No baseline by that name has been captured",
        {"file": exc.name},
    )


def _schema_ahead(exc: SchemaAhead) -> ApiError:
    return ApiError(
        ErrorCode.CONFLICT,
        SCHEMA_AHEAD_MESSAGE,
        {"reason": "schema_ahead", "recorded": exc.recorded, "shipped": exc.shipped},
    )


# -- the three operations (§13.5) -------------------------------------------------


@router.get("", response_model=BaselineStateResponse, dependencies=[Depends(require_admin)])
async def baseline_state(
    service: Annotated[BaselineService, Depends(get_baseline)],
) -> BaselineStateResponse:
    """The current baseline's card, and every dated copy beside it (Q14)."""
    current = await service.store.current_info()
    return BaselineStateResponse(
        current=None if current is None else _card(current),
        copies=[_card(info) for info in await service.store.copies()],
    )


@router.post("", response_model=BaselineCard)
async def capture(
    request: Request,
    claims: Annotated[TokenClaims, Depends(require_admin)],
    service: Annotated[BaselineService, Depends(get_baseline)],
    db: Annotated[Database, Depends(get_db)],
) -> BaselineCard:
    """Capture a new current baseline, keeping the one it replaces (§13.5, Q14)."""
    info = await service.capture(captured_by=claims.tier)
    await record_event(
        db,
        "config_changed",
        user_ident=claims.tier,
        ip_address=client_ip(request),
        detail={
            "setting": "venue_baseline",
            "action": "captured",
            "file": info.name,
            "schema_version": info.schema_version,
            "app_version": info.app_version,
        },
    )
    return _card(info)


@router.get("/compare", response_model=CompareResponse, dependencies=[Depends(require_admin)])
async def compare(
    service: Annotated[BaselineService, Depends(get_baseline)],
    file: str | None = None,
) -> CompareResponse:
    """What has drifted since the baseline was captured (§21.24).

    The baseline is migrated forward on a copy first, so one captured
    against an older schema still compares (Q14).
    """
    try:
        return _compare_response(await service.compare(file or CURRENT_FILENAME))
    except NoBaselineError as exc:
        raise _not_found(exc) from exc
    except SchemaAhead as exc:
        raise _schema_ahead(exc) from exc


@router.post("/restore", response_model=RestoreResponse)
async def restore(
    body: RestoreBody,
    request: Request,
    claims: Annotated[TokenClaims, Depends(require_admin)],
    snapshot: PreChangeSnapshot,
    service: Annotated[BaselineService, Depends(get_baseline)],
    db: Annotated[Database, Depends(get_db)],
) -> RestoreResponse:
    """Apply the baseline in one transaction and rebuild the live state.

    Refuses, and lists every one, if the baseline needs a device this
    appliance no longer has: a baseline never creates a device (§13.5).
    """
    try:
        name = body.file or CURRENT_FILENAME
        result = await service.restore(
            name, snapshot=lambda: snapshot(f"restore venue baseline {name}")
        )
    except NoBaselineError as exc:
        raise _not_found(exc) from exc
    except SchemaAhead as exc:
        raise _schema_ahead(exc) from exc
    except MissingDevicesError as exc:
        raise ApiError(
            ErrorCode.CONFLICT,
            "This baseline needs devices this appliance no longer has. "
            "Recreate them on the Devices screen, then restore again.",
            {
                "reason": "missing_devices",
                "devices": [
                    {
                        "id": device.id,
                        "name": device.name,
                        "category": device.category,
                        "driver_key": device.driver_key,
                    }
                    for device in exc.devices
                ],
            },
        ) from exc
    except ScenesStillRunningError as exc:
        raise ApiError(
            ErrorCode.CONFLICT,
            "A scene is still running. Wait for it to finish, then restore again.",
            {"reason": "scene_running", "scene_ids": list(exc.scene_ids)},
        ) from exc
    except BaselineError as exc:
        raise ApiError(
            ErrorCode.CONFLICT,
            "The baseline could not be applied and nothing was changed",
            {"reason": "restore_failed", "detail": str(exc)},
        ) from exc
    await record_event(
        db,
        "baseline_restored",
        user_ident=claims.tier,
        ip_address=client_ip(request),
        detail={
            "file": result.baseline.name,
            "captured_at": result.baseline.captured_at,
            "snapshot": result.snapshot,
            "migrated": list(result.migrated),
            "rows": sum(result.restored.values()),
            "pulled_down": {str(k): v for k, v in sorted(result.pulled_down.items())},
        },
    )
    return _restore_response(result)
