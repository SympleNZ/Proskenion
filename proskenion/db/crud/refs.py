"""Shared helpers for the Phase 2 CRUD modules — KNX, lighting, rules, scenes.

Kept separate from :mod:`proskenion.db.crud.base`, which is Phase 1 work with
its own pinned tests: ``base.InUseError`` takes only ``table`` and ``row_id``,
and several Phase 1 tests construct it exactly that way. The delete-with-
references and constraint-naming behaviour the Phase 2 entities need (§16.1,
§21.22, §22.2) is layered on top here instead of changing that signature.
"""

from __future__ import annotations

from dataclasses import dataclass

from proskenion.db.crud import base


@dataclass(frozen=True, slots=True)
class Reference:
    """One row that refers to an entity — the shape a 409 ``in_use`` response
    and ``GET /{entity}/{id}/references`` are built from (§16.1)."""

    entity: str
    id: int
    name: str


class InUseError(base.InUseError):
    """``base.InUseError`` plus the rows that reference the blocked delete."""

    def __init__(self, table: str, row_id: int, references: list[Reference]) -> None:
        super().__init__(table, row_id)
        self.references = references


class ConstraintError(base.CrudError):
    """A ``CHECK`` or ``UNIQUE`` constraint was violated by an insert or update.

    Raised instead of letting ``sqlite3.IntegrityError`` escape the CRUD layer,
    naming the constraint, so the API layer can return 422
    ``validation_failed`` rather than 500 ``internal_error`` (§16.1, §22.2).
    """

    def __init__(self, constraint: str, message: str) -> None:
        super().__init__(f"{constraint}: {message}")
        self.constraint = constraint
        self.raw_message = message
