"""First-run wizard endpoints and the first-run gate (spec §16.4, §10.4, Q4)."""

from collections.abc import AsyncIterator
from datetime import datetime
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response

from proskenion.api import setup as setup_api
from proskenion.api.app import API_PREFIX, create_app
from proskenion.api.deps import FIRST_RUN_INCOMPLETE_REASON
from proskenion.config import Config
from proskenion.core import auth, certs, system_config
from proskenion.core import network as network_core
from proskenion.core.auth import COOKIE_NAME, TokenService
from proskenion.core.bus import EventBus
from proskenion.core.devices import DeviceManager
from proskenion.core.platform import DevelopmentPlatform, Platform
from proskenion.core.ratelimit import RateLimiter
from proskenion.core.state import StateStore
from proskenion.db.connection import MEMORY, Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import mixer as mixer_crud
from proskenion.db.crud import security_events, users
from proskenion.db.migrations import migrate
from tests.unit.api.conftest import build_app, make_client

SETUP = f"{API_PREFIX}/setup"
DRIVERS = f"{API_PREFIX}/drivers"
DEVICES = f"{API_PREFIX}/devices"
ADMIN_PASSWORD = "admin-password-long"
OPERATOR_PASSWORD = "operator-password-long"


@pytest.fixture(autouse=True)
def cheap_bcrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "BCRYPT_ROUNDS", 4)


@pytest.fixture
async def fresh_db() -> AsyncIterator[Database]:
    """The seeded database as it is on a new appliance: placeholder credentials."""
    database = Database()
    await database.open(MEMORY)
    try:
        await migrate(database)
        yield database
    finally:
        await database.close()


@pytest.fixture
def setup_app(
    config: Config, fresh_db: Database, tokens: TokenService, limiter: RateLimiter, tmp_path: Path
) -> FastAPI:
    app = build_app(config, fresh_db, tokens, limiter)
    # The platform and the reload hook are injected: the wizard writes the
    # certificate under tmp_path and never shells out to systemctl in a test.
    app.state.platform = DevelopmentPlatform(data_dir=tmp_path / "data")
    app.state.nginx_reload_hook = lambda: certs.ReloadOutcome(True, "nginx reloaded")
    return app


@pytest.fixture
async def setup_client(setup_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with make_client(setup_app) as http:
        yield http


@pytest.fixture
async def setup_app_with_devices(
    config: Config, fresh_db: Database, tokens: TokenService, limiter: RateLimiter, tmp_path: Path
) -> AsyncIterator[FastAPI]:
    """The wizard app with a running (empty) device manager.

    ``/drivers`` and ``/devices`` need one to answer at all (§5.5); the other
    setup tests never reach it because they never call those two routes.
    """
    bus = EventBus()
    await bus.start()
    state = StateStore(config, bus)
    manager = DeviceManager(fresh_db, state, bus, config)
    await manager.start()
    app = create_app(
        config,
        db=fresh_db,
        tokens=tokens,
        limiter=limiter,
        bus=bus,
        state=state,
        devices_manager=manager,
    )
    app.state.platform = DevelopmentPlatform(data_dir=tmp_path / "data")
    app.state.nginx_reload_hook = lambda: certs.ReloadOutcome(True, "nginx reloaded")
    try:
        yield app
    finally:
        await manager.stop()
        await bus.stop()


@pytest.fixture
async def devices_client(setup_app_with_devices: FastAPI) -> AsyncIterator[AsyncClient]:
    async with make_client(setup_app_with_devices) as http:
        yield http


def session_cookie(response: Response) -> tuple[str, str] | None:
    """(value, raw Set-Cookie header) for the session cookie, if the response set one."""
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(f"{COOKIE_NAME}="):
            jar: SimpleCookie = SimpleCookie()
            jar.load(header)
            return jar[COOKIE_NAME].value, header
    return None


async def _post(client: AsyncClient, step: int, body: dict[str, Any] | None = None) -> Any:
    return await client.post(f"{SETUP}/step/{step}", json=body or {})


async def _steps_one_and_two(client: AsyncClient) -> None:
    assert (
        await _post(client, 1, {"locale": "en_NZ.UTF-8", "timezone": "Pacific/Auckland"})
    ).status_code == 200
    assert (
        await _post(
            client, 2, {"password": ADMIN_PASSWORD, "password_confirm": ADMIN_PASSWORD}
        )
    ).status_code == 200


async def _through_step_seven(client: AsyncClient) -> None:
    await _steps_one_and_two(client)
    for step, body in (
        (3, {"skipped": True}),
        (4, {"device_ids": [1, 2]}),
        (5, {"password": OPERATOR_PASSWORD, "password_confirm": OPERATOR_PASSWORD}),
        (6, {"option": "self_signed", "hostname": "av.school.nz"}),
        (7, {}),
    ):
        response = await _post(client, step, body)
        assert response.status_code == 200, response.text


# -- state ------------------------------------------------------------------------


async def test_state_reports_the_next_step_and_what_to_display(
    setup_client: AsyncClient,
) -> None:
    body = (await setup_client.get(f"{SETUP}/state")).json()
    assert body["first_run"] is True
    assert body["next_step"] == 1
    assert [step["step"] for step in body["steps"]] == [1, 2, 3, 4, 5, 6, 7]
    assert body["steps"][0]["key"] == "welcome"
    detected = body["detected"]
    assert detected["timezone"]
    assert detected["platform"] == "development"
    assert "hostname" in detected and "address" in detected
    options = {o["id"]: o for o in body["certificate"]["options"]}
    assert options["self_signed"]["available"] is True
    # This harness never runs the real boot sequence, so app.state.certs is
    # never built and the option reports honestly unavailable (Q7) — see
    # test_step_six_falls_back_to_self_signed_with_no_manager for what a real
    # submission does in that state.
    assert options["lets_encrypt"]["available"] is False
    assert options["lets_encrypt"]["reason"]
    assert body["certificate"]["installed"] is None


async def test_resumability_across_a_discarded_client(
    setup_app: FastAPI, setup_client: AsyncClient
) -> None:
    await _steps_one_and_two(setup_client)
    await setup_client.aclose()

    async with make_client(setup_app) as reloaded:
        body = (await reloaded.get(f"{SETUP}/state")).json()
    assert body["next_step"] == 3
    completed = {step["step"]: step for step in body["steps"] if step["completed"]}
    assert set(completed) == {1, 2}
    assert completed[1]["summary"] == {"locale": "en_NZ.UTF-8", "timezone": "Pacific/Auckland"}
    assert completed[2]["summary"]["admin_password_set"] is True
    assert completed[2]["completed_at"] is not None
    assert ADMIN_PASSWORD not in str(body)


async def test_abort_after_step_two_keeps_the_password_and_first_run(
    setup_client: AsyncClient, fresh_db: Database
) -> None:
    await _steps_one_and_two(setup_client)
    admin = await users.get_by_tier(fresh_db, "admin")
    assert admin is not None and auth.verify_secret(ADMIN_PASSWORD, admin.password)
    assert (await setup_client.get(f"{SETUP}/state")).json()["first_run"] is True
    # Signing in works, but the gate still refuses everything else.
    login = await setup_client.post(
        f"{API_PREFIX}/auth/login", json={"password": ADMIN_PASSWORD}
    )
    assert login.status_code == 200
    gated = await setup_client.get(f"{API_PREFIX}/probe/admin-only")
    assert gated.status_code == 403
    assert gated.json()["error"]["detail"]["reason"] == FIRST_RUN_INCOMPLETE_REASON


# -- submission -------------------------------------------------------------------


async def test_a_step_out_of_order_is_refused_and_a_completed_one_revisited(
    setup_client: AsyncClient,
) -> None:
    early = await _post(setup_client, 4, {"device_ids": []})
    assert early.status_code == 422
    error = early.json()["error"]
    assert error["code"] == "validation_failed"
    assert error["detail"]["fields"][0]["type"] == "out_of_order"

    await _steps_one_and_two(setup_client)
    revisit = await _post(setup_client, 1, {"locale": "en_GB.UTF-8"})
    assert revisit.status_code == 200
    assert revisit.json()["step"]["summary"]["locale"] == "en_GB.UTF-8"
    assert revisit.json()["next_step"] == 3


async def test_short_password_and_mismatch_are_refused_with_field_detail(
    setup_client: AsyncClient,
) -> None:
    await _post(setup_client, 1, {"locale": "en_NZ.UTF-8"})
    short = await _post(
        setup_client, 2, {"password": "01234567890", "password_confirm": "01234567890"}
    )
    assert short.status_code == 422
    fields = short.json()["error"]["detail"]["fields"]
    assert fields[0]["field"] == "password" and fields[0]["type"] == "too_short"

    mismatch = await _post(
        setup_client, 2, {"password": ADMIN_PASSWORD, "password_confirm": "something-else-12"}
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["error"]["detail"]["fields"][0]["field"] == "password_confirm"
    assert (await setup_client.get(f"{SETUP}/state")).json()["next_step"] == 2


async def test_an_unknown_step_number_is_refused(setup_client: AsyncClient) -> None:
    response = await _post(setup_client, 9, {})
    assert response.status_code == 422
    assert response.json()["error"]["detail"]["fields"][0]["type"] == "unknown_step"


async def test_step_two_replaces_both_placeholders_and_step_five_the_operator(
    setup_client: AsyncClient, fresh_db: Database
) -> None:
    assert await users.has_placeholder_passwords(fresh_db) is True
    await _steps_one_and_two(setup_client)
    assert await users.has_placeholder_passwords(fresh_db) is False
    seeded = await users.get_by_tier(fresh_db, "operator")
    assert seeded is not None

    await _post(setup_client, 3, {"skipped": True})
    await _post(setup_client, 4, {})
    assert (
        await _post(
            setup_client,
            5,
            {"password": OPERATOR_PASSWORD, "password_confirm": OPERATOR_PASSWORD},
        )
    ).status_code == 200
    operator = await users.get_by_tier(fresh_db, "operator")
    assert operator is not None
    assert operator.password != seeded.password
    assert auth.verify_secret(OPERATOR_PASSWORD, operator.password)


async def test_step_six_writes_the_certificate_and_reports_the_reload(
    setup_client: AsyncClient, tmp_path: Path
) -> None:
    await _steps_one_and_two(setup_client)
    await _post(setup_client, 3, {"skipped": True})
    await _post(setup_client, 4, {})
    await _post(
        setup_client, 5, {"password": OPERATOR_PASSWORD, "password_confirm": OPERATOR_PASSWORD}
    )
    response = await _post(setup_client, 6, {"option": "self_signed", "hostname": "av.school.nz"})
    assert response.status_code == 200
    summary = response.json()["step"]["summary"]
    assert summary["nginx_reloaded"] is True
    assert summary["self_signed"] is True
    assert certs.certificate_paths(tmp_path / "data", "av.school.nz").exists

    state = (await setup_client.get(f"{SETUP}/state")).json()
    assert state["certificate"]["installed"]["domain"] == "av.school.nz"
    assert state["certificate"]["installed"]["renewal_history"] == []


async def test_step_three_applies_a_real_network_change(
    setup_client: AsyncClient, config: Config
) -> None:
    """Step 3 goes through the same path the admin Network card
    uses (proskenion.core.network) when it is not skipped and a full
    address is given — the same validation, the same confirm-or-revert."""
    await _steps_one_and_two(setup_client)
    response = await _post(
        setup_client,
        3,
        {
            "hostname": "auditorium",
            "address": "10.2.30.60",
            "prefix_length": 24,
            "gateway": "10.2.30.1",
            "dns": ["10.2.30.1"],
            "skipped": False,
        },
    )
    assert response.status_code == 200, response.text
    summary = response.json()["step"]["summary"]
    assert summary["changed"] is True
    assert summary["change"]["address"] == "10.2.30.60"
    assert summary["change"]["hostname"] == "auditorium"
    assert summary["change"]["confirm_token"]

    doc = system_config.read(config.app.data_dir)
    assert doc["network"]["address"] == "10.2.30.60/24"
    pending = network_core.read_pending(config.app.data_dir)
    assert pending is not None
    assert pending.confirm_token == summary["change"]["confirm_token"]

    requests = list((config.app.data_dir / "run" / "helper").glob("*.json"))
    assert len(requests) == 1


async def test_step_three_skipped_records_only_and_applies_nothing(
    setup_client: AsyncClient, config: Config
) -> None:
    await _steps_one_and_two(setup_client)
    response = await _post(
        setup_client, 3, {"address": "10.2.30.60", "hostname": "auditorium", "skipped": True}
    )
    assert response.status_code == 200
    summary = response.json()["step"]["summary"]
    assert summary["changed"] is False
    assert "change" not in summary
    assert network_core.read_pending(config.app.data_dir) is None


async def test_step_three_rejects_an_invalid_network_change(setup_client: AsyncClient) -> None:
    await _steps_one_and_two(setup_client)
    response = await _post(
        setup_client,
        3,
        {
            "hostname": "auditorium",
            "address": "10.2.30.60",
            "prefix_length": 24,
            "gateway": "10.9.9.1",  # off the submitted subnet
            "dns": ["10.2.30.1"],
            "skipped": False,
        },
    )
    assert response.status_code == 422
    fields = {f["field"] for f in response.json()["error"]["detail"]["fields"]}
    assert "gateway" in fields


async def test_step_six_refuses_lets_encrypt_with_no_token(setup_client: AsyncClient) -> None:
    await _steps_one_and_two(setup_client)
    await _post(setup_client, 3, {"skipped": True})
    await _post(setup_client, 4, {})
    await _post(
        setup_client, 5, {"password": OPERATOR_PASSWORD, "password_confirm": OPERATOR_PASSWORD}
    )
    response = await _post(setup_client, 6, {"option": "lets_encrypt"})
    assert response.status_code == 422
    assert response.json()["error"]["detail"]["fields"][0]["type"] == "required"


async def test_step_six_falls_back_to_self_signed_with_no_manager(
    setup_client: AsyncClient,
) -> None:
    """First-run must never be blocked by a failed issuance (Q7)."""
    await _steps_one_and_two(setup_client)
    await _post(setup_client, 3, {"skipped": True})
    await _post(setup_client, 4, {})
    await _post(
        setup_client, 5, {"password": OPERATOR_PASSWORD, "password_confirm": OPERATOR_PASSWORD}
    )
    response = await _post(
        setup_client,
        6,
        {"option": "lets_encrypt", "hostname": "av.school.nz", "token": "cf-token"},
    )
    assert response.status_code == 200
    summary = response.json()["step"]["summary"]
    assert summary["option"] == "self_signed"
    assert summary["requested_option"] == "lets_encrypt"
    assert summary["fallback_reason"]


async def test_step_two_is_rate_limited(setup_client: AsyncClient) -> None:
    """The bootstrap path sets a password without a session; §6.8 applies (Q4)."""
    await _post(setup_client, 1, {"locale": "en_NZ.UTF-8"})
    for _ in range(3):
        await _post(setup_client, 2, {"password": "short", "password_confirm": "short"})
    locked = await _post(
        setup_client, 2, {"password": ADMIN_PASSWORD, "password_confirm": ADMIN_PASSWORD}
    )
    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "rate_limited"
    assert locked.headers["Retry-After"]
    # Other steps are unaffected.
    assert (await _post(setup_client, 1, {"locale": "en_NZ.UTF-8"})).status_code == 200


# -- commit -----------------------------------------------------------------------


async def test_complete_is_refused_while_a_step_is_incomplete(
    setup_client: AsyncClient,
) -> None:
    await _steps_one_and_two(setup_client)
    response = await setup_client.post(f"{SETUP}/complete")
    assert response.status_code == 422
    fields = {f["field"] for f in response.json()["error"]["detail"]["fields"]}
    assert {"step_3", "step_4", "step_5", "step_6"} <= fields


async def test_complete_is_refused_while_a_placeholder_remains(
    setup_client: AsyncClient, fresh_db: Database
) -> None:
    await _through_step_seven(setup_client)
    await users.set_password_hash(fresh_db, "operator", users.PLACEHOLDER_HASH_PREFIX + ".x")
    response = await setup_client.post(f"{SETUP}/complete")
    assert response.status_code == 422
    assert any(
        f["type"] == "placeholder" for f in response.json()["error"]["detail"]["fields"]
    )


async def test_complete_commits_and_closes_the_wizard(
    setup_client: AsyncClient, fresh_db: Database
) -> None:
    await _through_step_seven(setup_client)
    committed = await setup_client.post(f"{SETUP}/complete")
    assert committed.status_code == 200
    assert committed.json()["first_run"] is False
    assert committed.json()["completed_at"]

    from proskenion.core import setup as setup_core

    assert await setup_core.first_run_completed(fresh_db) is True

    for method, path in (
        ("GET", f"{SETUP}/state"),
        ("POST", f"{SETUP}/step/1"),
        ("POST", f"{SETUP}/complete"),
    ):
        response = await setup_client.request(method, path, json={})
        assert response.status_code == 403, path
        error = response.json()["error"]
        assert error["code"] == "permission_denied"
        assert error["detail"]["reason"] == "setup_complete"
        assert "database reset" in error["message"]


# -- the gate (Q4) ----------------------------------------------------------------


async def test_the_gate_refuses_other_api_routes_during_first_run(
    setup_client: AsyncClient,
) -> None:
    response = await setup_client.get(f"{API_PREFIX}/probe/ok")
    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "permission_denied"
    assert error["detail"]["reason"] == FIRST_RUN_INCOMPLETE_REASON
    assert error["detail"]["setup_path"] == "/setup"
    assert error["request_id"]


async def test_health_and_auth_are_never_gated(setup_client: AsyncClient) -> None:
    assert (await setup_client.get("/health")).status_code == 200
    # A wrong password reaches the login handler rather than the gate.
    login = await setup_client.post(f"{API_PREFIX}/auth/login", json={"password": "wrong-one"})
    assert login.status_code == 401
    assert (await setup_client.get(f"{SETUP}/state")).status_code == 200


async def test_the_gate_is_inert_after_the_commit(setup_client: AsyncClient) -> None:
    await _through_step_seven(setup_client)
    assert (await setup_client.post(f"{SETUP}/complete")).status_code == 200
    # No restart, no cache invalidation by hand: the very next request passes.
    assert (await setup_client.get(f"{API_PREFIX}/probe/ok")).status_code == 200


async def test_a_configured_appliance_is_not_gated(client: AsyncClient) -> None:
    """The shared fixture is a commissioned appliance: first run is long done."""
    assert (await client.get(f"{API_PREFIX}/probe/ok")).status_code == 200
    assert (await client.get(f"{SETUP}/state")).status_code == 403


# -- step 2 signs the wizard in (§10.4 step 4, §6.4) -------------------------------


async def test_step_two_returns_tier_and_expires_at_and_sets_the_session_cookie(
    setup_client: AsyncClient,
) -> None:
    await _post(setup_client, 1, {"locale": "en_NZ.UTF-8"})
    response = await _post(
        setup_client, 2, {"password": ADMIN_PASSWORD, "password_confirm": ADMIN_PASSWORD}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["tier"] == "admin"
    assert datetime.fromisoformat(body["expires_at"]).utcoffset() is not None

    cookie = session_cookie(response)
    assert cookie is not None
    _, header = cookie
    lowered = header.lower()
    assert "httponly" in lowered
    assert "samesite=strict" in lowered
    assert "secure" in lowered


async def test_step_two_drops_the_secure_flag_under_development(
    dev_config: Config, fresh_db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    app = build_app(dev_config, fresh_db, tokens, limiter)
    async with make_client(app) as client:
        await _post(client, 1, {"locale": "en_NZ.UTF-8"})
        response = await _post(
            client, 2, {"password": ADMIN_PASSWORD, "password_confirm": ADMIN_PASSWORD}
        )
    cookie = session_cookie(response)
    assert cookie is not None
    lowered = cookie[1].lower()
    assert "secure" not in lowered
    assert "httponly" in lowered and "samesite=strict" in lowered


async def test_step_two_writes_a_login_success_event_marked_from_the_wizard(
    setup_client: AsyncClient, fresh_db: Database
) -> None:
    await _post(setup_client, 1, {"locale": "en_NZ.UTF-8"})
    await _post(setup_client, 2, {"password": ADMIN_PASSWORD, "password_confirm": ADMIN_PASSWORD})

    events = await security_events.query(fresh_db, event_type="login_success")
    assert len(events) == 1
    assert events[0].detail is not None
    assert '"tier": "admin"' in events[0].detail
    assert '"source": "first_run"' in events[0].detail


# -- the wizard's device step creates a mixer's channels (§7.3) -------------------


async def test_wizard_device_step_creates_the_mixers_channels(
    setup_app_with_devices: FastAPI, devices_client: AsyncClient, fresh_db: Database
) -> None:
    """``POST /devices`` (``proskenion/api/devices.py``) is the primary place
    a mixer's channels are created; the wizard's device step is the
    defensive second one (see ``_ensure_mixer_channels``'s docstring).
    This proves the wizard's own call, for a mixer device that exists in the
    database without ever having gone through that endpoint — a database
    reset and re-run, in the module's own words."""
    device = await devices_crud.create(
        fresh_db,
        category="mixer",
        driver_key="stub",
        name="Desk",
        config={"transport": {"type": "loopback"}, "driver": {}},
    )
    await setup_app_with_devices.state.devices.reload(device.id)
    assert await mixer_crud.list_channels(fresh_db, device_id=device.id) == []

    await _steps_one_and_two(devices_client)
    assert (await _post(devices_client, 3, {"skipped": True})).status_code == 200
    response = await _post(devices_client, 4, {"device_ids": [device.id], "skipped": False})
    assert response.status_code == 200, response.text

    channels = await mixer_crud.list_channels_with_refs(fresh_db, device.id)
    assert [(c.channel.channel_kind, [r.driver_ref for r in c.refs]) for c in channels] == [
        ("main", ["main"]),
        *(("input", [f"in{n}"]) for n in range(1, 7)),
        ("output", ["out1"]),
    ]


async def test_wizard_device_step_gives_a_configured_mixer_only_its_main(
    setup_app_with_devices: FastAPI, devices_client: AsyncClient, fresh_db: Database
) -> None:
    """A mixer that already has channels of its own is given a Main if it
    lacks one, and nothing else: the rest is the admin's to add."""
    device = await devices_crud.create(
        fresh_db,
        category="mixer",
        driver_key="stub",
        name="Desk",
        config={"transport": {"type": "loopback"}, "driver": {}},
    )
    await setup_app_with_devices.state.devices.reload(device.id)
    mic = await mixer_crud.create_channel(fresh_db, device_id=device.id, name="Mic")
    await mixer_crud.set_channel_refs(fresh_db, mic.id, ["in1"])

    await _steps_one_and_two(devices_client)
    assert (await _post(devices_client, 3, {"skipped": True})).status_code == 200
    response = await _post(devices_client, 4, {"device_ids": [device.id], "skipped": False})
    assert response.status_code == 200, response.text

    channels = await mixer_crud.list_channels(fresh_db, device_id=device.id)
    assert [(c.channel_kind, c.name) for c in channels] == [("input", "Mic"), ("main", "Main")]


async def test_wizard_device_step_is_a_no_op_with_no_devices(
    devices_client: AsyncClient, fresh_db: Database
) -> None:
    """No device manager row at all: a silent no-op, not an error."""
    await _steps_one_and_two(devices_client)
    assert (await _post(devices_client, 3, {"skipped": True})).status_code == 200

    response = await _post(devices_client, 4, {"device_ids": [], "skipped": True})

    assert response.status_code == 200, response.text
    assert await mixer_crud.list_channels(fresh_db) == []


# -- drivers and devices are exempt from the gate (this task) ---------------------


async def test_drivers_and_devices_succeed_during_first_run_with_the_wizard_session(
    devices_client: AsyncClient,
) -> None:
    await _post(devices_client, 1, {"locale": "en_NZ.UTF-8"})
    login = await _post(
        devices_client, 2, {"password": ADMIN_PASSWORD, "password_confirm": ADMIN_PASSWORD}
    )
    assert login.status_code == 200
    # Still first run: nothing after step 2 has been submitted.
    assert (await devices_client.get(f"{SETUP}/state")).json()["first_run"] is True

    drivers = await devices_client.get(DRIVERS)
    assert drivers.status_code == 200
    devices = await devices_client.get(DEVICES)
    assert devices.status_code == 200
    assert devices.json()["devices"] == []


async def test_drivers_and_devices_without_the_cookie_are_401_not_the_gates_403(
    devices_client: AsyncClient,
) -> None:
    # First run, and no session at all: the exemption means the gate never
    # gets a say, and the ordinary admin-tier check answers instead.
    drivers = await devices_client.get(DRIVERS)
    assert drivers.status_code == 401
    error = drivers.json()["error"]
    assert error["code"] == "unauthenticated"
    assert error["detail"]["reason"] == "missing"

    devices = await devices_client.get(DEVICES)
    assert devices.status_code == 401
    assert devices.json()["error"]["code"] == "unauthenticated"


async def test_every_other_gated_route_still_answers_the_gates_403(
    setup_client: AsyncClient,
) -> None:
    """The exemption is scoped to drivers and devices, not to every admin route."""
    await _post(setup_client, 1, {"locale": "en_NZ.UTF-8"})
    await _post(setup_client, 2, {"password": ADMIN_PASSWORD, "password_confirm": ADMIN_PASSWORD})
    # A valid admin session from the wizard itself — still refused, because
    # /probe/admin-only is neither /setup, /auth, /drivers nor /devices.
    response = await setup_client.get(f"{API_PREFIX}/probe/admin-only")
    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "permission_denied"
    assert error["detail"]["reason"] == FIRST_RUN_INCOMPLETE_REASON


async def test_the_exemptions_change_nothing_after_the_commit(
    setup_app_with_devices: FastAPI, devices_client: AsyncClient
) -> None:
    await _steps_one_and_two(devices_client)
    for step, body in (
        (3, {"skipped": True}),
        (4, {"device_ids": []}),
        (5, {"password": OPERATOR_PASSWORD, "password_confirm": OPERATOR_PASSWORD}),
        (6, {"option": "self_signed", "hostname": "av.school.nz"}),
        (7, {}),
    ):
        assert (await _post(devices_client, step, body)).status_code == 200
    assert (await devices_client.post(f"{SETUP}/complete")).status_code == 200

    # Same admin-tier behaviour as during first run: the session still works,
    # and a fresh client without one still gets 401, not the (now inert) gate.
    assert (await devices_client.get(DRIVERS)).status_code == 200
    async with make_client(setup_app_with_devices) as anon:
        response = await anon.get(DRIVERS)
    assert response.status_code == 401


# -- GET /auth/session during first run (this task, defect 1) ---------------------
#
# /auth is exempt from the gate (decision Q4) so signing in still works during
# commissioning, which makes it the one request an anonymous page makes at
# boot. Answering plain 401 unauthenticated there sent an installer to /login
# instead of /setup (docs/phase-1-milestone.md). The fix distinguishes the two
# reasons instead.


async def test_anonymous_session_during_first_run_is_403_first_run_incomplete(
    setup_client: AsyncClient,
) -> None:
    response = await setup_client.get(f"{API_PREFIX}/auth/session")
    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "permission_denied"
    assert error["detail"]["reason"] == FIRST_RUN_INCOMPLETE_REASON
    assert error["detail"]["setup_path"] == "/setup"


async def test_signed_in_session_during_first_run_is_still_200(
    setup_client: AsyncClient,
) -> None:
    """Step 2 signs the wizard in and needs this to keep working (§10.4)."""
    await _steps_one_and_two(setup_client)
    response = await setup_client.get(f"{API_PREFIX}/auth/session")
    assert response.status_code == 200
    assert response.json()["tier"] == "admin"


async def test_anonymous_session_after_commit_reverts_to_401_unauthenticated(
    setup_client: AsyncClient,
) -> None:
    await _through_step_seven(setup_client)
    assert (await setup_client.post(f"{SETUP}/complete")).status_code == 200
    setup_client.cookies.clear()
    response = await setup_client.get(f"{API_PREFIX}/auth/session")
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "unauthenticated"
    assert error["detail"]["reason"] == "missing"


# -- the platform fallback of _platform() honours config too (this task, finding 5) --


async def test_platform_fallback_honours_configured_dirs_when_none_is_injected(
    config: Config,
    fresh_db: Database,
    tokens: TokenService,
    limiter: RateLimiter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5.4.

    The boot sequence sets ``app.state.platform`` (T9 follow-up 4, closed), so
    ``/setup/state`` normally never reaches this fallback; this proves that a
    caller which has not run it still gets the configured directories, not
    the real, hard-coded ``/srv/appliance`` and ``/data``.
    """
    calls: list[dict[str, object]] = []

    def spy(**kwargs: object) -> DevelopmentPlatform:
        calls.append(kwargs)
        return DevelopmentPlatform(
            appliance_dir=kwargs.get("appliance_dir"),  # type: ignore[arg-type]
            data_dir=kwargs.get("data_dir"),  # type: ignore[arg-type]
        )

    monkeypatch.setattr("proskenion.api.setup.detect_platform", spy)

    app = build_app(config, fresh_db, tokens, limiter)
    # No app.state.platform set: the fallback in `_platform()` must detect it.
    async with make_client(app) as client:
        response = await client.get(f"{SETUP}/state")
    assert response.status_code == 200
    assert calls == [{"appliance_dir": config.app.state_dir, "data_dir": config.app.data_dir}]


# -- where step 6 puts the certificate (§6.16, §5.4) ----------------------------


@pytest.mark.parametrize("machine", ["development", "raspberry-pi"])
async def test_step_six_issues_the_certificate_under_the_platforms_data_directory(
    config: Config,
    fresh_db: Database,
    tokens: TokenService,
    limiter: RateLimiter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    machine: str,
) -> None:
    """The platform is detected from ``config`` as the application detects it.

    On a development machine the certificate follows ``config.app.data_dir``,
    so two appliances started side by side never share a certificate file. On
    the appliance it stays under ``/data`` (here the fake tree's), whatever
    ``data_dir`` says, because that is where nginx reads it.
    """
    root = tmp_path / "root"
    root.mkdir()
    if machine == "raspberry-pi":
        model = root / "proc" / "device-tree" / "model"
        model.parent.mkdir(parents=True)
        model.write_bytes(b"Raspberry Pi Compute Module 5 Rev 1.0\0")
    configured = tmp_path / "configured-data"
    config = config.model_copy(
        update={"app": config.app.model_copy(update={"data_dir": configured})}
    )
    real_detect = setup_api.detect_platform

    def detect(**kwargs: Any) -> Platform:
        return real_detect(root=root, **kwargs)

    monkeypatch.setattr("proskenion.api.setup.detect_platform", detect)
    app = build_app(config, fresh_db, tokens, limiter)  # no platform injected
    app.state.nginx_reload_hook = lambda: certs.ReloadOutcome(True, "nginx reloaded")

    async with make_client(app) as client:
        await _steps_one_and_two(client)
        await _post(client, 3, {"skipped": True})
        await _post(client, 4, {})
        await _post(
            client, 5, {"password": OPERATOR_PASSWORD, "password_confirm": OPERATOR_PASSWORD}
        )
        response = await _post(client, 6, {"option": "self_signed", "hostname": "av.school.nz"})
    assert response.status_code == 200

    expected_data = configured if machine == "development" else root / "data"
    paths = certs.certificate_paths(expected_data, "av.school.nz")
    assert response.json()["step"]["summary"]["certificate_path"] == str(paths.certificate)
    assert paths.certificate.is_file() and paths.key.is_file()
    if machine == "raspberry-pi":
        assert not configured.exists()


# -- the certificate step's default hostname -------------------
#
# On the real appliance (24 September 2026) config.server.hostname is None
# (§4.14's bootstrap carries no [server] hostname) and the certificate step's
# default fell back to environment.hostname — socket.gethostname(),
# "auditorium" — a single-label name no Cloudflare zone can own. The default
# must be the name nginx actually serves; the network step's own field
# (detected.hostname) must stay the machine's own short name.


def _write_site(path: Path, *names: str) -> None:
    path.write_text(
        "".join(
            f"    ssl_certificate     /data/certs/live/{name}/fullchain.pem;\n"
            for name in names
        ),
        encoding="utf-8",
    )


async def test_certificate_default_is_the_served_name_not_the_machine_hostname(
    setup_client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = tmp_path / "auditorium.conf"
    _write_site(site, "auditorium.obhs.school.nz")
    monkeypatch.setattr(certs, "NGINX_SITE_CONFIG", site)

    body = (await setup_client.get(f"{SETUP}/state")).json()
    assert body["certificate"]["hostname"] == "auditorium.obhs.school.nz"
    # The network step's field is unaffected — still the machine's own short name.
    assert body["detected"]["hostname"] != "auditorium.obhs.school.nz"


async def test_certificate_default_falls_through_to_a_completed_steps_own_hostname(
    setup_client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The served name is only the last resort: a certificate already issued
    (a resumed wizard) is still what ``GET /setup/state`` reports, exactly as
    before this task."""
    site = tmp_path / "auditorium.conf"
    _write_site(site, "auditorium.obhs.school.nz")
    monkeypatch.setattr(certs, "NGINX_SITE_CONFIG", site)

    await _steps_one_and_two(setup_client)
    await _post(setup_client, 3, {"skipped": True})
    await _post(setup_client, 4, {})
    await _post(
        setup_client, 5, {"password": OPERATOR_PASSWORD, "password_confirm": OPERATOR_PASSWORD}
    )
    await _post(setup_client, 6, {"option": "self_signed", "hostname": "auditorium.obhs.school.nz"})

    body = (await setup_client.get(f"{SETUP}/state")).json()
    assert body["certificate"]["hostname"] == "auditorium.obhs.school.nz"


# -- a certificate for a name nginx does not serve is refused ---


async def test_certificate_step_refuses_a_hostname_nginx_does_not_serve(
    setup_client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both failed attempts on the real appliance left a certificate under a
    name nginx never reads — a stray self-signed ``auditorium`` pair beside
    the real, served ``auditorium.obhs.school.nz`` one. The wizard must
    refuse, saying which name nginx serves, rather than write one nobody uses.
    """
    site = tmp_path / "auditorium.conf"
    _write_site(site, "auditorium.obhs.school.nz")
    monkeypatch.setattr(certs, "NGINX_SITE_CONFIG", site)

    await _steps_one_and_two(setup_client)
    await _post(setup_client, 3, {"skipped": True})
    await _post(setup_client, 4, {})
    await _post(
        setup_client, 5, {"password": OPERATOR_PASSWORD, "password_confirm": OPERATOR_PASSWORD}
    )

    response = await _post(setup_client, 6, {"option": "self_signed", "hostname": "auditorium"})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_failed"
    assert error["detail"]["fields"][0]["field"] == "hostname"
    assert "auditorium.obhs.school.nz" in error["message"]
    # Nothing was written for the refused name.
    assert not certs.certificate_paths(tmp_path / "data", "auditorium").exists


# -- the certificate step and the browser's certificate exception ---------------------
#
# 25 September 2026, the rebuilt appliance: the application's first start
# installed a self-signed fallback for the name nginx serves (§3.2), the
# browser running the wizard accepted it, and step 6 (self-signed) then issued
# a *new* one and reloaded nginx. The browser's exception was for the old
# certificate, so the wizard's next request failed at TLS and never reached the
# controller: "Could not reach the controller".

SERVED_NAME = "auditorium.obhs.school.nz"


async def _through_step_five(client: AsyncClient) -> None:
    await _steps_one_and_two(client)
    await _post(client, 3, {"skipped": True})
    await _post(client, 4, {})
    await _post(client, 5, {"password": OPERATOR_PASSWORD, "password_confirm": OPERATOR_PASSWORD})


async def test_step_six_keeps_the_certificate_the_first_start_installed(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both sides for real: the startup fallback (the lifespan's
    ``CertificateManager.ensure_served``) and the wizard's step 6, over HTTP."""
    site = tmp_path / "auditorium.conf"
    _write_site(site, SERVED_NAME)
    monkeypatch.setattr(certs, "NGINX_SITE_CONFIG", site)
    app = create_app(config)
    async with app.router.lifespan_context(app):
        data = app.state.platform.data_dir()
        served_at_start = certs.served_certificate_bytes(data, SERVED_NAME)
        assert served_at_start is not None, "the first start installed no fallback"
        async with make_client(app) as client:
            await _through_step_five(client)
            response = await _post(client, 6, {"option": "self_signed", "hostname": SERVED_NAME})

    assert response.status_code == 200, response.text
    body = response.json()
    assert certs.served_certificate_bytes(data, SERVED_NAME) == served_at_start, (
        "step 6 replaced a self-signed certificate the browser had already accepted"
    )
    assert body["certificate_replaced"] is False
    assert SERVED_NAME in body["certificate_names"]
    assert body["step"]["summary"]["self_signed"] is True


async def test_step_six_says_when_it_did_replace_the_certificate(
    setup_client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No certificate yet, or one that will not do: a new one is issued, and
    the response says so, with the names it is valid for."""
    site = tmp_path / "auditorium.conf"
    _write_site(site, SERVED_NAME)
    monkeypatch.setattr(certs, "NGINX_SITE_CONFIG", site)
    data = tmp_path / "data"
    assert certs.served_certificate_bytes(data, SERVED_NAME) is None

    await _through_step_five(setup_client)
    first = await _post(setup_client, 6, {"option": "self_signed", "hostname": SERVED_NAME})
    assert first.status_code == 200, first.text
    assert first.json()["certificate_replaced"] is True
    assert first.json()["certificate_names"][0] == SERVED_NAME
    issued = certs.served_certificate_bytes(data, SERVED_NAME)
    assert issued is not None

    # Submitting the step again keeps what it just issued.
    again = await _post(setup_client, 6, {"option": "self_signed", "hostname": SERVED_NAME})
    assert again.json()["certificate_replaced"] is False
    assert certs.served_certificate_bytes(data, SERVED_NAME) == issued

    # One that will not do (here: judged so) is replaced, and reported replaced.
    monkeypatch.setattr(certs, "reusable_self_signed", lambda *args, **kwargs: None)
    replaced = await _post(setup_client, 6, {"option": "self_signed", "hostname": SERVED_NAME})
    assert replaced.json()["certificate_replaced"] is True
    assert certs.served_certificate_bytes(data, SERVED_NAME) != issued
