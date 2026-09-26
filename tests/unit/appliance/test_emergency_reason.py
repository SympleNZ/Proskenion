"""``appliance/lib/auditorium_emergency_reason.py`` — the file every entry
path into emergency mode writes before starting the responder (§4.6, §16.7,
contracts).
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest


def test_write_then_read_round_trips(tmp_path: Path, emergency_reason: ModuleType) -> None:
    path = tmp_path / "emergency-reason.json"
    written = emergency_reason.write("disk_full", "only 42 bytes free", path=path)
    assert written["reason"] == "disk_full"
    assert written["detail"] == "only 42 bytes free"
    assert written["at"]  # an ISO 8601 timestamp was filled in

    read_back = emergency_reason.read(path)
    assert read_back == written


def test_every_closed_set_reason_is_accepted(tmp_path: Path, emergency_reason: ModuleType) -> None:
    for reason in emergency_reason.REASONS:
        path = tmp_path / f"{reason}.json"
        emergency_reason.write(reason, "detail", path=path)
        assert emergency_reason.read(path)["reason"] == reason


def test_a_reason_outside_the_closed_set_is_refused(
    tmp_path: Path, emergency_reason: ModuleType
) -> None:
    with pytest.raises(ValueError):
        emergency_reason.write("out_of_disk_space", "not a real reason", path=tmp_path / "x.json")


def test_reading_a_missing_file_is_none_not_an_error(
    tmp_path: Path, emergency_reason: ModuleType
) -> None:
    assert emergency_reason.read(tmp_path / "does-not-exist.json") is None


def test_reading_a_malformed_file_is_none_not_an_error(
    tmp_path: Path, emergency_reason: ModuleType
) -> None:
    path = tmp_path / "emergency-reason.json"
    path.write_text("not json at all", encoding="utf-8")
    assert emergency_reason.read(path) is None

    path.write_text('{"reason": "not_in_the_closed_set"}', encoding="utf-8")
    assert emergency_reason.read(path) is None


def test_the_write_is_atomic_and_leaves_no_temporary_files(
    tmp_path: Path, emergency_reason: ModuleType
) -> None:
    path = tmp_path / "emergency-reason.json"
    emergency_reason.write("migration_failed", "no version below the running one", path=path)
    leftovers = [entry.name for entry in tmp_path.iterdir() if entry.name != path.name]
    assert leftovers == []


def test_clear_removes_the_file_and_is_safe_to_call_twice(
    tmp_path: Path, emergency_reason: ModuleType
) -> None:
    path = tmp_path / "emergency-reason.json"
    emergency_reason.write("data_readonly", "remounted read-only", path=path)
    emergency_reason.clear(path)
    assert not path.exists()
    emergency_reason.clear(path)  # does not raise
