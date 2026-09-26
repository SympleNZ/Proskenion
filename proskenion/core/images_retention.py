"""Retention arithmetic for captured system images (§13.6, Q4). Pure —
no I/O — over the image index, the same shape
:mod:`proskenion.core.backup_retention` is for archives.

Q4: "the USB keeps 2 images, checks capacity first, and evicts images,
never archives." §13.6: "Retain the last 3" (locally). Both are **counts**,
not ages — a captured image is a rare, deliberate act, not a nightly job, so
"the last N" is a truer policy than any calendar window. The USB's capacity
eviction is separate, handled by
:func:`proskenion.core.backup_destinations.evict_images_for_space` (called
on every write to the stick regardless of what retention here would have
kept); this module only decides which images a **destination whose capacity
was not the constraint** should give up once it holds more than its count.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Literal

from proskenion.db.crud.images import ImageRow

ImageDestinationName = Literal["local", "usb"]

#: §13.6, Q4.
KEEP: Final[dict[ImageDestinationName, int]] = {"local": 3, "usb": 2}


def expired(rows: Sequence[ImageRow], destination: ImageDestinationName) -> list[ImageRow]:
    """Rows present at ``destination`` beyond the newest :data:`KEEP` there,
    oldest first — the order eviction and pruning walk.

    ``rows`` is expected to already be filtered to what is present there
    (:func:`proskenion.db.crud.images.list_images_present`, itself already
    oldest-first); this only applies the count, exactly as
    :func:`proskenion.core.backup_retention.expired` applies the date cutoff
    for archives.
    """
    keep = KEEP[destination]
    ordered = sorted(rows, key=lambda row: row.created_at)
    if len(ordered) <= keep:
        return []
    return ordered[: len(ordered) - keep]


__all__ = ["KEEP", "ImageDestinationName", "expired"]
