"""The HDMI matrix API: state, source selection and configuration CRUD
(spec §16.5, §16.8, §7.5, `docs/plans/phase-3-contracts.md`).

Every endpoint has a success test and one covering its primary failure mode,
asserting the §16.1 envelope's code, not only the HTTP status (§22.4).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response

from proskenion.api.app import API_PREFIX, create_app
from proskenion.config import Config
from proskenion.core.auth import TokenService
from proskenion.core.bus import EventBus
from proskenion.core.devices import DeviceManager
from proskenion.core.drivers.lkv422 import MatrixError
from proskenion.core.ratelimit import RateLimiter
from proskenion.core.state import StateStore
from proskenion.core.video import VideoService
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import scenes as scenes_crud
from proskenion.db.crud import video as video_crud
from tests.unit.api.conftest import ADMIN_PASSWORD, OPERATOR_PASSWORD, make_client

HDMI = f"{API_PREFIX}/hdmi"
AUTH = f"{API_PREFIX}/auth"
LOOPBACK: dict[str, Any] = {"transport": {"type": "loopback"}, "driver": {}}


# -- fixtures -------------------------------------------------------------------


@dataclass
class Services:
    bus: EventBus
    state: StateStore
    manager: DeviceManager
    video: VideoService


@pytest.fixture
async def services(db: Database, config: Config) -> AsyncIterator[Services]:
    bus = EventBus()
    await bus.start()
    state = StateStore(config, bus)
    manager = DeviceManager(
        db, state, bus, config, connect_timeout=1.0, probe_timeout=0.5, stop_timeout=2.0
    )
    await manager.start()
    video = VideoService(state, bus, db, manager)
    await video.start()
    try:
        yield Services(bus, state, manager, video)
    finally:
        await video.stop()
        await manager.stop()
        await bus.stop()


@pytest.fixture
def app(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter, services: Services
) -> FastAPI:
    return create_app(
        config,
        db=db,
        tokens=tokens,
        limiter=limiter,
        bus=services.bus,
        state=services.state,
        devices_manager=services.manager,
        video=services.video,
    )


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with make_client(app) as http:
        yield http


async def login(client: AsyncClient, password: str = ADMIN_PASSWORD) -> Response:
    return await client.post(f"{AUTH}/login", json={"password": password})


def code(response: Response) -> str:
    body: dict[str, Any] = response.json()
    return str(body["error"]["code"])


def detail(response: Response) -> dict[str, Any]:
    body: dict[str, Any] = response.json()
    return dict(body["error"]["detail"])


async def make_matrix_device(
    db: Database, services: Services, *, driver_key: str = "stub"
) -> int:
    """A ``video_matrix`` device, connected — the service picks it up and reads
    its routing (§12.2), so a test using this can rely on ``state.hdmi``."""
    device = await devices_crud.create(
        db, category="video_matrix", driver_key=driver_key, name="Matrix", config=LOOPBACK
    )
    await services.manager.reload(device.id)
    await services.manager.wait_for_connection(device.id)
    await _wait_until(lambda: services.video.device_id == device.id)
    return device.id


async def _wait_until(predicate: Callable[[], bool], within: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def make_destination(
    db: Database,
    services: Services,
    *,
    output_refs: tuple[str, ...] = ("1", "2"),
    input_refs: tuple[str, ...] = ("1", "2", "3", "4"),
) -> tuple[int, list[int], list[int]]:
    """A connected matrix, with inputs/outputs for every ref and one destination
    covering every output. Returns ``(destination_id, input_ids, output_ids)``."""
    device_id = await make_matrix_device(db, services)
    inputs = [
        (await video_crud.create_input(db, device_id=device_id, driver_ref=r, name=f"In {r}")).id
        for r in input_refs
    ]
    outputs = [
        (await video_crud.create_output(db, device_id=device_id, driver_ref=r, name=f"Out {r}")).id
        for r in output_refs
    ]
    destination = await video_crud.create_destination(db, device_id=device_id, name="The room")
    await video_crud.set_destination_outputs(db, destination.id, outputs)
    return destination.id, inputs, outputs


# -- GET /hdmi/state --------------------------------------------------------------


async def test_get_state_reports_the_configured_matrix(
    client: AsyncClient, db: Database, services: Services
) -> None:
    destination_id, inputs, outputs = await make_destination(db, services)
    await login(client, OPERATOR_PASSWORD)

    response = await client.get(f"{HDMI}/state")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["device_id"] == services.video.device_id
    assert body["supports_atomic_route"] is True
    (destination,) = body["destinations"]
    assert destination["id"] == destination_id
    assert destination["default_input_id"] is None
    assert [o["id"] for o in destination["outputs"]] == outputs
    assert {i["id"] for i in body["inputs"]} == set(inputs)


async def test_get_state_with_no_matrix_configured_is_empty(client: AsyncClient) -> None:
    await login(client, OPERATOR_PASSWORD)
    response = await client.get(f"{HDMI}/state")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "device_id": None,
        "supports_atomic_route": False,
        "destinations": [],
        "inputs": [],
    }


async def test_get_state_requires_a_session(client: AsyncClient) -> None:
    response = await client.get(f"{HDMI}/state")
    assert response.status_code == 401
    assert code(response) == "unauthenticated"


# -- POST /hdmi/destinations/{id}/source -------------------------------------------


async def test_set_destination_source_succeeds(
    client: AsyncClient, db: Database, services: Services
) -> None:
    destination_id, inputs, _outputs = await make_destination(db, services)
    await login(client, OPERATOR_PASSWORD)

    response = await client.post(
        f"{HDMI}/destinations/{destination_id}/source", json={"input_id": inputs[2]}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == destination_id
    assert body["input_id"] == inputs[2]
    assert body["diverged"] is False


async def test_set_destination_source_unknown_destination_is_not_found(
    client: AsyncClient,
) -> None:
    await login(client, OPERATOR_PASSWORD)
    response = await client.post(f"{HDMI}/destinations/999/source", json={"input_id": 1})
    assert response.status_code == 404
    assert code(response) == "not_found"


async def test_set_destination_source_of_another_device_is_validation_failed(
    client: AsyncClient, db: Database, services: Services
) -> None:
    destination_id, _inputs, _outputs = await make_destination(db, services)
    other_device = await devices_crud.create(
        db, category="video_matrix", driver_key="stub", name="Other", config=LOOPBACK
    )
    foreign_input = await video_crud.create_input(
        db, device_id=other_device.id, driver_ref="1", name="Elsewhere"
    )
    await login(client, OPERATOR_PASSWORD)

    response = await client.post(
        f"{HDMI}/destinations/{destination_id}/source", json={"input_id": foreign_input.id}
    )

    assert response.status_code == 422
    assert code(response) == "validation_failed"
    assert detail(response)["input_id"] == ["unknown"]


# -- configuration: /hdmi/inputs (admin only, §16.1) -------------------------------


async def test_create_matrix_input_succeeds(
    client: AsyncClient, db: Database, services: Services
) -> None:
    device_id = await make_matrix_device(db, services)
    await login(client)

    response = await client.post(
        f"{HDMI}/inputs",
        json={"device_id": device_id, "driver_ref": "1", "name": "Side of stage"},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["driver_ref"] == "1"
    assert await video_crud.get_input(db, body["id"]) is not None


async def test_create_matrix_input_with_an_unknown_device_is_validation_failed(
    client: AsyncClient,
) -> None:
    await login(client)
    response = await client.post(
        f"{HDMI}/inputs", json={"device_id": 999, "driver_ref": "1", "name": "X"}
    )
    assert response.status_code == 422
    assert code(response) == "validation_failed"


async def test_list_matrix_inputs_requires_a_session(client: AsyncClient) -> None:
    response = await client.get(f"{HDMI}/inputs")
    assert response.status_code == 401
    assert code(response) == "unauthenticated"


async def test_list_matrix_inputs_includes_a_created_row(
    client: AsyncClient, db: Database, services: Services
) -> None:
    device_id = await make_matrix_device(db, services)
    await video_crud.create_input(db, device_id=device_id, driver_ref="1", name="In 1")
    await login(client)  # admin only (§16.1, the contract)
    response = await client.get(f"{HDMI}/inputs")
    assert response.status_code == 200, response.text
    assert len(response.json()["inputs"]) == 1


async def test_get_matrix_input_unknown_id_is_not_found(client: AsyncClient) -> None:
    await login(client)  # admin only (§16.1, the contract)
    response = await client.get(f"{HDMI}/inputs/999")
    assert response.status_code == 404
    assert code(response) == "not_found"


async def test_get_matrix_input_succeeds(
    client: AsyncClient, db: Database, services: Services
) -> None:
    device_id = await make_matrix_device(db, services)
    row = await video_crud.create_input(db, device_id=device_id, driver_ref="1", name="In 1")
    await login(client)  # admin only (§16.1, the contract)
    response = await client.get(f"{HDMI}/inputs/{row.id}")
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "In 1"


async def test_update_matrix_input_succeeds(
    client: AsyncClient, db: Database, services: Services
) -> None:
    device_id = await make_matrix_device(db, services)
    row = await video_crud.create_input(db, device_id=device_id, driver_ref="1", name="In 1")
    await login(client)

    response = await client.put(
        f"{HDMI}/inputs/{row.id}",
        json={"name": "Side of stage"},
        headers={"If-Unmodified-Since-Version": row.updated_at},
    )

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Side of stage"


async def test_update_matrix_input_with_a_stale_version_is_conflict(
    client: AsyncClient, db: Database, services: Services
) -> None:
    device_id = await make_matrix_device(db, services)
    row = await video_crud.create_input(db, device_id=device_id, driver_ref="1", name="In 1")
    await login(client)

    response = await client.put(
        f"{HDMI}/inputs/{row.id}",
        json={"name": "Renamed"},
        headers={"If-Unmodified-Since-Version": "2000-01-01T00:00:00.000000+12:00"},
    )

    assert response.status_code == 409
    assert code(response) == "conflict"
    assert detail(response)["current"]["id"] == row.id


async def test_delete_matrix_input_succeeds(
    client: AsyncClient, db: Database, services: Services
) -> None:
    device_id = await make_matrix_device(db, services)
    row = await video_crud.create_input(db, device_id=device_id, driver_ref="1", name="In 1")
    await login(client)
    response = await client.delete(f"{HDMI}/inputs/{row.id}")
    assert response.status_code == 204, response.text
    assert await video_crud.get_input(db, row.id) is None


async def test_delete_matrix_input_named_by_a_scene_is_in_use(
    client: AsyncClient, db: Database, services: Services
) -> None:
    device_id = await make_matrix_device(db, services)
    row = await video_crud.create_input(db, device_id=device_id, driver_ref="1", name="In 1")
    scene = await scenes_crud.create_scene(db, name="Interval")
    await scenes_crud.create_action(
        db, scene_id=scene.id, sort_order=0, domain="hdmi_source", hdmi_input_id=row.id
    )
    await login(client)

    response = await client.delete(f"{HDMI}/inputs/{row.id}")

    assert response.status_code == 409
    assert code(response) == "in_use"
    references = detail(response)["references"]
    assert any(r["entity"] == "scene_actions" for r in references)


# -- configuration: /hdmi/outputs (admin only, §16.1) ------------------------------


async def test_create_matrix_output_succeeds(
    client: AsyncClient, db: Database, services: Services
) -> None:
    device_id = await make_matrix_device(db, services)
    await login(client)

    response = await client.post(
        f"{HDMI}/outputs", json={"device_id": device_id, "driver_ref": "1", "name": "Projector"}
    )

    assert response.status_code == 201, response.text
    assert await video_crud.get_output(db, response.json()["id"]) is not None


async def test_create_matrix_output_with_an_unknown_device_is_validation_failed(
    client: AsyncClient,
) -> None:
    await login(client)
    response = await client.post(
        f"{HDMI}/outputs", json={"device_id": 999, "driver_ref": "1", "name": "X"}
    )
    assert response.status_code == 422
    assert code(response) == "validation_failed"


async def test_get_matrix_output_unknown_id_is_not_found(client: AsyncClient) -> None:
    await login(client)  # admin only (§16.1, the contract)
    response = await client.get(f"{HDMI}/outputs/999")
    assert response.status_code == 404
    assert code(response) == "not_found"


async def test_get_matrix_output_succeeds(
    client: AsyncClient, db: Database, services: Services
) -> None:
    device_id = await make_matrix_device(db, services)
    row = await video_crud.create_output(db, device_id=device_id, driver_ref="1", name="Out 1")
    await login(client)  # admin only (§16.1, the contract)
    response = await client.get(f"{HDMI}/outputs/{row.id}")
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Out 1"


async def test_update_matrix_output_succeeds(
    client: AsyncClient, db: Database, services: Services
) -> None:
    device_id = await make_matrix_device(db, services)
    row = await video_crud.create_output(db, device_id=device_id, driver_ref="1", name="Out 1")
    await login(client)

    response = await client.put(
        f"{HDMI}/outputs/{row.id}",
        json={"name": "Back of house"},
        headers={"If-Unmodified-Since-Version": row.updated_at},
    )

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Back of house"


async def test_update_matrix_output_with_a_stale_version_is_conflict(
    client: AsyncClient, db: Database, services: Services
) -> None:
    device_id = await make_matrix_device(db, services)
    row = await video_crud.create_output(db, device_id=device_id, driver_ref="1", name="Out 1")
    await login(client)

    response = await client.put(
        f"{HDMI}/outputs/{row.id}",
        json={"name": "Renamed"},
        headers={"If-Unmodified-Since-Version": "2000-01-01T00:00:00.000000+12:00"},
    )

    assert response.status_code == 409
    assert code(response) == "conflict"


async def test_delete_matrix_output_succeeds(
    client: AsyncClient, db: Database, services: Services
) -> None:
    device_id = await make_matrix_device(db, services)
    row = await video_crud.create_output(db, device_id=device_id, driver_ref="1", name="Out 1")
    await login(client)
    response = await client.delete(f"{HDMI}/outputs/{row.id}")
    assert response.status_code == 204, response.text
    assert await video_crud.get_output(db, row.id) is None


async def test_delete_matrix_output_unknown_id_is_not_found(client: AsyncClient) -> None:
    await login(client)
    response = await client.delete(f"{HDMI}/outputs/999")
    assert response.status_code == 404
    assert code(response) == "not_found"


async def test_list_matrix_outputs_requires_a_session(client: AsyncClient) -> None:
    response = await client.get(f"{HDMI}/outputs")
    assert response.status_code == 401
    assert code(response) == "unauthenticated"


async def test_list_matrix_outputs_includes_a_created_row(
    client: AsyncClient, db: Database, services: Services
) -> None:
    device_id = await make_matrix_device(db, services)
    await video_crud.create_output(db, device_id=device_id, driver_ref="1", name="Out 1")
    await login(client)  # admin only (§16.1, the contract)
    response = await client.get(f"{HDMI}/outputs")
    assert response.status_code == 200, response.text
    assert len(response.json()["outputs"]) == 1


# -- configuration: /hdmi/destinations (admin only, §16.1, §15.10) ----------------


async def test_create_video_destination_with_outputs_succeeds(
    client: AsyncClient, db: Database, services: Services
) -> None:
    device_id = await make_matrix_device(db, services)
    output = await video_crud.create_output(db, device_id=device_id, driver_ref="1", name="Out 1")
    await login(client)

    response = await client.post(
        f"{HDMI}/destinations",
        json={"device_id": device_id, "name": "The room", "output_ids": [output.id]},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["output_ids"] == [output.id]


async def test_create_video_destination_with_an_unknown_output_is_validation_failed(
    client: AsyncClient, db: Database, services: Services
) -> None:
    device_id = await make_matrix_device(db, services)
    await login(client)

    response = await client.post(
        f"{HDMI}/destinations",
        json={"device_id": device_id, "name": "The room", "output_ids": [999]},
    )

    assert response.status_code == 422
    assert code(response) == "validation_failed"
    assert detail(response)["output_ids"] == ["unknown"]


async def test_list_video_destinations_requires_a_session(client: AsyncClient) -> None:
    response = await client.get(f"{HDMI}/destinations")
    assert response.status_code == 401
    assert code(response) == "unauthenticated"


async def test_list_video_destinations_includes_a_created_row(
    client: AsyncClient, db: Database, services: Services
) -> None:
    destination_id, _inputs, outputs = await make_destination(db, services)
    await login(client)  # admin only (§16.1, the contract)
    response = await client.get(f"{HDMI}/destinations")
    assert response.status_code == 200, response.text
    (destination,) = response.json()["destinations"]
    assert destination["id"] == destination_id
    assert destination["output_ids"] == outputs


async def test_get_video_destination_unknown_id_is_not_found(client: AsyncClient) -> None:
    await login(client)  # admin only (§16.1, the contract)
    response = await client.get(f"{HDMI}/destinations/999")
    assert response.status_code == 404
    assert code(response) == "not_found"


async def test_get_video_destination_succeeds(
    client: AsyncClient, db: Database, services: Services
) -> None:
    destination_id, _inputs, outputs = await make_destination(db, services)
    await login(client)  # admin only (§16.1, the contract)
    response = await client.get(f"{HDMI}/destinations/{destination_id}")
    assert response.status_code == 200, response.text
    assert response.json()["output_ids"] == outputs


async def test_update_video_destination_replaces_its_output_list(
    client: AsyncClient, db: Database, services: Services
) -> None:
    destination_id, _inputs, outputs = await make_destination(db, services)
    row = await video_crud.get_destination(db, destination_id)
    assert row is not None
    await login(client)

    response = await client.put(
        f"{HDMI}/destinations/{destination_id}",
        json={"output_ids": [outputs[0]]},
        headers={"If-Unmodified-Since-Version": row.updated_at},
    )

    assert response.status_code == 200, response.text
    assert response.json()["output_ids"] == [outputs[0]]


async def test_update_video_destination_with_a_stale_version_is_conflict(
    client: AsyncClient, db: Database, services: Services
) -> None:
    destination_id, _inputs, _outputs = await make_destination(db, services)
    await login(client)

    response = await client.put(
        f"{HDMI}/destinations/{destination_id}",
        json={"name": "Renamed"},
        headers={"If-Unmodified-Since-Version": "2000-01-01T00:00:00.000000+12:00"},
    )

    assert response.status_code == 409
    assert code(response) == "conflict"


async def test_delete_video_destination_succeeds(
    client: AsyncClient, db: Database, services: Services
) -> None:
    destination_id, _inputs, _outputs = await make_destination(db, services)
    await login(client)
    response = await client.delete(f"{HDMI}/destinations/{destination_id}")
    assert response.status_code == 204, response.text
    assert await video_crud.get_destination(db, destination_id) is None


async def test_delete_video_destination_named_by_a_scene_is_in_use(
    client: AsyncClient, db: Database, services: Services
) -> None:
    destination_id, _inputs, _outputs = await make_destination(db, services)
    scene = await scenes_crud.create_scene(db, name="Interval")
    await scenes_crud.create_action(
        db, scene_id=scene.id, sort_order=0, domain="hdmi_source", hdmi_destination=destination_id
    )
    await login(client)

    response = await client.delete(f"{HDMI}/destinations/{destination_id}")

    assert response.status_code == 409
    assert code(response) == "in_use"


# -- added by the Phase 3 milestone's §22.4 audit ------------------------------------
#
# Source selection's primary failure is the device's, not the request's: the
# matrix is offline, or the switch was sent and PAXXR did not confirm it
# (docs/plans/phase-3-contracts.md). Neither had a test.


async def test_set_destination_source_on_a_matrix_not_connected_is_device_unavailable(
    client: AsyncClient, db: Database, services: Services
) -> None:
    # A matrix row the device manager has not started: configured, not connected.
    device = await devices_crud.create(
        db, category="video_matrix", driver_key="stub", name="Offline", config=LOOPBACK
    )
    source = await video_crud.create_input(db, device_id=device.id, driver_ref="1", name="In 1")
    output = await video_crud.create_output(db, device_id=device.id, driver_ref="1", name="Out 1")
    destination = await video_crud.create_destination(db, device_id=device.id, name="The room")
    await video_crud.set_destination_outputs(db, destination.id, [output.id])
    await login(client, OPERATOR_PASSWORD)

    response = await client.post(
        f"{HDMI}/destinations/{destination.id}/source", json={"input_id": source.id}
    )

    assert response.status_code == 503
    assert code(response) == "device_unavailable"


async def test_set_destination_source_not_confirmed_is_device_unavailable(
    client: AsyncClient, db: Database, services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination_id, inputs, _outputs = await make_destination(db, services)
    driver = services.manager.running_driver(services.video.device_id or 0)
    assert driver is not None

    async def unconfirmed(outputs: list[str], input: str) -> None:  # noqa: A002
        raise MatrixError(f"route of output(s) {', '.join(outputs)} to input {input} not confirmed")

    # The matrix takes the command and PAXXR shows the old routing (§7.5).
    monkeypatch.setattr(driver, "route", unconfirmed)
    await login(client, OPERATOR_PASSWORD)

    response = await client.post(
        f"{HDMI}/destinations/{destination_id}/source", json={"input_id": inputs[1]}
    )

    assert response.status_code == 503
    assert code(response) == "device_unavailable"
    assert detail(response) == {"reason": "route_not_confirmed"}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "inputs"),
        ("put", "inputs/1"),
        ("delete", "inputs/1"),
        ("post", "outputs"),
        ("put", "outputs/1"),
        ("delete", "outputs/1"),
        ("post", "destinations"),
        ("put", "destinations/1"),
        ("delete", "destinations/1"),
    ],
)
async def test_configuration_writes_are_refused_for_an_operator(
    client: AsyncClient, method: str, path: str
) -> None:
    """Configuration is admin only (§16.1, the contract): the tier, before anything else."""
    await login(client, OPERATOR_PASSWORD)
    body = {"device_id": 1, "driver_ref": "1", "name": "x"} if method != "delete" else None
    response = await client.request(method.upper(), f"{HDMI}/{path}", json=body)
    assert response.status_code == 403
    assert code(response) == "permission_denied"


@pytest.mark.parametrize(
    "path",
    [
        "inputs",
        "inputs/1",
        "outputs",
        "outputs/1",
        "destinations",
        "destinations/1",
    ],
)
async def test_configuration_reads_are_refused_for_an_operator(
    client: AsyncClient, path: str
) -> None:
    """Configuration reads are admin only too (the contract): ``GET /hdmi/state``
    is the one read the operator Video view needs, and stays staff."""
    await login(client, OPERATOR_PASSWORD)
    response = await client.get(f"{HDMI}/{path}")
    assert response.status_code == 403
    assert code(response) == "permission_denied"
