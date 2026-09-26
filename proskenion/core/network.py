"""Network configuration: validation, and the §10.8 reconnection flow (contracts §5).

Three things live here, kept together because they share one document —
``system.json``'s ``hostname`` and ``network.address``/``gateway``/``dns``
keys (contracts §4) — and one small marker file beside it.

**Validation** (:func:`validate`, §21.24's Network card): format, the
gateway on the subnet, and no conflict with a configured device's address
(§3.1) — all checked *before* anything is written, exactly as §21.24 asks
("Apply stays disabled until valid").

**The pending-change marker** (:func:`begin_change`, :func:`read_pending`,
:func:`confirm_change`) — ``system.json``'s directory holds one more file,
``.network-revert.json``, recording what the previous settings were, a
``confirm_token``, and the deadline. Both this module and
``auditorium-helper --check-network-revert`` (which does the actual
reverting — see that script's ``check_network_revert``) read and write it as
a plain JSON file; no privilege is needed for the bookkeeping, only for
making a reverted address live again, which goes through the helper the
same as applying one does.

**Applying** (:func:`apply_change`) — writes the new settings and the
marker, then *asks* the helper to render and reload them
(``HelperClient.submit``, not ``.run``): ``POST /system/network`` answers
``202`` without waiting for the apply to finish, because the browser is
about to be sent to the reconnection page regardless (§10.8) — waiting here
would only hold the HTTP response open for no one to read.

**Why the revert timer is not in this module's memory.** Confirm-or-revert
(§10.8's third bullet) has to hold even when the application that started it
has crashed, been redeployed, or the machine has rebooted mid-window — "a
headless appliance must never be strandable behind a typo'd address." An
``asyncio`` timer here would not survive any of those. So the deadline is
written to disk (:func:`begin_change`, before this module ever calls the
helper) and enforced by a systemd timer running
``auditorium-helper --check-network-revert`` independently of this process,
on a schedule that also fires shortly after every boot. This module's job
ends at writing the marker and asking the helper to apply — the actual
revert is root-side and this process's health is irrelevant to whether it
happens.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from proskenion.core import certs, system_config
from proskenion.core.cloudflare import CloudflareClient, CloudflareError
from proskenion.core.elapsed import elapsed_after
from proskenion.core.helper import HelperClient
from proskenion.core.secrets import DeviceSecret

log = logging.getLogger(__name__)

#: §10.8: three minutes to confirm, or the helper restores the previous settings.
CONFIRM_WINDOW = timedelta(minutes=3)

#: Same shape auditorium-config-apply uses for a hostname (§4.3).
HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

_PENDING_FILENAME = ".network-revert.json"


class NetworkValidationError(ValueError):
    """Per-field validation errors, in the §16.1 ``validation_failed`` shape."""

    def __init__(self, fields: dict[str, list[str]]) -> None:
        super().__init__("the network configuration is not valid")
        self.fields = fields


@dataclass(frozen=True, slots=True)
class NetworkSettings:
    """A validated §21.24 Network card submission."""

    hostname: str
    address: str  # dotted quad, no mask
    prefix_length: int
    gateway: str
    dns: list[str] = field(default_factory=list)

    @property
    def cidr(self) -> str:
        """``"10.2.30.251/24"`` — the shape ``system.json``'s ``network.address`` stores."""
        return f"{self.address}/{self.prefix_length}"


def validate(
    *,
    hostname: str,
    address: str,
    prefix_length: int,
    gateway: str,
    dns: Sequence[str],
    device_addresses: Iterable[str] = (),
) -> NetworkSettings:
    """§21.24's Network card validation, entirely before anything is applied.

    Checks format for every field, that the gateway sits on the submitted
    subnet, and that the address does not collide with a configured
    device's (§3.1) — raises :class:`NetworkValidationError` naming every
    field that failed, never just the first.
    """
    errors: dict[str, list[str]] = {}

    hostname_clean = hostname.strip().lower()
    if not HOSTNAME_RE.match(hostname_clean):
        errors.setdefault("hostname", []).append(
            "Enter a valid hostname (lowercase letters, digits and hyphens)"
        )

    if not 0 <= prefix_length <= 32:
        errors.setdefault("prefix_length", []).append("Enter a mask between /0 and /32")

    interface: ipaddress.IPv4Interface | None = None
    try:
        interface = ipaddress.IPv4Interface(f"{address}/{prefix_length}")
    except ValueError:
        errors.setdefault("address", []).append("Enter a valid IPv4 address")

    gateway_ip: ipaddress.IPv4Address | None = None
    try:
        gateway_ip = ipaddress.IPv4Address(gateway)
    except ValueError:
        errors.setdefault("gateway", []).append("Enter a valid gateway address")

    if interface is not None and gateway_ip is not None and gateway_ip not in interface.network:
        errors.setdefault("gateway", []).append(
            f"The gateway must be on {interface.network}"
        )

    dns_clean: list[str] = []
    for entry in dns:
        try:
            dns_clean.append(str(ipaddress.IPv4Address(entry)))
        except ValueError:
            errors.setdefault("dns", []).append(f"{entry!r} is not a valid DNS server address")
    if not dns_clean and not errors.get("dns"):
        errors.setdefault("dns", []).append("At least one DNS server is required")

    if interface is not None and str(interface.ip) in set(device_addresses):
        errors.setdefault("address", []).append(
            "This address is already used by a configured device (§3.1)"
        )

    if errors:
        raise NetworkValidationError(errors)

    assert interface is not None and gateway_ip is not None  # every field checked above
    return NetworkSettings(
        hostname=hostname_clean,
        address=str(interface.ip),
        prefix_length=interface.network.prefixlen,
        gateway=str(gateway_ip),
        dns=dns_clean,
    )


# -- the pending-change marker -------------------------------------------------------


def _pending_path(data_dir: Path | str) -> Path:
    return system_config.system_config_path(data_dir).with_name(_PENDING_FILENAME)


@dataclass(frozen=True, slots=True)
class PendingChange:
    """What ``GET /system/network/state`` reports (contracts §5)."""

    confirm_token: str
    applied_at: str
    reverts_at: str
    previous_address: str | None


def read_pending(data_dir: Path | str) -> PendingChange | None:
    """The in-flight change, or ``None`` when nothing is pending or the
    marker is unusable (in which case it is treated as absent, never as an
    error — a malformed marker must not stop the Network card from loading)."""
    data = system_config.read_json(_pending_path(data_dir))
    if data is None:
        return None
    token = data.get("confirm_token")
    applied_at = data.get("applied_at")
    reverts_at = data.get("reverts_at")
    if not isinstance(token, str) or not isinstance(applied_at, str) or not isinstance(
        reverts_at, str
    ):
        return None
    previous = data.get("previous")
    previous_network = previous.get("network") if isinstance(previous, dict) else None
    previous_address: str | None = None
    if isinstance(previous_network, dict):
        candidate = previous_network.get("address")
        if isinstance(candidate, str):
            previous_address = candidate
    return PendingChange(
        confirm_token=token,
        applied_at=applied_at,
        reverts_at=reverts_at,
        previous_address=previous_address,
    )


def begin_change(
    data_dir: Path | str, settings: NetworkSettings, *, now: datetime | None = None
) -> str:
    """Write the new settings to ``system.json`` and a revert marker beside
    it (contracts §5 steps 1 and 4), and return the ``confirm_token``.

    This happens before anything asks the helper to apply the change, so the
    three-minute deadline is on disk — and enforceable by
    ``auditorium-helper --check-network-revert`` independently of this
    process — from the instant this function returns, whether or not the
    request that called it ever gets to respond (see the module docstring).
    """
    moment = now or datetime.now().astimezone()
    current = system_config.read(data_dir)
    raw_network = current.get("network")
    current_network: dict[str, object] = raw_network if isinstance(raw_network, dict) else {}
    previous = {
        "hostname": current.get("hostname"),
        "network": {
            key: current_network[key]
            for key in ("address", "gateway", "dns")
            if key in current_network
        },
    }
    token = str(uuid.uuid4())
    # Three real minutes: the helper compares instants, and a wall-clock
    # window would be an hour long, or already over, across a daylight-saving
    # change.
    reverts_at = elapsed_after(moment, CONFIRM_WINDOW)

    system_config.merge(
        data_dir,
        {
            "hostname": settings.hostname,
            "network": {
                **current_network,
                "address": settings.cidr,
                "gateway": settings.gateway,
                "dns": settings.dns,
            },
        },
    )
    system_config.write_json(
        _pending_path(data_dir),
        {
            "confirm_token": token,
            "applied_at": moment.isoformat(timespec="seconds"),
            "reverts_at": reverts_at.isoformat(timespec="seconds"),
            "previous": previous,
        },
    )
    return token


def confirm_change(data_dir: Path | str, confirm_token: str) -> bool:
    """Keep the new address (contracts §5 step 4): cancel the pending revert.

    ``True`` when a matching pending change existed and was cleared; ``False``
    when there was nothing pending, or the token did not match (an admin
    confirming a change that was already reverted, or a stale link) —
    :mod:`proskenion.api.network` turns that into ``404``.
    """
    path = _pending_path(data_dir)
    pending = system_config.read_json(path)
    if pending is None or pending.get("confirm_token") != confirm_token:
        return False
    system_config.delete_file(path)
    return True


# -- applying -------------------------------------------------------------------------


async def _update_dns(
    data_dir: Path | str, secret: DeviceSecret, hostname: str, address: str
) -> bool:
    """Contracts §5 step 3: point ``hostname`` at ``address`` before the
    change is applied, so the hostname resolves to the new address as soon
    as it takes effect. ``True`` only on a confirmed update — no token
    configured, or a Cloudflare failure, both come back ``False`` rather
    than raising: DNS is secondary to the address change itself, and a
    Cloudflare hiccup must not be the reason a network change cannot be
    applied. ``False`` is exactly the "no token" case §10.8 describes: the
    reconnection page falls back to the bare new address and says DNS
    needs updating manually.
    """
    path = Path(data_dir)
    if not await certs.token_configured(path):
        return False
    try:
        token = await certs.load_token(path, secret)
    except certs.TokenError as exc:
        log.warning("could not read the stored Cloudflare token: %s", exc)
        return False
    if not token:
        return False

    def _do() -> None:
        with CloudflareClient(token) as client:
            zone_id = client.zone_id_for(hostname)
            client.upsert_a_record(zone_id, hostname, address)

    try:
        await asyncio.to_thread(_do)
    except CloudflareError as exc:
        log.warning("could not update the DNS A record for %s: %s", hostname, exc)
        return False
    return True


@dataclass(frozen=True, slots=True)
class NetworkChange:
    confirm_token: str
    applied_at: str
    reverts_at: str
    dns_updated: bool


async def apply_change(
    data_dir: Path | str,
    helper: HelperClient,
    settings: NetworkSettings,
    *,
    secret: DeviceSecret | None = None,
) -> NetworkChange:
    """Contracts §5 steps 1 and 3: update DNS first when a token is
    configured, write the change, then ask the helper to apply it. Does not
    wait for the apply to finish — see the module docstring."""
    dns_updated = False
    if secret is not None:
        dns_updated = await _update_dns(data_dir, secret, settings.hostname, settings.address)
    token = begin_change(data_dir, settings)
    await helper.submit("apply-network")
    pending = read_pending(data_dir)
    assert pending is not None and pending.confirm_token == token  # just written, above
    return NetworkChange(
        confirm_token=pending.confirm_token,
        applied_at=pending.applied_at,
        reverts_at=pending.reverts_at,
        dns_updated=dns_updated,
    )
