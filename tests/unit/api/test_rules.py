"""Rules and derived-status endpoints (spec §16.5, §8, §21.17, §22.4).

§22.4: one success and one failure test per endpoint, the failure asserting
the error envelope's code. The application runs over the rules test rig — a
real lighting service and rules engine over the test database — so a fired
rule really moves the level store.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response

from proskenion.api import rules as rules_api
from proskenion.api.app import API_PREFIX, create_app
from proskenion.config import Config
from proskenion.core.auth import COOKIE_NAME, TokenService
from proskenion.core.ratelimit import RateLimiter
from proskenion.db.connection import Database
from proskenion.db.crud import knx as knx_crud
from proskenion.db.crud import rules as rules_crud
from tests.unit.api.conftest import ADMIN_PASSWORD, OPERATOR_PASSWORD, make_client
from tests.unit.rules.conftest import Rig, add_scene, start_rig, stop_rig

RULES = f"{API_PREFIX}/rules"
STATUSES = f"{API_PREFIX}/derived-status"
VERSION = "If-Unmodified-Since-Version"


@pytest.fixture
async def rig(db: Database, dev_config: Config) -> AsyncIterator[Rig]:
    running = await start_rig(db, dev_config)
    try:
        yield running
    finally:
        await stop_rig(running)


@pytest.fixture
def app(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter, rig: Rig
) -> FastAPI:
    application = create_app(
        config, db=db, tokens=tokens, limiter=limiter, bus=rig.bus, state=rig.state
    )
    application.state.rules = rig.engine
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with make_client(app) as http:
        yield http


async def login(client: AsyncClient, password: str = ADMIN_PASSWORD) -> Response:
    response = await client.post(f"{API_PREFIX}/auth/login", json={"password": password})
    assert response.status_code == 200, response.text
    return response


def code(response: Response) -> str:
    return str(response.json()["error"]["code"])


def fields(response: Response) -> dict[str, Any]:
    return dict(response.json()["error"]["detail"]["fields"])


def alarm_rule(rig: Rig, scene_id: int, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "Alarm armed",
        "trigger_type": "knx",
        "knx_address_id": rig.venue.addresses["0/5/0"],
        "match_type": "equal",
        "match_value": 1,
        "action_type": "run_scene",
        "scene_id": scene_id,
    }
    body.update(overrides)
    return body


def binding(rig: Rig, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "Row 2 again",
        "trigger_type": "knx",
        "knx_address_id": rig.venue.addresses["1/0/2"],
        "match_type": "any",
        "action_type": "lighting_group",
        "lighting_group_id": rig.venue.groups["Row 2"],
        "on_level": 100,
        "off_level": 0,
    }
    body.update(overrides)
    return body


# -- GET /rules ---------------------------------------------------------------------------


async def test_the_lighting_view_reads_the_stage_banks_as_an_operator(
    client: AsyncClient, rig: Rig
) -> None:
    await login(client, OPERATOR_PASSWORD)
    response = await client.get(RULES, params={"action_type": "lighting_group"})
    assert response.status_code == 200
    rules = response.json()["rules"]
    assert [r["name"] for r in rules] == ["Stage Bank 1", "Stage Bank 2", "All Stage", "House"]
    first = rules[0]
    assert {k: first[k] for k in ("id", "name", "lighting_group_id", "on_level", "off_level")} == {
        "id": rig.venue.rules["Stage Bank 1"],
        "name": "Stage Bank 1",
        "lighting_group_id": rig.venue.groups["Row 1"],
        "on_level": 100.0,
        "off_level": 0.0,
    }


async def test_listing_rules_needs_a_session(client: AsyncClient) -> None:
    response = await client.get(RULES)
    assert response.status_code == 401
    assert code(response) == "unauthenticated"


async def test_an_unknown_action_type_filter_is_refused(client: AsyncClient) -> None:
    await login(client)
    response = await client.get(RULES, params={"action_type": "chain"})
    assert response.status_code == 422
    assert code(response) == "validation_failed"


# -- POST /rules ----------------------------------------------------------------------------


async def test_creating_a_rule_stores_it_and_the_engine_runs_it(
    client: AsyncClient, rig: Rig
) -> None:
    await login(client)
    scene = await add_scene(rig.db, rig.venue, "All Off", priority="critical")
    response = await client.post(RULES, json=alarm_rule(rig, scene))
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["match_value"] == "1" and body["fires_automatically"] is True

    await rig.telegram("0/5/0", True)
    await rig.settle(lambda: len(rig.scenes.calls) == 1)
    assert rig.scenes.calls[0].triggered_by == "knx:0/5/0"


async def test_creating_rules_is_admin_only(client: AsyncClient, rig: Rig) -> None:
    await login(client, OPERATOR_PASSWORD)
    response = await client.post(RULES, json=binding(rig))
    assert response.status_code == 403
    assert code(response) == "permission_denied"


async def test_a_match_type_the_addresss_dpt_does_not_allow_is_refused(
    client: AsyncClient, rig: Rig
) -> None:
    await login(client)
    scene = await add_scene(rig.db, rig.venue, "Scene")
    response = await client.post(RULES, json=alarm_rule(rig, scene, match_type="gte"))
    assert response.status_code == 422
    assert code(response) == "validation_failed"
    assert "1.001" in fields(response)["match_type"][0]
    # The same match is fine on a numeric address.
    lux = alarm_rule(
        rig,
        scene,
        knx_address_id=rig.venue.addresses["1/5/1"],
        match_type="range",
        match_value=100,
        match_value_max=500,
    )
    assert (await client.post(RULES, json=lux)).status_code == 201


async def test_a_binding_that_is_not_any_on_a_one_bit_address_is_refused(
    client: AsyncClient, rig: Rig
) -> None:
    await login(client)
    response = await client.post(RULES, json=binding(rig, match_type="equal", match_value=1))
    assert response.status_code == 422
    assert code(response) == "validation_failed"
    assert "any" in fields(response)["match_type"][0]

    numeric = await client.post(
        RULES, json=binding(rig, knx_address_id=rig.venue.addresses["1/5/2"])
    )
    assert numeric.status_code == 422
    assert "1-bit" in fields(numeric)["knx_address_id"][0]


async def test_a_malformed_cron_is_refused_and_a_valid_one_stored_with_its_next_time(
    client: AsyncClient, rig: Rig
) -> None:
    await login(client)
    scene = await add_scene(rig.db, rig.venue, "All Off")
    nightly: dict[str, Any] = {
        "name": "Nightly off",
        "trigger_type": "schedule",
        "cron": "23:00 daily",
        "action_type": "run_scene",
        "scene_id": scene,
    }
    response = await client.post(RULES, json=nightly)
    assert response.status_code == 422
    assert code(response) == "validation_failed"
    assert "cron" in fields(response)

    nightly["cron"] = "0 23 * * *"
    stored = await client.post(RULES, json=nightly)
    assert stored.status_code == 201
    assert stored.json()["fires_automatically"] is True
    assert stored.json()["note"] is None
    upcoming = datetime.fromisoformat(stored.json()["next_fire_at"])
    assert (upcoming.hour, upcoming.minute) == (23, 0)
    assert upcoming.utcoffset() in (timedelta(hours=12), timedelta(hours=13))


async def test_a_rule_on_a_derived_status_address_is_refused(client: AsyncClient, rig: Rig) -> None:
    await login(client)
    scene = await add_scene(rig.db, rig.venue, "Would chain")
    response = await client.post(
        RULES,
        json=alarm_rule(rig, scene, knx_address_id=rig.venue.addresses["1/0/11"], match_type="any"),
    )
    assert response.status_code == 422
    messages = " ".join(fields(response)["knx_address_id"])
    assert "§8.7" in messages


async def test_a_violated_check_constraint_is_validation_failed_not_internal_error(
    client: AsyncClient, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The database's CHECK is the last line; its error is the caller's (§16.1)."""

    async def skip(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(rules_api, "_validate_rule", skip)
    await login(client)
    response = await client.post(RULES, json=binding(rig, match_type="equal", match_value=1))
    assert response.status_code == 422
    assert code(response) == "validation_failed"
    assert response.json()["error"]["detail"]["constraint"] == "rules_action_shape"


# -- GET /rules/{id} ----------------------------------------------------------------------


async def test_reading_one_rule(client: AsyncClient, rig: Rig) -> None:
    await login(client, OPERATOR_PASSWORD)
    response = await client.get(f"{RULES}/{rig.venue.rules['House']}")
    assert response.status_code == 200
    assert response.json()["name"] == "House"


async def test_reading_a_missing_rule_is_not_found(client: AsyncClient) -> None:
    await login(client)
    response = await client.get(f"{RULES}/9999")
    assert response.status_code == 404
    assert code(response) == "not_found"


# -- PUT /rules/{id} ------------------------------------------------------------------------


async def test_updating_a_rule_with_its_version(client: AsyncClient, rig: Rig) -> None:
    await login(client)
    rule_id = rig.venue.rules["Stage Bank 1"]
    current = (await client.get(f"{RULES}/{rule_id}")).json()
    response = await client.put(
        f"{RULES}/{rule_id}",
        json={"on_level": 80, "fade_ms": 0},
        headers={VERSION: current["updated_at"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["on_level"] == 80.0
    await rig.telegram("1/0/1", True)  # the engine has the new level
    assert rig.level("A") == 80.0


async def test_a_stale_update_is_a_conflict_carrying_the_current_rule(
    client: AsyncClient, rig: Rig
) -> None:
    await login(client)
    rule_id = rig.venue.rules["Stage Bank 1"]
    stale = (await client.get(f"{RULES}/{rule_id}")).json()["updated_at"]
    first = await client.put(
        f"{RULES}/{rule_id}", json={"name": "Bank 1"}, headers={VERSION: stale}
    )
    assert first.status_code == 200
    second = await client.put(f"{RULES}/{rule_id}", json={"name": "B1"}, headers={VERSION: stale})
    assert second.status_code == 409
    assert code(second) == "conflict"
    assert second.json()["error"]["detail"]["current"]["name"] == "Bank 1"


async def test_an_update_without_a_version_or_breaking_the_shape_is_refused(
    client: AsyncClient, rig: Rig
) -> None:
    await login(client)
    rule_id = rig.venue.rules["Stage Bank 1"]
    missing = await client.put(f"{RULES}/{rule_id}", json={"name": "x"})
    assert missing.status_code == 422 and code(missing) == "validation_failed"
    version = (await client.get(f"{RULES}/{rule_id}")).json()["updated_at"]
    broken = await client.put(
        f"{RULES}/{rule_id}", json={"match_type": "equal"}, headers={VERSION: version}
    )
    assert broken.status_code == 422
    assert "match_type" in fields(broken)


# -- DELETE /rules/{id} ------------------------------------------------------------------------


async def test_deleting_a_rule(client: AsyncClient, rig: Rig) -> None:
    await login(client)
    rule_id = rig.venue.rules["House"]
    response = await client.delete(f"{RULES}/{rule_id}")
    assert response.status_code == 204
    assert rule_id not in {s["id"] for s in rig.engine.rule_states()}


async def test_deleting_a_missing_rule_is_not_found(client: AsyncClient) -> None:
    await login(client)
    response = await client.delete(f"{RULES}/9999")
    assert response.status_code == 404
    assert code(response) == "not_found"


# -- GET /rules/state ----------------------------------------------------------------------------


async def test_rule_state_is_live_and_shows_suppression(client: AsyncClient, rig: Rig) -> None:
    await login(client, OPERATOR_PASSWORD)
    await rig.telegram("1/0/1", True)
    await rig.settle(lambda: rig.engine.binding_state(rig.venue.rules["Stage Bank 1"]))
    rig.lighting.set_external_manual(True)

    response = await client.get(f"{RULES}/state")
    assert response.status_code == 200
    body = response.json()
    assert body["external_control"] is True
    by_name = {r["name"]: r for r in body["rules"]}
    assert by_name["Stage Bank 1"]["suppressed"] is True
    assert by_name["House"]["suppressed"] is False
    assert by_name["Stage Bank 1"]["last_result"] == "success"


async def test_rule_state_without_a_running_engine_is_device_unavailable(
    client: AsyncClient, app: FastAPI
) -> None:
    app.state.rules = None
    await login(client)
    response = await client.get(f"{RULES}/state")
    assert response.status_code == 503
    assert code(response) == "device_unavailable"


# -- POST /rules/{id}/fire


async def test_firing_a_bank_as_an_operator(client: AsyncClient, rig: Rig) -> None:
    await login(client, OPERATOR_PASSWORD)
    bank = rig.venue.rules["Stage Bank 2"]
    response = await client.post(f"{RULES}/{bank}/fire", json={"value": 1})
    assert response.status_code == 200, response.text
    assert response.json()["result"] == "success"
    assert rig.level("C") == 100.0 and rig.level("D") == 100.0
    await rig.engine.flush_log()
    entries = await rules_crud.list_executions(rig.db, rule_id=bank)
    assert entries[0].triggered_by == "api:operator"


async def test_firing_a_missing_rule_is_not_found(client: AsyncClient) -> None:
    await login(client, OPERATOR_PASSWORD)
    response = await client.post(f"{RULES}/9999/fire", json={"value": 1})
    assert response.status_code == 404
    assert code(response) == "not_found"


# -- POST /rules/{id}/test


async def test_testing_a_rule_reports_a_scene_inline(client: AsyncClient, rig: Rig) -> None:
    await login(client)
    scene = await add_scene(rig.db, rig.venue, "Projector on")
    rule_id = (await client.post(RULES, json=alarm_rule(rig, scene, enabled=False))).json()["id"]
    response = await client.post(f"{RULES}/{rule_id}/test", json={})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["result"] == "success" and body["detail"]["scene_result"] == "success"
    assert rig.scenes.calls[-1].triggered_by == "api:admin"


async def test_testing_is_admin_only(client: AsyncClient, rig: Rig) -> None:
    await login(client, OPERATOR_PASSWORD)
    response = await client.post(f"{RULES}/{rig.venue.rules['House']}/test", json={})
    assert response.status_code == 403
    assert code(response) == "permission_denied"


# -- GET /rules/log


async def test_the_log_carries_trigger_guard_action_and_outcome(
    client: AsyncClient, rig: Rig
) -> None:
    await rig.telegram("1/0/1", True)
    await rig.engine.flush_log()
    await login(client, OPERATOR_PASSWORD)
    response = await client.get(f"{RULES}/log", params={"rule_id": rig.venue.rules["Stage Bank 1"]})
    assert response.status_code == 200
    (entry,) = response.json()["entries"]
    assert entry["triggered_by"] == "knx:1/0/1"
    assert entry["guard_result"] is None and entry["result"] == "success"
    assert entry["detail"]["action"] == "lighting_group" and entry["detail"]["level"] == 100.0


async def test_a_log_filter_without_an_offset_is_refused(client: AsyncClient) -> None:
    await login(client)
    response = await client.get(f"{RULES}/log", params={"since": "2026-09-11T08:00:00"})
    assert response.status_code == 422
    assert code(response) == "validation_failed"
    assert "since" in fields(response)


# -- derived status CRUD


async def new_status_address(rig: Rig, ga: str = "1/0/13", dpt: str = "1.001") -> int:
    address = await knx_crud.create_address(
        rig.db, group_address=ga, name=f"Status {ga}", dpt=dpt, direction="outgoing"
    )
    return address.id


async def test_listing_derived_statuses(client: AsyncClient) -> None:
    await login(client)
    response = await client.get(STATUSES)
    assert response.status_code == 200
    by_address = {s["group_address"]: s for s in response.json()["derived_statuses"]}
    assert by_address["1/0/9"]["source_type"] == "external_control"


async def test_derived_status_configuration_is_admin_only(client: AsyncClient) -> None:
    await login(client, OPERATOR_PASSWORD)
    response = await client.get(STATUSES)
    assert response.status_code == 403
    assert code(response) == "permission_denied"


async def test_creating_a_derived_status_writes_it_at_once(client: AsyncClient, rig: Rig) -> None:
    await login(client)
    address_id = await new_status_address(rig)
    response = await client.post(
        STATUSES,
        json={
            "name": "Row 2 half",
            "knx_address_id": address_id,
            "source_type": "lighting_group_all_at",
            "lighting_group_id": rig.venue.groups["Row 2"],
            "compare_level": 0,
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["group_address"] == "1/0/13"
    await rig.settle(lambda: rig.knx.to("1/0/13") == [True])  # every member at 0 now


async def test_a_lamp_only_derived_status_accepts_a_null_address(
    client: AsyncClient, rig: Rig
) -> None:
    """Q6 (migration 006): a status with no wall-panel indicator — 'House at
    100%', say — has nothing to write to the KNX bus, but still lights a
    page button's lamp once evaluated."""
    await login(client)
    response = await client.post(
        STATUSES,
        json={
            "name": "House at 100%",
            "knx_address_id": None,
            "source_type": "lighting_group_all_at",
            "lighting_group_id": rig.venue.groups["House"],
            "compare_level": 100,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["knx_address_id"] is None
    assert body["group_address"] is None
    status_id = body["id"]
    await rig.settle(
        lambda: rig.state.status.get_item("lamps", str(status_id))
        == {"on": False, "transitioning": False}
    )

    version = body["updated_at"]
    update = await client.put(
        f"{STATUSES}/{status_id}",
        json={"knx_address_id": None, "compare_level": 100},
        headers={VERSION: version},
    )
    assert update.status_code == 200, update.text
    assert update.json()["knx_address_id"] is None


async def test_a_duplicate_derived_status_address_is_refused(client: AsyncClient, rig: Rig) -> None:
    await login(client)
    response = await client.post(
        STATUSES,
        json={
            "name": "Second writer",
            "knx_address_id": rig.venue.addresses["1/0/9"],
            "source_type": "external_control",
        },
    )
    assert response.status_code == 422
    assert code(response) == "validation_failed"
    assert "already written" in fields(response)["knx_address_id"][0]


async def test_a_derived_status_needs_a_one_bit_address_no_rule_triggers_on(
    client: AsyncClient, rig: Rig
) -> None:
    await login(client)
    numeric = await new_status_address(rig, "1/0/14", "5.001")
    response = await client.post(
        STATUSES,
        json={"name": "Wrong", "knx_address_id": numeric, "source_type": "external_control"},
    )
    assert response.status_code == 422 and "DPT" in fields(response)["knx_address_id"][0]
    command = await client.post(
        STATUSES,
        json={
            "name": "Loop",
            "knx_address_id": rig.venue.addresses["1/0/1"],
            "source_type": "external_control",
        },
    )
    assert command.status_code == 422
    assert any("§8.7" in m for m in fields(command)["knx_address_id"])


async def test_the_unique_constraint_is_validation_failed_not_internal_error(
    client: AsyncClient, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def skip(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(rules_api, "_validate_status", skip)
    await login(client)
    response = await client.post(
        STATUSES,
        json={
            "name": "Second writer",
            "knx_address_id": rig.venue.addresses["1/0/9"],
            "source_type": "external_control",
        },
    )
    assert response.status_code == 422
    assert code(response) == "validation_failed"
    assert response.json()["error"]["detail"]["constraint"] == "derived_status_knx_address_unique"


async def test_reading_one_derived_status(client: AsyncClient, rig: Rig) -> None:
    await login(client)
    response = await client.get(f"{STATUSES}/{rig.venue.statuses['1/0/11']}")
    assert response.status_code == 200
    assert response.json()["group_address"] == "1/0/11"


async def test_reading_a_missing_derived_status_is_not_found(client: AsyncClient) -> None:
    await login(client)
    response = await client.get(f"{STATUSES}/9999")
    assert response.status_code == 404
    assert code(response) == "not_found"


async def test_updating_a_derived_status_with_its_version(client: AsyncClient, rig: Rig) -> None:
    await login(client)
    status_id = rig.venue.statuses["1/0/11"]
    version = (await client.get(f"{STATUSES}/{status_id}")).json()["updated_at"]
    response = await client.put(
        f"{STATUSES}/{status_id}", json={"compare_level": 0}, headers={VERSION: version}
    )
    assert response.status_code == 200, response.text
    await rig.settle(lambda: rig.knx.to("1/0/11")[-1:] == [True])  # now "all at 0"


async def test_a_stale_derived_status_update_is_a_conflict(client: AsyncClient, rig: Rig) -> None:
    await login(client)
    status_id = rig.venue.statuses["1/0/11"]
    stale = (await client.get(f"{STATUSES}/{status_id}")).json()["updated_at"]
    ok = await client.put(f"{STATUSES}/{status_id}", json={"name": "B1"}, headers={VERSION: stale})
    assert ok.status_code == 200
    again = await client.put(
        f"{STATUSES}/{status_id}", json={"name": "x"}, headers={VERSION: stale}
    )
    assert again.status_code == 409
    assert code(again) == "conflict"


async def test_deleting_a_derived_status(client: AsyncClient, rig: Rig) -> None:
    await login(client)
    response = await client.delete(f"{STATUSES}/{rig.venue.statuses['1/0/12']}")
    assert response.status_code == 204
    assert "1/0/12" not in rig.engine.derived.addresses


async def test_deleting_a_missing_derived_status_is_not_found(client: AsyncClient) -> None:
    await login(client)
    response = await client.delete(f"{STATUSES}/9999")
    assert response.status_code == 404
    assert code(response) == "not_found"


# -- GET /derived-status/state


async def test_derived_state_shows_each_address_now(client: AsyncClient, rig: Rig) -> None:
    await rig.telegram("1/0/1", True)
    await rig.settle(lambda: rig.knx.to("1/0/11")[-1:] == [True])
    await login(client, OPERATOR_PASSWORD)
    response = await client.get(f"{STATUSES}/state")
    assert response.status_code == 200
    by_address = {s["group_address"]: s for s in response.json()["statuses"]}
    assert by_address["1/0/11"]["value"] is True and by_address["1/0/11"]["written"] is True
    assert by_address["1/0/12"]["value"] is False
    assert by_address["1/0/11"]["changed_at"].endswith(("+12:00", "+13:00"))


async def test_derived_state_without_a_running_engine_is_device_unavailable(
    client: AsyncClient, app: FastAPI
) -> None:
    app.state.rules = None
    await login(client, OPERATOR_PASSWORD)
    response = await client.get(f"{STATUSES}/state")
    assert response.status_code == 503
    assert code(response) == "device_unavailable"


# -- GET /derived-status/monitor (server-sent events, §8.10)


async def first_events(app: FastAPI, cookie: str, count: int) -> tuple[int, list[str]]:
    """Drive the ASGI app directly: read ``count`` events, then disconnect."""
    status = 0
    chunks: list[str] = []
    got = asyncio.Event()
    request_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await got.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
        elif message["type"] == "http.response.body" and message.get("body"):
            chunks.append(message["body"].decode())
            if sum(c.count("event: status") for c in chunks) >= count:
                got.set()

    path = f"{STATUSES}/monitor"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test"), (b"cookie", f"{COOKIE_NAME}={cookie}".encode())],
        "client": ("127.0.0.1", 50000),
        "server": ("test", 443),
    }
    await asyncio.wait_for(app(scope, receive, send), 5.0)
    return status, chunks


async def test_the_monitor_streams_every_status_as_server_sent_events(
    client: AsyncClient, app: FastAPI, rig: Rig
) -> None:
    cookie = (await login(client)).cookies[COOKIE_NAME]
    status, chunks = await first_events(app, cookie, len(rig.venue.statuses))
    assert status == 200
    events = [
        json.loads(line.removeprefix("data: "))
        for chunk in chunks
        for line in chunk.splitlines()
        if line.startswith("data: ")
    ]
    assert {e["group_address"] for e in events} == set(rig.venue.statuses)
    assert all({"value", "changed_at", "written"} <= set(e) for e in events)


async def test_the_monitor_is_admin_only(client: AsyncClient) -> None:
    await login(client, OPERATOR_PASSWORD)
    response = await client.get(f"{STATUSES}/monitor")
    assert response.status_code == 403
    assert code(response) == "permission_denied"


# -- §22.4 audit: failure tests added ----------------------------------------------------
#
# Both endpoints below already had tier-only failure coverage (an operator or
# unauthenticated caller refused). Each also implements its own domain failure
# mode, so this adds that instead of leaving the tier refusal as the only case.


async def test_testing_a_missing_rule_is_not_found(client: AsyncClient) -> None:
    """POST /rules/{id}/test shares fire's UnknownRuleError -> not_found handling."""
    await login(client)
    response = await client.post(f"{RULES}/9999/test", json={})
    assert response.status_code == 404
    assert code(response) == "not_found"


async def test_the_monitor_without_a_running_engine_is_device_unavailable(
    client: AsyncClient, app: FastAPI
) -> None:
    """derived_monitor resolves the engine via ``_engine`` before it starts
    streaming, so a stopped engine is reported the same way as the other
    rules-engine-backed endpoints, not as a broken stream."""
    app.state.rules = None
    await login(client)
    response = await client.get(f"{STATUSES}/monitor")
    assert response.status_code == 503
    assert code(response) == "device_unavailable"
