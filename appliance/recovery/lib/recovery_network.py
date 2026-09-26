"""Network settings for the recovery environment itself (§13.7).

Not `system.json`, not `auditorium-config-apply` — this is the recovery
image's *own* addressing, set with NetworkManager (already required on the
image, §13.7's tool list) so the web interface and console menu can be
reached at all when the appliance's usual address is unknown or the VLAN has
changed. `nmcli`'s own connection profile (one, named `recovery`) is kept
deliberately separate from the main appliance's `auditorium.nmconnection`:
they are never the same file, on two different filesystems, on two different
operating systems.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Any

_CIDR_RE = re.compile(r"\A\d{1,3}(\.\d{1,3}){3}/\d{1,2}\Z")
_IP_RE = re.compile(r"\A\d{1,3}(\.\d{1,3}){3}\Z")

CONNECTION_NAME = "recovery"


class NetworkConfigError(ValueError):
    """The requested network settings are not ones this module will apply."""


def _check_octets(address: str, *, what: str) -> None:
    for octet in address.split(".")[:4]:
        if not 0 <= int(octet) <= 255:
            raise NetworkConfigError(f"{what} has an octet out of range: {address!r}")


def check_cidr(address: str) -> str:
    """`a.b.c.d/n` with every field in range. Raises :class:`NetworkConfigError`."""
    if not _CIDR_RE.match(address):
        raise NetworkConfigError(f"not an IPv4 CIDR address: {address!r}")
    host, prefix = address.split("/")
    _check_octets(host, what="the address")
    if not 0 <= int(prefix) <= 32:
        raise NetworkConfigError(f"prefix length out of range: {address!r}")
    return address


def check_ip(address: str, *, what: str) -> str:
    if not _IP_RE.match(address):
        raise NetworkConfigError(f"{what} is not an IPv4 address: {address!r}")
    _check_octets(address, what=what)
    return address


@dataclass(frozen=True, slots=True)
class StaticSettings:
    device: str
    address: str  # CIDR, "10.2.30.50/24"
    gateway: str
    dns: tuple[str, ...]


def static_settings(
    *, device: str, address: str, gateway: str, dns: tuple[str, ...]
) -> StaticSettings:
    """Validate every field before anything is built into an `nmcli` command."""
    if not device:
        raise NetworkConfigError("no network device given")
    checked_address = check_cidr(address)
    checked_gateway = check_ip(gateway, what="the gateway")
    checked_dns = tuple(check_ip(entry, what="a DNS server") for entry in dns)
    return StaticSettings(
        device=device, address=checked_address, gateway=checked_gateway, dns=checked_dns
    )


def static_args(settings: StaticSettings) -> list[str]:
    """The `nmcli` invocation that applies `settings` as one connection profile.

    One `con add` covers the whole profile: recreating it from scratch on
    every apply (`con delete` first, ignored if absent) is simpler and safer
    than reasoning about which fields an in-place `con mod` needs to change,
    for a connection this module owns exclusively.
    """
    return [
        "nmcli",
        "con",
        "add",
        "type",
        "ethernet",
        "con-name",
        CONNECTION_NAME,
        "ifname",
        settings.device,
        "ipv4.method",
        "manual",
        "ipv4.addresses",
        settings.address,
        "ipv4.gateway",
        settings.gateway,
        "ipv4.dns",
        ",".join(settings.dns) if settings.dns else "",
    ]


def dhcp_args(device: str) -> list[str]:
    if not device:
        raise NetworkConfigError("no network device given")
    return [
        "nmcli",
        "con",
        "add",
        "type",
        "ethernet",
        "con-name",
        CONNECTION_NAME,
        "ifname",
        device,
        "ipv4.method",
        "auto",
    ]


def apply_static(settings: StaticSettings, *, run: Any = subprocess.run) -> None:
    run(["nmcli", "con", "delete", CONNECTION_NAME], capture_output=True, check=False)
    result = run(static_args(settings), capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise NetworkConfigError(f"nmcli refused the static profile: {result.stderr.strip()}")
    run(["nmcli", "con", "up", CONNECTION_NAME], capture_output=True, check=False)


def apply_dhcp(device: str, *, run: Any = subprocess.run) -> None:
    run(["nmcli", "con", "delete", CONNECTION_NAME], capture_output=True, check=False)
    result = run(dhcp_args(device), capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise NetworkConfigError(f"nmcli refused the DHCP profile: {result.stderr.strip()}")
    run(["nmcli", "con", "up", CONNECTION_NAME], capture_output=True, check=False)


def current_devices(*, run: Any = subprocess.run) -> list[tuple[str, str, str]]:
    """`(device, type, state)` for every device `nmcli` reports, ethernet and Wi-Fi both."""
    result = run(
        ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device", "status"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    devices: list[tuple[str, str, str]] = []
    for line in result.stdout.splitlines():
        fields = line.split(":")
        if len(fields) >= 3:
            devices.append((fields[0], fields[1], fields[2]))
    return devices
