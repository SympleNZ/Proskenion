"""``/system/network*`` (contracts §4, §5, §10.8, §22.4).

Every endpoint has a success test and one covering its primary failure mode,
asserting the §16.1 envelope code (§22.4). The helper itself is real here —
``HelperClient.submit`` only writes a request file — so these tests also
prove that file lands, without needing a running ``auditorium-helper``.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response

from proskenion.api.app import API_PREFIX
from proskenion.config import Config
from proskenion.core import network, system_config
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from tests.unit.api.conftest import ADMIN_PASSWORD, OPERATOR_PASSWORD, make_client

NETWORK = f"{API_PREFIX}/system/network"
AUTH = f"{API_PREFIX}/auth"

VALID_BODY: dict[str, Any] = {
    "hostname": "auditorium",
    "address": "10.2.30.50",
    "prefix_length": 24,
    "gateway": "10.2.30.1",
    "dns": ["10.2.30.1"],
}


async def login(client: AsyncClient, password: str = ADMIN_PASSWORD) -> Response:
    return await client.post(f"{AUTH}/login", json={"password": password})


def code(response: Response) -> str:
    return str(response.json()["error"]["code"])


@pytest.fixture
async def client(app: FastAPI) -> Any:
    async with make_client(app) as http:
        yield http


# -- GET /system/network --------------------------------------------------------------


async def test_get_reports_nothing_configured_by_default(client: AsyncClient) -> None:
    await login(client)
    response = await client.get(NETWORK)
    assert response.status_code == 200
    assert response.json() == {
        "hostname": None,
        "address": None,
        "prefix_length": None,
        "gateway": None,
        "dns": [],
    }


async def test_get_reflects_what_is_in_system_json(
    client: AsyncClient, config: Config
) -> None:
    system_config.merge(
        config.app.data_dir,
        {
            "hostname": "auditorium",
            "network": {"address": "10.2.30.45/24", "gateway": "10.2.30.1", "dns": ["10.2.30.1"]},
        },
    )
    await login(client)
    response = await client.get(NETWORK)
    assert response.json() == {
        "hostname": "auditorium",
        "address": "10.2.30.45",
        "prefix_length": 24,
        "gateway": "10.2.30.1",
        "dns": ["10.2.30.1"],
    }


async def test_get_requires_admin(client: AsyncClient) -> None:
    await login(client, OPERATOR_PASSWORD)
    response = await client.get(NETWORK)
    assert response.status_code == 403
    assert code(response) == "permission_denied"


# -- POST /system/network --------------------------------------------------------------


async def test_post_rejects_an_invalid_submission(client: AsyncClient) -> None:
    await login(client)
    response = await client.post(
        NETWORK, json={**VALID_BODY, "gateway": "10.9.9.1"}  # off the submitted subnet
    )
    assert response.status_code == 422
    assert code(response) == "validation_failed"
    assert "gateway" in response.json()["error"]["detail"]


async def test_post_rejects_an_address_already_used_by_a_device(
    client: AsyncClient, db: Database
) -> None:
    await devices_crud.create(
        db,
        category="projector",
        driver_key="pjlink",
        name="Projector",
        config={"transport": {"type": "tcp", "host": "10.2.30.50", "port": 4352}},
    )
    await login(client)
    response = await client.post(NETWORK, json=VALID_BODY)  # address 10.2.30.50, above
    assert response.status_code == 422
    assert code(response) == "validation_failed"
    assert "address" in response.json()["error"]["detail"]


async def test_post_answers_202_and_hands_over_to_the_reconnection_flow(
    client: AsyncClient, config: Config
) -> None:
    await login(client)
    response = await client.post(NETWORK, json=VALID_BODY)
    assert response.status_code == 202
    body = response.json()
    assert body["confirm_token"]
    assert body["applied_at"]
    assert body["reverts_at"]
    assert body["address"] == "10.2.30.50"
    assert body["hostname"] == "auditorium"
    assert body["dns_updated"] is False  # no Cloudflare token configured

    # system.json now carries the new settings, and a revert marker exists —
    # both written before the helper was ever asked to apply anything, so
    # the deadline holds even if this request had never returned (§10.8).
    doc = system_config.read(config.app.data_dir)
    assert doc["network"]["address"] == "10.2.30.50/24"
    pending = network.read_pending(config.app.data_dir)
    assert pending is not None
    assert pending.confirm_token == body["confirm_token"]

    # And the helper was asked (contracts §5 step 1: "asks the helper to apply").
    requests = list((config.app.data_dir / "run" / "helper").glob("*.json"))
    assert len(requests) == 1
    assert '"apply-network"' in requests[0].read_text(encoding="utf-8")


async def test_post_requires_admin(client: AsyncClient) -> None:
    await login(client, OPERATOR_PASSWORD)
    response = await client.post(NETWORK, json=VALID_BODY)
    assert response.status_code == 403
    assert code(response) == "permission_denied"


# -- POST /system/network/confirm -------------------------------------------------------


async def test_confirm_keeps_the_change(client: AsyncClient, config: Config) -> None:
    await login(client)
    posted = await client.post(NETWORK, json=VALID_BODY)
    token = posted.json()["confirm_token"]

    response = await client.post(f"{NETWORK}/confirm", json={"confirm_token": token})
    assert response.status_code == 200
    assert response.json() == {"confirmed": True}
    assert network.read_pending(config.app.data_dir) is None


async def test_confirm_with_an_unknown_token_is_not_found(client: AsyncClient) -> None:
    await login(client)
    response = await client.post(f"{NETWORK}/confirm", json={"confirm_token": "does-not-exist"})
    assert response.status_code == 404
    assert code(response) == "not_found"


# -- GET /system/network/state -----------------------------------------------------------


async def test_state_reports_nothing_pending_by_default(client: AsyncClient) -> None:
    await login(client)
    response = await client.get(f"{NETWORK}/state")
    assert response.json() == {
        "pending": False,
        "applied_at": None,
        "reverts_at": None,
        "previous_address": None,
    }


async def test_state_reports_a_pending_change(client: AsyncClient, config: Config) -> None:
    system_config.merge(config.app.data_dir, {"network": {"address": "10.2.30.10/24"}})
    await login(client)
    posted = await client.post(NETWORK, json=VALID_BODY)
    assert posted.status_code == 202

    response = await client.get(f"{NETWORK}/state")
    body = response.json()
    assert body["pending"] is True
    assert body["applied_at"] == posted.json()["applied_at"]
    assert body["reverts_at"] == posted.json()["reverts_at"]
    assert body["previous_address"] == "10.2.30.10/24"
