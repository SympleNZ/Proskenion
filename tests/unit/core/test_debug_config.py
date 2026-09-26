"""``debug.json`` — per-module DEBUG toggling (spec §4.10).

Level changes go through :mod:`proskenion.logging`'s real logger objects,
never faked, so a test that reads ``logging.getLogger(name).level`` is
reading the live effect ``reload_levels`` produced.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from proskenion.core import debug_config
from proskenion.logging import reload_levels


@pytest.fixture(autouse=True)
def _clear_overrides() -> Iterator[None]:
    """Every override reverts to NOTSET before and after each test."""
    reload_levels({})
    yield
    reload_levels({})


def test_available_loggers_are_the_top_level_packages() -> None:
    names = debug_config.available_loggers()
    assert names == sorted(names)
    assert {"proskenion.api", "proskenion.core", "proskenion.db"} <= set(names)
    # A submodule, not a top-level package, is never offered.
    assert "proskenion.core.knx" not in names


def test_missing_file_means_every_module_at_info(tmp_path: Path) -> None:
    assert debug_config.load_overrides(tmp_path) == {}
    applied = debug_config.apply(tmp_path)
    assert applied == {}
    assert logging.getLogger("proskenion.core").level == logging.NOTSET


def test_set_enabled_persists_and_takes_effect_without_a_restart(tmp_path: Path) -> None:
    debug_config.set_enabled(tmp_path, "proskenion.core", True)

    # Persisted — a fresh read (as at startup) sees it too.
    assert debug_config.load_overrides(tmp_path) == {"proskenion.core": True}
    path = debug_config.debug_config_path(tmp_path)
    assert json.loads(path.read_text(encoding="utf-8")) == {"proskenion.core": True}

    # Live: no restart, no re-application needed.
    assert logging.getLogger("proskenion.core").level == logging.DEBUG
    # The logger hierarchy carries it to every module beneath the package.
    assert logging.getLogger("proskenion.core.knx").getEffectiveLevel() == logging.DEBUG
    assert logging.getLogger("proskenion.api").level == logging.NOTSET

    debug_config.set_enabled(tmp_path, "proskenion.core", False)
    assert logging.getLogger("proskenion.core").level == logging.NOTSET
    assert debug_config.load_overrides(tmp_path) == {"proskenion.core": False}


def test_set_enabled_rejects_an_unknown_logger(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="proskenion.bogus"):
        debug_config.set_enabled(tmp_path, "proskenion.bogus", True)
    assert not debug_config.debug_config_path(tmp_path).exists()


def test_apply_reads_the_file_at_startup(tmp_path: Path) -> None:
    """The file is re-read at startup: writing it directly, the way an admin
    session would have left it before a restart, is picked up by ``apply``
    without going through ``set_enabled`` first."""
    path = debug_config.debug_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"proskenion.api": True}), encoding="utf-8", newline="\n")

    applied = debug_config.apply(tmp_path)
    assert applied == {"proskenion.api": True}
    assert logging.getLogger("proskenion.api").level == logging.DEBUG


def test_malformed_json_is_logged_and_ignored_not_fatal(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = debug_config.debug_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not valid json", encoding="utf-8", newline="\n")

    with caplog.at_level(logging.WARNING, logger="proskenion.core.debug_config"):
        applied = debug_config.apply(tmp_path)

    assert applied == {}
    assert logging.getLogger("proskenion.core").level == logging.NOTSET
    assert any("not valid JSON" in record.message for record in caplog.records)


def test_a_document_that_is_not_a_json_object_is_ignored(tmp_path: Path) -> None:
    path = debug_config.debug_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("[1, 2, 3]", encoding="utf-8", newline="\n")
    assert debug_config.load_overrides(tmp_path) == {}


def test_unrecognised_entries_are_dropped_but_recognised_ones_still_apply(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = debug_config.debug_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    doc = {"proskenion.core": True, "proskenion.nonexistent": True, "proskenion.api": "yes"}
    path.write_text(json.dumps(doc), encoding="utf-8", newline="\n")

    with caplog.at_level(logging.WARNING, logger="proskenion.core.debug_config"):
        overrides = debug_config.load_overrides(tmp_path)

    assert overrides == {"proskenion.core": True}
    assert any("does not recognise" in record.message for record in caplog.records)
