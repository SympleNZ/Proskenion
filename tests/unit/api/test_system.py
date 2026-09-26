"""``/health`` (§16.7) and the emergency payload model."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from pydantic import ValidationError

from proskenion import __version__
from proskenion.api.app import API_PREFIX, create_app
from proskenion.api.request_id import REQUEST_ID_HEADER
from proskenion.api.system import (
    EmergencyDetail,
    EmergencyHealth,
    EmergencyReason,
    StorageState,
)
from proskenion.config import Config
from proskenion.core.auth import TokenService
from proskenion.core.drivers import load_shipped_drivers, registry
from proskenion.core.drivers.base import Driver, ProbeResult
from proskenion.core.drivers.capabilities import MatrixCapabilities
from proskenion.core.drivers.categories import Category
from proskenion.core.ratelimit import RateLimiter
from proskenion.core.update import UpdatePaths
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import security_events
from proskenion.logging import APPLICATION_LOG_NAME, configure_logging, shutdown_logging
from proskenion.main import (
    ACCESS_LOG_NAME,
    configure_access_logging,
    shutdown_access_logging,
)
from tests.unit.api.conftest import ADMIN_PASSWORD, HIRER_PIN, OPERATOR_PASSWORD, make_client


async def test_health_ok(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"status", "version", "uptime"}
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert isinstance(body["uptime"], float)
    assert body["uptime"] >= 0.0


async def test_health_is_unversioned(client: AsyncClient) -> None:
    response = await client.get(f"{API_PREFIX}/health")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_health_carries_request_id(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert len(response.headers[REQUEST_ID_HEADER]) == 26


def test_emergency_payload_matches_spec_shape() -> None:
    nz = timezone(timedelta(hours=12))
    payload = EmergencyHealth(
        version="1.3.0",
        reason=EmergencyReason.DATA_UNAVAILABLE,
        detail=EmergencyDetail(
            mount_state="failed",
            storage=StorageState(detected=True, smart="failing", media_errors=4),
            last_backup=datetime(2026, 9, 7, 3, 0, tzinfo=nz),
            backup_media_present=True,
        ),
    )
    assert payload.model_dump(mode="json") == {
        "status": "emergency",
        "version": "1.3.0",
        "reason": "data_unavailable",
        "detail": {
            "mount_state": "failed",
            "storage": {"detected": True, "smart": "failing", "media_errors": 4},
            "last_backup": "2026-09-07T03:00:00+12:00",
            "backup_media_present": True,
        },
    }


def test_emergency_reason_is_closed() -> None:
    assert {reason.value for reason in EmergencyReason} == {
        "data_unavailable",
        "data_readonly",
        "migration_failed",
        "disk_full",
        "not_installed",
    }
    with pytest.raises(ValidationError):
        EmergencyHealth.model_validate(
            {
                "version": "1.0.0",
                "reason": "gremlins",
                "detail": {
                    "mount_state": "failed",
                    "storage": {"detected": False, "smart": "unknown", "media_errors": 0},
                    "last_backup": None,
                    "backup_media_present": False,
                },
            }
        )


def test_emergency_last_backup_may_be_absent_but_never_naive() -> None:
    detail = {
        "mount_state": "failed",
        "storage": {"detected": False, "smart": "unknown", "media_errors": 0},
        "last_backup": None,
        "backup_media_present": False,
    }
    EmergencyHealth.model_validate({"version": "1", "reason": "disk_full", "detail": detail})
    with pytest.raises(ValidationError):
        EmergencyHealth.model_validate(
            {
                "version": "1",
                "reason": "disk_full",
                "detail": {**detail, "last_backup": "2026-09-07T03:00:00"},
            }
        )


# -- the versioned system endpoints (§16.7) --------------------------------------


LOOPBACK: dict[str, Any] = {"transport": {"type": "loopback"}, "driver": {}}
SYSTEM = f"{API_PREFIX}/system"

# Fixed instants for the security log's date-range and ordering tests.
EARLY = "2026-09-10T09:00:00+12:00"
MID = "2026-09-10T10:00:00+12:00"
LATE = "2026-09-10T11:00:00+12:00"


class NeverConnectsDriver(Driver):
    """A device that is still connecting, and always will be (§12.1)."""

    key: ClassVar[str] = "never"
    category: ClassVar[Category] = Category.VIDEO_MATRIX
    name: ClassVar[str] = "Never answers"
    SUPPORTED_TRANSPORTS: ClassVar[list[str]] = ["loopback"]

    async def connect(self) -> None:
        await asyncio.Event().wait()  # the projector is off; nobody is coming

    async def probe(self) -> ProbeResult:
        return ProbeResult(True)

    def capabilities(self) -> MatrixCapabilities:
        return MatrixCapabilities(1, 1, supports_atomic_route=True)


@pytest.fixture
def slow_driver(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    load_shipped_drivers()
    table = dict(registry.DRIVERS)
    table[(NeverConnectsDriver.category, NeverConnectsDriver.key)] = NeverConnectsDriver
    monkeypatch.setattr(registry, "DRIVERS", table)
    yield


async def login(client: AsyncClient, password: str = ADMIN_PASSWORD) -> Response:
    return await client.post(f"{API_PREFIX}/auth/login", json={"password": password})


@asynccontextmanager
async def running(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """The application as it actually runs: the §12.1 boot sequence and a client."""
    async with app.router.lifespan_context(app), make_client(app) as http:
        yield http


async def test_system_health_matches_the_shape_the_health_screen_is_built_against(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    app = create_app(config, db=db, tokens=tokens, limiter=limiter)
    async with running(app) as client:
        await login(client)
        response = await client.get(f"{SYSTEM}/health")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "platform",
        "version",
        "uptime_seconds",
        "cpu",
        "memory",
        "storage",
        "partitions",
        "backup_media",
        "application",
        "devices",
        "time",
        "overall",
    }
    assert body["version"] == __version__
    assert set(body["cpu"]) == {"temperature_c", "level"}
    assert set(body["backup_media"]) == {"present", "absent_since", "level"}
    assert set(body["application"]) == {
        "loop_lag_p50_ms",
        "loop_lag_p99_ms",
        "level",
        "clients",
        "bus",
    }
    assert set(body["time"]) == {"synced", "degraded", "server_time"}
    # The development platform can read almost nothing: null, and "unknown".
    assert body["platform"] == "development"
    assert body["cpu"] == {"temperature_c": None, "level": "unknown"}
    assert body["memory"]["percent"] is None
    assert body["memory"]["level"] == "unknown"
    assert body["uptime_seconds"] is None


async def test_system_health_is_admin_only(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    app = create_app(config, db=db, tokens=tokens, limiter=limiter)
    async with running(app) as client:
        anonymous = await client.get(f"{SYSTEM}/health")
        await login(client, OPERATOR_PASSWORD)
        operator = await client.get(f"{SYSTEM}/health")
    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["code"] == "unauthenticated"
    assert operator.status_code == 403
    assert operator.json()["error"]["code"] == "permission_denied"


async def test_system_time_reports_the_degraded_flag_and_rtc_presence(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    app = create_app(config, db=db, tokens=tokens, limiter=limiter)
    async with running(app) as client:
        await login(client)
        response = await client.get(f"{SYSTEM}/time")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "synced",
        "degraded",
        "rtc_present",
        "server_time",
        "checked_at",
        "source",
    }
    assert isinstance(body["synced"], bool)
    assert isinstance(body["degraded"], bool)
    assert body["rtc_present"] is None or isinstance(body["rtc_present"], bool)
    assert body["server_time"].endswith(("+12:00", "+13:00"))


async def test_system_version_is_open_to_operators(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    app = create_app(config, db=db, tokens=tokens, limiter=limiter)
    async with running(app) as client:
        await login(client, OPERATOR_PASSWORD)
        response = await client.get(f"{SYSTEM}/version")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "version": __version__,
        "platform": "development",
        "environment": "production",
    }


async def test_system_health_before_the_boot_sequence_is_answered_not_crashed(
    client: AsyncClient,
) -> None:
    await login(client)
    response = await client.get(f"{SYSTEM}/health")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"


# -- §12.1: the interface comes up before the devices do --------------------------


async def test_health_is_served_while_a_device_is_still_connecting(
    config: Config,
    db: Database,
    tokens: TokenService,
    limiter: RateLimiter,
    slow_driver: None,
) -> None:
    await devices_crud.create(
        db,
        category="video_matrix",
        driver_key="never",
        name="Booth matrix",
        config=LOOPBACK,
    )
    app = create_app(config, db=db, tokens=tokens, limiter=limiter)
    loop = asyncio.get_running_loop()
    started = loop.time()
    async with running(app) as client:
        booted = loop.time() - started
        response = await client.get("/health")
        record = app.state.state_store.devices.record("hdmi")

    # Startup did not wait for a device that never answers (§12.1).
    assert booted < 5.0
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    # Last-known state is shown, clearly unconfirmed, until the device reports in.
    assert record is not None
    assert record.status == "connecting"


# -- §4.10: access lines -----------------------------------------------------------


async def test_access_lines_go_to_access_log_and_never_to_application_log(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    configure_logging(config)
    configure_access_logging(config)
    try:
        app = create_app(config, db=db, tokens=tokens, limiter=limiter)
        async with make_client(app) as http:
            await http.get("/health?probe=1", headers={"User-Agent": "Auditorium/1.0"})
        access = (Path(config.logging.path) / ACCESS_LOG_NAME).read_text(encoding="utf-8")
        application = (Path(config.logging.path) / APPLICATION_LOG_NAME).read_text(
            encoding="utf-8"
        )
    finally:
        shutdown_access_logging()
        shutdown_logging()

    assert '"GET /health?probe=1 HTTP/1.1" 200' in access
    assert '"Auditorium/1.0"' in access  # combined format, not uvicorn's
    assert access.startswith("127.0.0.1 - - [")
    assert "GET /health" not in application


async def test_the_access_line_records_the_forwarded_client_address(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    """Behind nginx the peer is the proxy; the tablet's address is forwarded (§4.13)."""
    configure_logging(config)
    configure_access_logging(config)
    try:
        app = create_app(config, db=db, tokens=tokens, limiter=limiter)
        async with make_client(app) as http:
            await http.get("/health", headers={"X-Real-IP": "10.20.30.40"})
        access = (Path(config.logging.path) / ACCESS_LOG_NAME).read_text(encoding="utf-8")
    finally:
        shutdown_access_logging()
        shutdown_logging()

    assert access.startswith("10.20.30.40 - - [")


# -- §22.4: the primary failure mode of the two remaining system endpoints -------


async def test_system_time_is_admin_only(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    """§16.7 marks `/system/time` [admin]; §22.4 wants the refusal's code asserted."""
    app = create_app(config, db=db, tokens=tokens, limiter=limiter)
    async with running(app) as client:
        anonymous = await client.get(f"{SYSTEM}/time")
        await login(client, OPERATOR_PASSWORD)
        operator = await client.get(f"{SYSTEM}/time")
    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["code"] == "unauthenticated"
    assert operator.status_code == 403
    assert operator.json()["error"]["code"] == "permission_denied"


async def test_system_version_needs_a_session(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    """Open to any staff tier, but not to a caller with no session at all (§16.7)."""
    app = create_app(config, db=db, tokens=tokens, limiter=limiter)
    async with running(app) as client:
        response = await client.get(f"{SYSTEM}/version")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


# -- restart and reboot (§21.24, contracts §2, §5, §22.4) -------------------------------
#
# The helper is real here too (see test_network.py's own note): `submit`
# only writes a request file, so these tests prove that file lands — and,
# for reboot, that it does not — without needing a running
# `auditorium-helper`.


def _helper_requests(config: Config) -> list[str]:
    directory = config.app.data_dir / "run" / "helper"
    return [p.read_text(encoding="utf-8") for p in directory.glob("*.json")]


async def test_restart_asks_the_helper_and_answers_before_it_takes_effect(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    app = create_app(config, db=db, tokens=tokens, limiter=limiter)
    async with running(app) as client:
        await login(client)
        response = await client.post(f"{SYSTEM}/restart")
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["requested"] == "restart"
    assert body["requested_at"]

    requests = _helper_requests(config)
    assert len(requests) == 1
    assert '"restart-core"' in requests[0]

    rows = await security_events.query(db, event_type="config_changed", limit=10)
    detail = json.loads(rows[0].detail or "{}")
    assert detail["setting"] == "restart"
    assert rows[0].user_ident == "admin"


async def test_restart_requires_admin(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    app = create_app(config, db=db, tokens=tokens, limiter=limiter)
    async with running(app) as client:
        await login(client, OPERATOR_PASSWORD)
        response = await client.post(f"{SYSTEM}/restart")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"
    assert _helper_requests(config) == []


async def test_reboot_asks_the_helper_for_a_normal_reboot(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    app = create_app(config, db=db, tokens=tokens, limiter=limiter)
    async with running(app) as client:
        await login(client)
        response = await client.post(f"{SYSTEM}/reboot")
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["requested"] == "reboot"
    assert body["mode"] == "normal"
    assert body["requested_at"]

    requests = _helper_requests(config)
    assert len(requests) == 1
    assert '"reboot"' in requests[0]
    assert '"normal"' in requests[0]

    rows = await security_events.query(db, event_type="config_changed", limit=10)
    detail = json.loads(rows[0].detail or "{}")
    assert detail["setting"] == "reboot"


async def test_reboot_requires_admin(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    app = create_app(config, db=db, tokens=tokens, limiter=limiter)
    async with running(app) as client:
        await login(client, OPERATOR_PASSWORD)
        response = await client.post(f"{SYSTEM}/reboot")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"
    assert _helper_requests(config) == []


async def test_reboot_is_refused_while_an_os_slot_is_on_trial(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    """Q11: `tryboot.txt` is a one-shot — a plain reboot would abandon the
    trial silently rather than repeating it. OS roll back is the way to give
    up on a trial, and it says so; a reboot that did the same thing quietly
    would not be able to."""
    app = create_app(config, db=db, tokens=tokens, limiter=limiter)
    async with running(app) as client:
        paths = UpdatePaths.for_appliance(
            config.app.data_dir, config.app.state_dir, config.database.path
        )
        await paths.store().update(
            trial={
                "slot": "b",
                "version": "v1.5.0",
                "started_at": "2026-09-20T03:00:00+12:00",
                "deadline_at": "2026-09-20T03:10:00+12:00",
                "booted_at": "2026-09-20T03:00:05+12:00",
            }
        )
        await login(client)
        response = await client.post(f"{SYSTEM}/reboot")
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"]["code"] == "validation_failed"
    assert body["error"]["detail"]["reason"] == "os_trial"
    assert body["error"]["detail"]["slot"] == "b"
    assert body["error"]["detail"]["version"] == "v1.5.0"
    assert "roll back" in body["error"]["message"].lower()

    # Refused before the helper is ever asked — the trial is still intact.
    assert _helper_requests(config) == []


# -- security log (§6.14, §21.24 "Logs") -------------------------------------------


async def hirer_login(client: AsyncClient) -> Response:
    return await client.post(f"{API_PREFIX}/auth/hirer", json={"pin": HIRER_PIN})


async def test_security_log_is_newest_first_and_admin_only(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    """Distinct IP addresses isolate these two rows from the ``login_success``
    row the admin sign-in below writes for real — real login events land at
    the actual current time, after either fixed timestamp here, so an
    unfiltered read would see three rows in a different order than these two
    alone."""
    app = create_app(config, db=db, tokens=tokens, limiter=limiter)
    await security_events.insert(
        db, "login_success", user_ident="admin", ip_address="10.0.0.1", timestamp=EARLY
    )
    await security_events.insert(
        db, "login_failure", user_ident="10.0.0.2", ip_address="10.0.0.2", timestamp=LATE
    )
    async with running(app) as client:
        anonymous = await client.get(f"{SYSTEM}/security-log")
        await login(client, OPERATOR_PASSWORD)
        operator = await client.get(f"{SYSTEM}/security-log")
        await login(client)
        admin = await client.get(f"{SYSTEM}/security-log")
    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["code"] == "unauthenticated"
    assert operator.status_code == 403
    assert operator.json()["error"]["code"] == "permission_denied"
    assert admin.status_code == 200, admin.text
    entries = [e for e in admin.json()["entries"] if e["ip_address"] in ("10.0.0.1", "10.0.0.2")]
    assert [e["event_type"] for e in entries] == ["login_failure", "login_success"]
    assert entries[0]["outcome"] == "failure"
    assert entries[1]["outcome"] == "success"
    assert entries[0]["ip_address"] == "10.0.0.2"


async def test_security_log_is_refused_to_a_hirer_session(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    app = create_app(config, db=db, tokens=tokens, limiter=limiter)
    async with running(app) as client:
        await login(client)
        enabled = await client.post(f"{API_PREFIX}/hirer/enabled", json={"enabled": True})
        assert enabled.status_code == 200, enabled.text
        # The hirer sign-in below replaces the admin cookie in this same jar.
        assert (await hirer_login(client)).is_success
        response = await client.get(f"{SYSTEM}/security-log")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


async def test_security_log_filters_by_event_type_outcome_ip_and_date_range(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    app = create_app(config, db=db, tokens=tokens, limiter=limiter)
    await security_events.insert(
        db, "login_success", ip_address="10.0.0.1", timestamp=EARLY
    )
    await security_events.insert(
        db, "login_failure", ip_address="10.0.0.1", timestamp=MID
    )
    await security_events.insert(
        db, "lockout", ip_address="10.0.0.2", timestamp=LATE
    )
    async with running(app) as client:
        await login(client)
        by_type = await client.get(f"{SYSTEM}/security-log", params={"event_type": "lockout"})
        by_outcome = await client.get(f"{SYSTEM}/security-log", params={"outcome": "failure"})
        by_ip = await client.get(f"{SYSTEM}/security-log", params={"ip_address": "10.0.0.1"})
        by_range = await client.get(
            f"{SYSTEM}/security-log", params={"from": MID, "to": LATE}
        )
        bad_type = await client.get(f"{SYSTEM}/security-log", params={"event_type": "made_up"})
    assert [e["event_type"] for e in by_type.json()["entries"]] == ["lockout"]
    assert {e["event_type"] for e in by_outcome.json()["entries"]} == {"login_failure", "lockout"}
    assert [e["event_type"] for e in by_ip.json()["entries"]] == ["login_failure", "login_success"]
    assert [e["event_type"] for e in by_range.json()["entries"]] == ["lockout", "login_failure"]
    assert bad_type.status_code == 422
    assert bad_type.json()["error"]["code"] == "validation_failed"


async def test_security_log_paginates_with_limit_and_offset(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    app = create_app(config, db=db, tokens=tokens, limiter=limiter)
    # ``config_changed`` rather than ``login_success``, so the admin sign-in
    # below — which writes its own ``login_success`` row at the real current
    # time — cannot land inside this page.
    for i in range(3):
        await security_events.insert(
            db,
            "config_changed",
            detail=json.dumps({"n": i}),
            timestamp=f"2026-09-1{i}T10:00:00+12:00",
        )
    async with running(app) as client:
        await login(client)
        page = await client.get(
            f"{SYSTEM}/security-log",
            params={"event_type": "config_changed", "limit": 1, "offset": 1},
        )
    entries = page.json()["entries"]
    assert len(entries) == 1
    assert entries[0]["detail"] == {"n": 1}


async def test_security_log_redacts_sensitive_detail_keys(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    """§6.14's ``detail`` never returns anything that looks like a credential,
    even though nothing this application writes today puts one there — the
    API layer's own defence, not a promise about every past or future writer."""
    detail = {
        "scope": "staff_login",
        "reason": "password",
        "new_password": "hunter2",
        "token_version": 4,
    }
    await security_events.insert(db, "password_changed", detail=json.dumps(detail))
    await security_events.insert(db, "config_changed", detail="{not valid json")
    app = create_app(config, db=db, tokens=tokens, limiter=limiter)
    async with running(app) as client:
        await login(client)
        response = await client.get(f"{SYSTEM}/security-log")
    entries = {e["event_type"]: e for e in response.json()["entries"]}
    changed = entries["password_changed"]["detail"]
    assert changed["new_password"] == "[redacted]"
    assert changed["reason"] == "password"
    assert changed["token_version"] == 4  # not a credential — never redacted
    assert entries["config_changed"]["detail"] == {"unparsed": "[redacted]"}


# -- per-module DEBUG toggling (§4.10, §21.24 "Logs") -------------------------------


async def test_debug_logging_lists_known_loggers_admin_only(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    app = create_app(config, db=db, tokens=tokens, limiter=limiter)
    async with running(app) as client:
        anonymous = await client.get(f"{SYSTEM}/debug-logging")
        await login(client, OPERATOR_PASSWORD)
        operator = await client.get(f"{SYSTEM}/debug-logging")
        await login(client)
        admin = await client.get(f"{SYSTEM}/debug-logging")
    assert anonymous.status_code == 401
    assert operator.status_code == 403
    assert admin.status_code == 200, admin.text
    names = {row["name"] for row in admin.json()["loggers"]}
    assert {"proskenion.api", "proskenion.core", "proskenion.db"} <= names
    assert all(row["enabled"] is False for row in admin.json()["loggers"])


async def test_debug_logging_toggle_takes_effect_live_and_persists(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    app = create_app(config, db=db, tokens=tokens, limiter=limiter)
    try:
        async with running(app) as client:
            await login(client)
            on = await client.put(
                f"{SYSTEM}/debug-logging", json={"logger": "proskenion.core", "enabled": True}
            )
            assert on.status_code == 200, on.text
            row = next(r for r in on.json()["loggers"] if r["name"] == "proskenion.core")
            assert row["enabled"] is True
            # Live, through the real logging module — no restart.
            assert logging.getLogger("proskenion.core").level == logging.DEBUG

            # Re-reading the file (what happens at startup) agrees.
            on_disk = json.loads(
                (config.app.data_dir / "config" / "debug.json").read_text(encoding="utf-8")
            )
            assert on_disk == {"proskenion.core": True}

            rows = await security_events.query(db, event_type="config_changed", limit=10)
            detail = json.loads(rows[0].detail or "{}")
            assert detail == {
                "setting": "debug_logging",
                "logger": "proskenion.core",
                "enabled": True,
            }

            off = await client.put(
                f"{SYSTEM}/debug-logging", json={"logger": "proskenion.core", "enabled": False}
            )
            assert off.status_code == 200
            assert logging.getLogger("proskenion.core").level == logging.NOTSET
    finally:
        logging.getLogger("proskenion.core").setLevel(logging.NOTSET)


async def test_debug_logging_rejects_an_unknown_logger(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    app = create_app(config, db=db, tokens=tokens, limiter=limiter)
    async with running(app) as client:
        await login(client)
        response = await client.put(
            f"{SYSTEM}/debug-logging", json={"logger": "proskenion.bogus", "enabled": True}
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"


async def test_debug_logging_put_requires_admin(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    app = create_app(config, db=db, tokens=tokens, limiter=limiter)
    async with running(app) as client:
        await login(client, OPERATOR_PASSWORD)
        response = await client.put(
            f"{SYSTEM}/debug-logging", json={"logger": "proskenion.core", "enabled": True}
        )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"
