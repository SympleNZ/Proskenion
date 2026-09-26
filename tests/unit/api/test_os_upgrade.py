"""``/system/os`` and the OS half of ``/system/update`` (contracts §5, §14.4).

Two things are being proved at this level. **The routing:** one upload route
takes both kinds of package (§14.1) and sends each to the service that applies
it — and a package that claims to be one kind while being the other is refused
by the verifier whichever way round it is offered, which is contracts §3's
rule 5 seen from the endpoint rather than from the verifier's own tests.
**The reading:** ``GET /system/os`` says which slot is running, what the other
one holds, and where the trial has got to, through each state a trial passes.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
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
from proskenion.core.osupgrade import OsUpgradeService, read_os_pending
from proskenion.core.ratelimit import RateLimiter
from proskenion.core.state import StateStore
from proskenion.core.update import UpdatePaths, read_pending, swap_current
from proskenion.core.update_service import UpdateService
from proskenion.db.connection import Database
from proskenion.db.migrations import migrate
from tests.package_factory import Signing, build_package, make_os_source, make_signing, make_source
from tests.unit.api.conftest import ADMIN_PASSWORD, make_client
from tests.unit.core.test_osupgrade import FakePlatform, RecordingHelper
from tests.unit.core.test_update import MIGRATION_OK
from tests.unit.core.test_update import RecordingHelper as AppHelper

AUCKLAND = ZoneInfo("Pacific/Auckland")
NOW = datetime(2026, 9, 20, 19, 0, tzinfo=AUCKLAND)
UPDATE = f"{API_PREFIX}/system/update"
OS = f"{API_PREFIX}/system/os"


class Appliance:
    """/data, /srv/appliance, a boot partition with two slots, and a signing key."""

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
        self.boot = root / "boot"
        (self.boot / "slot-a").mkdir(parents=True)
        (self.boot / "slot-b").mkdir(parents=True)
        self.set_slot_version("a", "v1.0.0")
        self.paths = UpdatePaths.for_appliance(
            self.data, self.state_dir, self.data / "auditorium.db"
        )
        self.write_boot_state({"active_slot": "a", "slots": {"a": "aaaa-02", "b": "aaaa-03"}})

    def set_slot_version(self, slot: str, version: str) -> None:
        (self.boot / f"slot-{slot}" / "os-version.txt").write_text(
            version + "\n", encoding="utf-8"
        )

    def write_boot_state(self, document: dict[str, Any]) -> None:
        (self.state_dir / "boot-state.json").write_text(
            json.dumps(document, indent=2) + "\n", encoding="utf-8"
        )

    def boot_state(self) -> dict[str, Any]:
        return json.loads(
            (self.state_dir / "boot-state.json").read_text(encoding="utf-8")
        )

    def os_package(self, version: str = "v2.0.0") -> bytes:
        return build_package(
            self.root,
            version,
            self.signing,
            source=make_os_source(self.root, version),
            package_type="os",
        ).read_bytes()

    def app_package(self, version: str = "v1.3.0") -> bytes:
        source = make_source(
            self.root,
            version,
            extra={
                "proskenion/db/__init__.py": "",
                "proskenion/db/migrations.py": MIGRATION_OK,
            },
        )
        return build_package(self.root, version, self.signing, source=source).read_bytes()


@pytest.fixture
def appliance(tmp_path: Path) -> Appliance:
    return Appliance(tmp_path / "appliance")


@pytest.fixture
async def file_db(appliance: Appliance) -> AsyncIterator[Database]:
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
        await system_state.set(
            database, setup.SYSTEM_DOMAIN, setup.FIRST_RUN_COMPLETED_KEY, "true"
        )
        yield database
    finally:
        await database.close()


@pytest.fixture
def os_app(
    config: Config,
    file_db: Database,
    tokens: TokenService,
    limiter: RateLimiter,
    appliance: Appliance,
    request: pytest.FixtureRequest,
) -> FastAPI:
    """The application with both update services over the fake appliance."""
    running: str = getattr(request, "param", "a")
    bus = EventBus()
    state = StateStore(config, bus)
    broadcaster = Broadcaster(state, bus)
    updates = UpdateService(
        state,
        file_db,
        broadcaster,
        appliance.paths,
        alert_sink=RecordingAlertSink(),
        helper=AppHelper(),
        anchors_dir=appliance.signing.anchors,
        now=lambda: NOW,
    )
    helper = RecordingHelper(appliance)  # type: ignore[arg-type]
    os_upgrade = OsUpgradeService(
        state,
        file_db,
        broadcaster,
        appliance.paths,
        platform=FakePlatform(appliance.boot, running),  # type: ignore[arg-type]
        helper=helper,  # type: ignore[arg-type]
        alert_sink=RecordingAlertSink(),
        healthy=lambda: True,
        boot_dir=appliance.boot,
        anchors_dir=appliance.signing.anchors,
        now=lambda: NOW,
    )
    application = create_app(
        config,
        db=file_db,
        tokens=tokens,
        limiter=limiter,
        broadcaster=broadcaster,
        update_service=updates,
        os_upgrade_service=os_upgrade,
    )
    application.state.os_test_helper = helper
    return application


@pytest.fixture
async def http(os_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with make_client(os_app) as client:
        yield client


async def sign_in(http: AsyncClient) -> None:
    response = await http.post(f"{API_PREFIX}/auth/login", json={"password": ADMIN_PASSWORD})
    assert response.status_code == 200


# -- the routing (§14.1, contracts §3) ------------------------------------------------


class TestRouting:
    async def test_an_os_package_goes_to_the_slot_service(
        self, http: AsyncClient, appliance: Appliance
    ) -> None:
        await sign_in(http)
        response = await http.post(UPDATE, content=appliance.os_package())
        assert response.status_code == 200, response.text
        assert response.json()["manifest"]["type"] == "os"
        assert read_os_pending(appliance.paths) is not None
        assert read_pending(appliance.paths) is None, "it must not reach the app path"

    async def test_an_application_package_goes_to_the_application_service(
        self, http: AsyncClient, appliance: Appliance
    ) -> None:
        await sign_in(http)
        response = await http.post(UPDATE, content=appliance.app_package())
        assert response.status_code == 200, response.text
        assert response.json()["manifest"]["type"] == "app"
        assert read_pending(appliance.paths) is not None
        assert read_os_pending(appliance.paths) is None

    async def test_an_os_package_forced_down_the_application_path_is_refused(
        self, http: AsyncClient, appliance: Appliance, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The peek is a hint; the verifier is the decision (contracts §3, rule 5)."""
        from proskenion.api import update as update_api

        monkeypatch.setattr(update_api, "peek_manifest_type", lambda path: "app")
        await sign_in(http)
        response = await http.post(UPDATE, content=appliance.os_package())
        assert response.status_code == 422, response.text
        assert response.json()["error"]["detail"]["rule"] == "type"
        assert read_pending(appliance.paths) is None
        assert read_os_pending(appliance.paths) is None

    async def test_an_application_package_forced_down_the_os_path_is_refused(
        self, http: AsyncClient, appliance: Appliance, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from proskenion.api import update as update_api

        monkeypatch.setattr(update_api, "peek_manifest_type", lambda path: "os")
        await sign_in(http)
        response = await http.post(UPDATE, content=appliance.app_package())
        assert response.status_code == 422, response.text
        assert response.json()["error"]["detail"]["rule"] == "type"
        assert read_os_pending(appliance.paths) is None

    async def test_a_refused_package_leaves_nothing_in_data_tmp(
        self, http: AsyncClient, appliance: Appliance, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from proskenion.api import update as update_api

        monkeypatch.setattr(update_api, "peek_manifest_type", lambda path: "os")
        await sign_in(http)
        await http.post(UPDATE, content=appliance.app_package())
        assert list((appliance.data / "tmp").glob("upload-*")) == []

    async def test_discard_forgets_an_os_package_too(
        self, http: AsyncClient, appliance: Appliance
    ) -> None:
        await sign_in(http)
        await http.post(UPDATE, content=appliance.os_package())
        response = await http.request("DELETE", UPDATE)
        assert response.status_code == 200
        assert response.json()["discarded"] is True
        assert read_os_pending(appliance.paths) is None


# -- applying (§14.4) -----------------------------------------------------------------


class TestApply:
    async def test_applying_an_os_package_stages_a_trial_and_reboots(
        self, http: AsyncClient, os_app: FastAPI, appliance: Appliance
    ) -> None:
        await sign_in(http)
        await http.post(UPDATE, content=appliance.os_package())
        response = await http.post(f"{UPDATE}/apply", json={"when": "now"})
        assert response.status_code == 200, response.text
        answer = response.json()
        assert answer["state"] == "os_trial"
        assert answer["trial"]["slot"] == "b"
        assert answer["applied"] is None
        assert os_app.state.os_test_helper.verbs == ["write-slot", "stage-slot", "reboot"]

    async def test_quiet_does_not_apply_to_an_os_upgrade(
        self, http: AsyncClient, os_app: FastAPI, appliance: Appliance
    ) -> None:
        """§14.4: rare, deliberate, at a desk — never unattended at three a.m."""
        await sign_in(http)
        await http.post(UPDATE, content=appliance.os_package())
        response = await http.post(f"{UPDATE}/apply", json={"when": "quiet"})
        assert response.json()["state"] == "os_trial"
        assert "reboot" in os_app.state.os_test_helper.verbs

    async def test_an_application_package_still_takes_the_application_path(
        self, http: AsyncClient, os_app: FastAPI, appliance: Appliance
    ) -> None:
        await sign_in(http)
        await http.post(UPDATE, content=appliance.app_package())
        response = await http.post(f"{UPDATE}/apply", json={"when": "quiet"})
        assert response.json()["state"] == "waiting_for_quiet"
        assert os_app.state.os_test_helper.verbs == []


# -- GET /system/os -------------------------------------------------------------------


class TestStatus:
    async def test_it_reports_both_slots_when_nothing_is_happening(
        self, http: AsyncClient, appliance: Appliance
    ) -> None:
        appliance.set_slot_version("b", "v0.9.0")
        await sign_in(http)
        response = await http.get(OS)
        assert response.status_code == 200, response.text
        answer = response.json()
        assert answer["active_slot"] == "a"
        assert answer["standby_slot"] == "b"
        assert answer["active_version"] == "v1.0.0"
        assert answer["standby_version"] == "v0.9.0"
        assert answer["trial"] is None
        assert answer["staged"] is None

    async def test_after_staging_it_reports_the_trial_and_its_deadline(
        self, http: AsyncClient, appliance: Appliance
    ) -> None:
        await sign_in(http)
        await http.post(UPDATE, content=appliance.os_package())
        await http.post(f"{UPDATE}/apply", json={"when": "now"})
        answer = (await http.get(OS)).json()
        assert answer["staged"] == "b"
        assert answer["trial"]["slot"] == "b"
        assert answer["trial"]["deadline_at"]
        # Still running slot a: the reboot has been asked for, not taken.
        assert answer["trial"]["on_trial"] is False

    @pytest.mark.parametrize("os_app", ["b"], indirect=True)
    async def test_inside_the_trial_it_says_which_slot_is_running(
        self, http: AsyncClient, appliance: Appliance
    ) -> None:
        appliance.set_slot_version("b", "v2.0.0")
        appliance.write_boot_state(
            {
                "active_slot": "a",
                "slots": {"a": "aaaa-02", "b": "aaaa-03"},
                "staged": "b",
                "trial": {
                    "slot": "b",
                    "version": "v2.0.0",
                    "started_at": NOW.isoformat(timespec="seconds"),
                    "deadline_at": (NOW + timedelta(minutes=10)).isoformat(
                        timespec="seconds"
                    ),
                    "booted_at": NOW.isoformat(timespec="seconds"),
                },
            }
        )
        await sign_in(http)
        answer = (await http.get(OS)).json()
        assert answer["active_slot"] == "b"
        assert answer["standby_slot"] == "a"
        assert answer["trial"]["on_trial"] is True
        assert answer["active_version"] == "v2.0.0"

    async def test_a_verified_package_waiting_is_on_the_card(
        self, http: AsyncClient, appliance: Appliance
    ) -> None:
        await sign_in(http)
        await http.post(UPDATE, content=appliance.os_package())
        answer = (await http.get(OS)).json()
        assert answer["pending"]["version"] == "v2.0.0"
        assert answer["pending"]["manifest"]["type"] == "os"


# -- POST /system/os/rollback ---------------------------------------------------------


class TestRollback:
    @pytest.mark.parametrize("os_app", ["b"], indirect=True)
    async def test_during_a_trial_it_is_an_ordinary_reboot(
        self, http: AsyncClient, os_app: FastAPI, appliance: Appliance
    ) -> None:
        appliance.set_slot_version("b", "v2.0.0")
        appliance.write_boot_state(
            {
                "active_slot": "a",
                "slots": {"a": "aaaa-02", "b": "aaaa-03"},
                "trial": {
                    "slot": "b",
                    "version": "v2.0.0",
                    "started_at": NOW.isoformat(timespec="seconds"),
                    "deadline_at": (NOW + timedelta(minutes=10)).isoformat(
                        timespec="seconds"
                    ),
                    "booted_at": NOW.isoformat(timespec="seconds"),
                },
            }
        )
        await sign_in(http)
        response = await http.post(f"{OS}/rollback")
        assert response.status_code == 200, response.text
        assert response.json() == {"slot": "a", "version": "v1.0.0", "mode": "trial_abandoned"}
        assert os_app.state.os_test_helper.verbs == ["reboot"]

    async def test_with_an_empty_other_slot_it_is_refused_with_a_reason(
        self, http: AsyncClient, os_app: FastAPI
    ) -> None:
        await sign_in(http)
        response = await http.post(f"{OS}/rollback")
        assert response.status_code == 422, response.text
        assert response.json()["error"]["detail"]["rule"] == "no_standby"
        assert os_app.state.os_test_helper.verbs == []
