"""The soak instance's configuration names nothing of the venue's (tests/soak/__main__.py)."""

from __future__ import annotations

import tarfile
import tomllib
from pathlib import Path

from proskenion.config import Environment, parse_config
from tests.soak import rig
from tests.soak.__main__ import main, soak_config


def test_the_soak_config_is_a_valid_bootstrap_config_entirely_under_its_root() -> None:
    config = parse_config(tomllib.loads(soak_config(Path("/data/soak"))))
    assert config.database.path.as_posix() == "/data/soak/auditorium.db"
    assert config.logging.path.as_posix() == "/data/soak/logs"
    assert config.app.data_dir.as_posix() == "/data/soak/data"
    assert config.app.state_dir.as_posix() == "/data/soak/appliance"
    assert config.app.environment is Environment.PRODUCTION
    # KNX goes to the stub over loopback, never knxd's socket or the gateway.
    assert (config.knx.host, config.knx.port) == (rig.HOST, rig.KNXD_PORT)
    assert config.server.host == "127.0.0.1" and config.server.port == 8000


def test_the_bundle_is_the_stubs_and_the_harness_and_nothing_else(tmp_path: Path) -> None:
    out = tmp_path / "soak-bundle.tar.gz"
    assert main(["bundle", str(out)]) == 0
    with tarfile.open(out) as archive:
        names = archive.getnames()
    assert "tests/__init__.py" in names
    assert "tests/soak/__main__.py" in names and "tests/stubs/knxd_stub.py" in names
    assert all(
        n == "tests/__init__.py" or n.startswith(("tests/soak/", "tests/stubs/")) for n in names
    )
    assert not any("__pycache__" in n for n in names)
