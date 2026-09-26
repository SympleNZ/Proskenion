"""Application factory and request-id middleware."""

import logging
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.middleware.cors import CORSMiddleware

from proskenion.api.app import API_PREFIX, create_app
from proskenion.api.request_id import REQUEST_ID_HEADER, new_request_id
from proskenion.config import Config
from proskenion.core import certs
from proskenion.logging import get_request_id

CROCKFORD = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


def test_new_request_id_shape() -> None:
    ids = {new_request_id() for _ in range(200)}
    assert len(ids) == 200
    for request_id in ids:
        assert len(request_id) == 26
        assert set(request_id) <= CROCKFORD


def test_new_request_id_is_time_sortable() -> None:
    earlier = new_request_id()
    time.sleep(0.005)
    later = new_request_id()
    assert earlier < later


async def test_success_response_has_request_id(client: AsyncClient) -> None:
    response = await client.get(f"{API_PREFIX}/probe/ok")
    assert response.status_code == 200
    request_id = response.headers[REQUEST_ID_HEADER]
    assert len(request_id) == 26 and set(request_id) <= CROCKFORD


async def test_each_request_gets_its_own_id(client: AsyncClient) -> None:
    first = await client.get("/health")
    second = await client.get("/health")
    assert first.headers[REQUEST_ID_HEADER] != second.headers[REQUEST_ID_HEADER]


async def test_request_id_bound_during_handler_and_cleared_after(app: FastAPI) -> None:
    seen: list[str | None] = []

    @app.get(f"{API_PREFIX}/probe/context")
    async def context() -> dict[str, str | None]:
        seen.append(get_request_id())
        return {"request_id": get_request_id()}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"{API_PREFIX}/probe/context")
    assert seen == [response.headers[REQUEST_ID_HEADER]]
    assert response.json()["request_id"] == response.headers[REQUEST_ID_HEADER]
    assert get_request_id() is None


def test_no_cors_middleware(app: FastAPI) -> None:
    names = {getattr(m.cls, "__name__", "") for m in app.user_middleware}
    assert CORSMiddleware.__name__ not in names


def test_app_state(app: FastAPI, config: Config) -> None:
    assert app.state.config is config
    assert isinstance(app.state.started_at, float)


def test_routes_are_versioned_except_health(app: FastAPI) -> None:
    # The OpenAPI document is the public view of every mounted path.
    paths = set(app.openapi()["paths"])
    api_paths = {p for p in paths if p.startswith("/api/")}
    assert api_paths
    assert all(p.startswith(f"{API_PREFIX}/") for p in api_paths)
    assert {p for p in paths if not p.startswith(API_PREFIX)} == {"/health"}


async def test_lifespan_logs_startup_and_shutdown(
    config: Config, caplog: pytest.LogCaptureFixture
) -> None:
    app = create_app(config)
    with caplog.at_level(logging.INFO, logger="proskenion.api.app"):
        async with app.router.lifespan_context(app):
            pass
    messages = [record.getMessage() for record in caplog.records]
    assert any("starting" in m for m in messages)
    assert any("shutting down" in m for m in messages)


async def test_startup_gives_the_name_nginx_serves_a_certificate(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§3.2: the first boot after the first install must not leave nginx with nothing to load.

    auditorium.conf reads /data/certs/live/<fqdn>/ and nginx will not start
    without it — and the wizard and Certificates screen that could issue one
    are behind nginx. §4.14's bootstrap file names no host, so the manager
    takes the name from the site as well.
    """
    site = tmp_path / "auditorium.conf"
    site.write_text(
        "    ssl_certificate     /data/certs/live/auditorium.example.nz/fullchain.pem;\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(certs, "NGINX_SITE_CONFIG", site)
    assert config.server.hostname is None
    app = create_app(config)
    async with app.router.lifespan_context(app):
        assert app.state.certs.hostname == "auditorium.example.nz"
        assert certs.certificate_installed(config.app.data_dir, "auditorium.example.nz")
        assert certs.reload_sentinel_path(config.app.data_dir).is_file()
