"""Loading the appliance's root-side scripts as modules.

``appliance/bin/*`` are scripts with no ``.py`` suffix, run by the system
Python on the read-only root rather than from the application's venv — the
helper in particular must never import anything out of ``/data/app``, which
the unprivileged application can write (§6.11). Importing them here by path is
how their logic is tested without a Raspberry Pi: every one guards its
``main``, so importing runs nothing.

``appliance/lib`` goes on ``sys.path`` first, because that is what the scripts
themselves do with ``/usr/local/lib/auditorium``.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

APPLIANCE = Path(__file__).resolve().parents[3] / "appliance"
LIB = APPLIANCE / "lib"
BIN = APPLIANCE / "bin"


def load_script(path: Path, name: str) -> ModuleType:
    """Import a suffix-less script by path, under ``name``."""
    if name in sys.modules:
        return sys.modules[name]
    if str(LIB) not in sys.path:
        sys.path.insert(0, str(LIB))
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    # No __pycache__ beside the appliance's scripts: the image build and the
    # systemd harness both install everything in appliance/bin, and a stray
    # directory there is a broken install.
    written = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = written
    return module


@pytest.fixture(scope="session")
def bootstate() -> ModuleType:
    """``appliance/lib/auditorium_bootstate.py`` — the root-side boot-state code."""
    if str(LIB) not in sys.path:
        sys.path.insert(0, str(LIB))
    # As in load_script: no __pycache__ in a directory the image build and the
    # harness install wholesale.
    written = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        import auditorium_bootstate
    finally:
        sys.dont_write_bytecode = written
    return auditorium_bootstate


@pytest.fixture(scope="session")
def slots() -> ModuleType:
    """``appliance/lib/auditorium_slots.py`` — the root-side slot rendering."""
    if str(LIB) not in sys.path:
        sys.path.insert(0, str(LIB))
    written = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        import auditorium_slots
    finally:
        sys.dont_write_bytecode = written
    return auditorium_slots


@pytest.fixture(scope="session")
def helper() -> ModuleType:
    return load_script(BIN / "auditorium-helper", "auditorium_helper")


@pytest.fixture(scope="session")
def rollback() -> ModuleType:
    return load_script(BIN / "auditorium-update-rollback", "auditorium_update_rollback")


@pytest.fixture(scope="session")
def knx_wait() -> ModuleType:
    """``appliance/bin/auditorium-wait-for-knx-gateway`` — knxd.service's ExecStartPre=."""
    return load_script(BIN / "auditorium-wait-for-knx-gateway", "auditorium_wait_for_knx_gateway")


def _load_lib_module(name: str) -> ModuleType:
    """Import one of ``appliance/lib``'s root-side modules by its real name,
    the way ``appliance/bin/*`` scripts do (``sys.path.insert(0, LIB)``)."""
    if str(LIB) not in sys.path:
        sys.path.insert(0, str(LIB))
    written = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        return importlib.import_module(name)
    finally:
        sys.dont_write_bytecode = written


@pytest.fixture(scope="session")
def emergency_reason() -> ModuleType:
    """``appliance/lib/auditorium_emergency_reason.py``."""
    return _load_lib_module("auditorium_emergency_reason")


@pytest.fixture(scope="session")
def device_secret() -> ModuleType:
    """``appliance/lib/auditorium_device_secret.py`` — needs ``cryptography``
    on whatever Python runs this; skip rather than fail where it is absent
    (the real appliance image does not ship ``cryptography`` today)."""
    pytest.importorskip("cryptography")
    return _load_lib_module("auditorium_device_secret")


@pytest.fixture(scope="session")
def fallback_mail() -> ModuleType:
    """``appliance/lib/auditorium_fallback_mail.py``."""
    return _load_lib_module("auditorium_fallback_mail")


@pytest.fixture(scope="session")
def emergency() -> ModuleType:
    """``appliance/lib/auditorium_emergency.py`` — the responder itself,
    importable with nothing from ``proskenion`` (a non-negotiable requirement)."""
    return _load_lib_module("auditorium_emergency")


@pytest.fixture(scope="session")
def image_keys() -> ModuleType:
    """``appliance/lib/auditorium_image_keys.py`` — needs ``cryptography`` on
    whatever Python runs this, the same way ``device_secret`` above does."""
    pytest.importorskip("cryptography")
    return _load_lib_module("auditorium_image_keys")


@pytest.fixture
def helper_dir(tmp_path: Path) -> Iterator[Path]:
    directory = tmp_path / "run" / "helper"
    directory.mkdir(parents=True)
    yield directory
