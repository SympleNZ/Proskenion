"""The lighting API: control, configuration and external control (spec §16.5, §22.4).

Every endpoint has a success test and one covering its primary failure mode,
asserting the §16.1 envelope's code, not only the HTTP status.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from starlette.testclient import TestClient

from proskenion.api.app import API_PREFIX, create_app
from proskenion.config import AppSection, Config, DatabaseSection, LoggingSection, ServerSection
from proskenion.core import auth
from proskenion.core.auth import COOKIE_NAME
from proskenion.core.bus import EventBus
from proskenion.core.dmx.desk import DeskInput
from proskenion.core.dmx.fade import SceneRun
from proskenion.core.lighting import LightingService
from proskenion.core.ratelimit import RateLimiter
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import hirer as hirer_crud
from proskenion.db.crud import knx as knx_crud
from proskenion.db.crud import lighting as lighting_crud
from proskenion.db.crud import scenes as scenes_crud
from proskenion.db.crud import users as users_crud
from proskenion.db.migrations import migrate
from tests.unit.api.conftest import ADMIN_PASSWORD, OPERATOR_PASSWORD, TEST_ROUNDS, make_client
from tests.unit.core.dmx.conftest import FakeDevices, FakeKnx

AUTH = f"{API_PREFIX}/auth"
LIGHTING = f"{API_PREFIX}/lighting"


# -- helpers shared by every test in this module -------------------------------------


async def login(client: AsyncClient, password: str = ADMIN_PASSWORD) -> Response:
    return await client.post(f"{AUTH}/login", json={"password": password})


def code(response: Response) -> str:
    body: dict[str, Any] = response.json()
    return str(body["error"]["code"])


def detail(response: Response) -> dict[str, Any]:
    body: dict[str, Any] = response.json()
    return dict(body["error"]["detail"])


async def wait_until(predicate: Callable[[], bool], within: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def make_device(db: Database) -> int:
    device = await devices_crud.create(
        db, category="lighting_output", driver_key="artnet", name="eDMX8", config={}
    )
    return device.id


async def make_dimmer_channel(db: Database, *, name: str = "Front wash", address: int = 1) -> int:
    device_id = await make_device(db)
    channel = await lighting_crud.create_channel(
        db, name=name, type="dmx", profile_id=1, device_id=device_id, address=address
    )
    return channel.id


# -- REST fixtures: a lighting service injected directly, no lifespan (§5.5) ---------


@pytest.fixture
async def bus() -> AsyncIterator[EventBus]:
    events = EventBus()
    await events.start()
    try:
        yield events
    finally:
        await events.stop()


@pytest.fixture
def state(dev_config: Config, bus: EventBus) -> StateStore:
    return StateStore(dev_config, bus)


@pytest.fixture
async def lighting_service(
    state: StateStore, bus: EventBus, db: Database
) -> AsyncIterator[LightingService]:
    service = LightingService(state, bus, db, FakeDevices(), FakeKnx())
    await service.start()
    try:
        yield service
    finally:
        await service.stop()


@pytest.fixture
def app(
    config: Config,
    db: Database,
    tokens: auth.TokenService,
    limiter: RateLimiter,
    bus: EventBus,
    state: StateStore,
    lighting_service: LightingService,
) -> FastAPI:
    # `bus` and `state` must be the same instances the fixture started the
    # lighting service on, or its LightingConfigChanged subscription and the
    # level store it writes are disconnected from what the API reaches.
    return create_app(
        config,
        db=db,
        tokens=tokens,
        limiter=limiter,
        bus=bus,
        state=state,
        lighting=lighting_service,
    )


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with make_client(app) as http:
        yield http


# -- GET /lighting/state -------------------------------------------------------------


async def test_get_state_reports_the_current_look(
    client: AsyncClient, db: Database, lighting_service: LightingService
) -> None:
    channel_id = await make_dimmer_channel(db)
    await lighting_service.reload_config()
    lighting_service.set_level(channel_id, 42.0)
    await login(client, OPERATOR_PASSWORD)

    response = await client.get(f"{LIGHTING}/state")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["channels"][str(channel_id)]["level"] == 42.0
    assert body["master"] == 100.0
    assert body["external_control"] == "off"


async def test_get_state_requires_a_session(client: AsyncClient) -> None:
    response = await client.get(f"{LIGHTING}/state")
    assert response.status_code == 401
    assert code(response) == "unauthenticated"


# -- POST /lighting/channels/{id}/level -----------------------------------------------


async def test_set_channel_level_succeeds_at_the_requested_value(
    client: AsyncClient, db: Database, lighting_service: LightingService
) -> None:
    channel_id = await make_dimmer_channel(db)
    await lighting_service.reload_config()
    await login(client, OPERATOR_PASSWORD)

    response = await client.post(f"{LIGHTING}/channels/{channel_id}/level", json={"level": 55.0})

    assert response.status_code == 200, response.text
    assert response.json() == {"level": 55.0}


async def test_set_channel_level_above_max_value_clamps_and_answers_value_out_of_range(
    client: AsyncClient, db: Database, lighting_service: LightingService
) -> None:
    channel_id = await make_dimmer_channel(db)
    await lighting_service.reload_config()
    await login(client, OPERATOR_PASSWORD)

    response = await client.post(f"{LIGHTING}/channels/{channel_id}/level", json={"level": 150.0})

    assert response.status_code == 422
    assert code(response) == "value_out_of_range"
    assert detail(response)["clamped"] == 100.0
    # B35: the value is accepted at the clamped level, not rejected outright.
    assert lighting_service.composited_level(channel_id) == 100.0


async def test_set_channel_level_locked_by_a_critical_scene_is_permission_denied(
    client: AsyncClient, db: Database, lighting_service: LightingService
) -> None:
    channel_id = await make_dimmer_channel(db)
    await lighting_service.reload_config()
    run = SceneRun(scene_id=1, priority="critical")
    lighting_service.begin_critical_scene(run, [channel_id])
    await login(client, OPERATOR_PASSWORD)

    response = await client.post(f"{LIGHTING}/channels/{channel_id}/level", json={"level": 10.0})

    assert response.status_code == 403
    assert code(response) == "permission_denied"


async def test_set_channel_level_unknown_channel_is_not_found(client: AsyncClient) -> None:
    await login(client, OPERATOR_PASSWORD)
    response = await client.post(f"{LIGHTING}/channels/999/level", json={"level": 10.0})
    assert response.status_code == 404
    assert code(response) == "not_found"


# -- POST /lighting/channels/{id}/colour ----------------------------------------------


async def test_set_channel_colour_succeeds(
    client: AsyncClient, db: Database, lighting_service: LightingService
) -> None:
    device_id = await make_device(db)
    channel = await lighting_crud.create_channel(
        db, name="Cyc", type="dmx", profile_id=2, device_id=device_id, address=1
    )
    await lighting_service.reload_config()
    await login(client, OPERATOR_PASSWORD)

    response = await client.post(
        f"{LIGHTING}/channels/{channel.id}/colour", json={"r": 255, "g": 10, "b": 0}
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"r": 255, "g": 10, "b": 0}


async def test_set_channel_colour_out_of_byte_range_is_validation_failed(
    client: AsyncClient,
) -> None:
    await login(client, OPERATOR_PASSWORD)
    response = await client.post(
        f"{LIGHTING}/channels/1/colour", json={"r": 999, "g": 0, "b": 0}
    )
    assert response.status_code == 422
    assert code(response) == "validation_failed"


# -- POST /lighting/channels/{id}/test [admin] -----------------------------------------


async def test_test_channel_admin_only_succeeds(
    client: AsyncClient, db: Database, lighting_service: LightingService
) -> None:
    channel_id = await make_dimmer_channel(db)
    await lighting_service.reload_config()
    await login(client)

    response = await client.post(f"{LIGHTING}/channels/{channel_id}/test", json={"mode": "full"})

    assert response.status_code == 204, response.text
    assert lighting_service.composited_level(channel_id) == 100.0


async def test_test_channel_refused_for_an_operator(
    client: AsyncClient, db: Database, lighting_service: LightingService
) -> None:
    channel_id = await make_dimmer_channel(db)
    await lighting_service.reload_config()
    await login(client, OPERATOR_PASSWORD)

    response = await client.post(f"{LIGHTING}/channels/{channel_id}/test", json={"mode": "off"})

    assert response.status_code == 403
    assert code(response) == "permission_denied"


# -- POST /lighting/groups/{id}/level --------------------------------------------------


async def test_set_group_level_of_60_reaches_the_service_as_0_6(
    client: AsyncClient, db: Database, lighting_service: LightingService
) -> None:
    group = await lighting_crud.create_group(db, name="Wash")
    await lighting_service.reload_config()
    await login(client, OPERATOR_PASSWORD)

    response = await client.post(f"{LIGHTING}/groups/{group.id}/level", json={"level": 60})

    assert response.status_code == 200, response.text
    assert response.json() == {"level": 60.0}
    assert lighting_service.compositor.config.groups  # configured
    # The store holds the 0–1 scale the service actually applied.
    assert abs(lighting_service._view.group_multiplier(group.id) - 0.6) < 1e-9  # noqa: SLF001


async def test_set_group_level_unknown_group_is_not_found(client: AsyncClient) -> None:
    await login(client, OPERATOR_PASSWORD)
    response = await client.post(f"{LIGHTING}/groups/999/level", json={"level": 50})
    assert response.status_code == 404
    assert code(response) == "not_found"


# -- POST /lighting/master -------------------------------------------------------------


async def test_set_master_succeeds(client: AsyncClient, lighting_service: LightingService) -> None:
    await login(client, OPERATOR_PASSWORD)
    response = await client.post(f"{LIGHTING}/master", json={"level": 40.0})
    assert response.status_code == 200, response.text
    assert response.json() == {"level": 40.0}
    assert lighting_service.master == 40.0


async def test_set_master_rejects_a_non_numeric_level(client: AsyncClient) -> None:
    await login(client, OPERATOR_PASSWORD)
    response = await client.post(f"{LIGHTING}/master", json={"level": "loud"})
    assert response.status_code == 422
    assert code(response) == "validation_failed"


# -- POST /lighting/blackout ------------------------------------------------------------


async def test_blackout_zeroes_every_dmx_channel_through_the_level_store(
    client: AsyncClient, db: Database, lighting_service: LightingService
) -> None:
    channel_id = await make_dimmer_channel(db)
    await lighting_service.reload_config()
    lighting_service.set_level(channel_id, 80.0)
    await login(client, OPERATOR_PASSWORD)

    response = await client.post(f"{LIGHTING}/blackout")

    assert response.status_code == 204, response.text
    assert lighting_service.composited_level(channel_id) == 0.0


async def test_blackout_requires_a_session(client: AsyncClient) -> None:
    response = await client.post(f"{LIGHTING}/blackout")
    assert response.status_code == 401
    assert code(response) == "unauthenticated"


# -- POST /lighting/levels (§16.5 addition) --------------------------------------------


async def test_set_levels_starts_every_fade_together_with_one_shared_fade_time(
    client: AsyncClient, db: Database, lighting_service: LightingService
) -> None:
    a = await make_dimmer_channel(db, name="A", address=1)
    device_id = await lighting_crud.get_channel(db, a)
    assert device_id is not None
    b = await lighting_crud.create_channel(
        db, name="B", type="dmx", profile_id=1, device_id=device_id.device_id, address=2
    )
    await lighting_service.reload_config()
    await login(client, OPERATOR_PASSWORD)

    response = await client.post(
        f"{LIGHTING}/levels", json={"levels": {str(a): 30.0, str(b.id): 70.0}, "fade_ms": 500}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["levels"] == {str(a): 30.0, str(b.id): 70.0}
    # Both fades are in flight together, started by the one request.
    assert lighting_service.fades.is_fading(a)
    assert lighting_service.fades.is_fading(b.id)


async def test_set_levels_with_an_unknown_channel_is_not_found_but_still_applies_the_rest(
    client: AsyncClient, db: Database, lighting_service: LightingService
) -> None:
    a = await make_dimmer_channel(db)
    await lighting_service.reload_config()
    await login(client, OPERATOR_PASSWORD)

    response = await client.post(
        f"{LIGHTING}/levels", json={"levels": {str(a): 30.0, "999": 10.0}, "fade_ms": 0}
    )

    assert response.status_code == 404
    assert code(response) == "not_found"
    assert detail(response)["unknown"] == ["999"]
    assert lighting_service.composited_level(a) == 30.0  # still applied


# -- GET/POST /lighting/external-control ------------------------------------------------


async def test_get_external_control_reports_off_by_default(
    client: AsyncClient, lighting_service: LightingService
) -> None:
    await login(client, OPERATOR_PASSWORD)
    response = await client.get(f"{LIGHTING}/external-control")
    assert response.status_code == 200
    assert response.json() == {"state": "off", "active": False, "last_frame_at": None}


async def test_get_external_control_reports_the_desk_inputs_last_frame_at(
    client: AsyncClient, app: FastAPI, state: StateStore, lighting_service: LightingService
) -> None:
    """§16.5: filled from the desk input, as ISO 8601 with offset — not the
    monotonic float :class:`~proskenion.core.dmx.desk.DeskInput` otherwise
    keeps internally, and never null once a frame has arrived."""
    desk = DeskInput(
        state, lighting_service.set_external_detected, lambda: lighting_service.config
    )
    desk.art_dmx(1, 0, bytes(512))
    app.state.desk_input = desk

    await login(client, OPERATOR_PASSWORD)
    response = await client.get(f"{LIGHTING}/external-control")

    assert response.status_code == 200
    body = response.json()
    assert body["last_frame_at"] == desk.last_frame_at
    assert body["last_frame_at"] is not None
    # ISO 8601 with a Pacific/Auckland offset (§4.9), not a bare float.
    stamp = body["last_frame_at"]
    assert "T" in stamp and ("+" in stamp or "Z" in stamp)


async def test_turning_external_control_manual_on_succeeds(
    client: AsyncClient, lighting_service: LightingService
) -> None:
    await login(client, OPERATOR_PASSWORD)
    response = await client.post(f"{LIGHTING}/external-control", json={"manual": True})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "manual"
    assert lighting_service.external_active is True


async def test_turning_external_control_off_while_frames_are_arriving_is_a_conflict(
    client: AsyncClient, lighting_service: LightingService
) -> None:
    lighting_service.set_external_detected(True)  # the detection input slot (§7.2.7)
    await login(client, OPERATOR_PASSWORD)

    response = await client.post(f"{LIGHTING}/external-control", json={"manual": False})

    assert response.status_code == 409
    assert code(response) == "conflict"
    assert detail(response)["reason"] == "frames_arriving"
    assert lighting_service.external_active is True  # detection still holds it active


# -- POST /lighting/snapshot [admin] -----------------------------------------------------


async def test_save_snapshot_admin_captures_the_current_look(
    client: AsyncClient, db: Database, lighting_service: LightingService
) -> None:
    channel_id = await make_dimmer_channel(db)
    await lighting_service.reload_config()
    lighting_service.set_level(channel_id, 65.0)
    await login(client)

    response = await client.post(f"{LIGHTING}/snapshot")

    assert response.status_code == 200, response.text
    assert response.json()["snapshot"][str(channel_id)]["level"] == 65.0


async def test_save_snapshot_refused_for_an_operator(client: AsyncClient) -> None:
    await login(client, OPERATOR_PASSWORD)
    response = await client.post(f"{LIGHTING}/snapshot")
    assert response.status_code == 403
    assert code(response) == "permission_denied"


# -- configuration: lighting channels ----------------------------------------------------


async def test_list_channels_matches_the_contract_field_for_field(
    client: AsyncClient, db: Database
) -> None:
    device_id = await make_device(db)
    channel = await lighting_crud.create_channel(
        db, name="Cyc", type="dmx", profile_id=2, device_id=device_id, address=1, bar_id=1
    )
    group = await lighting_crud.create_group(db, name="Wash")
    await lighting_crud.set_group_members(db, group.id, [channel.id])
    await login(client, OPERATOR_PASSWORD)

    response = await client.get(f"{LIGHTING}/channels")

    assert response.status_code == 200, response.text
    (row,) = [c for c in response.json()["channels"] if c["id"] == channel.id]
    assert row == {
        "id": channel.id,
        "name": "Cyc",
        "type": "dmx",
        "profile_id": 2,
        "min_value": 0.0,
        "max_value": 100.0,
        "has_colour": True,
        "group_ids": [group.id],
        "bar_id": 1,
        "position": 0.5,
        "visible_staff": True,
        "device_id": device_id,
        "universe": 1,
        "address": 1,
        "knx_command_address_id": None,
        "knx_status_address_id": None,
        "knx_switch_address_id": None,
        "fade_mode": "hardware",
        "colour_r": None,
        "colour_g": None,
        "colour_b": None,
        "colour_w": None,
        "notes": None,
        "updated_at": channel.updated_at,
        "conflicts": [],
    }


async def test_get_channel_unknown_id_is_not_found(client: AsyncClient) -> None:
    await login(client, OPERATOR_PASSWORD)
    response = await client.get(f"{LIGHTING}/channels/999")
    assert response.status_code == 404
    assert code(response) == "not_found"


async def test_create_channel_resolves_has_colour_to_the_seeded_dimmer_profile(
    client: AsyncClient, db: Database, lighting_service: LightingService
) -> None:
    device_id = await make_device(db)
    await login(client)

    response = await client.post(
        f"{LIGHTING}/channels",
        json={
            "name": "Front wash",
            "type": "dmx",
            "min_value": 0.0,
            "max_value": 100.0,
            "has_colour": False,
            "group_ids": [],
            "bar_id": None,
            "position": 0.5,
            "visible_staff": True,
            "device_id": device_id,
            "universe": 1,
            "address": 3,
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["has_colour"] is False
    stored = await lighting_crud.get_channel(db, body["id"])
    assert stored is not None and stored.profile_id == 1  # §15.2's seeded dimmer profile


async def test_create_channel_with_a_profile_id_wins_over_has_colour(
    client: AsyncClient, db: Database
) -> None:
    device_id = await make_device(db)
    await login(client)

    response = await client.post(
        f"{LIGHTING}/channels",
        json={
            "name": "Cyc",
            "type": "dmx",
            "profile_id": 3,  # §15.2's seeded RGBW — deliberately not what has_colour would pick
            "has_colour": False,
            "device_id": device_id,
            "address": 1,
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["profile_id"] == 3
    assert body["has_colour"] is True  # RGBW does have a colour role


async def test_create_channel_missing_a_required_dmx_field_is_validation_failed(
    client: AsyncClient, db: Database
) -> None:
    device_id = await make_device(db)
    await login(client)

    response = await client.post(
        f"{LIGHTING}/channels",
        json={"name": "Cyc", "type": "dmx", "profile_id": 2, "device_id": device_id},
    )

    assert response.status_code == 422
    assert code(response) == "validation_failed"
    assert "address" in detail(response)


async def test_create_channel_with_no_matching_profile_for_has_colour_asks_to_choose_one(
    client: AsyncClient, db: Database
) -> None:
    device_id = await make_device(db)
    # Nothing shaped like has_colour=True is left once every seeded profile
    # with a colour role is gone — the seeded dimmer stays, unreferenced.
    for profile in await lighting_crud.list_fixture_profiles(db):
        if profile.name != "Single-channel dimmer":
            await lighting_crud.delete_fixture_profile(db, profile.id)
    await login(client)

    response = await client.post(
        f"{LIGHTING}/channels",
        json={
            "name": "Odd fixture",
            "type": "dmx",
            "has_colour": True,
            "device_id": device_id,
            "address": 1,
        },
    )

    assert response.status_code == 422
    assert code(response) == "validation_failed"
    assert "profile_id" in detail(response)
    # Never creates one to fall back on (§21.12's minimal form is a shorthand,
    # not a profile editor) — only the one profile left over survives.
    profiles = await lighting_crud.list_fixture_profiles(db)
    assert len(profiles) == 1


async def test_create_knx_dimmer_channel_with_a_command_address_succeeds(
    client: AsyncClient, db: Database
) -> None:
    address = await knx_crud.create_address(
        db, group_address="1/1/10", name="House command", dpt="5.001", direction="outgoing"
    )
    await login(client)

    response = await client.post(
        f"{LIGHTING}/channels",
        json={
            "name": "House centre",
            "type": "knx_dimmer",
            "knx_command_address_id": address.id,
            "fade_mode": "software",
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["knx_command_address_id"] == address.id
    assert body["profile_id"] is None
    assert body["address"] is None
    assert body["fade_mode"] == "software"
    assert body["has_colour"] is False


async def test_create_knx_dimmer_channel_missing_a_command_address_is_validation_failed(
    client: AsyncClient,
) -> None:
    await login(client)
    response = await client.post(
        f"{LIGHTING}/channels", json={"name": "House centre", "type": "knx_dimmer"}
    )
    assert response.status_code == 422
    assert code(response) == "validation_failed"
    assert "knx_command_address_id" in detail(response)


async def test_create_channel_with_an_unknown_fade_mode_is_validation_failed(
    client: AsyncClient, db: Database
) -> None:
    device_id = await make_device(db)
    await login(client)

    response = await client.post(
        f"{LIGHTING}/channels",
        json={
            "name": "Cyc",
            "type": "dmx",
            "profile_id": 1,
            "device_id": device_id,
            "address": 1,
            "fade_mode": "instant",
        },
    )

    assert response.status_code == 422
    assert code(response) == "validation_failed"
    assert "fade_mode" in detail(response)


async def test_create_channel_referencing_an_unknown_knx_address_is_validation_failed(
    client: AsyncClient,
) -> None:
    await login(client)
    response = await client.post(
        f"{LIGHTING}/channels",
        json={"name": "House", "type": "knx_dimmer", "knx_command_address_id": 999},
    )
    assert response.status_code == 422
    assert code(response) == "validation_failed"
    assert "knx_command_address_id" in detail(response)


async def test_update_channel_changes_type_from_dmx_to_knx_dimmer(
    client: AsyncClient, db: Database
) -> None:
    channel_id = await make_dimmer_channel(db)
    channel = await lighting_crud.get_channel(db, channel_id)
    assert channel is not None
    address = await knx_crud.create_address(
        db, group_address="1/1/11", name="House command", dpt="5.001", direction="outgoing"
    )
    await login(client)

    response = await client.put(
        f"{LIGHTING}/channels/{channel_id}",
        json={
            "type": "knx_dimmer",
            "knx_command_address_id": address.id,
            "profile_id": None,
            "device_id": None,
            "address": None,
        },
        headers={"If-Unmodified-Since-Version": channel.updated_at},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["type"] == "knx_dimmer"
    assert body["knx_command_address_id"] == address.id
    assert body["profile_id"] is None
    assert body["address"] is None


async def test_update_channel_type_change_without_nulling_the_old_shape_is_validation_failed(
    client: AsyncClient, db: Database
) -> None:
    channel_id = await make_dimmer_channel(db)
    channel = await lighting_crud.get_channel(db, channel_id)
    assert channel is not None
    address = await knx_crud.create_address(
        db, group_address="1/1/12", name="House command", dpt="5.001", direction="outgoing"
    )
    await login(client)

    # profile_id and address are left as they were — still set from the dmx shape.
    response = await client.put(
        f"{LIGHTING}/channels/{channel_id}",
        json={"type": "knx_dimmer", "knx_command_address_id": address.id},
        headers={"If-Unmodified-Since-Version": channel.updated_at},
    )

    assert response.status_code == 422
    assert code(response) == "validation_failed"
    fields = detail(response)
    assert "profile_id" in fields
    assert "address" in fields


async def test_update_channel_moves_it_to_a_bar_and_position(
    client: AsyncClient, db: Database
) -> None:
    channel_id = await make_dimmer_channel(db)
    channel = await lighting_crud.get_channel(db, channel_id)
    assert channel is not None
    await login(client)

    response = await client.put(
        f"{LIGHTING}/channels/{channel_id}",
        json={"bar_id": 1, "position": 0.25},
        headers={"If-Unmodified-Since-Version": channel.updated_at},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["bar_id"] == 1
    assert body["position"] == 0.25


async def test_update_channel_with_a_stale_version_is_conflict_with_current(
    client: AsyncClient, db: Database
) -> None:
    channel_id = await make_dimmer_channel(db)
    await login(client)

    response = await client.put(
        f"{LIGHTING}/channels/{channel_id}",
        json={"position": 0.9},
        headers={"If-Unmodified-Since-Version": "2000-01-01T00:00:00.000000+12:00"},
    )

    assert response.status_code == 409
    assert code(response) == "conflict"
    assert detail(response)["current"]["id"] == channel_id


async def test_create_channel_with_an_overlapping_address_saves_and_reports_the_conflict(
    client: AsyncClient, db: Database
) -> None:
    """§9.1, §21.18: an overlap warns and never refuses the save."""
    device_id = await make_device(db)
    existing = await lighting_crud.create_channel(
        db, name="A", type="dmx", profile_id=2, device_id=device_id, address=1  # slots 1-3
    )
    await login(client)

    response = await client.post(
        f"{LIGHTING}/channels",
        json={
            "name": "B",
            "type": "dmx",
            "profile_id": 1,
            "device_id": device_id,
            "address": 2,  # overlaps A's slot 2
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    (conflict,) = body["conflicts"]
    assert conflict["channel_ids"] == sorted([existing.id, body["id"]])
    assert conflict["slots"] == [2]
    # Saved anyway — never refused.
    assert await lighting_crud.get_channel(db, body["id"]) is not None


async def test_delete_channel_succeeds(client: AsyncClient, db: Database) -> None:
    channel_id = await make_dimmer_channel(db)
    await login(client)
    response = await client.delete(f"{LIGHTING}/channels/{channel_id}")
    assert response.status_code == 204, response.text
    assert await lighting_crud.get_channel(db, channel_id) is None


async def test_delete_channel_named_by_a_scene_snapshot_is_in_use(
    client: AsyncClient, db: Database
) -> None:
    channel_id = await make_dimmer_channel(db)
    scene = await scenes_crud.create_scene(db, name="Save look 1")
    await scenes_crud.create_action(
        db,
        scene_id=scene.id,
        sort_order=0,
        domain="dmx",
        dmx_snapshot={str(channel_id): {"level": 50.0}},
    )
    await login(client)

    response = await client.delete(f"{LIGHTING}/channels/{channel_id}")

    assert response.status_code == 409
    assert code(response) == "in_use"
    references = detail(response)["references"]
    assert any(r["entity"] == "scenes" and r["id"] == scene.id for r in references)


# -- configuration: lighting groups --------------------------------------------------


async def test_create_group_succeeds(client: AsyncClient, db: Database) -> None:
    channel_id = await make_dimmer_channel(db)
    await login(client)
    response = await client.post(
        f"{LIGHTING}/groups", json={"name": "Wash", "channel_ids": [channel_id]}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["channel_ids"] == [channel_id]


async def test_create_group_missing_name_is_validation_failed(client: AsyncClient) -> None:
    await login(client)
    response = await client.post(f"{LIGHTING}/groups", json={"name": "", "channel_ids": []})
    assert response.status_code == 422
    assert code(response) == "validation_failed"


async def test_delete_group_named_by_a_rule_is_in_use(client: AsyncClient, db: Database) -> None:
    group = await lighting_crud.create_group(db, name="Wash")

    async def rule_reference() -> None:
        async with db.write() as conn:
            await conn.execute(
                "INSERT INTO rules (name, enabled, trigger_type, match_type, action_type, "
                "lighting_group_id, on_level, off_level, created_at, updated_at) "
                "VALUES (?, 1, 'knx', 'any', 'lighting_group', ?, 100.0, 0.0, ?, ?)",
                (
                    "Bank 1",
                    group.id,
                    "2026-01-01T00:00:00+13:00",
                    "2026-01-01T00:00:00+13:00",
                ),
            )

    await rule_reference()
    await login(client)

    response = await client.delete(f"{LIGHTING}/groups/{group.id}")

    assert response.status_code == 409
    assert code(response) == "in_use"


# -- configuration: lighting bars -----------------------------------------------------


async def test_list_bars_includes_the_seeded_proscenium(client: AsyncClient) -> None:
    await login(client, OPERATOR_PASSWORD)
    response = await client.get(f"{LIGHTING}/bars")
    assert response.status_code == 200, response.text
    assert any(b["name"] == "Proscenium" for b in response.json()["bars"])


async def test_get_bar_unknown_id_is_not_found(client: AsyncClient) -> None:
    await login(client, OPERATOR_PASSWORD)
    response = await client.get(f"{LIGHTING}/bars/999")
    assert response.status_code == 404
    assert code(response) == "not_found"


async def test_create_bar_succeeds(client: AsyncClient) -> None:
    await login(client)
    response = await client.post(f"{LIGHTING}/bars", json={"name": "Bar 3", "sort_order": 3})
    assert response.status_code == 201, response.text
    assert response.json()["name"] == "Bar 3"


async def test_create_bar_refused_for_an_operator(client: AsyncClient) -> None:
    await login(client, OPERATOR_PASSWORD)
    response = await client.post(f"{LIGHTING}/bars", json={"name": "Bar 3"})
    assert response.status_code == 403
    assert code(response) == "permission_denied"


# -- configuration: colour presets ----------------------------------------------------


async def test_create_preset_succeeds(client: AsyncClient) -> None:
    await login(client)
    response = await client.post(
        f"{LIGHTING}/presets", json={"name": "Warm", "r": 255, "g": 180, "b": 100}
    )
    assert response.status_code == 201, response.text
    assert response.json()["name"] == "Warm"


async def test_get_preset_unknown_id_is_not_found(client: AsyncClient) -> None:
    await login(client)
    response = await client.get(f"{LIGHTING}/presets/999")
    assert response.status_code == 404
    assert code(response) == "not_found"


# -- configuration: fixture profiles --------------------------------------------------


async def test_list_profiles_includes_the_seeded_rgb_profile(client: AsyncClient) -> None:
    await login(client)
    response = await client.get(f"{LIGHTING}/profiles")
    assert response.status_code == 200, response.text
    assert any(p["name"] == "RGB" for p in response.json()["profiles"])


async def test_create_profile_with_a_bad_shape_is_validation_failed(client: AsyncClient) -> None:
    await login(client)
    response = await client.post(
        f"{LIGHTING}/profiles",
        json={
            "name": "Broken",
            "channel_count": 2,
            "channels": [{"offset": 0, "role": "dimmer"}],  # only one entry, needs two
        },
    )
    assert response.status_code == 422
    assert code(response) == "validation_failed"


# -- GET /lighting/patch/conflicts ----------------------------------------------------


async def test_patch_conflicts_reports_overlapping_dmx_addresses(
    client: AsyncClient, db: Database
) -> None:
    device_id = await make_device(db)
    a = await lighting_crud.create_channel(
        db, name="A", type="dmx", profile_id=2, device_id=device_id, address=1  # 3 slots: 1-3
    )
    b = await lighting_crud.create_channel(
        db, name="B", type="dmx", profile_id=1, device_id=device_id, address=2  # 1 slot: 2
    )
    await login(client, OPERATOR_PASSWORD)

    response = await client.get(f"{LIGHTING}/patch/conflicts")

    assert response.status_code == 200, response.text
    (conflict,) = response.json()["conflicts"]
    assert conflict["channel_ids"] == sorted([a.id, b.id])
    assert conflict["device_id"] == device_id
    assert conflict["slots"] == [2]


async def test_patch_conflicts_requires_a_session(client: AsyncClient) -> None:
    response = await client.get(f"{LIGHTING}/patch/conflicts")
    assert response.status_code == 401
    assert code(response) == "unauthenticated"


# -- a configuration write reloads the running service --------------------------------


async def test_a_configuration_write_emits_lighting_config_changed_and_the_service_reloads(
    client: AsyncClient, db: Database, lighting_service: LightingService
) -> None:
    device_id = await make_device(db)
    await login(client)

    response = await client.post(
        f"{LIGHTING}/channels",
        json={
            "name": "Front wash",
            "type": "dmx",
            "min_value": 0.0,
            "max_value": 100.0,
            "has_colour": False,
            "group_ids": [],
            "bar_id": None,
            "position": 0.5,
            "visible_staff": True,
            "device_id": device_id,
            "universe": 1,
            "address": 7,
        },
    )
    assert response.status_code == 201, response.text
    new_id = response.json()["id"]

    await wait_until(lambda: new_id in lighting_service.compositor.dmx_channel_ids)


# -- the WebSocket write path (§16.8, §21.2) ------------------------------------------


@pytest.fixture
def ws_config(tmp_path: Path) -> Config:
    return Config(
        database=DatabaseSection(path=tmp_path / "auditorium.db"),
        logging=LoggingSection(path=tmp_path / "logs"),
        server=ServerSection(hostname="av.school.nz"),
        app=AppSection(state_dir=tmp_path / "appliance", data_dir=tmp_path / "data"),
    )


async def _seed_ws(path: Path) -> int:
    """Migrate the file-backed database and patch one DMX channel, returning its id."""
    db = Database()
    await db.open(path)
    try:
        await migrate(db)
        await users_crud.set_password_hash(
            db, "admin", auth.hash_secret(ADMIN_PASSWORD, rounds=TEST_ROUNDS)
        )
        await users_crud.set_password_hash(
            db, "operator", auth.hash_secret(OPERATOR_PASSWORD, rounds=TEST_ROUNDS)
        )
        await hirer_crud.set_pin_hash(
            db, auth.hash_secret("246810", rounds=TEST_ROUNDS), updated_by=None
        )
        return await make_dimmer_channel(db)
    finally:
        await db.close()


def sign_in(client: TestClient, password: str = OPERATOR_PASSWORD) -> str:
    response = client.post(f"{API_PREFIX}/auth/login", json={"password": password})
    assert response.status_code == 200, response.text
    return f"{COOKIE_NAME}={response.cookies[COOKIE_NAME]}"


def run(client: TestClient, work: Callable[[], Coroutine[Any, Any, None]]) -> None:
    assert client.portal is not None
    client.portal.call(work)


def receive_until(session: Any, kind: str, limit: int = 20) -> dict[str, Any]:
    for _ in range(limit):
        message: dict[str, Any] = session.receive_json()
        if message["type"] == kind:
            return message
    raise AssertionError(f"no {kind!r} message arrived")


async def test_ws_set_on_an_unknown_channel_is_nacked_not_found(ws_config: Config) -> None:
    await _seed_ws(ws_config.database.path)
    app = create_app(ws_config)
    with TestClient(app, base_url="https://av.school.nz") as client:
        cookie = sign_in(client)
        with client.websocket_connect(
            "/ws?v=1", headers={"Origin": "https://av.school.nz", "Cookie": cookie}
        ) as ws:
            ws.send_json(
                {"type": "set", "domain": "lighting", "id": 999, "value": 10.0, "token": 1}
            )
            nack = receive_until(ws, "nack")
            assert nack == {"type": "nack", "token": 1, "reason": "not_found"}


async def test_ws_set_on_a_channel_locked_by_a_critical_scene_is_nacked_permission_denied(
    ws_config: Config,
) -> None:
    channel_id = await _seed_ws(ws_config.database.path)
    app = create_app(ws_config)
    with TestClient(app, base_url="https://av.school.nz") as client:

        async def lock() -> None:
            service: LightingService = app.state.lighting
            run_ = SceneRun(scene_id=1, priority="critical")
            service.begin_critical_scene(run_, [channel_id])

        run(client, lock)
        cookie = sign_in(client)
        with client.websocket_connect(
            "/ws?v=1", headers={"Origin": "https://av.school.nz", "Cookie": cookie}
        ) as ws:
            ws.send_json(
                {"type": "set", "domain": "lighting", "id": channel_id, "value": 10.0, "token": 7}
            )
            nack = receive_until(ws, "nack")
            assert nack["type"] == "nack"
            assert nack["token"] == 7
            assert nack["reason"] == "permission_denied"


async def test_ws_set_lighting_group_divides_by_100_before_the_service_sees_it(
    ws_config: Config,
) -> None:
    await _seed_ws(ws_config.database.path)
    app = create_app(ws_config)
    with TestClient(app, base_url="https://av.school.nz") as client:
        group_id: int = 0

        async def make_group() -> None:
            nonlocal group_id
            service: LightingService = app.state.lighting
            db: Database = app.state.db
            group = await lighting_crud.create_group(db, name="Wash")
            group_id = group.id
            await service.reload_config()

        run(client, make_group)
        cookie = sign_in(client)
        with client.websocket_connect(
            "/ws?v=1", headers={"Origin": "https://av.school.nz", "Cookie": cookie}
        ) as ws:
            ws.send_json(
                {
                    "type": "set",
                    "domain": "lighting_group",
                    "id": group_id,
                    "value": 60.0,
                    "token": 3,
                }
            )
            assert receive_until(ws, "ack") == {"type": "ack", "token": 3}

        async def read() -> float:
            service: LightingService = app.state.lighting
            return service._view.group_multiplier(group_id)  # noqa: SLF001

        result: list[float] = []
        run(client, lambda: _capture(read, result))
        assert abs(result[0] - 0.6) < 1e-9


async def _capture(work: Callable[[], Awaitable[float]], out: list[float]) -> None:
    out.append(await work())


# -- §22.4 audit: failure tests added ----------------------------------------------
#
# Every lighting endpoint's success case and primary failure mode, grouped by
# endpoint in route order. Each test stands alone, as every other test in this
# module does.


async def test_test_channel_unknown_channel_is_not_found(client: AsyncClient) -> None:
    await login(client)
    response = await client.post(f"{LIGHTING}/channels/999/test", json={"mode": "full"})
    assert response.status_code == 404
    assert code(response) == "not_found"


async def test_get_external_control_requires_a_session(client: AsyncClient) -> None:
    response = await client.get(f"{LIGHTING}/external-control")
    assert response.status_code == 401
    assert code(response) == "unauthenticated"


async def test_list_channels_requires_a_session(client: AsyncClient) -> None:
    response = await client.get(f"{LIGHTING}/channels")
    assert response.status_code == 401
    assert code(response) == "unauthenticated"


async def test_get_channel_succeeds(client: AsyncClient, db: Database) -> None:
    channel_id = await make_dimmer_channel(db, name="Front wash")
    await login(client, OPERATOR_PASSWORD)

    response = await client.get(f"{LIGHTING}/channels/{channel_id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == channel_id
    assert body["name"] == "Front wash"
    assert body["type"] == "dmx"


async def test_channel_references_lists_the_scene_that_names_it(
    client: AsyncClient, db: Database
) -> None:
    channel_id = await make_dimmer_channel(db)
    scene = await scenes_crud.create_scene(db, name="Save look 1")
    await scenes_crud.create_action(
        db,
        scene_id=scene.id,
        sort_order=0,
        domain="dmx",
        dmx_snapshot={str(channel_id): {"level": 50.0}},
    )
    await login(client)

    response = await client.get(f"{LIGHTING}/channels/{channel_id}/references")

    assert response.status_code == 200, response.text
    references = response.json()["references"]
    assert any(r["entity"] == "scenes" and r["id"] == scene.id for r in references)


async def test_channel_references_unknown_channel_is_not_found(client: AsyncClient) -> None:
    await login(client)
    response = await client.get(f"{LIGHTING}/channels/999/references")
    assert response.status_code == 404
    assert code(response) == "not_found"


async def test_list_groups_includes_a_created_group(client: AsyncClient, db: Database) -> None:
    group = await lighting_crud.create_group(db, name="Wash")
    await login(client, OPERATOR_PASSWORD)

    response = await client.get(f"{LIGHTING}/groups")

    assert response.status_code == 200, response.text
    assert any(g["id"] == group.id and g["name"] == "Wash" for g in response.json()["groups"])


async def test_list_groups_requires_a_session(client: AsyncClient) -> None:
    response = await client.get(f"{LIGHTING}/groups")
    assert response.status_code == 401
    assert code(response) == "unauthenticated"


async def test_get_group_succeeds(client: AsyncClient, db: Database) -> None:
    group = await lighting_crud.create_group(db, name="Wash")
    await login(client, OPERATOR_PASSWORD)

    response = await client.get(f"{LIGHTING}/groups/{group.id}")

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Wash"


async def test_get_group_unknown_id_is_not_found(client: AsyncClient) -> None:
    await login(client, OPERATOR_PASSWORD)
    response = await client.get(f"{LIGHTING}/groups/999")
    assert response.status_code == 404
    assert code(response) == "not_found"


async def test_update_group_succeeds(client: AsyncClient, db: Database) -> None:
    group = await lighting_crud.create_group(db, name="Wash")
    await login(client)

    response = await client.put(
        f"{LIGHTING}/groups/{group.id}",
        json={"name": "Wash renamed"},
        headers={"If-Unmodified-Since-Version": group.updated_at},
    )

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Wash renamed"


async def test_update_group_unknown_id_is_not_found(client: AsyncClient) -> None:
    await login(client)
    response = await client.put(
        f"{LIGHTING}/groups/999",
        json={"name": "Nobody"},
        headers={"If-Unmodified-Since-Version": "2000-01-01T00:00:00.000000+12:00"},
    )
    assert response.status_code == 404
    assert code(response) == "not_found"


async def test_delete_group_succeeds(client: AsyncClient, db: Database) -> None:
    group = await lighting_crud.create_group(db, name="Wash")
    await login(client)

    response = await client.delete(f"{LIGHTING}/groups/{group.id}")

    assert response.status_code == 204, response.text
    assert await lighting_crud.get_group(db, group.id) is None


async def test_list_bars_requires_a_session(client: AsyncClient) -> None:
    response = await client.get(f"{LIGHTING}/bars")
    assert response.status_code == 401
    assert code(response) == "unauthenticated"


async def test_get_bar_succeeds(client: AsyncClient, db: Database) -> None:
    bar = await lighting_crud.create_bar(db, name="Bar 4")
    await login(client, OPERATOR_PASSWORD)

    response = await client.get(f"{LIGHTING}/bars/{bar.id}")

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Bar 4"


async def test_create_bar_missing_name_is_validation_failed(client: AsyncClient) -> None:
    await login(client)
    response = await client.post(f"{LIGHTING}/bars", json={"name": ""})
    assert response.status_code == 422
    assert code(response) == "validation_failed"


async def test_update_bar_succeeds(client: AsyncClient, db: Database) -> None:
    bar = await lighting_crud.create_bar(db, name="Bar 5")
    await login(client)

    response = await client.put(
        f"{LIGHTING}/bars/{bar.id}",
        json={"name": "Bar 5 renamed"},
        headers={"If-Unmodified-Since-Version": bar.updated_at},
    )

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Bar 5 renamed"


async def test_update_bar_unknown_id_is_not_found(client: AsyncClient) -> None:
    await login(client)
    response = await client.put(
        f"{LIGHTING}/bars/999",
        json={"name": "Nobody"},
        headers={"If-Unmodified-Since-Version": "2000-01-01T00:00:00.000000+12:00"},
    )
    assert response.status_code == 404
    assert code(response) == "not_found"


async def test_delete_bar_succeeds(client: AsyncClient, db: Database) -> None:
    bar = await lighting_crud.create_bar(db, name="Bar 6")
    await login(client)

    response = await client.delete(f"{LIGHTING}/bars/{bar.id}")

    assert response.status_code == 204, response.text
    assert await lighting_crud.get_bar(db, bar.id) is None


async def test_delete_bar_unknown_id_is_not_found(client: AsyncClient) -> None:
    await login(client)
    response = await client.delete(f"{LIGHTING}/bars/999")
    assert response.status_code == 404
    assert code(response) == "not_found"


async def test_list_presets_succeeds(client: AsyncClient, db: Database) -> None:
    preset = await lighting_crud.create_preset(db, name="Warm", r=255, g=180, b=100)
    await login(client)

    response = await client.get(f"{LIGHTING}/presets")

    assert response.status_code == 200, response.text
    assert any(p["id"] == preset.id and p["name"] == "Warm" for p in response.json()["presets"])


async def test_list_presets_refused_for_an_operator(client: AsyncClient) -> None:
    await login(client, OPERATOR_PASSWORD)
    response = await client.get(f"{LIGHTING}/presets")
    assert response.status_code == 403
    assert code(response) == "permission_denied"


async def test_get_preset_succeeds(client: AsyncClient, db: Database) -> None:
    preset = await lighting_crud.create_preset(db, name="Cool", r=100, g=150, b=255)
    await login(client)

    response = await client.get(f"{LIGHTING}/presets/{preset.id}")

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Cool"


async def test_create_preset_missing_name_is_validation_failed(client: AsyncClient) -> None:
    await login(client)
    response = await client.post(f"{LIGHTING}/presets", json={"name": "", "r": 255, "g": 0, "b": 0})
    assert response.status_code == 422
    assert code(response) == "validation_failed"


async def test_update_preset_succeeds(client: AsyncClient, db: Database) -> None:
    preset = await lighting_crud.create_preset(db, name="Warm", r=255, g=180, b=100)
    await login(client)

    response = await client.put(
        f"{LIGHTING}/presets/{preset.id}",
        json={"name": "Warm 2"},
        headers={"If-Unmodified-Since-Version": preset.updated_at},
    )

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Warm 2"


async def test_update_preset_unknown_id_is_not_found(client: AsyncClient) -> None:
    await login(client)
    response = await client.put(
        f"{LIGHTING}/presets/999",
        json={"name": "Nobody"},
        headers={"If-Unmodified-Since-Version": "2000-01-01T00:00:00.000000+12:00"},
    )
    assert response.status_code == 404
    assert code(response) == "not_found"


async def test_delete_preset_succeeds(client: AsyncClient, db: Database) -> None:
    preset = await lighting_crud.create_preset(db, name="Gone", r=1, g=2, b=3)
    await login(client)

    response = await client.delete(f"{LIGHTING}/presets/{preset.id}")

    assert response.status_code == 204, response.text
    assert await lighting_crud.get_preset(db, preset.id) is None


async def test_delete_preset_unknown_id_is_not_found(client: AsyncClient) -> None:
    await login(client)
    response = await client.delete(f"{LIGHTING}/presets/999")
    assert response.status_code == 404
    assert code(response) == "not_found"


async def test_list_profiles_refused_for_an_operator(client: AsyncClient) -> None:
    await login(client, OPERATOR_PASSWORD)
    response = await client.get(f"{LIGHTING}/profiles")
    assert response.status_code == 403
    assert code(response) == "permission_denied"


async def test_get_profile_succeeds(client: AsyncClient) -> None:
    await login(client)
    response = await client.get(f"{LIGHTING}/profiles/2")  # §15.2's seeded RGB profile
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "RGB"


async def test_get_profile_unknown_id_is_not_found(client: AsyncClient) -> None:
    await login(client)
    response = await client.get(f"{LIGHTING}/profiles/999")
    assert response.status_code == 404
    assert code(response) == "not_found"


async def test_create_profile_succeeds(client: AsyncClient) -> None:
    await login(client)
    response = await client.post(
        f"{LIGHTING}/profiles",
        json={
            "name": "Uplighter",
            "channel_count": 1,
            "channels": [{"offset": 0, "role": "dimmer"}],
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["name"] == "Uplighter"


async def test_update_profile_succeeds(client: AsyncClient, db: Database) -> None:
    profile = await lighting_crud.create_fixture_profile(
        db, name="Uplighter", channel_count=1, channels=[{"offset": 0, "role": "dimmer"}]
    )
    await login(client)

    response = await client.put(
        f"{LIGHTING}/profiles/{profile.id}",
        json={"name": "Uplighter mk2"},
        headers={"If-Unmodified-Since-Version": profile.updated_at},
    )

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Uplighter mk2"


async def test_update_profile_unknown_id_is_not_found(client: AsyncClient) -> None:
    await login(client)
    response = await client.put(
        f"{LIGHTING}/profiles/999",
        json={"name": "Nobody"},
        headers={"If-Unmodified-Since-Version": "2000-01-01T00:00:00.000000+12:00"},
    )
    assert response.status_code == 404
    assert code(response) == "not_found"


async def test_delete_profile_referenced_by_a_channel_is_in_use(
    client: AsyncClient, db: Database
) -> None:
    await make_dimmer_channel(db)  # patched with the seeded dimmer profile, id 1
    await login(client)

    response = await client.delete(f"{LIGHTING}/profiles/1")

    assert response.status_code == 409
    assert code(response) == "in_use"
    references = detail(response)["references"]
    assert any(r["entity"] == "lighting_channels" for r in references)


async def test_delete_profile_succeeds(client: AsyncClient, db: Database) -> None:
    profile = await lighting_crud.create_fixture_profile(
        db, name="Unused", channel_count=1, channels=[{"offset": 0, "role": "dimmer"}]
    )
    await login(client)

    response = await client.delete(f"{LIGHTING}/profiles/{profile.id}")

    assert response.status_code == 204, response.text
    assert await lighting_crud.get_fixture_profile(db, profile.id) is None
