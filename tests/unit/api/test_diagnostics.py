"""``GET /system/diagnostics`` — the soak harness's in-process measurements (§22.7)."""

from __future__ import annotations

import os

from proskenion.api.app import create_app
from proskenion.config import Config
from proskenion.core.auth import TokenService
from proskenion.core.ratelimit import RateLimiter
from proskenion.db.connection import Database
from tests.unit.api.conftest import OPERATOR_PASSWORD
from tests.unit.api.test_system import SYSTEM, login, running


async def test_diagnostics_names_the_process_and_counts_what_proc_cannot(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    app = create_app(config, db=db, tokens=tokens, limiter=limiter)
    async with running(app) as client:
        await login(client)
        response = await client.get(f"{SYSTEM}/diagnostics")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "pid",
        "uptime_seconds",
        "tasks",
        "websocket_connections",
        "loop_lag_p50_ms",
        "loop_lag_p99_ms",
        "loop_lag_samples",
    }
    assert body["pid"] == os.getpid()
    assert body["uptime_seconds"] >= 0
    # The boot sequence's own tasks (watchdog, health poll, broadcaster tick…)
    # are running, and so is the one answering this request.
    assert body["tasks"] > 1
    assert body["websocket_connections"] == 0
    # The watchdog wakes every ten seconds; none has passed yet.
    assert body["loop_lag_samples"] == 0
    assert body["loop_lag_p99_ms"] is None


async def test_diagnostics_is_admin_only(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    app = create_app(config, db=db, tokens=tokens, limiter=limiter)
    async with running(app) as client:
        anonymous = await client.get(f"{SYSTEM}/diagnostics")
        await login(client, OPERATOR_PASSWORD)
        operator = await client.get(f"{SYSTEM}/diagnostics")
    assert anonymous.status_code == 401
    assert operator.status_code == 403
    assert operator.json()["error"]["code"] == "permission_denied"
