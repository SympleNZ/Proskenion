"""Shared pytest configuration.

Hardware-in-the-loop tests (spec §22.6) live in tests/hil/ and are skipped
unless ``--run-hil`` is passed explicitly.

``tools/perf``'s self-test (``tests/unit/tools/test_perf_harness.py``)
is marked ``perf`` and skipped the same way, unless ``--run-perf`` is
passed: it runs a real ``uvicorn.Server`` and real WebSocket connections
against the device stubs, which is slower than the rest of the default
suite.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

#: The production data directory (``AppSection.data_dir``'s default). A test
#: that leaves it at that default writes the appliance's real configuration —
#: on a developer's machine, straight into the filesystem root.
PRODUCTION_DATA_DIR = Path("/data")


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-hil",
        action="store_true",
        default=False,
        help="run hardware-in-the-loop tests against the bench rig",
    )
    parser.addoption(
        "--run-perf",
        action="store_true",
        default=False,
        help="run tools/perf's self-test (P7-T8) against the in-process app over a real socket",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not config.getoption("--run-hil"):
        skip_hil = pytest.mark.skip(reason="hardware-in-the-loop; pass --run-hil to run")
        for item in items:
            if "hil" in item.keywords:
                item.add_marker(skip_hil)
    if not config.getoption("--run-perf"):
        skip_perf = pytest.mark.skip(reason="tools/perf self-test; pass --run-perf to run")
        for item in items:
            if "perf" in item.keywords:
                item.add_marker(skip_perf)


@pytest.fixture(autouse=True)
def _never_bind_the_real_artnet_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the one Art-Net socket at a free port for every test.

    In production it binds UDP 6454 (§7.2.5). On a development machine that
    port may belong to lighting software, or to another test run in a second
    checkout, and two sockets sharing it would steal each other's datagrams.
    Port 0 lets the OS choose; a test that needs a fixed port sets its own.
    """
    from proskenion.core.dmx.endpoint import ArtNetEndpoint

    monkeypatch.setattr(ArtNetEndpoint, "default_port", 0)


@pytest.fixture(autouse=True)
def _never_write_the_production_data_dir() -> Iterator[None]:
    """Fail the test that creates the real ``/data`` (contracts §4).

    Configuration writes — ``system.json``, the helper's requests, boot state —
    resolve under ``AppSection.data_dir``, whose default is the appliance's own
    ``/data``. A fixture that forgets to override it writes there for real,
    which a passing run would otherwise never mention. Per test rather than per
    session, so the failure names the test that did it.
    """
    existed = PRODUCTION_DATA_DIR.exists()
    yield
    if not existed and PRODUCTION_DATA_DIR.exists():
        raise AssertionError(
            f"this test created {PRODUCTION_DATA_DIR}: a Config was built without "
            "app.data_dir pointing at a temporary directory"
        )
