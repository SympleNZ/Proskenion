"""Database fixtures: an in-memory ``Database`` with every migration applied."""

from collections.abc import AsyncIterator

import pytest

from proskenion.db.connection import MEMORY, Database
from proskenion.db.migrations import migrate


@pytest.fixture
async def raw_db() -> AsyncIterator[Database]:
    """An open, empty in-memory database with no migrations applied."""
    db = Database()
    await db.open(MEMORY)
    try:
        yield db
    finally:
        await db.close()


@pytest.fixture
async def db(raw_db: Database) -> Database:
    """An in-memory database with the shipped schema and seed applied."""
    await migrate(raw_db)
    return raw_db
