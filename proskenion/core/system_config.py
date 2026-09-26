"""``/data/config/system.json`` — the application's side (contracts §4, §18).

``appliance/bin/auditorium-config-apply`` is what makes this file's content
live — hostname, addressing, the firewall — and it runs as root through the
helper. This module is the other half: everything the *application* does
with the file, which needs no privilege at all. The file already belongs to
the application user (``appliance/image/build.sh`` seeds it 0640
``<app>:<app>``), so reading and writing it is an ordinary file operation,
the same as any other row this process owns.

Read-merge-write, the same discipline ``appliance/lib/auditorium_bootstate.py``
documents for ``boot-state.json`` (contracts §1): a writer here touches only
the keys it owns and preserves everything else untouched, including keys this
version of the application does not model at all (``management_address``,
``admin_networks``: read only by ``auditorium-config-apply``). A writer that
round-tripped the document through a fixed set of fields would silently drop
whatever it does not model, exactly the boot-state bug contracts §1 calls out.

What the application owns, and where each part comes from
---------------------------------------------------------
* ``network.smtp_relay`` — mirrored from the saved email settings
  (:func:`sync_smtp_relay`). Outbound traffic is default-drop (§3.4), and
  config-apply opens the relay's port only to the addresses this key
  resolves to. This key previously went unwritten, and mail configured in the
  admin interface timed out at connect (24 September 2026).
* ``network.backup_destination`` — mirrored from the saved network backup
  destination (:func:`backup_destination_network`), present while one is
  configured and enabled.
* ``devices`` — the whole list (:func:`sync_devices`), **derived** on every
  write from the source of truth that owns each entry:

  - the ``devices`` table, one entry per networked device, in the
    ``{"name", "address", "ports", "listen_ports"}`` shape config-apply's
    firewall renderer expects;
  - the KNX gateway, from knxd's own configuration
    (``/data/config/knxd.conf``'s ``-b ipt:HOST[:PORT]``). KNX is a
    subsystem, not a device row (§5.5, B42), so the device table never
    carries it — and when the mirror replaced the image's list with the
    device table alone, the gateway's udp/3671 rule went with it;
  - the control surface, from ``network.control_surface_address`` (§3.3,
    §7.6), the key config-apply already reads for the surface's inbound
    rule.

  Why derive rather than preserve "entries the application did not write":
  the image's list also holds §3.1 placeholders (projector, mixer, DMX node)
  that the device table exists to replace, and a preserved placeholder is a
  stale rule to an address nothing uses — the very thing this derivation
  exists to remove. And an appliance already running an earlier version has *lost*
  its gateway entry; only deriving it brings it back. Anything else that
  needs a rule reaches the firewall through its own source: a device row,
  knxd.conf, or the network settings.

:func:`read` and :func:`merge` are a small, file-scoped counterpart to
``auditorium_bootstate``'s, without the cross-process ``flock`` that module
needs: this file has effectively one writer at a time in practice (an admin
session), and the cost of a rare lost update is a config re-save, not a
corrupted boot record.
"""

from __future__ import annotations

import asyncio
import functools
import ipaddress
import json
import logging
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from proskenion.core.drivers import registry
from proskenion.core.drivers.base import Driver
from proskenion.core.drivers.categories import Category
from proskenion.core.drivers.registry import UnknownDriver
from proskenion.core.helper import HelperClient
from proskenion.core.knx import KNXNET_IP_PORT
from proskenion.db.connection import Database
from proskenion.db.crud import backup as backup_crud
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import email as email_crud
from proskenion.db.crud.devices import Device

log = logging.getLogger(__name__)

#: Matches build.sh's seed (0640, owned by the application user).
SYSTEM_CONFIG_MODE = 0o640

#: The entries derived beside the device table. The names are the image's own
#: (build.sh's seed, config-apply's DEFAULT_DEVICES), so the rule comments in
#: /etc/nftables.conf read the same before and after the first device save.
KNX_GATEWAY_NAME: Final = "knx_gateway"
CONTROL_SURFACE_NAME: Final = "control_surface"
#: config-apply's DEFAULT_CONTROL_SURFACE: what an absent
#: ``network.control_surface_address`` means to it, so it means that here too.
DEFAULT_CONTROL_SURFACE: Final = "10.2.30.100"
#: §3.3, §7.6: RTP-MIDI, "UDP 5004 (control), 5005 (data)".
CONTROL_SURFACE_PORTS: Final = "udp/5004-5005"

#: knxd's tunnelling backend, ``-b ipt:HOST[:PORT[...]]`` (``iptn`` is the
#: same tunnel through NAT). Routing (``ip:``, multicast) has no one gateway
#: address to open a rule to, so it is not matched.
_KNXD_TUNNEL_RE = re.compile(r"(?:^|[\s\"'])-b\s*iptn?:([^\s:\"']+)(?::(\d{1,5}))?")

_NAME_RE = re.compile(r"[^A-Za-z0-9_-]")


def system_config_path(data_dir: Path | str) -> Path:
    """``<data_dir>/config/system.json`` — ``/data/config/system.json`` on the appliance."""
    return Path(data_dir) / "config" / "system.json"


# -- generic JSON read/write --------------------------------------------------------


def read_json(path: Path) -> dict[str, Any] | None:
    """The document at ``path``, or ``None`` if it is absent or not a JSON object."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def write_json(path: Path, data: Mapping[str, Any]) -> None:
    """Write ``data`` atomically: a temporary file in the same directory,
    ``fsync``, then ``rename`` — never seen half-written (§4.3's pattern,
    matching ``auditorium_bootstate``'s and ``auditorium-config-apply``'s)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, SYSTEM_CONFIG_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, SYSTEM_CONFIG_MODE)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def delete_file(path: Path) -> None:
    """Remove ``path`` if it exists. Never raises for an already-absent file."""
    try:
        os.unlink(path)
    except OSError:
        pass


# -- system.json itself --------------------------------------------------------------


def read(data_dir: Path | str) -> dict[str, Any]:
    """The whole document, or ``{}`` when it is absent or unreadable — the
    same "safe with the file absent" rule ``auditorium-config-apply`` follows."""
    return read_json(system_config_path(data_dir)) or {}


def merge(data_dir: Path | str, changes: Mapping[str, Any]) -> dict[str, Any]:
    """Replace only the top-level keys named in ``changes``; every other key
    — including ones this version of the application does not model —
    survives untouched (see the module docstring). Returns the document as
    written."""
    path = system_config_path(data_dir)
    doc = read_json(path) or {}
    doc.update(changes)
    write_json(path, doc)
    return doc


# -- device table mirroring (contracts §4, the Phase 1 gap) -------------------------


def device_hosts(devices: Iterable[Device]) -> set[str]:
    """Every address a configured device's transport already uses.

    Independent of :func:`device_table_entry`'s firewall-row rules: used by
    the §21.24 Network card's "no conflict with a device address from §3.1"
    check, where a device with no valid port still occupies its address.
    """
    hosts: set[str] = set()
    for device in devices:
        transport = device.config.get("transport") if isinstance(device.config, Mapping) else None
        if not isinstance(transport, Mapping):
            continue
        host = transport.get("host")
        if isinstance(host, str) and host.strip():
            hosts.add(host.strip())
    return hosts


def _driver_class(device: Device) -> type[Driver] | None:
    try:
        category = Category(device.category)
    except ValueError:
        return None
    try:
        return registry.get(category, device.driver_key)
    except UnknownDriver:
        return None


def device_table_entry(device: Device) -> dict[str, Any] | None:
    """One §3.1 device-table row for ``device``, or ``None`` when it has
    nothing to firewall — a serial or loopback transport has no network
    address, and a device with neither a usable port nor a driver-declared
    extra one (:meth:`~proskenion.core.drivers.base.Driver.firewall_ports`)
    has no rule to write.
    """
    transport = device.config.get("transport") if isinstance(device.config, Mapping) else None
    if not isinstance(transport, Mapping):
        return None
    transport_type = transport.get("type")
    host = transport.get("host")
    port = transport.get("port")
    if transport_type not in ("tcp", "udp") or not isinstance(host, str) or not host.strip():
        return None  # serial/loopback/unix: no network address, no firewall entry

    ports: list[str] = []
    if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535:
        ports.append(f"{transport_type}/{port}")

    listen_ports: list[str] = []
    driver_cls = _driver_class(device)
    if driver_cls is not None:
        extra = driver_cls.firewall_ports(device.config)
        ports.extend(extra.outbound)
        listen_ports.extend(extra.inbound)

    if not ports and not listen_ports:
        return None

    name = _NAME_RE.sub("_", device.name)[:32] or f"device{device.id}"
    entry: dict[str, Any] = {"name": name, "address": host.strip(), "ports": ports}
    if listen_ports:
        entry["listen_ports"] = listen_ports
    return entry


def device_table(devices: Iterable[Device]) -> list[dict[str, Any]]:
    """The §3.1 device table for every row that needs one, in DB order."""
    entries: list[dict[str, Any]] = []
    for device in devices:
        entry = device_table_entry(device)
        if entry is not None:
            entries.append(entry)
    return entries


# -- the entries that are not device rows ----------------------------------------------


def knxd_conf_path(data_dir: Path | str) -> Path:
    """``<data_dir>/config/knxd.conf`` — reached from ``/etc/knxd.conf`` (§4.3, §4.11)."""
    return Path(data_dir) / "config" / "knxd.conf"


def knx_gateway_from_knxd(text: str) -> dict[str, Any] | None:
    """The firewall entry for the gateway knxd tunnels to, or ``None`` when
    this configuration names no tunnelling gateway. Comments are ignored; the
    port is knxd's default, KNXnet/IP's 3671, unless one is given."""
    for raw in text.splitlines():
        match = _KNXD_TUNNEL_RE.search(raw.split("#", 1)[0])
        if match is None:
            continue
        host, port_text = match[1], match[2]
        try:
            address = str(ipaddress.IPv4Address(host))
        except ValueError:
            # config-apply opens rules to literal IPv4 addresses only (§3.1).
            log.warning("knxd.conf tunnels to %r, which is not an IPv4 address: no rule", host)
            return None
        port = int(port_text) if port_text else KNXNET_IP_PORT
        if not 1 <= port <= 65535:
            log.warning("knxd.conf names port %d for the KNX gateway: no rule", port)
            return None
        return {"name": KNX_GATEWAY_NAME, "address": address, "ports": [f"udp/{port}"]}
    return None


def _entry_named(doc: Mapping[str, Any], name: str) -> dict[str, Any] | None:
    entries = doc.get("devices")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if isinstance(entry, Mapping) and entry.get("name") == name:
            return dict(entry)
    return None


def _knx_gateway_entry(data_dir: Path | str, doc: Mapping[str, Any]) -> dict[str, Any] | None:
    path = knxd_conf_path(data_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        # Unreadable is not "no gateway": keep the rule the document already
        # has rather than drop KNX at the next reconnect or reboot.
        previous = _entry_named(doc, KNX_GATEWAY_NAME)
        log.warning(
            "could not read %s (%s): %s",
            path,
            exc,
            "keeping the KNX gateway rule system.json has" if previous else "no KNX gateway rule",
        )
        return previous
    return knx_gateway_from_knxd(text)


def _control_surface_entry(doc: Mapping[str, Any]) -> dict[str, Any] | None:
    network = doc.get("network")
    settings: Mapping[str, Any] = network if isinstance(network, Mapping) else {}
    address = settings.get("control_surface_address", DEFAULT_CONTROL_SURFACE)
    if not isinstance(address, str) or not address.strip():
        return None  # set to none on purpose: config-apply writes no surface rule either
    return {
        "name": CONTROL_SURFACE_NAME,
        "address": address.strip(),
        "ports": [CONTROL_SURFACE_PORTS],
    }


def firewall_table(
    data_dir: Path | str, devices: Iterable[Device], doc: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """The whole ``devices`` key, derived (see the module docstring): the KNX
    gateway from knxd.conf, every networked device row, then the control
    surface — the image's own order."""
    table: list[dict[str, Any]] = []
    gateway = _knx_gateway_entry(data_dir, doc)
    if gateway is not None:
        table.append(gateway)
    table.extend(device_table(devices))
    surface = _control_surface_entry(doc)
    if surface is not None:
        table.append(surface)
    return table


def sync_devices(data_dir: Path | str, devices: Iterable[Device]) -> dict[str, Any]:
    """Re-derive ``system.json``'s ``devices`` key (contracts §4): the next
    ``apply-network`` re-renders the firewall from exactly this list, so a
    device saved at a new address is not left firewalled by the address it
    used to have (the Phase 1 gap), and the KNX gateway and control surface
    keep their rules through every device edit. Call this — then
    ask the helper to apply — *before* testing or waiting for the device to
    reconnect, or the new address is blocked by the stale rule the test
    would otherwise hit.
    """
    doc = read(data_dir)
    return merge(data_dir, {"devices": firewall_table(data_dir, devices, doc)})


# -- the SMTP relay (§11.4, §3.4) -------------------------------------------------------


def smtp_relay_network(
    doc: Mapping[str, Any], host: str | None, port: int | None
) -> dict[str, Any]:
    """``doc``'s ``network`` object with ``smtp_relay`` set to ``host:port``,
    or removed when either is missing — every other ``network`` key carried
    forward, as :func:`proskenion.core.network.begin_change` does."""
    network = doc.get("network")
    updated: dict[str, Any] = dict(network) if isinstance(network, Mapping) else {}
    if host and host.strip() and port:
        updated["smtp_relay"] = {"host": host.strip(), "port": int(port)}
    else:
        updated.pop("smtp_relay", None)
    return updated


def sync_smtp_relay(data_dir: Path | str, host: str | None, port: int | None) -> bool:
    """Mirror the saved relay into ``network.smtp_relay``; ``None`` for
    either clears it. ``True`` when the document changed, so the caller asks
    for ``apply-network`` only then. config-apply resolves the host and opens
    the port to those addresses alone (§3.4): without this key, mail cannot
    leave the appliance."""
    doc = read(data_dir)
    network = smtp_relay_network(doc, host, port)
    if network == doc.get("network"):
        return False
    merge(data_dir, {"network": network})
    return True


# -- the network backup destination (§13.1, §3.4) -----------------------------------------


def backup_destination_network(
    doc: Mapping[str, Any], protocol: str | None, host: str | None, enabled: bool
) -> dict[str, Any]:
    """``doc``'s ``network`` object with ``backup_destination`` mirrored from
    the saved destination — present only while one is configured and
    enabled — and every other ``network`` key carried forward."""
    network = doc.get("network")
    updated: dict[str, Any] = dict(network) if isinstance(network, Mapping) else {}
    if protocol and host and host.strip() and enabled:
        updated["backup_destination"] = {"address": host.strip(), "protocol": protocol}
    else:
        updated.pop("backup_destination", None)
    return updated


# -- start-up reconciliation -------------------------------------------------------------


class _Keep:
    """"Leave this key as it is" — distinct from ``None``, which clears it."""


KEEP: Final = _Keep()


@dataclass(frozen=True, slots=True)
class BackupDestinationSource:
    """The saved network backup destination, as :func:`reconcile` mirrors it."""

    protocol: str | None
    host: str | None
    enabled: bool


def reconcile(
    data_dir: Path | str,
    devices: Iterable[Device],
    smtp_host: str | None,
    smtp_port: int | None,
    *,
    backup_destination: BackupDestinationSource | None | _Keep = KEEP,
) -> bool:
    """Bring every key this module owns into line with its source of truth,
    and say whether anything changed.

    Run once at start-up, so an appliance that received this version as a
    package — its ``devices`` list stripped of the KNX gateway by an earlier
    version, and no ``smtp_relay`` at all — is put right without waiting for
    someone to edit a device or re-save the email settings. The same start
    ends every backup restore (the restore restarts the application), so a
    restored database's relay and backup destination reach the firewall the
    same way: a restore replaces the tables these keys are derived from and
    never ``system.json`` itself (Q15). ``backup_destination=None`` means no
    destination is saved; :data:`KEEP` leaves the key untouched. Never
    creates ``system.json``: an absent file means config-apply's §3.1
    defaults, and a partial one written here would replace them.
    """
    if not system_config_path(data_dir).is_file():
        return False
    doc = read(data_dir)
    changes: dict[str, Any] = {}
    table = firewall_table(data_dir, devices, doc)
    if table != doc.get("devices"):
        changes["devices"] = table
    network = smtp_relay_network(doc, smtp_host, smtp_port)
    if not isinstance(backup_destination, _Keep):
        saved = backup_destination
        network = backup_destination_network(
            {"network": network},
            saved.protocol if saved else None,
            saved.host if saved else None,
            saved.enabled if saved else False,
        )
    if network != doc.get("network"):
        changes["network"] = network
    if not changes:
        return False
    merge(data_dir, changes)
    return True


async def reconcile_and_apply(db: Database, data_dir: Path | str, helper: HelperClient) -> bool:
    """:func:`reconcile` from the database's rows, then ``apply-network`` if
    anything changed — the same re-render a device save asks for. Logged,
    never raised: a start-up that cannot reach the helper still serves."""
    try:
        devices = await devices_crud.list_all(db)
        email = await email_crud.get(db)
        destination = await backup_crud.get_destination(db)
        changed = await asyncio.to_thread(
            functools.partial(
                reconcile,
                data_dir,
                devices,
                email.host if email else None,
                email.port if email else None,
                backup_destination=(
                    None
                    if destination is None
                    else BackupDestinationSource(
                        protocol=destination.protocol,
                        host=destination.host,
                        enabled=destination.enabled,
                    )
                ),
            )
        )
        if changed:
            log.info("system.json was out of step with the configuration; re-applying the firewall")
            await helper.submit("apply-network")
        return changed
    except OSError as exc:
        log.warning("could not reconcile system.json with the configuration: %s", exc)
        return False
