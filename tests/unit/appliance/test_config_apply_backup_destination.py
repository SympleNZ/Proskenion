"""``network.backup_destination``'s DNS resolution and firewall rendering
(§3.4, carry-forward 4 from phase-6): a hostname destination is resolved the
same way as ``network.smtp_relay`` — resolved at apply time, cached so a
transient DNS failure keeps the last known addresses, never opened to every
address.

Before this fix, ``auditorium-config-apply`` ran the address through
``_ip()``, which only accepts a literal IP — a hostname destination (the
Backup screen's field has never restricted it, Q22) silently got no firewall
rule at all, so a NAS on the school's own DNS name was unreachable outbound
even though it was saved and shown as configured.

Loads ``appliance/bin/auditorium-config-apply`` the same way
``tests/unit/appliance/test_config_apply_smtp.py`` does — it ships with no
``.py`` suffix, so it is loaded by path.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

APPLIANCE = Path(__file__).resolve().parents[3] / "appliance"
GENERATOR = APPLIANCE / "bin" / "auditorium-config-apply"


def load_generator() -> ModuleType:
    loader = importlib.machinery.SourceFileLoader(
        "auditorium_config_apply_backup_destination", str(GENERATOR)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
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


def _ok_resolve(addresses: list[str]):  # noqa: ANN201 - closure over a test list
    def _resolve(host: str, port: int) -> list[str]:
        return list(addresses)

    return _resolve


def _failing_resolve(host: str, port: int) -> list[str]:
    raise OSError("simulated DNS failure")


# -- parsing and resolution -----------------------------------------------------------


def test_no_backup_destination_configured_resolves_nothing(
    generator: ModuleType, tmp_path: Path
) -> None:
    cfg = generator.parse(
        {}, config_path=tmp_path / "system.json", resolve=_ok_resolve(["10.2.1.5"])
    )
    assert cfg.backup_host is None
    assert cfg.backup_protocol is None
    assert cfg.backup_addresses == []


def test_a_hostname_destination_is_resolved_and_cached(
    generator: ModuleType, tmp_path: Path
) -> None:
    """The defect this test is written against: a hostname destination used
    to be dropped by ``_ip()`` with no rule at all."""
    config_path = tmp_path / "system.json"
    raw = {
        "network": {
            "backup_destination": {"address": "nas.school.local", "protocol": "smb"}
        }
    }
    cfg = generator.parse(
        raw, config_path=config_path, resolve=_ok_resolve(["10.2.1.5", "10.2.1.6"])
    )
    assert cfg.backup_host == "nas.school.local"
    assert cfg.backup_protocol == "smb"
    assert cfg.backup_addresses == ["10.2.1.5", "10.2.1.6"]

    cache = json.loads(
        (tmp_path / ".backup-destination-cache.json").read_text(encoding="utf-8")
    )
    assert cache == {
        "host": "nas.school.local",
        "port": 445,
        "addresses": ["10.2.1.5", "10.2.1.6"],
    }


def test_a_literal_address_still_resolves_to_itself(
    generator: ModuleType, tmp_path: Path
) -> None:
    raw = {
        "network": {"backup_destination": {"address": "10.2.1.5", "protocol": "smb"}}
    }
    cfg = generator.parse(
        raw,
        config_path=tmp_path / "system.json",
        resolve=_ok_resolve(["10.2.1.5"]),
    )
    assert cfg.backup_addresses == ["10.2.1.5"]


def test_sftp_resolves_on_port_22(generator: ModuleType, tmp_path: Path) -> None:
    resolved: list[tuple[str, int]] = []

    def _resolve(host: str, port: int) -> list[str]:
        resolved.append((host, port))
        return ["10.2.1.5"]

    raw = {
        "network": {"backup_destination": {"address": "nas.school.local", "protocol": "sftp"}}
    }
    generator.parse(raw, config_path=tmp_path / "system.json", resolve=_resolve)
    assert resolved == [("nas.school.local", 22)]


def test_a_resolution_failure_falls_back_to_the_cache(
    generator: ModuleType, tmp_path: Path
) -> None:
    config_path = tmp_path / "system.json"
    raw = {
        "network": {
            "backup_destination": {"address": "nas.school.local", "protocol": "smb"}
        }
    }
    generator.parse(raw, config_path=config_path, resolve=_ok_resolve(["10.2.1.5"]))
    cfg = generator.parse(raw, config_path=config_path, resolve=_failing_resolve)
    assert cfg.backup_addresses == ["10.2.1.5"]


def test_a_resolution_failure_with_nothing_cached_yields_no_addresses(
    generator: ModuleType, tmp_path: Path
) -> None:
    config_path = tmp_path / "system.json"
    raw = {
        "network": {
            "backup_destination": {"address": "nas.school.local", "protocol": "smb"}
        }
    }
    cfg = generator.parse(raw, config_path=config_path, resolve=_failing_resolve)
    assert cfg.backup_host == "nas.school.local"
    assert cfg.backup_addresses == []


def test_an_invalid_protocol_is_ignored(generator: ModuleType, tmp_path: Path) -> None:
    raw = {
        "network": {
            "backup_destination": {"address": "nas.school.local", "protocol": "ftp"}
        }
    }
    cfg = generator.parse(
        raw, config_path=tmp_path / "system.json", resolve=_ok_resolve(["10.2.1.5"])
    )
    assert cfg.backup_host is None
    assert cfg.backup_addresses == []


# -- rendering --------------------------------------------------------------------------


def test_render_writes_one_rule_per_resolved_address(
    generator: ModuleType, tmp_path: Path
) -> None:
    raw = {
        "network": {
            "backup_destination": {"address": "nas.school.local", "protocol": "smb"}
        }
    }
    cfg = generator.parse(
        raw,
        config_path=tmp_path / "system.json",
        resolve=_ok_resolve(["10.2.1.5", "10.2.1.6"]),
    )
    ruleset = generator.render_nft(cfg)
    assert "ip daddr 10.2.1.5 tcp dport 445 accept" in ruleset
    assert "ip daddr 10.2.1.6 tcp dport 445 accept" in ruleset
    # Never a bare "tcp dport 445 accept" with no address.
    assert "\n        tcp dport 445 accept\n" not in ruleset


def test_render_with_no_addresses_opens_nothing(generator: ModuleType, tmp_path: Path) -> None:
    raw = {
        "network": {
            "backup_destination": {"address": "nas.school.local", "protocol": "smb"}
        }
    }
    cfg = generator.parse(raw, config_path=tmp_path / "system.json", resolve=_failing_resolve)
    ruleset = generator.render_nft(cfg)
    assert "dport 445 accept" not in ruleset
    assert "did not resolve" in ruleset


def test_render_with_backup_destination_unset_opens_nothing(
    generator: ModuleType, tmp_path: Path
) -> None:
    cfg = generator.parse(
        {}, config_path=tmp_path / "system.json", resolve=_ok_resolve(["10.2.1.5"])
    )
    ruleset = generator.render_nft(cfg)
    assert "10.2.1.5" not in ruleset
    assert "backup_destination not set" in ruleset
