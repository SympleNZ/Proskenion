"""A refused ``WS /ws`` upgrade must never make
uvicorn log "ASGI callable returned without completing handshake." as an
ERROR (spec §16.8, §6.12; ``proskenion/api/ws.py`` and ``deps.py``).

Every test here runs a **real** ``uvicorn.Server`` against a real TCP port
and a real ``websockets`` client, deliberately not Starlette's
``TestClient``: ``TestClient`` talks ASGI in-process and has none of
uvicorn's own per-connection handshake bookkeeping, so it cannot reproduce
the bug this guards — it was already passing before the fix
(``tests/unit/api/test_ws.py::test_an_unauthenticated_upgrade_and_a_bad_origin_are_refused``),
which is exactly why the CM5's log line went unnoticed until 25 September.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import uvicorn
import websockets
from websockets.exceptions import InvalidStatus

from proskenion.api.app import API_PREFIX, create_app
from proskenion.config import AppSection, Config, DatabaseSection, LoggingSection, ServerSection
from proskenion.core import auth
from proskenion.core.auth import COOKIE_NAME
from proskenion.core.broadcast import Broadcaster
from proskenion.core.bus import EventBus
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import users as users_crud
from proskenion.db.migrations import migrate

HOSTNAME = "av.school.nz"
ORIGIN = f"https://{HOSTNAME}"
ADMIN_PASSWORD = "admin-password-long-enough"  # test-only
TEST_ROUNDS = 4  # bcrypt cost for test accounts; production is 12

#: uvicorn's own text — matched verbatim, as the filter under test does.
_HANDSHAKE_MESSAGE = "without completing handshake"


async def _seed(path: Path) -> None:
    db = Database()
    await db.open(path)
    try:
        await migrate(db)
        password_hash = auth.hash_secret(ADMIN_PASSWORD, rounds=TEST_ROUNDS)
        await users_crud.set_password_hash(db, "admin", password_hash)
    finally:
        await db.close()


class _CapturingHandler(logging.Handler):
    """Every record ``uvicorn.error`` emits, verbatim — installed at level 0
    so the filter under test (which demotes a record rather than dropping
    it) is exercised for real rather than bypassed by a handler threshold."""

    def __init__(self) -> None:
        super().__init__(level=logging.NOTSET)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _handshake_errors(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    """Records still in the bad shape: ERROR level, uvicorn's exact text."""
    return [
        r for r in records if r.levelno >= logging.ERROR and _HANDSHAKE_MESSAGE in r.getMessage()
    ]


class _RunningApp:
    def __init__(self, url: str, port: int, db_path: Path, handler: _CapturingHandler) -> None:
        self.ws_url = url
        self.port = port
        self.db_path = db_path
        self.handler = handler

    async def sign_in(self, password: str = ADMIN_PASSWORD) -> str:
        """Sign in over real HTTP against the real server and return the
        cookie header a browser would send — the same shape
        ``tests/unit/api/test_ws.py``'s ``sign_in`` builds from
        ``TestClient``, but from an actual response this time."""
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.port}") as client:
            response = await client.post(f"{API_PREFIX}/auth/login", json={"password": password})
            assert response.status_code == 200, response.text
            return f"{COOKIE_NAME}={response.cookies[COOKIE_NAME]}"


@pytest.fixture
async def running_app(tmp_path: Path) -> AsyncIterator[_RunningApp]:
    config = Config(
        database=DatabaseSection(path=tmp_path / "auditorium.db"),
        logging=LoggingSection(path=tmp_path / "logs"),
        server=ServerSection(hostname=HOSTNAME),
        app=AppSection(state_dir=tmp_path / "appliance", data_dir=tmp_path / "data"),
    )
    await _seed(config.database.path)
    bus = EventBus()
    state = StateStore(config, bus)
    broadcaster = Broadcaster(state, bus, ping_interval_s=5.0, fps=10.0)
    app = create_app(config, broadcaster=broadcaster)  # installs the log filter

    handler = _CapturingHandler()
    uvicorn_error = logging.getLogger("uvicorn.error")
    uvicorn_error.addHandler(handler)
    previous_level = uvicorn_error.level
    uvicorn_error.setLevel(logging.NOTSET)

    # log_config=None exactly as proskenion/main.py runs uvicorn in production
    # ("uvicorn's loggers propagate to our JSON handlers") — otherwise
    # Server.serve() calls logging.config.dictConfig() on its own default,
    # which replaces this fixture's handler and the filter under test before
    # the server is even listening, and the test would pass or fail no
    # matter what create_app() did.
    uvicorn_config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="info", log_config=None, lifespan="on"
    )
    server = uvicorn.Server(uvicorn_config)
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:  # noqa: ASYNC110 - condition spans uvicorn's own server task
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield _RunningApp(f"ws://127.0.0.1:{port}/ws", port, config.database.path, handler)
    finally:
        server.should_exit = True
        await task
        uvicorn_error.removeHandler(handler)
        uvicorn_error.setLevel(previous_level)


# -- each refusal path: no uvicorn ERROR, the right HTTP/close outcome -------


async def test_missing_cookie_is_401_with_no_uvicorn_error(running_app: _RunningApp) -> None:
    """§6.12: an unauthenticated upgrade is rejected 401. Before the fix this
    was ALSO followed by uvicorn's ERROR on every one of the client's retries
    — the CM5's "every ~30 s" symptom, since the client backs off to its
    30 s cap against a refusal that never resolves itself."""
    with pytest.raises(InvalidStatus) as excinfo:
        async with websockets.connect(
            f"{running_app.ws_url}?v=1", additional_headers={"Origin": ORIGIN}
        ):
            pass
    assert excinfo.value.response.status_code == 401
    assert _handshake_errors(running_app.handler.records) == []


async def test_bad_origin_is_403_with_no_uvicorn_error(running_app: _RunningApp) -> None:
    """§16.2, §6.12: a tab open on the bare address (10.2.30.251) alongside
    the hostname sends a mismatched Origin — refused 403, not an ERROR."""
    cookie = await running_app.sign_in()
    with pytest.raises(InvalidStatus) as excinfo:
        async with websockets.connect(
            f"{running_app.ws_url}?v=1",
            additional_headers={"Origin": "https://10.2.30.251", "Cookie": cookie},
        ):
            pass
    assert excinfo.value.response.status_code == 403
    assert _handshake_errors(running_app.handler.records) == []


async def test_stale_token_version_after_a_restore_is_401_with_no_uvicorn_error(
    running_app: _RunningApp,
) -> None:
    """A restore changes the stored ``token_version``; a cookie issued before
    it now fails :func:`~proskenion.core.auth.check_live_version` — the exact
    "old-token-version cookie after the restore" scenario named in the brief.
    Still 401, still no uvicorn ERROR."""
    cookie = await running_app.sign_in()

    db = Database()
    await db.open(running_app.db_path)
    try:
        await users_crud.bump_token_version(db, "admin")  # what a restore does (§6.4)
    finally:
        await db.close()

    with pytest.raises(InvalidStatus) as excinfo:
        async with websockets.connect(
            f"{running_app.ws_url}?v=1", additional_headers={"Origin": ORIGIN, "Cookie": cookie}
        ):
            pass
    assert excinfo.value.response.status_code == 401
    assert _handshake_errors(running_app.handler.records) == []


async def test_unknown_version_is_accepted_then_closed_4001_no_uvicorn_error(
    running_app: _RunningApp,
) -> None:
    """§16.8: the one refusal that *does* complete the handshake — accepted,
    then closed with 4001 — must keep behaving exactly as before and must
    equally never produce the ERROR (it did not before the fix either, but a
    regression here would be easy to introduce while touching the same file)."""
    cookie = await running_app.sign_in()
    async with websockets.connect(
        f"{running_app.ws_url}?v=999",
        additional_headers={"Origin": ORIGIN, "Cookie": cookie},
    ) as socket:
        with pytest.raises(websockets.ConnectionClosed) as excinfo:
            await socket.recv()
        assert excinfo.value.rcvd is not None
        assert excinfo.value.rcvd.code == 4001
    assert _handshake_errors(running_app.handler.records) == []
