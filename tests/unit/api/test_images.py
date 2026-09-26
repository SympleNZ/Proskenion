"""``/system/images*`` (contracts §5 "GET/POST/DELETE /system/images*", §13.6,
Q13) — the routing and the §16.1 envelope a refusal becomes. The
underlying behaviour (retention, verification, the write-slot/stage-slot/
reboot sequence) is proved in ``tests/unit/core/test_images.py``; this file
only proves that the endpoint reaches it and answers the right shape and
status.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from proskenion.api.app import API_PREFIX, create_app
from proskenion.config import Config
from proskenion.core.auth import TokenService
from proskenion.core.broadcast import Broadcaster
from proskenion.core.bus import EventBus
from proskenion.core.images import ImagePaths, ImagesService
from proskenion.core.ratelimit import RateLimiter
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from proskenion.db.migrations import migrate
from tests.package_factory import Signing, make_signing
from tests.unit.api.conftest import ADMIN_PASSWORD, make_client
from tests.unit.core.test_images import FakePlatform, RecordingHelper, _signed_image

AUCKLAND = ZoneInfo("Pacific/Auckland")
NOW = datetime(2026, 9, 20, 14, 30, 0, tzinfo=AUCKLAND)
IMAGES = f"{API_PREFIX}/system/images"


@pytest.fixture
def signing(tmp_path: Path) -> Signing:
    return make_signing(tmp_path, name="image-signing")


@pytest.fixture
async def file_db(config: Config) -> AsyncIterator[Database]:
    from proskenion.core import auth, setup
    from proskenion.db.crud import system_state
    from proskenion.db.crud import users as users_crud

    database = Database()
    await database.open(config.database.path)
    try:
        await migrate(database)
        await users_crud.set_password_hash(
            database, "admin", auth.hash_secret(ADMIN_PASSWORD, rounds=4)
        )
        await system_state.set(
            database, setup.SYSTEM_DOMAIN, setup.FIRST_RUN_COMPLETED_KEY, "true"
        )
        yield database
    finally:
        await database.close()


@pytest.fixture
def boot(tmp_path: Path) -> Path:
    directory = tmp_path / "boot"
    (directory / "slot-a").mkdir(parents=True)
    (directory / "slot-b").mkdir(parents=True)
    (directory / "slot-a" / "os-version.txt").write_text("v1.3.0\n", encoding="utf-8")
    (directory / "slot-a" / "cmdline.txt").write_text("ro\n", encoding="utf-8")
    return directory


@pytest.fixture
def images_app(
    config: Config,
    file_db: Database,
    tokens: TokenService,
    limiter: RateLimiter,
    signing: Signing,
    boot: Path,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> FastAPI:
    running: str | None = getattr(request, "param", "a")
    bus = EventBus()
    state = StateStore(config, bus)
    broadcaster = Broadcaster(state, bus)
    helper = RecordingHelper()
    local_dir = tmp_path / "srv-local"
    local_dir.mkdir()
    usb_dir = tmp_path / "mnt-backup"
    usb_dir.mkdir()

    from proskenion.core.backup_destinations import FilesystemDestination, UsbDestination

    class _FakeUsb(UsbDestination):
        async def available(self) -> bool:
            return True

    async def destinations() -> dict[str, Any]:
        return {"local": FilesystemDestination("local", local_dir), "usb": _FakeUsb(usb_dir)}

    images = ImagesService(
        file_db,
        broadcaster,
        ImagePaths(tmp_dir=tmp_path / "tmp", local_dir=local_dir, usb_dir=usb_dir),
        platform=FakePlatform(boot, running),  # type: ignore[arg-type]
        helper=helper,  # type: ignore[arg-type]
        anchors_dir=signing.anchors,
        now=lambda: NOW,
        destinations_provider=destinations,
    )
    application = create_app(
        config,
        db=file_db,
        tokens=tokens,
        limiter=limiter,
        broadcaster=broadcaster,
        images_service=images,
    )
    application.state.images_test_helper = helper
    application.state.images_test_local_dir = local_dir
    return application


@pytest.fixture
async def http(images_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with make_client(images_app) as client:
        yield client


async def sign_in(http: AsyncClient) -> None:
    response = await http.post(f"{API_PREFIX}/auth/login", json={"password": ADMIN_PASSWORD})
    assert response.status_code == 200, response.text


# -- capture ------------------------------------------------------------------------


class TestCapture:
    async def test_success_answers_the_row(
        self, http: AsyncClient, images_app: FastAPI, tmp_path: Path, signing: Signing
    ) -> None:
        await sign_in(http)
        image = _signed_image(tmp_path, signing, "v1.3.0")
        images_app.state.images_test_helper.image_to_deliver = image

        response = await http.post(f"{IMAGES}/capture")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["slot"] == "a"
        assert body["os_version"] == "v1.3.0"
        assert body["local_present"] is True

        listing = await http.get(IMAGES)
        assert listing.status_code == 200
        assert [i["id"] for i in listing.json()["images"]] == [body["id"]]

    @pytest.mark.parametrize("images_app", [None], indirect=True)
    async def test_no_active_slot_is_a_503(self, http: AsyncClient) -> None:
        await sign_in(http)
        response = await http.post(f"{IMAGES}/capture")
        assert response.status_code == 503, response.text
        assert response.json()["error"]["code"] == "device_unavailable"

    async def test_unauthenticated_is_refused(self, http: AsyncClient) -> None:
        response = await http.post(f"{IMAGES}/capture")
        assert response.status_code == 401


# -- restore ------------------------------------------------------------------------


class TestRestore:
    async def test_success_writes_the_standby_slot_and_reboots(
        self, http: AsyncClient, images_app: FastAPI, tmp_path: Path, signing: Signing
    ) -> None:
        await sign_in(http)
        image = _signed_image(tmp_path, signing, "v1.3.0")
        images_app.state.images_test_helper.image_to_deliver = image
        captured = await http.post(f"{IMAGES}/capture")
        image_id = captured.json()["id"]
        images_app.state.images_test_helper.calls.clear()

        response = await http.post(f"{IMAGES}/{image_id}/restore")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body == {
            "image_id": image_id,
            "slot": "b",
            "restarted": True,
            "os_version": "v1.3.0",
        }
        assert images_app.state.images_test_helper.verbs == [
            "write-slot",
            "stage-slot",
            "reboot",
        ]

    async def test_an_unknown_image_is_404(self, http: AsyncClient) -> None:
        await sign_in(http)
        response = await http.post(f"{IMAGES}/no-such-image/restore")
        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "not_found"


# -- delete ---------------------------------------------------------------------------


class TestDelete:
    async def test_success_removes_it(
        self, http: AsyncClient, images_app: FastAPI, tmp_path: Path, signing: Signing
    ) -> None:
        await sign_in(http)
        image = _signed_image(tmp_path, signing, "v1.3.0")
        images_app.state.images_test_helper.image_to_deliver = image
        captured = await http.post(f"{IMAGES}/capture")
        image_id = captured.json()["id"]

        response = await http.delete(f"{IMAGES}/{image_id}")
        assert response.status_code == 204, response.text

        listing = await http.get(IMAGES)
        assert listing.json()["images"] == []

    async def test_an_unknown_image_is_404(self, http: AsyncClient) -> None:
        await sign_in(http)
        response = await http.delete(f"{IMAGES}/no-such-image")
        assert response.status_code == 404, response.text
