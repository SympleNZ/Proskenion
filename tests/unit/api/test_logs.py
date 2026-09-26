"""``GET /system/logs`` and ``GET /system/logs/export`` (spec §21.24 "Logs", §16.7).

The System tab's endpoints: the raw structured application log, filtered,
paginated and — for the export — streamed as plain text. Log files are
written directly under ``config.logging.path`` (real files, including a
rotated ``.gz``), exactly what an appliance's logrotate leaves behind
(``appliance/etc/logrotate.d/auditorium``).
"""

from __future__ import annotations

import gzip
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from httpx import AsyncClient, Response

from proskenion.api.app import API_PREFIX, create_app
from proskenion.config import Config
from proskenion.core.auth import TokenService
from proskenion.core.ratelimit import RateLimiter
from proskenion.db.connection import Database
from proskenion.logging import APPLICATION_LOG_NAME
from tests.unit.api.conftest import ADMIN_PASSWORD, HIRER_PIN, OPERATOR_PASSWORD, make_client

SYSTEM = f"{API_PREFIX}/system"


def _line(
    *,
    timestamp: str,
    level: str = "INFO",
    logger: str = "proskenion.core.mixer",
    message: str,
    **extra: object,
) -> str:
    return json.dumps(
        {"timestamp": timestamp, "level": level, "logger": logger, "message": message, **extra}
    )


async def login(client: AsyncClient, password: str = ADMIN_PASSWORD) -> Response:
    return await client.post(f"{API_PREFIX}/auth/login", json={"password": password})


@asynccontextmanager
async def running(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with app.router.lifespan_context(app), make_client(app) as http:
        yield http


async def test_logs_is_admin_only(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    (config.logging.path).mkdir(parents=True, exist_ok=True)
    app = create_app(config, db=db, tokens=tokens, limiter=limiter)
    async with running(app) as client:
        anonymous = await client.get(f"{SYSTEM}/logs")
        await login(client, OPERATOR_PASSWORD)
        operator = await client.get(f"{SYSTEM}/logs")
        await login(client)
        admin = await client.get(f"{SYSTEM}/logs")
    assert anonymous.status_code == 401
    assert operator.status_code == 403
    assert operator.json()["error"]["code"] == "permission_denied"
    assert admin.status_code == 200, admin.text


async def test_logs_is_refused_to_a_hirer_session(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    config.logging.path.mkdir(parents=True, exist_ok=True)
    app = create_app(config, db=db, tokens=tokens, limiter=limiter)
    async with running(app) as client:
        await login(client)
        assert (
            await client.post(f"{API_PREFIX}/hirer/enabled", json={"enabled": True})
        ).status_code == 200
        assert (await client.post(f"{API_PREFIX}/auth/hirer", json={"pin": HIRER_PIN})).is_success
        response = await client.get(f"{SYSTEM}/logs")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


async def test_logs_reads_the_live_and_rotated_files_newest_first(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    log_dir = config.logging.path
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / APPLICATION_LOG_NAME).write_text(
        _line(timestamp="2026-09-10T09:00:01+12:00", message="live") + "\n", encoding="utf-8"
    )
    with gzip.open(
        log_dir / f"{APPLICATION_LOG_NAME}-20260909-030000.gz", "wt", encoding="utf-8"
    ) as handle:
        handle.write(_line(timestamp="2026-09-09T09:00:00+12:00", message="rotated") + "\n")

    app = create_app(config, db=db, tokens=tokens, limiter=limiter)
    async with running(app) as client:
        await login(client)
        response = await client.get(f"{SYSTEM}/logs")

    assert response.status_code == 200, response.text
    body = response.json()
    assert [e["message"] for e in body["entries"]] == ["live", "rotated"]
    assert body["has_more"] is False


async def test_logs_filters_by_level_module_and_date_range(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    log_dir = config.logging.path
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / APPLICATION_LOG_NAME).write_text(
        "\n".join(
            [
                _line(
                    timestamp="2026-09-10T09:00:00+12:00",
                    level="DEBUG",
                    logger="proskenion.core.knx",
                    message="debug-knx",
                ),
                _line(
                    timestamp="2026-09-10T09:01:00+12:00",
                    level="WARNING",
                    logger="proskenion.core.mixer",
                    message="warn-mixer",
                ),
                _line(
                    timestamp="2026-09-10T09:02:00+12:00",
                    level="ERROR",
                    logger="proskenion.api.system",
                    message="error-api",
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    app = create_app(config, db=db, tokens=tokens, limiter=limiter)
    async with running(app) as client:
        await login(client)
        by_level = await client.get(f"{SYSTEM}/logs", params={"level": "WARNING"})
        by_module = await client.get(f"{SYSTEM}/logs", params={"module": "proskenion.core"})
        by_range = await client.get(
            f"{SYSTEM}/logs",
            params={"from": "2026-09-10T09:00:30+12:00", "to": "2026-09-10T09:01:30+12:00"},
        )
        bad_level = await client.get(f"{SYSTEM}/logs", params={"level": "not-a-level"})

    assert [e["message"] for e in by_level.json()["entries"]] == ["error-api", "warn-mixer"]
    assert [e["message"] for e in by_module.json()["entries"]] == ["warn-mixer", "debug-knx"]
    assert [e["message"] for e in by_range.json()["entries"]] == ["warn-mixer"]
    assert bad_level.status_code == 422
    assert bad_level.json()["error"]["code"] == "validation_failed"


async def test_logs_paginates_with_limit_and_offset(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    log_dir = config.logging.path
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / APPLICATION_LOG_NAME).write_text(
        "\n".join(
            _line(timestamp=f"2026-09-10T09:00:{i:02d}+12:00", message=f"m{i}") for i in range(5)
        )
        + "\n",
        encoding="utf-8",
    )
    app = create_app(config, db=db, tokens=tokens, limiter=limiter)
    async with running(app) as client:
        await login(client)
        page1 = await client.get(f"{SYSTEM}/logs", params={"limit": 2, "offset": 0})
        page2 = await client.get(f"{SYSTEM}/logs", params={"limit": 2, "offset": 2})

    assert [e["message"] for e in page1.json()["entries"]] == ["m4", "m3"]
    assert page1.json()["has_more"] is True
    assert [e["message"] for e in page2.json()["entries"]] == ["m2", "m1"]
    assert page2.json()["has_more"] is True


async def test_logs_redacts_sensitive_context_on_read(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    log_dir = config.logging.path
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / APPLICATION_LOG_NAME).write_text(
        _line(timestamp="2026-09-10T09:00:00+12:00", message="had a secret", password="hunter2")
        + "\n",
        encoding="utf-8",
    )
    app = create_app(config, db=db, tokens=tokens, limiter=limiter)
    async with running(app) as client:
        await login(client)
        response = await client.get(f"{SYSTEM}/logs")

    [entry] = response.json()["entries"]
    assert entry["context"]["password"] == "[redacted]"
    assert "hunter2" not in response.text


async def test_logs_a_malformed_line_does_not_fail_the_request(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    log_dir = config.logging.path
    log_dir.mkdir(parents=True, exist_ok=True)
    content = (
        _line(timestamp="2026-09-10T09:00:00+12:00", message="good") + "\nnot json at all {{{\n"
    )
    (log_dir / APPLICATION_LOG_NAME).write_text(content, encoding="utf-8")
    app = create_app(config, db=db, tokens=tokens, limiter=limiter)
    async with running(app) as client:
        await login(client)
        response = await client.get(f"{SYSTEM}/logs")

    assert response.status_code == 200, response.text
    assert [e["message"] for e in response.json()["entries"]] == ["good"]


async def test_logs_export_is_admin_only_plain_text_chronological_and_redacted(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    log_dir = config.logging.path
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / APPLICATION_LOG_NAME).write_text(
        "\n".join(
            [
                _line(timestamp="2026-09-10T09:00:00+12:00", message="first", device="mixer"),
                _line(timestamp="2026-09-10T09:00:01+12:00", message="second", pin="1234"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    app = create_app(config, db=db, tokens=tokens, limiter=limiter)
    async with running(app) as client:
        anonymous = await client.get(f"{SYSTEM}/logs/export")
        await login(client, OPERATOR_PASSWORD)
        operator = await client.get(f"{SYSTEM}/logs/export")
        await login(client)
        admin = await client.get(f"{SYSTEM}/logs/export")

    assert anonymous.status_code == 401
    assert operator.status_code == 403
    assert admin.status_code == 200, admin.text
    assert admin.headers["content-type"].startswith("text/plain")
    assert "attachment" in admin.headers["content-disposition"]
    lines = admin.text.strip("\n").split("\n")
    assert lines[0].endswith("first device=mixer") or "first" in lines[0]
    assert lines.index([line for line in lines if "first" in line][0]) < lines.index(
        [line for line in lines if "second" in line][0]
    )
    assert "1234" not in admin.text
    assert "[redacted]" in admin.text
