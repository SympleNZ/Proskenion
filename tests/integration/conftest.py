"""Fixtures for the integration suite (spec §22.4).

A real FastAPI application over an in-memory SQLite database with every
migration applied, driven through an HTTP client, with the §12.1 boot sequence
actually run — the device manager, the broadcaster, the health poller and the
state store are the real ones, and the only stubs are at the far end of a
protocol: decision Q8's ``video_matrix/stub`` over the loopback transport, and
for the lighting journeys a stub knxd (``knxd``) and a stub Art-Net node
(``artnet``), which the real KNX subsystem and the real ``artnet`` driver reach
over real sockets.

The database here is a **fresh appliance**: the seed's placeholder credentials
are left in place and ``first_run_completed`` is absent, because the journey
under test is commissioning. ``tests/unit/api/conftest.py`` does the opposite —
it represents an appliance that was commissioned long ago.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from proskenion.api.app import create_app
from proskenion.config import (
    AppSection,
    Config,
    DatabaseSection,
    Environment,
    KnxSection,
    LoggingSection,
)
from proskenion.core.auth import JWT_SECRET_FILENAME, TokenService
from proskenion.core.platform import DevelopmentPlatform
from proskenion.core.ratelimit import CLEAR_LOCKOUTS_FILENAME, RateLimiter
from proskenion.db.connection import MEMORY, Database
from proskenion.db.migrations import migrate
from tests.stubs.artnet_stub import ArtNetStub
from tests.stubs.knxd_stub import KnxdStub


@pytest.fixture
def knx_section() -> KnxSection:
    """knxd as an appliance with none reachable would find it: the default socket.

    A development machine has no ``/run/knx``, so the KNX subsystem reports
    itself unavailable and keeps retrying, which is what the Phase 1 journeys
    run against. A module that drives KNX overrides this fixture with one
    pointing at a :class:`~tests.stubs.knxd_stub.KnxdStub` (the ``knxd``
    fixture below).
    """
    return KnxSection()


@pytest.fixture
def config(tmp_path: Path, knx_section: KnxSection) -> Config:
    """Development, so an unregistered state write raises rather than logs (B39)."""
    return Config(
        database=DatabaseSection(path=tmp_path / "auditorium.db"),
        logging=LoggingSection(path=tmp_path / "logs"),
        app=AppSection(
            environment=Environment.DEVELOPMENT,
            state_dir=tmp_path / "appliance",
            data_dir=tmp_path / "data",
        ),
        knx=knx_section,
    )


@pytest.fixture
async def knxd() -> AsyncIterator[KnxdStub]:
    """A stub knxd on a free loopback port, for the application's KNX subsystem.

    It outlives the application: pytest tears fixtures down in reverse, so the
    lifespan's shutdown runs while the stub is still answering.
    """
    async with KnxdStub() as stub:
        yield stub


@pytest.fixture
async def artnet() -> AsyncIterator[ArtNetStub]:
    """A stub Art-Net node on a free loopback port, recording every ArtDmx frame."""
    stub = ArtNetStub()
    await stub.start()
    try:
        yield stub
    finally:
        await stub.stop()


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    """An in-memory database with the schema and the seed, and nothing else."""
    database = Database()
    await database.open(MEMORY)
    try:
        await migrate(database)
        yield database
    finally:
        await database.close()


@pytest.fixture
def app(config: Config, db: Database) -> FastAPI:
    return create_app(
        config,
        db=db,
        tokens=TokenService(config.app.state_dir / JWT_SECRET_FILENAME),
        limiter=RateLimiter(signal_path=config.app.state_dir / CLEAR_LOCKOUTS_FILENAME),
    )


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """The application as it actually runs: §12.1's boot sequence, then requests.

    ``raise_app_exceptions=False`` so a 500 is observed as a response rather
    than re-raised, and an https base URL so the cookie jar returns the
    ``Secure`` session cookie.
    """
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with app.router.lifespan_context(app):
        # §5.4: the platform layer is injected, never reached for. The boot
        # sequence detected the real one; the wizard's certificate step writes
        # under ``platform.data_dir()``, which is ``/data`` and has no
        # configuration key, so the test's own platform is put in its place
        # and nothing lands outside tmp_path.
        config: Config = app.state.config
        app.state.platform = DevelopmentPlatform(
            appliance_dir=Path(config.app.state_dir),
            data_dir=Path(config.database.path).parent / "data",
        )
        async with AsyncClient(transport=transport, base_url="https://test") as http:
            yield http
