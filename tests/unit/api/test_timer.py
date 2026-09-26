"""The shared show timer over the real application (spec §16, §21.7, §16.8, §15.13).

§21.7: "An operator starting it at the top of an act and a second operator on
a tablet in the wings must see the same number." These tests are that
sentence: two real WebSocket clients on the real ASGI application, one
driving the timer through ``POST /timer/*`` and the other seeing every change
arrive as a §16.8 ``timer`` frame. Then the two things a per-client stopwatch
would lose — a reconnect, and a restart of the application.

The application is built as ``test_ws`` builds it: a file-backed database
opened by the lifespan inside the test client's own event loop.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient, WebSocketTestSession

from proskenion.api.app import API_PREFIX, create_app
from proskenion.config import AppSection, Config, DatabaseSection, LoggingSection, ServerSection
from proskenion.core import setup
from proskenion.core.broadcast import Broadcaster
from proskenion.core.bus import EventBus
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import system_state
from proskenion.db.crud.base import now_iso
from tests.unit.api.conftest import ADMIN_PASSWORD, OPERATOR_PASSWORD
from tests.unit.api.test_ws import (
    DEFAULT_TEST_FPS,
    HOSTNAME,
    ORIGIN,
    connect,
    sign_in,
    sign_in_hirer,
)
from tests.unit.api.test_ws import _seed as seed_accounts

SLOW_PING_S = 30.0

TIMER = f"{API_PREFIX}/timer"


@pytest.fixture
def timer_config(tmp_path: Path) -> Config:
    return Config(
        database=DatabaseSection(path=tmp_path / "auditorium.db"),
        logging=LoggingSection(path=tmp_path / "logs"),
        server=ServerSection(hostname=HOSTNAME),
        app=AppSection(state_dir=tmp_path / "appliance", data_dir=tmp_path / "data"),
    )


async def _complete_first_run(path: Path) -> None:
    """The timer routes sit behind the first-run gate like every control route."""
    db = Database()
    await db.open(path)
    try:
        await system_state.set(db, setup.SYSTEM_DOMAIN, setup.FIRST_RUN_COMPLETED_KEY, "true")
    finally:
        await db.close()


def _build(config: Config) -> Any:
    """The real application. Pings are slow here: a socket may sit unread
    while the other client posts, and liveness is not what is tested."""
    asyncio.run(seed_accounts(config.database.path))
    asyncio.run(_complete_first_run(config.database.path))
    bus = EventBus()
    state = StateStore(config, bus)
    broadcaster = Broadcaster(state, bus, ping_interval_s=SLOW_PING_S, fps=DEFAULT_TEST_FPS)
    return create_app(config, broadcaster=broadcaster), state


def _cookie_for(client: TestClient, password: str) -> str:
    """Each account's cookie, passed explicitly; the jar is emptied so one
    client can act as two people without the jar speaking for either."""
    cookie = sign_in(client, password)
    client.cookies.clear()
    return cookie


def _post(client: TestClient, action: str, cookie: str) -> dict[str, Any]:
    response = client.post(f"{TIMER}/{action}", headers={"Cookie": cookie})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _next_timer(ws: WebSocketTestSession, limit: int = 50) -> dict[str, Any]:
    """The next ``timer`` frame, skipping pings and anything else."""
    for _ in range(limit):
        message: dict[str, Any] = ws.receive_json()
        if message["type"] == "timer":
            return message
    raise AssertionError("no timer frame arrived")


def _subscribe(ws: WebSocketTestSession) -> dict[str, Any]:
    """Subscribe with a resync, as the client does on every (re)connect, and
    return the snapshot's timer frame."""
    ws.send_json({"type": "resync", "domains": ["timer"]})
    return _next_timer(ws)


def test_one_operator_starts_the_timer_and_the_other_sees_it_running(
    timer_config: Config,
) -> None:
    """The hand-off: start from one client, seen by both; stop from the
    other, seen by both, with the same numbers on each."""
    app, _ = _build(timer_config)
    with TestClient(app, base_url=ORIGIN) as client:
        operator = _cookie_for(client, OPERATOR_PASSWORD)
        admin = _cookie_for(client, ADMIN_PASSWORD)
        with connect(client, operator) as front, connect(client, admin) as wings:
            assert _subscribe(front)["running"] is False
            assert _subscribe(wings)["running"] is False

            started = _post(client, "start", operator)
            assert started["running"] is True
            assert started["started_at"] is not None
            assert "+" in started["started_at"]  # ISO 8601 with offset (§4.9)

            seen_in_wings = _next_timer(wings)
            seen_in_front = _next_timer(front)
            expected = {"type": "timer", **started}
            assert seen_in_wings == expected
            assert seen_in_front == expected

            stopped = _post(client, "stop", admin)
            assert stopped["running"] is False
            assert stopped["started_at"] is None
            assert stopped["accumulated_ms"] >= 0

            assert _next_timer(front) == {"type": "timer", **stopped}
            assert _next_timer(wings) == {"type": "timer", **stopped}

            reset = _post(client, "reset", operator)
            assert reset == {"running": False, "started_at": None, "accumulated_ms": 0}
            if stopped["accumulated_ms"] != 0:
                assert _next_timer(wings) == {"type": "timer", **reset}


def test_start_while_running_is_one_start_not_two(timer_config: Config) -> None:
    """Two operators pressing start at once: the second changes nothing."""
    app, _ = _build(timer_config)
    with TestClient(app, base_url=ORIGIN) as client:
        operator = _cookie_for(client, OPERATOR_PASSWORD)
        first = _post(client, "start", operator)
        second = _post(client, "start", operator)
        assert second == first


def test_a_reconnecting_client_recovers_the_running_timer(timer_config: Config) -> None:
    """§21.7: "a page reload or a reconnect does not lose it"."""
    app, _ = _build(timer_config)
    with TestClient(app, base_url=ORIGIN) as client:
        operator = _cookie_for(client, OPERATOR_PASSWORD)
        started = _post(client, "start", operator)
        with connect(client, operator) as ws:
            assert _subscribe(ws) == {"type": "timer", **started}
        # The socket has gone; the timer has not.
        with connect(client, operator) as ws:
            assert _subscribe(ws) == {"type": "timer", **started}


def test_a_restart_mid_performance_resumes_the_timer_rather_than_zeroing_it(
    timer_config: Config,
) -> None:
    """§15.13/§21.7: running and started_at are stored in system_state, so
    the application coming back finds the same run still going."""
    app, _ = _build(timer_config)
    with TestClient(app, base_url=ORIGIN) as client:
        operator = _cookie_for(client, OPERATOR_PASSWORD)
        started = _post(client, "start", operator)

    app, state = _build(timer_config)
    with TestClient(app, base_url=ORIGIN) as client:
        assert state.timer.running is True
        assert state.timer.started_at == started["started_at"]
        operator = _cookie_for(client, OPERATOR_PASSWORD)
        with connect(client, operator) as ws:
            assert _subscribe(ws) == {"type": "timer", **started}
        before_stop = datetime.fromisoformat(now_iso())
        stopped = _post(client, "stop", operator)
        # Elapsed runs from the original start, across the restart: the whole
        # run is folded in, not just the part since the application came back.
        began = datetime.fromisoformat(started["started_at"])
        since_start_ms = int((before_stop - began).total_seconds() * 1000)
        assert stopped["accumulated_ms"] >= since_start_ms


def test_a_stopped_timer_keeps_its_accumulated_time_across_a_restart(
    timer_config: Config,
) -> None:
    app, _ = _build(timer_config)
    with TestClient(app, base_url=ORIGIN) as client:
        operator = _cookie_for(client, OPERATOR_PASSWORD)
        _post(client, "start", operator)
        stopped = _post(client, "stop", operator)

    app, state = _build(timer_config)
    with TestClient(app, base_url=ORIGIN):
        assert state.timer.running is False
        assert state.timer.accumulated_ms == stopped["accumulated_ms"]


def test_a_hirer_may_not_start_stop_or_reset_the_timer(timer_config: Config) -> None:
    """§21.7: "Operator and admin only. Hirers see the clock and not the timer"."""
    app, state = _build(timer_config)
    with TestClient(app, base_url=ORIGIN) as client:
        hirer = sign_in_hirer(client)
        client.cookies.clear()
        for action in ("start", "stop", "reset"):
            response = client.post(f"{TIMER}/{action}", headers={"Cookie": hirer})
            assert response.status_code == 403, response.text
            assert response.json()["error"]["code"] == "permission_denied"
        assert state.timer.running is False
