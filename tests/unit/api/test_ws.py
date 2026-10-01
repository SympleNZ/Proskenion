"""The WebSocket endpoint: upgrade, versioning, liveness, backgrounding and writes.

Spec §16.8 (the wire contract), §6.12 (authentication and tiers), §10.7
(reconnection) and §21.2 (the token protocol). §22.2 names connection
liveness and backgrounding as tests in their own right.

The application is built the way ``test_deps`` builds it — a file-backed
database opened by the lifespan inside the test client's own event loop — so
nothing crosses loops. State is changed through the client's portal, which
runs in that same loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient, WebSocketDenialResponse, WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from proskenion.api.app import API_PREFIX, create_app
from proskenion.api.errors import ErrorCode
from proskenion.api.ws import (
    CLOSE_LIVENESS,
    CLOSE_REFRESH_REQUIRED,
    REFRESH_REQUIRED_MESSAGE,
    SetRequest,
    SetResult,
)
from proskenion.config import AppSection, Config, DatabaseSection, LoggingSection, ServerSection
from proskenion.core import auth
from proskenion.core.auth import COOKIE_NAME, TokenClaims
from proskenion.core.broadcast import Broadcaster
from proskenion.core.bus import EventBus
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import hirer as hirer_crud
from proskenion.db.crud import users as users_crud
from proskenion.db.migrations import migrate
from tests.unit.api.conftest import (
    ADMIN_PASSWORD,
    HIRER_PIN,
    OPERATOR_PASSWORD,
    TEST_ROUNDS,
)

HOSTNAME = "av.school.nz"
ORIGIN = f"https://{HOSTNAME}"
STARTED_AT = "2026-09-10T19:42:11.400+12:00"

#: Fast enough that the liveness test costs a tenth of a second, and slow
#: enough that a loaded Windows runner still schedules a pong in time.
TEST_PING_INTERVAL_S = 0.1


@pytest.fixture
def ws_config(tmp_path: Path) -> Config:
    return Config(
        database=DatabaseSection(path=tmp_path / "auditorium.db"),
        logging=LoggingSection(path=tmp_path / "logs"),
        server=ServerSection(hostname=HOSTNAME),
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
        await users_crud.set_password_hash(
            db, "operator", auth.hash_secret(OPERATOR_PASSWORD, rounds=TEST_ROUNDS)
        )
        await hirer_crud.set_pin_hash(
            db, auth.hash_secret(HIRER_PIN, rounds=TEST_ROUNDS), updated_by=None
        )
        await hirer_crud.set_enabled(db, True, updated_by=None)
    finally:
        await db.close()


def build(config: Config, **kwargs: Any) -> tuple[FastAPI, Broadcaster, StateStore]:
    """The real application with a broadcaster that pings fast enough to test."""
    asyncio.run(_seed(config.database.path))
    bus = EventBus()
    state = StateStore(config, bus)
    kwargs.setdefault("fps", DEFAULT_TEST_FPS)
    broadcaster = Broadcaster(state, bus, ping_interval_s=TEST_PING_INTERVAL_S, **kwargs)
    return create_app(config, broadcaster=broadcaster), broadcaster, state


#: Ticking faster than §16.8's 10–15 fps keeps the tests from sleeping; the
#: production default sits inside the range.
DEFAULT_TEST_FPS = 60.0


def sign_in(client: TestClient, password: str = OPERATOR_PASSWORD) -> str:
    """Sign in over HTTP and return the cookie header a browser would send.

    The test client upgrades over ``ws://``, so its jar withholds the Secure
    session cookie; it is passed explicitly instead.
    """
    response = client.post(f"{API_PREFIX}/auth/login", json={"password": password})
    assert response.status_code == 200, response.text
    return f"{COOKIE_NAME}={response.cookies[COOKIE_NAME]}"


def sign_in_hirer(client: TestClient, pin: str = HIRER_PIN) -> str:
    """A hirer signs in with a PIN; the token carries identity only (B31)."""
    response = client.post(f"{API_PREFIX}/auth/hirer", json={"pin": pin})
    assert response.status_code == 200, response.text
    return f"{COOKIE_NAME}={response.cookies[COOKIE_NAME]}"


def connect(client: TestClient, cookie: str, url: str = "/ws?v=1") -> WebSocketTestSession:
    return client.websocket_connect(url, headers={"Origin": ORIGIN, "Cookie": cookie})


def run(client: TestClient, work: Callable[[], Coroutine[Any, Any, None]]) -> None:
    """Run ``work`` on the application's own event loop."""
    assert client.portal is not None
    client.portal.call(work)


def receive_until(session: WebSocketTestSession, kind: str, limit: int = 20) -> dict[str, Any]:
    """The next message of type ``kind``, skipping server pings."""
    for _ in range(limit):
        message: dict[str, Any] = session.receive_json()
        if message["type"] == kind:
            return message
    raise AssertionError(f"no {kind!r} message arrived")


# -- upgrade (§6.12, §16.8) ----------------------------------------------------------


def test_an_unknown_or_missing_version_closes_with_4001(ws_config: Config) -> None:
    """The only thing that makes a stale service worker diagnosable (§16.8)."""
    app, _, _ = build(ws_config)
    with TestClient(app, base_url=ORIGIN) as client:
        cookie = sign_in(client)
        for url in ("/ws", "/ws?v=2", "/ws?v=one"):
            with pytest.raises(WebSocketDisconnect) as closed, connect(client, cookie, url) as ws:
                ws.receive_json()
            assert closed.value.code == CLOSE_REFRESH_REQUIRED
            assert closed.value.reason == REFRESH_REQUIRED_MESSAGE


def test_an_unauthenticated_upgrade_and_a_bad_origin_are_refused(ws_config: Config) -> None:
    app, broadcaster, _ = build(ws_config)
    with TestClient(app, base_url=ORIGIN) as client:
        with pytest.raises(WebSocketDenialResponse) as denied:
            with client.websocket_connect("/ws?v=1", headers={"Origin": ORIGIN}):
                pass
        assert denied.value.status_code == 401
        assert denied.value.json()["error"]["code"] == ErrorCode.UNAUTHENTICATED.value

        cookie = sign_in(client)
        with pytest.raises(WebSocketDenialResponse) as denied:
            with client.websocket_connect(
                "/ws?v=1", headers={"Origin": "https://evil.example", "Cookie": cookie}
            ):
                pass
        assert denied.value.status_code == 403
        assert denied.value.json()["error"]["code"] == ErrorCode.PERMISSION_DENIED.value
        assert broadcaster.connection_count == 0


# -- liveness (§16.8, §22.2) ---------------------------------------------------------


def test_a_socket_that_never_pongs_is_closed_after_two_intervals(ws_config: Config) -> None:
    app, broadcaster, _ = build(ws_config)
    with TestClient(app, base_url=ORIGIN) as client:
        cookie = sign_in(client)
        with pytest.raises(WebSocketDisconnect) as closed, connect(client, cookie) as ws:
            ws.send_json({"type": "subscribe", "domains": ["devices"]})
            assert ws.receive_json() == {"type": "ping"}  # one interval
            ws.receive_json()  # two intervals with no pong: closed
        assert closed.value.code == CLOSE_LIVENESS
        # Closed *and* unsubscribed: nothing is left fanning out to it.
        assert broadcaster.connection_count == 0


def test_a_pong_keeps_the_socket_open(ws_config: Config) -> None:
    app, _, _ = build(ws_config)
    with TestClient(app, base_url=ORIGIN) as client:
        cookie = sign_in(client)
        with connect(client, cookie) as ws:
            for _ in range(3):
                assert ws.receive_json() == {"type": "ping"}
                ws.send_json({"type": "pong"})
            ws.send_json({"type": "ping"})
            assert receive_until(ws, "pong") == {"type": "pong"}


def test_a_closed_browser_is_detected_immediately(ws_config: Config) -> None:
    app, broadcaster, _ = build(ws_config)
    with TestClient(app, base_url=ORIGIN) as client:
        cookie = sign_in(client)
        with connect(client, cookie) as ws:
            ws.send_json({"type": "subscribe", "domains": ["devices"]})
            ws.send_json({"type": "ping"})
            assert receive_until(ws, "pong")
            assert broadcaster.connection_count == 1
        # The context manager sends the transport's FIN; no timeout is waited on.
        assert broadcaster.connection_count == 0


# -- subscribe, batching and discrete delivery ---------------------------------------


def test_a_device_status_change_arrives_without_waiting_for_the_tick(ws_config: Config) -> None:
    app, _, state = build(ws_config)
    with TestClient(app, base_url=ORIGIN) as client:
        cookie = sign_in(client)
        with connect(client, cookie) as ws:
            ws.send_json({"type": "subscribe", "domains": ["devices"]})
            ws.send_json({"type": "ping"})
            receive_until(ws, "pong")

            async def change() -> None:
                state.devices.writer("probe").set_status("mixer", "connected")

            run(client, change)
            assert receive_until(ws, "device_status") == {
                "type": "device_status",
                "device": "mixer",
                "status": "connected",
            }


def test_many_changes_inside_one_tick_arrive_as_one_frame(ws_config: Config) -> None:
    app, _, state = build(ws_config)
    with TestClient(app, base_url=ORIGIN) as client:
        cookie = sign_in(client)
        with connect(client, cookie) as ws:
            ws.send_json({"type": "subscribe", "domains": ["lighting"]})
            ws.send_json({"type": "ping"})
            receive_until(ws, "pong")

            async def drag() -> None:
                levels = state.lighting.writer("fade_engine")
                for step in range(1, 21):
                    levels.set_item("levels", 7, float(step))

            run(client, drag)
            frame = receive_until(ws, "lighting_state")
            assert frame["channels"] == {"7": {"level": 20.0}}


# -- backgrounding and resync (§16.8, §10.7, §22.2) ----------------------------------


def test_backgrounding_stops_continuous_frames_and_resync_restores_them(
    ws_config: Config,
) -> None:
    app, _, state = build(ws_config)
    with TestClient(app, base_url=ORIGIN) as client:
        cookie = sign_in(client)
        with connect(client, cookie) as ws:
            ws.send_json({"type": "subscribe", "domains": ["lighting", "mixer", "devices"]})
            ws.send_json({"type": "background"})
            ws.send_json({"type": "ping"})
            receive_until(ws, "pong")

            async def change() -> None:
                state.lighting.writer("fade_engine").set_item("levels", 1, 55.0)
                state.mixer.writer("mixer_client").set_item("meters", 1, [-12.4])
                state.devices.writer("probe").set_status("knx", "degraded")

            run(client, change)
            # The discrete event arrives; the continuous frames do not.
            assert receive_until(ws, "device_status")["status"] == "degraded"

            # Returning to the foreground: a full snapshot, no new sign-in.
            ws.send_json({"type": "resync", "domains": ["lighting", "mixer", "devices"]})
            seen: list[dict[str, Any]] = []
            while True:
                message = ws.receive_json()
                if message["type"] == "ping":
                    continue
                seen.append(message)
                if message["type"] == "mixer_meters":
                    # Domains resync alphabetically ("devices", "lighting",
                    # "mixer" last), and mixer_meters is built right after
                    # mixer_state for that domain, so this is the last frame
                    # of the batch.
                    break
            by_type = {message["type"]: message for message in seen}
            assert by_type["lighting_state"]["channels"] == {"1": {"level": 55.0}}
            assert by_type["lighting_state"]["source"] == "resync"
            # mixer_state itself never carries meters...
            assert "meters" not in by_type["mixer_state"]
            # ...but the reading missed while backgrounded still reaches the
            # client, via the fresh one-off catch-up frame (§16.8, B58): not
            # a replay of the dropped tick, read live at resync time instead.
            assert by_type["mixer_meters"]["channels"] == {"1": [-12.4]}


# -- writes (§16.8, §21.2) -----------------------------------------------------------


def test_a_set_is_nacked_with_the_echoed_token_and_a_closed_vocabulary_reason(
    ws_config: Config,
) -> None:
    """Phase 1 has no settable domain, so a well-formed write is not_found."""
    app, _, _ = build(ws_config)
    with TestClient(app, base_url=ORIGIN) as client:
        cookie = sign_in(client)
        with connect(client, cookie) as ws:
            ws.send_json(
                {"type": "set", "domain": "lighting", "id": 7, "value": 82.5, "token": 4413}
            )
            nack = receive_until(ws, "nack")
            assert nack == {"type": "nack", "token": 4413, "reason": ErrorCode.NOT_FOUND.value}
            assert ErrorCode(nack["reason"]) in set(ErrorCode)


def test_a_malformed_set_is_nacked_rather_than_ignored(ws_config: Config) -> None:
    app, _, _ = build(ws_config)
    with TestClient(app, base_url=ORIGIN) as client:
        cookie = sign_in(client)
        with connect(client, cookie) as ws:
            ws.send_json({"type": "set", "domain": "invented", "id": 1, "value": 1.0, "token": 5})
            assert receive_until(ws, "nack") == {
                "type": "nack",
                "token": 5,
                "reason": ErrorCode.VALIDATION_FAILED.value,
            }
            ws.send_json({"type": "set", "domain": "lighting", "id": 1, "value": "loud"})
            assert receive_until(ws, "nack")["token"] is None


def test_writes_are_applied_in_arrival_order_and_acked_with_their_own_token(
    ws_config: Config,
) -> None:
    app, _, _ = build(ws_config)
    applied: list[int] = []

    async def handler(request: SetRequest, claims: TokenClaims) -> SetResult:
        # An await between arrival and application: order must still hold.
        await asyncio.sleep(0.01 if not applied else 0)
        applied.append(request.token)
        if request.value > 100.0:
            return SetResult.rejected(ErrorCode.VALUE_OUT_OF_RANGE, 100.0)
        return SetResult.accepted()

    app.state.writes.register("lighting", handler)
    with TestClient(app, base_url=ORIGIN) as client:
        cookie = sign_in(client)
        with connect(client, cookie) as ws:
            ws.send_json({"type": "set", "domain": "lighting", "id": 1, "value": 10.0, "token": 1})
            ws.send_json({"type": "set", "domain": "lighting", "id": 1, "value": 120.0, "token": 2})
            assert receive_until(ws, "ack") == {"type": "ack", "token": 1}
            assert receive_until(ws, "nack") == {
                "type": "nack",
                "token": 2,
                "reason": ErrorCode.VALUE_OUT_OF_RANGE.value,
                "value": 100.0,
            }
    assert applied == [1, 2]


def test_a_write_s_own_frame_arrives_before_its_ack(ws_config: Config) -> None:
    """§21.2: the client deletes its pending entry on the ack and falls
    through to the authoritative value, which must already be the new one."""
    # One tick a minute: any frame that arrives came from the write, not the loop.
    app, _, state = build(ws_config, fps=1 / 60)

    async def handler(request: SetRequest, claims: TokenClaims) -> SetResult:
        assert request.id is not None and request.value is not None
        state.lighting.writer("fade_engine").set_item("levels", request.id, request.value)
        return SetResult.accepted()

    app.state.writes.register("lighting", handler)
    with TestClient(app, base_url=ORIGIN) as client:
        cookie = sign_in(client)
        with connect(client, cookie) as ws:
            ws.send_json({"type": "subscribe", "domains": ["lighting"]})
            ws.send_json({"type": "ping"})
            receive_until(ws, "pong")
            ws.send_json({"type": "set", "domain": "lighting", "id": 7, "value": 42.0, "token": 9})
            seen: list[dict[str, Any]] = []
            while not seen or seen[-1]["type"] != "ack":
                message: dict[str, Any] = ws.receive_json()
                if message["type"] in ("lighting_state", "ack"):
                    seen.append(message)
            assert seen[-1] == {"type": "ack", "token": 9}
            frames = [m for m in seen if m["type"] == "lighting_state"]
            assert frames and frames[-1]["channels"]["7"]["level"] == 42.0


def test_an_unknown_message_type_is_ignored_and_never_closes_the_socket(
    ws_config: Config,
) -> None:
    app, _, _ = build(ws_config)
    with TestClient(app, base_url=ORIGIN) as client:
        cookie = sign_in(client)
        with connect(client, cookie) as ws:
            ws.send_json({"type": "teleport", "where": "away"})
            ws.send_text("not json at all")
            ws.send_json([1, 2, 3])
            ws.send_json({"type": "ping"})
            assert receive_until(ws, "pong") == {"type": "pong"}


# -- tiers (§6.12) -------------------------------------------------------------------


def test_a_hirer_never_receives_a_domain_its_tier_may_not_see(ws_config: Config) -> None:
    app, broadcaster, state = build(ws_config)
    with TestClient(app, base_url=ORIGIN) as client:
        cookie = sign_in_hirer(client)
        with connect(client, cookie) as ws:
            ws.send_json({"type": "resync", "domains": ["lighting", "mixer", "timer", "devices"]})
            ws.send_json({"type": "ping"})

            async def change() -> None:
                state.lighting.writer("fade_engine").set_item("levels", 1, 55.0)
                state.timer.writer("show_timer").start(STARTED_AT)
                state.devices.writer("probe").set_status("projector", "connected")

            run(client, change)
            seen: list[str] = []
            for _ in range(10):
                message = ws.receive_json()
                seen.append(message["type"])
                if message["type"] == "device_status":
                    break
            assert "device_status" in seen
            assert "lighting_state" not in seen
            assert "timer" not in seen
        assert broadcaster.connection_count == 0


def test_a_hirer_write_to_an_unseen_domain_is_permission_denied(ws_config: Config) -> None:
    app, _, _ = build(ws_config)
    with TestClient(app, base_url=ORIGIN) as client:
        cookie = sign_in_hirer(client)
        with connect(client, cookie) as ws:
            ws.send_json({"type": "set", "domain": "mixer", "id": 3, "value": -5.0, "token": 4414})
            assert receive_until(ws, "nack") == {
                "type": "nack",
                "token": 4414,
                "reason": ErrorCode.PERMISSION_DENIED.value,
            }
