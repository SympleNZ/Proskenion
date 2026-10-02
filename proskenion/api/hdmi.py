"""HDMI matrix: state, source selection and configuration CRUD (spec §16.5, §16.8, §7.5).

Implements ``docs/plans/phase-3-contracts.md``'s HDMI section exactly — the
operator video view and the admin configuration screen are both built
against it, so a change here needs the coordinator's agreement before it
diverges from that document.

``GET /hdmi/state`` and the source-change response both resolve a
destination's per-output source live, from ``state.hdmi.routing`` (the
matrix's own last-reported state) plus configuration, through the same
:func:`~proskenion.core.video.resolve_destination_routing` the video service
uses to populate ``state.hdmi.destinations`` — one divergence rule, not two.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Response
from pydantic import BaseModel, ConfigDict
from pydantic import Field as ModelField

from proskenion.api.deps import (
    get_bus,
    get_db,
    get_devices,
    get_state,
    get_video,
    require_admin,
    require_staff,
)
from proskenion.api.devices import VERSION_HEADER
from proskenion.api.errors import ApiError, ErrorCode
from proskenion.api.snapshots import PreChangeSnapshot
from proskenion.core.auth import TokenClaims
from proskenion.core.bus import EventBus
from proskenion.core.devices import DeviceManager, DeviceUnavailable
from proskenion.core.drivers.capabilities import MatrixCapabilities
from proskenion.core.events import VideoConfigChanged
from proskenion.core.state import StateStore
from proskenion.core.video import (
    MatrixOfflineError,
    RouteNotConfirmedError,
    UnknownDestinationError,
    UnknownInputError,
    VideoService,
    resolve_destination_routing,
)
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import video as video_crud
from proskenion.db.crud.base import ConflictError, NotFoundError
from proskenion.db.crud.refs import ConstraintError, InUseError, Reference

router = APIRouter(prefix="/hdmi", tags=["hdmi"])

Admin = Annotated[TokenClaims, Depends(require_admin)]
Staff = Annotated[TokenClaims, Depends(require_staff)]
Db = Annotated[Database, Depends(get_db)]
Bus = Annotated[EventBus, Depends(get_bus)]
State = Annotated[StateStore, Depends(get_state)]
Devices = Annotated[DeviceManager, Depends(get_devices)]
Video = Annotated[VideoService, Depends(get_video)]


def _version_or_422(version: str | None) -> str:
    if not version:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            f"{VERSION_HEADER} is required so a concurrent edit is not overwritten",
            {VERSION_HEADER: ["required"]},
        )
    return version


def _reference_list(references: list[Reference]) -> list[dict[str, Any]]:
    return [{"entity": r.entity, "id": r.id, "name": r.name} for r in references]


def _provided(body: BaseModel) -> dict[str, Any]:
    """Only the fields the client actually sent — an explicit ``null`` (clearing
    ``default_input_id``, say) is distinguished from a field simply left out (§16.1)."""
    return {name: getattr(body, name) for name in body.model_fields_set}


async def _emit_config_changed(bus: EventBus, reason: str) -> None:
    """Every destination or output write ends with this: the video service
    recomputes ``state.hdmi`` from the matrix's known routing on receipt, so
    a destination or output configured after the matrix attached does not
    wait for the next routing change to appear (see ``core/video.py``)."""
    bus.emit(VideoConfigChanged(reason=reason))


# -- GET /hdmi/state (§16.5, §16.8) ------------------------------------------------


class HdmiOutputModel(BaseModel):
    id: int
    name: str
    input_id: int | None


class HdmiDestinationModel(BaseModel):
    id: int
    name: str
    input_id: int | None
    diverged: bool
    default_input_id: int | None
    outputs: list[HdmiOutputModel]


class HdmiInputModel(BaseModel):
    id: int
    name: str
    driver_ref: str


class HdmiStateResponse(BaseModel):
    device_id: int | None
    supports_atomic_route: bool
    destinations: list[HdmiDestinationModel]
    inputs: list[HdmiInputModel]


def _current_routing(state: StateStore) -> dict[str, str]:
    """``state.hdmi.routing`` — the matrix's own last-reported state (§7.5)."""
    raw = state.hdmi.get("routing")
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


async def _supports_atomic_route(devices: DeviceManager, device_id: int) -> bool:
    try:
        report = await devices.capabilities(device_id)
    except DeviceUnavailable:
        return False
    caps = report.capabilities
    return isinstance(caps, MatrixCapabilities) and caps.supports_atomic_route


async def _destination_model(
    db: Database, destination: video_crud.VideoDestination, *, routing: dict[str, str]
) -> HdmiDestinationModel:
    outputs = await video_crud.list_outputs(db, device_id=destination.device_id)
    inputs = await video_crud.list_inputs(db, device_id=destination.device_id)
    outputs_by_id = {o.id: o for o in outputs}
    inputs_by_ref = {i.driver_ref: i for i in inputs}
    dest_outputs = await video_crud.get_destination_outputs(db, destination.id)
    resolved = resolve_destination_routing(dest_outputs, outputs_by_id, routing, inputs_by_ref)
    output_models = [
        HdmiOutputModel(
            id=do.output_id,
            name=outputs_by_id[do.output_id].name,
            input_id=resolved.output_input_ids.get(do.output_id),
        )
        for do in dest_outputs
        if do.output_id in outputs_by_id
    ]
    return HdmiDestinationModel(
        id=destination.id,
        name=destination.name,
        input_id=resolved.input_id,
        diverged=resolved.diverged,
        default_input_id=destination.default_input_id,
        outputs=output_models,
    )


async def _hdmi_state(db: Database, state: StateStore, devices: DeviceManager) -> HdmiStateResponse:
    rows = await devices_crud.list_all(db, category="video_matrix")
    if not rows:
        return HdmiStateResponse(
            device_id=None, supports_atomic_route=False, destinations=[], inputs=[]
        )
    device = rows[0]
    routing = _current_routing(state)
    destinations = await video_crud.list_destinations(db, device_id=device.id)
    inputs = await video_crud.list_inputs(db, device_id=device.id)
    destination_models = [
        await _destination_model(db, d, routing=routing) for d in destinations
    ]
    return HdmiStateResponse(
        device_id=device.id,
        supports_atomic_route=await _supports_atomic_route(devices, device.id),
        destinations=destination_models,
        inputs=[
            HdmiInputModel(id=i.id, name=i.name, driver_ref=i.driver_ref) for i in inputs
        ],
    )


@router.get("/state", response_model=HdmiStateResponse)
async def get_hdmi_state(_: Staff, db: Db, state: State, devices: Devices) -> HdmiStateResponse:
    return await _hdmi_state(db, state, devices)


# -- POST /hdmi/destinations/{id}/source ------------------------------------------


class SetSourceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_id: int


@router.post("/destinations/{destination_id}/source", response_model=HdmiDestinationModel)
async def set_destination_source(
    _: Staff, db: Db, state: State, video: Video, destination_id: int, body: SetSourceBody
) -> HdmiDestinationModel:
    try:
        await video.set_source(destination_id, body.input_id)
    except UnknownDestinationError as exc:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no HDMI destination with that id") from exc
    except UnknownInputError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "That input does not belong to this destination's matrix",
            {"input_id": ["unknown"]},
        ) from exc
    except MatrixOfflineError as exc:
        raise ApiError(
            ErrorCode.DEVICE_UNAVAILABLE, "The HDMI matrix is not available"
        ) from exc
    except RouteNotConfirmedError as exc:
        raise ApiError(
            ErrorCode.DEVICE_UNAVAILABLE,
            "The switch was sent but the matrix did not confirm it",
            {"reason": "route_not_confirmed"},
        ) from exc
    destination = await video_crud.get_destination(db, destination_id)
    assert destination is not None  # set_source already proved it exists
    return await _destination_model(db, destination, routing=_current_routing(state))


# -- configuration: matrix inputs (admin only, §16.1) ------------------------------


class MatrixInputModel(BaseModel):
    id: int
    device_id: int
    driver_ref: str
    name: str
    description: str | None
    sort_order: int
    updated_at: str


class MatrixInputsResponse(BaseModel):
    inputs: list[MatrixInputModel]


class MatrixInputCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: int
    driver_ref: str = ModelField(min_length=1)
    name: str = ModelField(min_length=1, max_length=120)
    description: str | None = None
    sort_order: int = 0


class MatrixInputUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: int | None = None
    driver_ref: str | None = None
    name: str | None = None
    description: str | None = None
    sort_order: int | None = None


def _input_model(row: video_crud.MatrixInput) -> MatrixInputModel:
    return MatrixInputModel(
        id=row.id,
        device_id=row.device_id,
        driver_ref=row.driver_ref,
        name=row.name,
        description=row.description,
        sort_order=row.sort_order,
        updated_at=row.updated_at,
    )


async def _input_or_404(db: Database, input_id: int) -> video_crud.MatrixInput:
    row = await video_crud.get_input(db, input_id)
    if row is None:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no HDMI input with that id")
    return row


async def _device_or_422(db: Database, device_id: int) -> dict[str, list[str]]:
    if await devices_crud.get(db, device_id) is None:
        return {"device_id": ["no device with that id"]}
    return {}


@router.get("/inputs", response_model=MatrixInputsResponse)
async def list_matrix_inputs(_: Admin, db: Db) -> MatrixInputsResponse:
    return MatrixInputsResponse(inputs=[_input_model(r) for r in await video_crud.list_inputs(db)])


@router.get("/inputs/{input_id}", response_model=MatrixInputModel)
async def get_matrix_input(_: Admin, db: Db, input_id: int) -> MatrixInputModel:
    return _input_model(await _input_or_404(db, input_id))


@router.post("/inputs", response_model=MatrixInputModel, status_code=201)
async def create_matrix_input(_: Admin, db: Db, body: MatrixInputCreate) -> MatrixInputModel:
    errors = await _device_or_422(db, body.device_id)
    if errors:
        raise ApiError(ErrorCode.VALIDATION_FAILED, "The input's device does not exist", errors)
    try:
        row = await video_crud.create_input(
            db,
            device_id=body.device_id,
            driver_ref=body.driver_ref,
            name=body.name,
            description=body.description,
            sort_order=body.sort_order,
        )
    except ConstraintError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "That driver reference is already used on this device",
            {"driver_ref": [exc.constraint]},
        ) from exc
    return _input_model(row)


@router.put("/inputs/{input_id}", response_model=MatrixInputModel)
async def update_matrix_input(
    _: Admin,
    db: Db,
    input_id: int,
    body: MatrixInputUpdate,
    if_unmodified_since_version: Annotated[str | None, Header()] = None,
) -> MatrixInputModel:
    version = _version_or_422(if_unmodified_since_version)
    fields = _provided(body)
    if "device_id" in fields:
        errors = await _device_or_422(db, fields["device_id"])
        if errors:
            raise ApiError(ErrorCode.VALIDATION_FAILED, "The input's device does not exist", errors)
    try:
        row = await video_crud.update_input(db, input_id, version, **fields)
    except NotFoundError as exc:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no HDMI input with that id") from exc
    except ConflictError as exc:
        current = await _input_or_404(db, input_id)
        raise ApiError(
            ErrorCode.CONFLICT,
            "This input was changed by someone else since you loaded it",
            {"current": _input_model(current).model_dump()},
        ) from exc
    except ConstraintError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "That driver reference is already used on this device",
            {"driver_ref": [exc.constraint]},
        ) from exc
    return _input_model(row)


@router.delete("/inputs/{input_id}", status_code=204, response_class=Response)
async def delete_matrix_input(
    _: Admin, snapshot: PreChangeSnapshot, db: Db, input_id: int
) -> Response:
    await snapshot(f"delete matrix input {input_id}")
    await _input_or_404(db, input_id)
    try:
        await video_crud.delete_input(db, input_id)
    except InUseError as exc:
        raise ApiError(
            ErrorCode.IN_USE,
            "This input is used by a saved scene and cannot be removed",
            {"references": _reference_list(exc.references)},
        ) from exc
    return Response(status_code=204)


# -- configuration: matrix outputs (admin only, §16.1) ------------------------------


class MatrixOutputModel(BaseModel):
    id: int
    device_id: int
    driver_ref: str
    name: str
    description: str | None
    sort_order: int
    updated_at: str


class MatrixOutputsResponse(BaseModel):
    outputs: list[MatrixOutputModel]


class MatrixOutputCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: int
    driver_ref: str = ModelField(min_length=1)
    name: str = ModelField(min_length=1, max_length=120)
    description: str | None = None
    sort_order: int = 0


class MatrixOutputUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: int | None = None
    driver_ref: str | None = None
    name: str | None = None
    description: str | None = None
    sort_order: int | None = None


def _output_model(row: video_crud.MatrixOutput) -> MatrixOutputModel:
    return MatrixOutputModel(
        id=row.id,
        device_id=row.device_id,
        driver_ref=row.driver_ref,
        name=row.name,
        description=row.description,
        sort_order=row.sort_order,
        updated_at=row.updated_at,
    )


async def _output_or_404(db: Database, output_id: int) -> video_crud.MatrixOutput:
    row = await video_crud.get_output(db, output_id)
    if row is None:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no HDMI output with that id")
    return row


@router.get("/outputs", response_model=MatrixOutputsResponse)
async def list_matrix_outputs(_: Admin, db: Db) -> MatrixOutputsResponse:
    return MatrixOutputsResponse(
        outputs=[_output_model(r) for r in await video_crud.list_outputs(db)]
    )


@router.get("/outputs/{output_id}", response_model=MatrixOutputModel)
async def get_matrix_output(_: Admin, db: Db, output_id: int) -> MatrixOutputModel:
    return _output_model(await _output_or_404(db, output_id))


@router.post("/outputs", response_model=MatrixOutputModel, status_code=201)
async def create_matrix_output(
    _: Admin, db: Db, bus: Bus, body: MatrixOutputCreate
) -> MatrixOutputModel:
    errors = await _device_or_422(db, body.device_id)
    if errors:
        raise ApiError(ErrorCode.VALIDATION_FAILED, "The output's device does not exist", errors)
    try:
        row = await video_crud.create_output(
            db,
            device_id=body.device_id,
            driver_ref=body.driver_ref,
            name=body.name,
            description=body.description,
            sort_order=body.sort_order,
        )
    except ConstraintError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "That driver reference is already used on this device",
            {"driver_ref": [exc.constraint]},
        ) from exc
    await _emit_config_changed(bus, "output_created")
    return _output_model(row)


@router.put("/outputs/{output_id}", response_model=MatrixOutputModel)
async def update_matrix_output(
    _: Admin,
    db: Db,
    bus: Bus,
    output_id: int,
    body: MatrixOutputUpdate,
    if_unmodified_since_version: Annotated[str | None, Header()] = None,
) -> MatrixOutputModel:
    version = _version_or_422(if_unmodified_since_version)
    fields = _provided(body)
    if "device_id" in fields:
        errors = await _device_or_422(db, fields["device_id"])
        if errors:
            raise ApiError(
                ErrorCode.VALIDATION_FAILED, "The output's device does not exist", errors
            )
    try:
        row = await video_crud.update_output(db, output_id, version, **fields)
    except NotFoundError as exc:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no HDMI output with that id") from exc
    except ConflictError as exc:
        current = await _output_or_404(db, output_id)
        raise ApiError(
            ErrorCode.CONFLICT,
            "This output was changed by someone else since you loaded it",
            {"current": _output_model(current).model_dump()},
        ) from exc
    except ConstraintError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "That driver reference is already used on this device",
            {"driver_ref": [exc.constraint]},
        ) from exc
    await _emit_config_changed(bus, "output_updated")
    return _output_model(row)


@router.delete("/outputs/{output_id}", status_code=204, response_class=Response)
async def delete_matrix_output(
    _: Admin, snapshot: PreChangeSnapshot, db: Db, bus: Bus, output_id: int
) -> Response:
    await snapshot(f"delete matrix output {output_id}")
    await _output_or_404(db, output_id)
    await video_crud.delete_output(db, output_id)  # never blocked (§15.10)
    await _emit_config_changed(bus, "output_deleted")
    return Response(status_code=204)


# -- configuration: video destinations (admin only, §16.1, §15.10) ------------------


class VideoDestinationModel(BaseModel):
    id: int
    device_id: int
    name: str
    default_input_id: int | None
    sort_order: int
    output_ids: list[int]
    updated_at: str


class VideoDestinationsResponse(BaseModel):
    destinations: list[VideoDestinationModel]


class VideoDestinationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: int
    name: str = ModelField(min_length=1, max_length=120)
    default_input_id: int | None = None
    sort_order: int = 0
    output_ids: list[int] = ModelField(default_factory=list)


class VideoDestinationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: int | None = None
    name: str | None = None
    default_input_id: int | None = None
    sort_order: int | None = None
    output_ids: list[int] | None = None


async def _destination_output_ids(db: Database, destination_id: int) -> list[int]:
    return [
        do.output_id for do in await video_crud.get_destination_outputs(db, destination_id)
    ]


async def _destination_config_model(
    db: Database, row: video_crud.VideoDestination
) -> VideoDestinationModel:
    return VideoDestinationModel(
        id=row.id,
        device_id=row.device_id,
        name=row.name,
        default_input_id=row.default_input_id,
        sort_order=row.sort_order,
        output_ids=await _destination_output_ids(db, row.id),
        updated_at=row.updated_at,
    )


async def _destination_or_404(db: Database, destination_id: int) -> video_crud.VideoDestination:
    row = await video_crud.get_destination(db, destination_id)
    if row is None:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no HDMI destination with that id")
    return row


async def _validate_output_ids(
    db: Database, device_id: int, output_ids: list[int]
) -> dict[str, list[str]]:
    if len(set(output_ids)) != len(output_ids):
        return {"output_ids": ["an output cannot be assigned to the same destination twice"]}
    for output_id in output_ids:
        output = await video_crud.get_output(db, output_id)
        if output is None or output.device_id != device_id:
            return {"output_ids": ["unknown"]}
    return {}


@router.get("/destinations", response_model=VideoDestinationsResponse)
async def list_video_destinations(_: Admin, db: Db) -> VideoDestinationsResponse:
    rows = await video_crud.list_destinations(db)
    return VideoDestinationsResponse(
        destinations=[await _destination_config_model(db, r) for r in rows]
    )


@router.get("/destinations/{destination_id}", response_model=VideoDestinationModel)
async def get_video_destination(_: Admin, db: Db, destination_id: int) -> VideoDestinationModel:
    row = await _destination_or_404(db, destination_id)
    return await _destination_config_model(db, row)


@router.post("/destinations", response_model=VideoDestinationModel, status_code=201)
async def create_video_destination(
    _: Admin, db: Db, bus: Bus, body: VideoDestinationCreate
) -> VideoDestinationModel:
    errors = await _device_or_422(db, body.device_id)
    errors.update(await _validate_output_ids(db, body.device_id, body.output_ids))
    if errors:
        raise ApiError(ErrorCode.VALIDATION_FAILED, "The destination is not valid", errors)
    row = await video_crud.create_destination(
        db,
        device_id=body.device_id,
        name=body.name,
        default_input_id=body.default_input_id,
        sort_order=body.sort_order,
    )
    if body.output_ids:
        await video_crud.set_destination_outputs(db, row.id, body.output_ids)
    await _emit_config_changed(bus, "destination_created")
    return await _destination_config_model(db, row)


@router.put("/destinations/{destination_id}", response_model=VideoDestinationModel)
async def update_video_destination(
    _: Admin,
    snapshot: PreChangeSnapshot,
    db: Db,
    bus: Bus,
    destination_id: int,
    body: VideoDestinationUpdate,
    if_unmodified_since_version: Annotated[str | None, Header()] = None,
) -> VideoDestinationModel:
    version = _version_or_422(if_unmodified_since_version)
    current = await _destination_or_404(db, destination_id)
    fields = _provided(body)
    output_ids = fields.pop("output_ids", None)
    device_id = fields.get("device_id", current.device_id)

    errors: dict[str, list[str]] = {}
    if "device_id" in fields:
        errors.update(await _device_or_422(db, device_id))
    if output_ids is not None:
        errors.update(await _validate_output_ids(db, device_id, output_ids))
    if errors:
        raise ApiError(ErrorCode.VALIDATION_FAILED, "The destination is not valid", errors)
    if output_ids is not None:
        # The destination's output list is replaced wholesale: a snapshot first,
        # unless the same outputs are sent back in the same order.
        await snapshot.before_replacing(
            f"video destination {destination_id}'s outputs",
            [o.output_id for o in await video_crud.get_destination_outputs(db, destination_id)],
            output_ids,
        )

    try:
        row = await video_crud.update_destination(db, destination_id, version, **fields)
    except NotFoundError as exc:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no HDMI destination with that id") from exc
    except ConflictError as exc:
        current_now = await _destination_or_404(db, destination_id)
        raise ApiError(
            ErrorCode.CONFLICT,
            "This destination was changed by someone else since you loaded it",
            {"current": (await _destination_config_model(db, current_now)).model_dump()},
        ) from exc
    if output_ids is not None:
        await video_crud.set_destination_outputs(db, destination_id, output_ids)
    await _emit_config_changed(bus, "destination_updated")
    return await _destination_config_model(db, row)


@router.delete("/destinations/{destination_id}", status_code=204, response_class=Response)
async def delete_video_destination(
    _: Admin, snapshot: PreChangeSnapshot, db: Db, bus: Bus, destination_id: int
) -> Response:
    await snapshot(f"delete video destination {destination_id}")
    await _destination_or_404(db, destination_id)
    try:
        await video_crud.delete_destination(db, destination_id)
    except InUseError as exc:
        raise ApiError(
            ErrorCode.IN_USE,
            "This destination is used by a saved scene and cannot be removed",
            {"references": _reference_list(exc.references)},
        ) from exc
    await _emit_config_changed(bus, "destination_deleted")
    return Response(status_code=204)


__all__ = ["router"]
