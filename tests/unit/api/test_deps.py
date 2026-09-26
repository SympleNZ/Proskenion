"""Client address, origin policy and the WebSocket upgrade check (§4.13, §6.12, §16.2)."""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, WebSocket
from starlette.testclient import TestClient, WebSocketDenialResponse

from proskenion.api.app import API_PREFIX, create_app
from proskenion.api.deps import (
    WebSocketDenied,
    authenticate_websocket,
    client_address,
    origin_host,
    origin_is_expected,
    require_tier,
)
from proskenion.config import AppSection, Config, DatabaseSection, LoggingSection, ServerSection
from proskenion.core import auth
from proskenion.core.auth import COOKIE_NAME
from proskenion.db.connection import Database
from proskenion.db.crud import security_events
from proskenion.db.crud import users as users_crud
from proskenion.db.migrations import migrate
from tests.unit.api.conftest import ADMIN_PASSWORD, TEST_ROUNDS


def scope(peer: str | None = "127.0.0.1", headers: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "type": "http",
        "client": None if peer is None else (peer, 4321),
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }


# -- client address -------------------------------------------------------------


def test_non_loopback_peer_is_used_as_is() -> None:
    assert client_address(scope("10.0.0.5", {"X-Forwarded-For": "9.9.9.9"})) == "10.0.0.5"


def test_loopback_peer_trusts_x_real_ip_first() -> None:
    headers = {"X-Real-IP": "10.0.0.5", "X-Forwarded-For": "9.9.9.9, 10.0.0.5"}
    assert client_address(scope("127.0.0.1", headers)) == "10.0.0.5"


def test_loopback_peer_uses_rightmost_untrusted_forwarded_entry() -> None:
    headers = {"X-Forwarded-For": "9.9.9.9, 10.0.0.5, 127.0.0.1"}
    assert client_address(scope("127.0.0.1", headers)) == "10.0.0.5"
    assert client_address(scope("::1", {"X-Forwarded-For": " 10.0.0.6 "})) == "10.0.0.6"


def test_loopback_peer_without_headers_is_loopback() -> None:
    assert client_address(scope("127.0.0.1")) == "127.0.0.1"
    assert client_address(scope("127.0.0.1", {"X-Forwarded-For": "127.0.0.1"})) == "127.0.0.1"


def test_no_peer_is_unknown() -> None:
    assert client_address(scope(None)) == "unknown"


# -- origin ---------------------------------------------------------------------


def test_origin_host_parsing() -> None:
    assert origin_host("https://AV.School.nz") == "av.school.nz"
    assert origin_host("https://av.school.nz:8443") == "av.school.nz"
    assert origin_host("null") is None
    assert origin_host("") is None


def test_origin_is_expected() -> None:
    assert origin_is_expected(None, "av.school.nz")
    assert origin_is_expected("https://evil.example", None)
    assert origin_is_expected("https://av.school.nz", "AV.school.nz ")
    assert not origin_is_expected("https://evil.example", "av.school.nz")
    assert not origin_is_expected("null", "av.school.nz")


def test_require_tier_needs_a_tier() -> None:
    with pytest.raises(ValueError):
        require_tier()


# -- WebSocket upgrade (§6.12) --------------------------------------------------


@pytest.fixture
def ws_config(tmp_path: Path) -> Config:
    return Config(
        database=DatabaseSection(path=tmp_path / "auditorium.db"),
        logging=LoggingSection(path=tmp_path / "logs"),
        server=ServerSection(hostname="av.school.nz"),
        app=AppSection(state_dir=tmp_path / "appliance", data_dir=tmp_path / "data"),
    )


async def _seed(path: Path) -> None:
    db = Database()
    await db.open(path)
    try:
        await migrate(db)
        await users_crud.set_password_hash(
            db, "admin", auth.hash_secret(ADMIN_PASSWORD, rounds=TEST_ROUNDS)
        )
    finally:
        await db.close()


async def _events(path: Path, event_type: str) -> list[security_events.SecurityEvent]:
    db = Database()
    await db.open(path)
    try:
        return await security_events.query(db, event_type=event_type)
    finally:
        await db.close()


def _ws_app(config: Config) -> FastAPI:
    """The real app (its lifespan opens the database) plus a test WebSocket endpoint."""
    app = create_app(config)

    @app.websocket(f"{API_PREFIX}/probe/ws")
    async def probe(websocket: WebSocket) -> None:
        try:
            claims = await authenticate_websocket(websocket)
        except WebSocketDenied:
            return
        await websocket.accept()
        await websocket.send_json({"tier": claims.tier})
        await websocket.close()

    return app


def test_websocket_upgrade_checks_origin_then_cookie(ws_config: Config) -> None:
    asyncio.run(_seed(ws_config.database.path))
    app = _ws_app(ws_config)
    with TestClient(app, base_url="https://av.school.nz") as client:
        # Unauthenticated: 401.
        with pytest.raises(WebSocketDenialResponse) as denied:
            with client.websocket_connect(
                f"{API_PREFIX}/probe/ws", headers={"Origin": "https://av.school.nz"}
            ):
                pass
        assert denied.value.status_code == 401
        assert denied.value.json()["error"]["code"] == "unauthenticated"

        login = client.post(f"{API_PREFIX}/auth/login", json={"password": ADMIN_PASSWORD})
        assert login.status_code == 200
        # The test client upgrades over ws://, so its jar withholds the Secure
        # cookie; send it the way a browser on https:// would.
        cookie = f"{COOKIE_NAME}={login.cookies[COOKIE_NAME]}"

        # Wrong origin, even with a valid cookie: 403.
        with pytest.raises(WebSocketDenialResponse) as denied:
            with client.websocket_connect(
                f"{API_PREFIX}/probe/ws",
                headers={"Origin": "https://evil.example", "Cookie": cookie},
            ):
                pass
        assert denied.value.status_code == 403
        assert denied.value.json()["error"]["code"] == "permission_denied"

        # Right origin and cookie: accepted and tagged with the tier.
        with client.websocket_connect(
            f"{API_PREFIX}/probe/ws",
            headers={"Origin": "https://av.school.nz", "Cookie": cookie},
        ) as websocket:
            assert websocket.receive_json() == {"tier": "admin"}

    logged = asyncio.run(_events(ws_config.database.path, "unexpected_origin"))
    assert len(logged) == 1
    assert logged[0].detail is not None and "evil.example" in logged[0].detail
