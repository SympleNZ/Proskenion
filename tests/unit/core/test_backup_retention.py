"""Retention arithmetic (§13.3, Q4): 14 days local, 7 USB, 30 network."""

from __future__ import annotations

from datetime import datetime

from proskenion.core.backup_retention import cutoff, expired
from proskenion.db.crud.backup import ArchiveRow

NOW = datetime.fromisoformat("2026-09-20T03:00:00+12:00")


def _row(archive_id: str, created_at: str) -> ArchiveRow:
    return ArchiveRow(
        id=archive_id,
        created_at=created_at,
        source="scheduled",
        size_bytes=100,
        sha256="a" * 64,
        schema_version=8,
        app_version="0.1.0",
        local_present=True,
        usb_present=True,
        network_present=True,
        verified_at=None,
        untrusted=False,
        untrusted_reason=None,
    )


def test_cutoff_is_retention_days_before_now() -> None:
    assert cutoff("local", now=NOW).isoformat() == "2026-09-06T03:00:00+12:00"
    assert cutoff("usb", now=NOW).isoformat() == "2026-09-13T03:00:00+12:00"
    assert cutoff("network", now=NOW).isoformat() == "2026-08-21T03:00:00+12:00"


def test_expired_uses_the_right_window_per_destination() -> None:
    # 10 days old: past the USB's 7-day window, still inside local's 14 and
    # network's 30.
    ten_days = _row("ten-days-old", "2026-09-10T03:00:00+12:00")
    rows = [ten_days]

    assert expired(rows, "usb", now=NOW) == [ten_days]
    assert expired(rows, "local", now=NOW) == []
    assert expired(rows, "network", now=NOW) == []


def test_expired_is_empty_for_a_fresh_archive() -> None:
    fresh = _row("today", "2026-09-20T02:00:00+12:00")
    assert expired([fresh], "usb", now=NOW) == []
    assert expired([fresh], "local", now=NOW) == []
    assert expired([fresh], "network", now=NOW) == []


def test_expired_is_oldest_first() -> None:
    old = _row("oldest", "2026-01-01T03:00:00+12:00")
    older = _row("older-still", "2025-01-01T03:00:00+12:00")
    result = expired([old, older], "local", now=NOW)
    assert [r.id for r in result] == ["older-still", "oldest"]


def test_a_boundary_archive_exactly_at_the_cutoff_is_not_expired() -> None:
    """§13.3's "14 days" keeps an archive exactly 14 days old, not just newer."""
    boundary = _row("boundary", cutoff("local", now=NOW).isoformat())
    assert expired([boundary], "local", now=NOW) == []
