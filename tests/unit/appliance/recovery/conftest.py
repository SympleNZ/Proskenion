"""Loading the recovery environment's standalone modules.

``appliance/recovery/lib`` goes on ``sys.path`` first, the same pattern
``tests/unit/appliance/conftest.py`` uses for the main appliance's root-side
scripts: these modules are installed onto the recovery image's own root by
``appliance/recovery/build.sh`` and must import with nothing beyond the
standard library, ``proskenion.core.packages`` and
``appliance/lib/auditorium_slots.py`` (both of which the build also installs
alongside them, and both of which are already importable from this checkout).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

RECOVERY_LIB = Path(__file__).resolve().parents[4] / "appliance" / "recovery" / "lib"
RECOVERY_WEBAPP = Path(__file__).resolve().parents[4] / "appliance" / "recovery" / "webapp"
APPLIANCE_LIB = Path(__file__).resolve().parents[4] / "appliance" / "lib"


def _load(name: str) -> ModuleType:
    if str(RECOVERY_LIB) not in sys.path:
        sys.path.insert(0, str(RECOVERY_LIB))
    if str(APPLIANCE_LIB) not in sys.path:
        sys.path.insert(0, str(APPLIANCE_LIB))
    written = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        if name in sys.modules:
            del sys.modules[name]
        import importlib

        return importlib.import_module(name)
    finally:
        sys.dont_write_bytecode = written


@pytest.fixture(scope="session")
def partitioning() -> ModuleType:
    """``appliance/recovery/lib/recovery_partitioning.py``."""
    return _load("recovery_partitioning")


@pytest.fixture(scope="session")
def archive() -> ModuleType:
    """``appliance/recovery/lib/recovery_archive.py``."""
    return _load("recovery_archive")


@pytest.fixture(scope="session")
def image() -> ModuleType:
    """``appliance/recovery/lib/recovery_image.py``."""
    return _load("recovery_image")


@pytest.fixture(scope="session")
def diagnostics() -> ModuleType:
    """``appliance/recovery/lib/recovery_diagnostics.py``."""
    return _load("recovery_diagnostics")


@pytest.fixture(scope="session")
def network() -> ModuleType:
    """``appliance/recovery/lib/recovery_network.py``."""
    return _load("recovery_network")


@pytest.fixture(scope="session")
def destinations() -> ModuleType:
    """``appliance/recovery/lib/recovery_destinations.py``."""
    return _load("recovery_destinations")


@pytest.fixture
def webapp_module() -> ModuleType:
    """``appliance/recovery/webapp/app.py`` — the Flask application module."""
    if str(RECOVERY_WEBAPP) not in sys.path:
        sys.path.insert(0, str(RECOVERY_WEBAPP))
    return _load("app")
