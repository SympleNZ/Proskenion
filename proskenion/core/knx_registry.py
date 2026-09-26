"""A database-backed :class:`~proskenion.core.knx.AddressRegistry` (§7.1, §15.7).

:mod:`proskenion.core.knx` deliberately takes its address library as an
injected :class:`~proskenion.core.knx.AddressRegistry` protocol rather than
reading the database itself — "the schema for it is other work happening in
parallel" (its module docstring). This module is that other work: a registry
over ``knx_group_addresses``, loaded at startup and reloaded whenever the
library changes, so a newly imported or edited address is decoded by its DPT
on the next telegram without a restart.

:meth:`KnxSubsystem._handle_incoming` calls :meth:`DbAddressRegistry.lookup`
synchronously, from inside the subsystem's read loop — it cannot ``await`` a
database query per telegram. So the whole library is loaded into a plain
``dict`` up front by :meth:`reload`, which the caller (the lifespan at
startup, the API layer after every address create/update/delete and after a
confirmed import) awaits explicitly. Between reloads, ``lookup`` answers from
the in-memory snapshot — exactly what :class:`~proskenion.core.knx.InMemoryAddressRegistry`
does, except this one knows how to refill itself from SQLite.
"""

from __future__ import annotations

from proskenion.core.knx import AddressDirection, AddressEntry
from proskenion.db.connection import Database
from proskenion.db.crud import knx as knx_crud


class DbAddressRegistry:
    """:class:`AddressRegistry` over ``knx_group_addresses``, cached in memory.

    Satisfies :class:`~proskenion.core.knx.AddressRegistry` structurally (it is
    a ``Protocol``): :meth:`lookup` is synchronous and answers from the last
    :meth:`reload`. Construct one per application, call :meth:`reload` once
    before the KNX subsystem starts reading telegrams, and call it again after
    any write to the library.
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        self._entries: dict[str, AddressEntry] = {}

    def lookup(self, group_address: str) -> AddressEntry | None:
        return self._entries.get(group_address)

    async def reload(self) -> None:
        """Refill the in-memory snapshot from the database."""
        rows = await knx_crud.list_addresses(self._db)
        self._entries = {
            row.group_address: AddressEntry(row.dpt, AddressDirection(row.direction))
            for row in rows
        }

    def snapshot(self) -> dict[str, AddressEntry]:
        """A copy of the current in-memory library, keyed by group address."""
        return dict(self._entries)


__all__ = ["DbAddressRegistry"]
