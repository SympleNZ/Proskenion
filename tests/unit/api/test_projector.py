"""The projector API: state, power and input (spec §16.5, §7.4, §22.4).

Every endpoint has a success test and one covering its primary failure mode,
asserting the §16.1 envelope's code, exactly as ``docs/plans/phase-3-contracts.md``'s
Projector section fixes it — the Operator view is built against the
same contract in parallel.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient, Response

from proskenion.api.app import API_PREFIX, create_app
from proskenion.config import Config
from proskenion.core.auth import TokenService
from proskenion.core.bus import EventBus
from proskenion.core.devices import DeviceManager
from proskenion.core.projector import ProjectorService
from proskenion.core.ratelimit import RateLimiter
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from tests.stubs.pjlink_stub import PJLinkStub
from tests.unit.api.conftest import ADMIN_PASSWORD, OPERATOR_PASSWORD, make_client

AUTH = f"{API_PREFIX}/auth"
PROJECTOR = f"{API_PREFIX}/projector"


async def login(client: AsyncClient, password: str = ADMIN_PASSWORD) -> Response:
    return await client.post(f"{AUTH}/login", json={"password": password})


def code(response: Response) -> str:
    body: dict[str, Any] = response.json()
    return str(body["error"]["code"])


def detail(response: Response) -> dict[str, Any]:
    body: dict[str, Any] = response.json()
    return dict(body["error"]["detail"])


@asynccontextmanager
async def _running_app(
    config: Config,
    db: Database,
    tokens: TokenService,
    limiter: RateLimiter,
    stub: PJLinkStub | None,
    *,
    password: str | None = None,
) -> AsyncIterator[FastAPI]:
    """A fully wired application over a real device manager and projector
    service, against ``stub`` (or with no projector configured at all).
    """
    bus = EventBus()
    await bus.start()
    state = StateStore(config, bus)
    device_id: int | None = None
    if stub is not None:
        device = await devices_crud.create(
            db,
            category="projector",
            driver_key="pjlink",
            name="Projector",
            config={
                "transport": {"type": "tcp", "host": "127.0.0.1", "port": stub.port},
                "driver": {"password": password},
            },
        )
        device_id = device.id
    manager = DeviceManager(
        db, state, bus, config, connect_timeout=2.0, probe_timeout=1.0, stop_timeout=2.0
    )
    await manager.start()
    if device_id is not None:
        await manager.wait_for_connection(device_id)
    projector = ProjectorService(state, bus, db, manager)
    await projector.start()
    app = create_app(
        config,
        db=db,
        tokens=tokens,
        limiter=limiter,
        bus=bus,
        state=state,
        devices_manager=manager,
        projector=projector,
    )
    try:
        yield app
    finally:
        await projector.stop()
        await manager.stop()
        await bus.stop()


# -- GET /projector/state -----------------------------------------------------------


async def test_get_projector_state_succeeds(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    async with PJLinkStub(inputs=("11", "31")) as stub:
        stub.set_power_immediately("1")
        async with (
            _running_app(config, db, tokens, limiter, stub) as app,
            make_client(app) as client,
        ):
            await login(client, OPERATOR_PASSWORD)

            response = await client.get(f"{PROJECTOR}/state")

            assert response.status_code == 200, response.text
            body = response.json()
            assert body["state"] == "on"
            assert body["input_ref"] == stub.current_input
            assert {"ref": "31", "label": "Digital 1"} in body["inputs"]
            assert body["remaining_s"] is None


async def test_get_projector_state_requires_a_session(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    async with _running_app(config, db, tokens, limiter, None) as app, make_client(app) as client:
        response = await client.get(f"{PROJECTOR}/state")

        assert response.status_code == 401
        assert code(response) == "unauthenticated"


async def test_get_projector_state_with_none_configured_is_all_nulls(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    async with _running_app(config, db, tokens, limiter, None) as app, make_client(app) as client:
        await login(client)

        response = await client.get(f"{PROJECTOR}/state")

        assert response.status_code == 200, response.text
        assert response.json() == {
            "device_id": None,
            "state": None,
            "input_ref": None,
            "inputs": [],
            "remaining_s": None,
        }


# -- POST /projector/power -----------------------------------------------------------


async def test_post_projector_power_succeeds(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    async with PJLinkStub() as stub:
        stub.set_power_immediately("0")
        async with (
            _running_app(config, db, tokens, limiter, stub) as app,
            make_client(app) as client,
        ):
            await login(client, OPERATOR_PASSWORD)

            response = await client.post(f"{PROJECTOR}/power", json={"on": True})

            assert response.status_code == 200, response.text
            # The stub reports "on" at once; the minimum warm-up hold (§7.4,
            # default 60 s) shows warming, with the hold's countdown.
            body = response.json()
            assert body["state"] == "warming"
            assert body["remaining_s"] == 60.0

            # Power-off inside the hold: refused exactly as in real warm-up (B52).
            refused = await client.post(f"{PROJECTOR}/power", json={"on": False})
            assert refused.status_code == 503, refused.text
            assert code(refused) == "device_unavailable"
            assert detail(refused) == {"state": "warming", "reason": "transitioning"}
            assert stub.power == "1"  # the off never reached the projector

            state = await client.get(f"{PROJECTOR}/state")
            assert state.json()["state"] == "warming"
            assert 0 < state.json()["remaining_s"] <= 60.0


async def test_post_projector_power_during_warm_up_is_device_unavailable(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    async with PJLinkStub(warm_seconds=5.0) as stub:
        stub.set_power_immediately("3")  # already warming when discovered
        async with (
            _running_app(config, db, tokens, limiter, stub) as app,
            make_client(app) as client,
        ):
            await login(client)
            received_before = len(stub.received)

            response = await client.post(f"{PROJECTOR}/power", json={"on": False})

            assert response.status_code == 503
            assert code(response) == "device_unavailable"
            assert detail(response) == {"state": "warming", "reason": "transitioning"}
            assert len(stub.received) == received_before  # B52: nothing was sent


async def test_post_projector_power_with_no_projector_is_not_found(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    async with _running_app(config, db, tokens, limiter, None) as app, make_client(app) as client:
        await login(client)

        response = await client.post(f"{PROJECTOR}/power", json={"on": True})

        assert response.status_code == 404
        assert code(response) == "not_found"
        assert detail(response) == {"reason": "no_projector"}


# -- POST /projector/input -----------------------------------------------------------


async def test_post_projector_input_succeeds(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    async with PJLinkStub(inputs=("11", "31")) as stub:
        stub.set_power_immediately("1")
        stub.current_input = "11"
        async with (
            _running_app(config, db, tokens, limiter, stub) as app,
            make_client(app) as client,
        ):
            await login(client, OPERATOR_PASSWORD)

            response = await client.post(f"{PROJECTOR}/input", json={"input": "31"})

            assert response.status_code == 200, response.text
            assert response.json()["input_ref"] == "31"
            assert stub.current_input == "31"


async def test_post_projector_input_unknown_ref_is_validation_failed(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    async with PJLinkStub(inputs=("11", "31")) as stub:
        stub.set_power_immediately("1")
        async with (
            _running_app(config, db, tokens, limiter, stub) as app,
            make_client(app) as client,
        ):
            await login(client)

            response = await client.post(f"{PROJECTOR}/input", json={"input": "99"})

            assert response.status_code == 422
            assert code(response) == "validation_failed"
            assert detail(response) == {"input": ["unknown"]}


# -- added by the Phase 3 milestone's §22.4 audit ------------------------------------
#
# B52 covers input as well as power, and the contract's "unreachable" case had
# no test for either command.


async def test_post_projector_input_during_warm_up_is_device_unavailable(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    async with PJLinkStub(warm_seconds=5.0) as stub:
        stub.set_power_immediately("3")  # warming when discovered
        async with (
            _running_app(config, db, tokens, limiter, stub) as app,
            make_client(app) as client,
        ):
            await login(client, OPERATOR_PASSWORD)
            received_before = len(stub.received)

            response = await client.post(f"{PROJECTOR}/input", json={"input": "31"})

            assert response.status_code == 503
            assert code(response) == "device_unavailable"
            assert detail(response) == {"state": "warming", "reason": "transitioning"}
            assert len(stub.received) == received_before  # B52: nothing was sent


async def test_post_projector_input_with_no_projector_is_not_found(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    async with _running_app(config, db, tokens, limiter, None) as app, make_client(app) as client:
        await login(client)

        response = await client.post(f"{PROJECTOR}/input", json={"input": "31"})

        assert response.status_code == 404
        assert code(response) == "not_found"
        assert detail(response) == {"reason": "no_projector"}


async def test_post_projector_power_when_the_projector_has_gone_is_device_unavailable(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    stub = PJLinkStub()
    await stub.start()
    stub.set_power_immediately("0")
    try:
        async with (
            _running_app(config, db, tokens, limiter, stub) as app,
            make_client(app) as client,
        ):
            await login(client)
            await stub.stop()  # unplugged; its next probe is up to 5 min away (§7.4)

            response = await client.post(f"{PROJECTOR}/power", json={"on": True})

            assert response.status_code == 503, response.text
            assert code(response) == "device_unavailable"
            assert detail(response)["state"] == "unreachable"
    finally:
        await stub.stop()
