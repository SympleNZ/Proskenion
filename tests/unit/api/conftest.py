"""Fixtures for API tests: a configured app over an open database and an httpx client.

The application is built with an already-open in-memory database, a token
service and a rate limiter that share one fake clock, so expiry and lockouts
are tested by advancing time rather than sleeping. Test accounts are hashed
with a low bcrypt cost so a login costs milliseconds.
"""

from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from proskenion.api.app import API_PREFIX, create_app
from proskenion.api.deps import current_session, require_admin
from proskenion.api.errors import ApiError, ErrorCode
from proskenion.config import (
    AppSection,
    Config,
    DatabaseSection,
    Environment,
    LoggingSection,
    ServerSection,
)
from proskenion.core import auth, setup
from proskenion.core.auth import TokenClaims, TokenService
from proskenion.core.ratelimit import CLEAR_LOCKOUTS_FILENAME, RateLimiter
from proskenion.db.connection import MEMORY, Database
from proskenion.db.crud import hirer as hirer_crud
from proskenion.db.crud import system_state
from proskenion.db.crud import users as users_crud
from proskenion.db.crud.base import AUCKLAND
from proskenion.db.migrations import migrate
from proskenion.logging import APPLICATION_LOG_NAME, configure_logging, shutdown_logging

# A string that must never appear in a response body.
SECRET_EXCEPTION_TEXT = "secret-exception-text-7f3a"

ADMIN_PASSWORD = "admin-password-long-enough"
OPERATOR_PASSWORD = "operator-password-long-enough"
HIRER_PIN = "246810"
TEST_ROUNDS = 4  # bcrypt cost for test accounts; production is 12

START = datetime(2026, 9, 10, 18, 0, 0, tzinfo=AUCKLAND)


class FakeClock:
    """One clock for tokens (aware datetime) and the limiter (monotonic seconds)."""

    def __init__(self, start: datetime = START) -> None:
        self.current = start

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return (self.current - START).total_seconds()

    def advance(self, *, seconds: float = 0, minutes: float = 0, hours: float = 0) -> None:
        self.current += timedelta(seconds=seconds, minutes=minutes, hours=hours)


class LevelBody(BaseModel):
    level: float
    name: str


def _add_probe_routes(app: FastAPI) -> None:
    """Routes that exercise each failure path. Test-only."""

    @app.get(f"{API_PREFIX}/probe/api-error")
    async def api_error() -> None:
        raise ApiError(
            ErrorCode.PERMISSION_DENIED,
            "Channel 3 is not available to this session",
            {"channel_id": 3},
        )

    @app.get(f"{API_PREFIX}/probe/rate-limited")
    async def rate_limited() -> None:
        raise ApiError(
            ErrorCode.RATE_LIMITED, detail={"retry_after": 30}, headers={"Retry-After": "30"}
        )

    @app.post(f"{API_PREFIX}/probe/validate")
    async def validate(body: LevelBody) -> dict[str, float]:
        return {"level": body.level}

    @app.get(f"{API_PREFIX}/probe/http/{{status}}")
    async def http_exception(status: int) -> None:
        raise HTTPException(status_code=status, detail=f"http {status}")

    @app.get(f"{API_PREFIX}/probe/boom")
    async def boom() -> None:
        raise RuntimeError(SECRET_EXCEPTION_TEXT)

    @app.get(f"{API_PREFIX}/probe/ok")
    async def ok() -> dict[str, bool]:
        return {"ok": True}

    @app.get(f"{API_PREFIX}/probe/whoami")
    async def whoami(claims: Annotated[TokenClaims, Depends(current_session)]) -> dict[str, Any]:
        """An ordinary authenticated request: must never extend the session."""
        return {"tier": claims.tier, "session_id": claims.session_id}

    @app.get(f"{API_PREFIX}/probe/admin-only")
    async def admin_only(claims: Annotated[TokenClaims, Depends(require_admin)]) -> dict[str, str]:
        return {"tier": claims.tier}


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "appliance"


@pytest.fixture
def config(tmp_path: Path, state_dir: Path) -> Config:
    return Config(
        database=DatabaseSection(path=tmp_path / "auditorium.db"),
        logging=LoggingSection(path=tmp_path / "logs"),
        # A real, writable directory rather than AppSection's "/data" default:
        # the devices API mirrors the devices table into
        # <data_dir>/config/system.json on every save (contracts §4), and the
        # helper (proskenion.core.helper.HelperClient) writes its request
        # files under <data_dir>/run/helper. Nothing here is privileged, so
        # a tmp directory is a faithful stand-in.
        app=AppSection(state_dir=state_dir, data_dir=tmp_path / "data"),
    )


@pytest.fixture
def dev_config(config: Config, state_dir: Path) -> Config:
    return config.model_copy(
        update={
            "app": AppSection(
                environment=Environment.DEVELOPMENT,
                state_dir=state_dir,
                data_dir=config.app.data_dir,
            )
        }
    )


@pytest.fixture
def hostname_config(config: Config) -> Config:
    return config.model_copy(update={"server": ServerSection(hostname="av.school.nz")})


@pytest.fixture
def log_file(config: Config) -> Iterator[Path]:
    configure_logging(config)
    try:
        yield Path(config.logging.path) / APPLICATION_LOG_NAME
    finally:
        shutdown_logging()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    """An in-memory database with the schema, seed and the test staff passwords."""
    database = Database()
    await database.open(MEMORY)
    try:
        await migrate(database)
        await users_crud.set_password_hash(
            database, "admin", auth.hash_secret(ADMIN_PASSWORD, rounds=TEST_ROUNDS)
        )
        await users_crud.set_password_hash(
            database, "operator", auth.hash_secret(OPERATOR_PASSWORD, rounds=TEST_ROUNDS)
        )
        await hirer_crud.set_pin_hash(
            database, auth.hash_secret(HIRER_PIN, rounds=TEST_ROUNDS), updated_by=None
        )
        # A commissioned appliance: the first-run wizard committed long ago, so
        # the §10.4 gate is inert. tests/unit/api/test_setup.py builds its own
        # database without this row to exercise first run itself.
        await system_state.set(
            database, setup.SYSTEM_DOMAIN, setup.FIRST_RUN_COMPLETED_KEY, "true"
        )
        yield database
    finally:
        await database.close()


@pytest.fixture
def tokens(state_dir: Path, clock: FakeClock) -> TokenService:
    return TokenService(state_dir / auth.JWT_SECRET_FILENAME, clock=clock.now)


@pytest.fixture
def limiter(state_dir: Path, clock: FakeClock) -> RateLimiter:
    return RateLimiter(clock=clock.monotonic, signal_path=state_dir / CLEAR_LOCKOUTS_FILENAME)


def build_app(config: Config, db: Database, tokens: TokenService, limiter: RateLimiter) -> FastAPI:
    application = create_app(config, db=db, tokens=tokens, limiter=limiter)
    _add_probe_routes(application)
    return application


@pytest.fixture
def app(config: Config, db: Database, tokens: TokenService, limiter: RateLimiter) -> FastAPI:
    return build_app(config, db, tokens, limiter)


def make_client(app: FastAPI) -> AsyncClient:
    # raise_app_exceptions=False: Starlette re-raises unhandled exceptions after
    # sending the 500 response, and we want to see the response. The base URL
    # is https so the cookie jar returns the Secure session cookie.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="https://test")


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with make_client(app) as http:
        yield http
