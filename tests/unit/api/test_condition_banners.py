"""§21.26's device-offline and no-Venue-Default banners, end to end.

The real application — its lifespan, its device manager, its broadcaster —
with a staff WebSocket client watching ``banner`` frames while real state
changes: a device the manager starts and cannot reach, a mixer held amber by
a desk that refuses the connection, and the Venue Default desk scene set and
unset through the admin API. ``tests/unit/core/test_banners.py`` covers each
rule in isolation; this is what an operator's tablet actually receives.
"""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient, WebSocketTestSession

from proskenion.api.app import API_PREFIX, create_app
from proskenion.config import AppSection, Config, DatabaseSection, LoggingSection, ServerSection
from proskenion.core import setup
from proskenion.core.banners import (
    DEVICE_OFFLINE_KEY,
    DEVICES_OFFLINE_KEY,
    VENUE_DEFAULT_KEY,
    VENUE_DEFAULT_TEXT,
)
from proskenion.core.broadcast import Broadcaster
from proskenion.core.bus import EventBus
from proskenion.core.devices import DeviceManager
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import system_state
from tests.unit.api.conftest import ADMIN_PASSWORD, OPERATOR_PASSWORD
from tests.unit.api.test_ws import DEFAULT_TEST_FPS, HOSTNAME, ORIGIN, connect, run, sign_in
from tests.unit.api.test_ws import _seed as seed_accounts

SLOW_PING_S = 30.0

LOOPBACK: dict[str, Any] = {"transport": {"type": "loopback"}, "driver": {}}
MISSING_SERIAL: dict[str, Any] = {
    "transport": {
        "type": "serial",
        "device_path": "/dev/serial/by-id/usb-no-such-matrix",
        "baud": 9600,
        "bits": 8,
        "parity": "none",
        "stop": "1",
        "flow": "none",
    },
    "driver": {},
}


@pytest.fixture
def banner_config(tmp_path: Path) -> Config:
    return Config(
        database=DatabaseSection(path=tmp_path / "auditorium.db"),
        logging=LoggingSection(path=tmp_path / "logs"),
        server=ServerSection(hostname=HOSTNAME),
        app=AppSection(state_dir=tmp_path / "appliance", data_dir=tmp_path / "data"),
    )


def _closed_port() -> int:
    """A loopback port nothing listens on: a connection to it is refused."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


async def _seed(path: Path, rows: list[dict[str, Any]]) -> list[int]:
    db = Database()
    await db.open(path)
    try:
        await system_state.set(db, setup.SYSTEM_DOMAIN, setup.FIRST_RUN_COMPLETED_KEY, "true")
        ids = []
        for row in rows:
            device = await devices_crud.create(db, **row)
            ids.append(device.id)
        return ids
    finally:
        await db.close()


def _build(config: Config, rows: list[dict[str, Any]]) -> tuple[Any, StateStore, list[int]]:
    """The real application. Pings are slow here: these tests hold the socket
    unread while a device is started, which liveness is not what is tested."""
    asyncio.run(seed_accounts(config.database.path))
    bus = EventBus()
    state = StateStore(config, bus)
    broadcaster = Broadcaster(state, bus, ping_interval_s=SLOW_PING_S, fps=DEFAULT_TEST_FPS)
    app = create_app(config, broadcaster=broadcaster)
    ids = asyncio.run(_seed(config.database.path, rows))
    return app, state, ids


def _cookie(client: TestClient, password: str) -> str:
    cookie = sign_in(client, password)
    client.cookies.clear()
    return cookie


def _wait(condition: Callable[[], bool], timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("the condition never became true")
        time.sleep(0.02)


def _resync_banners(ws: WebSocketTestSession) -> dict[str, dict[str, Any]]:
    """Every banner currently up, from a resync of ``system`` — the frames
    before the ``pong`` that follows it."""
    ws.send_json({"type": "resync", "domains": ["system"]})
    ws.send_json({"type": "ping"})
    seen: dict[str, dict[str, Any]] = {}
    for _ in range(100):
        message: dict[str, Any] = ws.receive_json()
        if message["type"] == "pong":
            return seen
        if message["type"] == "banner":
            seen[message["key"]] = message
    raise AssertionError("the resync never finished")


class BannerView:
    """What a client's store holds: every banner frame applied in order, a
    cleared banner (``text`` null) removed — the same model as the web's
    ``setBanner``."""

    def __init__(self, ws: WebSocketTestSession, up: dict[str, dict[str, Any]]) -> None:
        self._ws = ws
        self.up = {key: (frame["level"], frame["text"]) for key, frame in up.items()}

    def until(
        self, condition: Callable[[dict[str, tuple[str, str]]], bool], limit: int = 400
    ) -> dict[str, tuple[str, str]]:
        for _ in range(limit):
            if condition(self.up):
                return self.up
            message: dict[str, Any] = self._ws.receive_json()
            if message["type"] == "ping":
                self._ws.send_json({"type": "pong"})
            elif message["type"] == "banner":
                if message["text"] is None:
                    self.up.pop(message["key"], None)
                else:
                    self.up[message["key"]] = (message["level"], message["text"])
        raise AssertionError(f"the banners never settled; last seen {self.up}")


def _set_enabled(client: TestClient, app: Any, device_id: int, enabled: bool) -> None:
    """Turn a device on or off the way a save does: the row, then the
    manager rebuilding it from the row."""

    async def change() -> None:
        db: Database = app.state.db
        manager: DeviceManager = app.state.devices
        row = await devices_crud.get(db, device_id)
        assert row is not None
        await devices_crud.update(db, device_id, row.updated_at, enabled=enabled)
        await manager.reload(device_id)

    run(client, change)


def test_a_red_device_raises_the_banner_and_an_amber_one_does_not(banner_config: Config) -> None:
    rows = [
        # A CQ whose MIDI port refuses the connection: amber, "another MIDI
        # client is connected" (§7.3) — held, not offline.
        {
            "category": "mixer",
            "driver_key": "cq20b",
            "name": "CQ-20B",
            "config": {
                "transport": {"type": "tcp", "host": "127.0.0.1", "port": _closed_port()},
                "driver": {"metering": False},
            },
            "enabled": True,
        },
        # A matrix on a serial port that is not there: red once it is turned on.
        {
            "category": "video_matrix",
            "driver_key": "lkv422",
            "name": "LKV422",
            "config": MISSING_SERIAL,
            "enabled": False,
        },
    ]
    app, state, (_mixer_id, matrix_id) = _build(banner_config, rows)
    with TestClient(app, base_url=ORIGIN) as client:
        operator = _cookie(client, OPERATOR_PASSWORD)

        def status_of(key: str) -> str | None:
            record = state.devices.record(key)
            return None if record is None else record.status

        # No knxd runs beside a test application, so KNX is genuinely red:
        # the first offline device, and the one the banner names.
        _wait(lambda: status_of("mixer") == "degraded" and status_of("knx") == "error")
        with connect(client, operator) as ws:
            up = _resync_banners(ws)
            assert up[DEVICE_OFFLINE_KEY]["level"] == "amber"
            assert up[DEVICE_OFFLINE_KEY]["text"] == (
                "KNX offline — house lighting controls unavailable"
            )
            assert DEVICES_OFFLINE_KEY not in up  # the amber mixer is not counted

            view = BannerView(ws, up)
            _set_enabled(client, app, matrix_id, True)
            now = view.until(lambda b: DEVICES_OFFLINE_KEY in b and DEVICE_OFFLINE_KEY not in b)
            assert now[DEVICES_OFFLINE_KEY] == ("amber", "2 devices offline — tap for details")
            assert status_of("hdmi") == "error"
            assert status_of("mixer") == "degraded"  # still amber, still not counted

            _set_enabled(client, app, matrix_id, False)
            now = view.until(lambda b: DEVICE_OFFLINE_KEY in b and DEVICES_OFFLINE_KEY not in b)
            assert now[DEVICE_OFFLINE_KEY] == (
                "amber",
                "KNX offline — house lighting controls unavailable",
            )


def test_setting_and_unsetting_the_venue_default_clears_and_raises_the_banner(
    banner_config: Config,
) -> None:
    rows = [
        {
            "category": "mixer",
            "driver_key": "stub",
            "name": "Mixer",
            "config": LOOPBACK,
            "enabled": True,
        }
    ]
    app, state, (mixer_id,) = _build(banner_config, rows)
    with TestClient(app, base_url=ORIGIN) as client:
        admin = _cookie(client, ADMIN_PASSWORD)
        with connect(client, admin) as ws:
            up = _resync_banners(ws)
            assert up[VENUE_DEFAULT_KEY]["level"] == "amber"
            assert up[VENUE_DEFAULT_KEY]["text"] == VENUE_DEFAULT_TEXT

            created = client.post(
                f"{API_PREFIX}/mixer/desk-scenes",
                json={
                    "device_id": mixer_id,
                    "scene_ref": "1",
                    "name": "Venue Default",
                    "is_venue_default": True,
                },
                headers={"Cookie": admin},
            )
            assert created.status_code == 201, created.text
            view = BannerView(ws, up)
            view.until(lambda b: VENUE_DEFAULT_KEY not in b)
            assert VENUE_DEFAULT_KEY not in state.system.banners()

            scene = created.json()
            unset = client.put(
                f"{API_PREFIX}/mixer/desk-scenes/{scene['id']}",
                json={"is_venue_default": False},
                headers={"Cookie": admin, "If-Unmodified-Since-Version": scene["updated_at"]},
            )
            assert unset.status_code == 200, unset.text
            now = view.until(lambda b: VENUE_DEFAULT_KEY in b)
            assert now[VENUE_DEFAULT_KEY] == ("amber", VENUE_DEFAULT_TEXT)


def test_with_no_mixer_there_is_no_venue_default_banner(banner_config: Config) -> None:
    app, state, _ = _build(banner_config, [])
    with TestClient(app, base_url=ORIGIN) as client:
        operator = _cookie(client, OPERATOR_PASSWORD)
        with connect(client, operator) as ws:
            assert VENUE_DEFAULT_KEY not in _resync_banners(ws)
        assert VENUE_DEFAULT_KEY not in state.system.banners()
