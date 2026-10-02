"""Mixer: control and configuration (spec §16.5, §7.3, §13.5, §22.4).

Exactly the contract fixed in ``docs/plans/phase-4-contracts.md``'s "Phase 4
wave 3" section — the operator Mixer view and the admin configuration
screens are both built against it.

Hirers (§6.7, §16.5, ``docs/plans/phase-5-contracts.md``)
---------------------------------------------------------
A hirer is admitted to exactly three routes here, and every one is then held
to the live ``state.hirer`` snapshot, target by target:

* ``GET /mixer/state`` — filtered exactly as the ``mixer_state`` frame is
  (:func:`~proskenion.core.broadcast.filter_for_hirer`): reachable channels
  only, ``main`` only when Main is reachable. A hirer is sent no desk scenes
  and no last recall: they have no recall route (Q2).
* ``POST /mixer/channels/{id}/level`` — an unreachable channel is
  ``permission_denied`` (with an audit row), whether or not it exists, so the
  answer leaks nothing. A level above the channel's ceiling is applied **at**
  the ceiling and answered ``200`` with the channel object plus
  ``"clamped": true`` and the applied ``db`` (B35: a working limit is not a
  failure). A hirer's answer always carries ``clamped``.
* ``POST /mixer/channels/{id}/mute`` — reach only; a toggle is resolved and
  sent as an absolute mute, as for staff.

Pan, desk-scene recall and test, and every configuration route stay staff or
admin. The socket's ``mixer`` domain applies the same reach check (in
:meth:`~proskenion.core.broadcast.Broadcaster.may_write`) and the same clamp
(in :func:`_mixer_write_handler`), answering a clamp with ``nack``
``value_out_of_range`` carrying the clamped dB (§16.8).

Channel objects (§16.5)
------------------------
``GET /mixer/state`` and every control endpoint below answer with a channel
object shaped by ``channel_kind``: Main carries ``{channel_id, name, db,
muted, origin}`` — MixPad can move Main's fader, so it badges exactly like
an output or input; an output or input additionally carries ``short_name``
and ``stereo``, and an input further carries ``show_pan`` and ``pan``
(``null`` unless ``show_pan`` is set **and** the driver supports pan —
§5.5's capability degradation applied to one field). ``stereo`` is not a
stored column: it is computed per request from the channel's first driver
reference against the connected driver's ``available_refs()`` (a ganged
channel with more than one reference is always stereo), the same way
``unmapped`` and ``updated_at`` are the only stored, read-only fields the
contract lists.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict
from pydantic import Field as ModelField

from proskenion.api.deps import (
    get_bus,
    get_db,
    get_devices,
    get_mixer,
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
from proskenion.core.devices import DeviceManager, DeviceUnavailable
from proskenion.core.drivers.capabilities import ChannelRef, MixerCapabilities
from proskenion.core.drivers.categories import Category
from proskenion.core.drivers.stub_mixer import MixerCapabilityError
from proskenion.core.events import MixerConfigChanged
from proskenion.core.hirer_enforcement import clamp_to_ceiling
from proskenion.core.hirer_permissions import NO_PERMISSIONS, HirerPermissions
from proskenion.core.mixer import desk_channels
from proskenion.core.mixer.service import (
    ChannelLive,
    MixerOffline,
    MixerPanUnsupported,
    MixerService,
    NoMixerConfigured,
    UnknownDeskSceneError,
    UnknownMixerChannelError,
)
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import mixer as mixer_crud
from proskenion.db.crud.base import ConflictError, NotFoundError
from proskenion.db.crud.devices import Device
from proskenion.db.crud.refs import ConstraintError, InUseError

router = APIRouter(tags=["mixer"])

#: The §16.1 optimistic-concurrency header every configuration ``PUT`` carries.
VERSION_HEADER = "If-Unmodified-Since-Version"

# Declared locally rather than imported, matching every other endpoint
# module's own pattern (``proskenion/api/lighting.py``, ``.../hdmi.py``).
Admin = Annotated[TokenClaims, Depends(require_admin)]
Staff = Annotated[TokenClaims, Depends(require_staff)]
#: Staff, or a hirer held to ``state.hirer`` (see the module docstring).
Control = Annotated[TokenClaims, Depends(require_control)]
Db = Annotated[Database, Depends(get_db)]
State = Annotated[StateStore, Depends(get_state)]
Bus = Annotated[EventBus, Depends(get_bus)]
Devices = Annotated[DeviceManager, Depends(get_devices)]
MixerSvc = Annotated[MixerService, Depends(get_mixer)]


def _version_or_422(version: str | None) -> str:
    if not version:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            f"{VERSION_HEADER} is required so a concurrent edit is not overwritten",
            {VERSION_HEADER: ["required"]},
        )
    return version


def _provided(body: BaseModel) -> dict[str, Any]:
    """Only the fields the client actually sent, exactly as
    ``proskenion/api/lighting.py``'s helper of the same name does — so an
    explicit ``null`` is distinguished from a field simply left out (§16.1)."""
    return {name: getattr(body, name) for name in body.model_fields_set}


def _reference_list(references: list[Any]) -> list[dict[str, Any]]:
    return [asdict(r) for r in references]


async def _emit_config_changed(bus: EventBus, reason: str) -> None:
    """Every mixer configuration write ends with this: the mixer service
    reloads its channel index from the database on receipt (§7.3)."""
    bus.emit(MixerConfigChanged(reason=reason))


# -- driver capability lookup (§5.5) -----------------------------------------


async def _driver_capabilities(
    manager: DeviceManager, device_id: int
) -> tuple[MixerCapabilities | None, dict[str, ChannelRef]]:
    """The connected mixer's capabilities and its ``available_refs()``, or
    ``(None, {})`` when the driver cannot presently be resolved — the same
    degradation :func:`proskenion.api.devices.device_capabilities` shows."""
    try:
        driver, _as_connected = await manager.resolve_driver(device_id)
    except DeviceUnavailable:
        return None, {}
    caps = driver.capabilities()
    assert isinstance(caps, MixerCapabilities)
    available_refs = getattr(driver, "available_refs", None)
    available_by_ref = {r.ref: r for r in available_refs()} if available_refs is not None else {}
    return caps, available_by_ref


def _is_stereo(driver_refs: list[str], available_by_ref: Mapping[str, ChannelRef]) -> bool:
    """Whether a channel reads as stereo (§5.5): more than one ganged
    reference always is; a single reference is whatever the driver's own
    table says it is."""
    if len(driver_refs) > 1:
        return True
    if driver_refs:
        info = available_by_ref.get(driver_refs[0])
        if info is not None:
            return info.stereo
    return False


def _channel_object(
    channel: mixer_crud.MixerChannel,
    driver_refs: list[str],
    live: ChannelLive | None,
    caps: MixerCapabilities | None,
    available_by_ref: Mapping[str, ChannelRef],
) -> dict[str, Any]:
    """A channel object exactly as the contract shapes it — see the module
    docstring's "Channel objects" section."""
    db_value = live.db if live is not None else None
    muted = live.muted if live is not None else False
    origin = live.origin if live is not None else None
    pan_value = live.pan if live is not None else None
    if channel.channel_kind == "main":
        return {
            "channel_id": channel.id,
            "name": channel.name,
            "db": db_value,
            "muted": muted,
            "origin": origin,
        }
    stereo = _is_stereo(driver_refs, available_by_ref)
    if channel.channel_kind == "output":
        return {
            "channel_id": channel.id,
            "name": channel.name,
            "short_name": channel.short_name,
            "stereo": stereo,
            "db": db_value,
            "muted": muted,
            "origin": origin,
        }
    # Every other kind (§7.3, §5.5: inputs are the common case; fx_return and
    # dca are reserved vocabulary no shipped driver produces yet) is shown as
    # an input.
    shows_pan = channel.show_pan and caps is not None and caps.supports_pan
    return {
        "channel_id": channel.id,
        "name": channel.name,
        "short_name": channel.short_name,
        "stereo": stereo,
        "show_pan": channel.show_pan,
        "pan": pan_value if shows_pan else None,
        "db": db_value,
        "muted": muted,
        "origin": origin,
    }


async def _channel_response(
    db: Database, manager: DeviceManager, service: MixerService, channel: mixer_crud.MixerChannel
) -> dict[str, Any]:
    refs = [r.driver_ref for r in await mixer_crud.get_channel_refs(db, channel.id)]
    caps, available_by_ref = await _driver_capabilities(manager, channel.device_id)
    live = service.live(channel.id)
    return _channel_object(channel, refs, live, caps, available_by_ref)


async def _channel_or_404(db: Database, channel_id: int) -> mixer_crud.MixerChannel:
    channel = await mixer_crud.get_channel(db, channel_id)
    if channel is None:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no mixer channel with that id")
    return channel


def _mixer_unavailable() -> ApiError:
    return ApiError(ErrorCode.DEVICE_UNAVAILABLE, "The mixer is not available")


def _unsupported(message: str) -> ApiError:
    return ApiError(ErrorCode.VALIDATION_FAILED, message, {"reason": "unsupported"})


# -- GET /mixer/state (§16.5) --------------------------------------------------------


def _hirer_state(body: dict[str, Any], permissions: HirerPermissions) -> dict[str, Any]:
    """``GET /mixer/state`` as a hirer may see it: filtered by the very
    function that filters the ``mixer_state`` frame, so the two cannot drift.

    The channels are keyed by id into a resync-shaped frame, filtered, and
    put back in their original order. Desk scenes and the last recall are
    never in a hirer's frames, so they are empty here too.
    """
    frame: dict[str, Any] = {
        "type": "mixer_state",
        "source": RESYNC_SOURCE,
        "main": body["main"],
        "outputs": {str(o["channel_id"]): o for o in body["outputs"]},
        "inputs": {str(i["channel_id"]): i for i in body["inputs"]},
    }
    seen = filter_for_hirer(frame, permissions) or {}
    outputs = seen.get("outputs") or {}
    inputs = seen.get("inputs") or {}
    return {
        **body,
        "main": seen.get("main"),
        "outputs": [o for o in body["outputs"] if str(o["channel_id"]) in outputs],
        "inputs": [i for i in body["inputs"] if str(i["channel_id"]) in inputs],
        "desk_scenes": [],
        "last_recalled_scene": None,
    }


@router.get("/mixer/state")
async def get_mixer_state(
    claims: Control, db: Db, manager: Devices, service: MixerSvc, state: State
) -> dict[str, Any]:
    body = await _mixer_state(db, manager, service, include_hidden=claims.is_hirer)
    if claims.is_hirer:
        return _hirer_state(body, state.hirer.permissions)
    return body


async def _mixer_state(
    db: Database, manager: DeviceManager, service: MixerService, *, include_hidden: bool
) -> dict[str, Any]:
    """The §16.5 body. ``include_hidden`` keeps channels not ``visible_staff``:
    staff never see those, but a hirer's view is decided by reach alone."""
    device_id = service.device_id
    if device_id is None:
        return {
            "device_id": None,
            "connected": False,
            "capabilities": {
                "scene_recall": False,
                "pan": False,
                "metering": False,
                "metering_reason": None,
            },
            "main": None,
            "outputs": [],
            "inputs": [],
            "desk_scenes": [],
            "last_recalled_scene": None,
        }
    caps, available_by_ref = await _driver_capabilities(manager, device_id)
    rows = await mixer_crud.list_channels_with_refs(db, device_id)
    main: dict[str, Any] | None = None
    outputs: list[dict[str, Any]] = []
    inputs: list[dict[str, Any]] = []
    for row in rows:
        channel = row.channel
        if channel.channel_kind != "main" and not channel.visible_staff and not include_hidden:
            continue
        if channel.unmapped:
            continue  # not controllable until re-mapped (§15.6, §21.21)
        refs = [r.driver_ref for r in row.refs]
        live = service.live(channel.id)
        obj = _channel_object(channel, refs, live, caps, available_by_ref)
        if channel.channel_kind == "main":
            main = obj
        elif channel.channel_kind == "output":
            outputs.append(obj)
        else:
            inputs.append(obj)
    desk_scenes = [
        {"id": s.id, "name": s.name, "is_venue_default": s.is_venue_default}
        for s in await mixer_crud.list_desk_scenes(db, device_id=device_id)
        if s.visible_staff
    ]
    last = service.last_recalled_scene
    return {
        "device_id": device_id,
        "connected": service.connected,
        "capabilities": {
            "scene_recall": caps.supports_scene_recall if caps is not None else False,
            "pan": caps.supports_pan if caps is not None else False,
            "metering": caps.supports_metering if caps is not None else False,
            # The service's own tracked reason, not derived from ``caps``:
            # it distinguishes *why* metering is unavailable, which
            # capabilities() itself does not carry.
            "metering_reason": service.metering_reason,
        },
        "main": main,
        "outputs": outputs,
        "inputs": inputs,
        "desk_scenes": desk_scenes,
        "last_recalled_scene": last,
    }


# -- control: level, mute, pan (§16.5) -----------------------------------------------


class LevelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    db: float | None


class MuteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    muted: bool | None = None
    toggle: bool | None = None


class PanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pan: float = ModelField(ge=-1.0, le=1.0)


async def _require_reach(
    request: Request, claims: TokenClaims, state: StateStore, channel_id: int
) -> HirerPermissions | None:
    """For a hirer, the snapshot the write is held to — or the audited 403 when
    the channel is out of reach. ``None`` for staff, who are not held to one.

    Checked before the channel is looked up, so an id a hirer cannot reach
    answers the same whether or not it exists.
    """
    if not claims.is_hirer:
        return None
    permissions = state.hirer.permissions
    if not permissions.mixer_reachable(channel_id):
        raise await refuse_hirer(
            request, claims, domain="mixer", target_id=channel_id, reason="unreachable"
        )
    return permissions


@router.post("/mixer/channels/{channel_id}/level")
async def set_channel_level(
    claims: Control,
    request: Request,
    db: Db,
    manager: Devices,
    service: MixerSvc,
    state: State,
    channel_id: int,
    body: LevelBody,
) -> dict[str, Any]:
    """``{db}`` — ``null`` is off (§5.5). Success answers the channel object;
    a hirer's is clamped to the ceiling (see the module docstring)."""
    permissions = await _require_reach(request, claims, state, channel_id)
    target, clamped = body.db, False
    if permissions is not None:
        target, clamped = clamp_to_ceiling(body.db, permissions.ceiling_db(channel_id))
    channel = await _channel_or_404(db, channel_id)
    try:
        applied = await service.set_level(channel_id, target)
    except UnknownMixerChannelError as exc:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no mixer channel with that id") from exc
    except (NoMixerConfigured, MixerOffline) as exc:
        raise _mixer_unavailable() from exc
    if target is not None and applied != target:
        raise ApiError(
            ErrorCode.VALUE_OUT_OF_RANGE,
            "The level was outside the fader law's range",
            {"clamped": applied},
        )
    response = await _channel_response(db, manager, service, channel)
    if permissions is not None:
        response["db"] = applied
        response["clamped"] = clamped
    return response


@router.post("/mixer/channels/{channel_id}/mute")
async def set_channel_mute(
    claims: Control,
    request: Request,
    db: Db,
    manager: Devices,
    service: MixerSvc,
    state: State,
    channel_id: int,
    body: MuteBody,
) -> dict[str, Any]:
    """``{muted}`` or ``{toggle}`` — exactly one. A toggle resolves against the
    service's known state and is sent as an absolute mute (§7.3). A hirer
    needs only reach: a mute has no ceiling."""
    await _require_reach(request, claims, state, channel_id)
    if (body.muted is None) == (body.toggle is None):
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "Send exactly one of muted or toggle",
            {"muted": ["exactly one of muted or toggle is required"]},
        )
    channel = await _channel_or_404(db, channel_id)
    try:
        if body.toggle:
            await service.toggle_mute(channel_id)
        else:
            assert body.muted is not None
            await service.set_mute(channel_id, body.muted)
    except UnknownMixerChannelError as exc:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no mixer channel with that id") from exc
    except (NoMixerConfigured, MixerOffline) as exc:
        raise _mixer_unavailable() from exc
    return await _channel_response(db, manager, service, channel)


@router.post("/mixer/channels/{channel_id}/pan")
async def set_channel_pan(
    _: Staff, db: Db, manager: Devices, service: MixerSvc, channel_id: int, body: PanBody
) -> dict[str, Any]:
    """``{pan}``, -1.0 to 1.0. Unsupported (the stub, or a channel without
    ``show_pan``) answers ``validation_failed`` with ``detail.reason =
    "unsupported"`` (§5.5's capability degradation)."""
    channel = await _channel_or_404(db, channel_id)
    try:
        await service.set_pan(channel_id, body.pan)
    except UnknownMixerChannelError as exc:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no mixer channel with that id") from exc
    except MixerPanUnsupported as exc:
        raise _unsupported("This channel does not show pan") from exc
    except MixerCapabilityError as exc:
        raise _unsupported("This mixer has no pan control") from exc
    except ValueError as exc:
        # The driver has no pan for this reference at all (§7.3: pan is
        # input-only on the CQ-20B) — the same "unsupported" shape.
        raise _unsupported("This channel has no pan control") from exc
    except (NoMixerConfigured, MixerOffline) as exc:
        raise _mixer_unavailable() from exc
    return await _channel_response(db, manager, service, channel)


# -- desk scene recall and test (§13.5, §16.5) ---------------------------------------


@router.post("/mixer/desk-scenes/{scene_id}/recall")
async def recall_desk_scene(_: Staff, service: MixerSvc, scene_id: int) -> dict[str, Any]:
    """Success answers once the recall has been **sent**; the resync that
    follows arrives as frames, never inline here (contrast the test
    endpoint below)."""
    try:
        scene = await service.recall_desk_scene(scene_id)
    except UnknownDeskSceneError as exc:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no desk scene with that id") from exc
    except (NoMixerConfigured, MixerOffline) as exc:
        raise _mixer_unavailable() from exc
    except MixerCapabilityError as exc:
        raise _unsupported("This mixer has no scene recall") from exc
    return {"last_recalled_scene": {"id": scene.id, "name": scene.name}}


@router.post("/mixer/desk-scenes/{scene_id}/test")
async def test_desk_scene(_: Admin, service: MixerSvc, scene_id: int) -> dict[str, Any]:
    """The same recall, answered inline once the resync completes — the
    CQ-20B driver's ``recall_scene`` only returns after its own resync."""
    try:
        await service.recall_desk_scene(scene_id)
    except UnknownDeskSceneError as exc:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no desk scene with that id") from exc
    except (NoMixerConfigured, MixerOffline) as exc:
        raise _mixer_unavailable() from exc
    except MixerCapabilityError as exc:
        raise _unsupported("This mixer has no scene recall") from exc
    return {"sent": True, "resynced": True}


# -- configuration: channels (§15.6, §16.1) ------------------------------------------


class MixerChannelModel(BaseModel):
    id: int
    device_id: int
    channel_kind: str
    name: str
    short_name: str | None
    notes: str | None
    driver_refs: list[str]
    visible_staff: bool
    hirer_max_db: float | None
    show_pan: bool
    tracked: bool
    sort_order: int
    unmapped: bool
    updated_at: str


class MixerChannelsResponse(BaseModel):
    channels: list[MixerChannelModel]


class MixerChannelCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: int
    channel_kind: str = "input"
    name: str = ModelField(min_length=1, max_length=120)
    short_name: str | None = None
    notes: str | None = None
    driver_refs: list[str] = ModelField(default_factory=list)
    visible_staff: bool = True
    hirer_max_db: float | None = None
    show_pan: bool = False
    tracked: bool = True
    sort_order: int = 0


class MixerChannelUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_kind: str | None = None
    name: str | None = None
    short_name: str | None = None
    notes: str | None = None
    driver_refs: list[str] | None = None
    visible_staff: bool | None = None
    hirer_max_db: float | None = None
    show_pan: bool | None = None
    tracked: bool | None = None
    sort_order: int | None = None


async def _channel_config_model(
    db: Database, channel: mixer_crud.MixerChannel
) -> MixerChannelModel:
    refs = [r.driver_ref for r in await mixer_crud.get_channel_refs(db, channel.id)]
    return MixerChannelModel(
        id=channel.id,
        device_id=channel.device_id,
        channel_kind=channel.channel_kind,
        name=channel.name,
        short_name=channel.short_name,
        notes=channel.notes,
        driver_refs=refs,
        visible_staff=channel.visible_staff,
        hirer_max_db=channel.hirer_max_db,
        show_pan=channel.show_pan,
        tracked=channel.tracked,
        sort_order=channel.sort_order,
        unmapped=channel.unmapped,
        updated_at=channel.updated_at,
    )


async def _validate_driver_refs(
    manager: DeviceManager, device_id: int, driver_refs: list[str]
) -> list[str]:
    """The references in ``driver_refs`` the connected driver does not know,
    or ``[]`` when the driver cannot presently be resolved to check against —
    a configuration is still accepted while the device is unreachable, the
    same tolerance ``proskenion/api/devices.py``'s own validation shows."""
    try:
        driver, _as_connected = await manager.resolve_driver(device_id)
    except DeviceUnavailable:
        return []
    available_refs = getattr(driver, "available_refs", None)
    if available_refs is None:
        return []
    available = {r.ref for r in available_refs()}
    return [ref for ref in driver_refs if ref not in available]


def _unknown_kind_error(kind: str) -> ApiError:
    allowed = ", ".join(sorted(mixer_crud.CHANNEL_KINDS))
    return ApiError(
        ErrorCode.VALIDATION_FAILED,
        "Unknown channel kind",
        {"channel_kind": [f"{kind!r} is not known; must be one of {allowed}"]},
    )


def _unknown_refs_error(refs: list[str]) -> ApiError:
    return ApiError(
        ErrorCode.VALIDATION_FAILED,
        "One or more driver references are not known to this device",
        {"driver_refs": [f"unknown reference {ref!r}" for ref in refs]},
    )


@router.get("/mixer/channels", response_model=MixerChannelsResponse)
async def list_mixer_channels(_: Admin, db: Db) -> MixerChannelsResponse:
    channels = await mixer_crud.list_channels(db)
    return MixerChannelsResponse(
        channels=[await _channel_config_model(db, c) for c in channels]
    )


@router.get("/mixer/channels/{channel_id}", response_model=MixerChannelModel)
async def get_mixer_channel(_: Admin, db: Db, channel_id: int) -> MixerChannelModel:
    channel = await _channel_or_404(db, channel_id)
    return await _channel_config_model(db, channel)


@router.post("/mixer/channels", response_model=MixerChannelModel, status_code=201)
async def create_mixer_channel(
    _: Admin, db: Db, bus: Bus, manager: Devices, body: MixerChannelCreate
) -> MixerChannelModel:
    if body.channel_kind not in mixer_crud.CHANNEL_KINDS:
        raise _unknown_kind_error(body.channel_kind)
    invalid = await _validate_driver_refs(manager, body.device_id, body.driver_refs)
    if invalid:
        raise _unknown_refs_error(invalid)
    try:
        channel = await mixer_crud.create_channel(
            db,
            device_id=body.device_id,
            channel_kind=body.channel_kind,
            name=body.name,
            short_name=body.short_name,
            notes=body.notes,
            visible_staff=body.visible_staff,
            hirer_max_db=body.hirer_max_db,
            show_pan=body.show_pan,
            tracked=body.tracked,
            sort_order=body.sort_order,
        )
    except ConstraintError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "This device already has a Main channel",
            {"constraint": [exc.constraint]},
        ) from exc
    except sqlite3.IntegrityError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "The channel references something that no longer exists",
            {"detail": [str(exc)]},
        ) from exc
    if body.driver_refs:
        await mixer_crud.set_channel_refs(db, channel.id, body.driver_refs)
    await _emit_config_changed(bus, "channel_created")
    return await _channel_config_model(db, channel)


@router.put("/mixer/channels/{channel_id}", response_model=MixerChannelModel)
async def update_mixer_channel(
    _: Admin,
    snapshot: PreChangeSnapshot,
    request: Request,
    db: Db,
    bus: Bus,
    manager: Devices,
    channel_id: int,
    body: MixerChannelUpdate,
    if_unmodified_since_version: Annotated[str | None, Header()] = None,
) -> MixerChannelModel:
    version = _version_or_422(if_unmodified_since_version)
    current = await _channel_or_404(db, channel_id)
    fields = _provided(body)
    driver_refs = fields.pop("driver_refs", None)
    if "channel_kind" in fields and fields["channel_kind"] not in mixer_crud.CHANNEL_KINDS:
        raise _unknown_kind_error(str(fields["channel_kind"]))
    if driver_refs is not None:
        invalid = await _validate_driver_refs(manager, current.device_id, driver_refs)
        if invalid:
            raise _unknown_refs_error(invalid)
        if driver_refs:
            # Choosing references for a channel is re-mapping it (§21.21).
            fields["unmapped"] = False
    if driver_refs is not None:
        # The channel's desk references are replaced wholesale: a snapshot first,
        # unless the same references are sent back in the same order.
        await snapshot.before_replacing(
            f"mixer channel {channel_id}'s desk references",
            [r.driver_ref for r in await mixer_crud.get_channel_refs(db, channel_id)],
            driver_refs,
        )
    try:
        channel = await mixer_crud.update_channel(db, channel_id, version, **fields)
    except NotFoundError as exc:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no mixer channel with that id") from exc
    except ConflictError as exc:
        current_now = await _channel_or_404(db, channel_id)
        raise ApiError(
            ErrorCode.CONFLICT,
            "This channel was changed by someone else since you loaded it",
            {"current": (await _channel_config_model(db, current_now)).model_dump()},
        ) from exc
    except ConstraintError as exc:
        reason = "main_immutable" if "main_immutable" in exc.constraint else exc.constraint
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "The Main channel's kind cannot be changed",
            {"reason": reason},
        ) from exc
    if driver_refs is not None:
        await mixer_crud.set_channel_refs(db, channel_id, driver_refs)
    await _emit_config_changed(bus, "channel_updated")
    # A channel's kind and its ceiling decide what a hirer reaches, and how far.
    await settle_hirer_permissions(request, "channel_updated")
    return await _channel_config_model(db, channel)


@router.delete("/mixer/channels/{channel_id}", status_code=204, response_class=Response)
async def delete_mixer_channel(
    _: Admin, snapshot: PreChangeSnapshot, request: Request, db: Db, bus: Bus, channel_id: int
) -> Response:
    await snapshot(f"delete mixer channel {channel_id}")
    await _channel_or_404(db, channel_id)
    try:
        await mixer_crud.delete_channel(db, channel_id)
    except ConstraintError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "The Main channel cannot be deleted",
            {"reason": "main_immutable"},
        ) from exc
    except InUseError as exc:
        raise ApiError(
            ErrorCode.IN_USE,
            "This channel is referenced by a scene and cannot be removed",
            {"references": _reference_list(exc.references)},
        ) from exc
    await _emit_config_changed(bus, "channel_deleted")
    # A channel's kind and its ceiling decide what a hirer reaches, and how far.
    await settle_hirer_permissions(request, "channel_deleted")
    return Response(status_code=204)


# -- configuration: desk channels no channel covers (§7.3, §21.21) -----------------


class MissingChannelModel(BaseModel):
    ref: str
    label: str
    kind: str
    stereo: bool


class MissingChannelsResponse(BaseModel):
    device_id: int
    missing: list[MissingChannelModel]


class AddedChannelsResponse(BaseModel):
    device_id: int
    created: list[MixerChannelModel]


async def _mixer_device_or_404(db: Database, manager: DeviceManager, device_id: int) -> Device:
    """The mixer device, once its driver can be resolved to say what the desk has."""
    device = await devices_crud.get(db, device_id)
    if device is None or device.category != Category.MIXER.value:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no mixer with that id")
    try:
        await manager.resolve_driver(device_id)
    except DeviceUnavailable as exc:
        raise _mixer_unavailable() from exc
    return device


@router.get("/mixer/devices/{device_id}/missing-channels", response_model=MissingChannelsResponse)
async def list_missing_channels(
    _: Admin, db: Db, manager: Devices, device_id: int
) -> MissingChannelsResponse:
    """The desk channels no channel on this mixer covers, in the driver's order."""
    device = await _mixer_device_or_404(db, manager, device_id)
    missing = await desk_channels.missing_channels(db, manager, device)
    return MissingChannelsResponse(
        device_id=device_id,
        missing=[
            MissingChannelModel(
                ref=d.ref.ref, label=d.ref.label, kind=d.ref.kind, stereo=d.ref.stereo
            )
            for d in missing
        ],
    )


@router.post("/mixer/devices/{device_id}/missing-channels", response_model=AddedChannelsResponse)
async def add_missing_channels(
    _: Admin, db: Db, bus: Bus, manager: Devices, device_id: int
) -> AddedChannelsResponse:
    """Add a channel for every desk channel no channel covers.

    Adds only: an existing channel is never renamed, reordered, re-pointed or
    deleted, so this takes no pre-change snapshot. Run again, it adds nothing.
    """
    device = await _mixer_device_or_404(db, manager, device_id)
    created = await desk_channels.add_missing_channels(db, manager, device)
    if created:
        await _emit_config_changed(bus, "desk_channels_created")
    return AddedChannelsResponse(
        device_id=device_id,
        created=[await _channel_config_model(db, channel) for channel in created],
    )


# -- configuration: desk scenes (§13.5, §15.6, §16.1) --------------------------------


class MixerDeskSceneModel(BaseModel):
    id: int
    device_id: int
    scene_ref: str
    name: str
    description: str | None
    notes: str | None
    is_venue_default: bool
    visible_staff: bool
    sort_order: int
    updated_at: str


class MixerDeskScenesResponse(BaseModel):
    desk_scenes: list[MixerDeskSceneModel]


class MixerDeskSceneCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: int
    scene_ref: str = ModelField(min_length=1)
    name: str = ModelField(min_length=1, max_length=120)
    description: str | None = None
    notes: str | None = None
    is_venue_default: bool = False
    visible_staff: bool = True
    sort_order: int = 0


class MixerDeskSceneUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scene_ref: str | None = None
    name: str | None = None
    description: str | None = None
    notes: str | None = None
    is_venue_default: bool | None = None
    visible_staff: bool | None = None
    sort_order: int | None = None


def _desk_scene_model(scene: mixer_crud.MixerDeskScene) -> MixerDeskSceneModel:
    return MixerDeskSceneModel(
        id=scene.id,
        device_id=scene.device_id,
        scene_ref=scene.scene_ref,
        name=scene.name,
        description=scene.description,
        notes=scene.notes,
        is_venue_default=scene.is_venue_default,
        visible_staff=scene.visible_staff,
        sort_order=scene.sort_order,
        updated_at=scene.updated_at,
    )


async def _desk_scene_or_404(db: Database, scene_id: int) -> mixer_crud.MixerDeskScene:
    scene = await mixer_crud.get_desk_scene(db, scene_id)
    if scene is None:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no desk scene with that id")
    return scene


@router.get("/mixer/desk-scenes", response_model=MixerDeskScenesResponse)
async def list_mixer_desk_scenes(_: Admin, db: Db) -> MixerDeskScenesResponse:
    scenes = await mixer_crud.list_desk_scenes(db)
    return MixerDeskScenesResponse(desk_scenes=[_desk_scene_model(s) for s in scenes])


@router.get("/mixer/desk-scenes/{scene_id}", response_model=MixerDeskSceneModel)
async def get_mixer_desk_scene(_: Admin, db: Db, scene_id: int) -> MixerDeskSceneModel:
    return _desk_scene_model(await _desk_scene_or_404(db, scene_id))


@router.post("/mixer/desk-scenes", response_model=MixerDeskSceneModel, status_code=201)
async def create_mixer_desk_scene(
    _: Admin, db: Db, bus: Bus, body: MixerDeskSceneCreate
) -> MixerDeskSceneModel:
    try:
        scene = await mixer_crud.create_desk_scene(
            db,
            device_id=body.device_id,
            scene_ref=body.scene_ref,
            name=body.name,
            description=body.description,
            notes=body.notes,
            is_venue_default=body.is_venue_default,
            visible_staff=body.visible_staff,
            sort_order=body.sort_order,
        )
    except ConstraintError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "This device already has a desk scene with that reference",
            {"constraint": [exc.constraint]},
        ) from exc
    await _emit_config_changed(bus, "desk_scene_created")
    return _desk_scene_model(scene)


@router.put("/mixer/desk-scenes/{scene_id}", response_model=MixerDeskSceneModel)
async def update_mixer_desk_scene(
    _: Admin,
    db: Db,
    bus: Bus,
    scene_id: int,
    body: MixerDeskSceneUpdate,
    if_unmodified_since_version: Annotated[str | None, Header()] = None,
) -> MixerDeskSceneModel:
    version = _version_or_422(if_unmodified_since_version)
    fields = _provided(body)
    try:
        scene = await mixer_crud.update_desk_scene(db, scene_id, version, **fields)
    except NotFoundError as exc:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no desk scene with that id") from exc
    except ConflictError as exc:
        current = await _desk_scene_or_404(db, scene_id)
        raise ApiError(
            ErrorCode.CONFLICT,
            "This desk scene was changed by someone else since you loaded it",
            {"current": _desk_scene_model(current).model_dump()},
        ) from exc
    except ConstraintError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "This device already has a desk scene with that reference",
            {"constraint": [exc.constraint]},
        ) from exc
    await _emit_config_changed(bus, "desk_scene_updated")
    return _desk_scene_model(scene)


@router.delete("/mixer/desk-scenes/{scene_id}", status_code=204, response_class=Response)
async def delete_mixer_desk_scene(
    _: Admin, snapshot: PreChangeSnapshot, db: Db, bus: Bus, scene_id: int
) -> Response:
    await snapshot(f"delete desk scene {scene_id}")
    await _desk_scene_or_404(db, scene_id)
    try:
        await mixer_crud.delete_desk_scene(db, scene_id)
    except InUseError as exc:
        raise ApiError(
            ErrorCode.IN_USE,
            "This desk scene is referenced by a scene and cannot be removed",
            {"references": _reference_list(exc.references)},
        ) from exc
    await _emit_config_changed(bus, "desk_scene_deleted")
    return Response(status_code=204)


# -- the WebSocket write path (§16.8, §21.2) -----------------------------------------


PermissionsSource = Callable[[], HirerPermissions]
"""Where the socket handler reads the live ``state.hirer`` snapshot from."""


def _mixer_write_handler(service: MixerService, permissions: PermissionsSource) -> SetHandler:
    """The ``mixer`` domain: a channel level in dB (``None`` is off).

    A hirer's value is clamped to the channel's ceiling before it is sent,
    and a clamped write is answered ``nack`` ``value_out_of_range`` with the
    clamped dB — applied, not refused (§16.8, B35). Reach was checked before
    this handler ran (:meth:`~proskenion.core.broadcast.Broadcaster.may_write`)
    and is checked again here against the same snapshot, so a handler reached
    any other way still refuses an unreachable channel.
    """

    async def handle(request: SetRequest, claims: TokenClaims) -> SetResult:
        channel_id = request.id
        if channel_id is None:
            return SetResult.rejected(ErrorCode.NOT_FOUND)
        target, clamped = request.value, False
        if claims.is_hirer:
            snapshot = permissions()
            if not snapshot.mixer_reachable(channel_id):
                return SetResult.rejected(ErrorCode.PERMISSION_DENIED)
            target, clamped = clamp_to_ceiling(request.value, snapshot.ceiling_db(channel_id))
        try:
            applied = await service.set_level(channel_id, target)
        except UnknownMixerChannelError:
            return SetResult.rejected(ErrorCode.NOT_FOUND)
        except (NoMixerConfigured, MixerOffline):
            live = service.live(channel_id)
            return SetResult.rejected(
                ErrorCode.DEVICE_UNAVAILABLE, live.db if live is not None else None
            )
        if clamped or (target is not None and applied != target):
            return SetResult.rejected(ErrorCode.VALUE_OUT_OF_RANGE, applied)
        return SetResult.accepted()

    return handle


def register_write_handlers(
    writes: WriteRouter,
    service: MixerService,
    permissions: PermissionsSource = lambda: NO_PERMISSIONS,
) -> None:
    """Wire the ``mixer`` WS domain (§16.8): ``ack``/``nack`` with the
    authoritative dB, registered exactly as
    :func:`proskenion.api.lighting.register_write_handlers` registers
    lighting's. Idempotent through :meth:`WriteRouter.handles`.
    ``permissions`` reads the live ``state.hirer`` snapshot; the default
    reaches nothing, so a hirer write without it is refused.
    """
    if not writes.handles("mixer"):
        writes.register("mixer", _mixer_write_handler(service, permissions))


__all__ = ["register_write_handlers", "router"]
