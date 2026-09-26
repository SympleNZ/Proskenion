"""This appliance's own address (§3.1, contracts §4).

``auditorium-config-apply`` used to leave IP addressing alone entirely — the
NetworkManager connection profile was baked into the image by
``appliance/image/build.sh`` and nothing on the appliance ever touched it
again. This is the other half of the ``/system/network`` confirm-or-revert
flow tested in ``test_helper.py``: the profile is now rendered from
``system.json``'s ``network.address``/``gateway``/``dns`` the same way the
firewall is rendered from the device table, and applied idempotently the
same way.

``auditorium-config-apply`` itself is loaded as a module by
``tests/unit/appliance/conftest.py``'s ``generator`` fixture (also used by
``test_firewall_ports.py``); ``nft`` and ``nmcli`` are never invoked for
real here — see ``appliance/tests/verify-in-docker.sh`` for the ``nmcli``-
free but real-``nft`` acceptance run, and that script's dedicated
``--render-network`` checks for this exact rendering, against real files.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture(scope="module")
def generator() -> ModuleType:
    from tests.unit.appliance.conftest import BIN, load_script

    return load_script(BIN / "auditorium-config-apply", "auditorium_config_apply")


def load(generator: ModuleType, tmp_path: Path, doc: dict[str, object]) -> object:
    path = tmp_path / "system.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    config, ok = generator.load(path)
    assert ok
    return config


# -- parsing ------------------------------------------------------------------------


def test_address_is_unmanaged_when_the_network_key_is_absent(generator: ModuleType) -> None:
    config, ok = generator.load(Path("/nonexistent-system.json"))
    assert ok
    assert config.network_address is None


def test_address_is_unmanaged_when_address_and_gateway_are_unset(
    generator: ModuleType, tmp_path: Path
) -> None:
    config = load(generator, tmp_path, {"network": {"vlan": "10.2.30.0/24"}})
    assert config.network_address is None


def test_a_full_address_gateway_and_dns_are_parsed(
    generator: ModuleType, tmp_path: Path
) -> None:
    config = load(
        generator,
        tmp_path,
        {
            "network": {
                "address": "10.2.30.45/24",
                "gateway": "10.2.30.1",
                "dns": ["10.2.30.1", "8.8.8.8"],
            }
        },
    )
    assert config.network_address is not None
    assert config.network_address.cidr == "10.2.30.45/24"
    assert config.network_address.gateway == "10.2.30.1"
    assert config.network_address.dns == ["10.2.30.1", "8.8.8.8"]


def test_a_single_dns_string_is_accepted_too(generator: ModuleType, tmp_path: Path) -> None:
    config = load(
        generator,
        tmp_path,
        {"network": {"address": "10.2.30.45/24", "gateway": "10.2.30.1", "dns": "10.2.30.1"}},
    )
    assert config.network_address is not None
    assert config.network_address.dns == ["10.2.30.1"]


def test_a_malformed_address_leaves_addressing_unmanaged(
    generator: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = load(
        generator, tmp_path, {"network": {"address": "not-an-address", "gateway": "10.2.30.1"}}
    )
    assert config.network_address is None
    assert "network.address invalid" in capsys.readouterr().err


def test_a_missing_gateway_leaves_addressing_unmanaged(
    generator: ModuleType, tmp_path: Path
) -> None:
    config = load(generator, tmp_path, {"network": {"address": "10.2.30.45/24"}})
    assert config.network_address is None


def test_a_gateway_off_the_subnet_leaves_addressing_unmanaged(
    generator: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = load(
        generator, tmp_path, {"network": {"address": "10.2.30.45/24", "gateway": "10.9.9.1"}}
    )
    assert config.network_address is None
    assert "not on" in capsys.readouterr().err


def test_an_ipv6_address_leaves_addressing_unmanaged(
    generator: ModuleType, tmp_path: Path
) -> None:
    config = load(
        generator,
        tmp_path,
        {"network": {"address": "2001:db8::1/64", "gateway": "2001:db8::ff"}},
    )
    assert config.network_address is None


def test_an_invalid_dns_entry_is_dropped_not_fatal(generator: ModuleType, tmp_path: Path) -> None:
    config = load(
        generator,
        tmp_path,
        {
            "network": {
                "address": "10.2.30.45/24",
                "gateway": "10.2.30.1",
                "dns": ["10.2.30.1", "not-an-ip"],
            }
        },
    )
    assert config.network_address is not None
    assert config.network_address.dns == ["10.2.30.1"]


# -- rendering ------------------------------------------------------------------------


def test_render_nm_connection_carries_the_address_gateway_and_dns(
    generator: ModuleType,
) -> None:
    addr = generator.NetworkAddress(cidr="10.2.30.45/24", gateway="10.2.30.1", dns=["10.2.30.1"])
    text = generator.render_nm_connection(addr)
    assert "id=auditorium" in text
    assert "method=manual" in text
    assert "address1=10.2.30.45/24,10.2.30.1" in text
    assert "dns=10.2.30.1;" in text
    assert "method=disabled" in text  # ipv6


def test_render_nm_connection_with_several_dns_servers(generator: ModuleType) -> None:
    addr = generator.NetworkAddress(
        cidr="10.2.30.45/24", gateway="10.2.30.1", dns=["10.2.30.1", "8.8.8.8"]
    )
    text = generator.render_nm_connection(addr)
    assert "dns=10.2.30.1;8.8.8.8;" in text


def test_render_nm_connection_with_no_dns_omits_the_line(generator: ModuleType) -> None:
    addr = generator.NetworkAddress(cidr="10.2.30.45/24", gateway="10.2.30.1", dns=[])
    text = generator.render_nm_connection(addr)
    assert "dns=" not in text


# -- applying ---------------------------------------------------------------------------


def test_apply_network_address_writes_nothing_when_unmanaged(
    generator: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nm_path = tmp_path / "auditorium.nmconnection"
    monkeypatch.setattr(generator, "NM_CONNECTION_PATH", nm_path)
    config = generator.Config()  # network_address is None by default
    assert generator.apply_network_address(config, dry_run=False) is True
    assert not nm_path.exists()


def test_apply_network_address_dry_run_never_calls_nmcli(
    generator: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nm_path = tmp_path / "auditorium.nmconnection"
    monkeypatch.setattr(generator, "NM_CONNECTION_PATH", nm_path)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        generator.subprocess,
        "run",
        lambda argv, **kwargs: calls.append(list(argv)) or _ok(),
    )
    config = generator.Config(
        network_address=generator.NetworkAddress(
            cidr="10.2.30.45/24", gateway="10.2.30.1", dns=["10.2.30.1"]
        )
    )
    assert generator.apply_network_address(config, dry_run=True) is True
    assert calls == []
    assert not nm_path.exists(), "dry-run reports what would change; it does not write"


class _Result:
    returncode = 0
    stderr = ""
    stdout = ""


def _ok() -> _Result:
    return _Result()


def test_apply_network_address_writes_and_calls_nmcli_when_changed(
    generator: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nm_path = tmp_path / "auditorium.nmconnection"
    monkeypatch.setattr(generator, "NM_CONNECTION_PATH", nm_path)
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> _Result:
        calls.append(list(argv))
        return _ok()

    monkeypatch.setattr(generator.subprocess, "run", fake_run)
    monkeypatch.setattr(generator, "network_manager_running", lambda: True)
    config = generator.Config(
        network_address=generator.NetworkAddress(
            cidr="10.2.30.45/24", gateway="10.2.30.1", dns=["10.2.30.1"]
        )
    )

    assert generator.apply_network_address(config, dry_run=False) is True
    assert nm_path.exists()
    assert "address1=10.2.30.45/24,10.2.30.1" in nm_path.read_text(encoding="utf-8")
    assert calls == [
        ["nmcli", "connection", "reload"],
        ["nmcli", "connection", "up", "auditorium"],
    ]


def test_apply_network_address_is_idempotent(
    generator: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unchanged profile is not rewritten, and nmcli is not asked to
    reload or bring the connection up again — matching apply_firewall's own
    "unchanged" short-circuit."""
    nm_path = tmp_path / "auditorium.nmconnection"
    monkeypatch.setattr(generator, "NM_CONNECTION_PATH", nm_path)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        generator.subprocess, "run", lambda argv, **_kw: calls.append(list(argv)) or _ok()
    )
    monkeypatch.setattr(generator, "network_manager_running", lambda: True)
    config = generator.Config(
        network_address=generator.NetworkAddress(
            cidr="10.2.30.45/24", gateway="10.2.30.1", dns=["10.2.30.1"]
        )
    )
    generator.apply_network_address(config, dry_run=False)
    calls.clear()

    assert generator.apply_network_address(config, dry_run=False) is True
    assert calls == [], "unchanged: nmcli is not called a second time"


def test_apply_network_address_reports_failure_when_nmcli_fails(
    generator: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nm_path = tmp_path / "auditorium.nmconnection"
    monkeypatch.setattr(generator, "NM_CONNECTION_PATH", nm_path)

    class Failing:
        returncode = 1
        stderr = "no such connection profile"
        stdout = ""

    monkeypatch.setattr(generator.subprocess, "run", lambda *a, **k: Failing())
    monkeypatch.setattr(generator, "network_manager_running", lambda: True)
    config = generator.Config(
        network_address=generator.NetworkAddress(
            cidr="10.2.30.45/24", gateway="10.2.30.1", dns=["10.2.30.1"]
        )
    )
    assert generator.apply_network_address(config, dry_run=False) is False


def test_at_boot_the_profile_is_written_and_nothing_is_reloaded(
    generator: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The boot case: NetworkManager has not started, so it will read the file.

    This unit runs Before=NetworkManager.service, and the root is a RAM
    overlay, so at every boot the profile is freshly written and NetworkManager
    is not yet running. Asking it to reload then is what failed this unit on
    every boot of the CM5 (24 September 2026). Written, not reloaded, success.
    """
    nm_path = tmp_path / "auditorium.nmconnection"
    monkeypatch.setattr(generator, "NM_CONNECTION_PATH", nm_path)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        generator.subprocess, "run", lambda argv, **_kw: calls.append(list(argv)) or _ok()
    )
    monkeypatch.setattr(generator, "network_manager_running", lambda: False)
    config = generator.Config(
        network_address=generator.NetworkAddress(
            cidr="10.2.30.251/24", gateway="10.2.30.254", dns=["10.2.40.1"]
        )
    )

    assert generator.apply_network_address(config, dry_run=False) is True
    assert "address1=10.2.30.251/24,10.2.30.254" in nm_path.read_text(encoding="utf-8")
    assert calls == [], "nmcli must not be asked to reload a NetworkManager that is not running"


def test_the_unit_is_ordered_before_network_manager() -> None:
    """Without the ordering, whether the address applies at boot is a race."""
    unit = (
        Path(__file__).resolve().parents[3]
        / "appliance" / "systemd" / "auditorium-config-apply.service"
    ).read_text(encoding="utf-8")
    before = [line for line in unit.splitlines() if line.startswith("Before=")]
    assert before, "auditorium-config-apply.service has no Before= line"
    assert "NetworkManager.service" in before[0].split(), (
        "config-apply is no longer ordered before NetworkManager: NetworkManager may "
        "start without the appliance's profile, and this unit fails asking it to reload"
    )
