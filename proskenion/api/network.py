"""``/system/network*`` — the §21.24 Network card and §10.8's reconnection
flow (contracts §4, §5).

``POST /system/network`` is the one endpoint here that changes anything: it
validates, writes the new settings and a revert marker
(:func:`proskenion.core.network.apply_change`), and asks the helper to make
them live — then answers ``202`` without waiting, because the browser is
about to be sent to ``http://{old_address}/reconnect`` regardless (a static
page from the root image, not this application — see
``appliance/share/auditorium/reconnect/index.html``). ``POST
.../confirm`` and ``GET .../state`` are the other two legs of that flow;
what actually reverts an unconfirmed change is root-side and independent of
this process — see :mod:`proskenion.core.network`'s module docstring.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from proskenion.api.deps import get_config, get_db, get_helper, require_admin
from proskenion.api.errors import ApiError, ErrorCode
from proskenion.config import Config
from proskenion.core import network, system_config
from proskenion.core.helper import HelperClient
from proskenion.core.secrets import DEFAULT_SECRET_PATH, DeviceSecret, generate_secret_if_missing
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud

router = APIRouter(prefix="/system/network", tags=["network"])


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NetworkConfigResponse(_Payload):
    """``GET /system/network`` — the editable §21.24 Network card fields."""

    hostname: str | None
    address: str | None
    prefix_length: int | None
    gateway: str | None
    dns: list[str]


class NetworkUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hostname: str = Field(min_length=1, max_length=64)
    address: str = Field(min_length=1, max_length=64)
    prefix_length: int = Field(ge=0, le=32)
    gateway: str = Field(min_length=1, max_length=64)
    dns: list[str] = Field(min_length=1, max_length=4)


class NetworkChangeResponse(_Payload):
    """``202`` from ``POST /system/network`` (contracts §5 step 1).

    ``address`` and ``hostname`` are the values the reconnection overlay
    needs to build the ``/reconnect`` URL it sends the browser to; ``dns_updated``
    says which of §10.8's two paths applies — the hostname (a Cloudflare
    token updated the A record already) or the bare new address (no token;
    DNS needs updating manually).
    """

    confirm_token: str
    applied_at: str
    reverts_at: str
    address: str
    hostname: str
    dns_updated: bool


class NetworkConfirmBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm_token: str = Field(min_length=1, max_length=64)


class NetworkConfirmResponse(_Payload):
    confirmed: bool


class NetworkStateResponse(_Payload):
    """``GET /system/network/state`` (contracts §5)."""

    pending: bool
    applied_at: str | None
    reverts_at: str | None
    previous_address: str | None


def _device_secret(config: Config) -> DeviceSecret:
    """The same device secret ``PUT /system/certs/token`` encrypts the
    Cloudflare token with (``proskenion/core/certs.py``) — loaded here, not
    through the device manager, so this endpoint needs no running device
    subsystem (``proskenion/api/system.py``'s ``_device_secret`` is the same
    three lines, for email; duplicated rather than shared across modules
    that otherwise have nothing to do with each other)."""
    path = config.app.state_dir / DEFAULT_SECRET_PATH.name
    generate_secret_if_missing(path)
    return DeviceSecret.load(path)


def _split_address(cidr: object) -> tuple[str | None, int | None]:
    if not isinstance(cidr, str) or "/" not in cidr:
        return (cidr if isinstance(cidr, str) else None), None
    address, _, prefix = cidr.partition("/")
    try:
        return address, int(prefix)
    except ValueError:
        return address, None


@router.get("", response_model=NetworkConfigResponse, dependencies=[Depends(require_admin)])
async def get_network(config: Annotated[Config, Depends(get_config)]) -> NetworkConfigResponse:
    doc = system_config.read(config.app.data_dir)
    raw_network = doc.get("network")
    net: dict[str, object] = raw_network if isinstance(raw_network, dict) else {}
    address, prefix_length = _split_address(net.get("address"))
    dns = net.get("dns")
    hostname = doc.get("hostname")
    gateway = net.get("gateway")
    return NetworkConfigResponse(
        hostname=hostname if isinstance(hostname, str) else None,
        address=address,
        prefix_length=prefix_length,
        gateway=gateway if isinstance(gateway, str) else None,
        dns=[str(d) for d in dns] if isinstance(dns, list) else [],
    )


@router.post(
    "", response_model=NetworkChangeResponse, status_code=202, dependencies=[Depends(require_admin)]
)
async def post_network(
    body: NetworkUpdate,
    config: Annotated[Config, Depends(get_config)],
    db: Annotated[Database, Depends(get_db)],
    helper: Annotated[HelperClient, Depends(get_helper)],
) -> NetworkChangeResponse:
    """Validate, then hand over to the reconnection flow (contracts §5 step 1)."""
    rows = await devices_crud.list_all(db)
    try:
        settings = network.validate(
            hostname=body.hostname,
            address=body.address,
            prefix_length=body.prefix_length,
            gateway=body.gateway,
            dns=body.dns,
            device_addresses=system_config.device_hosts(rows),
        )
    except network.NetworkValidationError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED, "The network configuration is not valid", exc.fields
        ) from exc
    change = await network.apply_change(
        config.app.data_dir, helper, settings, secret=_device_secret(config)
    )
    return NetworkChangeResponse(
        confirm_token=change.confirm_token,
        applied_at=change.applied_at,
        reverts_at=change.reverts_at,
        address=settings.address,
        hostname=settings.hostname,
        dns_updated=change.dns_updated,
    )


@router.post(
    "/confirm", response_model=NetworkConfirmResponse, dependencies=[Depends(require_admin)]
)
async def confirm_network(
    body: NetworkConfirmBody, config: Annotated[Config, Depends(get_config)]
) -> NetworkConfirmResponse:
    """Keep the new address (contracts §5 step 4): cancels the pending revert."""
    confirmed = network.confirm_change(config.app.data_dir, body.confirm_token)
    if not confirmed:
        raise ApiError(
            ErrorCode.NOT_FOUND, "There is no pending network change with that token"
        )
    return NetworkConfirmResponse(confirmed=True)


@router.get("/state", response_model=NetworkStateResponse, dependencies=[Depends(require_admin)])
async def network_state(config: Annotated[Config, Depends(get_config)]) -> NetworkStateResponse:
    pending = network.read_pending(config.app.data_dir)
    if pending is None:
        return NetworkStateResponse(
            pending=False, applied_at=None, reverts_at=None, previous_address=None
        )
    return NetworkStateResponse(
        pending=True,
        applied_at=pending.applied_at,
        reverts_at=pending.reverts_at,
        previous_address=pending.previous_address,
    )
