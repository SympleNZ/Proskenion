"""The scenes endpoints (spec §16.5 *Scenes*, §16.1, §8.11–§8.16, §5.5).

Every endpoint has a success test and one covering its primary failure mode,
asserting the §16.1 envelope code rather than only the status (§22.4). The
scene engine behind them is the real one, over the real lighting service and
an in-memory database; KNX, capabilities and the broadcaster are recording
fakes from :mod:`tests.unit.scene.conftest`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response

from proskenion.api.app import API_PREFIX, create_app
from proskenion.api.scenes import VERSION_HEADER
from proskenion.config import Config
from proskenion.core.auth import TokenService
from proskenion.core.bus import EventBus
from proskenion.core.lighting import LightingService
from proskenion.core.ratelimit import RateLimiter
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import mixer as mixer_crud
from proskenion.db.crud import rules as rules_crud
from proskenion.db.crud import scenes as scenes_crud
from proskenion.db.crud import video as video_crud
from proskenion.db.crud.base import AUCKLAND
from proskenion.scene.av_handlers import (
    HdmiSourceHandler,
    ProjectorInputHandler,
    ProjectorPowerHandler,
)
from proskenion.scene.engine import SceneEngine
from tests.unit.api.conftest import ADMIN_PASSWORD, OPERATOR_PASSWORD, make_client
from tests.unit.core.dmx.conftest import FakeDevices, FakeKnx
from tests.unit.scene.conftest import (
    FakeBroadcaster,
    FakeCapabilities,
    FakeKnxWriter,
    RecordingHandler,
    Rig,
    build_rig,
    dmx,
    knx,
    make_scene,
    mixer_capabilities,
)

SCENES = f"{API_PREFIX}/scenes"
AUTH = f"{API_PREFIX}/auth"


@dataclass
class Services:
    bus: EventBus
    state: StateStore
    lighting: LightingService
    knx: FakeKnxWriter
    devices: FakeCapabilities
    broadcaster: FakeBroadcaster
    rig: Rig


@pytest.fixture
async def services(db: Database, config: Config) -> AsyncIterator[Services]:
    bus = EventBus()
    await bus.start()
    state = StateStore(config, bus)
    rig = await build_rig(db)
    lighting = LightingService(state, bus, db, FakeDevices(rig.output), FakeKnx())
    await lighting.start()
    try:
        yield Services(
            bus, state, lighting, FakeKnxWriter(), FakeCapabilities(), FakeBroadcaster(), rig
        )
    finally:
        await lighting.stop()
        await bus.stop()


@pytest.fixture
async def app(
    config: Config,
    db: Database,
    tokens: TokenService,
    limiter: RateLimiter,
    services: Services,
) -> AsyncIterator[FastAPI]:
    application = create_app(
        config, db=db, tokens=tokens, limiter=limiter, bus=services.bus, state=services.state
    )
    engine = SceneEngine(
        db,
        services.state,
        lighting=services.lighting,
        knx=services.knx,
        devices=services.devices,
        broadcaster=services.broadcaster,
    )
    application.state.scene_engine = engine
    application.state.devices = services.devices
    try:
        yield application
    finally:
        await engine.stop()


@pytest.fixture
def engine(app: FastAPI) -> SceneEngine:
    scene_engine: SceneEngine = app.state.scene_engine
    return scene_engine


@pytest.fixture
async def admin(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with make_client(app) as http:
        assert (await http.post(f"{AUTH}/login", json={"password": ADMIN_PASSWORD})).is_success
        yield http


@pytest.fixture
async def operator(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with make_client(app) as http:
        assert (await http.post(f"{AUTH}/login", json={"password": OPERATOR_PASSWORD})).is_success
        yield http


def code(response: Response) -> str:
    body: dict[str, Any] = response.json()
    return str(body["error"]["code"])


def detail(response: Response) -> dict[str, Any]:
    body: dict[str, Any] = response.json()
    return dict(body["error"]["detail"])


def fields(response: Response) -> set[str]:
    return {str(f["field"]) for f in detail(response)["fields"]}


async def settle(engine: SceneEngine) -> None:
    for handle in engine.running():
        await handle.result()


# -- GET /scenes/domains ---------------------------------------------------------------------


async def test_domains_reports_knx_and_dmx_available_with_no_device_needed(
    admin: AsyncClient,
) -> None:
    """The engine always registers ``knx`` and ``dmx`` (§8.12); neither needs a device."""
    response = await admin.get(f"{SCENES}/domains")
    assert response.status_code == 200
    by_domain = {row["domain"]: row for row in response.json()["domains"]}
    assert by_domain["knx"] == {"domain": "knx", "available": True, "reason": None}
    assert by_domain["dmx"] == {"domain": "dmx", "available": True, "reason": None}


async def test_domains_reports_an_unregistered_domain_disabled_with_its_reason(
    admin: AsyncClient,
) -> None:
    """No handler is registered here for the other six; the editor must disable them (§21.16)."""
    response = await admin.get(f"{SCENES}/domains")
    by_domain = {row["domain"]: row for row in response.json()["domains"]}
    assert by_domain["mixer_recall"] == {
        "domain": "mixer_recall",
        "available": False,
        "reason": "Mixer actions arrive with the mixer driver",
    }
    assert by_domain["projector_power"]["available"] is False
    assert by_domain["hdmi_source"]["reason"] == "HDMI actions arrive with the HDMI matrix driver"


async def test_domains_reports_a_registered_domain_disabled_when_no_device_is_configured(
    admin: AsyncClient, engine: SceneEngine
) -> None:
    engine.handlers.register("mixer_recall", RecordingHandler())
    response = await admin.get(f"{SCENES}/domains")
    row = next(r for r in response.json()["domains"] if r["domain"] == "mixer_recall")
    assert row == {"domain": "mixer_recall", "available": False, "reason": "No mixer is configured"}


async def test_domains_reports_available_once_a_handler_and_a_device_both_exist(
    admin: AsyncClient, engine: SceneEngine, db: Database
) -> None:
    engine.handlers.register("mixer_recall", RecordingHandler())
    await devices_crud.create(db, category="mixer", driver_key="cq", name="CQ-20", config={})

    response = await admin.get(f"{SCENES}/domains")
    row = next(r for r in response.json()["domains"] if r["domain"] == "mixer_recall")
    assert row == {"domain": "mixer_recall", "available": True, "reason": None}


async def test_domains_reports_the_three_phase_3_domains_once_registered_with_a_device(
    admin: AsyncClient, engine: SceneEngine, db: Database
) -> None:
    """The real handlers, registered the way ``proskenion/api/app.py``
    registers them at startup — the generic logic in ``list_domains`` needs
    no change (§21.16)."""
    engine.handlers.register("projector_power", ProjectorPowerHandler(None))
    engine.handlers.register("projector_input", ProjectorInputHandler(None))
    engine.handlers.register("hdmi_source", HdmiSourceHandler(None, db))
    still_unavailable = await admin.get(f"{SCENES}/domains")
    by_domain = {row["domain"]: row for row in still_unavailable.json()["domains"]}
    assert by_domain["projector_power"] == {
        "domain": "projector_power",
        "available": False,
        "reason": "No projector is configured",
    }
    assert by_domain["hdmi_source"]["reason"] == "No HDMI matrix is configured"

    await devices_crud.create(
        db, category="projector", driver_key="pjlink", name="EZ570", config={}
    )
    await devices_crud.create(
        db, category="video_matrix", driver_key="lkv422", name="Matrix", config={}
    )
    now_available = await admin.get(f"{SCENES}/domains")
    by_domain = {row["domain"]: row for row in now_available.json()["domains"]}
    assert by_domain["projector_power"] == {
        "domain": "projector_power",
        "available": True,
        "reason": None,
    }
    assert by_domain["projector_input"] == {
        "domain": "projector_input",
        "available": True,
        "reason": None,
    }
    assert by_domain["hdmi_source"] == {"domain": "hdmi_source", "available": True, "reason": None}


async def test_domains_is_admin_only(operator: AsyncClient) -> None:
    response = await operator.get(f"{SCENES}/domains")
    assert response.status_code == 403
    assert code(response) == "permission_denied"


# -- GET /scenes ----------------------------------------------------------------------------


async def test_list_scenes_shows_operators_only_their_scenes_with_last_run(
    admin: AsyncClient, operator: AsyncClient, db: Database, engine: SceneEngine
) -> None:
    shown = await make_scene(db, name="Assembly")
    hidden = await make_scene(db, name="All Off", priority="critical", visible_operator=False)
    await (await engine.run(shown.id, triggered_by="api:admin")).result()

    everything = await admin.get(SCENES)
    assert everything.status_code == 200
    assert [s["name"] for s in everything.json()["scenes"]] == ["Assembly", "All Off"]
    first = everything.json()["scenes"][0]
    assert first["last_run"]["result"] == "success"
    assert first["running"] is False

    mine = await operator.get(SCENES)
    assert [s["id"] for s in mine.json()["scenes"]] == [shown.id]
    assert hidden.id not in [s["id"] for s in mine.json()["scenes"]]


async def test_list_scenes_needs_a_session(app: FastAPI) -> None:
    async with make_client(app) as anonymous:
        response = await anonymous.get(SCENES)
    assert response.status_code == 401
    assert code(response) == "unauthenticated"


# -- POST /scenes ---------------------------------------------------------------------------


async def test_create_scene(admin: AsyncClient) -> None:
    response = await admin.post(
        SCENES, json={"name": "Restore Venue Default", "protected": True, "priority": "normal"}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["protected"] is True
    assert body["enabled"] is True


async def test_create_scene_refuses_an_unknown_priority(
    admin: AsyncClient, operator: AsyncClient
) -> None:
    response = await admin.post(SCENES, json={"name": "Loud", "priority": "urgent"})
    assert response.status_code == 422
    assert code(response) == "validation_failed"
    refused = await operator.post(SCENES, json={"name": "Mine"})
    assert refused.status_code == 403
    assert code(refused) == "permission_denied"


# -- GET /scenes/{id} -----------------------------------------------------------------------


async def test_get_scene_includes_its_actions(admin: AsyncClient, db: Database, rig: Rig) -> None:
    scene = await make_scene(db, dmx({rig.front: 50.0}), knx(rig.lamp, "1", delay_ms=2000))
    response = await admin.get(f"{SCENES}/{scene.id}")
    assert response.status_code == 200
    assert [a["domain"] for a in response.json()["actions"]] == ["dmx", "knx"]


async def test_get_scene_not_found(admin: AsyncClient) -> None:
    response = await admin.get(f"{SCENES}/404")
    assert response.status_code == 404
    assert code(response) == "not_found"


# -- PUT /scenes/{id} -----------------------------------------------------------------------


async def test_update_scene_with_its_version(admin: AsyncClient, db: Database) -> None:
    scene = await make_scene(db)
    response = await admin.put(
        f"{SCENES}/{scene.id}",
        json={"name": "Performance End", "priority": "critical"},
        headers={VERSION_HEADER: scene.updated_at},
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Performance End"
    assert response.json()["updated_at"] != scene.updated_at


async def test_update_scene_with_a_stale_version_is_a_conflict(
    admin: AsyncClient, db: Database
) -> None:
    scene = await make_scene(db)
    await scenes_crud.update_scene(db, scene.id, scene.updated_at, name="Changed elsewhere")
    response = await admin.put(
        f"{SCENES}/{scene.id}", json={"name": "Mine"}, headers={VERSION_HEADER: scene.updated_at}
    )
    assert response.status_code == 409
    assert code(response) == "conflict"
    assert detail(response)["current"]["name"] == "Changed elsewhere"
    missing = await admin.put(f"{SCENES}/{scene.id}", json={"name": "Mine"})
    assert code(missing) == "validation_failed"


# -- DELETE /scenes/{id} --------------------------------------------------------------------


async def test_delete_scene(admin: AsyncClient, db: Database, rig: Rig) -> None:
    scene = await make_scene(db, dmx({rig.front: 10.0}))
    response = await admin.delete(f"{SCENES}/{scene.id}")
    assert response.status_code == 204
    assert await scenes_crud.get_scene(db, scene.id) is None


async def test_a_protected_scene_cannot_be_deleted(admin: AsyncClient, db: Database) -> None:
    scene = await make_scene(db, name="Restore Venue Default", protected=True)
    response = await admin.delete(f"{SCENES}/{scene.id}")
    assert response.status_code == 403
    assert code(response) == "permission_denied"
    assert detail(response)["reason"] == "protected"
    assert await scenes_crud.get_scene(db, scene.id) is not None


async def test_a_scene_a_rule_runs_cannot_be_deleted(
    admin: AsyncClient, db: Database, rig: Rig
) -> None:
    scene = await make_scene(db, name="All Off", priority="critical")
    rule = await rules_crud.create_rule(
        db,
        name="Alarm armed",
        trigger_type="knx",
        knx_address_id=rig.armed,
        match_value="1",
        action_type="run_scene",
        scene_id=scene.id,
    )
    response = await admin.delete(f"{SCENES}/{scene.id}")
    assert response.status_code == 409
    assert code(response) == "in_use"
    assert detail(response)["references"] == [{"entity": "rules", "id": rule.id, "name": rule.name}]


# -- GET /scenes/{id}/references --------------------------------------------------------------


async def test_scene_references(admin: AsyncClient, db: Database) -> None:
    scene = await make_scene(db)
    response = await admin.get(f"{SCENES}/{scene.id}/references")
    assert response.status_code == 200
    assert response.json() == {"references": []}


async def test_scene_references_not_found(admin: AsyncClient) -> None:
    response = await admin.get(f"{SCENES}/77/references")
    assert code(response) == "not_found"


# -- /scenes/{id}/actions ------------------------------------------------------------------------


async def test_list_actions(admin: AsyncClient, db: Database, rig: Rig) -> None:
    scene = await make_scene(db, dmx({rig.front: 10.0}))
    response = await admin.get(f"{SCENES}/{scene.id}/actions")
    assert response.status_code == 200
    assert response.json()["actions"][0]["dmx_snapshot"] == {str(rig.front): {"level": 10.0}}


async def test_list_actions_not_found(admin: AsyncClient) -> None:
    response = await admin.get(f"{SCENES}/5/actions")
    assert code(response) == "not_found"


async def test_create_actions(admin: AsyncClient, db: Database, rig: Rig) -> None:
    scene = await make_scene(db)
    snapshot = await admin.post(
        f"{SCENES}/{scene.id}/actions",
        json={
            "domain": "dmx",
            "dmx_snapshot": {str(rig.front): {"level": 78.5}},
            "dmx_fade_ms": 2000,
        },
    )
    assert snapshot.status_code == 201
    write = await admin.post(
        f"{SCENES}/{scene.id}/actions",
        json={"domain": "knx", "delay_ms": 2000, "knx_address_id": rig.lamp, "knx_value": "1"},
    )
    assert write.status_code == 201
    later = await admin.post(
        f"{SCENES}/{scene.id}/actions",
        json={"domain": "projector_input", "delay_ms": 8000, "projector_input": "hdmi1"},
    )
    assert later.status_code == 201  # no handler yet: saved, and skipped at execution
    assert [a.domain for a in await scenes_crud.list_actions(db, scene.id)] == [
        "dmx",
        "knx",
        "projector_input",
    ]


async def test_create_action_refuses_an_invalid_action(
    admin: AsyncClient, db: Database, rig: Rig
) -> None:
    scene = await make_scene(db)
    unknown = await admin.post(f"{SCENES}/{scene.id}/actions", json={"domain": "pjlink_power"})
    assert unknown.status_code == 422
    assert code(unknown) == "validation_failed"
    assert fields(unknown) == {"domain"}
    missing = await admin.post(
        f"{SCENES}/{scene.id}/actions",
        json={"domain": "dmx", "dmx_snapshot": {"999": {"level": 50}}},
    )
    assert code(missing) == "validation_failed"
    assert fields(missing) == {"dmx_snapshot.999"}
    assert await scenes_crud.list_actions(db, scene.id) == []


async def test_create_action_hdmi_source_with_no_input_id_is_restore_venue_default(
    admin: AsyncClient, db: Database
) -> None:
    """§13.5: a null ``hdmi_input_id`` is never a missing-field error."""
    device = await devices_crud.create(
        db, category="video_matrix", driver_key="lkv422", name="Matrix", config={}
    )
    destination = await video_crud.create_destination(db, device_id=device.id, name="The room")
    scene = await make_scene(db)
    response = await admin.post(
        f"{SCENES}/{scene.id}/actions",
        json={"domain": "hdmi_source", "hdmi_destination": destination.id, "hdmi_input_id": None},
    )
    assert response.status_code == 201
    assert response.json()["hdmi_input_id"] is None


async def test_create_action_hdmi_source_refuses_an_unknown_destination(
    admin: AsyncClient, db: Database
) -> None:
    scene = await make_scene(db)
    response = await admin.post(
        f"{SCENES}/{scene.id}/actions",
        json={"domain": "hdmi_source", "hdmi_destination": 404},
    )
    assert response.status_code == 422
    assert code(response) == "validation_failed"
    assert fields(response) == {"hdmi_destination"}


async def test_create_action_refuses_what_the_driver_does_not_support(
    admin: AsyncClient, db: Database, engine: SceneEngine, services: Services
) -> None:
    """§5.5: the API is one of the three places capability is enforced."""
    mixer = await devices_crud.create(
        db, category="mixer", driver_key="cq20b", name="CQ-20B", config={}
    )
    desk_scene = await mixer_crud.create_desk_scene(
        db, device_id=mixer.id, scene_ref="1", name="Baseline"
    )
    services.devices.reports[mixer.id] = mixer_capabilities(scene_recall=False)
    engine.handlers.register(
        "mixer_recall",
        RecordingHandler(
            gate=lambda a, caps: (
                None if getattr(caps, "supports_scene_recall", False) else "no scene recall"
            )
        ),
    )
    scene = await make_scene(db)
    response = await admin.post(
        f"{SCENES}/{scene.id}/actions",
        json={"domain": "mixer_recall", "mixer_scene_id": desk_scene.id},
    )
    assert response.status_code == 422
    assert code(response) == "validation_failed"
    assert detail(response)["reason"] == "unsupported"
    assert "no scene recall" in detail(response)["fields"][0]["message"]


async def test_get_action(admin: AsyncClient, db: Database, rig: Rig) -> None:
    scene = await make_scene(db, knx(rig.lamp, "1"))
    action = (await scenes_crud.list_actions(db, scene.id))[0]
    response = await admin.get(f"{SCENES}/{scene.id}/actions/{action.id}")
    assert response.status_code == 200
    assert response.json()["knx_value"] == "1"


async def test_get_action_of_another_scene_is_not_found(
    admin: AsyncClient, db: Database, rig: Rig
) -> None:
    scene = await make_scene(db, knx(rig.lamp, "1"))
    other = await make_scene(db, name="Other")
    action = (await scenes_crud.list_actions(db, scene.id))[0]
    response = await admin.get(f"{SCENES}/{other.id}/actions/{action.id}")
    assert code(response) == "not_found"


async def test_update_action_with_its_version(admin: AsyncClient, db: Database, rig: Rig) -> None:
    scene = await make_scene(db, dmx({rig.front: 10.0}))
    action = (await scenes_crud.list_actions(db, scene.id))[0]
    response = await admin.put(
        f"{SCENES}/{scene.id}/actions/{action.id}",
        json={"dmx_fade_ms": 1500, "delay_ms": 500},
        headers={VERSION_HEADER: action.updated_at},
    )
    assert response.status_code == 200
    assert (response.json()["dmx_fade_ms"], response.json()["delay_ms"]) == (1500, 500)


async def test_update_action_conflict_and_invalid(
    admin: AsyncClient, db: Database, rig: Rig
) -> None:
    scene = await make_scene(db, dmx({rig.front: 10.0}))
    action = (await scenes_crud.list_actions(db, scene.id))[0]
    await scenes_crud.update_action(db, action.id, action.updated_at, dmx_fade_ms=100)
    stale = await admin.put(
        f"{SCENES}/{scene.id}/actions/{action.id}",
        json={"dmx_fade_ms": 1500},
        headers={VERSION_HEADER: action.updated_at},
    )
    assert stale.status_code == 409
    assert code(stale) == "conflict"
    assert detail(stale)["current"]["dmx_fade_ms"] == 100
    current = await scenes_crud.get_action(db, action.id)
    assert current is not None
    invalid = await admin.put(
        f"{SCENES}/{scene.id}/actions/{action.id}",
        json={"domain": "knx"},  # a dmx action's fields with no address
        headers={VERSION_HEADER: current.updated_at},
    )
    assert code(invalid) == "validation_failed"


async def test_delete_action(admin: AsyncClient, db: Database, rig: Rig) -> None:
    scene = await make_scene(db, dmx({rig.front: 10.0}))
    action = (await scenes_crud.list_actions(db, scene.id))[0]
    response = await admin.delete(f"{SCENES}/{scene.id}/actions/{action.id}")
    assert response.status_code == 204
    assert await scenes_crud.list_actions(db, scene.id) == []


async def test_delete_action_not_found(admin: AsyncClient, db: Database) -> None:
    scene = await make_scene(db)
    response = await admin.delete(f"{SCENES}/{scene.id}/actions/31")
    assert code(response) == "not_found"


# -- POST /scenes/{id}/trigger ---------------------------------------------------------------------


async def test_trigger_starts_the_scene_for_admin_and_operator(
    admin: AsyncClient,
    operator: AsyncClient,
    db: Database,
    rig: Rig,
    engine: SceneEngine,
    services: Services,
) -> None:
    scene = await make_scene(db, dmx({rig.front: 64.0}, fade_ms=50))
    response = await operator.post(f"{SCENES}/{scene.id}/trigger")
    assert response.status_code == 202
    body = response.json()
    assert body["triggered_by"] == "api:operator"
    assert body["scene_id"] == scene.id
    await settle(engine)
    assert services.state.lighting.get_item("levels", rig.front) == 64.0
    assert services.broadcaster.types() == ["scene_started", "scene_completed"]

    again = await admin.post(f"{SCENES}/{scene.id}/trigger")
    assert again.json()["triggered_by"] == "api:admin"
    await settle(engine)


async def test_trigger_refuses_disabled_hidden_and_missing_scenes(
    admin: AsyncClient, operator: AsyncClient, db: Database
) -> None:
    disabled = await make_scene(db, enabled=False)
    response = await admin.post(f"{SCENES}/{disabled.id}/trigger")
    assert response.status_code == 403
    assert code(response) == "permission_denied"
    assert detail(response)["reason"] == "scene_disabled"

    hidden = await make_scene(db, name="All Off", visible_operator=False)
    refused = await operator.post(f"{SCENES}/{hidden.id}/trigger")
    assert code(refused) == "permission_denied"
    assert detail(refused)["reason"] == "not_visible"

    missing = await admin.post(f"{SCENES}/999/trigger")
    assert code(missing) == "not_found"


async def test_trigger_without_a_running_engine_is_device_unavailable(
    app: FastAPI, admin: AsyncClient, db: Database
) -> None:
    scene = await make_scene(db)
    engine, app.state.scene_engine = app.state.scene_engine, None
    try:
        response = await admin.post(f"{SCENES}/{scene.id}/trigger")
    finally:
        app.state.scene_engine = engine
    assert response.status_code == 503
    assert code(response) == "device_unavailable"


# -- POST /scenes/{id}/test ----------------------------------------------------------------------


async def test_test_respects_delays_and_reports_every_action_inline(
    admin: AsyncClient, db: Database, rig: Rig, services: Services
) -> None:
    """§22.4: scene execution with a failing action — partial, and the rest still ran."""
    services.knx.unknown.add("1/0/5")
    scene = await make_scene(
        db,
        dmx({rig.front: 80.0}),
        knx(rig.level, "40"),
        knx(rig.lamp, "1", delay_ms=150),
        {"domain": "projector_power", "projector_power": "on", "delay_ms": 150},
    )
    response = await admin.post(f"{SCENES}/{scene.id}/test")
    assert response.status_code == 200
    body = response.json()
    assert body["result"] == "partial"
    assert body["duration_ms"] >= 150 - 40
    lines = [(a["domain"], a["result"], a["marker"]) for a in body["actions"]]
    assert lines == [
        ("dmx", "sent", "✓"),
        ("knx", "failed", "✗"),
        ("knx", "sent", "✓"),
        ("projector_power", "skipped", "⊘"),
    ]
    assert all(a["reason"] for a in body["actions"] if a["result"] != "sent")
    assert services.state.lighting.get_item("levels", rig.front) == 80.0
    assert [ga for _, ga, _, _ in services.knx.writes] == ["1/0/9"]


async def test_test_is_admin_only(operator: AsyncClient, db: Database) -> None:
    scene = await make_scene(db)
    response = await operator.post(f"{SCENES}/{scene.id}/test")
    assert response.status_code == 403
    assert code(response) == "permission_denied"


# -- POST /scenes/{id}/test-group ----------------------------------------------------------------


async def test_test_group_fires_one_group_now(
    admin: AsyncClient, db: Database, rig: Rig, services: Services
) -> None:
    scene = await make_scene(db, dmx({rig.front: 30.0}), dmx({rig.mid: 60.0}, delay_ms=8000))
    response = await admin.post(f"{SCENES}/{scene.id}/test-group", json={"delay_ms": 8000})
    assert response.status_code == 200
    body = response.json()
    assert [a["delay_ms"] for a in body["actions"]] == [8000]
    assert body["duration_ms"] < 1000
    assert services.state.lighting.get_item("levels", rig.mid) == 60.0
    assert services.state.lighting.get_item("levels", rig.front) in (None, 0.0)


async def test_test_group_with_no_actions_at_that_delay(
    admin: AsyncClient, db: Database, rig: Rig
) -> None:
    scene = await make_scene(db, dmx({rig.front: 30.0}))
    response = await admin.post(f"{SCENES}/{scene.id}/test-group", json={"delay_ms": 1234})
    assert response.status_code == 422
    assert code(response) == "validation_failed"
    assert fields(response) == {"delay_ms"}


# -- the execution log ---------------------------------------------------------------------------


async def test_scene_log(
    admin: AsyncClient, operator: AsyncClient, db: Database, rig: Rig, engine: SceneEngine
) -> None:
    scene = await make_scene(db, dmx({rig.front: 30.0}))
    await (await engine.run(scene.id, triggered_by="knx:1/0/1", trigger_value=True)).result()
    response = await operator.get(f"{SCENES}/{scene.id}/log")
    assert response.status_code == 200
    entry = response.json()["entries"][0]
    assert entry["triggered_by"] == "knx:1/0/1"
    assert entry["result"] == "success"
    assert entry["completed_at"] is not None
    assert entry["action_results"][0]["marker"] == "✓"


async def test_scene_log_not_found(admin: AsyncClient) -> None:
    response = await admin.get(f"{SCENES}/404/log")
    assert code(response) == "not_found"


async def test_all_scenes_log_filters_by_result_and_date_range(
    admin: AsyncClient, db: Database, rig: Rig, engine: SceneEngine, services: Services
) -> None:
    good = await make_scene(db, dmx({rig.front: 30.0}), name="Good")
    services.knx.unknown.add("1/0/9")
    mixed = await make_scene(db, dmx({rig.mid: 30.0}), knx(rig.lamp, "1"), name="Mixed")
    await (await engine.run(good.id, triggered_by="schedule")).result()
    await (await engine.run(mixed.id, triggered_by="schedule")).result()

    everything = await admin.get(f"{SCENES}/log")
    assert [e["scene_id"] for e in everything.json()["entries"]] == [mixed.id, good.id]
    partial = await admin.get(f"{SCENES}/log", params={"result": "partial"})
    assert [e["scene_id"] for e in partial.json()["entries"]] == [mixed.id]

    now = datetime.now(tz=AUCKLAND)
    window = {
        "from": (now - timedelta(minutes=5)).isoformat(),
        "to": (now + timedelta(minutes=5)).isoformat(),
    }
    assert len((await admin.get(f"{SCENES}/log", params=window)).json()["entries"]) == 2
    past = {"to": (now - timedelta(days=1)).isoformat()}
    assert (await admin.get(f"{SCENES}/log", params=past)).json()["entries"] == []


async def test_all_scenes_log_refuses_an_unknown_result(admin: AsyncClient) -> None:
    response = await admin.get(f"{SCENES}/log", params={"result": "exploded"})
    assert response.status_code == 422
    assert code(response) == "validation_failed"


@pytest.fixture
def rig(services: Services) -> Rig:
    return services.rig


# -- wiring --------------------------------------------------------------------------------------


async def test_the_lifespan_builds_the_scene_engine_and_settles_it_on_shutdown(
    config: Config,
) -> None:
    application = create_app(config)
    async with application.router.lifespan_context(application):
        engine = application.state.scene_engine
        assert isinstance(engine, SceneEngine)
        assert application.state.state_store.owners("scenes") == frozenset({"scene_engine"})
    assert application.state.scene_engine is None


# -- §22.4 audit: failure tests added ----


async def test_test_scene_not_found(admin: AsyncClient) -> None:
    """POST /scenes/{id}/test's only prior failure coverage was tier-only (admin-only).

    ``_start_error`` maps a ``SceneNotFoundError`` from ``engine.test`` to
    ``not_found``, the same as every other id-addressed scene endpoint.
    """
    response = await admin.post(f"{SCENES}/999/test")
    assert response.status_code == 404
    assert code(response) == "not_found"
