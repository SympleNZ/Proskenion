""":class:`DbAddressRegistry` (§7.1, §15.7)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from proskenion.core.knx import AddressDirection
from proskenion.core.knx_registry import DbAddressRegistry
from proskenion.db.connection import MEMORY, Database
from proskenion.db.crud import knx as knx_crud
from proskenion.db.migrations import migrate


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    database = Database()
    await database.open(MEMORY)
    try:
        await migrate(database)
        yield database
    finally:
        await database.close()


async def test_lookup_is_empty_before_reload(db: Database) -> None:
    registry = DbAddressRegistry(db)
    assert registry.lookup("1/0/1") is None


async def test_reload_loads_every_address(db: Database) -> None:
    await knx_crud.create_address(
        db, group_address="1/0/1", name="Stage", dpt="1.001", direction="incoming"
    )
    await knx_crud.create_address(
        db, group_address="1/0/2", name="Stage Status", dpt="5.001", direction="outgoing"
    )
    registry = DbAddressRegistry(db)
    await registry.reload()

    first = registry.lookup("1/0/1")
    assert first is not None
    assert first.dpt == "1.001"
    assert first.direction is AddressDirection.INCOMING

    second = registry.lookup("1/0/2")
    assert second is not None
    assert second.dpt == "5.001"
    assert second.direction is AddressDirection.OUTGOING

    assert registry.lookup("9/9/9") is None


async def test_reload_reflects_a_newly_added_address_without_a_restart(db: Database) -> None:
    """The core scope requirement: a freshly imported/created address is
    decoded by its DPT on the next telegram, without a restart — i.e.
    without reconstructing the registry, only calling reload()."""
    registry = DbAddressRegistry(db)
    await registry.reload()
    assert registry.lookup("2/0/1") is None

    await knx_crud.create_address(
        db, group_address="2/0/1", name="New Address", dpt="9.001", direction="both"
    )
    await registry.reload()

    entry = registry.lookup("2/0/1")
    assert entry is not None
    assert entry.dpt == "9.001"
    assert entry.direction is AddressDirection.BOTH


async def test_reload_drops_a_deleted_address(db: Database) -> None:
    address = await knx_crud.create_address(
        db, group_address="1/0/1", name="Stage", dpt="1.001", direction="incoming"
    )
    registry = DbAddressRegistry(db)
    await registry.reload()
    assert registry.lookup("1/0/1") is not None

    await knx_crud.delete_address(db, address.id)
    await registry.reload()
    assert registry.lookup("1/0/1") is None


async def test_snapshot_is_a_copy(db: Database) -> None:
    await knx_crud.create_address(
        db, group_address="1/0/1", name="Stage", dpt="1.001", direction="incoming"
    )
    registry = DbAddressRegistry(db)
    await registry.reload()
    snapshot = registry.snapshot()
    snapshot.clear()
    assert registry.lookup("1/0/1") is not None
