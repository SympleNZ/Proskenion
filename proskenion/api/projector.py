"""Projector: state, power and input (spec §16.5, §7.4).

Exactly the contract fixed in ``docs/plans/phase-3-contracts.md``'s Projector
section — the Operator view is built against it.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from proskenion.api.deps import get_devices, get_projector, require_staff
from proskenion.api.errors import ApiError, ErrorCode
from proskenion.core.auth import TokenClaims
from proskenion.core.devices import DeviceManager
from proskenion.core.drivers.capabilities import ProjectorCapabilities
from proskenion.core.projector import (
    NoProjectorConfigured,
    ProjectorService,
    ProjectorUnavailable,
    UnknownProjectorInput,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["projector"])

Staff = Annotated[TokenClaims, Depends(require_staff)]
Devices = Annotated[DeviceManager, Depends(get_devices)]
ProjectorSvc = Annotated[ProjectorService, Depends(get_projector)]

#: PJLink's own input-type digit (§5.5, §7.4), decoded here for the operator
#: screen only — the reference itself stays opaque to the core (B59). A code
#: this table does not recognise falls back to the raw reference.
_INPUT_TYPE_LABELS: dict[str, str] = {
    "1": "RGB",
    "2": "Video",
    "3": "Digital",
    "4": "Storage",
    "5": "Network",
}


def describe_input(ref: str) -> str:
    """A human label for a PJLink input reference, e.g. ``"31"`` -> ``"Digital 1"``."""
    if len(ref) != 2 or not ref.isdigit():
        return ref
    type_label = _INPUT_TYPE_LABELS.get(ref[0])
    return ref if type_label is None else f"{type_label} {ref[1]}"


class InputModel(BaseModel):
    ref: str
    label: str


class ProjectorStateResponse(BaseModel):
    device_id: int | None
    state: str | None
    input_ref: str | None
    inputs: list[InputModel]
    #: Always ``null`` on PJLink Class 1, which has no remaining-time report
    #: (§21.14); carried for a future projector that does.
    remaining_s: float | None = None


class PowerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    on: bool


class InputBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: str


async def _state_response(
    service: ProjectorService, devices: DeviceManager
) -> ProjectorStateResponse:
    """The current §16.5 response — the service's own snapshot, plus the
    input list a capability lookup adds (never stored in ``state.projector``
    itself, since capabilities are a driver property, not live state)."""
    snapshot = service.snapshot()
    if snapshot.device_id is None:
        return ProjectorStateResponse(
            device_id=None, state=None, input_ref=None, inputs=[], remaining_s=None
        )
    report = await devices.capabilities(snapshot.device_id)
    caps = report.capabilities
    assert isinstance(caps, ProjectorCapabilities)
    inputs = [InputModel(ref=ref, label=describe_input(ref)) for ref in caps.inputs]
    return ProjectorStateResponse(
        device_id=snapshot.device_id,
        state=snapshot.state,
        input_ref=snapshot.input_ref,
        inputs=inputs,
        remaining_s=None,
    )


def _unavailable_error(exc: ProjectorUnavailable) -> ApiError:
    detail: dict[str, Any] = {"state": exc.state}
    if exc.reason is not None:
        detail["reason"] = exc.reason
    return ApiError(ErrorCode.DEVICE_UNAVAILABLE, "The projector is not available", detail)


def _no_projector_error() -> ApiError:
    return ApiError(
        ErrorCode.NOT_FOUND, "No projector is configured", {"reason": "no_projector"}
    )


@router.get("/projector/state", response_model=ProjectorStateResponse)
async def get_projector_state(
    _: Staff, service: ProjectorSvc, devices: Devices
) -> ProjectorStateResponse:
    return await _state_response(service, devices)


@router.post("/projector/power", response_model=ProjectorStateResponse)
async def set_projector_power(
    _: Staff, service: ProjectorSvc, devices: Devices, body: PowerBody
) -> ProjectorStateResponse:
    try:
        await service.set_power(body.on)
    except NoProjectorConfigured as exc:
        raise _no_projector_error() from exc
    except ProjectorUnavailable as exc:
        raise _unavailable_error(exc) from exc
    return await _state_response(service, devices)


@router.post("/projector/input", response_model=ProjectorStateResponse)
async def set_projector_input(
    _: Staff, service: ProjectorSvc, devices: Devices, body: InputBody
) -> ProjectorStateResponse:
    try:
        await service.set_input(body.input)
    except NoProjectorConfigured as exc:
        raise _no_projector_error() from exc
    except UnknownProjectorInput as exc:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "The projector does not have that input",
            {"input": ["unknown"]},
        ) from exc
    except ProjectorUnavailable as exc:
        raise _unavailable_error(exc) from exc
    return await _state_response(service, devices)


__all__ = ["describe_input", "router"]
