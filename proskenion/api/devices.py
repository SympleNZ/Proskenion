"""Drivers and devices endpoints (spec §16.7 *Drivers and devices*, §21.24).

The Devices screen is generated, not hand-written: each driver declares a
``CONFIG_SCHEMA`` of typed fields and the screen renders it, so adding a driver
needs no interface work (§21.24). That only holds if this module serialises a
:class:`~proskenion.core.drivers.fields.Field` faithfully — every attribute,
``depends_on`` and ``options`` included — and publishes each supported
transport's own schema beside it. It does.

Two rules here are worth stating plainly.

**Encrypted fields never come back.** A field the schema marks ``encrypted``
is returned as :data:`SECRET_SENTINEL`, which says a value is set without
revealing it. Sending that sentinel back — or omitting the field — on a
``PUT`` means "unchanged" (§6.10).

**A save that cannot connect is undone.** ``PUT`` writes the row, reloads the
device and waits briefly for it to report in; if it does not, the previous row
is restored and the failure explained. A wrong address should not leave the
system unable to reach a working device (§16.7, §21.24).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict
from pydantic import Field as ModelField

from proskenion.api.deps import (
    current_session,
    get_bus,
    get_config,
    get_db,
    get_devices,
    get_helper,
    require_admin,
    require_staff,
    settle_hirer_permissions,
)
from proskenion.api.errors import ApiError, ErrorCode
from proskenion.api.snapshots import PreChangeSnapshot
from proskenion.config import Config
from proskenion.core import system_config
from proskenion.core.auth import TokenClaims
from proskenion.core.bus import EventBus
from proskenion.core.devices import DeviceManager, DeviceUnavailable, TestReport
from proskenion.core.drivers import registry
from proskenion.core.drivers.base import Driver
from proskenion.core.drivers.capabilities import ChannelRef, MatrixRefs, MixerCapabilities
from proskenion.core.drivers.categories import Category
from proskenion.core.drivers.fields import Field, is_encrypted_value
from proskenion.core.drivers.registry import ConfigValidationError, DriverInfo, UnknownDriver
from proskenion.core.events import MixerConfigChanged, VideoConfigChanged
from proskenion.core.helper import HelperClient
from proskenion.core.mixer.desk_channels import (
    add_missing_channels,
    declared_desk_channels,
    uncovered,
)
from proskenion.core.remap import Available, RemapRejected, propose, resolve
from proskenion.core.state import DeviceStatusRecord
from proskenion.core.transport.serial import enumerate_serial_ports
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import mixer as mixer_crud
from proskenion.db.crud import remap as remap_crud
from proskenion.db.crud.base import ConflictError, InUseError, NotFoundError
from proskenion.db.crud.devices import Device

log = logging.getLogger(__name__)

router = APIRouter(tags=["devices"])

#: What a set-but-secret value looks like in a response, and what "unchanged"
#: looks like coming back in a ``PUT`` (§6.10).
SECRET_SENTINEL: dict[str, Any] = {"set": True}

#: The §16.1 optimistic-concurrency header every configuration ``PUT`` carries.
VERSION_HEADER = "If-Unmodified-Since-Version"

Admin = Annotated[TokenClaims, Depends(require_admin)]
Staff = Annotated[TokenClaims, Depends(require_staff)]
AnySession = Annotated[TokenClaims, Depends(current_session)]
Db = Annotated[Database, Depends(get_db)]
Bus = Annotated[EventBus, Depends(get_bus)]
Devices = Annotated[DeviceManager, Depends(get_devices)]
AppConfig = Annotated[Config, Depends(get_config)]
Helper = Annotated[HelperClient, Depends(get_helper)]


# -- serialisation ------------------------------------------------------------------


class OptionModel(BaseModel):
    """One choice of an ``enum`` field."""

    value: str
    label: str


class DependsOnModel(BaseModel):
    """``depends_on``: show this field only while another field equals ``equals``."""

    field: str
    equals: Any


class FieldModel(BaseModel):
    """A :class:`Field`, whole. The form is generated from this and nothing else."""

    key: str
    type: str
    label: str
    required: bool
    default: Any = None
    min: int | None = None
    max: int | None = None
    pattern: str | None = None
    options: list[OptionModel] | None = None
    depends_on: DependsOnModel | None = None
    encrypted: bool = False
    help: str | None = None


class TransportModel(BaseModel):
    """One transport a driver supports, with its own schema and the driver's defaults."""

    type: str
    config_schema: list[FieldModel]
    defaults: dict[str, Any]


class DriverModel(BaseModel):
    key: str
    category: str
    name: str
    transports: list[TransportModel]
    config_schema: list[FieldModel]
    capabilities: dict[str, Any]


class DriversResponse(BaseModel):
    drivers: list[DriverModel]


class PortModel(BaseModel):
    """One row of the serial picker (§21.24 *Serial device picker*)."""

    path: str
    label: str
    vendor_id: str | None = None
    product_id: str | None = None
    serial: str | None = None
    in_use: bool = False
    stable: bool = True
    in_use_by: str | None = None


class PortsResponse(BaseModel):
    ports: list[PortModel]


class DeviceModel(BaseModel):
    id: int
    category: str
    driver_key: str
    name: str
    enabled: bool
    config: dict[str, Any]
    created_at: str
    updated_at: str
    state_key: str | None = None
    status: dict[str, Any] | None = None


class DevicesResponse(BaseModel):
    devices: list[DeviceModel]


class DeviceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: Category
    driver_key: str = ModelField(min_length=1, max_length=64)
    name: str = ModelField(min_length=1, max_length=120)
    config: dict[str, Any]
    enabled: bool = True


class DeviceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = ModelField(default=None, min_length=1, max_length=120)
    driver_key: str | None = ModelField(default=None, min_length=1, max_length=64)
    enabled: bool | None = None
    config: dict[str, Any] | None = None


class StageModel(BaseModel):
    ok: bool
    detail: str | None = None
    attempted: bool = True


class TestResponse(BaseModel):
    """Both §5.3 stages, separately — connect proves the transport, probe the device."""

    ok: bool
    connect: StageModel
    probe: StageModel
    message: str


class CapabilitiesResponse(BaseModel):
    category: str
    #: §21.24 shows capabilities *as connected*, not as declared — the
    #: difference is the diagnosis when something is missing.
    as_connected: bool
    capabilities: dict[str, Any]


class RemapRow(BaseModel):
    """One row holding references to the device, with a proposal per old reference."""

    holder: str
    id: int
    name: str
    kind: str
    #: Only a mixer channel can be unmapped (§15.6).
    unmapped: bool
    old_refs: list[str]
    #: The pre-selection for each old reference, in the same order; ``None``
    #: where the screen starts blank (§5.5: never a positional guess).
    new_refs: list[str | None]


class RemapResponse(BaseModel):
    """Old references beside the driver's own, for a driver change (§5.5)."""

    device_id: int
    driver_key: str
    as_connected: bool
    mappings: list[RemapRow]
    available: dict[str, Any]
    #: Desk channels no mapped channel covers (a mixer only; 0 otherwise). The
    #: re-mapping screen offers to add them once references are applied (§5.5).
    missing_channels: int = 0


class RemapChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    holder: Literal["mixer_channel", "matrix_input", "matrix_output"]
    id: int
    #: The new references in order; ``None`` or empty leaves the row unmapped.
    new_refs: list[str] | None = None


class RemapApply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mappings: list[RemapChoice]


def field_model(field: Field) -> FieldModel:
    return FieldModel(
        key=field.key,
        type=field.type,
        label=field.label,
        required=field.required,
        default=field.default,
        min=field.min,
        max=field.max,
        pattern=field.pattern,
        options=(
            None
            if field.options is None
            else [OptionModel(value=value, label=label) for value, label in field.options]
        ),
        depends_on=(
            None
            if field.depends_on is None
            else DependsOnModel(field=field.depends_on[0], equals=field.depends_on[1])
        ),
        encrypted=field.encrypted,
        help=field.help,
    )


def driver_model(info: DriverInfo) -> DriverModel:
    return DriverModel(
        key=info.key,
        category=info.category.value,
        name=info.name,
        transports=[
            TransportModel(
                type=transport.type,
                config_schema=[field_model(f) for f in transport.schema],
                defaults=dict(transport.defaults),
            )
            for transport in info.transports
        ],
        config_schema=[field_model(f) for f in info.config_schema],
        capabilities=_as_dict(info.capabilities),
    )


def _as_dict(value: object) -> dict[str, Any]:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise TypeError(f"not a dataclass: {type(value).__name__}")  # pragma: no cover


def public_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """``config`` with every encrypted value replaced by :data:`SECRET_SENTINEL`.

    Schema-free on purpose: a value stored in the encrypted form is redacted
    whether or not the driver that wrote it still ships, so a row whose driver
    has gone cannot leak a secret through the list endpoint.
    """
    public: dict[str, Any] = {}
    for key, value in config.items():
        if is_encrypted_value(value):
            public[key] = dict(SECRET_SENTINEL)
        elif isinstance(value, Mapping):
            public[key] = public_config(value)
        else:
            public[key] = value
    return public


def device_model(device: Device, manager: DeviceManager) -> DeviceModel:
    record = manager.status(device.id)
    return DeviceModel(
        id=device.id,
        category=device.category,
        driver_key=device.driver_key,
        name=device.name,
        enabled=device.enabled,
        config=public_config(device.config),
        created_at=device.created_at,
        updated_at=device.updated_at,
        state_key=manager.state_key(device.id),
        status=None if record is None else record.as_dict(),
    )


def test_response(report: TestReport) -> TestResponse:
    return TestResponse(
        ok=report.ok,
        connect=StageModel(**asdict(report.connect)),
        probe=StageModel(**asdict(report.probe)),
        message=report.message,
    )


# -- helpers ------------------------------------------------------------------------


async def _sync_firewall(db: Database, config: Config, helper: HelperClient) -> None:
    """Mirror the ``devices`` table into ``system.json`` and ask the helper
    to re-render the firewall from it (contracts §4, the Phase 1 gap):
    called after every create, update and delete, and — for an update —
    before the reconnect-and-test below, so a device just moved to a new
    address is not blocked by the rule its old one left behind.

    Best-effort: the device row itself is already committed by the time
    this runs, and a helper or filesystem hiccup here is logged rather than
    turned into a failed save — the alternative is a device CRUD that can
    never succeed while ``auditorium-helper.path`` is not running, which is
    worse than a firewall re-render lagging by one save.
    """
    try:
        rows = await devices_crud.list_all(db)
        await asyncio.to_thread(system_config.sync_devices, config.app.data_dir, rows)
        await helper.submit("apply-network")
    except OSError as exc:
        log.warning("could not mirror the devices table into the firewall: %s", exc)


async def _device_or_404(db: Database, device_id: int) -> Device:
    device = await devices_crud.get(db, device_id)
    if device is None:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no device with that id")
    return device


def _driver_class_or_422(category: Category, driver_key: str) -> type[Driver]:
    try:
        return registry.get(category, driver_key)
    except UnknownDriver as exc:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "That driver does not ship with this version",
            {"driver_key": [f"no {category.value} driver named {driver_key!r}"]},
        ) from exc


async def _validated(
    manager: DeviceManager, category: Category, driver_key: str, config: Mapping[str, Any]
) -> dict[str, Any]:
    """The configuration as it will be stored, or 422 with per-field detail."""
    driver_cls = _driver_class_or_422(category, driver_key)
    stored = manager.encrypt_stored(driver_cls, config)
    try:
        await manager.validate(category, driver_key, stored)
    except ConfigValidationError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "The device configuration is not valid",
            exc.detail,
        ) from exc
    return stored


def _keep_secrets(
    manager: DeviceManager,
    driver_cls: type[Driver],
    new_config: Mapping[str, Any],
    old_config: Mapping[str, Any],
) -> dict[str, Any]:
    """An absent or sentinel password on ``PUT`` means "unchanged" (§6.10)."""
    merged: dict[str, Any] = {
        key: dict(value) if isinstance(value, Mapping) else value
        for key, value in new_config.items()
    }
    for block, key in manager.encrypted_keys(driver_cls, new_config):
        old_block = old_config.get(block)
        stored = old_block.get(key) if isinstance(old_block, Mapping) else None
        target = merged.get(block)
        if not isinstance(target, dict):
            continue
        value = target.get(key)
        unchanged = value is None or value == SECRET_SENTINEL or is_encrypted_value(value)
        if not unchanged:
            continue
        if stored is None:
            target.pop(key, None)
        else:
            target[key] = stored
    return merged


def _version_or_422(version: str | None) -> str:
    if not version:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            f"{VERSION_HEADER} is required so a concurrent edit is not overwritten",
            {VERSION_HEADER: ["required"]},
        )
    return version


async def _resolve(manager: DeviceManager, device_id: int) -> tuple[Driver, bool]:
    try:
        return await manager.resolve_driver(device_id)
    except DeviceUnavailable as exc:
        raise ApiError(ErrorCode.DEVICE_UNAVAILABLE, str(exc)) from exc


def _unsupported(what: str) -> ApiError:
    """§5.5 capability degradation: what this device cannot do is not hidden, it is 404."""
    return ApiError(ErrorCode.NOT_FOUND, f"This device does not provide {what}")


def _ref_model(ref: ChannelRef) -> dict[str, Any]:
    return asdict(ref)


# -- drivers ------------------------------------------------------------------------


@router.get("/drivers", response_model=DriversResponse)
async def list_drivers(_: Admin, category: Category | None = None) -> DriversResponse:
    """Shipped drivers, with their transports, config schemas and declared capabilities."""
    return DriversResponse(drivers=[driver_model(info) for info in registry.available(category)])


@router.get("/drivers/serial-ports", response_model=PortsResponse)
async def list_serial_ports(_: Admin, manager: Devices) -> PortsResponse:
    """Ports for the picker, with *in use* resolved from the configured devices.

    §16.7's table does not list this endpoint; the picker it specifies in
    §21.24 cannot be built without it (see the Phase 1 decisions).
    """
    ports = await enumerate_serial_ports(held_paths=manager.held_paths())
    return PortsResponse(ports=[PortModel(**asdict(port)) for port in ports])


# -- devices ------------------------------------------------------------------------


@router.get("/devices", response_model=DevicesResponse)
async def list_devices(_: Admin, db: Db, manager: Devices) -> DevicesResponse:
    rows = await devices_crud.list_all(db)
    return DevicesResponse(devices=[device_model(row, manager) for row in rows])


@router.post("/devices", response_model=DeviceModel, status_code=201)
async def create_device(
    _: Admin,
    db: Db,
    bus: Bus,
    manager: Devices,
    config: AppConfig,
    helper: Helper,
    body: Annotated[DeviceCreate, Body()],
) -> DeviceModel:
    stored = await _validated(manager, body.category, body.driver_key, body.config)
    device = await devices_crud.create(
        db,
        category=body.category.value,
        driver_key=body.driver_key,
        name=body.name,
        config=stored,
        enabled=body.enabled,
    )
    await manager.reload(device.id)
    await _sync_firewall(db, config, helper)
    # A mixer is given a channel for every desk channel, Main among them
    # (§7.3). POST /devices and the first-run wizard's device step
    # (proskenion/api/setup.py) are the two places that create them.
    if await add_missing_channels(db, manager, device):
        bus.emit(MixerConfigChanged(reason="desk_channels_created"))
    return device_model(device, manager)


@router.get("/devices/{device_id}", response_model=DeviceModel)
async def get_device(_: Admin, db: Db, manager: Devices, device_id: int) -> DeviceModel:
    return device_model(await _device_or_404(db, device_id), manager)


@router.put("/devices/{device_id}", response_model=DeviceModel)
async def update_device(
    _: Admin,
    request: Request,
    db: Db,
    bus: Bus,
    manager: Devices,
    config: AppConfig,
    helper: Helper,
    snapshot: PreChangeSnapshot,
    device_id: int,
    body: Annotated[DeviceUpdate, Body()],
    if_unmodified_since_version: Annotated[str | None, Header()] = None,
) -> DeviceModel:
    """Save, reconnect, and revert if the device cannot be reached (§16.7, §21.24).

    §7.2.4/§18: changing a device's driver or transport is a pre-change
    action — the connection it replaces cannot be recovered by anything
    short of a database snapshot, unlike the rest of a device row. A rename
    or an enable/disable toggle with the config untouched takes none.

    §5.5: changing the driver invalidates every reference the device's
    channels hold, so each is marked unmapped until the re-mapping screen
    maps it again (``/devices/{id}/remap``). A save that is reverted
    restores them, because the driver change never happened.
    """
    version = _version_or_422(if_unmodified_since_version)
    previous = await _device_or_404(db, device_id)
    driver_key = body.driver_key or previous.driver_key
    category = Category(previous.category)

    stored: dict[str, Any] | None = None
    if body.config is not None:
        driver_cls = _driver_class_or_422(category, driver_key)
        merged = _keep_secrets(manager, driver_cls, body.config, previous.config)
        stored = await _validated(manager, category, driver_key, merged)

    if driver_key != previous.driver_key or (stored is not None and stored != previous.config):
        await snapshot(f"change device {device_id}'s driver or transport")

    try:
        updated = await devices_crud.update(
            db,
            device_id,
            version,
            name=body.name,
            driver_key=body.driver_key,
            enabled=body.enabled,
            config=stored,
        )
    except ConflictError as exc:
        current = await _device_or_404(db, device_id)
        raise ApiError(
            ErrorCode.CONFLICT,
            "This device was changed by someone else since you loaded it",
            {"current": device_model(current, manager).model_dump()},
        ) from exc
    except NotFoundError as exc:  # pragma: no cover - _device_or_404 ran first
        raise ApiError(ErrorCode.NOT_FOUND, "There is no device with that id") from exc

    invalidated: list[int] = []
    if driver_key != previous.driver_key:
        invalidated = await remap_crud.invalidate_device(db, device_id)
        if invalidated:
            bus.emit(MixerConfigChanged(reason="driver_changed"))
            await settle_hirer_permissions(request, "driver_changed")

    await manager.reload(device_id)
    # Mirrored — and the firewall re-rendered — before the reconnect attempt
    # below, so a device just saved at a new address passes the firewall
    # before its test runs rather than being blocked by the rule its old
    # address left behind (contracts §4, the Phase 1 gap).
    await _sync_firewall(db, config, helper)
    if not updated.enabled:
        return device_model(updated, manager)

    record = await manager.wait_for_connection(device_id)
    if record is not None and record.status == "connected":
        return device_model(updated, manager)

    reason = record.detail if record is not None and record.detail else "the device did not reply"
    restored = await devices_crud.update(
        db,
        device_id,
        updated.updated_at,
        name=previous.name,
        driver_key=previous.driver_key,
        enabled=previous.enabled,
        config=previous.config,
    )
    if invalidated:
        await remap_crud.restore_mapped(db, invalidated)
        bus.emit(MixerConfigChanged(reason="driver_change_reverted"))
        await settle_hirer_permissions(request, "driver_change_reverted")
    await manager.reload(device_id)
    # The firewall was already re-rendered for the address that just failed;
    # re-render it again for the address just restored, or "revert" would
    # leave the device firewalled by the address it was reverted away from.
    await _sync_firewall(db, config, helper)
    log.warning(
        "device %s reverted after a failed save: %s", device_id, reason, extra={"device": device_id}
    )
    raise ApiError(
        ErrorCode.DEVICE_UNAVAILABLE,
        "The new settings could not reach the device; the previous settings were restored",
        {
            "reason": reason,
            "reverted": True,
            "device": device_model(restored, manager).model_dump(),
        },
    )


@router.delete("/devices/{device_id}", status_code=204, response_class=Response)
async def delete_device(
    _: Admin,
    snapshot: PreChangeSnapshot,
    db: Db,
    manager: Devices,
    config: AppConfig,
    helper: Helper,
    device_id: int,
) -> Response:
    await snapshot(f"delete device {device_id}")
    await _device_or_404(db, device_id)
    try:
        await devices_crud.delete(db, device_id)
    except InUseError as exc:
        raise ApiError(
            ErrorCode.IN_USE,
            "This device is referenced elsewhere and cannot be removed",
            {"device_id": device_id},
        ) from exc
    await manager.reload(device_id)
    await _sync_firewall(db, config, helper)
    return Response(status_code=204)


def _shares_connection(category: Category, driver_key: str) -> bool:
    """Whether a *second*, simultaneous connection to this device could be
    refused, or could knock the running one off — true for every mixer (the
    CQ-20B's desk allows one MIDI client, §7.3 bench question 6), or for any
    driver that declares :attr:`~proskenion.core.drivers.base.Driver.SHARES_CONNECTION`
    for the same reason in another category. The mixer category is checked
    first so this still answers correctly for a driver key the registry does
    not (or no longer) recognise, rather than special-casing ``"cq20b"``
    here."""
    if category is Category.MIXER:
        return True
    try:
        driver_cls = registry.get(category, driver_key)
    except UnknownDriver:
        return False
    return bool(driver_cls.SHARES_CONNECTION)


def _running_test_response(record: DeviceStatusRecord | None) -> TestResponse:
    """The already-running driver's own status, for a device the test must
    not open a second connection to (see :func:`_shares_connection`). Neither
    stage is freshly attempted; both report what the supervised connection
    already knows, so the desk is never asked to admit — or refuse — a
    second client just to answer this button."""
    connected = record is not None and record.status == "connected"
    detail = record.detail if record is not None else None
    return TestResponse(
        ok=connected,
        connect=StageModel(ok=True, detail=None, attempted=False),
        probe=StageModel(ok=connected, detail=detail, attempted=False),
        message=(
            "Already connected; reporting the running connection's status."
            if connected
            else "The running connection is not currently connected"
            + (f": {detail}" if detail else ".")
        ),
    )


@router.post("/devices/{device_id}/test", response_model=TestResponse)
async def test_device(_: Admin, db: Db, manager: Devices, device_id: int) -> TestResponse:
    """Connect, then probe, and report the two separately (§5.3, §21.24).

    A device whose category or driver declares that it shares one connection
    (see :func:`_shares_connection`) is never given a second one to test with
    while it is already running: the running driver's own status is reported
    instead (§7.3 bench question 6).
    """
    device = await _device_or_404(db, device_id)
    category = Category(device.category)
    already_running = manager.running_driver(device_id) is not None
    if _shares_connection(category, device.driver_key) and already_running:
        return _running_test_response(manager.status(device_id))
    try:
        report = await manager.test(category, device.driver_key, device.config)
    except UnknownDriver as exc:
        raise ApiError(ErrorCode.DEVICE_UNAVAILABLE, str(exc)) from exc
    except ConfigValidationError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED, "The stored configuration is not valid", exc.detail
        ) from exc
    return test_response(report)


@router.get("/devices/{device_id}/capabilities", response_model=CapabilitiesResponse)
async def device_capabilities(
    _: Staff, db: Db, manager: Devices, device_id: int
) -> CapabilitiesResponse:
    device = await _device_or_404(db, device_id)
    driver, as_connected = await _resolve(manager, device_id)
    return CapabilitiesResponse(
        category=device.category,
        as_connected=as_connected,
        capabilities=_as_dict(driver.capabilities()),
    )


@router.get("/devices/{device_id}/refs")
async def device_refs(_: Admin, db: Db, manager: Devices, device_id: int) -> dict[str, Any]:
    """``available_refs()`` — what the admin picker offers (§5.5, §7.5)."""
    await _device_or_404(db, device_id)
    driver, as_connected = await _resolve(manager, device_id)
    available_refs = getattr(driver, "available_refs", None)
    if available_refs is None:
        raise _unsupported("addressable references")
    refs = available_refs()
    if isinstance(refs, MatrixRefs):
        return {
            "as_connected": as_connected,
            "inputs": [_ref_model(ref) for ref in refs.inputs],
            "outputs": [_ref_model(ref) for ref in refs.outputs],
        }
    return {"as_connected": as_connected, "refs": [_ref_model(ref) for ref in refs]}


@router.get("/devices/{device_id}/manifest")
async def device_manifest(_: Admin, db: Db, manager: Devices, device_id: int) -> dict[str, Any]:
    """A control surface's physical layout (§5.5). Any other category is a 404."""
    await _device_or_404(db, device_id)
    driver, as_connected = await _resolve(manager, device_id)
    manifest = getattr(driver, "manifest", None)
    if manifest is None:
        raise _unsupported("a control-surface manifest")
    return {"as_connected": as_connected, "controls": [asdict(control) for control in manifest()]}


@router.get("/devices/{device_id}/fader-law")
async def device_fader_law(
    _: AnySession, db: Db, manager: Devices, device_id: int
) -> dict[str, Any]:
    """The position↔dB law, with printed labels and detents (§5.5). Mixers only."""
    await _device_or_404(db, device_id)
    driver, as_connected = await _resolve(manager, device_id)
    fader_law = getattr(driver, "fader_law", None)
    if fader_law is None:
        raise _unsupported("a fader law")
    # §5.5 publishes the table under "fader_law"; the screens read that key.
    return {
        "as_connected": as_connected,
        "fader_law": [asdict(point) for point in fader_law()],
    }


@router.get("/devices/{device_id}/meter-scale")
async def device_meter_scale(
    _: AnySession, db: Db, manager: Devices, device_id: int
) -> dict[str, Any]:
    """Meter range and measurement point — 404 where metering is unavailable (§5.5).

    Where there is no metering the interface shows nothing at all; a fader
    position must never stand in for a meter (§21.9, B58).
    """
    await _device_or_404(db, device_id)
    driver, as_connected = await _resolve(manager, device_id)
    capabilities = driver.capabilities()
    if not isinstance(capabilities, MixerCapabilities) or not capabilities.supports_metering:
        raise _unsupported("metering")
    return {
        "as_connected": as_connected,
        "min_db": capabilities.meter_min_db,
        "max_db": capabilities.meter_max_db,
        "meter_point": capabilities.meter_point,
    }


@router.get("/devices/{device_id}/remap", response_model=RemapResponse)
async def get_remap(_: Admin, db: Db, manager: Devices, device_id: int) -> RemapResponse:
    """Every row holding a reference to this device beside the driver's own
    references, with a proposal per old reference (§5.5 *Driver references and
    swaps*). Read after a driver change, "the driver" is the new one; the
    proposals and their limits are :mod:`proskenion.core.remap`'s."""
    return await _remap_response(db, manager, await _device_or_404(db, device_id))


@router.post("/devices/{device_id}/remap", response_model=RemapResponse)
async def apply_remap(
    _: Admin,
    snapshot: PreChangeSnapshot,
    request: Request,
    db: Db,
    bus: Bus,
    manager: Devices,
    device_id: int,
    body: Annotated[RemapApply, Body()],
) -> RemapResponse:
    """Apply the admin's choices in one transaction, all or nothing (§5.5).

    A mixer channel given no reference, or not named at all, is marked
    unmapped and kept; a matrix row must be mapped, and a request that leaves
    one out is refused with nothing changed. Re-pointing every reference a
    device's configuration holds is a pre-change action.
    """
    await snapshot(f"re-map device {device_id}'s references")
    device = await _device_or_404(db, device_id)
    driver, _as_connected = await _resolve(manager, device_id)
    available = Available.from_driver(_available_refs(driver))
    holders = await remap_crud.list_holders(db, device_id)
    chosen: dict[tuple[remap_crud.Holder, int], list[str] | None] = {}
    for item in body.mappings:
        if (item.holder, item.id) in chosen:
            raise ApiError(
                ErrorCode.VALIDATION_FAILED,
                "A row is named twice in the re-mapping",
                {f"{item.holder}:{item.id}": ["named more than once"]},
            )
        chosen[(item.holder, item.id)] = item.new_refs
    try:
        assignments = resolve(holders, chosen, available)
    except RemapRejected as exc:
        raise ApiError(ErrorCode.VALIDATION_FAILED, exc.message, exc.detail) from exc
    await remap_crud.apply(db, assignments)
    await _announce_remap(request, bus, holders)
    return await _remap_response(db, manager, device)


def _available_refs(driver: Driver) -> object:
    available_refs = getattr(driver, "available_refs", None)
    return None if available_refs is None else available_refs()


def _available_model(refs: object) -> dict[str, Any]:
    if isinstance(refs, MatrixRefs):
        return {
            "inputs": [_ref_model(ref) for ref in refs.inputs],
            "outputs": [_ref_model(ref) for ref in refs.outputs],
        }
    if isinstance(refs, list):
        return {"refs": [_ref_model(ref) for ref in refs if isinstance(ref, ChannelRef)]}
    return {}


async def _remap_response(db: Database, manager: DeviceManager, device: Device) -> RemapResponse:
    driver, as_connected = await _resolve(manager, device.id)
    refs = _available_refs(driver)
    holders = await remap_crud.list_holders(db, device.id)
    missing = 0
    if device.category == Category.MIXER.value:
        existing = await mixer_crud.list_channels_with_refs(db, device.id)
        missing = len(uncovered(declared_desk_channels(driver), existing))
    return RemapResponse(
        device_id=device.id,
        driver_key=device.driver_key,
        as_connected=as_connected,
        mappings=[
            RemapRow(
                holder=proposal.holder.holder,
                id=proposal.holder.id,
                name=proposal.holder.name,
                kind=proposal.holder.kind,
                unmapped=proposal.holder.unmapped,
                old_refs=list(proposal.holder.refs),
                new_refs=list(proposal.proposed),
            )
            for proposal in propose(holders, Available.from_driver(refs))
        ],
        available=_available_model(refs),
        missing_channels=missing,
    )


async def _announce_remap(
    request: Request, bus: EventBus, holders: list[remap_crud.RefHolder]
) -> None:
    """Tell the services that read these rows, and settle what a hirer reaches
    before answering: an unmapped channel is excluded from hirer access (§15.6)."""
    kinds = {holder.holder for holder in holders}
    if remap_crud.MIXER_CHANNEL in kinds:
        bus.emit(MixerConfigChanged(reason="remapped"))
        await settle_hirer_permissions(request, "remapped")
    if kinds & {remap_crud.MATRIX_INPUT, remap_crud.MATRIX_OUTPUT}:
        bus.emit(VideoConfigChanged(reason="remapped"))
