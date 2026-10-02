"""Rules and derived-status endpoints (spec §16.5 *Rules*, §8, §21.17).

Configuration follows the standard REST shape with §16.1's optimistic
concurrency: every ``PUT`` carries ``If-Unmodified-Since-Version`` and a stale
one is refused with ``409 conflict`` and the current record. Validation is
done here, before the database is touched, and answers ``422
validation_failed`` with ``detail.fields`` mapping each field to its messages
— a match type the address's DPT does not allow, a binding that is not
``any`` on a 1-bit address, a malformed cron, an address already bound to a
derived status. The database's own ``CHECK`` and ``UNIQUE`` constraints stay
as the last line, and their :class:`~proskenion.db.crud.refs.ConstraintError`
maps to ``validation_failed`` too, never ``internal_error``.

Two validations go beyond the column constraints, both to keep §8.7's flat
prohibition on chaining visible rather than silent:

* a ``knx`` rule may not trigger on an address a derived status writes, and a
  derived status may not write an address a rule triggers on — the rule layer
  ignores telegrams on status addresses, so such a rule could never fire;
* a derived status writes DPT 1.x to an address the controller may write
  (§8.9: "outgoing, DPT 1.001").

``GET /rules`` is open to operators as well as admins: the Lighting view reads
the stage banks from ``GET /rules?action_type=lighting_group``. Everything else
that changes configuration is admin-only.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator, Mapping
from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from pydantic import Field as ModelField

from proskenion.api.deps import get_db, require_admin, require_staff
from proskenion.api.errors import ApiError, ErrorCode
from proskenion.api.snapshots import PreChangeSnapshot
from proskenion.core.auth import TokenClaims
from proskenion.core.events import RulesConfigChanged
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import knx as knx_crud
from proskenion.db.crud import lighting as lighting_crud
from proskenion.db.crud import rules as rules_crud
from proskenion.db.crud import scenes as scenes_crud
from proskenion.db.crud import video as video_crud
from proskenion.db.crud.base import ConflictError, InUseError, NotFoundError
from proskenion.db.crud.knx import KnxGroupAddress
from proskenion.db.crud.refs import ConstraintError
from proskenion.db.crud.rules import DerivedStatus, Rule, RuleExecution
from proskenion.rules.engine import RulesEngine, UnknownRuleError
from proskenion.rules.model import (
    SCHEDULE_NEVER_OCCURS,
    SURFACE_NOT_FIRING,
    ActionType,
    Basis,
    DptClass,
    GuardType,
    MatchType,
    SourceType,
    TriggerType,
    allowed_match_types,
    dpt_class,
    normalise_individual_address,
    parse_device_state_guard,
    parse_external_control_guard,
    parse_match_value,
    parse_time_window,
    valid_state_name,
    validate_cron,
)
from proskenion.rules.scheduler import next_fire_at

router = APIRouter(tags=["rules"])

#: The §16.1 optimistic-concurrency header every configuration ``PUT`` carries.
VERSION_HEADER = "If-Unmodified-Since-Version"
#: A server-sent events comment this often keeps an idle monitor connection open.
SSE_KEEPALIVE_S = 15.0

Admin = Annotated[TokenClaims, Depends(require_admin)]
Staff = Annotated[TokenClaims, Depends(require_staff)]
Db = Annotated[Database, Depends(get_db)]

Scalar = str | bool | int | float


# -- models -----------------------------------------------------------------------


class RuleModel(BaseModel):
    """A ``rules`` row, plus whether it fires on its own and, if not, why not.

    ``next_fire_at`` is when an enabled schedule rule next fires, ISO 8601 in
    Pacific/Auckland, or ``null`` for anything else and for a schedule that
    never occurs.
    """

    id: int
    name: str
    enabled: bool
    sort_order: int
    notes: str | None
    trigger_type: str
    knx_address_id: int | None
    match_type: str
    match_value: str | None
    match_value_max: str | None
    debounce_ms: int | None
    cron: str | None
    trigger_device_id: int | None
    trigger_state: str | None
    trigger_for_ms: int | None
    #: "Only from device" (migration 013): a knx trigger fires only for
    #: telegrams from this individual address, e.g. ``1.1.26``. ``null`` = any.
    trigger_source_address: str | None = None
    guard_type: str | None
    guard_value: str | None
    action_type: str
    scene_id: int | None
    lighting_group_id: int | None
    on_level: float | None
    off_level: float | None
    fade_ms: int | None
    message: str | None
    created_at: str
    updated_at: str
    fires_automatically: bool
    note: str | None
    next_fire_at: str | None = None


class RulesResponse(BaseModel):
    rules: list[RuleModel]


class RuleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = ModelField(min_length=1, max_length=120)
    enabled: bool = True
    sort_order: int = 0
    notes: str | None = ModelField(default=None, max_length=2000)
    trigger_type: TriggerType
    knx_address_id: int | None = None
    match_type: MatchType = "equal"
    match_value: Scalar | None = None
    match_value_max: Scalar | None = None
    debounce_ms: int | None = ModelField(default=None, ge=0, le=60_000)
    cron: str | None = ModelField(default=None, max_length=120)
    trigger_device_id: int | None = None
    trigger_state: str | None = ModelField(default=None, max_length=32)
    trigger_for_ms: int | None = ModelField(default=None, ge=0, le=86_400_000)
    trigger_source_address: str | None = ModelField(default=None, max_length=16)
    guard_type: GuardType | None = None
    guard_value: str | None = ModelField(default=None, max_length=64)
    action_type: ActionType
    scene_id: int | None = None
    lighting_group_id: int | None = None
    on_level: float | None = ModelField(default=None, ge=0, le=100)
    off_level: float | None = ModelField(default=None, ge=0, le=100)
    fade_ms: int | None = ModelField(default=None, ge=0, le=3_600_000)
    message: str | None = ModelField(default=None, max_length=2000)


class RuleUpdate(BaseModel):
    """Any subset of a rule's fields; the merged rule is validated as a whole.

    A field sent as ``null`` is cleared; a field left out is unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = ModelField(default=None, min_length=1, max_length=120)
    enabled: bool | None = None
    sort_order: int | None = None
    notes: str | None = ModelField(default=None, max_length=2000)
    trigger_type: TriggerType | None = None
    knx_address_id: int | None = None
    match_type: MatchType | None = None
    match_value: Scalar | None = None
    match_value_max: Scalar | None = None
    debounce_ms: int | None = ModelField(default=None, ge=0, le=60_000)
    cron: str | None = ModelField(default=None, max_length=120)
    trigger_device_id: int | None = None
    trigger_state: str | None = ModelField(default=None, max_length=32)
    trigger_for_ms: int | None = ModelField(default=None, ge=0, le=86_400_000)
    trigger_source_address: str | None = ModelField(default=None, max_length=16)
    guard_type: GuardType | None = None
    guard_value: str | None = ModelField(default=None, max_length=64)
    action_type: ActionType | None = None
    scene_id: int | None = None
    lighting_group_id: int | None = None
    on_level: float | None = ModelField(default=None, ge=0, le=100)
    off_level: float | None = ModelField(default=None, ge=0, le=100)
    fade_ms: int | None = ModelField(default=None, ge=0, le=3_600_000)
    message: str | None = ModelField(default=None, max_length=2000)


#: Columns that may never be cleared by a ``PUT``.
_NOT_NULL = frozenset({"name", "enabled", "sort_order", "trigger_type", "match_type"})


class FireBody(BaseModel):
    """``{ value }`` — the telegram value to fire with. Omitted: the action runs
    directly, and a binding toggles its bank."""

    model_config = ConfigDict(extra="forbid")

    value: Scalar | None = None


class FireResponse(BaseModel):
    rule_id: int
    triggered_by: str
    fired: bool
    guard_result: str | None
    result: str
    detail: dict[str, Any]


class RuleStatesResponse(BaseModel):
    external_control: bool
    rules: list[dict[str, Any]]


class LogEntryModel(BaseModel):
    id: int
    rule_id: int | None
    triggered_by: str
    fired_at: str
    guard_result: str | None
    result: str | None
    detail: Any


class LogResponse(BaseModel):
    entries: list[LogEntryModel]


class DerivedStatusModel(BaseModel):
    id: int
    name: str
    enabled: bool
    # Nullable since migration 006 (Q6, Phase 5 plan): a lamp with no
    # wall-panel indicator is evaluated and broadcast like any other status,
    # just never written to the KNX bus.
    knx_address_id: int | None
    group_address: str | None
    source_type: str
    lighting_group_id: int | None
    compare_level: float | None
    device_id: int | None
    compare_state: str | None
    #: What ``lighting_group_all_at`` compares: the stored level or the
    #: composited output (migration 011). ``level`` for every other source type.
    basis: str
    created_at: str
    updated_at: str
    #: ``video_destination_input`` (migration 013): the HDMI destination and
    #: the input it must be showing. ``null`` for every other source type.
    video_destination_id: int | None = None
    compare_input_id: int | None = None


class DerivedStatusesResponse(BaseModel):
    derived_statuses: list[DerivedStatusModel]


class DerivedStatusCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = ModelField(min_length=1, max_length=120)
    enabled: bool = True
    # Nullable since migration 006 (Q6, Phase 5 plan): a lamp-only status —
    # "House at 100%", say, with no wall-panel indicator — is evaluated and
    # broadcast on the status frame like any other, just never written to
    # the KNX bus (§8.9).
    knx_address_id: int | None = None
    source_type: SourceType
    lighting_group_id: int | None = None
    compare_level: float | None = ModelField(default=None, ge=0, le=100)
    device_id: int | None = None
    compare_state: str | None = ModelField(default=None, max_length=32)
    basis: Basis = "level"
    video_destination_id: int | None = None
    compare_input_id: int | None = None


class DerivedStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = ModelField(default=None, min_length=1, max_length=120)
    enabled: bool | None = None
    knx_address_id: int | None = None
    source_type: SourceType | None = None
    lighting_group_id: int | None = None
    compare_level: float | None = ModelField(default=None, ge=0, le=100)
    device_id: int | None = None
    compare_state: str | None = ModelField(default=None, max_length=32)
    basis: Basis | None = None
    video_destination_id: int | None = None
    compare_input_id: int | None = None


_STATUS_NOT_NULL = frozenset({"name", "enabled", "source_type", "basis"})


class DerivedStatesResponse(BaseModel):
    statuses: list[dict[str, Any]]


# -- helpers ------------------------------------------------------------------------


def _engine(request: Request) -> RulesEngine:
    engine: RulesEngine | None = getattr(request.app.state, "rules", None)
    if engine is None:
        raise ApiError(
            ErrorCode.DEVICE_UNAVAILABLE,
            "The rules engine is not running",
            {"reason": "not_started"},
        )
    return engine


def _running_engine(request: Request) -> RulesEngine | None:
    engine: RulesEngine | None = getattr(request.app.state, "rules", None)
    return engine if engine is not None and engine.running else None


async def _reload(request: Request) -> None:
    """Configuration changed: the running engine re-reads it, and anything
    derived from rules (what a hirer's page buttons reach) is told."""
    engine: RulesEngine | None = getattr(request.app.state, "rules", None)
    if engine is not None:
        await engine.reload()
    request.app.state.bus.emit(RulesConfigChanged(reason="rules"))


def _version_or_422(version: str | None) -> str:
    if not version:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            f"{VERSION_HEADER} is required so a concurrent edit is not overwritten",
            {"fields": {VERSION_HEADER: ["required"]}},
        )
    return version


def _invalid(fields: Mapping[str, list[str]], message: str | None = None) -> ApiError:
    return ApiError(
        ErrorCode.VALIDATION_FAILED,
        message or "The rule is not valid",
        {"fields": {k: list(v) for k, v in fields.items()}},
    )


def _constraint(exc: ConstraintError, field: str) -> ApiError:
    """A violated ``CHECK`` or ``UNIQUE`` is the caller's error, never ``internal_error``."""
    return ApiError(
        ErrorCode.VALIDATION_FAILED,
        "The request breaks a rule of the configuration",
        {"fields": {field: [str(exc.raw_message)]}, "constraint": exc.constraint},
    )


def _as_text(value: Scalar | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def _tidy_source_address(values: dict[str, Any]) -> None:
    """A blank "only from device" is none; a valid one is stored in the form a
    telegram's ``source_address`` takes (``01.1.026`` → ``1.1.26``). An
    invalid one is left for :func:`_validate_rule` to report."""
    if "trigger_source_address" not in values:
        return
    raw = values["trigger_source_address"]
    if raw is None or not raw.strip():
        values["trigger_source_address"] = None
        return
    try:
        values["trigger_source_address"] = normalise_individual_address(raw)
    except ValueError:
        pass


def rule_model(rule: Rule, engine: RulesEngine | None = None) -> RuleModel:
    """The read model. With the running engine, ``next_fire_at`` is the
    scheduler's own plan; without it, the cron's next time from now."""
    note: str | None = None
    upcoming: datetime | None = None
    if rule.trigger_type == "schedule":
        upcoming = next_fire_at(rule.cron) if engine is None else engine.next_fire_at(rule)
        if next_fire_at(rule.cron) is None:
            note = SCHEDULE_NEVER_OCCURS
    elif rule.trigger_type == "surface":
        note = SURFACE_NOT_FIRING
    return RuleModel(
        id=rule.id,
        name=rule.name,
        enabled=rule.enabled,
        sort_order=rule.sort_order,
        notes=rule.notes,
        trigger_type=rule.trigger_type,
        knx_address_id=rule.knx_address_id,
        match_type=rule.match_type,
        match_value=rule.match_value,
        match_value_max=rule.match_value_max,
        debounce_ms=rule.debounce_ms,
        cron=rule.cron,
        trigger_device_id=rule.trigger_device_id,
        trigger_state=rule.trigger_state,
        trigger_for_ms=rule.trigger_for_ms,
        trigger_source_address=rule.trigger_source_address,
        guard_type=rule.guard_type,
        guard_value=rule.guard_value,
        action_type=rule.action_type,
        scene_id=rule.scene_id,
        lighting_group_id=rule.lighting_group_id,
        on_level=rule.on_level,
        off_level=rule.off_level,
        fade_ms=rule.fade_ms,
        message=rule.message,
        created_at=rule.created_at,
        updated_at=rule.updated_at,
        fires_automatically=(
            rule.enabled and rule.trigger_type in ("knx", "device_state", "schedule")
        ),
        note=note,
        next_fire_at=(
            upcoming.isoformat(timespec="seconds") if upcoming and rule.enabled else None
        ),
    )


def _rule_values(rule: Rule) -> dict[str, Any]:
    """The editable columns of a stored rule."""
    return {
        name: getattr(rule, name)
        for name in RuleCreate.model_fields
        if hasattr(rule, name)  # every field of RuleCreate is a column
    }


async def _rule_or_404(db: Database, rule_id: int) -> Rule:
    rule = await rules_crud.get_rule(db, rule_id)
    if rule is None:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no rule with that id")
    return rule


# -- rule validation (§8.2–§8.5, §8.9) -------------------------------------------------


class _Errors(dict[str, list[str]]):
    def add(self, field: str, message: str) -> None:
        self.setdefault(field, []).append(message)


async def _validate_rule(db: Database, values: Mapping[str, Any]) -> None:
    """Raise 422 with every problem found, field by field."""
    errors = _Errors()
    trigger = values["trigger_type"]
    action = values["action_type"]
    match_type = values["match_type"]
    address: KnxGroupAddress | None = None

    def must_be_empty(field: str, why: str) -> None:
        if values.get(field) is not None:
            errors.add(field, why)

    # -- the trigger (§8.3, §8.4) --
    if trigger == "knx":
        address_id = values.get("knx_address_id")
        if address_id is None:
            errors.add("knx_address_id", "a knx trigger needs a group address")
        else:
            address = await knx_crud.get_address(db, address_id)
            if address is None:
                errors.add("knx_address_id", "there is no group address with that id")
        if address is not None:
            cls = dpt_class(address.dpt)
            if cls is None:
                errors.add(
                    "knx_address_id",
                    f"DPT {address.dpt} is not supported (§7.1); its telegrams never "
                    "reach the rule layer",
                )
            else:
                if address.direction == "outgoing":
                    errors.add(
                        "knx_address_id", "a trigger needs an incoming address, not outgoing-only"
                    )
                if match_type not in allowed_match_types(cls):
                    allowed = ", ".join(sorted(allowed_match_types(cls)))
                    errors.add(
                        "match_type",
                        f"{match_type} is not available for DPT {address.dpt}; "
                        f"this address allows {allowed} (§8.4)",
                    )
                elif match_type != "any":
                    _check_match_values(values, cls, match_type, errors)
            bound = [
                s
                for s in await rules_crud.list_derived_status(db)
                if s.knx_address_id == address.id
            ]
            if bound:
                errors.add(
                    "knx_address_id",
                    f"{address.group_address} is written by derived status "
                    f"“{bound[0].name}”; the rule layer never fires on the "
                    "controller's own writes (§8.7)",
                )
        source = values.get("trigger_source_address")
        if source is not None:
            try:
                normalise_individual_address(source)
            except ValueError as exc:
                errors.add("trigger_source_address", str(exc))
    else:
        must_be_empty("knx_address_id", "only a knx trigger has a group address")
        must_be_empty(
            "trigger_source_address", "only a knx trigger can be limited to one device"
        )
        must_be_empty("match_value", "only a knx trigger matches a value")
        must_be_empty("match_value_max", "only a knx trigger matches a value")
        must_be_empty("debounce_ms", "debounce applies to knx triggers only (§8.4)")
    if trigger == "schedule":
        cron = values.get("cron")
        if not cron:
            errors.add("cron", "a schedule trigger needs a cron expression")
        else:
            problem = validate_cron(cron)
            if problem is not None:
                errors.add("cron", f"not a valid five-field cron expression: {problem}")
    else:
        must_be_empty("cron", "only a schedule trigger has a cron expression")
    if trigger == "device_state":
        device_id = values.get("trigger_device_id")
        if device_id is None:
            errors.add("trigger_device_id", "a device_state trigger needs a device")
        elif await devices_crud.get(db, device_id) is None:
            errors.add("trigger_device_id", "there is no device with that id")
        state = values.get("trigger_state")
        if not state:
            errors.add("trigger_state", "a device_state trigger needs a state, e.g. offline")
        elif not valid_state_name(state):
            errors.add("trigger_state", "the state is a lower-case name, e.g. online or offline")
    else:
        must_be_empty("trigger_device_id", "only a device_state trigger names a device")
        must_be_empty("trigger_state", "only a device_state trigger names a state")
        must_be_empty("trigger_for_ms", "only a device_state trigger has a sustained duration")

    # -- the guard (§8.5): one, from a fixed list --
    guard = values.get("guard_type")
    guard_value = values.get("guard_value")
    if guard is None:
        must_be_empty("guard_value", "a guard value needs a guard type")
    elif not guard_value:
        errors.add("guard_value", f"a {guard} guard needs a value")
    else:
        try:
            if guard == "time_window":
                parse_time_window(guard_value)
            elif guard == "external_control":
                parse_external_control_guard(guard_value)
            else:
                device_id, _ = parse_device_state_guard(guard_value)
                if await devices_crud.get(db, device_id) is None:
                    errors.add("guard_value", "there is no device with that id")
        except ValueError as exc:
            errors.add("guard_value", str(exc))

    # -- the action (§8.9) --
    if action == "run_scene":
        scene_id = values.get("scene_id")
        if scene_id is None:
            errors.add("scene_id", "a run_scene rule needs a scene")
        elif await scenes_crud.get_scene(db, scene_id) is None:
            errors.add("scene_id", "there is no scene with that id")
    else:
        must_be_empty("scene_id", "only a run_scene rule names a scene")
    if action == "lighting_group":
        group_id = values.get("lighting_group_id")
        if group_id is None:
            errors.add("lighting_group_id", "a binding needs a lighting group")
        elif (group := await lighting_crud.get_group(db, group_id)) is None:
            errors.add("lighting_group_id", "there is no lighting group with that id")
        elif group.indicator_only:
            errors.add(
                "lighting_group_id",
                f"“{group.name}” is indicator-only: it has no fader, and a binding forces "
                "its group's level to full (§8.8), which would do nothing. Bind the groups "
                "that do have faders instead",
            )
        for level in ("on_level", "off_level"):
            if values.get(level) is None:
                errors.add(level, "a binding needs both an on level and an off level")
        if trigger != "knx":
            errors.add("trigger_type", "a binding is triggered by a knx address (§8.2)")
        elif match_type != "any":
            errors.add(
                "match_type",
                "a binding needs match type any: the telegram's value selects the on "
                "or off level (§8.2)",
            )
        if address is not None and dpt_class(address.dpt) not in (None, "boolean"):
            errors.add(
                "knx_address_id",
                f"a binding needs a 1-bit address; DPT {address.dpt} is not (§8.2)",
            )
    else:
        must_be_empty("lighting_group_id", "only a lighting_group rule names a group")
        must_be_empty("on_level", "only a lighting_group rule has levels")
        must_be_empty("off_level", "only a lighting_group rule has levels")
        must_be_empty("fade_ms", "only a lighting_group rule has a fade")
    if action == "notify":
        if not values.get("message"):
            errors.add("message", "a notify rule needs a message")
    else:
        must_be_empty("message", "only a notify rule has a message")

    if errors:
        raise _invalid(errors)


def _check_match_values(
    values: Mapping[str, Any], cls: DptClass, match_type: str, errors: _Errors
) -> None:
    raw = values.get("match_value")
    low: bool | float | None = None
    if raw is None:
        errors.add("match_value", f"match type {match_type} needs a value")
    else:
        try:
            low = parse_match_value(str(raw), cls)
        except ValueError as exc:
            errors.add("match_value", str(exc))
    high_raw = values.get("match_value_max")
    if match_type == "range":
        if high_raw is None:
            errors.add("match_value_max", "a range needs an upper value")
            return
        try:
            high = parse_match_value(str(high_raw), cls)
        except ValueError as exc:
            errors.add("match_value_max", str(exc))
            return
        if isinstance(low, float) and isinstance(high, float) and high < low:
            errors.add("match_value_max", "the upper value is below the lower one")
    elif high_raw is not None:
        errors.add("match_value_max", "only a range has an upper value")


# -- rules: collection and live state -------------------------------------------------


@router.get("/rules", response_model=RulesResponse)
async def list_rules(
    _: Staff,
    db: Db,
    request: Request,
    action_type: Annotated[ActionType | None, Query()] = None,
    trigger_type: Annotated[TriggerType | None, Query()] = None,
) -> RulesResponse:
    """Every rule, in the order they run (§8.7). The Lighting view reads the stage
    banks with ``?action_type=lighting_group``."""
    rules = await rules_crud.list_rules(db)
    engine = _running_engine(request)
    return RulesResponse(
        rules=[
            rule_model(r, engine)
            for r in rules
            if (action_type is None or r.action_type == action_type)
            and (trigger_type is None or r.trigger_type == trigger_type)
        ]
    )


@router.post("/rules", response_model=RuleModel, status_code=201)
async def create_rule(
    _: Admin, db: Db, request: Request, body: Annotated[RuleCreate, Body()]
) -> RuleModel:
    values = body.model_dump()
    values["match_value"] = _as_text(body.match_value)
    values["match_value_max"] = _as_text(body.match_value_max)
    _tidy_source_address(values)
    await _validate_rule(db, values)
    try:
        rule = await rules_crud.create_rule(db, **values)
    except ConstraintError as exc:
        raise _constraint(exc, "action_type") from exc
    except sqlite3.IntegrityError as exc:
        raise _invalid({"rule": [str(exc)]}) from exc
    await _reload(request)
    return rule_model(rule, _running_engine(request))


@router.get("/rules/state", response_model=RuleStatesResponse)
async def rule_states(_: Staff, request: Request) -> RuleStatesResponse:
    """Live on/off per rule, suppression under external control, and why a rule
    will not fire on its own (§16.5, §21.17)."""
    engine = _engine(request)
    return RuleStatesResponse(
        external_control=engine.derived.external_active, rules=engine.rule_states()
    )


@router.get("/rules/log", response_model=LogResponse)
async def rule_log(
    _: Staff,
    db: Db,
    rule_id: int | None = None,
    result: Annotated[str | None, Query(max_length=32)] = None,
    since: str | None = None,
    until: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> LogResponse:
    """Event rules only — trigger, guard result, action, outcome (§8.10). Newest first."""
    bad = _Errors()
    for name, value in (("since", since), ("until", until)):
        if value is not None:
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                bad.add(name, "an ISO 8601 date and time with offset, e.g. 2026-09-11T08:00+12:00")
                continue
            if parsed.tzinfo is None:
                bad.add(name, "include the offset, e.g. +12:00 (§4.9)")
    if bad:
        raise _invalid(bad, "The log filter is not valid")
    entries = await rules_crud.list_executions(
        db, rule_id=rule_id, result=result, since=since, until=until, limit=limit
    )
    return LogResponse(entries=[_log_entry(e) for e in entries])


def _log_entry(entry: RuleExecution) -> LogEntryModel:
    detail: Any = entry.detail
    if entry.detail is not None:
        try:
            detail = json.loads(entry.detail)
        except ValueError:
            detail = entry.detail
    return LogEntryModel(
        id=entry.id,
        rule_id=entry.rule_id,
        triggered_by=entry.triggered_by,
        fired_at=entry.fired_at,
        guard_result=entry.guard_result,
        result=entry.result,
        detail=detail,
    )


# -- rules: one rule -------------------------------------------------------------------


@router.get("/rules/{rule_id}", response_model=RuleModel)
async def get_rule(_: Staff, db: Db, request: Request, rule_id: int) -> RuleModel:
    return rule_model(await _rule_or_404(db, rule_id), _running_engine(request))


@router.put("/rules/{rule_id}", response_model=RuleModel)
async def update_rule(
    _: Admin,
    db: Db,
    request: Request,
    rule_id: int,
    body: Annotated[RuleUpdate, Body()],
    if_unmodified_since_version: Annotated[str | None, Header()] = None,
) -> RuleModel:
    version = _version_or_422(if_unmodified_since_version)
    current = await _rule_or_404(db, rule_id)
    changes = {name: getattr(body, name) for name in body.model_fields_set}
    cleared = sorted(name for name, value in changes.items() if value is None and name in _NOT_NULL)
    if cleared:
        raise _invalid({name: ["may not be cleared"] for name in cleared})
    for name in ("match_value", "match_value_max"):
        if name in changes:
            changes[name] = _as_text(changes[name])
    _tidy_source_address(changes)
    merged = {**_rule_values(current), **changes}
    await _validate_rule(db, merged)
    try:
        rule = await rules_crud.update_rule(db, rule_id, version, **changes)
    except ConflictError as exc:
        latest = await _rule_or_404(db, rule_id)
        raise ApiError(
            ErrorCode.CONFLICT,
            "This rule was changed by someone else since you loaded it",
            {"current": rule_model(latest).model_dump()},
        ) from exc
    except NotFoundError as exc:  # pragma: no cover - checked above
        raise ApiError(ErrorCode.NOT_FOUND, "There is no rule with that id") from exc
    except ConstraintError as exc:
        raise _constraint(exc, "action_type") from exc
    except sqlite3.IntegrityError as exc:
        raise _invalid({"rule": [str(exc)]}) from exc
    await _reload(request)
    return rule_model(rule, _running_engine(request))


@router.delete("/rules/{rule_id}", status_code=204, response_class=Response)
async def delete_rule(
    _: Admin, snapshot: PreChangeSnapshot, db: Db, request: Request, rule_id: int
) -> Response:
    await snapshot(f"delete rule {rule_id}")
    await _rule_or_404(db, rule_id)
    try:
        await rules_crud.delete_rule(db, rule_id)
    except InUseError as exc:
        raise ApiError(
            ErrorCode.IN_USE,
            "This rule is assigned to a button and cannot be removed",
            # §16.1's shape. Nothing references a rule with RESTRICT yet; page
            # buttons will (Phase 5), and their InUseError carries the rows.
            {"references": [asdict(r) for r in getattr(exc, "references", ())]},
        ) from exc
    await _reload(request)
    return Response(status_code=204)


@router.post("/rules/{rule_id}/fire", response_model=FireResponse)
async def fire_rule(
    claims: Staff,
    request: Request,
    rule_id: int,
    body: Annotated[FireBody | None, Body()] = None,
) -> FireResponse:
    """Fire a rule as if triggered (§16.5): its match, guard and suppression apply.

    A scene is started rather than awaited — ``result`` is ``started`` and the
    outcome reaches the log. A disabled rule does not fire.
    """
    engine = _engine(request)
    value = None if body is None else body.value
    try:
        report = await engine.fire(rule_id, value, triggered_by=f"api:{claims.tier}")
    except UnknownRuleError as exc:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no rule with that id") from exc
    return FireResponse(**report.as_dict())


@router.post("/rules/{rule_id}/test", response_model=FireResponse)
async def test_rule(
    claims: Admin,
    request: Request,
    rule_id: int,
    body: Annotated[FireBody | None, Body()] = None,
) -> FireResponse:
    """Fire and report inline (§16.5, §21.17) — a scene is awaited and its outcome
    returned. Runs a disabled rule too: this is how a rule is checked at
    commissioning before it is switched on."""
    engine = _engine(request)
    value = None if body is None else body.value
    try:
        report = await engine.fire(
            rule_id,
            value,
            triggered_by=f"api:{claims.tier}",
            inline=True,
            ignore_enabled=True,
        )
    except UnknownRuleError as exc:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no rule with that id") from exc
    return FireResponse(**report.as_dict())


# -- derived status ----------------------------------------------------------------------


async def _status_model(db: Database, status: DerivedStatus) -> DerivedStatusModel:
    # knx_address_id is nullable since migration 006 (Q6 of the Phase 5
    # plan): a lamp with no wall-panel indicator has no address to look up.
    address = (
        None
        if status.knx_address_id is None
        else await knx_crud.get_address(db, status.knx_address_id)
    )
    return DerivedStatusModel(
        id=status.id,
        name=status.name,
        enabled=status.enabled,
        knx_address_id=status.knx_address_id,
        group_address=None if address is None else address.group_address,
        source_type=status.source_type,
        lighting_group_id=status.lighting_group_id,
        compare_level=status.compare_level,
        device_id=status.device_id,
        compare_state=status.compare_state,
        basis=status.basis,
        created_at=status.created_at,
        updated_at=status.updated_at,
        video_destination_id=status.video_destination_id,
        compare_input_id=status.compare_input_id,
    )


async def _status_or_404(db: Database, status_id: int) -> DerivedStatus:
    status = await rules_crud.get_derived_status(db, status_id)
    if status is None:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no derived status with that id")
    return status


async def _validate_status(
    db: Database, values: Mapping[str, Any], *, status_id: int | None = None
) -> None:
    errors = _Errors()
    address_id = values.get("knx_address_id")
    address: KnxGroupAddress | None = None
    # Q6 (migration 006): a null address is valid — a lamp-only status, with
    # no wall-panel indicator, needs none of the checks below.
    if address_id is not None:
        address = await knx_crud.get_address(db, address_id)
        if address is None:
            errors.add("knx_address_id", "there is no group address with that id")
    if address is not None:
        if dpt_class(address.dpt) != "boolean":
            errors.add(
                "knx_address_id",
                f"a derived status writes DPT 1.001; {address.group_address} is DPT "
                f"{address.dpt} (§8.9)",
            )
        if address.direction == "incoming":
            errors.add(
                "knx_address_id",
                f"{address.group_address} is incoming-only; the controller never writes it (§7.1)",
            )
        others = [
            s
            for s in await rules_crud.list_derived_status(db)
            if s.knx_address_id == address.id and s.id != status_id
        ]
        if others:
            errors.add(
                "knx_address_id",
                f"{address.group_address} is already written by derived status "
                f"“{others[0].name}”; two statuses on one address would be "
                "non-deterministic (§8.9)",
            )
        triggers = [
            r
            for r in await rules_crud.list_rules(db)
            if r.trigger_type == "knx" and r.knx_address_id == address.id
        ]
        if triggers:
            errors.add(
                "knx_address_id",
                f"rule “{triggers[0].name}” triggers on {address.group_address}; a "
                "status written there would feed the rule layer (§8.7)",
            )
    source = values["source_type"]
    if source == "lighting_group_all_at":
        group_id = values.get("lighting_group_id")
        if group_id is None:
            errors.add("lighting_group_id", "this status reflects a lighting group")
        elif await lighting_crud.get_group(db, group_id) is None:
            errors.add("lighting_group_id", "there is no lighting group with that id")
        if values.get("compare_level") is None:
            errors.add("compare_level", "the level every member must be at, e.g. 100")
    else:
        for name in ("lighting_group_id", "compare_level"):
            if values.get(name) is not None:
                errors.add(name, "only a lighting_group_all_at status has a group and level")
        if values.get("basis", "level") != "level":
            errors.add("basis", "only a lighting_group_all_at status compares what the room sees")
    if source == "device_state":
        device_id = values.get("device_id")
        if device_id is None:
            errors.add("device_id", "this status reflects a device")
        elif await devices_crud.get(db, device_id) is None:
            errors.add("device_id", "there is no device with that id")
        state = values.get("compare_state")
        if not state:
            errors.add("compare_state", "the state it reflects, e.g. online")
        elif not valid_state_name(state):
            errors.add("compare_state", "the state is a lower-case name, e.g. online")
    else:
        for name in ("device_id", "compare_state"):
            if values.get(name) is not None:
                errors.add(name, "only a device_state status names a device and state")
    if source == "video_destination_input":
        destination_id = values.get("video_destination_id")
        input_id = values.get("compare_input_id")
        destination = None
        if destination_id is None:
            errors.add("video_destination_id", "this status reflects an HDMI destination")
        else:
            destination = await video_crud.get_destination(db, destination_id)
            if destination is None:
                errors.add("video_destination_id", "there is no HDMI destination with that id")
        if input_id is None:
            errors.add("compare_input_id", "the input the destination must be showing")
        else:
            input_row = await video_crud.get_input(db, input_id)
            if input_row is None:
                errors.add("compare_input_id", "there is no HDMI input with that id")
            elif destination is not None and input_row.device_id != destination.device_id:
                errors.add(
                    "compare_input_id",
                    "that input is on a different matrix from the destination",
                )
    else:
        for name in ("video_destination_id", "compare_input_id"):
            if values.get(name) is not None:
                errors.add(
                    name, "only a video_destination_input status names a destination and input"
                )
    if errors:
        raise _invalid(errors, "The derived status is not valid")


def _duplicate_address(exc: ConstraintError) -> ApiError:
    return ApiError(
        ErrorCode.VALIDATION_FAILED,
        "The derived status is not valid",
        {
            "fields": {"knx_address_id": ["that address is already bound to a derived status"]},
            "constraint": exc.constraint,
        },
    )


@router.get("/derived-status", response_model=DerivedStatusesResponse)
async def list_derived_statuses(_: Admin, db: Db) -> DerivedStatusesResponse:
    statuses = await rules_crud.list_derived_status(db)
    return DerivedStatusesResponse(derived_statuses=[await _status_model(db, s) for s in statuses])


@router.post("/derived-status", response_model=DerivedStatusModel, status_code=201)
async def create_derived_status(
    _: Admin, db: Db, request: Request, body: Annotated[DerivedStatusCreate, Body()]
) -> DerivedStatusModel:
    values = body.model_dump()
    await _validate_status(db, values)
    try:
        status = await rules_crud.create_derived_status(db, **values)
    except ConstraintError as exc:
        raise _duplicate_address(exc) from exc
    except sqlite3.IntegrityError as exc:
        raise _invalid({"derived_status": [str(exc)]}) from exc
    await _reload(request)
    return await _status_model(db, status)


@router.get("/derived-status/state", response_model=DerivedStatesResponse)
async def derived_states(_: Staff, request: Request) -> DerivedStatesResponse:
    """Current value per address, and when it last changed (§16.5, §8.10)."""
    engine = _engine(request)
    return DerivedStatesResponse(statuses=[r.as_dict() for r in engine.derived.readings()])


@router.get("/derived-status/monitor")
async def derived_monitor(_: Admin, request: Request) -> StreamingResponse:
    """§8.10's live monitor, as server-sent events — the shape of the KNX telegram
    monitor (§21.19): every status as it stands, then each change as it happens."""
    engine = _engine(request)
    return StreamingResponse(
        _sse(engine),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _sse(engine: RulesEngine) -> AsyncIterator[str]:
    async for reading in engine.derived.stream(idle_s=SSE_KEEPALIVE_S):
        if reading is None:
            yield ": keepalive\n\n"  # also how a closed client is noticed
        else:
            yield f"event: status\ndata: {json.dumps(reading.as_dict())}\n\n"


@router.get("/derived-status/{status_id}", response_model=DerivedStatusModel)
async def get_derived_status(_: Admin, db: Db, status_id: int) -> DerivedStatusModel:
    return await _status_model(db, await _status_or_404(db, status_id))


@router.put("/derived-status/{status_id}", response_model=DerivedStatusModel)
async def update_derived_status(
    _: Admin,
    db: Db,
    request: Request,
    status_id: int,
    body: Annotated[DerivedStatusUpdate, Body()],
    if_unmodified_since_version: Annotated[str | None, Header()] = None,
) -> DerivedStatusModel:
    version = _version_or_422(if_unmodified_since_version)
    current = await _status_or_404(db, status_id)
    changes = {name: getattr(body, name) for name in body.model_fields_set}
    cleared = sorted(n for n, v in changes.items() if v is None and n in _STATUS_NOT_NULL)
    if cleared:
        raise _invalid({name: ["may not be cleared"] for name in cleared})
    merged = {
        "name": current.name,
        "enabled": current.enabled,
        "knx_address_id": current.knx_address_id,
        "source_type": current.source_type,
        "lighting_group_id": current.lighting_group_id,
        "compare_level": current.compare_level,
        "device_id": current.device_id,
        "compare_state": current.compare_state,
        "basis": current.basis,
        "video_destination_id": current.video_destination_id,
        "compare_input_id": current.compare_input_id,
        **changes,
    }
    await _validate_status(db, merged, status_id=status_id)
    try:
        status = await rules_crud.update_derived_status(db, status_id, version, **changes)
    except ConflictError as exc:
        latest = await _status_or_404(db, status_id)
        raise ApiError(
            ErrorCode.CONFLICT,
            "This derived status was changed by someone else since you loaded it",
            {"current": (await _status_model(db, latest)).model_dump()},
        ) from exc
    except ConstraintError as exc:
        raise _duplicate_address(exc) from exc
    except sqlite3.IntegrityError as exc:
        raise _invalid({"derived_status": [str(exc)]}) from exc
    await _reload(request)
    return await _status_model(db, status)


@router.delete("/derived-status/{status_id}", status_code=204, response_class=Response)
async def delete_derived_status(
    _: Admin, snapshot: PreChangeSnapshot, db: Db, request: Request, status_id: int
) -> Response:
    await snapshot(f"delete derived status {status_id}")
    await _status_or_404(db, status_id)
    await rules_crud.delete_derived_status(db, status_id)
    await _reload(request)
    return Response(status_code=204)
