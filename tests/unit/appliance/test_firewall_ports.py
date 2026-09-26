"""Every port every driver uses passes the appliance firewall (§3.3, §3.4).

``appliance/bin/auditorium-config-apply`` renders ``/etc/nftables.conf`` from
``/data/config/system.json``'s device address table at every boot: default
drop both ways, one rule per device port. A driver whose port is missing from
that table works on a development machine and fails on the appliance, and
Phase 1 shipped exactly that for the mixer's MIDI port.

The generator is loaded and run here (its ``render_nft``; ``nft`` itself is
not needed to render text, only to load it), for three configurations:

- **no system.json**: the generator's own §3.1 defaults, which must also be
  what ``appliance/etc/nftables.conf.default`` holds, byte for byte
- **the image's system.json**: the one ``appliance/image/build.sh`` writes to
  ``/data/config`` — what an appliance actually boots with, since the
  defaults apply only when the file has no ``devices`` key at all
- **devices at other addresses**: the defaults' devices moved onto another
  subnet, as a site whose addressing differs from §3.1's would set them

Every expected port comes from the module that uses it — the driver's or
client's own constant — never retyped here, so a port that moves in the code
fails this test until the firewall moves with it.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import ipaddress
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from proskenion.core import system_config
from proskenion.core.dmx import artnet
from proskenion.core.dmx.drivers import ArtnetDriver, SacnDriver
from proskenion.core.drivers.cq20b import CQ_MIDI_PORT, NATIVE_PORT, CQ20BDriver
from proskenion.core.drivers.pjlink import PJLINK_PORT, PJLinkDriver
from proskenion.core.knx import KNXNET_IP_PORT
from proskenion.core.mixer.native import DEFAULT_LOCAL_UDP_PORT
from proskenion.db.crud.devices import Device

APPLIANCE = Path(__file__).resolve().parents[3] / "appliance"
GENERATOR = APPLIANCE / "bin" / "auditorium-config-apply"
DEFAULT_RULESET = APPLIANCE / "etc" / "nftables.conf.default"
BUILD_SCRIPT = APPLIANCE / "image" / "build.sh"
KNXD_DEFAULT = APPLIANCE / "etc" / "knxd.conf.default"

#: The control surface's RTP-MIDI ports (§3.3, §7.6: "UDP 5004 (control),
#: 5005 (data)"). The one expectation not taken from a module: the surface
#: driver is Phase 9 and does not exist yet. When it does, its own constant
#: replaces this.
RTP_MIDI_PORTS = (5004, 5005)


def load_generator() -> ModuleType:
    """The generator as a module. It is a script with no ``.py`` suffix, so it
    is loaded by path; importing it runs nothing (its ``main`` is guarded)."""
    loader = importlib.machinery.SourceFileLoader("auditorium_config_apply", str(GENERATOR))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    # Its dataclasses resolve their annotations through sys.modules.
    sys.modules[loader.name] = module
    # Executed from its own path, so Python would leave a __pycache__ beside
    # it — inside appliance/, where check-units.sh reads every file.
    written = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = written
    return module


@pytest.fixture(scope="module")
def generator() -> ModuleType:
    return load_generator()


# -- reading a rendered ruleset ---------------------------------------------------


@dataclass(frozen=True)
class Rule:
    chain: str  # "input" | "output"
    address: str | None  # ``ip saddr`` on input, ``ip daddr`` on output
    proto: str
    ports: tuple[tuple[int, int], ...]

    def admits(self, address: str, proto: str, port: int) -> bool:
        if proto != self.proto or not any(lo <= port <= hi for lo, hi in self.ports):
            return False
        if self.address is None:
            return True
        return ipaddress.ip_address(address) in ipaddress.ip_network(self.address, strict=False)


_RULE = re.compile(
    r"^(?:ip (?P<dir>saddr|daddr) (?P<addr>\S+) )?(?P<proto>tcp|udp) dport "
    r"(?P<ports>\{[^}]*\}|\S+) accept"
)


def _port_ranges(spec: str) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    for part in spec.strip("{} ").split(","):
        lo, _, hi = part.strip().partition("-")
        ranges.append((int(lo), int(hi or lo)))
    return tuple(ranges)


def parse_rules(ruleset: str) -> list[Rule]:
    """The port rules of the ``input`` and ``output`` chains — enough nft to
    answer "is this admitted", not a general parser."""
    rules: list[Rule] = []
    chain: str | None = None
    for raw in ruleset.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line.startswith("chain "):
            chain = line.split()[1]
            continue
        match = _RULE.match(line)
        if chain in ("input", "output") and match:
            direction = match["dir"]
            assert direction is None or direction == ("saddr" if chain == "input" else "daddr")
            rules.append(Rule(chain, match["addr"], match["proto"], _port_ranges(match["ports"])))
    return rules


def outbound(rules: list[Rule], address: str, proto: str, port: int) -> bool:
    return any(r.chain == "output" and r.admits(address, proto, port) for r in rules)


def inbound(rules: list[Rule], address: str, proto: str, port: int) -> bool:
    return any(r.chain == "input" and r.admits(address, proto, port) for r in rules)


# -- what the drivers use -----------------------------------------------------------


@dataclass(frozen=True)
class Flow:
    """One flow a driver needs: ``out`` to a device's port, or ``in`` from the
    device to one of ours. ``port`` ``None`` means every port — the mixer's
    meter port is negotiated per connection (cq20b-native.md §2)."""

    device: str  # the name in system.json's device table
    direction: str
    proto: str
    port: int | None
    why: str


FLOWS: tuple[Flow, ...] = (
    Flow("mixer", "out", "tcp", CQ_MIDI_PORT, "CQ-20B MIDI control (§7.3)"),
    Flow("mixer", "out", "tcp", NATIVE_PORT, "CQ-20B native metering connection (§7.3)"),
    Flow("mixer", "out", "udp", None, "native keep-alives to the desk's negotiated port"),
    Flow("mixer", "in", "udp", DEFAULT_LOCAL_UDP_PORT, "meters to our fixed port (§3.3)"),
    Flow("projector", "out", "tcp", PJLINK_PORT, "PJLink (§7.4)"),
    Flow("knx_gateway", "out", "udp", KNXNET_IP_PORT, "knxd's KNXnet/IP tunnel (§4.11)"),
    Flow("dmx_node", "out", "udp", artnet.ARTNET_PORT, "Art-Net output and ArtPoll (§7.2.5)"),
    Flow("dmx_node", "in", "udp", artnet.ARTNET_PORT, "ArtPollReply (§3.3)"),
    Flow("dmx_node", "out", "udp", artnet.SACN_PORT, "sACN (E1.31) output (§7.2.5)"),
    *(Flow("control_surface", "out", "udp", p, "RTP-MIDI (§7.6)") for p in RTP_MIDI_PORTS),
    *(Flow("control_surface", "in", "udp", p, "RTP-MIDI (§3.3)") for p in RTP_MIDI_PORTS),
)

#: A port no rule should admit, to prove "every port" is a real answer.
_SOME_PORT = 40000


def test_the_drivers_default_to_their_own_constants() -> None:
    """The constants above are what the drivers actually connect to."""
    assert CQ20BDriver.TRANSPORT_DEFAULTS["tcp"]["port"] == CQ_MIDI_PORT
    assert CQ20BDriver.NATIVE_PORT == NATIVE_PORT
    meter_port = next(f for f in CQ20BDriver.CONFIG_SCHEMA if f.key == "meter_udp_port")
    assert meter_port.default == DEFAULT_LOCAL_UDP_PORT
    assert PJLinkDriver.TRANSPORT_DEFAULTS["tcp"]["port"] == PJLINK_PORT
    assert ArtnetDriver.TRANSPORT_DEFAULTS["udp"]["port"] == artnet.ARTNET_PORT
    assert SacnDriver.TRANSPORT_DEFAULTS["udp"]["port"] == artnet.SACN_PORT


def assert_admits(ruleset: str, addresses: dict[str, str]) -> None:
    rules = parse_rules(ruleset)
    missing: list[str] = []
    for flow in FLOWS:
        address = addresses[flow.device]
        check = outbound if flow.direction == "out" else inbound
        ports = [flow.port] if flow.port is not None else [1, _SOME_PORT, 65535]
        for port in ports:
            if not check(rules, address, flow.proto, port):
                missing.append(f"{flow.direction} {flow.proto}/{port} {address} — {flow.why}")
    assert missing == [], "the firewall blocks:\n  " + "\n  ".join(missing)


def addresses_of(config: Any) -> dict[str, str]:
    return {device.name: device.address for device in config.devices}


# -- the three configurations ---------------------------------------------------------


def test_the_default_ruleset_admits_every_driver_port(
    generator: ModuleType, tmp_path: Path
) -> None:
    config, ok = generator.load(tmp_path / "absent.json")
    assert ok
    assert_admits(generator.render_nft(config), addresses_of(config))


def test_the_shipped_default_ruleset_is_what_the_generator_renders(
    generator: ModuleType, tmp_path: Path
) -> None:
    """``appliance/etc/nftables.conf.default`` is the ``--render`` of no
    system.json; regenerate it with ``auditorium-config-apply --render
    --config /nonexistent`` whenever the generator changes."""
    config, _ = generator.load(tmp_path / "absent.json")
    shipped = DEFAULT_RULESET.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert shipped == generator.render_nft(config)


def image_system_json() -> dict[str, Any]:
    """The system.json ``build.sh`` writes, with its shell variables filled in
    as a build with default options fills them."""
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    match = re.search(r'config/system\.json".*?<<SYS\n(.*?)\nSYS\n', script, re.DOTALL)
    assert match, "build.sh no longer writes system.json from a heredoc"
    text = (
        match[1]
        .replace("${HOSTNAME_}", "auditorium")
        .replace("${mgmt_json}", "null")
        .replace("${vlan}", "10.2.30.0/24")
        .replace("${ADDRESS}", "10.2.30.45/24")
        .replace("${GATEWAY}", "10.2.30.254")
        # build.sh turns --dns's comma-separated value into a JSON list before
        # the heredoc, so what lands here is the list, not the raw setting.
        .replace("${dns_json}", '"10.2.40.1", "122.56.237.1"')
        .replace("${DNS}", "10.2.40.1")
        # --admin-networks likewise arrives as a JSON list.
        .replace("${admin_json}", '"10.46.0.0/23", "10.2.10.0/23"')
    )
    assert "${" not in text, text
    body: dict[str, Any] = json.loads(text)
    return body


def test_the_images_system_json_admits_every_driver_port(
    generator: ModuleType, tmp_path: Path
) -> None:
    """What an appliance actually boots with: the image's system.json has a
    ``devices`` key, so the generator's defaults never apply to it."""
    path = tmp_path / "system.json"
    path.write_text(json.dumps(image_system_json()), encoding="utf-8")
    config, ok = generator.load(path)
    assert ok
    assert {d.name for d in config.devices} >= {f.device for f in FLOWS}
    assert_admits(generator.render_nft(config), addresses_of(config))


def test_devices_at_other_addresses_are_admitted_there_and_only_there(
    generator: ModuleType, tmp_path: Path
) -> None:
    """A site on another subnet: the rules follow the addresses in the table."""
    moved: list[dict[str, Any]] = []
    for index, device in enumerate(generator.DEFAULT_DEVICES):
        moved.append({**device, "address": f"10.9.40.{20 + index}"})
    raw = {
        "network": {"vlan": "10.9.40.0/24", "control_surface_address": "10.9.40.24"},
        "devices": moved,
    }
    path = tmp_path / "system.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config, ok = generator.load(path)
    assert ok
    ruleset = generator.render_nft(config)
    addresses = addresses_of(config)
    assert set(addresses.values()) == {d["address"] for d in moved}
    assert_admits(ruleset, addresses)

    # And not at the §3.1 addresses the devices left behind.
    rules = parse_rules(ruleset)
    assert not outbound(rules, "10.2.30.248", "tcp", CQ_MIDI_PORT)
    assert not inbound(rules, "10.2.30.248", "udp", DEFAULT_LOCAL_UDP_PORT)
    # The meter port is the mixer's alone: nobody else reaches it.
    assert not inbound(rules, addresses["projector"], "udp", DEFAULT_LOCAL_UDP_PORT)
    # "Every port" to the mixer is not every port to everyone.
    assert not outbound(rules, addresses["projector"], "udp", _SOME_PORT)


# -- administrators' networks beyond the VLAN (a departure from §3.3) -----------------
#
# §3.3 admits the web interface from the VLAN only. The site administers the
# appliance from its office (10.46.0.0/23) and desktop (10.2.10.0/23) subnets,
# and asked on 24 September 2026 for those to reach it too. admin_networks is
# that request as configuration: it opens tcp 80 and 443 and nothing else.


def _admin_rule(ruleset: str) -> str | None:
    lines = [line.strip() for line in ruleset.splitlines()]
    web = [line for line in lines if "tcp dport { 80, 443 }" in line]
    beyond_vlan = [line for line in web if "10.2.30.0" not in line]
    return beyond_vlan[0] if beyond_vlan else None


def test_admin_networks_reach_the_web_interface_and_nothing_more(
    generator: ModuleType, tmp_path: Path
) -> None:
    path = tmp_path / "system.json"
    path.write_text(
        json.dumps({"admin_networks": ["10.46.0.0/23", "10.2.10.0/23"]}), encoding="utf-8"
    )
    config, ok = generator.load(path)
    assert ok
    assert config.admin_networks == ["10.46.0.0/23", "10.2.10.0/23"]
    ruleset = generator.render_nft(config)
    rule = _admin_rule(ruleset)
    assert rule == "ip saddr { 10.46.0.0/23, 10.2.10.0/23 } tcp dport { 80, 443 } accept"
    # SSH stays the management address's alone: an admin network is not a
    # licence to SSH, and nothing else these networks might send is admitted.
    for line in ruleset.splitlines():
        if "10.46.0.0/23" in line or "10.2.10.0/23" in line:
            assert "dport { 80, 443 }" in line, f"admin networks reach more than the web: {line}"


def test_no_admin_networks_is_section_3_3_exactly(generator: ModuleType, tmp_path: Path) -> None:
    path = tmp_path / "system.json"
    path.write_text(json.dumps({}), encoding="utf-8")
    config, _ = generator.load(path)
    assert config.admin_networks == []
    assert _admin_rule(generator.render_nft(config)) is None


def test_a_typo_in_admin_networks_costs_that_entry_not_the_firewall(
    generator: ModuleType, tmp_path: Path
) -> None:
    """An invalid entry is dropped with a warning; the valid ones still apply,
    and the apply still succeeds. A string instead of a list is ignored whole."""
    path = tmp_path / "system.json"
    entries = ["10.46.0.0/23", "10.2.10.0/33", "fe80::/10", "10.46.1.7/23"]
    path.write_text(json.dumps({"admin_networks": entries}), encoding="utf-8")
    config, ok = generator.load(path)
    assert ok
    # /33 is not a network; IPv6 is not filtered here; 10.46.1.7/23 normalises
    # to the network already listed, so it collapses rather than duplicating.
    assert config.admin_networks == ["10.46.0.0/23"]

    path.write_text(json.dumps({"admin_networks": "10.46.0.0/23"}), encoding="utf-8")
    config, ok = generator.load(path)
    assert ok
    assert config.admin_networks == []


def test_the_image_ships_the_sites_admin_networks() -> None:
    assert image_system_json()["admin_networks"] == ["10.46.0.0/23", "10.2.10.0/23"]


# -- the application's writes, rendered by the real generator -------------------------
#
# Commissioning on 24 September 2026 found two hand-offs broken between the
# application, which writes system.json, and this generator, which renders it:
# a device edit replaced the image's device list with the device table alone,
# taking the KNX gateway's udp/3671 rule (knxd is not a device row, §5.5, B42)
# and the control surface's; and nothing wrote network.smtp_relay, so saved
# mail never left. Each test below starts from what the image ships — its
# system.json and knxd.conf — makes the application's real write, and renders
# the result with the real generator.

KNX_GATEWAY = "10.2.30.252"  # knxd.conf.default's "-b ipt:10.2.30.252" (§3.1)
RELAY_HOST = "au-smtp-outbound-1.mimecast.com"
RELAY_ADDRESSES = ["205.139.110.221", "205.139.110.242"]


def image_data_dir(tmp_path: Path) -> Path:
    """``/data`` as the image leaves it: build.sh's system.json and knxd.conf."""
    data = tmp_path / "data"
    (data / "config").mkdir(parents=True)
    system_config.system_config_path(data).write_text(
        json.dumps(image_system_json()), encoding="utf-8"
    )
    shutil.copyfile(KNXD_DEFAULT, system_config.knxd_conf_path(data))
    return data


def _device(device_id: int, category: str, driver: str, name: str, host: str, port: int) -> Device:
    kind = "tcp" if category in ("mixer", "projector") else "udp"
    return Device(
        id=device_id,
        category=category,
        driver_key=driver,
        name=name,
        enabled=True,
        config={"transport": {"type": kind, "host": host, "port": port}},
        created_at="2026-09-24T16:00:00+12:00",
        updated_at="2026-09-24T16:00:00+12:00",
    )


def the_rig() -> list[Device]:
    """The devices as the admin interface saves them, named for FLOWS."""
    return [
        _device(1, "mixer", "cq20b", "mixer", "10.2.30.248", CQ_MIDI_PORT),
        _device(2, "projector", "pjlink", "projector", "10.2.30.249", PJLINK_PORT),
        _device(3, "lighting_output", "artnet", "dmx_node", "10.2.30.245", artnet.ARTNET_PORT),
        _device(4, "lighting_output", "sacn", "dmx_node", "10.2.30.245", artnet.SACN_PORT),
    ]


def render(generator: ModuleType, data: Path) -> tuple[Any, str]:
    path = system_config.system_config_path(data)
    raw = json.loads(path.read_text(encoding="utf-8"))
    config = generator.parse(
        raw,
        config_path=path,
        resolve=lambda host, port: RELAY_ADDRESSES if host == RELAY_HOST else [],
    )
    return config, generator.render_nft(config)


def test_a_device_edit_keeps_the_knx_gateways_rule(generator: ModuleType, tmp_path: Path) -> None:
    data = image_data_dir(tmp_path)
    _, before = render(generator, data)
    assert outbound(parse_rules(before), KNX_GATEWAY, "udp", KNXNET_IP_PORT), "the image's own"

    # What PUT /devices/{id} does (proskenion/api/devices.py's _sync_firewall).
    system_config.sync_devices(data, the_rig())

    config, ruleset = render(generator, data)
    rules = parse_rules(ruleset)
    assert outbound(rules, KNX_GATEWAY, "udp", KNXNET_IP_PORT), (
        "a device edit dropped the KNX gateway's rule: knxd's tunnel dies at its next reconnect"
    )
    # And every other flow a driver needs, the control surface's included.
    assert_admits(ruleset, addresses_of(config))


def test_the_gateway_rule_follows_knxd_conf(generator: ModuleType, tmp_path: Path) -> None:
    """knxd.conf is where the gateway address lives (§4.3): the rule goes with it."""
    data = image_data_dir(tmp_path)
    system_config.knxd_conf_path(data).write_text(
        'KNXD_OPTS="-e 0.0.1 -E 0.0.2:8 -b ipt:10.2.30.240"\n', encoding="utf-8"
    )
    system_config.sync_devices(data, the_rig())
    _, ruleset = render(generator, data)
    rules = parse_rules(ruleset)
    assert outbound(rules, "10.2.30.240", "udp", KNXNET_IP_PORT)
    assert not outbound(rules, KNX_GATEWAY, "udp", KNXNET_IP_PORT)


def test_an_appliance_that_already_lost_the_gateway_gets_it_back_at_start_up(
    generator: ModuleType, tmp_path: Path
) -> None:
    """The CM5 as v0.1.0 left it: ``devices`` is the device table alone.
    Installing the fix as a package must restore the rule with no device edit."""
    data = image_data_dir(tmp_path)
    system_config.merge(data, {"devices": system_config.device_table(the_rig())})
    _, stripped = render(generator, data)
    assert not outbound(parse_rules(stripped), KNX_GATEWAY, "udp", KNXNET_IP_PORT)

    assert system_config.reconcile(data, the_rig(), None, None) is True

    config, ruleset = render(generator, data)
    assert outbound(parse_rules(ruleset), KNX_GATEWAY, "udp", KNXNET_IP_PORT)
    assert_admits(ruleset, addresses_of(config))
    assert system_config.reconcile(data, the_rig(), None, None) is False, "not idempotent"


def test_saving_the_relay_opens_its_port_and_clearing_closes_it(
    generator: ModuleType, tmp_path: Path
) -> None:
    data = image_data_dir(tmp_path)
    before = system_config.read(data)["network"]

    # What PUT /system/email does (proskenion/api/system.py's _sync_smtp_firewall).
    assert system_config.sync_smtp_relay(data, RELAY_HOST, 25) is True
    config, ruleset = render(generator, data)
    rules = parse_rules(ruleset)
    for address in RELAY_ADDRESSES:
        assert outbound(rules, address, "tcp", 25), f"mail to {address}:25 is dropped"
    assert not outbound(rules, "198.51.100.7", "tcp", 25), "port 25 opened to everyone"
    # Every other network key carried forward, so nothing else in the ruleset moved.
    after = system_config.read(data)["network"]
    assert {k: v for k, v in after.items() if k != "smtp_relay"} == {
        k: v for k, v in before.items() if k != "smtp_relay"
    }
    assert config.network_address is not None, "the appliance's own addressing was lost"

    # DELETE /system/email.
    assert system_config.sync_smtp_relay(data, None, None) is True
    _, cleared = render(generator, data)
    assert not any(outbound(parse_rules(cleared), a, "tcp", 25) for a in RELAY_ADDRESSES)
