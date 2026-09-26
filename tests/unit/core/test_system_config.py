"""``system.json``: read/merge/write, and the devices-table mirror (contracts §4).

The mirror closes a Phase 1 gap: a device saved at a new address
must reach the rendered firewall table before anything tests the new
address. These tests build :class:`~proskenion.db.crud.devices.Device` rows
directly — the mirror only reads ``category``/``driver_key``/``name``/``config``,
none of which needs a real database row behind it.
"""

from __future__ import annotations

import json
from pathlib import Path

from proskenion.core import system_config
from proskenion.db.crud.devices import Device

# -- generic read/merge/write -------------------------------------------------------


def make_device(
    *,
    id: int = 1,
    category: str = "projector",
    driver_key: str = "pjlink",
    name: str = "Projector",
    config: dict[str, object] | None = None,
    enabled: bool = True,
) -> Device:
    return Device(
        id=id,
        category=category,
        driver_key=driver_key,
        name=name,
        enabled=enabled,
        config=config or {},
        created_at="2026-09-20T12:00:00+12:00",
        updated_at="2026-09-20T12:00:00+12:00",
    )


def test_read_is_empty_when_the_file_is_absent(tmp_path: Path) -> None:
    assert system_config.read(tmp_path) == {}


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    system_config.merge(tmp_path, {"hostname": "auditorium"})
    assert system_config.read(tmp_path) == {"hostname": "auditorium"}


def test_merge_touches_only_the_named_keys(tmp_path: Path) -> None:
    """The read-merge-write discipline (contracts §1's rule, applied here too):
    a key this call does not name — including one this module does not
    model at all — survives untouched."""
    path = system_config.system_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "hostname": "old",
                "network": {"smtp_relay": {"host": "relay.n4l.co.nz", "port": 25}},
                "unmodelled_key": {"kept": True},
            }
        ),
        encoding="utf-8",
    )
    system_config.merge(tmp_path, {"hostname": "new"})
    doc = system_config.read(tmp_path)
    assert doc["hostname"] == "new"
    assert doc["network"] == {"smtp_relay": {"host": "relay.n4l.co.nz", "port": 25}}
    assert doc["unmodelled_key"] == {"kept": True}


def test_write_is_atomic_and_leaves_no_temp_file(tmp_path: Path) -> None:
    system_config.merge(tmp_path, {"hostname": "auditorium"})
    entries = list((tmp_path / "config").iterdir())
    assert [e.name for e in entries] == ["system.json"]


def test_delete_file_is_quiet_when_already_absent(tmp_path: Path) -> None:
    system_config.delete_file(tmp_path / "config" / "system.json")  # must not raise


# -- device_table_entry ---------------------------------------------------------------


def test_a_serial_device_has_no_firewall_entry() -> None:
    device = make_device(
        category="video_matrix",
        driver_key="lkv422",
        config={"transport": {"type": "serial", "path": "/dev/serial/by-id/x"}},
    )
    assert system_config.device_table_entry(device) is None


def test_a_loopback_device_has_no_firewall_entry() -> None:
    device = make_device(config={"transport": {"type": "loopback"}})
    assert system_config.device_table_entry(device) is None


def test_a_tcp_device_gets_its_transports_own_port() -> None:
    device = make_device(
        name="Projector 1",
        config={"transport": {"type": "tcp", "host": "10.2.30.249", "port": 4352}},
    )
    entry = system_config.device_table_entry(device)
    assert entry == {
        "name": "Projector_1",
        "address": "10.2.30.249",
        "ports": ["tcp/4352"],
    }


def test_the_device_name_is_sanitised_for_the_firewall_comment() -> None:
    device = make_device(
        name="Front-of-house projector (main)!",
        config={"transport": {"type": "tcp", "host": "10.2.30.249", "port": 4352}},
    )
    entry = system_config.device_table_entry(device)
    assert entry is not None
    assert entry["name"] == "Front-of-house_projector__main__"


def test_a_device_with_no_valid_port_and_no_driver_extras_has_no_entry() -> None:
    device = make_device(config={"transport": {"type": "tcp", "host": "10.2.30.249"}})
    assert system_config.device_table_entry(device) is None


def test_the_mixer_gets_its_drivers_extra_ports_beyond_the_transports_own() -> None:
    """CQ20BDriver.firewall_ports (proskenion/core/drivers/cq20b.py) declares
    the native connection and meter return beyond the MIDI port the tcp
    transport config already carries — exactly what used to be hard-coded in
    auditorium-config-apply's DEFAULT_DEVICES (Phase 1 gap, contracts §4)."""
    device = make_device(
        category="mixer",
        driver_key="cq20b",
        name="Mixer",
        config={"transport": {"type": "tcp", "host": "10.2.30.71", "port": 51325}},
    )
    entry = system_config.device_table_entry(device)
    assert entry == {
        "name": "Mixer",
        "address": "10.2.30.71",
        "ports": ["tcp/51325", "tcp/51326", "udp/any"],
        "listen_ports": ["udp/51327"],
    }


def test_the_mixers_meter_port_follows_its_own_configuration() -> None:
    device = make_device(
        category="mixer",
        driver_key="cq20b",
        name="Mixer",
        config={
            "transport": {"type": "tcp", "host": "10.2.30.71", "port": 51325},
            "meter_udp_port": 61327,
        },
    )
    entry = system_config.device_table_entry(device)
    assert entry is not None
    assert entry["listen_ports"] == ["udp/61327"]


def test_an_unknown_driver_key_still_gets_its_transports_own_port() -> None:
    """The row was saved by a version that shipped the driver; this one no
    longer does. The transport's own port — all this module can still know —
    still gets a rule, rather than the device vanishing from the firewall."""
    device = make_device(
        driver_key="retired-driver",
        config={"transport": {"type": "tcp", "host": "10.2.30.249", "port": 4352}},
    )
    entry = system_config.device_table_entry(device)
    assert entry == {"name": "Projector", "address": "10.2.30.249", "ports": ["tcp/4352"]}


# -- device_hosts (the §21.24 conflict check) ------------------------------------------


def test_device_hosts_collects_every_tcp_or_udp_address() -> None:
    devices = [
        make_device(id=1, config={"transport": {"type": "tcp", "host": "10.2.30.249"}}),
        make_device(id=2, config={"transport": {"type": "udp", "host": "10.2.30.90"}}),
        make_device(id=3, config={"transport": {"type": "serial", "path": "/dev/x"}}),
    ]
    assert system_config.device_hosts(devices) == {"10.2.30.249", "10.2.30.90"}


def test_device_hosts_counts_a_device_with_no_valid_port_too() -> None:
    """Independent of the firewall-row rules: a device occupies its address
    even when it has nothing to firewall yet."""
    device = make_device(config={"transport": {"type": "tcp", "host": "10.2.30.249"}})
    assert system_config.device_hosts([device]) == {"10.2.30.249"}


# -- sync_devices -----------------------------------------------------------------------


def test_sync_devices_writes_the_table_and_preserves_other_keys(tmp_path: Path) -> None:
    system_config.merge(
        tmp_path,
        {"hostname": "auditorium", "network": {"vlan": "10.2.30.0/24"}},
    )
    devices = [
        make_device(
            id=1,
            name="Projector",
            config={"transport": {"type": "tcp", "host": "10.2.30.249", "port": 4352}},
        )
    ]
    doc = system_config.sync_devices(tmp_path, devices)
    assert doc["hostname"] == "auditorium"
    assert doc["network"] == {"vlan": "10.2.30.0/24"}
    # No knxd.conf here, so no gateway; the control surface is config-apply's
    # default, since the network settings name none.
    assert doc["devices"] == [
        {"name": "Projector", "address": "10.2.30.249", "ports": ["tcp/4352"]},
        {"name": "control_surface", "address": "10.2.30.100", "ports": ["udp/5004-5005"]},
    ]
    # And it is what is actually on disk, not just the return value.
    assert system_config.read(tmp_path)["devices"] == doc["devices"]


def test_sync_devices_moves_a_device_to_its_new_address(tmp_path: Path) -> None:
    """The Phase 1 gap, directly: re-syncing after an address edit replaces
    the old rule with the new one, in one write (the API-level version of
    this — a real PUT /devices/{id} — is in tests/unit/api/test_devices.py)."""
    moved = make_device(
        name="Projector",
        config={"transport": {"type": "tcp", "host": "10.2.30.249", "port": 4352}},
    )
    system_config.sync_devices(tmp_path, [moved])
    assert system_config.read(tmp_path)["devices"][0]["address"] == "10.2.30.249"

    relocated = make_device(
        name="Projector",
        config={"transport": {"type": "tcp", "host": "10.2.99.5", "port": 4352}},
    )
    system_config.sync_devices(tmp_path, [relocated])
    devices = system_config.read(tmp_path)["devices"]
    assert [d["address"] for d in devices if d["name"] == "Projector"] == ["10.2.99.5"]
    assert "10.2.30.249" not in {d["address"] for d in devices}


# -- the entries that are not device rows ---------------------------------------
#
# Rendered end to end, with the image's own system.json and knxd.conf and the
# real firewall generator, in tests/unit/appliance/test_firewall_ports.py.


def test_the_gateway_comes_from_knxds_tunnel_backend() -> None:
    conf = '# -b ipt:HOST is the tunnel\nKNXD_OPTS="-e 0.0.1 -E 0.0.2:8 -b ipt:10.2.30.252"\n'
    assert system_config.knx_gateway_from_knxd(conf) == {
        "name": "knx_gateway",
        "address": "10.2.30.252",
        "ports": ["udp/3671"],
    }
    nat = system_config.knx_gateway_from_knxd('KNXD_OPTS="-b iptn:10.2.30.9:3700"')
    assert nat is not None and nat["ports"] == ["udp/3700"]


def test_knxd_with_no_single_gateway_gets_no_rule() -> None:
    assert system_config.knx_gateway_from_knxd('KNXD_OPTS="-e 0.0.1 -b ip:"') is None
    assert system_config.knx_gateway_from_knxd('KNXD_OPTS="-b ipt:gateway.local"') is None
    assert system_config.knx_gateway_from_knxd("# -b ipt:10.2.30.252\n") is None


def _seed(tmp_path: Path, doc: dict[str, object]) -> None:
    path = system_config.system_config_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def test_an_unreadable_knxd_conf_keeps_the_gateway_rule_it_had(tmp_path: Path) -> None:
    """Unreadable is not "no gateway": KNX must not be cut off by a read error."""
    gateway = {"name": "knx_gateway", "address": "10.2.30.252", "ports": ["udp/3671"]}
    _seed(tmp_path, {"devices": [gateway]})
    doc = system_config.sync_devices(tmp_path, [])
    assert gateway in doc["devices"]


def test_the_control_surface_follows_the_network_settings(tmp_path: Path) -> None:
    _seed(tmp_path, {"network": {"control_surface_address": "10.2.30.77"}})
    doc = system_config.sync_devices(tmp_path, [])
    assert doc["devices"] == [
        {"name": "control_surface", "address": "10.2.30.77", "ports": ["udp/5004-5005"]}
    ]
    _seed(tmp_path, {"network": {"control_surface_address": None}})
    assert system_config.sync_devices(tmp_path, [])["devices"] == []


def test_the_relay_is_mirrored_with_every_other_network_key_carried_forward(
    tmp_path: Path,
) -> None:
    network = {"vlan": "10.2.30.0/24", "address": "10.2.30.251/24", "backup_destination": None}
    _seed(tmp_path, {"hostname": "auditorium", "network": dict(network)})
    assert system_config.sync_smtp_relay(tmp_path, "au-smtp-outbound-1.mimecast.com", 25)
    doc = system_config.read(tmp_path)
    assert doc["network"] == {
        **network,
        "smtp_relay": {"host": "au-smtp-outbound-1.mimecast.com", "port": 25},
    }
    assert doc["hostname"] == "auditorium"
    assert system_config.sync_smtp_relay(tmp_path, "au-smtp-outbound-1.mimecast.com", 25) is False

    assert system_config.sync_smtp_relay(tmp_path, None, None) is True
    assert system_config.read(tmp_path)["network"] == network


def test_reconcile_never_creates_system_json(tmp_path: Path) -> None:
    """Absent means config-apply's §3.1 defaults; a partial file would replace them."""
    assert system_config.reconcile(tmp_path, [], "relay.example", 25) is False
    assert not system_config.system_config_path(tmp_path).exists()
