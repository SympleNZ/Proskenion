"""``network.smtp_relay``'s DNS resolution and firewall rendering (§11.4,
phase-6 contracts §4): a host and a port, resolved at apply time, cached so a
transient DNS failure keeps the last known addresses rather than closing the
port, and never opened to every address.

Loads ``appliance/bin/auditorium-config-apply`` the same way
``tests/unit/appliance/test_firewall_ports.py`` does — it ships with no
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
    loader = importlib.machinery.SourceFileLoader("auditorium_config_apply_smtp", str(GENERATOR))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
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


def _ok_resolve(addresses: list[str]):  # noqa: ANN201 - closure over a test list
    def _resolve(host: str, port: int) -> list[str]:
        return list(addresses)

    return _resolve


def _failing_resolve(host: str, port: int) -> list[str]:
    raise OSError("simulated DNS failure")


# -- parsing and resolution -----------------------------------------------------------


def test_no_smtp_relay_configured_resolves_nothing(generator: ModuleType, tmp_path: Path) -> None:
    cfg = generator.parse(
        {}, config_path=tmp_path / "system.json", resolve=_ok_resolve(["10.2.1.25"])
    )
    assert cfg.smtp_relay_host is None
    assert cfg.smtp_relay_port is None
    assert cfg.smtp_relay_addresses == []


def test_a_resolved_relay_is_cached(generator: ModuleType, tmp_path: Path) -> None:
    config_path = tmp_path / "system.json"
    raw = {"network": {"smtp_relay": {"host": "relay.n4l.co.nz", "port": 25}}}
    cfg = generator.parse(
        raw, config_path=config_path, resolve=_ok_resolve(["10.2.1.25", "10.2.1.26"])
    )
    assert cfg.smtp_relay_host == "relay.n4l.co.nz"
    assert cfg.smtp_relay_port == 25
    assert cfg.smtp_relay_addresses == ["10.2.1.25", "10.2.1.26"]

    cache = json.loads((tmp_path / ".smtp-relay-cache.json").read_text(encoding="utf-8"))
    assert cache == {
        "host": "relay.n4l.co.nz",
        "port": 25,
        "addresses": ["10.2.1.25", "10.2.1.26"],
    }


def test_a_resolution_failure_falls_back_to_the_cache(
    generator: ModuleType, tmp_path: Path
) -> None:
    config_path = tmp_path / "system.json"
    raw = {"network": {"smtp_relay": {"host": "relay.n4l.co.nz", "port": 25}}}
    # First apply resolves and caches.
    generator.parse(raw, config_path=config_path, resolve=_ok_resolve(["10.2.1.25"]))
    # A later apply, with DNS down, keeps the addresses last resolved.
    cfg = generator.parse(raw, config_path=config_path, resolve=_failing_resolve)
    assert cfg.smtp_relay_addresses == ["10.2.1.25"]


def test_a_resolution_failure_with_nothing_cached_yields_no_addresses(
    generator: ModuleType, tmp_path: Path
) -> None:
    """Port 25 is never opened to every address — including when the relay
    that would justify a rule has never once resolved."""
    config_path = tmp_path / "system.json"
    raw = {"network": {"smtp_relay": {"host": "relay.n4l.co.nz", "port": 25}}}
    cfg = generator.parse(raw, config_path=config_path, resolve=_failing_resolve)
    assert cfg.smtp_relay_host == "relay.n4l.co.nz"
    assert cfg.smtp_relay_addresses == []


def test_a_cache_for_a_different_host_is_not_reused(generator: ModuleType, tmp_path: Path) -> None:
    config_path = tmp_path / "system.json"
    generator.parse(
        {"network": {"smtp_relay": {"host": "old-relay.example.nz", "port": 25}}},
        config_path=config_path,
        resolve=_ok_resolve(["10.2.1.25"]),
    )
    cfg = generator.parse(
        {"network": {"smtp_relay": {"host": "new-relay.example.nz", "port": 25}}},
        config_path=config_path,
        resolve=_failing_resolve,
    )
    assert cfg.smtp_relay_addresses == []


def test_an_invalid_relay_object_is_ignored(generator: ModuleType, tmp_path: Path) -> None:
    cfg = generator.parse(
        {"network": {"smtp_relay": "10.2.1.25"}},  # the old, pre-phase-6 shape
        config_path=tmp_path / "system.json",
        resolve=_ok_resolve(["10.2.1.25"]),
    )
    assert cfg.smtp_relay_host is None
    assert cfg.smtp_relay_addresses == []


def test_an_out_of_range_port_is_ignored(generator: ModuleType, tmp_path: Path) -> None:
    cfg = generator.parse(
        {"network": {"smtp_relay": {"host": "relay.n4l.co.nz", "port": 99999}}},
        config_path=tmp_path / "system.json",
        resolve=_ok_resolve(["10.2.1.25"]),
    )
    assert cfg.smtp_relay_host is None


# -- rendering --------------------------------------------------------------------------


def test_render_writes_one_rule_per_resolved_address(
    generator: ModuleType, tmp_path: Path
) -> None:
    raw = {"network": {"smtp_relay": {"host": "relay.n4l.co.nz", "port": 25}}}
    cfg = generator.parse(
        raw, config_path=tmp_path / "system.json", resolve=_ok_resolve(["10.2.1.25", "10.2.1.26"])
    )
    ruleset = generator.render_nft(cfg)
    assert "ip daddr 10.2.1.25 tcp dport 25 accept" in ruleset
    assert "ip daddr 10.2.1.26 tcp dport 25 accept" in ruleset
    # Never a bare "tcp dport 25 accept" with no address (§4: never opened to all).
    assert "\n        tcp dport 25 accept\n" not in ruleset


def test_render_with_no_addresses_opens_nothing(generator: ModuleType, tmp_path: Path) -> None:
    raw = {"network": {"smtp_relay": {"host": "relay.n4l.co.nz", "port": 25}}}
    cfg = generator.parse(raw, config_path=tmp_path / "system.json", resolve=_failing_resolve)
    ruleset = generator.render_nft(cfg)
    assert "dport 25 accept" not in ruleset
    assert "did not resolve" in ruleset


def test_render_with_smtp_relay_unset_opens_nothing(
    generator: ModuleType, tmp_path: Path
) -> None:
    cfg = generator.parse(
        {}, config_path=tmp_path / "system.json", resolve=_ok_resolve(["10.2.1.25"])
    )
    ruleset = generator.render_nft(cfg)
    assert "10.2.1.25" not in ruleset
    assert "network.smtp_relay not set" in ruleset
