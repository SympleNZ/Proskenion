"""Fixtures shared by the core tests: configurations and an in-memory database."""

from collections.abc import AsyncIterator

import pytest

from proskenion.config import Config, parse_config
from proskenion.db.connection import MEMORY, Database
from proskenion.db.migrations import migrate


def _config(environment: str) -> Config:
    return parse_config(
        {
            "database": {"path": "/data/db/auditorium.db"},
            "logging": {"path": "/data/logs"},
            "app": {"environment": environment},
        }
    )


@pytest.fixture
def dev_config() -> Config:
    return _config("development")


@pytest.fixture
def prod_config() -> Config:
    return _config("production")


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    """An in-memory database with the shipped schema and seed applied."""
    database = Database()
    await database.open(MEMORY)
    try:
        await migrate(database)
        yield database
    finally:
        await database.close()
