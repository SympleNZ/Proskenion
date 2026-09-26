"""Retention arithmetic for system images (§13.6, Q4): the newest 3 stay
local, the newest 2 stay on the USB stick — counts, not ages, because a
capture is rare and deliberate rather than nightly."""

from __future__ import annotations

from proskenion.core.images_retention import KEEP, expired
from proskenion.db.crud.images import ImageRow


def _row(image_id: str, created_at: str) -> ImageRow:
    return ImageRow(
        id=image_id,
        filename=f"{image_id}.img.gz",
        created_at=created_at,
        slot="a",
        version="v1.0.0",
        size_bytes=100,
        sha256="a" * 64,
        key_id="deadbeef",
        local_present=True,
        usb_present=True,
    )


def test_the_counts_are_q4s() -> None:
    assert KEEP == {"local": 3, "usb": 2}


def test_fewer_than_the_limit_expires_nothing() -> None:
    rows = [_row("a", "2026-09-01T00:00:00+12:00"), _row("b", "2026-09-02T00:00:00+12:00")]
    assert expired(rows, "local") == []
    assert expired(rows, "usb") == []


def test_exactly_the_limit_expires_nothing() -> None:
    rows = [_row(str(n), f"2026-09-0{n}T00:00:00+12:00") for n in range(1, 4)]
    assert expired(rows, "local") == []


def test_beyond_the_limit_the_oldest_go_first() -> None:
    rows = [_row(str(n), f"2026-09-0{n}T00:00:00+12:00") for n in range(1, 5)]  # 1..4
    stale = expired(rows, "local")
    assert [r.id for r in stale] == ["1"]

    stale_usb = expired(rows, "usb")
    assert [r.id for r in stale_usb] == ["1", "2"]


def test_order_of_the_input_does_not_matter() -> None:
    newest = _row("newest", "2026-09-04T00:00:00+12:00")
    oldest = _row("oldest", "2026-09-01T00:00:00+12:00")
    middle = _row("middle", "2026-09-02T00:00:00+12:00")
    another = _row("another", "2026-09-03T00:00:00+12:00")
    # Deliberately not sorted, unlike list_images_present's own contract.
    rows = [newest, oldest, another, middle]
    stale = expired(rows, "local")
    assert [r.id for r in stale] == ["oldest"]
