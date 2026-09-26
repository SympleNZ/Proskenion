"""``/system/update*`` — upload, review, apply, roll back (contracts §5, §21.24).

The endpoint that matters most here is the upload. It is the only route in the
application that accepts gigabytes, on a machine with 4 GB of RAM, and the
thing being asserted is where those bytes go: straight to ``/data/tmp``, one
chunk at a time, never through ``UploadFile``'s buffer and never into
``/tmp``, which is ``PrivateTmp`` on a read-only root (Q9).

The rest is the §16.1 envelope: every refusal names the rule that refused it,
so §21.24's rejection panel says what happened rather than guessing.
"""

from __future__ import annotations

import sqlite3
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
from proskenion.core.alerts import RecordingAlertSink
from proskenion.core.auth import TokenService
from proskenion.core.broadcast import Broadcaster
from proskenion.core.bus import EventBus
from proskenion.core.ratelimit import RateLimiter
from proskenion.core.state import StateStore
from proskenion.core.update import UpdatePaths, installed_version, swap_current, venv_name
from proskenion.core.update_service import UPDATE_READY_BANNER, UpdateService
from proskenion.db.connection import Database
from proskenion.db.migrations import migrate
from tests.package_factory import Signing, build_package, make_signing, make_source
from tests.unit.api.conftest import ADMIN_PASSWORD, OPERATOR_PASSWORD, make_client
from tests.unit.core.test_update import MIGRATION_OK, RecordingHelper

AUCKLAND = ZoneInfo("Pacific/Auckland")
UPDATE = f"{API_PREFIX}/system/update"


class Appliance:
    """/data, /srv/appliance and a signing key, for one test."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.signing: Signing = make_signing(root)
        self.data = root / "data"
        (self.data / "app" / "v1.2.0").mkdir(parents=True)
        (self.data / "app" / "v1.2.0" / "VERSION").write_text("v1.2.0\n", encoding="utf-8")
        (self.data / "tmp").mkdir(parents=True)
        self.state_dir = root / "srv-appliance"
        self.state_dir.mkdir(parents=True)
        swap_current(self.data / "app", "v1.2.0")
        self.paths = UpdatePaths.for_appliance(
            self.data, self.state_dir, self.data / "auditorium.db"
        )

    def package(self, version: str = "v1.3.0", **kwargs: Any) -> bytes:
        source = make_source(
            self.root,
            version,
            extra={
                "proskenion/db/__init__.py": "",
                "proskenion/db/migrations.py": MIGRATION_OK,
            },
        )
        return build_package(
            self.root, version, self.signing, source=source, **kwargs
        ).read_bytes()


@pytest.fixture
def appliance(tmp_path: Path) -> Appliance:
    return Appliance(tmp_path / "appliance")


@pytest.fixture
async def file_db(appliance: Appliance) -> AsyncIterator[Database]:
    """An on-disk database: a snapshot has to be able to copy it."""
    from proskenion.core import auth, setup
    from proskenion.db.crud import system_state
    from proskenion.db.crud import users as users_crud

    database = Database()
    await database.open(appliance.paths.database)
    try:
        await migrate(database)
        await users_crud.set_password_hash(
            database, "admin", auth.hash_secret(ADMIN_PASSWORD, rounds=4)
        )
        await users_crud.set_password_hash(
            database, "operator", auth.hash_secret(OPERATOR_PASSWORD, rounds=4)
        )
        await system_state.set(
            database, setup.SYSTEM_DOMAIN, setup.FIRST_RUN_COMPLETED_KEY, "true"
        )
        yield database
    finally:
        await database.close()


@pytest.fixture
def update_app(
    config: Config,
    file_db: Database,
    tokens: TokenService,
    limiter: RateLimiter,
    appliance: Appliance,
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    """The application with a real update service over the fake appliance.

    The environment build is stood in for — it is proved for real in
    ``tests/unit/core/test_update.py`` and in the systemd harness — so that
    these tests are about the endpoints and their envelopes.
    """
    import sys

    from proskenion.core import update as update_module
    from proskenion.core.update import UpdateRunner

    def build(self: UpdateRunner, destination: Path) -> Path:
        target = destination / venv_name()
        target.mkdir(parents=True, exist_ok=True)
        return target

    monkeypatch.setattr(UpdateRunner, "_build_venv", build)
    monkeypatch.setattr(update_module, "_venv_python", lambda venv: Path(sys.executable))

    bus = EventBus()
    state = StateStore(config, bus)
    broadcaster = Broadcaster(state, bus)
    service = UpdateService(
        state,
        file_db,
        broadcaster,
        appliance.paths,
        alert_sink=RecordingAlertSink(),
        helper=RecordingHelper(),
        anchors_dir=appliance.signing.anchors,
        now=lambda: datetime(2026, 9, 20, 23, 15, tzinfo=AUCKLAND),
    )
    application = create_app(
        config,
        db=file_db,
        tokens=tokens,
        limiter=limiter,
        broadcaster=broadcaster,
        update_service=service,
    )
    application.state.update_test_service = service
    return application


@pytest.fixture
async def http(update_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with make_client(update_app) as client:
        yield client


async def sign_in(http: AsyncClient, password: str = ADMIN_PASSWORD) -> None:
    response = await http.post(f"{API_PREFIX}/auth/login", json={"password": password})
    assert response.status_code == 200


# -- the upload (Q9) -------------------------------------------------------------------


class TestUpload:
    async def test_a_signed_package_answers_the_manifest_for_review(
        self, http: AsyncClient, appliance: Appliance
    ) -> None:
        await sign_in(http)
        body = appliance.package(
            "v1.3.0",
            changes=["Improved CQ-20B state synchronisation"],
            min_app_version="v1.1.0",
            description="Autumn release",
        )
        response = await http.post(UPDATE, content=body)
        assert response.status_code == 200, response.text
        answer = response.json()
        assert answer["manifest"]["version"] == "v1.3.0"
        assert answer["manifest"]["min_app_version"] == "v1.1.0"
        assert answer["manifest"]["changes"] == ["Improved CQ-20B state synchronisation"]
        assert answer["manifest"]["key_id"]
        assert answer["size"] == len(body)
        # Nothing has been installed: this is a review card, not an apply.
        assert installed_version(appliance.paths) == "v1.2.0"
        assert not (appliance.data / "app" / "v1.3.0").exists()

    async def test_the_body_is_spooled_to_data_tmp_and_never_to_the_private_tmp(
        self, http: AsyncClient, appliance: Appliance
    ) -> None:
        """Q9's actual requirement, asserted on the path rather than on 2 GB of RAM."""
        await sign_in(http)
        body = appliance.package("v1.3.0")
        assert (await http.post(UPDATE, content=body)).status_code == 200
        spooled = list(appliance.paths.tmp_dir.glob("upload-*.tar"))
        assert len(spooled) == 1
        assert spooled[0].parent == appliance.data / "tmp"
        assert spooled[0].stat().st_size == len(body)

    async def test_a_second_upload_replaces_the_first_and_frees_its_space(
        self, http: AsyncClient, appliance: Appliance
    ) -> None:
        await sign_in(http)
        assert (await http.post(UPDATE, content=appliance.package("v1.3.0"))).status_code == 200
        first = next(iter(appliance.paths.tmp_dir.glob("upload-*.tar")))
        assert (await http.post(UPDATE, content=appliance.package("v1.4.0"))).status_code == 200
        assert not first.exists()
        assert len(list(appliance.paths.tmp_dir.glob("upload-*.tar"))) == 1

    async def test_a_tampered_package_is_refused_naming_the_rule(
        self, http: AsyncClient, appliance: Appliance
    ) -> None:
        await sign_in(http)
        raw = bytearray(appliance.package("v1.3.0"))
        raw[raw.index(b'"created_at"') + 20] ^= 0x20
        response = await http.post(UPDATE, content=bytes(raw))
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "validation_failed"
        assert error["detail"]["rule"] == "signature"
        assert "not built with a trusted signing key" in error["message"]
        # A refused upload does not sit in /data/tmp taking up room.
        assert list(appliance.paths.tmp_dir.glob("upload-*.tar")) == []

    async def test_a_downgrade_is_refused_and_says_to_roll_back_instead(
        self, http: AsyncClient, appliance: Appliance
    ) -> None:
        await sign_in(http)
        response = await http.post(UPDATE, content=appliance.package("v1.1.0"))
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["detail"]["rule"] == "downgrade"
        assert "Roll back" in error["message"]

    async def test_an_empty_body_is_refused(self, http: AsyncClient) -> None:
        await sign_in(http)
        response = await http.post(UPDATE, content=b"")
        assert response.status_code == 422
        assert response.json()["error"]["detail"]["rule"] == "upload"

    async def test_an_operator_may_not_upload(
        self, http: AsyncClient, appliance: Appliance
    ) -> None:
        await sign_in(http, OPERATOR_PASSWORD)
        response = await http.post(UPDATE, content=appliance.package("v1.3.0"))
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "permission_denied"

    async def test_discarding_takes_the_upload_with_it(
        self, http: AsyncClient, appliance: Appliance
    ) -> None:
        await sign_in(http)
        await http.post(UPDATE, content=appliance.package("v1.3.0"))
        response = await http.request("DELETE", UPDATE)
        assert response.status_code == 200
        assert response.json() == {"discarded": True}
        assert list(appliance.paths.tmp_dir.glob("upload-*.tar")) == []


# -- apply -----------------------------------------------------------------------------


class TestApply:
    async def test_apply_now_installs_and_audits_it(
        self, http: AsyncClient, appliance: Appliance
    ) -> None:
        await sign_in(http)
        await http.post(UPDATE, content=appliance.package("v1.3.0"))
        response = await http.post(f"{UPDATE}/apply", json={"when": "now"})
        assert response.status_code == 200, response.text
        answer = response.json()
        assert answer["state"] == "applied"
        assert answer["applied"]["from_version"] == "v1.2.0"
        assert answer["applied"]["to_version"] == "v1.3.0"
        assert installed_version(appliance.paths) == "v1.3.0"

        with sqlite3.connect(appliance.paths.database) as connection:
            rows = list(
                connection.execute(
                    "SELECT user_ident, detail FROM security_events "
                    "WHERE event_type = 'update_applied'"
                )
            )
        assert len(rows) == 1
        assert rows[0][0] == "admin"
        assert '"when": "now"' in rows[0][1]

    async def test_apply_quiet_raises_the_banner_and_installs_nothing_yet(
        self, http: AsyncClient, appliance: Appliance, update_app: FastAPI
    ) -> None:
        await sign_in(http)
        await http.post(UPDATE, content=appliance.package("v1.3.0"))
        response = await http.post(f"{UPDATE}/apply", json={"when": "quiet"})
        assert response.status_code == 200, response.text
        answer = response.json()
        assert answer["state"] == "waiting_for_quiet"
        assert answer["quiet"]["quiet"] is False
        assert installed_version(appliance.paths) == "v1.2.0"
        banner = update_app.state.broadcaster.state.system.banner(UPDATE_READY_BANNER)
        assert banner is not None and banner.level == "info"

    async def test_a_package_whose_migrations_fail_changes_nothing(
        self, http: AsyncClient, appliance: Appliance
    ) -> None:
        await sign_in(http)
        source = make_source(
            appliance.root,
            "v1.3.0",
            extra={
                "proskenion/db/__init__.py": "",
                "proskenion/db/migrations.py": (
                    "import sys\n"
                    "print('004_pages.sql failed', file=sys.stderr)\n"
                    "raise SystemExit(2)\n"
                ),
            },
        )
        body = build_package(
            appliance.root, "v1.3.0", appliance.signing, source=source
        ).read_bytes()
        assert (await http.post(UPDATE, content=body)).status_code == 200
        response = await http.post(f"{UPDATE}/apply", json={"when": "now"})
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["detail"]["rule"] == "migration"
        assert "004_pages.sql" in error["detail"]["reason"]
        assert installed_version(appliance.paths) == "v1.2.0"
        assert not (appliance.data / "app" / "v1.3.0").exists()

    async def test_applying_with_nothing_waiting_is_refused(self, http: AsyncClient) -> None:
        await sign_in(http)
        response = await http.post(f"{UPDATE}/apply", json={"when": "now"})
        assert response.status_code == 422
        assert response.json()["error"]["detail"]["rule"] == "no_pending"

    async def test_an_unknown_when_is_rejected_by_the_schema(
        self, http: AsyncClient
    ) -> None:
        await sign_in(http)
        response = await http.post(f"{UPDATE}/apply", json={"when": "tonight"})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_failed"


# -- rollback and status ---------------------------------------------------------------


class TestRollbackAndStatus:
    async def test_rollback_returns_to_the_previous_version(
        self, http: AsyncClient, appliance: Appliance
    ) -> None:
        await sign_in(http)
        await http.post(UPDATE, content=appliance.package("v1.3.0"))
        await http.post(f"{UPDATE}/apply", json={"when": "now"})
        response = await http.post(f"{UPDATE}/rollback", json={})
        assert response.status_code == 200, response.text
        assert response.json()["from_version"] == "v1.3.0"
        assert response.json()["to_version"] == "v1.2.0"
        assert installed_version(appliance.paths) == "v1.2.0"

    async def test_rollback_with_nowhere_to_go_is_refused(self, http: AsyncClient) -> None:
        await sign_in(http)
        response = await http.post(f"{UPDATE}/rollback", json={})
        assert response.status_code == 422
        assert response.json()["error"]["detail"]["rule"] == "no_previous"

    async def test_status_shows_what_is_installed_and_what_is_waiting(
        self, http: AsyncClient, appliance: Appliance
    ) -> None:
        await sign_in(http)
        empty = (await http.get(f"{UPDATE}/status")).json()
        assert empty["installed_version"] == "v1.2.0"
        assert empty["pending"] is None
        assert empty["state"] == "idle"
        assert empty["quiet"]["outside_nightly_window"] is True

        await http.post(UPDATE, content=appliance.package("v1.3.0"))
        waiting = (await http.get(f"{UPDATE}/status")).json()
        assert waiting["pending"]["version"] == "v1.3.0"
        assert waiting["pending"]["manifest"]["version"] == "v1.3.0"

    async def test_status_is_admin_only(self, http: AsyncClient) -> None:
        await sign_in(http, OPERATOR_PASSWORD)
        assert (await http.get(f"{UPDATE}/status")).status_code == 403
