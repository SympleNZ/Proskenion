"""Lighting: control, configuration and the external-control toggle (spec §16.5, §9).

Tiers follow §16.5's own annotations, with one considered exception, recorded
here and in the task report rather than left silent (CONVENTIONS.md
*Deviations*): §16.1's blanket "configuration entities... admin tier" would
make ``GET /lighting/channels``, ``GET /lighting/groups``, ``GET
/lighting/bars`` and ``GET /lighting/patch/conflicts`` admin-only, but the
already-shipped operator Lighting view and Stage Plan call them
unconditionally for *any* signed-in staff session — an operator's screen
would 403 on load. Reads of these four are **staff** (admin, operator); every
write stays admin, matching the blanket rule and the brief. §16.5 also lists
``hirer`` for ``GET /lighting/state`` and the channel/group level and colour
writes, and those four admit a hirer — each held to the live ``state.hirer``
snapshot target by target, never to the token (§6.7, B31):

* ``GET /lighting/state`` is filtered exactly as the ``lighting_state`` frame
  is (:func:`~proskenion.core.broadcast.filter_for_hirer`), and is empty
  while lighting is off for hirers;
* a channel level needs the channel writable — placed on an assigned page, or
  in a placed group while individual fixtures are on (the phase-5 plan's Q3);
* a colour needs that **and** colour enabled;
* a group level needs the group's master on an assigned page.

A refusal is ``permission_denied`` with an audit row, checked before the
target is looked up so an id a hirer cannot write answers the same whether or
not it exists. The master, blackout, the bulk ``POST /lighting/levels``,
presets, the snapshot and every configuration route stay staff or admin.

Continuous vs REST (§16.1 *Continuous writes are not REST*)
-------------------------------------------------------------
The level, colour, group-level and master endpoints below exist for
scripting and testing, rate-limited accordingly; the interactive path for a
drag is the WebSocket ``set`` handlers this module also registers (see
:func:`register_write_handlers`), not these routes.

``POST /lighting/blackout`` (§7.1, an interpretation)
------------------------------------------------------
The specification does not define this endpoint beyond its existence and
tier. §7.1's *Why DMX stays down until someone raises it* explains, for the
alarm scene's own blackout snapshot, exactly the shape this endpoint borrows:
a **model-level** write through the fade engine, not a transport suspend — a
suspend is undone by the compositor on the next tick, while a zeroed level
stays zero until a fader, a stage-plan tap or a scene raises it again, and
survives a reboot. :func:`~proskenion.core.lifecycle.blackout_dmx_channels`
carries this out; the §12.4 shutdown hook uses the same function.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict
from pydantic import Field as ModelField

from proskenion.api.deps import (
    get_bus,
    get_db,
    get_desk_input,
    get_lighting,
    get_state,
    refuse_hirer,
    require_admin,
    require_control,
    require_staff,
    settle_hirer_permissions,
)
from proskenion.api.errors import ApiError, ErrorCode
from proskenion.api.snapshots import PreChangeSnapshot
from proskenion.api.ws import SetHandler, SetRequest, SetResult, WriteRouter
from proskenion.core.auth import TokenClaims
from proskenion.core.broadcast import RESYNC_SOURCE, filter_for_hirer
from proskenion.core.bus import EventBus
from proskenion.core.dmx.compositor import COLOUR_ROLES, Colour, clamp_level
from proskenion.core.dmx.desk import DeskInput
from proskenion.core.dmx.fade import ChannelLockedError, UnknownChannelError, UnknownGroupError
from proskenion.core.events import LightingConfigChanged
from proskenion.core.hirer_enforcement import hirer_may_colour
from proskenion.core.lifecycle import blackout_dmx_channels
from proskenion.core.lighting import BumpRefusedError, IndicatorOnlyGroupError, LightingService
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import knx as knx_crud
from proskenion.db.crud import lighting as lighting_crud
from proskenion.db.crud.base import ConflictError, NotFoundError
from proskenion.db.crud.refs import ConstraintError, InUseError, Reference

log = logging.getLogger(__name__)

router = APIRouter(tags=["lighting"])

#: The §16.1 optimistic-concurrency header every configuration PUT carries.
VERSION_HEADER = "If-Unmodified-Since-Version"

#: §15.9's DDL comment, verbatim: "fade_mode  hardware | hardware_timed | software".
#: ``core/lighting.py``'s ``FadeMode`` only runs two of these (``hardware_timed``
#: is driven as ``hardware`` with a logged gap — no fade-time group address
#: column exists yet); all three are still a channel's valid *configured* value.
FADE_MODES = frozenset({"hardware", "hardware_timed", "software"})

Admin = Annotated[TokenClaims, Depends(require_admin)]
Staff = Annotated[TokenClaims, Depends(require_staff)]
#: Staff, or a hirer held to ``state.hirer`` target by target (§6.7).
Control = Annotated[TokenClaims, Depends(require_control)]
Db = Annotated[Database, Depends(get_db)]
Bus = Annotated[EventBus, Depends(get_bus)]
State = Annotated[StateStore, Depends(get_state)]
Lighting = Annotated[LightingService, Depends(get_lighting)]
DeskInputDep = Annotated[DeskInput | None, Depends(get_desk_input)]


def _version_or_422(version: str | None) -> str:
    if not version:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            f"{VERSION_HEADER} is required so a concurrent edit is not overwritten",
            {VERSION_HEADER: ["required"]},
        )
    return version


def _reference_list(references: list[Reference]) -> list[dict[str, Any]]:
    return [asdict(r) for r in references]


def _provided(body: BaseModel) -> dict[str, Any]:
    """Only the fields the client actually sent — so an explicit ``null`` (clearing
    ``bar_id``, say) is distinguished from a field simply left out (§16.1)."""
    return {name: getattr(body, name) for name in body.model_fields_set}


async def _emit_config_changed(bus: EventBus, reason: str) -> None:
    """Every configuration write ends with this: the lighting service
    reloads its configuration from the database on receipt."""
    bus.emit(LightingConfigChanged(reason=reason))


# -- lighting state and control (§16.5) --------------------------------------------


def _channel_sort_key(item: str) -> tuple[int, int | str]:
    return (0, int(item)) if item.isdigit() else (1, item)


def _lighting_state_snapshot(state: StateStore) -> dict[str, Any]:
    """The current look in §16.8's ``lighting_state`` shape, minus ``type``/``source``."""
    domain = state.lighting
    levels = domain.get("levels")
    colour = domain.get("colour")
    assert isinstance(levels, dict) and isinstance(colour, dict)
    channels: dict[str, dict[str, Any]] = {}
    for channel_id in sorted(set(levels) | set(colour), key=_channel_sort_key):
        entry: dict[str, Any] = {}
        if channel_id in levels:
            entry["level"] = levels[channel_id]
        component = colour.get(channel_id)
        if isinstance(component, dict):
            entry.update(component)
        if entry:
            channels[channel_id] = entry
    observed = domain.get("observed")
    return {
        "channels": channels,
        "master": domain.get("master"),
        "external_control": domain.get("external_control"),
        "observed": observed if observed else None,
    }


#: What a hirer is sent while lighting is off for them (Q3): nothing to show.
_EMPTY_HIRER_LOOK: dict[str, Any] = {
    "channels": {},
    "master": None,
    "external_control": None,
    "observed": None,
}


@router.get("/lighting/state")
async def get_lighting_state(claims: Control, state: State) -> dict[str, Any]:
    """The current look (§16.5). A hirer's is filtered by the very function that
    filters the ``lighting_state`` frame, so the two cannot drift: reachable
    channels (a group's members among them, so a group fader can show its
    level) and ``master`` read-only; nothing at all while lighting is off for
    them. There is no ``groups`` section: a group fader sets its members'
    levels and has no stored value of its own (owner decision 2026-09-30)."""
    look = _lighting_state_snapshot(state)
    if not claims.is_hirer:
        return look
    frame = {"type": "lighting_state", "source": RESYNC_SOURCE, **look}
    seen = filter_for_hirer(frame, state.hirer.permissions)
    if seen is None:
        return dict(_EMPTY_HIRER_LOOK)
    return {key: seen.get(key) for key in _EMPTY_HIRER_LOOK}


class LevelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: float
    fade_ms: int = ModelField(default=0, ge=0)


class ColourBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    r: int = ModelField(ge=0, le=255)
    g: int = ModelField(ge=0, le=255)
    b: int = ModelField(ge=0, le=255)
    w: int | None = ModelField(default=None, ge=0, le=255)


class TestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["full", "off"]


class MasterBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: float


class ExternalControlBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manual: bool


class LevelsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    levels: dict[str, float]
    fade_ms: int = ModelField(default=0, ge=0)


def _locked_detail(exc: ChannelLockedError) -> dict[str, Any]:
    return {"scene_id": exc.run.scene_id}


async def _refuse_unless(
    allowed: bool,
    request: Request,
    claims: TokenClaims,
    *,
    domain: str,
    target_id: int,
    reason: str,
) -> None:
    """For a hirer, the audited 403 unless ``allowed``. Staff always pass.

    Checked before the target is looked up, so an id a hirer cannot write
    answers the same whether or not it exists.
    """
    if claims.is_hirer and not allowed:
        raise await refuse_hirer(request, claims, domain=domain, target_id=target_id, reason=reason)


@router.post("/lighting/channels/{channel_id}/level")
async def set_channel_level(
    claims: Control,
    request: Request,
    state: State,
    service: Lighting,
    channel_id: int,
    body: LevelBody,
) -> dict[str, float]:
    """``{level, fade_ms?}``. A value outside the channel's range succeeds at the
    clamped value — 422 ``value_out_of_range`` with ``detail.clamped`` (§16.1, B35).
    A hirer needs the channel writable (Q3)."""
    await _refuse_unless(
        state.hirer.permissions.lighting_writable(channel_id),
        request,
        claims,
        domain="lighting",
        target_id=channel_id,
        reason="not_writable",
    )
    try:
        min_value, max_value = service.fades.range_of(channel_id)
    except UnknownChannelError as exc:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no lighting channel with that id") from exc
    try:
        handle = service.set_level(channel_id, body.level, fade_ms=body.fade_ms)
    except ChannelLockedError as exc:
        raise ApiError(
            ErrorCode.PERMISSION_DENIED,
            "This channel is locked by a running scene",
            _locked_detail(exc),
        ) from exc
    if body.level < min_value or body.level > max_value:
        raise ApiError(
            ErrorCode.VALUE_OUT_OF_RANGE,
            "The level was outside the channel's range",
            {"clamped": handle.target_level},
        )
    assert handle.target_level is not None
    return {"level": handle.target_level}


@router.post("/lighting/channels/{channel_id}/colour")
async def set_channel_colour(
    claims: Control,
    request: Request,
    state: State,
    service: Lighting,
    channel_id: int,
    body: ColourBody,
) -> dict[str, Any]:
    """``{r, g, b, w?}``, 0–255 each. A hirer needs the channel writable and
    colour enabled (Q3)."""
    permissions = state.hirer.permissions
    await _refuse_unless(
        hirer_may_colour(permissions, channel_id),
        request,
        claims,
        domain="lighting_colour",
        target_id=channel_id,
        reason=(
            "colour_disabled" if permissions.lighting_writable(channel_id) else "not_writable"
        ),
    )
    colour = Colour.of(body.r, body.g, body.b, body.w)
    try:
        handle = service.set_colour(channel_id, colour)
    except UnknownChannelError as exc:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no lighting channel with that id") from exc
    except ChannelLockedError as exc:
        raise ApiError(
            ErrorCode.PERMISSION_DENIED,
            "This channel is locked by a running scene",
            _locked_detail(exc),
        ) from exc
    assert handle.target_colour is not None
    return handle.target_colour.to_store()


@router.post("/lighting/channels/{channel_id}/test", status_code=204, response_class=Response)
async def test_channel(_: Admin, service: Lighting, channel_id: int, body: TestBody) -> Response:
    """``{mode: "full"|"off"}`` — drive a fixture for patch verification.

    The lighting service has no bypass path separate from an ordinary write
    (out of this task's scope to add — see ``proskenion/core/lighting.py``),
    so this is a zero-duration ``set_level``: the result is persisted like any
    other write, not reverted when the test view closes.
    """
    try:
        service.set_level(channel_id, 100.0 if body.mode == "full" else 0.0)
    except UnknownChannelError as exc:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no lighting channel with that id") from exc
    except ChannelLockedError as exc:
        raise ApiError(
            ErrorCode.PERMISSION_DENIED,
            "This channel is locked by a running scene",
            _locked_detail(exc),
        ) from exc
    return Response(status_code=204)


@router.post("/lighting/groups/{group_id}/level")
async def set_group_level(
    claims: Control,
    request: Request,
    state: State,
    service: Lighting,
    group_id: int,
    body: LevelBody,
) -> dict[str, Any]:
    """``{level, fade_ms?}`` 0–100: every member's level to ``level`` (owner
    decision 2026-09-30 — a group fader sets levels; it is no longer a
    multiplier). A hirer needs the group's master on an assigned page.

    Answers ``{level, levels}``: the level asked for (clamped to 0–100) and
    each member's level after its own range clamped it — a member held to its
    range is the group fader working as intended, not an error. Members a
    critical scene holds are left alone and, once every other member has
    been set, reported as 403 ``permission_denied`` with ``detail.locked``,
    as ``POST /lighting/levels`` does."""
    await _refuse_unless(
        state.hirer.permissions.group_reachable(group_id),
        request,
        claims,
        domain="lighting_group",
        target_id=group_id,
        reason="unreachable",
    )
    try:
        result = service.set_group_level(group_id, body.level, fade_ms=body.fade_ms)
    except UnknownGroupError as exc:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no lighting group with that id") from exc
    except IndicatorOnlyGroupError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "This group is indicator-only: it has no fader",
            {"group_id": ["an indicator-only group has no fader"]},
        ) from exc
    except ValueError as exc:
        raise ApiError(ErrorCode.VALIDATION_FAILED, str(exc), {"level": [str(exc)]}) from exc
    if result.refused:
        raise ApiError(
            ErrorCode.PERMISSION_DENIED,
            "Some of this group's channels are locked by a running scene",
            {"locked": [str(c) for c in result.refused]},
        )
    levels: dict[str, float] = {}
    for channel_id, handle in result.handles.items():
        assert handle.target_level is not None
        levels[str(channel_id)] = handle.target_level
    return {"level": clamp_level(body.level), "levels": levels}


@router.post("/lighting/master")
async def set_master(_: Staff, service: Lighting, body: MasterBody) -> dict[str, float]:
    return {"level": service.set_master(body.level)}


@router.post("/lighting/blackout", status_code=204, response_class=Response)
async def blackout(_: Staff, service: Lighting) -> Response:
    """Zero every DMX channel through the fade engine — see the module docstring."""
    blackout_dmx_channels(service)
    return Response(status_code=204)


@router.post("/lighting/levels")
async def set_levels(_: Staff, service: Lighting, body: LevelsBody) -> dict[str, dict[str, float]]:
    """``{levels: {"<channel id>": level}, fade_ms}`` — an addition to §16.5 (recorded
    in ``WORKLOG.md``): several channels' fades start together as one intent, in one
    call, rather than one REST request per channel racing to arrive.

    Every channel it can apply, it does (§8.15's philosophy: one bad id never
    blocks the rest). If any id was unknown, 404 ``not_found`` with
    ``detail.unknown``; else if any was locked, 403 ``permission_denied`` with
    ``detail.locked``; else if any level landed clamped, 422
    ``value_out_of_range`` with ``detail.clamped`` — in that order, since every
    write that could apply already has by the time this responds.
    """
    applied: dict[str, float] = {}
    clamped: dict[str, float] = {}
    unknown: list[str] = []
    locked: list[str] = []
    for key, level in body.levels.items():
        try:
            channel_id = int(key)
            min_value, max_value = service.fades.range_of(channel_id)
        except (ValueError, UnknownChannelError):
            unknown.append(key)
            continue
        try:
            handle = service.set_level(channel_id, level, fade_ms=body.fade_ms)
        except ChannelLockedError:
            locked.append(key)
            continue
        assert handle.target_level is not None
        applied[key] = handle.target_level
        if level < min_value or level > max_value:
            clamped[key] = handle.target_level
    if unknown:
        raise ApiError(ErrorCode.NOT_FOUND, "Some channels do not exist", {"unknown": unknown})
    if locked:
        raise ApiError(
            ErrorCode.PERMISSION_DENIED,
            "Some channels are locked by a running scene",
            {"locked": locked},
        )
    if clamped:
        raise ApiError(
            ErrorCode.VALUE_OUT_OF_RANGE,
            "Some levels were outside their channel's range",
            {"clamped": clamped},
        )
    return {"levels": applied}


class ExternalControlResponse(BaseModel):
    state: str
    active: bool
    #: §16.5. From the desk input's own ``last_frame_at`` (§4.9's ISO 8601
    #: with offset) — the most recent ArtDmx frame from the node's booth
    #: input, whether or not detection is still active: it answers "when did
    #: the desk last say anything", not "is it still talking" (``active``
    #: already answers that). ``None`` when nothing has ever arrived, or
    #: when there is no desk input at all (most venues today).
    last_frame_at: str | None = None


def _external_control_response(
    service: LightingService, desk_input: DeskInput | None
) -> ExternalControlResponse:
    return ExternalControlResponse(
        state=service.external.state,
        active=service.external_active,
        last_frame_at=desk_input.last_frame_at if desk_input is not None else None,
    )


@router.get("/lighting/external-control", response_model=ExternalControlResponse)
async def get_external_control(
    _: Staff, service: Lighting, desk_input: DeskInputDep
) -> ExternalControlResponse:
    return _external_control_response(service, desk_input)


@router.post("/lighting/external-control", response_model=ExternalControlResponse)
async def set_external_control(
    _: Staff, service: Lighting, body: ExternalControlBody, desk_input: DeskInputDep
) -> ExternalControlResponse:
    """``{manual}``. Cannot force off while a desk is sending — detection wins.

    The underlying flag is still cleared exactly as
    :meth:`~proskenion.core.lighting.ExternalControl.set_manual` (untouched by
    this handler) already does — ``active`` stays true either way while
    detection holds — so the moment detection stops, off no longer needs a
    second toggle.
    """
    state = service.set_external_manual(body.manual)
    if not body.manual and state == "detected":
        raise ApiError(
            ErrorCode.CONFLICT,
            "External control cannot be turned off while a desk is sending",
            {
                "reason": "frames_arriving",
                "current": _external_control_response(service, desk_input).model_dump(),
            },
        )
    return _external_control_response(service, desk_input)


@router.post("/lighting/snapshot")
async def save_snapshot(_: Admin, service: Lighting) -> dict[str, Any]:
    """Save look (§9.7, admin only — §16.5). Captures the current look in §8.12's
    ``dmx_snapshot`` format. Turning this into a reusable named scene is the scene
    engine's job (``proskenion/scene/``), out of this task's scope."""
    return {"snapshot": service.capture_snapshot()}


# -- patch overlap models, used by both the channels endpoints and the
#    dedicated /lighting/patch/conflicts view (§9.1) -------------------------------


class PatchConflictModel(BaseModel):
    channel_ids: list[int]
    device_id: int
    universe: int
    slots: list[int]


class PatchConflictsResponse(BaseModel):
    conflicts: list[PatchConflictModel]


def _overlap_to_conflict(overlap: lighting_crud.Overlap) -> PatchConflictModel:
    low = max(overlap.range_a[0], overlap.range_b[0])
    high = min(overlap.range_a[1], overlap.range_b[1])
    return PatchConflictModel(
        channel_ids=sorted({overlap.channel_a_id, overlap.channel_b_id}),
        device_id=overlap.device_id,
        universe=overlap.universe,
        slots=list(range(low, high + 1)),
    )


# -- configuration: lighting channels (§15.9, §16.1) -------------------------------


class ChannelModel(BaseModel):
    id: int
    name: str
    type: str
    profile_id: int | None
    min_value: float
    max_value: float
    has_colour: bool
    #: Every group the fixture belongs to (membership; writable on PUT).
    group_ids: list[int]
    #: Of those, the groups with a fader — ``group_ids`` without
    #: indicator-only groups (migration 011). Read-only: the Lighting view
    #: places a fixture's strip under the first of them.
    fader_group_ids: list[int]
    bar_id: int | None
    position: float
    visible_staff: bool
    device_id: int | None
    universe: int
    address: int | None
    knx_command_address_id: int | None
    knx_status_address_id: int | None
    knx_switch_address_id: int | None
    fade_mode: str
    colour_r: int | None
    colour_g: int | None
    colour_b: int | None
    colour_w: int | None
    notes: str | None
    updated_at: str
    #: Overlaps this fixture is now part of (§9.1) — populated by create and
    #: update, which warn rather than refuse; empty (not queried) on a plain
    #: read, where the dedicated GET /lighting/patch/conflicts is the full view.
    conflicts: list[PatchConflictModel] = ModelField(default_factory=list)


class ChannelsResponse(BaseModel):
    channels: list[ChannelModel]


class ChannelCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = ModelField(min_length=1, max_length=120)
    type: str
    #: Wins over `has_colour` when both are given (see `_profile_id_for_has_colour`).
    profile_id: int | None = None
    min_value: float = 0.0
    max_value: float = 100.0
    #: The stage plan's minimal Add-Fixture form's shorthand for `profile_id` —
    #: never used, and never creates a profile, once `profile_id` is given.
    has_colour: bool = False
    group_ids: list[int] = ModelField(default_factory=list)
    bar_id: int | None = None
    position: float = 0.5
    visible_staff: bool = True
    device_id: int | None = None
    universe: int = 1
    address: int | None = None
    knx_command_address_id: int | None = None
    knx_status_address_id: int | None = None
    knx_switch_address_id: int | None = None
    fade_mode: str = "hardware"
    colour_r: int | None = ModelField(default=None, ge=0, le=255)
    colour_g: int | None = ModelField(default=None, ge=0, le=255)
    colour_b: int | None = ModelField(default=None, ge=0, le=255)
    colour_w: int | None = ModelField(default=None, ge=0, le=255)
    notes: str | None = None


class ChannelUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    type: str | None = None
    profile_id: int | None = None
    min_value: float | None = None
    max_value: float | None = None
    group_ids: list[int] | None = None
    bar_id: int | None = None
    position: float | None = None
    visible_staff: bool | None = None
    device_id: int | None = None
    universe: int | None = None
    address: int | None = None
    knx_command_address_id: int | None = None
    knx_status_address_id: int | None = None
    knx_switch_address_id: int | None = None
    fade_mode: str | None = None
    colour_r: int | None = ModelField(default=None, ge=0, le=255)
    colour_g: int | None = ModelField(default=None, ge=0, le=255)
    colour_b: int | None = ModelField(default=None, ge=0, le=255)
    colour_w: int | None = ModelField(default=None, ge=0, le=255)
    notes: str | None = None


async def _channel_or_404(db: Database, channel_id: int) -> lighting_crud.LightingChannel:
    channel = await lighting_crud.get_channel(db, channel_id)
    if channel is None:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no lighting channel with that id")
    return channel


@dataclass(frozen=True, slots=True)
class ChannelGroups:
    """A fixture's groups: every membership, and those with a fader."""

    ids: tuple[int, ...] = ()
    with_fader: tuple[int, ...] = ()


NO_GROUPS = ChannelGroups()


async def _group_ids_by_channel(db: Database) -> dict[int, ChannelGroups]:
    ids: dict[int, list[int]] = {}
    with_fader: dict[int, list[int]] = {}
    for group in await lighting_crud.list_groups(db):
        for member in await lighting_crud.get_group_members(db, group.id):
            ids.setdefault(member.channel_id, []).append(group.id)
            if not group.indicator_only:
                with_fader.setdefault(member.channel_id, []).append(group.id)
    return {
        channel_id: ChannelGroups(tuple(groups), tuple(with_fader.get(channel_id, ())))
        for channel_id, groups in ids.items()
    }


async def _channel_has_colour(db: Database, channel: lighting_crud.LightingChannel) -> bool:
    if channel.profile_id is None:
        return False
    profile = await lighting_crud.get_fixture_profile(db, channel.profile_id)
    return profile is not None and any(c.role in COLOUR_ROLES for c in profile.channels)


async def _channel_conflicts(
    db: Database, channel: lighting_crud.LightingChannel
) -> list[PatchConflictModel]:
    """Overlaps ``channel`` is now part of. **Warns; never blocks the save** (§9.1)."""
    if channel.type != "dmx" or channel.device_id is None:
        return []
    overlaps = await lighting_crud.find_overlaps(db, device_id=channel.device_id)
    return [
        _overlap_to_conflict(o) for o in overlaps if channel.id in (o.channel_a_id, o.channel_b_id)
    ]


def _channel_model(
    channel: lighting_crud.LightingChannel,
    *,
    groups: ChannelGroups,
    has_colour: bool,
    conflicts: list[PatchConflictModel] | None = None,
) -> ChannelModel:
    return ChannelModel(
        id=channel.id,
        name=channel.name,
        type=channel.type,
        profile_id=channel.profile_id,
        min_value=channel.min_value,
        max_value=channel.max_value,
        has_colour=has_colour,
        group_ids=sorted(groups.ids),
        fader_group_ids=sorted(groups.with_fader),
        bar_id=channel.bar_id,
        position=channel.position,
        visible_staff=channel.visible_staff,
        device_id=channel.device_id,
        universe=channel.universe,
        address=channel.address,
        knx_command_address_id=channel.knx_command_address_id,
        knx_status_address_id=channel.knx_status_address_id,
        knx_switch_address_id=channel.knx_switch_address_id,
        fade_mode=channel.fade_mode,
        colour_r=channel.colour_r,
        colour_g=channel.colour_g,
        colour_b=channel.colour_b,
        colour_w=channel.colour_w,
        notes=channel.notes,
        updated_at=channel.updated_at,
        conflicts=list(conflicts) if conflicts is not None else [],
    )


@router.get("/lighting/channels", response_model=ChannelsResponse)
async def list_channels(_: Staff, db: Db) -> ChannelsResponse:
    channels = await lighting_crud.list_channels(db)
    group_ids = await _group_ids_by_channel(db)
    profiles = {p.id: p for p in await lighting_crud.list_fixture_profiles(db)}
    return ChannelsResponse(
        channels=[
            _channel_model(
                c,
                groups=group_ids.get(c.id, NO_GROUPS),
                has_colour=(
                    c.profile_id is not None
                    and c.profile_id in profiles
                    and any(role.role in COLOUR_ROLES for role in profiles[c.profile_id].channels)
                ),
            )
            for c in channels
        ]
    )


@router.get("/lighting/channels/{channel_id}", response_model=ChannelModel)
async def get_channel(_: Staff, db: Db, channel_id: int) -> ChannelModel:
    channel = await _channel_or_404(db, channel_id)
    groups = (await _group_ids_by_channel(db)).get(channel_id, NO_GROUPS)
    has_colour = await _channel_has_colour(db, channel)
    return _channel_model(channel, groups=groups, has_colour=has_colour)


async def _profile_id_for_has_colour(db: Database, *, has_colour: bool) -> int | None:
    """The first existing fixture profile shaped like ``has_colour`` asks for, or ``None``.

    ``has_colour`` is the stage plan's minimal *Add fixture* form's (§21.12,
    deliberately not the full patch editor) shorthand for a
    profile, used only when the caller gives no ``profile_id`` directly — it
    never creates one. This looks for the first profile with a single
    ``dimmer`` channel, or one with a colour role, which in a seeded database
    is §15.2's "Single-channel dimmer" or "RGB" — the same profiles a real
    patch would use. ``None`` means nothing matches; the caller answers 422
    asking for an explicit ``profile_id``.
    """
    for profile in await lighting_crud.list_fixture_profiles(db):
        roles = {c.role for c in profile.channels}
        if has_colour and roles & COLOUR_ROLES:
            return profile.id
        if not has_colour and roles == {"dimmer"}:
            return profile.id
    return None


async def _validate_channel_shape(
    db: Database,
    *,
    type: str,
    profile_id: int | None,
    device_id: int | None,
    address: int | None,
    knx_command_address_id: int | None,
    knx_status_address_id: int | None,
    knx_switch_address_id: int | None,
    fade_mode: str,
) -> dict[str, list[str]]:
    """§15.9's shape, validated ahead of the write so a bad one answers 422
    ``validation_failed`` with field errors, never a raw ``IntegrityError``.

    "A row is one shape or the other, never half of each" (§15.9's DDL
    comment): a ``dmx`` row needs ``profile_id``, ``device_id`` and
    ``address`` and none of the three KNX address columns; a ``knx_dimmer``
    row needs ``knx_command_address_id`` and none of ``profile_id``,
    ``device_id`` or ``address``. This is stricter than the database's own
    ``CHECK``, which does not constrain the KNX columns on a ``dmx`` row or
    ``device_id`` on a ``knx_dimmer`` one — enforced here anyway so a row is
    never half of each in practice, not only as far as SQLite can tell.
    Every id given is also checked against the table it references
    (``fixture_profiles``, ``devices``, ``knx_group_addresses``), so a
    dangling reference is a field error here rather than a foreign-key
    failure at the database.
    """
    if type not in lighting_crud.CHANNEL_TYPES:
        return {"type": ["must be dmx or knx_dimmer"]}
    errors: dict[str, list[str]] = {}
    if type == "dmx":
        if profile_id is None:
            errors.setdefault("profile_id", []).append("required for a dmx channel")
        if device_id is None:
            errors.setdefault("device_id", []).append("required for a dmx channel")
        if address is None:
            errors.setdefault("address", []).append("required for a dmx channel")
        for field, value in (
            ("knx_command_address_id", knx_command_address_id),
            ("knx_status_address_id", knx_status_address_id),
            ("knx_switch_address_id", knx_switch_address_id),
        ):
            if value is not None:
                errors.setdefault(field, []).append("must be empty for a dmx channel")
    else:
        if knx_command_address_id is None:
            errors.setdefault("knx_command_address_id", []).append(
                "required for a knx_dimmer channel"
            )
        if profile_id is not None:
            errors.setdefault("profile_id", []).append("must be empty for a knx_dimmer channel")
        if device_id is not None:
            errors.setdefault("device_id", []).append("must be empty for a knx_dimmer channel")
        if address is not None:
            errors.setdefault("address", []).append("must be empty for a knx_dimmer channel")
    if fade_mode not in FADE_MODES:
        errors.setdefault("fade_mode", []).append(f"must be one of {', '.join(sorted(FADE_MODES))}")
    if profile_id is not None and "profile_id" not in errors:
        if await lighting_crud.get_fixture_profile(db, profile_id) is None:
            errors.setdefault("profile_id", []).append("no fixture profile with that id")
    if device_id is not None and "device_id" not in errors:
        if await devices_crud.get(db, device_id) is None:
            errors.setdefault("device_id", []).append("no device with that id")
    for field, value in (
        ("knx_command_address_id", knx_command_address_id),
        ("knx_status_address_id", knx_status_address_id),
        ("knx_switch_address_id", knx_switch_address_id),
    ):
        if value is not None and field not in errors:
            if await knx_crud.get_address(db, value) is None:
                errors.setdefault(field, []).append("no KNX group address with that id")
    return errors


def _effective_channel_shape(
    current: lighting_crud.LightingChannel, fields: dict[str, Any]
) -> dict[str, Any]:
    """The channel's shape after ``fields`` (a PUT's provided values) is applied.

    A field the caller did not send keeps the current row's value; one sent
    as ``null`` overrides it — which is how a type change nulls the old
    shape's fields, per §15.9.
    """

    def value(name: str, current_value: Any) -> Any:
        return fields[name] if name in fields else current_value

    return {
        "type": value("type", current.type),
        "profile_id": value("profile_id", current.profile_id),
        "device_id": value("device_id", current.device_id),
        "address": value("address", current.address),
        "knx_command_address_id": value(
            "knx_command_address_id", current.knx_command_address_id
        ),
        "knx_status_address_id": value("knx_status_address_id", current.knx_status_address_id),
        "knx_switch_address_id": value("knx_switch_address_id", current.knx_switch_address_id),
        "fade_mode": value("fade_mode", current.fade_mode),
    }


async def _set_channel_groups(db: Database, channel_id: int, group_ids: list[int]) -> None:
    """Make ``channel_id`` a member of exactly ``group_ids``.

    ``lighting_crud`` only replaces a *group's* whole membership list, so this
    reads every group's current members, adds or removes this channel, and
    writes back only the groups that actually changed — one call per affected
    group, which is fine at this rig's scale.
    """
    wanted = set(group_ids)
    for group in await lighting_crud.list_groups(db):
        members = [m.channel_id for m in await lighting_crud.get_group_members(db, group.id)]
        is_member = channel_id in members
        should_be = group.id in wanted
        if is_member and not should_be:
            await lighting_crud.set_group_members(
                db, group.id, [c for c in members if c != channel_id]
            )
        elif should_be and not is_member:
            await lighting_crud.set_group_members(db, group.id, [*members, channel_id])


@router.post("/lighting/channels", response_model=ChannelModel, status_code=201)
async def create_channel(
    _: Admin, request: Request, db: Db, bus: Bus, body: ChannelCreate
) -> ChannelModel:
    if body.type not in lighting_crud.CHANNEL_TYPES:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "Unknown lighting channel type",
            {"type": ["must be dmx or knx_dimmer"]},
        )
    profile_id = body.profile_id
    if profile_id is None and body.type == "dmx":
        profile_id = await _profile_id_for_has_colour(db, has_colour=body.has_colour)
        if profile_id is None:
            raise ApiError(
                ErrorCode.VALIDATION_FAILED,
                "No fixture profile matches has_colour; choose a profile explicitly",
                {"profile_id": ["no existing profile is shaped this way — choose one"]},
            )

    errors = await _validate_channel_shape(
        db,
        type=body.type,
        profile_id=profile_id,
        device_id=body.device_id,
        address=body.address,
        knx_command_address_id=body.knx_command_address_id,
        knx_status_address_id=body.knx_status_address_id,
        knx_switch_address_id=body.knx_switch_address_id,
        fade_mode=body.fade_mode,
    )
    if errors:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED, "The fixture's shape is not valid", errors
        )

    try:
        channel = await lighting_crud.create_channel(
            db,
            name=body.name,
            type=body.type,
            profile_id=profile_id,
            device_id=body.device_id,
            universe=body.universe,
            address=body.address,
            bar_id=body.bar_id,
            position=body.position,
            colour_r=body.colour_r,
            colour_g=body.colour_g,
            colour_b=body.colour_b,
            colour_w=body.colour_w,
            knx_command_address_id=body.knx_command_address_id,
            knx_status_address_id=body.knx_status_address_id,
            knx_switch_address_id=body.knx_switch_address_id,
            fade_mode=body.fade_mode,
            min_value=body.min_value,
            max_value=body.max_value,
            visible_staff=body.visible_staff,
            notes=body.notes,
        )
    except ConstraintError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "The fixture's shape is not valid",
            {"constraint": [exc.constraint]},
        ) from exc
    except sqlite3.IntegrityError as exc:
        # Defensive only: every reference this endpoint accepts is validated
        # above, so this is reached only if something referenced vanished
        # between that check and this write.
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "The fixture references something that no longer exists",
            {"detail": [str(exc)]},
        ) from exc
    if body.group_ids:
        await _set_channel_groups(db, channel.id, body.group_ids)
    await _emit_config_changed(bus, "channel_created")
    # A new channel, and any group it joins, may be immediately reachable by
    # a hirer whose page already carries it (§6.7): wait for the rebuild.
    await settle_hirer_permissions(request, "channel_created")
    groups = (await _group_ids_by_channel(db)).get(channel.id, NO_GROUPS)
    has_colour = await _channel_has_colour(db, channel)
    conflicts = await _channel_conflicts(db, channel)
    return _channel_model(channel, groups=groups, has_colour=has_colour, conflicts=conflicts)


@router.put("/lighting/channels/{channel_id}", response_model=ChannelModel)
async def update_channel(
    _: Admin,
    request: Request,
    db: Db,
    bus: Bus,
    channel_id: int,
    body: ChannelUpdate,
    if_unmodified_since_version: Annotated[str | None, Header()] = None,
) -> ChannelModel:
    version = _version_or_422(if_unmodified_since_version)
    current = await _channel_or_404(db, channel_id)
    fields = _provided(body)
    group_ids = fields.pop("group_ids", None)

    shape = _effective_channel_shape(current, fields)
    errors = await _validate_channel_shape(db, **shape)
    if errors:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED, "The fixture's shape is not valid", errors
        )

    try:
        channel = await lighting_crud.update_channel(db, channel_id, version, **fields)
    except NotFoundError as exc:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no lighting channel with that id") from exc
    except ConflictError as exc:
        current_now = await _channel_or_404(db, channel_id)
        current_groups = (await _group_ids_by_channel(db)).get(channel_id, NO_GROUPS)
        current_colour = await _channel_has_colour(db, current_now)
        raise ApiError(
            ErrorCode.CONFLICT,
            "This fixture was changed by someone else since you loaded it",
            {
                "current": _channel_model(
                    current_now, groups=current_groups, has_colour=current_colour
                ).model_dump()
            },
        ) from exc
    except ConstraintError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "The fixture's shape is not valid",
            {"constraint": [exc.constraint]},
        ) from exc
    except sqlite3.IntegrityError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "The fixture references something that no longer exists",
            {"detail": [str(exc)]},
        ) from exc
    if group_ids is not None:
        await _set_channel_groups(db, channel_id, group_ids)
    await _emit_config_changed(bus, "channel_updated")
    # A channel's group membership decides what a hirer's tray reaches (§6.7).
    await settle_hirer_permissions(request, "channel_updated")
    final_groups = (await _group_ids_by_channel(db)).get(channel_id, NO_GROUPS)
    has_colour = await _channel_has_colour(db, channel)
    conflicts = await _channel_conflicts(db, channel)
    return _channel_model(
        channel, groups=final_groups, has_colour=has_colour, conflicts=conflicts
    )


@router.delete("/lighting/channels/{channel_id}", status_code=204, response_class=Response)
async def delete_channel(
    _: Admin, snapshot: PreChangeSnapshot, request: Request, db: Db, bus: Bus, channel_id: int
) -> Response:
    await snapshot(f"delete lighting channel {channel_id}")
    await _channel_or_404(db, channel_id)
    try:
        await lighting_crud.delete_channel(db, channel_id)
    except InUseError as exc:
        raise ApiError(
            ErrorCode.IN_USE,
            "This fixture is named by a saved look and cannot be removed",
            {"references": _reference_list(exc.references)},
        ) from exc
    await _emit_config_changed(bus, "channel_deleted")
    # A deleted channel must not stay writable for a hirer past this answer.
    await settle_hirer_permissions(request, "channel_deleted")
    return Response(status_code=204)


@router.get("/lighting/channels/{channel_id}/references")
async def channel_references(_: Admin, db: Db, channel_id: int) -> dict[str, Any]:
    await _channel_or_404(db, channel_id)
    return {"references": _reference_list(await lighting_crud.snapshot_references(db, channel_id))}


# -- configuration: lighting groups -------------------------------------------------


class GroupModel(BaseModel):
    id: int
    name: str
    colour: str
    sort_order: int
    #: Never scales output and has no fader anywhere; kept so a derived
    #: status can read its members' levels (migration 011).
    indicator_only: bool
    channel_ids: list[int]
    updated_at: str


class GroupsResponse(BaseModel):
    groups: list[GroupModel]


class GroupCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = ModelField(min_length=1, max_length=120)
    channel_ids: list[int] = ModelField(default_factory=list)
    colour: str = "#2E86C1"
    sort_order: int = 0
    indicator_only: bool = False


class GroupUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    colour: str | None = None
    sort_order: int | None = None
    indicator_only: bool | None = None
    channel_ids: list[int] | None = None


def _group_model(group: lighting_crud.LightingGroup, *, channel_ids: list[int]) -> GroupModel:
    return GroupModel(
        id=group.id,
        name=group.name,
        colour=group.colour,
        sort_order=group.sort_order,
        indicator_only=group.indicator_only,
        channel_ids=sorted(channel_ids),
        updated_at=group.updated_at,
    )


async def _group_or_404(db: Database, group_id: int) -> lighting_crud.LightingGroup:
    group = await lighting_crud.get_group(db, group_id)
    if group is None:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no lighting group with that id")
    return group


async def _group_channel_ids(db: Database, group_id: int) -> list[int]:
    return [m.channel_id for m in await lighting_crud.get_group_members(db, group_id)]


@router.get("/lighting/groups", response_model=GroupsResponse)
async def list_groups(_: Staff, db: Db) -> GroupsResponse:
    groups = await lighting_crud.list_groups(db)
    return GroupsResponse(
        groups=[
            _group_model(g, channel_ids=await _group_channel_ids(db, g.id)) for g in groups
        ]
    )


@router.get("/lighting/groups/{group_id}", response_model=GroupModel)
async def get_group(_: Staff, db: Db, group_id: int) -> GroupModel:
    group = await _group_or_404(db, group_id)
    return _group_model(group, channel_ids=await _group_channel_ids(db, group_id))


@router.post("/lighting/groups", response_model=GroupModel, status_code=201)
async def create_group(
    _: Admin, request: Request, db: Db, bus: Bus, body: GroupCreate
) -> GroupModel:
    group = await lighting_crud.create_group(
        db,
        name=body.name,
        colour=body.colour,
        sort_order=body.sort_order,
        indicator_only=body.indicator_only,
    )
    if body.channel_ids:
        await lighting_crud.set_group_members(db, group.id, body.channel_ids)
    await _emit_config_changed(bus, "group_created")
    # A new group placed on an assigned page is immediately reachable (§6.7).
    await settle_hirer_permissions(request, "group_created")
    return _group_model(group, channel_ids=body.channel_ids)


@router.put("/lighting/groups/{group_id}", response_model=GroupModel)
async def update_group(
    _: Admin,
    request: Request,
    db: Db,
    bus: Bus,
    group_id: int,
    body: GroupUpdate,
    if_unmodified_since_version: Annotated[str | None, Header()] = None,
) -> GroupModel:
    version = _version_or_422(if_unmodified_since_version)
    fields = _provided(body)
    channel_ids = fields.pop("channel_ids", None)
    try:
        group = await lighting_crud.update_group(db, group_id, version, **fields)
    except NotFoundError as exc:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no lighting group with that id") from exc
    except lighting_crud.IndicatorOnlyBindingError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "A binding drives this group, so it cannot be indicator-only: a binding forces "
            "the group's level to full, which an indicator-only group does not have. "
            "Delete or re-point the binding first",
            {
                "indicator_only": [
                    f"driven by binding “{r.name}” (rule {r.id})" for r in exc.references
                ]
            },
        ) from exc
    except ConflictError as exc:
        current = await _group_or_404(db, group_id)
        raise ApiError(
            ErrorCode.CONFLICT,
            "This group was changed by someone else since you loaded it",
            {
                "current": _group_model(
                    current, channel_ids=await _group_channel_ids(db, group_id)
                ).model_dump()
            },
        ) from exc
    if channel_ids is not None:
        await lighting_crud.set_group_members(db, group_id, channel_ids)
    await _emit_config_changed(bus, "group_updated")
    # A membership edit changes exactly which fixtures a hirer's tray reaches
    # and which are writable (§6.7, §21.9): a fixture removed here must be
    # refused the instant this answers.
    await settle_hirer_permissions(request, "group_updated")
    return _group_model(group, channel_ids=await _group_channel_ids(db, group_id))


@router.delete("/lighting/groups/{group_id}", status_code=204, response_class=Response)
async def delete_group(
    _: Admin, snapshot: PreChangeSnapshot, request: Request, db: Db, bus: Bus, group_id: int
) -> Response:
    await snapshot(f"delete lighting group {group_id}")
    await _group_or_404(db, group_id)
    try:
        await lighting_crud.delete_group(db, group_id)
    except InUseError as exc:
        raise ApiError(
            ErrorCode.IN_USE,
            "This group is referenced elsewhere and cannot be removed",
            {"references": _reference_list(exc.references)},
        ) from exc
    await _emit_config_changed(bus, "group_deleted")
    # A deleted group's tray and every member reached only through it must
    # stop being writable before this answers.
    await settle_hirer_permissions(request, "group_deleted")
    return Response(status_code=204)


# -- configuration: lighting bars ---------------------------------------------------


class BarModel(BaseModel):
    id: int
    name: str
    sort_order: int
    notes: str | None
    updated_at: str


class BarsResponse(BaseModel):
    bars: list[BarModel]


class BarCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = ModelField(min_length=1, max_length=120)
    sort_order: int = 0
    notes: str | None = None


class BarUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    sort_order: int | None = None
    notes: str | None = None


def _bar_model(bar: lighting_crud.LightingBar) -> BarModel:
    return BarModel(
        id=bar.id,
        name=bar.name,
        sort_order=bar.sort_order,
        notes=bar.notes,
        updated_at=bar.updated_at,
    )


async def _bar_or_404(db: Database, bar_id: int) -> lighting_crud.LightingBar:
    bar = await lighting_crud.get_bar(db, bar_id)
    if bar is None:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no lighting bar with that id")
    return bar


@router.get("/lighting/bars", response_model=BarsResponse)
async def list_bars(_: Staff, db: Db) -> BarsResponse:
    return BarsResponse(bars=[_bar_model(b) for b in await lighting_crud.list_bars(db)])


@router.get("/lighting/bars/{bar_id}", response_model=BarModel)
async def get_bar(_: Staff, db: Db, bar_id: int) -> BarModel:
    return _bar_model(await _bar_or_404(db, bar_id))


@router.post("/lighting/bars", response_model=BarModel, status_code=201)
async def create_bar(_: Admin, db: Db, bus: Bus, body: BarCreate) -> BarModel:
    bar = await lighting_crud.create_bar(
        db, name=body.name, sort_order=body.sort_order, notes=body.notes
    )
    await _emit_config_changed(bus, "bar_created")
    return _bar_model(bar)


@router.put("/lighting/bars/{bar_id}", response_model=BarModel)
async def update_bar(
    _: Admin,
    db: Db,
    bus: Bus,
    bar_id: int,
    body: BarUpdate,
    if_unmodified_since_version: Annotated[str | None, Header()] = None,
) -> BarModel:
    version = _version_or_422(if_unmodified_since_version)
    fields = _provided(body)
    try:
        bar = await lighting_crud.update_bar(db, bar_id, version, **fields)
    except NotFoundError as exc:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no lighting bar with that id") from exc
    except ConflictError as exc:
        current = await _bar_or_404(db, bar_id)
        raise ApiError(
            ErrorCode.CONFLICT,
            "This bar was changed by someone else since you loaded it",
            {"current": _bar_model(current).model_dump()},
        ) from exc
    await _emit_config_changed(bus, "bar_updated")
    return _bar_model(bar)


@router.delete("/lighting/bars/{bar_id}", status_code=204, response_class=Response)
async def delete_bar(
    _: Admin, snapshot: PreChangeSnapshot, db: Db, bus: Bus, bar_id: int
) -> Response:
    await snapshot(f"delete lighting bar {bar_id}")
    await _bar_or_404(db, bar_id)
    await lighting_crud.delete_bar(db, bar_id)  # never blocked — bar_id is SET NULL
    await _emit_config_changed(bus, "bar_deleted")
    return Response(status_code=204)


# -- configuration: colour presets (/lighting/presets) -------------------------------


class PresetModel(BaseModel):
    id: int
    name: str
    r: int
    g: int
    b: int
    w: int
    sort_order: int
    updated_at: str


class PresetsResponse(BaseModel):
    presets: list[PresetModel]


class PresetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = ModelField(min_length=1, max_length=120)
    r: int = ModelField(ge=0, le=255)
    g: int = ModelField(ge=0, le=255)
    b: int = ModelField(ge=0, le=255)
    w: int = ModelField(default=0, ge=0, le=255)
    sort_order: int = 0


class PresetUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    r: int | None = ModelField(default=None, ge=0, le=255)
    g: int | None = ModelField(default=None, ge=0, le=255)
    b: int | None = ModelField(default=None, ge=0, le=255)
    w: int | None = ModelField(default=None, ge=0, le=255)
    sort_order: int | None = None


def _preset_model(preset: lighting_crud.ColourPreset) -> PresetModel:
    return PresetModel(
        id=preset.id,
        name=preset.name,
        r=preset.r,
        g=preset.g,
        b=preset.b,
        w=preset.w,
        sort_order=preset.sort_order,
        updated_at=preset.updated_at,
    )


async def _preset_or_404(db: Database, preset_id: int) -> lighting_crud.ColourPreset:
    preset = await lighting_crud.get_preset(db, preset_id)
    if preset is None:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no colour preset with that id")
    return preset


@router.get("/lighting/presets", response_model=PresetsResponse)
async def list_presets(_: Admin, db: Db) -> PresetsResponse:
    return PresetsResponse(presets=[_preset_model(p) for p in await lighting_crud.list_presets(db)])


@router.get("/lighting/presets/{preset_id}", response_model=PresetModel)
async def get_preset(_: Admin, db: Db, preset_id: int) -> PresetModel:
    return _preset_model(await _preset_or_404(db, preset_id))


@router.post("/lighting/presets", response_model=PresetModel, status_code=201)
async def create_preset(_: Admin, db: Db, bus: Bus, body: PresetCreate) -> PresetModel:
    preset = await lighting_crud.create_preset(
        db, name=body.name, r=body.r, g=body.g, b=body.b, w=body.w, sort_order=body.sort_order
    )
    await _emit_config_changed(bus, "preset_created")
    return _preset_model(preset)


@router.put("/lighting/presets/{preset_id}", response_model=PresetModel)
async def update_preset(
    _: Admin,
    db: Db,
    bus: Bus,
    preset_id: int,
    body: PresetUpdate,
    if_unmodified_since_version: Annotated[str | None, Header()] = None,
) -> PresetModel:
    version = _version_or_422(if_unmodified_since_version)
    fields = _provided(body)
    try:
        preset = await lighting_crud.update_preset(db, preset_id, version, **fields)
    except NotFoundError as exc:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no colour preset with that id") from exc
    except ConflictError as exc:
        current = await _preset_or_404(db, preset_id)
        raise ApiError(
            ErrorCode.CONFLICT,
            "This preset was changed by someone else since you loaded it",
            {"current": _preset_model(current).model_dump()},
        ) from exc
    await _emit_config_changed(bus, "preset_updated")
    return _preset_model(preset)


@router.delete("/lighting/presets/{preset_id}", status_code=204, response_class=Response)
async def delete_preset(
    _: Admin, snapshot: PreChangeSnapshot, db: Db, bus: Bus, preset_id: int
) -> Response:
    await snapshot(f"delete colour preset {preset_id}")
    await _preset_or_404(db, preset_id)
    await lighting_crud.delete_preset(db, preset_id)  # nothing references a preset
    await _emit_config_changed(bus, "preset_deleted")
    return Response(status_code=204)


# -- configuration: fixture profiles (/lighting/profiles) -----------------------------


class ProfileChannelModel(BaseModel):
    offset: int
    role: str
    default: float


class ProfileModel(BaseModel):
    id: int
    manufacturer: str | None
    model: str | None
    name: str
    channel_count: int
    channels: list[ProfileChannelModel]
    updated_at: str


class ProfilesResponse(BaseModel):
    profiles: list[ProfileModel]


class ProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = ModelField(min_length=1, max_length=120)
    channel_count: int = ModelField(ge=1)
    channels: list[dict[str, Any]]
    manufacturer: str | None = None
    model: str | None = None


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    channel_count: int | None = ModelField(default=None, ge=1)
    channels: list[dict[str, Any]] | None = None
    manufacturer: str | None = None
    model: str | None = None


def _profile_model(profile: lighting_crud.FixtureProfile) -> ProfileModel:
    return ProfileModel(
        id=profile.id,
        manufacturer=profile.manufacturer,
        model=profile.model,
        name=profile.name,
        channel_count=profile.channel_count,
        channels=[
            ProfileChannelModel(offset=c.offset, role=c.role, default=c.default)
            for c in profile.channels
        ],
        updated_at=profile.updated_at,
    )


async def _profile_or_404(db: Database, profile_id: int) -> lighting_crud.FixtureProfile:
    profile = await lighting_crud.get_fixture_profile(db, profile_id)
    if profile is None:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no fixture profile with that id")
    return profile


@router.get("/lighting/profiles", response_model=ProfilesResponse)
async def list_profiles(_: Admin, db: Db) -> ProfilesResponse:
    return ProfilesResponse(
        profiles=[_profile_model(p) for p in await lighting_crud.list_fixture_profiles(db)]
    )


@router.get("/lighting/profiles/{profile_id}", response_model=ProfileModel)
async def get_profile(_: Admin, db: Db, profile_id: int) -> ProfileModel:
    return _profile_model(await _profile_or_404(db, profile_id))


@router.post("/lighting/profiles", response_model=ProfileModel, status_code=201)
async def create_profile(_: Admin, db: Db, bus: Bus, body: ProfileCreate) -> ProfileModel:
    try:
        profile = await lighting_crud.create_fixture_profile(
            db,
            name=body.name,
            channel_count=body.channel_count,
            channels=body.channels,
            manufacturer=body.manufacturer,
            model=body.model,
        )
    except ValueError as exc:
        raise ApiError(ErrorCode.VALIDATION_FAILED, str(exc), {"channels": [str(exc)]}) from exc
    await _emit_config_changed(bus, "profile_created")
    return _profile_model(profile)


@router.put("/lighting/profiles/{profile_id}", response_model=ProfileModel)
async def update_profile(
    _: Admin,
    db: Db,
    bus: Bus,
    profile_id: int,
    body: ProfileUpdate,
    if_unmodified_since_version: Annotated[str | None, Header()] = None,
) -> ProfileModel:
    version = _version_or_422(if_unmodified_since_version)
    fields = _provided(body)
    try:
        profile = await lighting_crud.update_fixture_profile(db, profile_id, version, **fields)
    except NotFoundError as exc:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no fixture profile with that id") from exc
    except ConflictError as exc:
        current = await _profile_or_404(db, profile_id)
        raise ApiError(
            ErrorCode.CONFLICT,
            "This profile was changed by someone else since you loaded it",
            {"current": _profile_model(current).model_dump()},
        ) from exc
    except ValueError as exc:
        raise ApiError(ErrorCode.VALIDATION_FAILED, str(exc), {"channels": [str(exc)]}) from exc
    await _emit_config_changed(bus, "profile_updated")
    return _profile_model(profile)


@router.delete("/lighting/profiles/{profile_id}", status_code=204, response_class=Response)
async def delete_profile(
    _: Admin, snapshot: PreChangeSnapshot, db: Db, bus: Bus, profile_id: int
) -> Response:
    await snapshot(f"delete fixture profile {profile_id}")
    await _profile_or_404(db, profile_id)
    try:
        await lighting_crud.delete_fixture_profile(db, profile_id)
    except InUseError as exc:
        raise ApiError(
            ErrorCode.IN_USE,
            "This profile is patched to fixtures and cannot be removed",
            {"references": _reference_list(exc.references)},
        ) from exc
    await _emit_config_changed(bus, "profile_deleted")
    return Response(status_code=204)


# -- patch overlap (§9.1) ------------------------------------------------------------


@router.get("/lighting/patch/conflicts", response_model=PatchConflictsResponse)
async def patch_conflicts(_: Staff, db: Db) -> PatchConflictsResponse:
    """Overlapping DMX addresses. **Warns; never blocks a save** (§9.1).

    A create or update that causes an overlap is saved anyway, and answers
    with the conflicts it now has as part of its own response
    (``ChannelModel.conflicts`` — see :func:`_channel_conflicts`); this
    endpoint is the equivalent full-rig view for the stage plan's own display.
    """
    overlaps = await lighting_crud.find_overlaps(db)
    return PatchConflictsResponse(conflicts=[_overlap_to_conflict(o) for o in overlaps])


# -- the WebSocket write path (§16.8, §21.2) -----------------------------------------


def _channel_write_handler(service: LightingService) -> SetHandler:
    async def handle(request: SetRequest, claims: TokenClaims) -> SetResult:
        channel_id = request.id
        if channel_id is None:
            return SetResult.rejected(ErrorCode.NOT_FOUND)
        if request.value is None:
            # null is a value only for mixer (proskenion/api/ws.py's module
            # docstring); parse_set already refuses it for every other
            # domain, lighting included, before a handler is ever reached.
            return SetResult.rejected(ErrorCode.VALIDATION_FAILED)
        try:
            min_value, max_value = service.fades.range_of(channel_id)
        except UnknownChannelError:
            return SetResult.rejected(ErrorCode.NOT_FOUND)
        try:
            # A fader, key step or tap: the output glides (§21.2, compositor).
            handle_ = service.set_level(channel_id, request.value, glide=True)
        except ChannelLockedError:
            return SetResult.rejected(
                ErrorCode.PERMISSION_DENIED, service.composited_level(channel_id)
            )
        if request.value < min_value or request.value > max_value:
            return SetResult.rejected(ErrorCode.VALUE_OUT_OF_RANGE, handle_.target_level)
        return SetResult.accepted()

    return handle


def _group_write_handler(service: LightingService) -> SetHandler:
    async def handle(request: SetRequest, claims: TokenClaims) -> SetResult:
        group_id = request.id
        if group_id is None:
            return SetResult.rejected(ErrorCode.NOT_FOUND)
        if request.value is None:
            # See _channel_write_handler: null never reaches a non-mixer
            # handler in practice; this is the type-narrowing backstop.
            return SetResult.rejected(ErrorCode.VALIDATION_FAILED)
        try:
            # A group level, 0–100, becomes every member's level (owner
            # decision 2026-09-30); each member is clamped to its own range,
            # which is the fader working, not a value_out_of_range.
            result = service.set_group_level(group_id, request.value, glide=True)
        except UnknownGroupError:
            return SetResult.rejected(ErrorCode.NOT_FOUND)
        except IndicatorOnlyGroupError:
            # No fader offers it; a write here is a stale or hand-made client.
            return SetResult.rejected(ErrorCode.VALIDATION_FAILED)
        except ValueError:
            return SetResult.rejected(ErrorCode.VALIDATION_FAILED)
        if result.refused:
            # A critical scene holds some members; the rest were set (§8.15).
            # The nack carries what the fader now shows so the client settles
            # on it rather than on the value it asked for.
            shown = service.group_level(group_id)
            if shown is None:
                return SetResult.rejected(ErrorCode.PERMISSION_DENIED)
            return SetResult.rejected(ErrorCode.PERMISSION_DENIED, shown)
        return SetResult.accepted()

    return handle


#: ``lighting_bump`` values: the BUMP held (a press, or the refresh of one), or let go.
BUMP_HELD = 1.0
BUMP_RELEASED = 0.0


def _bump_write_handler(service: LightingService) -> SetHandler:
    """A group's BUMP: flash its DMX members to full while held (owner decision 2026-10-01).

    ``{"type": "set", "domain": "lighting_bump", "id": <group>, "value": 1}``
    presses the BUMP, and the client re-sends it every
    :data:`~proskenion.core.dmx.bump.BUMP_REFRESH_S` while it stays held;
    ``"value": 0`` lets go. The hold is the connection's
    (:attr:`SetRequest.connection`): it ends with the socket, when the client
    goes to the background, or when the refreshes stop
    (:mod:`proskenion.core.dmx.bump`). Nothing is written to the level store,
    so there is no authoritative value to carry on a nack.

    Refused presses: ``not_found`` (no such group), ``validation_failed`` (an
    indicator-only group, which has no strip; a group with no DMX member,
    which a bump could not light; a value other than 0 or 1), and
    ``conflict`` while external control suspends stage output (§7.2.7). A
    release is always acknowledged.
    """

    async def handle(request: SetRequest, claims: TokenClaims) -> SetResult:
        group_id = request.id
        if group_id is None or request.connection is None:
            return SetResult.rejected(ErrorCode.NOT_FOUND)
        if request.value not in (BUMP_HELD, BUMP_RELEASED):
            return SetResult.rejected(ErrorCode.VALIDATION_FAILED)
        try:
            service.bump_group(group_id, request.connection, held=request.value == BUMP_HELD)
        except UnknownGroupError:
            return SetResult.rejected(ErrorCode.NOT_FOUND)
        except IndicatorOnlyGroupError:
            return SetResult.rejected(ErrorCode.VALIDATION_FAILED)
        except BumpRefusedError as exc:
            if exc.reason == "external_control":
                return SetResult.rejected(ErrorCode.CONFLICT)
            return SetResult.rejected(ErrorCode.VALIDATION_FAILED)
        return SetResult.accepted()

    return handle


def _master_write_handler(service: LightingService) -> SetHandler:
    async def handle(request: SetRequest, claims: TokenClaims) -> SetResult:
        if request.value is None:
            # See _channel_write_handler: null never reaches a non-mixer
            # handler in practice; this is the type-narrowing backstop.
            return SetResult.rejected(ErrorCode.VALIDATION_FAILED)
        service.set_master(request.value, glide=True)
        return SetResult.accepted()

    return handle


def register_write_handlers(writes: WriteRouter, service: LightingService) -> None:
    """Wire the ``lighting``, ``lighting_group``, ``lighting_bump`` and ``master``
    WS domains (§16.8), and let a closing or backgrounded connection's bumps go.

    Idempotent through :meth:`WriteRouter.handles`: a caller that has already
    registered its own handler for one of these domains (a test exercising
    the router's generic mechanics, say) is left alone rather than colliding.
    """
    handlers: dict[str, Callable[[LightingService], SetHandler]] = {
        "lighting": _channel_write_handler,
        "lighting_group": _group_write_handler,
        "lighting_bump": _bump_write_handler,
        "master": _master_write_handler,
    }
    for domain, factory in handlers.items():
        if not writes.handles(domain):
            writes.register(domain, factory(service))
    writes.on_release(service.release_bumps)


__all__ = ["register_write_handlers", "router"]
