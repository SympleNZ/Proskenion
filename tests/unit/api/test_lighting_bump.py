"""The ``lighting_bump`` WebSocket domain (owner decision 2026-10-01, "Option A").

A group's BUMP flashes its DMX members to full while held. The hold belongs
to the connection that made it, so every way that connection can go away
lets it go: an explicit release, the socket closing, the page going to the
background, and the client no longer refreshing it. External control
refuses a press and releases what is held. The overlay itself is tested in
``tests/unit/core/dmx/test_bump.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from proskenion.api.app import create_app
from proskenion.config import AppSection, Config, DatabaseSection, LoggingSection, ServerSection
from proskenion.core.lighting import LightingService
from proskenion.db.connection import Database
from proskenion.db.crud import lighting as lighting_crud
from tests.unit.api.conftest import ADMIN_PASSWORD
from tests.unit.api.test_lighting import _seed_ws, receive_until, run, sign_in

ORIGIN = {"Origin": "https://av.school.nz"}


@pytest.fixture
def ws_config(tmp_path: Path) -> Config:
    return Config(
        database=DatabaseSection(path=tmp_path / "auditorium.db"),
        logging=LoggingSection(path=tmp_path / "logs"),
        server=ServerSection(hostname="av.school.nz"),
        app=AppSection(state_dir=tmp_path / "appliance", data_dir=tmp_path / "data"),
    )


def bump(group_id: int, value: float, token: int) -> dict[str, Any]:
    return {
        "type": "set",
        "domain": "lighting_bump",
        "id": group_id,
        "value": value,
        "token": token,
    }


class Room:
    """An app with one DMX fixture in a group (and an indicator-only group)."""

    def __init__(self, client: TestClient, app: Any, channel_id: int) -> None:
        self.client = client
        self.app = app
        self.channel_id = channel_id
        self.group_id = 0
        self.indicator_id = 0

    @property
    def service(self) -> LightingService:
        service: LightingService = self.app.state.lighting
        return service

    def setup(self) -> None:
        async def work() -> None:
            db: Database = self.app.state.db
            group = await lighting_crud.create_group(db, name="Wash")
            await lighting_crud.set_group_members(db, group.id, [self.channel_id])
            indicator = await lighting_crud.create_group(db, name="All", indicator_only=True)
            await lighting_crud.set_group_members(db, indicator.id, [self.channel_id])
            self.group_id, self.indicator_id = group.id, indicator.id
            await self.service.reload_config()
            self.service.set_level(self.channel_id, 30.0)

        run(self.client, work)

    def call[T](self, work: Callable[[], Awaitable[T]]) -> T:
        out: list[T] = []

        async def capture() -> None:
            out.append(await work())

        run(self.client, capture)
        return out[0]

    def bumped(self) -> frozenset[int]:
        async def read() -> frozenset[int]:
            return self.service.bumped_groups

        return self.call(read)

    def output(self) -> float | None:
        async def read() -> float | None:
            return self.service.composited_level(self.channel_id)

        return self.call(read)

    def wait_until_released(self, within: float = 3.0) -> None:
        async def wait() -> bool:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + within
            while self.service.bumped_groups:
                if loop.time() > deadline:
                    return False
                await asyncio.sleep(0.01)
            return True

        assert self.call(wait), "the bump was never released"


async def test_press_flashes_the_group_and_release_restores_without_moving_a_level(
    ws_config: Config,
) -> None:
    channel_id = await _seed_ws(ws_config.database.path)
    app = create_app(ws_config)
    with TestClient(app, base_url="https://av.school.nz") as client:
        room = Room(client, app, channel_id)
        room.setup()
        cookie = sign_in(client)
        with client.websocket_connect("/ws?v=1", headers={**ORIGIN, "Cookie": cookie}) as ws:
            ws.send_json(bump(room.group_id, 1, 1))
            assert receive_until(ws, "ack") == {"type": "ack", "token": 1}
            assert room.bumped() == {room.group_id}
            assert room.output() == 100.0  # full × master 100 %

            ws.send_json(bump(room.group_id, 1, 2))  # the client's refresh
            assert receive_until(ws, "ack") == {"type": "ack", "token": 2}

            ws.send_json(bump(room.group_id, 0, 3))
            assert receive_until(ws, "ack") == {"type": "ack", "token": 3}
            assert room.bumped() == frozenset()
            assert room.output() == 30.0

        async def level() -> float:
            return room.service._view.level(channel_id)  # noqa: SLF001

        assert room.call(level) == 30.0  # the stored level never moved


async def test_closing_the_socket_releases_its_bump(ws_config: Config) -> None:
    channel_id = await _seed_ws(ws_config.database.path)
    app = create_app(ws_config)
    with TestClient(app, base_url="https://av.school.nz") as client:
        room = Room(client, app, channel_id)
        room.setup()
        cookie = sign_in(client)
        with client.websocket_connect("/ws?v=1", headers={**ORIGIN, "Cookie": cookie}) as ws:
            ws.send_json(bump(room.group_id, 1, 1))
            receive_until(ws, "ack")
            assert room.bumped() == {room.group_id}
        room.wait_until_released()
        assert room.output() == 30.0


async def test_going_to_the_background_releases_its_bump(ws_config: Config) -> None:
    channel_id = await _seed_ws(ws_config.database.path)
    app = create_app(ws_config)
    with TestClient(app, base_url="https://av.school.nz") as client:
        room = Room(client, app, channel_id)
        room.setup()
        cookie = sign_in(client)
        with client.websocket_connect("/ws?v=1", headers={**ORIGIN, "Cookie": cookie}) as ws:
            ws.send_json(bump(room.group_id, 1, 1))
            receive_until(ws, "ack")
            ws.send_json({"type": "background"})
            room.wait_until_released()
            ws.send_json({"type": "ping"})  # still connected: only the hold ended
            receive_until(ws, "pong")


async def test_a_bump_the_client_stops_refreshing_is_released(ws_config: Config) -> None:
    """A tablet that lost Wi-Fi with a finger down sends neither a release nor
    a close the server sees quickly; its hold expires instead."""
    channel_id = await _seed_ws(ws_config.database.path)
    app = create_app(ws_config)
    with TestClient(app, base_url="https://av.school.nz") as client:
        room = Room(client, app, channel_id)
        room.setup()
        room.service.bumps._timeout_s = 0.2  # noqa: SLF001 - the production 1.5 s, shortened
        cookie = sign_in(client)
        with client.websocket_connect("/ws?v=1", headers={**ORIGIN, "Cookie": cookie}) as ws:
            ws.send_json(bump(room.group_id, 1, 1))
            receive_until(ws, "ack")
            assert room.bumped() == {room.group_id}
            room.wait_until_released(within=2.0)  # no refresh, socket still open


async def test_a_session_that_ends_releases_its_bump(ws_config: Config) -> None:
    """Logging out, or the session's absolute expiry, closes the socket — the
    client's own close on logout, 4002 from the server at the expiry — and a
    closed socket holds nothing. Signing out on another tab: the socket is
    what holds, so the same release applies."""
    channel_id = await _seed_ws(ws_config.database.path)
    app = create_app(ws_config)
    with TestClient(app, base_url="https://av.school.nz") as client:
        room = Room(client, app, channel_id)
        room.setup()
        cookie = sign_in(client)
        with client.websocket_connect("/ws?v=1", headers={**ORIGIN, "Cookie": cookie}) as ws:
            ws.send_json(bump(room.group_id, 1, 1))
            receive_until(ws, "ack")
            response = client.post("/api/v1/auth/logout", headers={"Cookie": cookie})
            assert response.status_code == 204
            ws.close()
        room.wait_until_released()


async def test_external_control_refuses_a_press_and_releases_what_is_held(
    ws_config: Config,
) -> None:
    channel_id = await _seed_ws(ws_config.database.path)
    app = create_app(ws_config)
    with TestClient(app, base_url="https://av.school.nz") as client:
        room = Room(client, app, channel_id)
        room.setup()
        cookie = sign_in(client)
        with client.websocket_connect("/ws?v=1", headers={**ORIGIN, "Cookie": cookie}) as ws:
            ws.send_json(bump(room.group_id, 1, 1))
            receive_until(ws, "ack")

            async def engage() -> None:
                room.service.set_external_manual(True)

            room.call(engage)
            assert room.bumped() == frozenset()

            ws.send_json(bump(room.group_id, 1, 2))
            assert receive_until(ws, "nack") == {"type": "nack", "token": 2, "reason": "conflict"}
            ws.send_json(bump(room.group_id, 0, 3))  # a release is always acknowledged
            assert receive_until(ws, "ack") == {"type": "ack", "token": 3}


async def test_refused_presses(ws_config: Config) -> None:
    channel_id = await _seed_ws(ws_config.database.path)
    app = create_app(ws_config)
    with TestClient(app, base_url="https://av.school.nz") as client:
        room = Room(client, app, channel_id)
        room.setup()
        cookie = sign_in(client, ADMIN_PASSWORD)  # admin may bump as the operator does
        with client.websocket_connect("/ws?v=1", headers={**ORIGIN, "Cookie": cookie}) as ws:
            ws.send_json(bump(room.indicator_id, 1, 1))
            assert receive_until(ws, "nack")["reason"] == "validation_failed"
            ws.send_json(bump(999, 1, 2))
            assert receive_until(ws, "nack")["reason"] == "not_found"
            ws.send_json(bump(room.group_id, 0.5, 3))
            assert receive_until(ws, "nack")["reason"] == "validation_failed"
            assert room.bumped() == frozenset()
            ws.send_json(bump(room.group_id, 1, 4))
            assert receive_until(ws, "ack") == {"type": "ack", "token": 4}
