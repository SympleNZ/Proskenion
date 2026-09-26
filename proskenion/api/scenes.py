"""Scenes endpoints (spec §16.5 *Scenes*, §16.1 configuration REST, §21.10, §21.16).

Configuration — admin, with §16.1 optimistic concurrency on every ``PUT``::

    GET/POST        /scenes                      (GET is admin and operator)
    GET/PUT/DELETE  /scenes/{id}
    GET             /scenes/{id}/references
    GET/POST        /scenes/{id}/actions
    GET/PUT/DELETE  /scenes/{id}/actions/{action_id}

Control::

    POST /scenes/{id}/trigger     [admin, operator]
    POST /scenes/{id}/test        [admin]   respects delays; reports inline
    POST /scenes/{id}/test-group  [admin]   { delay_ms } — that group, now
    GET  /scenes/{id}/log         [admin, operator]
    GET  /scenes/log              [admin, operator]   date range, result
    GET  /scenes/domains          [admin]   per-domain availability for the editor (§21.16)

**Hirers.** §16.5 gives hirers ``GET /scenes`` and ``trigger`` on a page
assigned to them (§15.12). Page assignment arrives in Phase 5; until then
both are admin and operator only.

**Protected scenes cannot be deleted** (§8.11) — Restore Venue Default is
what the operator presses after a hire. The refusal is ``permission_denied``
with ``detail.reason = "protected"``: the vocabulary is closed, and "show and
stop; no retry" is exactly what the client should do.

Actions are validated on save by :mod:`proskenion.scene.validation`: the
eight domains, the fields each needs, snapshot channels that exist, and an
action type the target driver does not support refused with
``validation_failed`` (§5.5).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Body, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict
from pydantic import Field as ModelField

from proskenion.api.deps import get_db, require_admin, require_staff
from proskenion.api.errors import ApiError, ErrorCode
from proskenion.api.snapshots import PreChangeSnapshot
from proskenion.core.auth import TokenClaims
from proskenion.core.drivers.categories import Category
from proskenion.core.events import SceneConfigChanged
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import scenes as scenes_crud
from proskenion.db.crud.base import AUCKLAND, ConflictError, NotFoundError
from proskenion.db.crud.refs import InUseError
from proskenion.db.crud.scenes import Scene, SceneAction
from proskenion.scene import log as scene_log
from proskenion.scene.domains import DOMAIN_CATEGORY, DOMAINS, CapabilitySource
from proskenion.scene.engine import (
    NoActionsAtDelayError,
    SceneDisabledError,
    SceneEngine,
    SceneEngineStoppedError,
    SceneNotFoundError,
    SceneRunHandle,
    SceneRunResult,
)
from proskenion.scene.validation import ACTION_COLUMNS, ActionValidationError, validate_action

log = logging.getLogger(__name__)

router = APIRouter(tags=["scenes"])

#: The §16.1 optimistic-concurrency header every configuration ``PUT`` carries.
VERSION_HEADER = "If-Unmodified-Since-Version"

Admin = Annotated[TokenClaims, Depends(require_admin)]
Staff = Annotated[TokenClaims, Depends(require_staff)]
Db = Annotated[Database, Depends(get_db)]


def get_scene_engine(request: Request) -> SceneEngine:
    """The running scene engine; ``device_unavailable`` before startup has built it."""
    engine: SceneEngine | None = getattr(request.app.state, "scene_engine", None)
    if engine is None:
        raise ApiError(
            ErrorCode.DEVICE_UNAVAILABLE,
            "The scene engine is not running",
            {"reason": "not_started"},
        )
    return engine


Engine = Annotated[SceneEngine, Depends(get_scene_engine)]


# -- models ----------------------------------------------------------------------------

Priority = Literal["normal", "critical"]
Result = Literal["success", "partial", "failed"]


class LastRunModel(BaseModel):
    log_id: int
    triggered_by: str
    started_at: str
    completed_at: str | None
    result: str | None


class SceneModel(BaseModel):
    id: int
    name: str
    description: str | None
    enabled: bool
    icon: str | None
    priority: str
    protected: bool
    visible_operator: bool
    sort_order: int
    created_at: str
    updated_at: str
    running: bool = False
    last_run: LastRunModel | None = None


class ScenesResponse(BaseModel):
    scenes: list[SceneModel]


class SceneCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = ModelField(min_length=1, max_length=120)
    description: str | None = ModelField(default=None, max_length=2000)
    enabled: bool = True
    icon: str | None = ModelField(default=None, max_length=64)
    priority: Priority = "normal"
    protected: bool = False
    visible_operator: bool = True
    sort_order: int = 0


class SceneUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = ModelField(default=None, min_length=1, max_length=120)
    description: str | None = ModelField(default=None, max_length=2000)
    enabled: bool | None = None
    icon: str | None = ModelField(default=None, max_length=64)
    priority: Priority | None = None
    protected: bool | None = None
    visible_operator: bool | None = None
    sort_order: int | None = None


class ActionModel(BaseModel):
    id: int
    scene_id: int
    sort_order: int
    delay_ms: int
    domain: str
    knx_address_id: int | None
    knx_value: str | None
    knx_source: str
    knx_scale: str | None
    dmx_snapshot: dict[str, Any] | None
    dmx_fade_ms: int | None
    mixer_scene_id: int | None
    mixer_channel_id: int | None
    mixer_db: float | None
    mixer_muted: bool | None
    projector_power: str | None
    projector_input: str | None
    hdmi_destination: int | None
    hdmi_input_id: int | None
    device_id: int | None
    created_at: str
    updated_at: str


class ActionsResponse(BaseModel):
    actions: list[ActionModel]


class SceneDetail(SceneModel):
    actions: list[ActionModel]


class ActionFields(BaseModel):
    """Every action column; which ones a domain needs is checked on save (§8.12)."""

    model_config = ConfigDict(extra="forbid")

    sort_order: int = ModelField(default=0, ge=0)
    delay_ms: int = ModelField(default=0, ge=0)
    domain: str
    knx_address_id: int | None = None
    knx_value: str | None = None
    knx_source: str = "literal"
    knx_scale: str | None = None
    dmx_snapshot: dict[str, Any] | None = None
    dmx_fade_ms: int | None = ModelField(default=None, ge=0)
    mixer_scene_id: int | None = None
    mixer_channel_id: int | None = None
    mixer_db: float | None = None
    mixer_muted: bool | None = None
    projector_power: str | None = None
    projector_input: str | None = None
    hdmi_destination: int | None = None
    hdmi_input_id: int | None = None
    device_id: int | None = None


class ActionUpdate(BaseModel):
    """Only the fields sent are changed; the result is validated whole."""

    model_config = ConfigDict(extra="forbid")

    sort_order: int | None = ModelField(default=None, ge=0)
    delay_ms: int | None = ModelField(default=None, ge=0)
    domain: str | None = None
    knx_address_id: int | None = None
    knx_value: str | None = None
    knx_source: str | None = None
    knx_scale: str | None = None
    dmx_snapshot: dict[str, Any] | None = None
    dmx_fade_ms: int | None = ModelField(default=None, ge=0)
    mixer_scene_id: int | None = None
    mixer_channel_id: int | None = None
    mixer_db: float | None = None
    mixer_muted: bool | None = None
    projector_power: str | None = None
    projector_input: str | None = None
    hdmi_destination: int | None = None
    hdmi_input_id: int | None = None
    device_id: int | None = None


class ReferenceModel(BaseModel):
    entity: str
    id: int
    name: str


class ReferencesResponse(BaseModel):
    references: list[ReferenceModel]


class TriggerResponse(BaseModel):
    run_id: int
    scene_id: int
    priority: str
    triggered_by: str
    started_at: str


class TestGroupBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delay_ms: int = ModelField(ge=0)


class ActionReportModel(BaseModel):
    action_id: int
    domain: str
    delay_ms: int
    sort_order: int
    result: str
    marker: str
    reason: str | None
    detail: dict[str, Any]
    fired_at_ms: float | None


class RunResultModel(BaseModel):
    scene_id: int
    run_id: int
    log_id: int | None
    priority: str
    triggered_by: str
    result: str
    started_at: str
    completed_at: str
    duration_ms: float
    actions: list[ActionReportModel]


class LogEntryModel(BaseModel):
    id: int
    scene_id: int | None
    triggered_by: str
    started_at: str
    completed_at: str | None
    result: str | None
    action_results: list[dict[str, Any]]


class LogResponse(BaseModel):
    entries: list[LogEntryModel]


class DomainAvailability(BaseModel):
    """One of the eight §8.12 domains, and whether a new action may use it (§21.16)."""

    domain: str
    available: bool
    reason: str | None


class DomainsResponse(BaseModel):
    domains: list[DomainAvailability]


#: Why a domain with no registered handler is unavailable — phrased for the
#: scene editor's domain picker, not for a log line. ``knx`` and ``dmx`` are
#: not listed: the engine always registers them (§8.12), so this reason never
#: applies to either.
_NO_HANDLER_REASON: Final[Mapping[str, str]] = {
    "mixer_recall": "Mixer actions arrive with the mixer driver",
    "mixer_fader": "Mixer actions arrive with the mixer driver",
    "mixer_mute": "Mixer actions arrive with the mixer driver",
    "projector_power": "Projector actions arrive with the projector driver",
    "projector_input": "Projector actions arrive with the projector driver",
    "hdmi_source": "HDMI actions arrive with the HDMI matrix driver",
}

#: Why a domain with a registered handler is still unavailable: its category
#: (§5.5) has no configured device. ``knx`` and ``dmx`` have no category and
#: never reach this table.
_NO_DEVICE_REASON: Final[Mapping[Category, str]] = {
    Category.MIXER: "No mixer is configured",
    Category.PROJECTOR: "No projector is configured",
    Category.VIDEO_MATRIX: "No HDMI matrix is configured",
}


# -- conversion ------------------------------------------------------------------------


def scene_model(
    scene: Scene, *, engine: SceneEngine | None = None, last: scene_log.LogEntry | None = None
) -> SceneModel:
    return SceneModel(
        id=scene.id,
        name=scene.name,
        description=scene.description,
        enabled=scene.enabled,
        icon=scene.icon,
        priority=scene.priority,
        protected=scene.protected,
        visible_operator=scene.visible_operator,
        sort_order=scene.sort_order,
        created_at=scene.created_at,
        updated_at=scene.updated_at,
        running=engine.is_running(scene.id) if engine is not None else False,
        last_run=None
        if last is None
        else LastRunModel(
            log_id=last.id,
            triggered_by=last.triggered_by,
            started_at=last.started_at,
            completed_at=last.completed_at,
            result=last.result,
        ),
    )


def action_model(action: SceneAction) -> ActionModel:
    return ActionModel(
        id=action.id,
        scene_id=action.scene_id,
        sort_order=action.sort_order,
        delay_ms=action.delay_ms,
        domain=action.domain,
        knx_address_id=action.knx_address_id,
        knx_value=action.knx_value,
        knx_source=action.knx_source,
        knx_scale=action.knx_scale,
        dmx_snapshot=action.dmx_snapshot,
        dmx_fade_ms=action.dmx_fade_ms,
        mixer_scene_id=action.mixer_scene_id,
        mixer_channel_id=action.mixer_channel_id,
        mixer_db=action.mixer_db,
        mixer_muted=action.mixer_muted,
        projector_power=action.projector_power,
        projector_input=action.projector_input,
        hdmi_destination=action.hdmi_destination,
        hdmi_input_id=action.hdmi_input_id,
        device_id=action.device_id,
        created_at=action.created_at,
        updated_at=action.updated_at,
    )


def action_values(action: SceneAction) -> dict[str, Any]:
    return {column: getattr(action, column) for column in ACTION_COLUMNS}


def run_result_model(result: SceneRunResult) -> RunResultModel:
    return RunResultModel.model_validate(result.as_dict())


def log_entry_model(entry: scene_log.LogEntry) -> LogEntryModel:
    return LogEntryModel(
        id=entry.id,
        scene_id=entry.scene_id,
        triggered_by=entry.triggered_by,
        started_at=entry.started_at,
        completed_at=entry.completed_at,
        result=entry.result,
        action_results=entry.action_results,
    )


# -- helpers ---------------------------------------------------------------------------


async def _scene_or_404(db: Database, scene_id: int) -> Scene:
    scene = await scenes_crud.get_scene(db, scene_id)
    if scene is None:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no scene with that id")
    return scene


async def _action_or_404(db: Database, scene_id: int, action_id: int) -> SceneAction:
    action = await scenes_crud.get_action(db, action_id)
    if action is None or action.scene_id != scene_id:
        raise ApiError(ErrorCode.NOT_FOUND, "This scene has no action with that id")
    return action


def _version_or_422(version: str | None) -> str:
    if not version:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            f"{VERSION_HEADER} is required so a concurrent edit is not overwritten",
            {"fields": [{"field": VERSION_HEADER, "message": "required"}]},
        )
    return version


def _field_errors(exc: ActionValidationError) -> ApiError:
    fields = [
        {"field": key, "message": message}
        for key, messages in exc.fields.items()
        for message in messages
    ]
    message = (
        "The device this action targets does not support it"
        if exc.unsupported
        else "The scene action is not valid"
    )
    detail: dict[str, Any] = {"fields": fields}
    if exc.unsupported:
        detail["reason"] = "unsupported"
    return ApiError(ErrorCode.VALIDATION_FAILED, message, detail)


def _devices(request: Request) -> CapabilitySource | None:
    devices: CapabilitySource | None = getattr(request.app.state, "devices", None)
    return devices


def _visible_to(claims: TokenClaims, scene: Scene) -> bool:
    return claims.tier == "admin" or scene.visible_operator


def _start_error(exc: Exception) -> ApiError:
    if isinstance(exc, SceneNotFoundError):
        return ApiError(ErrorCode.NOT_FOUND, "There is no scene with that id")
    if isinstance(exc, SceneDisabledError):
        return ApiError(
            ErrorCode.PERMISSION_DENIED,
            "This scene is disabled and cannot be triggered",
            {"reason": "scene_disabled", "scene_id": exc.scene.id},
        )
    if isinstance(exc, NoActionsAtDelayError):
        return ApiError(
            ErrorCode.VALIDATION_FAILED,
            f"This scene has no actions at {exc.delay_ms} ms",
            {"fields": [{"field": "delay_ms", "message": "no actions at this delay"}]},
        )
    if isinstance(exc, SceneEngineStoppedError):
        return ApiError(
            ErrorCode.DEVICE_UNAVAILABLE,
            "The controller is shutting down",
            {"reason": "shutting_down"},
        )
    raise exc


def _instant(value: datetime | None) -> str | None:
    """A query parameter as an ISO instant; a time without an offset is Auckland time (§4.9)."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=AUCKLAND)
    return value.isoformat()


async def _conflict(db: Database, scene_id: int) -> ApiError:
    current = await _scene_or_404(db, scene_id)
    return ApiError(
        ErrorCode.CONFLICT,
        "This scene was changed by someone else since you loaded it",
        {"current": scene_model(current).model_dump()},
    )


# -- the execution log (declared before /scenes/{id} so "log" is not an id) ------------


@router.get("/scenes/log", response_model=LogResponse)
async def scenes_log(
    _: Staff,
    db: Db,
    scene_id: int | None = None,
    since: Annotated[datetime | None, Query(alias="from")] = None,
    until: Annotated[datetime | None, Query(alias="to")] = None,
    result: Result | None = None,
    limit: Annotated[int, Query(ge=1, le=scene_log.MAX_LIMIT)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> LogResponse:
    """Every scene execution, newest first (§8.16), filtered by date range and result."""
    entries = await scene_log.query(
        db,
        scene_id=scene_id,
        since=_instant(since),
        until=_instant(until),
        result=result,
        limit=limit,
        offset=offset,
    )
    return LogResponse(entries=[log_entry_model(e) for e in entries])


@router.get("/scenes/domains", response_model=DomainsResponse)
async def list_domains(_: Admin, db: Db, engine: Engine) -> DomainsResponse:
    """Whether each of the eight §8.12 domains may be picked for a new action (§21.16).

    Available once a handler is registered for the domain and, for a
    device-backed domain (§5.5's ``DOMAIN_CATEGORY``), a device of that
    category is configured. ``knx`` and ``dmx`` need no device — the engine
    always registers a handler for both, so they are available from Phase 2.
    """
    registered = engine.handlers.registered()
    rows: list[DomainAvailability] = []
    for domain in DOMAINS:
        if domain not in registered:
            reason = _NO_HANDLER_REASON.get(domain, f"{domain} actions have no handler registered")
            rows.append(DomainAvailability(domain=domain, available=False, reason=reason))
            continue
        category = DOMAIN_CATEGORY[domain]
        if category is None:
            rows.append(DomainAvailability(domain=domain, available=True, reason=None))
            continue
        devices = await devices_crud.list_all(db, category=category.value)
        if devices:
            rows.append(DomainAvailability(domain=domain, available=True, reason=None))
        else:
            rows.append(
                DomainAvailability(
                    domain=domain, available=False, reason=_NO_DEVICE_REASON[category]
                )
            )
    return DomainsResponse(domains=rows)


# -- scenes ------------------------------------------------------------------------------


@router.get("/scenes", response_model=ScenesResponse)
async def list_scenes(claims: Staff, db: Db, request: Request) -> ScenesResponse:
    """Admins see every scene; operators those visible to them (§21.10)."""
    engine: SceneEngine | None = getattr(request.app.state, "scene_engine", None)
    latest = await scene_log.latest_per_scene(db)
    scenes = [s for s in await scenes_crud.list_scenes(db) if _visible_to(claims, s)]
    return ScenesResponse(
        scenes=[scene_model(s, engine=engine, last=latest.get(s.id)) for s in scenes]
    )


@router.post("/scenes", response_model=SceneModel, status_code=201)
async def create_scene(_: Admin, db: Db, body: Annotated[SceneCreate, Body()]) -> SceneModel:
    scene = await scenes_crud.create_scene(db, **body.model_dump())
    return scene_model(scene)


@router.get("/scenes/{scene_id}", response_model=SceneDetail)
async def get_scene(_: Admin, db: Db, request: Request, scene_id: int) -> SceneDetail:
    scene = await _scene_or_404(db, scene_id)
    engine: SceneEngine | None = getattr(request.app.state, "scene_engine", None)
    latest = await scene_log.query(db, scene_id=scene_id, limit=1)
    base_model = scene_model(scene, engine=engine, last=latest[0] if latest else None)
    actions = await scenes_crud.list_actions(db, scene_id)
    return SceneDetail(**base_model.model_dump(), actions=[action_model(a) for a in actions])


@router.put("/scenes/{scene_id}", response_model=SceneModel)
async def update_scene(
    _: Admin,
    db: Db,
    scene_id: int,
    body: Annotated[SceneUpdate, Body()],
    if_unmodified_since_version: Annotated[str | None, Header()] = None,
) -> SceneModel:
    version = _version_or_422(if_unmodified_since_version)
    await _scene_or_404(db, scene_id)
    values = body.model_dump(exclude_unset=True)
    for key in ("name", "enabled", "priority", "protected", "visible_operator", "sort_order"):
        if key in values and values[key] is None:
            raise ApiError(
                ErrorCode.VALIDATION_FAILED,
                "The scene is not valid",
                {"fields": [{"field": key, "message": "may not be null"}]},
            )
    try:
        if values:
            scene = await scenes_crud.update_scene(db, scene_id, version, **values)
        else:
            scene = await _scene_or_404(db, scene_id)
            if scene.updated_at != version:
                raise ConflictError(scenes_crud.SCENES_TABLE, scene_id, {})
    except ConflictError as exc:
        raise await _conflict(db, scene_id) from exc
    except NotFoundError as exc:  # pragma: no cover - checked above
        raise ApiError(ErrorCode.NOT_FOUND, "There is no scene with that id") from exc
    return scene_model(scene)


@router.delete("/scenes/{scene_id}", status_code=204, response_class=Response)
async def delete_scene(_: Admin, snapshot: PreChangeSnapshot, db: Db, scene_id: int) -> Response:
    await snapshot(f"delete scene {scene_id}")
    scene = await _scene_or_404(db, scene_id)
    if scene.protected:
        raise ApiError(
            ErrorCode.PERMISSION_DENIED,
            f"{scene.name} is protected and cannot be deleted",
            {"reason": "protected", "scene_id": scene_id},
        )
    try:
        await scenes_crud.delete_scene(db, scene_id)
    except InUseError as exc:
        raise ApiError(
            ErrorCode.IN_USE,
            "This scene is run by a rule and cannot be removed",
            {
                "references": [
                    ReferenceModel(entity=r.entity, id=r.id, name=r.name).model_dump()
                    for r in exc.references
                ]
            },
        ) from exc
    return Response(status_code=204)


@router.get("/scenes/{scene_id}/references", response_model=ReferencesResponse)
async def scene_references(_: Admin, db: Db, scene_id: int) -> ReferencesResponse:
    await _scene_or_404(db, scene_id)
    references = await scenes_crud.references_scene(db, scene_id)
    return ReferencesResponse(
        references=[ReferenceModel(entity=r.entity, id=r.id, name=r.name) for r in references]
    )


# -- actions -----------------------------------------------------------------------------


@router.get("/scenes/{scene_id}/actions", response_model=ActionsResponse)
async def list_actions(_: Admin, db: Db, scene_id: int) -> ActionsResponse:
    await _scene_or_404(db, scene_id)
    actions = await scenes_crud.list_actions(db, scene_id)
    return ActionsResponse(actions=[action_model(a) for a in actions])


@router.post("/scenes/{scene_id}/actions", response_model=ActionModel, status_code=201)
async def create_action(
    _: Admin,
    db: Db,
    request: Request,
    engine: Engine,
    scene_id: int,
    body: Annotated[ActionFields, Body()],
) -> ActionModel:
    await _scene_or_404(db, scene_id)
    values = body.model_dump()
    try:
        await validate_action(
            db, scene_id, values, handlers=engine.handlers, devices=_devices(request)
        )
    except ActionValidationError as exc:
        raise _field_errors(exc) from exc
    action = await scenes_crud.create_action(db, scene_id=scene_id, **values)
    _actions_changed(request)
    return action_model(action)


@router.get("/scenes/{scene_id}/actions/{action_id}", response_model=ActionModel)
async def get_action(_: Admin, db: Db, scene_id: int, action_id: int) -> ActionModel:
    return action_model(await _action_or_404(db, scene_id, action_id))


@router.put("/scenes/{scene_id}/actions/{action_id}", response_model=ActionModel)
async def update_action(
    _: Admin,
    db: Db,
    request: Request,
    engine: Engine,
    scene_id: int,
    action_id: int,
    body: Annotated[ActionUpdate, Body()],
    if_unmodified_since_version: Annotated[str | None, Header()] = None,
) -> ActionModel:
    version = _version_or_422(if_unmodified_since_version)
    current = await _action_or_404(db, scene_id, action_id)
    changes = body.model_dump(exclude_unset=True)
    for key in ("sort_order", "delay_ms", "domain", "knx_source"):
        if key in changes and changes[key] is None:
            raise ApiError(
                ErrorCode.VALIDATION_FAILED,
                "The scene action is not valid",
                {"fields": [{"field": key, "message": "may not be null"}]},
            )
    merged = {**action_values(current), **changes}
    try:
        await validate_action(
            db, scene_id, merged, handlers=engine.handlers, devices=_devices(request)
        )
    except ActionValidationError as exc:
        raise _field_errors(exc) from exc
    try:
        if changes:
            action = await scenes_crud.update_action(db, action_id, version, **changes)
        elif current.updated_at != version:
            raise ConflictError(scenes_crud.ACTIONS_TABLE, action_id, {})
        else:
            action = current
    except ConflictError as exc:
        latest = await _action_or_404(db, scene_id, action_id)
        raise ApiError(
            ErrorCode.CONFLICT,
            "This action was changed by someone else since you loaded it",
            {"current": action_model(latest).model_dump()},
        ) from exc
    if changes:
        _actions_changed(request)
    return action_model(action)


@router.delete("/scenes/{scene_id}/actions/{action_id}", status_code=204, response_class=Response)
async def delete_action(
    _: Admin, snapshot: PreChangeSnapshot, db: Db, request: Request, scene_id: int, action_id: int
) -> Response:
    await snapshot(f"delete action {action_id} of scene {scene_id}")
    await _action_or_404(db, scene_id, action_id)
    await scenes_crud.delete_action(db, action_id)
    _actions_changed(request)
    return Response(status_code=204)


def _actions_changed(request: Request) -> None:
    """A scene's actions changed and committed: what is derived from what a
    scene does (the desk scenes a hirer's buttons can recall) is recomputed."""
    request.app.state.bus.emit(SceneConfigChanged(reason="scene_actions"))


# -- control -----------------------------------------------------------------------------


def _trigger_response(handle: SceneRunHandle) -> TriggerResponse:
    return TriggerResponse(
        run_id=handle.run_id,
        scene_id=handle.scene_id,
        priority=handle.priority,
        triggered_by=handle.triggered_by,
        started_at=handle.started_at,
    )


@router.post("/scenes/{scene_id}/trigger", response_model=TriggerResponse, status_code=202)
async def trigger_scene(claims: Staff, db: Db, engine: Engine, scene_id: int) -> TriggerResponse:
    """Start a scene and return at once; its result follows as ``scene_completed`` (§16.8)."""
    scene = await _scene_or_404(db, scene_id)
    if not _visible_to(claims, scene):
        raise ApiError(
            ErrorCode.PERMISSION_DENIED,
            "This scene is not available to your account",
            {"reason": "not_visible", "scene_id": scene_id},
        )
    try:
        handle = await engine.run(scene_id, triggered_by=f"api:{claims.tier}")
    except (SceneNotFoundError, SceneDisabledError, SceneEngineStoppedError) as exc:
        raise _start_error(exc) from exc
    return _trigger_response(handle)


@router.post("/scenes/{scene_id}/test", response_model=RunResultModel)
async def test_scene(claims: Admin, engine: Engine, scene_id: int) -> RunResultModel:
    """Run as if triggered, respecting delays, and report every action inline (§21.16)."""
    try:
        handle = await engine.test(scene_id, triggered_by=f"api:{claims.tier}")
    except (SceneNotFoundError, SceneEngineStoppedError) as exc:
        raise _start_error(exc) from exc
    return run_result_model(await handle.result())


@router.post("/scenes/{scene_id}/test-group", response_model=RunResultModel)
async def test_scene_group(
    claims: Admin, engine: Engine, scene_id: int, body: Annotated[TestGroupBody, Body()]
) -> RunResultModel:
    """Fire only the actions at ``delay_ms``, now, ignoring the delay itself (§21.16)."""
    try:
        handle = await engine.test_group(scene_id, body.delay_ms, triggered_by=f"api:{claims.tier}")
    except (SceneNotFoundError, NoActionsAtDelayError, SceneEngineStoppedError) as exc:
        raise _start_error(exc) from exc
    return run_result_model(await handle.result())


@router.get("/scenes/{scene_id}/log", response_model=LogResponse)
async def scene_log_entries(
    _: Staff,
    db: Db,
    scene_id: int,
    since: Annotated[datetime | None, Query(alias="from")] = None,
    until: Annotated[datetime | None, Query(alias="to")] = None,
    result: Result | None = None,
    limit: Annotated[int, Query(ge=1, le=scene_log.MAX_LIMIT)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> LogResponse:
    """One scene's executions, newest first (§8.16)."""
    await _scene_or_404(db, scene_id)
    entries = await scene_log.query(
        db,
        scene_id=scene_id,
        since=_instant(since),
        until=_instant(until),
        result=result,
        limit=limit,
        offset=offset,
    )
    return LogResponse(entries=[log_entry_model(e) for e in entries])


__all__ = ["VERSION_HEADER", "get_scene_engine", "router"]
