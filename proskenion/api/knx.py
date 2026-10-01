"""The KNX library and monitor endpoints (§7.1, §16.7, §21.19).

KNX is a subsystem, not a driver category (§5.5, B42) — its two entities,
``knx/addresses`` and ``knx/device-groups``, follow §16.1's generic
configuration CRUD shape (list/create/get/put/delete/references), and this
module adds the non-obvious ``/knx`` endpoints §16.7 names explicitly:
``GET /knx/monitor``, ``POST /knx/addresses/{id}/test-write``,
``POST /knx/import`` and ``GET /knx/export``. ``GET /knx/unsupported`` is an
addition beyond §16.7's table — the task brief's scope item 7 asks that the
DPTs the subsystem could not decode be surfaced per address (§7.1
*Unsupported types*, §21.19); §16.7 does not name a dedicated endpoint for
it, so this one exists to serve that screen without forcing the client to
paginate through every address to find the handful that need attention.

Every write here that changes the address library — create, update, delete,
a confirmed import — reloads the injected :class:`DbAddressRegistry`
afterwards, so the KNX subsystem decodes the next telegram against the
library as it now stands, without a restart.

Multipart uploads
------------------
``POST /knx/import`` takes FastAPI's own ``UploadFile``/``File``/``Form``
parameters, backed by ``python-multipart``, which streams each part into a
disk-spilling ``SpooledTemporaryFile`` rather than holding the body in
memory. Phase 6's larger uploads (§4.13's 256 MB firmware image, a backup
restore) should follow the same pattern. :data:`API_MAX_UPLOAD_BYTES` is this endpoint's own ceiling
— comfortably above §21.19's stated 5 MB import limit
(:data:`~proskenion.core.knx_import.MAX_UPLOAD_BYTES`), checked once the
upload is fully received (``python-multipart`` does not expose a way to
refuse mid-stream through FastAPI's ``File()``/``Form()`` parameters), so an
oversized file is still refused with a clear 422 before it reaches the parser.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, File, Form, Header, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict
from pydantic import Field as ModelField

from proskenion.api.deps import (
    get_bus,
    get_db,
    get_knx,
    get_knx_import_sessions,
    get_knx_optional,
    get_knx_registry,
    require_admin,
)
from proskenion.api.devices import VERSION_HEADER
from proskenion.api.errors import ApiError, ErrorCode
from proskenion.api.snapshots import PreChange, PreChangeSnapshot
from proskenion.core import knx as knx_core
from proskenion.core import knx_dpt, knx_import
from proskenion.core.auth import TokenClaims
from proskenion.core.bus import EventBus
from proskenion.core.events import LightingConfigChanged
from proskenion.core.knx_registry import DbAddressRegistry
from proskenion.db.connection import Database
from proskenion.db.crud import knx as knx_crud
from proskenion.db.crud.base import ConflictError, NotFoundError
from proskenion.db.crud.knx import DIRECTIONS, KnxDeviceGroup, KnxGroupAddress
from proskenion.db.crud.refs import InUseError, Reference

router = APIRouter(prefix="/knx", tags=["knx"])

Admin = Annotated[TokenClaims, Depends(require_admin)]
Db = Annotated[Database, Depends(get_db)]
Bus = Annotated[EventBus, Depends(get_bus)]
Knx = Annotated[knx_core.KnxSubsystem, Depends(get_knx)]
OptionalKnx = Annotated[knx_core.KnxSubsystem | None, Depends(get_knx_optional)]
KnxRegistry = Annotated[DbAddressRegistry, Depends(get_knx_registry)]
Sessions = Annotated[knx_import.ImportSessionStore, Depends(get_knx_import_sessions)]


# -- serialisation --------------------------------------------------------------


class ReferenceModel(BaseModel):
    entity: str
    id: int
    name: str


class UnsupportedTelegramModel(BaseModel):
    """A recorded telegram on an address whose DPT has no codec (§7.1)."""

    dpt: str
    raw: str  # hex
    timestamp: str


class KnxAddressModel(BaseModel):
    id: int
    group_address: str
    name: str
    description: str | None
    dpt: str
    direction: str
    device_id: int | None
    is_heartbeat: bool
    notes: str | None
    created_at: str
    updated_at: str
    #: Scene actions, rules, derived statuses and lighting channels referring
    #: to this address (§21.19's "Used" column) — zero means safely deletable.
    used_count: int
    unsupported: UnsupportedTelegramModel | None = None


class KnxAddressCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_address: str
    name: str = ModelField(min_length=1, max_length=120)
    description: str | None = None
    dpt: str
    direction: knx_core.AddressDirection
    device_id: int | None = None
    is_heartbeat: bool = False
    notes: str | None = None


class KnxAddressUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_address: str | None = None
    name: str | None = ModelField(default=None, min_length=1, max_length=120)
    description: str | None = None
    dpt: str | None = None
    direction: knx_core.AddressDirection | None = None
    device_id: int | None = None
    is_heartbeat: bool | None = None
    notes: str | None = None


class KnxDeviceGroupModel(BaseModel):
    id: int
    name: str
    description: str | None
    location: str | None
    created_at: str
    updated_at: str


class KnxDeviceGroupCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = ModelField(min_length=1, max_length=120)
    description: str | None = None
    location: str | None = None


class KnxDeviceGroupUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = ModelField(default=None, min_length=1, max_length=120)
    description: str | None = None
    location: str | None = None


class TestWriteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Any


class TestWriteResponse(BaseModel):
    ok: bool


class UnsupportedEntryModel(BaseModel):
    group_address: str
    name: str | None
    dpt: str
    raw: str
    timestamp: str


def _version_or_422(version: str | None) -> str:
    if not version:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            f"{VERSION_HEADER} is required so a concurrent edit is not overwritten",
            {VERSION_HEADER: ["required"]},
        )
    return version


def _validate_address_fields(*, group_address: str | None, dpt: str | None) -> None:
    """§7.1/§21.19: a valid group-address format and a DPT from the supported
    list, for the *manual* add/edit form — bulk import is more permissive
    (see :mod:`proskenion.core.knx_import`'s module docstring)."""
    errors: dict[str, list[str]] = {}
    if group_address is not None:
        try:
            knx_core.parse_group_address(group_address)
        except ValueError as exc:
            errors["group_address"] = [str(exc)]
    if dpt is not None and knx_dpt.resolve(dpt) is None:
        errors["dpt"] = [f'DPT "{dpt}" is not in the supported list (§7.1)']
    if errors:
        raise ApiError(ErrorCode.VALIDATION_FAILED, "The address is not valid", errors)


def _integrity_to_api_error(exc: sqlite3.IntegrityError, group_address: str) -> ApiError:
    if "UNIQUE" in str(exc).upper():
        return ApiError(
            ErrorCode.VALIDATION_FAILED,
            f'An address already exists for "{group_address}"',
            {"group_address": ["already in use"]},
        )
    return ApiError(
        ErrorCode.VALIDATION_FAILED,
        "The address violates a database constraint",
        {"detail": [str(exc)]},
    )


async def _address_or_404(db: Database, address_id: int) -> KnxGroupAddress:
    address = await knx_crud.get_address(db, address_id)
    if address is None:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no KNX address with that id")
    return address


async def _device_group_or_404(db: Database, group_id: int) -> KnxDeviceGroup:
    group = await knx_crud.get_device_group(db, group_id)
    if group is None:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no KNX device group with that id")
    return group


async def _address_model(
    db: Database, knx: knx_core.KnxSubsystem | None, address: KnxGroupAddress
) -> KnxAddressModel:
    references = await knx_crud.references_address(db, address.id)
    unsupported = None
    if knx is not None:
        entry = knx.unsupported().get(address.group_address)
        if entry is not None:
            unsupported = UnsupportedTelegramModel(
                dpt=entry.dpt, raw=entry.raw.hex(), timestamp=entry.timestamp
            )
    return KnxAddressModel(
        id=address.id,
        group_address=address.group_address,
        name=address.name,
        description=address.description,
        dpt=address.dpt,
        direction=address.direction,
        device_id=address.device_id,
        is_heartbeat=address.is_heartbeat,
        notes=address.notes,
        created_at=address.created_at,
        updated_at=address.updated_at,
        used_count=len(references),
        unsupported=unsupported,
    )


def _device_group_model(group: KnxDeviceGroup) -> KnxDeviceGroupModel:
    return KnxDeviceGroupModel(
        id=group.id,
        name=group.name,
        description=group.description,
        location=group.location,
        created_at=group.created_at,
        updated_at=group.updated_at,
    )


# -- addresses --------------------------------------------------------------------


@router.get("/addresses", response_model=list[KnxAddressModel])
async def list_addresses(
    _: Admin,
    db: Db,
    knx: OptionalKnx,
    device_group: int | None = None,
    direction: knx_core.AddressDirection | None = None,
    dpt: str | None = None,
) -> list[KnxAddressModel]:
    """§21.19's Addresses tab, optionally filtered (device group, direction, DPT)."""
    addresses = await knx_crud.list_addresses(db)
    if device_group is not None:
        addresses = [a for a in addresses if a.device_id == device_group]
    if direction is not None:
        addresses = [a for a in addresses if a.direction == direction.value]
    if dpt is not None:
        addresses = [a for a in addresses if a.dpt == dpt]
    return [await _address_model(db, knx, a) for a in addresses]


@router.post("/addresses", response_model=KnxAddressModel, status_code=201)
async def create_address(
    _: Admin,
    db: Db,
    registry: KnxRegistry,
    bus: Bus,
    knx: OptionalKnx,
    body: Annotated[KnxAddressCreate, Body()],
) -> KnxAddressModel:
    _validate_address_fields(group_address=body.group_address, dpt=body.dpt)
    try:
        address = await knx_crud.create_address(
            db,
            group_address=body.group_address,
            name=body.name,
            dpt=body.dpt,
            direction=body.direction.value,
            description=body.description,
            device_id=body.device_id,
            is_heartbeat=body.is_heartbeat,
            notes=body.notes,
        )
    except sqlite3.IntegrityError as exc:
        raise _integrity_to_api_error(exc, body.group_address) from exc
    await _library_changed(registry, bus)
    return await _address_model(db, knx, address)


@router.get("/addresses/{address_id}", response_model=KnxAddressModel)
async def get_address(_: Admin, db: Db, knx: OptionalKnx, address_id: int) -> KnxAddressModel:
    return await _address_model(db, knx, await _address_or_404(db, address_id))


@router.put("/addresses/{address_id}", response_model=KnxAddressModel)
async def update_address(
    _: Admin,
    db: Db,
    registry: KnxRegistry,
    bus: Bus,
    knx: OptionalKnx,
    address_id: int,
    body: Annotated[KnxAddressUpdate, Body()],
    if_unmodified_since_version: Annotated[str | None, Header()] = None,
) -> KnxAddressModel:
    version = _version_or_422(if_unmodified_since_version)
    _validate_address_fields(group_address=body.group_address, dpt=body.dpt)
    try:
        updated = await knx_crud.update_address(
            db,
            address_id,
            version,
            group_address=body.group_address,
            name=body.name,
            description=body.description,
            dpt=body.dpt,
            direction=None if body.direction is None else body.direction.value,
            device_id=body.device_id,
            is_heartbeat=body.is_heartbeat,
            notes=body.notes,
        )
    except ConflictError as exc:
        current = await _address_or_404(db, address_id)
        current_model = await _address_model(db, knx, current)
        raise ApiError(
            ErrorCode.CONFLICT,
            "This address was changed by someone else since you loaded it",
            {"current": current_model.model_dump()},
        ) from exc
    except NotFoundError as exc:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no KNX address with that id") from exc
    except sqlite3.IntegrityError as exc:
        raise _integrity_to_api_error(exc, body.group_address or "") from exc
    await _library_changed(registry, bus)
    return await _address_model(db, knx, updated)


@router.delete("/addresses/{address_id}", status_code=204, response_class=Response)
async def delete_address(
    _: Admin, snapshot: PreChangeSnapshot, db: Db, registry: KnxRegistry, bus: Bus, address_id: int
) -> Response:
    await snapshot(f"delete KNX address {address_id}")
    await _address_or_404(db, address_id)
    try:
        await knx_crud.delete_address(db, address_id)
    except InUseError as exc:
        raise ApiError(
            ErrorCode.IN_USE,
            "This address is referenced elsewhere and cannot be removed",
            {"references": [_reference_dict(r) for r in exc.references]},
        ) from exc
    await _library_changed(registry, bus)
    return Response(status_code=204)


async def _library_changed(registry: DbAddressRegistry, bus: EventBus) -> None:
    """After an address is added, changed or removed, or an import confirmed.

    The registry reloads so the next telegram decodes against the library as
    it now stands. The lighting service and the rules engine hold address ids
    resolved to group addresses and data types, and both reload from the
    database on :class:`LightingConfigChanged`.
    """
    await registry.reload()
    bus.emit(LightingConfigChanged(reason="knx_library"))


def _reference_dict(ref: Reference) -> dict[str, Any]:
    return {"entity": ref.entity, "id": ref.id, "name": ref.name}


@router.get("/addresses/{address_id}/references", response_model=list[ReferenceModel])
async def address_references(_: Admin, db: Db, address_id: int) -> list[ReferenceModel]:
    await _address_or_404(db, address_id)
    refs = await knx_crud.references_address(db, address_id)
    return [ReferenceModel(entity=r.entity, id=r.id, name=r.name) for r in refs]


# -- device groups ------------------------------------------------------------------


@router.get("/device-groups", response_model=list[KnxDeviceGroupModel])
async def list_device_groups(_: Admin, db: Db) -> list[KnxDeviceGroupModel]:
    groups = await knx_crud.list_device_groups(db)
    return [_device_group_model(g) for g in groups]


@router.post("/device-groups", response_model=KnxDeviceGroupModel, status_code=201)
async def create_device_group(
    _: Admin, db: Db, body: Annotated[KnxDeviceGroupCreate, Body()]
) -> KnxDeviceGroupModel:
    group = await knx_crud.create_device_group(
        db, name=body.name, description=body.description, location=body.location
    )
    return _device_group_model(group)


@router.get("/device-groups/{group_id}", response_model=KnxDeviceGroupModel)
async def get_device_group(_: Admin, db: Db, group_id: int) -> KnxDeviceGroupModel:
    return _device_group_model(await _device_group_or_404(db, group_id))


@router.put("/device-groups/{group_id}", response_model=KnxDeviceGroupModel)
async def update_device_group(
    _: Admin,
    db: Db,
    group_id: int,
    body: Annotated[KnxDeviceGroupUpdate, Body()],
    if_unmodified_since_version: Annotated[str | None, Header()] = None,
) -> KnxDeviceGroupModel:
    version = _version_or_422(if_unmodified_since_version)
    try:
        updated = await knx_crud.update_device_group(
            db,
            group_id,
            version,
            name=body.name,
            description=body.description,
            location=body.location,
        )
    except ConflictError as exc:
        current = await _device_group_or_404(db, group_id)
        raise ApiError(
            ErrorCode.CONFLICT,
            "This device group was changed by someone else since you loaded it",
            {"current": _device_group_model(current).model_dump()},
        ) from exc
    except NotFoundError as exc:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no KNX device group with that id") from exc
    return _device_group_model(updated)


@router.delete("/device-groups/{group_id}", status_code=204, response_class=Response)
async def delete_device_group(
    _: Admin, snapshot: PreChangeSnapshot, db: Db, group_id: int
) -> Response:
    """§21.19: "Deletion is allowed only when no addresses are assigned."

    ``knx_group_addresses.device_id`` is ``ON DELETE SET NULL`` (§15.7) —
    that is the schema's backstop, not the policy. §16.1's generic CRUD
    contract answers a blocked ``DELETE`` with 409 ``in_use`` and a
    reference list, the same as every other entity, so that check is made
    here rather than relaxing it to match what the database alone would
    allow.
    """
    await snapshot(f"delete KNX device group {group_id}")
    await _device_group_or_404(db, group_id)
    references = await knx_crud.references_device_group(db, group_id)
    if references:
        raise ApiError(
            ErrorCode.IN_USE,
            "This device group has addresses assigned and cannot be removed",
            {"references": [_reference_dict(r) for r in references]},
        )
    await knx_crud.delete_device_group(db, group_id)
    return Response(status_code=204)


@router.get("/device-groups/{group_id}/references", response_model=list[ReferenceModel])
async def device_group_references(_: Admin, db: Db, group_id: int) -> list[ReferenceModel]:
    await _device_group_or_404(db, group_id)
    refs = await knx_crud.references_device_group(db, group_id)
    return [ReferenceModel(entity=r.entity, id=r.id, name=r.name) for r in refs]


# -- test write (§21.19 *Test write*) ------------------------------------------------


def _coerce_test_value(dpt: str, value: Any) -> Any:
    """A JSON body's value, coerced to what the DPT codec expects.

    Every supported DPT but 3.007 already accepts the plain JSON types
    (``bool``, ``int``, ``float``) its codec was written against. 3.007
    (dimming control) is a small dataclass; a JSON object with ``increase``
    and ``step_code`` is converted to one so the mini-form §21.19 describes
    ("a value field typed to the DPT") works for it too.
    """
    codec = knx_dpt.resolve(dpt)
    if codec is not None and codec.dpt == "3.007" and isinstance(value, dict):
        return knx_dpt.DimmingControl(
            increase=bool(value.get("increase", False)), step_code=int(value.get("step_code", 0))
        )
    return value


@router.post("/addresses/{address_id}/test-write", response_model=TestWriteResponse)
async def test_write(
    _: Admin, db: Db, knx: Knx, address_id: int, body: Annotated[TestWriteBody, Body()]
) -> TestWriteResponse:
    address = await _address_or_404(db, address_id)
    value = _coerce_test_value(address.dpt, body.value)
    try:
        await knx.write(address.group_address, value, priority=knx_core.Priority.SCENE_STATUS)
    except knx_core.IncomingOnlyAddress as exc:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED, str(exc), {"reason": "incoming_only"}
        ) from exc
    except knx_core.UnknownGroupAddress as exc:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED, str(exc), {"reason": "not_registered"}
        ) from exc
    except knx_core.UnsupportedDpt as exc:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED, str(exc), {"reason": "unsupported_dpt"}
        ) from exc
    except knx_dpt.DptError as exc:
        raise ApiError(ErrorCode.VALIDATION_FAILED, str(exc), {"reason": "invalid_value"}) from exc
    return TestWriteResponse(ok=True)


# -- live monitor (§21.19) -----------------------------------------------------------


def _json_safe_value(value: Any) -> Any:
    if isinstance(value, knx_dpt.DimmingControl):
        return {"increase": value.increase, "step_code": value.step_code}
    return value


def _monitor_entry_dict(entry: knx_core.MonitorEntry) -> dict[str, Any]:
    return {
        "timestamp": entry.timestamp,
        "direction": entry.direction,
        "group_address": entry.group_address,
        "dpt": entry.dpt,
        "value": _json_safe_value(entry.value),
        "raw": entry.raw.hex(),
        "source_address": entry.source_address,
    }


@router.get("/monitor")
async def monitor(_: Admin, knx: Knx) -> StreamingResponse:
    """SSE, both directions, replaying a short backlog first (§21.19).

    :meth:`~proskenion.core.knx.TelegramMonitor.stream` already does the
    replay-then-live-feed; this only serialises each entry as one SSE
    ``data:`` frame.
    """

    async def events() -> AsyncIterator[bytes]:
        async for entry in knx.monitor.stream():
            yield f"data: {json.dumps(_monitor_entry_dict(entry))}\n\n".encode()

    # X-Accel-Buffering: nginx buffers a proxied response by default, so without
    # this the monitor showed telegrams in late clumps (found by the perf run on
    # the CM5, 1 Oct 2026); the derived-status monitor already sent it.
    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/unsupported", response_model=list[UnsupportedEntryModel])
async def unsupported_telegrams(_: Admin, db: Db, knx: Knx) -> list[UnsupportedEntryModel]:
    """DPTs the subsystem could not decode, per address (§7.1 *Unsupported types*)."""
    entries = knx.unsupported()
    if not entries:
        return []
    names = {a.group_address: a.name for a in await knx_crud.list_addresses(db)}
    return [
        UnsupportedEntryModel(
            group_address=address,
            name=names.get(address),
            dpt=e.dpt,
            raw=e.raw.hex(),
            timestamp=e.timestamp,
        )
        for address, e in entries.items()
    ]


# -- import: FastAPI's Form/UploadFile, over python-multipart (see module docstring) --

#: This endpoint's own transport-level ceiling — comfortably above §21.19's
#: 5 MB wizard limit (:data:`knx_import.MAX_UPLOAD_BYTES`), which still
#: applies on top once the file reaches the parser. Checked once
#: ``python-multipart`` has finished receiving the upload into its
#: disk-spilling ``SpooledTemporaryFile`` — there is no hook to refuse a
#: FastAPI ``File()`` upload mid-stream — so a file over this size is still
#: refused with a clear 422 before :func:`knx_import.parse_upload` ever sees it.
API_MAX_UPLOAD_BYTES = 10 * 1024 * 1024


async def _read_upload(file: UploadFile) -> bytes:
    if file.size is not None and file.size > API_MAX_UPLOAD_BYTES:
        raise _upload_too_large()
    content = await file.read()
    if len(content) > API_MAX_UPLOAD_BYTES:
        raise _upload_too_large()
    return content


def _upload_too_large() -> ApiError:
    limit_mb = API_MAX_UPLOAD_BYTES // (1024 * 1024)
    return ApiError(
        ErrorCode.VALIDATION_FAILED,
        f"The file is larger than the {limit_mb} MB limit",
        {"file": ["too_large"]},
    )


def _preview_row_dict(row: knx_import.PreviewRow) -> dict[str, Any]:
    return {
        "row_number": row.row_number,
        "group_address": row.group_address,
        "name": row.name,
        "description": row.description,
        "dpt": row.dpt,
        "importable": row.importable,
        "existing_id": row.existing_id,
        "existing_name": row.existing_name,
        "warnings": [asdict(w) for w in row.warnings],
    }


async def _preview_import(
    db: Database,
    sessions: knx_import.ImportSessionStore,
    file: UploadFile | None,
    fmt_raw: str | None,
    mapping_raw: str | None,
) -> dict[str, Any]:
    if file is None or not file.filename:
        raise ApiError(ErrorCode.VALIDATION_FAILED, "A file is required", {"file": ["required"]})
    filename = file.filename
    content = await _read_upload(file)

    requested_format: knx_import.ImportFormat | None = None
    if fmt_raw:
        if fmt_raw not in ("ets_csv", "ets_xml", "generic"):
            raise ApiError(
                ErrorCode.VALIDATION_FAILED,
                "format must be ets_csv, ets_xml or generic",
                {"format": ["invalid"]},
            )
        requested_format = fmt_raw  # type: ignore[assignment]

    mapping: dict[str, str] | None = None
    if mapping_raw:
        try:
            mapping = json.loads(mapping_raw)
        except json.JSONDecodeError as exc:
            raise ApiError(
                ErrorCode.VALIDATION_FAILED, "mapping must be valid JSON", {"mapping": [str(exc)]}
            ) from exc

    try:
        parsed = knx_import.parse_upload(
            filename, content, requested_format=requested_format, mapping=mapping
        )
    except knx_import.EsfNotSupportedError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED, str(exc), {"reason": "esf_not_supported"}
        ) from exc
    except knx_import.UnsafeXmlError as exc:
        raise ApiError(ErrorCode.VALIDATION_FAILED, str(exc), {"reason": "unsafe_xml"}) from exc
    except knx_import.KnxImportError as exc:
        raise ApiError(ErrorCode.VALIDATION_FAILED, str(exc), {"reason": "import_failed"}) from exc

    rows = await knx_import.annotate_duplicates(db, parsed.rows)
    token = sessions.put(parsed.format, filename, rows)
    return {
        "token": token,
        "format": parsed.format,
        "filename": filename,
        "row_count": parsed.row_count,
        "columns": parsed.columns,
        "importable_count": sum(1 for r in rows if r.importable),
        "duplicate_count": sum(1 for r in rows if r.existing_id is not None),
        "rows": [_preview_row_dict(r) for r in rows],
    }


async def _confirm_import(
    db: Database,
    registry: DbAddressRegistry,
    bus: EventBus,
    sessions: knx_import.ImportSessionStore,
    token: str | None,
    direction: str | None,
    strategy_raw: str | None,
    snapshot: PreChange,
) -> dict[str, Any]:
    if not token:
        raise ApiError(ErrorCode.VALIDATION_FAILED, "token is required", {"token": ["required"]})
    session = sessions.pop(token)
    if session is None:
        raise ApiError(
            ErrorCode.NOT_FOUND,
            "This import preview has expired or was already confirmed; upload the file again",
        )

    if direction not in DIRECTIONS:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "direction must be incoming, outgoing or both",
            {"direction": ["invalid"]},
        )
    strategy = strategy_raw or "skip"
    if strategy not in ("skip", "overwrite"):
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "duplicate_strategy must be skip or overwrite",
            {"duplicate_strategy": ["invalid"]},
        )

    result = await knx_import.confirm_import(
        db,
        session,
        direction=direction,
        duplicate_strategy=strategy,  # type: ignore[arg-type]
        snapshot=lambda: snapshot(f"KNX import of {session.filename}"),
    )
    await _library_changed(registry, bus)
    return {
        "added": result.added,
        "updated": result.updated,
        "skipped": result.skipped,
        "added_count": len(result.added),
        "updated_count": len(result.updated),
        "skipped_count": len(result.skipped),
    }


@router.post("/import")
async def import_library(
    _: Admin,
    snapshot: PreChangeSnapshot,
    db: Db,
    registry: KnxRegistry,
    bus: Bus,
    sessions: Sessions,
    step: Annotated[str, Form()] = "preview",
    file: Annotated[UploadFile | None, File()] = None,
    format: Annotated[str | None, Form(alias="format")] = None,  # noqa: A002 - the wire field name
    mapping: Annotated[str | None, Form()] = None,
    token: Annotated[str | None, Form()] = None,
    direction: Annotated[str | None, Form()] = None,
    duplicate_strategy: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    """Two steps in one endpoint, told apart by the ``step`` field (§7.1, §21.19):

    ``step=preview`` parses and validates ``file`` (plus, for a generic
    CSV/TSV, a JSON ``mapping`` of source column -> target field) and returns
    every row with inline warnings and an import token. ``step=confirm``
    takes that ``token``, the admin's ``direction`` and ``duplicate_strategy``
    choices, and applies the importable rows in one all-or-nothing
    transaction.
    """
    if step == "preview":
        return await _preview_import(db, sessions, file, format, mapping)
    if step == "confirm":
        return await _confirm_import(
            db, registry, bus, sessions, token, direction, duplicate_strategy, snapshot
        )
    raise ApiError(
        ErrorCode.VALIDATION_FAILED, "step must be 'preview' or 'confirm'", {"step": ["invalid"]}
    )


# -- export (§21.19) ------------------------------------------------------------------


@router.get("/export")
async def export_library(_: Admin, db: Db) -> Response:
    """The whole library as CSV in the ETS column layout (§21.19)."""
    addresses = await knx_crud.list_addresses(db)
    csv_text = knx_import.export_csv(
        [
            {
                "group_address": a.group_address,
                "name": a.name,
                "description": a.description,
                "dpt": a.dpt,
            }
            for a in addresses
        ]
    )
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="knx-group-addresses.csv"'},
    )


__all__ = ["router"]
