"""Retention arithmetic (§13.3, §15.3, Q4). Pure date maths — no I/O — over the
archive index, so the ladder is testable without a database or a filesystem.

§13.3/Q4: 14 days local, 7 on the USB, 30 on the network — three independent
clocks, because each destination keeps its own copy and each is pruned only
from what it itself holds (:mod:`proskenion.db.crud.backup`'s ``*_present``
columns), never by deleting the archive's row while another destination
still has it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from proskenion.core.backup_destinations import RETENTION_DAYS, DestinationName
from proskenion.db.crud.backup import ArchiveRow


def cutoff(destination: DestinationName, *, now: datetime) -> datetime:
    """The oldest ``created_at`` that destination still keeps."""
    return now - timedelta(days=RETENTION_DAYS[destination])


def expired(
    rows: Sequence[ArchiveRow], destination: DestinationName, *, now: datetime
) -> list[ArchiveRow]:
    """Rows present at ``destination`` older than its retention window, oldest first.

    ``rows`` is expected to already be filtered to what is present there
    (:func:`proskenion.db.crud.backup.list_archives_present`); this function
    only applies the age cutoff, so a caller filtering by a different
    predicate first still gets correct results.
    """
    limit = cutoff(destination, now=now)
    return sorted(
        (row for row in rows if datetime.fromisoformat(row.created_at) < limit),
        key=lambda row: row.created_at,
    )


__all__ = ["cutoff", "expired"]
