"""Sessions on the socket: revocation (4003), the race with the switch, and ``aexp`` (4002).

Spec §6.4 (the absolute cap, every tier; B65: the idle expiry is never
enforced on the socket), §6.6 (the kill switch drops connections at once),
§21.8 ("Access updated") and the phase-5 contract's close codes and the
re-check before every hirer ``set``.

The application runs for real under the test client, as in ``test_ws``; the
token service shares a fake clock with the test so ``aexp`` arrives by
advancing time rather than waiting twelve hours.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient, WebSocketDenialResponse, WebSocketTestSession
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocketDisconnect

from proskenion.api.app import API_PREFIX, create_app
from proskenion.api.ws import CLOSE_EXPIRED, CLOSE_REVOKED, SetRequest, SetResult
from proskenion.config import AppSection, Config, DatabaseSection, LoggingSection, ServerSection
from proskenion.core import auth, setup
from proskenion.core.auth import COOKIE_NAME, TokenClaims, TokenService
from proskenion.core.broadcast import Broadcaster, Connection
from proskenion.core.bus import EventBus
from proskenion.core.hirer_access import HirerAccess
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import system_state
from tests.unit.api.conftest import ADMIN_PASSWORD, HIRER_PIN, OPERATOR_PASSWORD, FakeClock
from tests.unit.api.test_ws import HOSTNAME, ORIGIN, _seed

HIRER_URL = f"{API_PREFIX}/hirer"
WAIT_S = 5.0

#: Long enough that no server ping arrives during a test: these tests answer
#: their own pings and read only what they asked for.
QUIET_PING_S = 30.0
#: How often a socket re-reads the fake clock against its ``aexp``.
EXPIRY_CHECK_S = 0.01


@pytest.fixture
def session_config(tmp_path: Path) -> Config:
    return Config(
        database=DatabaseSection(path=tmp_path / "auditorium.db"),
        logging=LoggingSection(path=tmp_path / "logs"),
        server=ServerSection(hostname=HOSTNAME),
        app=AppSection(state_dir=tmp_path / "appliance", data_dir=tmp_path / "data"),
    )


async def _commissioned(path: Path) -> None:
    """The ``test_ws`` seed, on an appliance whose first run completed long ago."""
    await _seed(path)
    db = Database()
    await db.open(path)
    try:
        await system_state.set(db, setup.SYSTEM_DOMAIN, setup.FIRST_RUN_COMPLETED_KEY, "true")
    finally:
        await db.close()


def build(
    config: Config,
    clock: FakeClock | None = None,
    *,
    hirer_may_write_mixer: bool = False,
) -> tuple[FastAPI, Broadcaster]:
    asyncio.run(_commissioned(config.database.path))
    bus = EventBus()
    state = StateStore(config, bus)
    extra: dict[str, Any] = {}
    if hirer_may_write_mixer:
        # Stands in for the permission resolver: the hirer may reach the mixer
        # domain, so a hirer ``set`` gets as far as its handler.
        extra["visibility"] = lambda _connection, _domain: True
        extra["writable"] = lambda _connection, _domain, _target: True
    broadcaster = Broadcaster(
        state,
        bus,
        fps=60.0,
        ping_interval_s=QUIET_PING_S,
        expiry_check_s=EXPIRY_CHECK_S,
        **extra,
    )
    tokens = (
        TokenService(config.app.state_dir / auth.JWT_SECRET_FILENAME, clock=clock.now)
        if clock is not None
        else None
    )
    return create_app(config, broadcaster=broadcaster, tokens=tokens), broadcaster


def cookie_of(response: Any) -> str:
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(f"{COOKIE_NAME}="):
            jar: SimpleCookie = SimpleCookie()
            jar.load(header)
            return f"{COOKIE_NAME}={jar[COOKIE_NAME].value}"
    raise AssertionError("no session cookie was set")


def sign_in(client: TestClient, *, password: str | None = None, pin: str | None = None) -> str:
    if pin is not None:
        response = client.post(f"{API_PREFIX}/auth/hirer", json={"pin": pin})
    else:
        response = client.post(f"{API_PREFIX}/auth/login", json={"password": password})
    assert response.status_code == 200, response.text
    # Every call below names its cookie; the jar would otherwise send the last.
    client.cookies.clear()
    return cookie_of(response)


def connect(client: TestClient, cookie: str) -> WebSocketTestSession:
    return client.websocket_connect("/ws?v=1", headers={"Origin": ORIGIN, "Cookie": cookie})


def wait_for(condition: Callable[[], bool]) -> None:
    """Wait for ``condition`` against a deadline — never a fixed sleep."""
    deadline = time.monotonic() + WAIT_S
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.001)


def open_connections(broadcaster: Broadcaster, count: int) -> list[Connection]:
    wait_for(lambda: broadcaster.connection_count == count)
    return list(broadcaster.connections())


def still_open(ws: WebSocketTestSession) -> None:
    ws.send_json({"type": "ping"})
    assert ws.receive_json() == {"type": "pong"}


def closed_with(ws: WebSocketTestSession) -> int:
    """Read until the server closes the socket; the close code."""
    with pytest.raises(WebSocketDisconnect) as closed:
        for _ in range(50):
            ws.receive_json()
    return closed.value.code


def post_as(client: TestClient, cookie: str, path: str, body: dict[str, Any]) -> Any:
    return client.post(path, json=body, headers={"Cookie": cookie})


def access_of(app: FastAPI) -> HirerAccess:
    access: HirerAccess = app.state.hirer_access
    return access


# -- revocation: 4003 (§6.6, §21.8) ---------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "body"),
    [("enabled", {"enabled": False}), ("pin", {"pin": "135790"}), ("pin", {"generate": True})],
)
def test_the_switch_closes_hirer_sockets_with_4003_and_leaves_staff_alone(
    session_config: Config, path: str, body: dict[str, Any]
) -> None:
    app, broadcaster = build(session_config)
    with TestClient(app, base_url=ORIGIN) as client:
        hirer = sign_in(client, pin=HIRER_PIN)
        operator = sign_in(client, password=OPERATOR_PASSWORD)
        admin = sign_in(client, password=ADMIN_PASSWORD)
        with connect(client, hirer) as phone, connect(client, operator) as booth:
            open_connections(broadcaster, 2)
            response = post_as(client, admin, f"{HIRER_URL}/{path}", body)
            assert response.status_code == 200
            assert response.json()["sessions_closed"] == 1
            assert closed_with(phone) == CLOSE_REVOKED
            still_open(booth)
        # The old token is refused at the upgrade too.
        with pytest.raises(WebSocketDenialResponse) as denied, connect(client, hirer):
            pass
        assert denied.value.status_code == 401


def test_a_hirer_set_after_revocation_is_nacked_then_closed(session_config: Config) -> None:
    """The re-check before every hirer ``set``, reached without the sweep."""
    app, broadcaster = build(session_config, hirer_may_write_mixer=True)
    applied: list[SetRequest] = []

    async def mixer(request: SetRequest, _claims: TokenClaims) -> SetResult:
        applied.append(request)
        return SetResult.accepted()

    app.state.writes.register("mixer", mixer)
    access = access_of(app)
    with TestClient(app, base_url=ORIGIN) as client:
        hirer = sign_in(client, pin=HIRER_PIN)
        with connect(client, hirer) as phone:
            open_connections(broadcaster, 1)
            phone.send_json({"type": "set", "domain": "mixer", "id": 1, "value": -10.0, "token": 1})
            assert phone.receive_json() == {"type": "ack", "token": 1}

            async def revoke_without_sweeping() -> None:
                # What the connection would see if a revocation's sweep had
                # somehow missed it: state.hirer says no, the socket is open.
                access._publish(False, access.token_version + 1)

            assert client.portal is not None
            client.portal.call(revoke_without_sweeping)
            phone.send_json({"type": "set", "domain": "mixer", "id": 1, "value": 0.0, "token": 2})
            assert phone.receive_json() == {
                "type": "nack",
                "token": 2,
                "reason": "unauthenticated",
                "detail": {"reason": "hirer_revoked"},
            }
            assert closed_with(phone) == CLOSE_REVOKED
    assert [r.token for r in applied] == [1]


class RecordResponseStart:
    """Notes the moment the switch's response starts to leave the server."""

    def __init__(self, app: ASGIApp, order: list[str]) -> None:
        self.app = app
        self.order = order

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def recording(message: Message) -> None:
            if message["type"] == "http.response.start" and scope["path"] == f"{HIRER_URL}/enabled":
                self.order.append("switch answered")
            await send(message)

        await self.app(scope, receive, recording)


def test_a_socket_write_racing_the_switch_lands_before_its_answer(session_config: Config) -> None:
    """A write in flight when the switch lands is applied before the switch
    answers; the switch's response is never followed by a hirer write."""
    app, broadcaster = build(session_config, hirer_may_write_mixer=True)
    order: list[str] = []
    entered = threading.Event()
    release = threading.Event()

    async def mixer(request: SetRequest, _claims: TokenClaims) -> SetResult:
        entered.set()
        # Still being applied when the switch lands. The test thread releases
        # it, and an asyncio.Event cannot be set from another thread.
        while not release.is_set():  # noqa: ASYNC110
            await asyncio.sleep(0.001)
        order.append(f"write {request.token} applied")
        return SetResult.accepted()

    app.state.writes.register("mixer", mixer)
    access = access_of(app)
    with TestClient(RecordResponseStart(app, order), base_url=ORIGIN) as client:
        hirer = sign_in(client, pin=HIRER_PIN)
        admin = sign_in(client, password=ADMIN_PASSWORD)
        with connect(client, hirer) as phone, ThreadPoolExecutor(1) as pool:
            open_connections(broadcaster, 1)
            phone.send_json({"type": "set", "domain": "mixer", "id": 1, "value": -6.0, "token": 1})
            assert entered.wait(WAIT_S)
            assert access.in_flight == 1

            switch = pool.submit(post_as, client, admin, f"{HIRER_URL}/enabled", {"enabled": False})
            wait_for(lambda: not access.enabled)  # the switch has landed
            wait_for(lambda: access.draining or switch.done())
            assert not switch.done()  # it waits for the write in flight
            assert order == []

            release.set()
            response = switch.result(timeout=WAIT_S)
            assert response.status_code == 200
            assert response.json() == {"enabled": False, "sessions_closed": 1}
            assert closed_with(phone) == CLOSE_REVOKED
    assert order == ["write 1 applied", "switch answered"]
    assert access.in_flight == 0


# -- absolute expiry: 4002 (§6.4, B65) --------------------------------------------------------


def test_every_socket_closes_at_aexp_and_never_at_the_idle_expiry(session_config: Config) -> None:
    clock = FakeClock()
    app, broadcaster = build(session_config, clock)
    with TestClient(app, base_url=ORIGIN) as client:
        operator = sign_in(client, password=OPERATOR_PASSWORD)
        admin = sign_in(client, password=ADMIN_PASSWORD)
        hirer = sign_in(client, pin=HIRER_PIN)
        aexp = clock.now() + timedelta(hours=auth.ABSOLUTE_HOURS)
        clock.advance(minutes=5)
        # A visible page re-issues its cookie; the re-issued token keeps aexp.
        reissued = client.get(f"{API_PREFIX}/auth/session", headers={"Cookie": operator})
        assert reissued.status_code == 200
        assert reissued.json()["absolute_expires_at"] == auth.iso(aexp)
        operator_again = cookie_of(reissued)
        client.cookies.clear()

        with (
            connect(client, operator) as first,
            connect(client, operator_again) as second,
            connect(client, admin) as desk,
            connect(client, hirer) as phone,
        ):
            sockets = (first, second, desk, phone)
            open_connections(broadcaster, 4)
            # Past every idle expiry, with no page holding the session: the
            # sockets stay open, because traffic is never activity (B65).
            clock.advance(minutes=31)
            for ws in sockets:
                still_open(ws)
            # A second before the absolute cap, still open.
            clock.current = aexp - timedelta(seconds=1)
            for ws in sockets:
                still_open(ws)
            # At it, every tier's socket closes with 4002.
            clock.current = aexp
            for ws in sockets:
                assert closed_with(ws) == CLOSE_EXPIRED
        wait_for(lambda: broadcaster.connection_count == 0)
