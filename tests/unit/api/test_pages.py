"""Pages: the everyday surface (spec §15.12, §21.9, §22.4).

Exactly ``docs/plans/phase-5-contracts.md``'s "Pages" and "Button lamps"
sections. One success and one failure per endpoint (§22.4), tier filtering
against the real permission resolver (``HirerPermissionResolver``,
started directly against the test database rather than stood in for), tray
computation, the validate findings, and the hirer flag reaching the scene
engine's ``ActionContext``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response

from proskenion.api.app import API_PREFIX, create_app
from proskenion.config import Config
from proskenion.core.auth import TokenService
from proskenion.core.devices import DeviceManager
from proskenion.core.drivers.capabilities import ProjectorCapabilities
from proskenion.core.events import HirerConfigChanged
from proskenion.core.ratelimit import RateLimiter
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import hirer as hirer_crud
from proskenion.db.crud import lighting as lighting_crud
from proskenion.db.crud import mixer as mixer_crud
from proskenion.db.crud import pages as pages_crud
from proskenion.db.crud import rules as rules_crud
from proskenion.db.crud import scenes as scenes_crud
from proskenion.db.crud.pages import PageButtonInput, PageItemInput
from proskenion.rules.engine import RulesEngine
from proskenion.scene.domains import ActionOutcome
from proskenion.scene.engine import SceneEngine
from tests.unit.api.conftest import ADMIN_PASSWORD, HIRER_PIN, make_client
from tests.unit.rules.conftest import Rig, add_scene, start_rig, stop_rig
from tests.unit.scene.conftest import FakeCapabilities, RecordingHandler

PAGES = f"{API_PREFIX}/pages"
RULES = f"{API_PREFIX}/rules"
HIRER = f"{API_PREFIX}/hirer"
AUTH = f"{API_PREFIX}/auth"
VERSION = "If-Unmodified-Since-Version"


# -- fixtures -------------------------------------------------------------------------


@pytest.fixture
async def rig(db: Database, dev_config: Config) -> AsyncIterator[Rig]:
    running = await start_rig(db, dev_config)
    try:
        yield running
    finally:
        await stop_rig(running)


@pytest.fixture
async def app(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter, rig: Rig
) -> AsyncIterator[FastAPI]:
    # A real device manager: GET /pages/{id} resolves a mixer item's "stereo"
    # through it (proskenion/api/pages.py's _stereo), the same dependency
    # proskenion/api/mixer.py's own channel endpoints carry.
    manager = DeviceManager(db, rig.state, rig.bus, config)
    await manager.start()
    application = create_app(
        config,
        db=db,
        tokens=tokens,
        limiter=limiter,
        bus=rig.bus,
        state=rig.state,
        devices_manager=manager,
    )
    application.state.rules = rig.engine
    # core.pages.DefaultPageWatcher is only started as part of the full
    # application lifespan, which these tests never run — so the generated
    # default page (§15.12, §21.9) is built once here instead.
    await pages_crud.regenerate_default_page(db)
    # The real resolver, against the test database — not a stand-in —
    # so tier filtering here proves what the endpoint actually does with it.
    await application.state.hirer_permissions.start(db)
    try:
        yield application
    finally:
        await application.state.hirer_permissions.stop()
        await manager.stop()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with make_client(app) as http:
        yield http


async def login(client: AsyncClient, password: str = ADMIN_PASSWORD) -> Response:
    response = await client.post(f"{AUTH}/login", json={"password": password})
    assert response.status_code == 200, response.text
    return response


async def enable_hirer(client: AsyncClient) -> None:
    response = await client.post(f"{HIRER}/enabled", json={"enabled": True})
    assert response.status_code == 200, response.text


async def sign_in_hirer(client: AsyncClient, pin: str = HIRER_PIN) -> None:
    response = await client.post(f"{AUTH}/hirer", json={"pin": pin})
    assert response.status_code == 200, response.text


def code(response: Response) -> str:
    return str(response.json()["error"]["code"])


def detail(response: Response) -> dict[str, Any]:
    return dict(response.json()["error"]["detail"] or {})


async def until(predicate: Callable[[], bool], within: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


async def assign_pages(app: FastAPI, page_ids: list[int]) -> None:
    """Assign pages to the hirer and wait for the live resolver to catch up."""
    await pages_crud.replace_hirer_pages(app.state.db, page_ids)
    app.state.bus.emit(HirerConfigChanged(reason="test"))
    await until(lambda: set(app.state.hirer_permissions.permissions.pages) == set(page_ids))


async def mixer_channel(
    db: Database,
    *,
    channel_kind: str = "input",
    name: str = "Wireless 1",
    ceiling: float | None = None,
) -> int:
    device = await devices_crud.create(
        db, category="mixer", driver_key="stub_mixer", name="Stub Mixer", config={}
    )
    channel = await mixer_crud.create_channel(
        db, device_id=device.id, channel_kind=channel_kind, name=name, hirer_max_db=ceiling
    )
    return channel.id


# -- GET /pages, GET /pages/{id}: staff -------------------------------------------------


async def test_listing_pages_flags_the_default_page_and_hirer_assignment(
    client: AsyncClient, rig: Rig, app: FastAPI
) -> None:
    await login(client)
    page = await pages_crud.create_page(rig.db, name="Performance")
    await assign_pages(app, [page.id])

    response = await client.get(PAGES)
    assert response.status_code == 200, response.text
    by_id = {p["id"]: p for p in response.json()["pages"]}
    assert by_id[page.id]["hirer"] is True
    default = next(p for p in by_id.values() if p["is_default"])
    assert default["hirer"] is False


async def test_listing_pages_requires_a_session(client: AsyncClient) -> None:
    response = await client.get(PAGES)
    assert response.status_code == 401
    assert code(response) == "unauthenticated"


async def test_reading_a_page_resolves_its_items(client: AsyncClient, rig: Rig) -> None:
    await login(client)
    group_id = rig.venue.groups["Row 1"]
    page = await pages_crud.create_page(rig.db, name="Stage")
    await pages_crud.replace_page(
        rig.db,
        page.id,
        page.updated_at,
        name=page.name,
        sort_order=0,
        items=[PageItemInput(kind="group_master", group_id=group_id)],
    )
    response = await client.get(f"{PAGES}/{page.id}")
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["kind"] == "group_master"
    assert set(item["members"]) == {rig.venue.channels["A"], rig.venue.channels["B"]}
    assert item["tray"] is True
    assert "ceiling_db" not in item.get("group", {})
    assert "members_writable" not in item  # staff-only response never carries it
    assert "member_channels" not in item  # staff resolve members from GET /lighting/channels


async def test_reading_a_missing_page_is_not_found(client: AsyncClient) -> None:
    await login(client)
    response = await client.get(f"{PAGES}/9999")
    assert response.status_code == 404
    assert code(response) == "not_found"


# -- GET /pages, GET /pages/{id}: hirer -------------------------------------------------


async def test_a_hirer_sees_only_assigned_pages_never_the_default_and_no_hirer_field(
    client: AsyncClient, rig: Rig, app: FastAPI
) -> None:
    assigned = await pages_crud.create_page(rig.db, name="Hire")
    unassigned = await pages_crud.create_page(rig.db, name="Staff only")
    await assign_pages(app, [assigned.id])
    await login(client)
    await enable_hirer(client)

    async with make_client(app) as hirer_http:
        await sign_in_hirer(hirer_http)
        response = await hirer_http.get(PAGES)
        assert response.status_code == 200, response.text
        pages = response.json()["pages"]
        assert [p["id"] for p in pages] == [assigned.id]
        assert "hirer" not in pages[0]
        assert unassigned.id not in [p["id"] for p in pages]


async def test_an_unassigned_page_answers_not_found_for_a_hirer_never_permission_denied(
    client: AsyncClient, rig: Rig, app: FastAPI
) -> None:
    other = await pages_crud.create_page(rig.db, name="Staff only")
    await login(client)
    await enable_hirer(client)
    async with make_client(app) as hirer_http:
        await sign_in_hirer(hirer_http)
        response = await hirer_http.get(f"{PAGES}/{other.id}")
        assert response.status_code == 404
        assert code(response) == "not_found"


async def test_a_hirer_gets_ceiling_db_on_a_reachable_mixer_item_and_writable_on_lighting(
    client: AsyncClient, rig: Rig, app: FastAPI
) -> None:
    channel_id = await mixer_channel(rig.db, ceiling=-6.0)
    lighting_channel = rig.venue.channels["A"]
    page = await pages_crud.create_page(rig.db, name="Hire")
    await pages_crud.replace_page(
        rig.db,
        page.id,
        page.updated_at,
        name=page.name,
        sort_order=0,
        items=[
            PageItemInput(kind="channel", channel_id=channel_id),
            PageItemInput(kind="channel", lighting_channel_id=lighting_channel),
        ],
    )
    hirer_config = await hirer_crud.get(rig.db)  # lighting_enabled defaults off (Q3)
    await hirer_crud.update(
        rig.db,
        {"lighting_enabled": True},
        expected_updated_at=hirer_config.updated_at,
        updated_by=None,
    )
    await assign_pages(app, [page.id])
    await login(client)
    await enable_hirer(client)
    async with make_client(app) as hirer_http:
        await sign_in_hirer(hirer_http)
        response = await hirer_http.get(f"{PAGES}/{page.id}")
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        mixer_item = next(i for i in items if i["source"] == "mixer")
        assert mixer_item["channel"]["ceiling_db"] == -6.0
        lighting_item = next(i for i in items if i["source"] == "lighting")
        assert lighting_item["writable"] is True


async def test_a_hirer_gets_member_channels_on_a_reachable_group_tray(
    client: AsyncClient, rig: Rig, app: FastAPI
) -> None:
    """A hirer is never admitted to ``GET /lighting/channels``, so a
    tray's members must be resolvable from the page object alone —
    ``member_channels`` carries exactly the object that endpoint would answer
    for each id in ``members``, in the same order."""
    group_id = rig.venue.groups["Row 1"]
    member_a, member_b = rig.venue.channels["A"], rig.venue.channels["B"]
    page = await pages_crud.create_page(rig.db, name="Hire")
    await pages_crud.replace_page(
        rig.db,
        page.id,
        page.updated_at,
        name=page.name,
        sort_order=0,
        items=[PageItemInput(kind="group_master", group_id=group_id)],
    )
    hirer_config = await hirer_crud.get(rig.db)  # lighting_enabled defaults off (Q3)
    await hirer_crud.update(
        rig.db,
        {"lighting_enabled": True},
        expected_updated_at=hirer_config.updated_at,
        updated_by=None,
    )
    await assign_pages(app, [page.id])
    await login(client)
    await enable_hirer(client)
    async with make_client(app) as hirer_http:
        await sign_in_hirer(hirer_http)
        response = await hirer_http.get(f"{PAGES}/{page.id}")
        assert response.status_code == 200, response.text
        item = next(i for i in response.json()["items"] if i["kind"] == "group_master")
        assert set(item["members"]) == {member_a, member_b}
        assert item["members_writable"] is False  # individual_fixtures defaults off (Q3)
        member_channels = item["member_channels"]
        assert [c["id"] for c in member_channels] == item["members"]  # same order as `members`
        assert {c["name"] for c in member_channels} == {"Fixture A", "Fixture B"}


async def test_an_output_channel_is_omitted_from_a_hirers_view(
    client: AsyncClient, rig: Rig, app: FastAPI
) -> None:
    channel_id = await mixer_channel(rig.db, channel_kind="output", name="Front of house")
    page = await pages_crud.create_page(rig.db, name="Hire")
    await pages_crud.replace_page(
        rig.db,
        page.id,
        page.updated_at,
        name=page.name,
        sort_order=0,
        items=[PageItemInput(kind="channel", channel_id=channel_id)],
    )
    await assign_pages(app, [page.id])
    await login(client)
    await enable_hirer(client)
    async with make_client(app) as hirer_http:
        await sign_in_hirer(hirer_http)
        response = await hirer_http.get(f"{PAGES}/{page.id}")
        assert response.status_code == 200, response.text
        assert response.json()["items"] == []  # the output is never hirer-reachable


async def test_a_saved_page_is_in_force_for_hirers_when_the_save_is_answered(
    client: AsyncClient, rig: Rig, app: FastAPI
) -> None:
    """§6.7: an admin's edit takes effect immediately. The resolver's
    rebuild has run by the time ``PUT /pages/{id}`` answers, so a channel
    taken off a page is out of reach from the very next request."""
    channel_id = await mixer_channel(rig.db, name="Wireless 1")
    page = await pages_crud.create_page(rig.db, name="Hire")
    await pages_crud.replace_page(
        rig.db,
        page.id,
        page.updated_at,
        name=page.name,
        sort_order=0,
        items=[PageItemInput(kind="channel", channel_id=channel_id)],
    )
    await assign_pages(app, [page.id])
    assert app.state.hirer_permissions.permissions.mixer_reachable(channel_id)
    await login(client)
    stored = (await client.get(f"{PAGES}/{page.id}")).json()
    response = await client.put(
        f"{PAGES}/{page.id}",
        json={"name": "Hire", "sort_order": 0, "items": []},
        headers={"If-Unmodified-Since-Version": stored["updated_at"]},
    )
    assert response.status_code == 200, response.text
    assert not app.state.hirer_permissions.permissions.mixer_reachable(channel_id)


# -- POST /pages ------------------------------------------------------------------------


async def test_creating_a_page(client: AsyncClient) -> None:
    await login(client)
    response = await client.post(PAGES, json={"name": "Assembly"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "Assembly"
    assert body["items"] == []
    assert body["is_default"] is False


async def test_creating_a_page_is_admin_only(client: AsyncClient) -> None:
    response = await client.post(PAGES, json={"name": "Assembly"})
    assert response.status_code == 401


# -- PUT /pages/{id} ----------------------------------------------------------------------


async def test_replacing_a_page_with_a_panel_button(client: AsyncClient, rig: Rig) -> None:
    await login(client)
    rule_id = rig.venue.rules["Stage Bank 1"]
    page = await pages_crud.create_page(rig.db, name="Room")
    response = await client.put(
        f"{PAGES}/{page.id}",
        json={
            "name": "Room",
            "sort_order": 1,
            "items": [
                {
                    "kind": "panel",
                    "panel_title": "Stage",
                    "panel_width": 1,
                    "buttons": [
                        {
                            "col": 0,
                            "row": 0,
                            "label": "Bank 1",
                            "rule_id": rule_id,
                            "confirm": False,
                        }
                    ],
                }
            ],
        },
        headers={VERSION: page.updated_at},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sort_order"] == 1
    assert body["items"][0]["buttons"][0]["rule_id"] == rule_id
    # "Stage Bank 1" is a lighting_group rule, not run_scene: it names no device.
    assert body["items"][0]["buttons"][0]["devices"] == []


async def test_an_indicator_only_group_cannot_be_placed_and_is_never_drawn(
    client: AsyncClient, rig: Rig
) -> None:
    """No fader anywhere (migration 011): a page may not place its master, and
    one placed before the group changed is left out of the page as read."""
    await login(client)
    group_id = rig.venue.groups["All Stage"]
    page = await pages_crud.create_page(rig.db, name="Room")
    placed = await pages_crud.replace_page(
        rig.db,
        page.id,
        page.updated_at,
        name="Room",
        sort_order=1,
        items=[PageItemInput(kind="group_master", group_id=group_id)],
    )
    # The binding on "All Stage" has to go before it can become indicator-only.
    await rules_crud.delete_rule(rig.db, rig.venue.rules["All Stage"])
    group = await lighting_crud.get_group(rig.db, group_id)
    assert group is not None
    await lighting_crud.update_group(rig.db, group_id, group.updated_at, indicator_only=True)

    read = await client.get(f"{PAGES}/{page.id}")
    assert read.status_code == 200, read.text
    assert read.json()["items"] == []

    response = await client.put(
        f"{PAGES}/{page.id}",
        json={
            "name": "Room",
            "sort_order": 1,
            "items": [{"kind": "group_master", "group_id": group_id}],
        },
        headers={VERSION: placed.page.updated_at},
    )
    assert response.status_code == 422, response.text
    assert code(response) == "validation_failed"
    assert "indicator-only" in response.json()["error"]["message"]


async def test_a_stale_version_on_put_is_a_conflict_with_the_current_page(
    client: AsyncClient, rig: Rig
) -> None:
    await login(client)
    page = await pages_crud.create_page(rig.db, name="Room")
    stale = page.updated_at
    first = await client.put(
        f"{PAGES}/{page.id}",
        json={"name": "Room renamed", "sort_order": 0, "items": []},
        headers={VERSION: stale},
    )
    assert first.status_code == 200
    again = await client.put(
        f"{PAGES}/{page.id}",
        json={"name": "Room again", "sort_order": 0, "items": []},
        headers={VERSION: stale},
    )
    assert again.status_code == 409
    assert code(again) == "conflict"
    assert detail(again)["current"]["name"] == "Room renamed"


async def test_a_button_naming_a_rule_that_does_not_exist_is_validation_failed(
    client: AsyncClient, rig: Rig
) -> None:
    await login(client)
    page = await pages_crud.create_page(rig.db, name="Room")
    response = await client.put(
        f"{PAGES}/{page.id}",
        json={
            "name": "Room",
            "sort_order": 0,
            "items": [
                {
                    "kind": "panel",
                    "panel_title": "Stage",
                    "panel_width": 1,
                    "buttons": [{"col": 0, "row": 0, "label": "Ghost", "rule_id": 9999}],
                }
            ],
        },
        headers={VERSION: page.updated_at},
    )
    assert response.status_code == 422
    assert code(response) == "validation_failed"
    assert detail(response)["field"] == "Ghost"


async def test_two_buttons_at_the_same_position_is_validation_failed_not_a_500(
    client: AsyncClient, rig: Rig
) -> None:
    """``page_buttons(item_id, col, row)`` is UNIQUE; the database's own
    constraint surfaces as ``ConstraintError``, which must be translated
    like any other stored-shape violation, not left to become a 500."""
    await login(client)
    rule_id = rig.venue.rules["Stage Bank 1"]
    page = await pages_crud.create_page(rig.db, name="Room")
    response = await client.put(
        f"{PAGES}/{page.id}",
        json={
            "name": "Room",
            "sort_order": 0,
            "items": [
                {
                    "kind": "panel",
                    "panel_title": "Stage",
                    "panel_width": 1,
                    "buttons": [
                        {"col": 0, "row": 0, "label": "One", "rule_id": rule_id},
                        {"col": 0, "row": 0, "label": "Two", "rule_id": rule_id},
                    ],
                }
            ],
        },
        headers={VERSION: page.updated_at},
    )
    assert response.status_code == 422, response.text
    assert code(response) == "validation_failed"


async def test_the_default_page_cannot_be_replaced(client: AsyncClient, rig: Rig) -> None:
    await login(client)
    pages = (await client.get(PAGES)).json()["pages"]
    default_page = next(p for p in pages if p["is_default"])
    response = await client.put(
        f"{PAGES}/{default_page['id']}",
        json={"name": "Hacked", "sort_order": 0, "items": []},
        headers={VERSION: default_page["updated_at"]},
    )
    assert response.status_code == 422
    assert detail(response)["reason"] == "default_page"


# -- DELETE /pages/{id} -------------------------------------------------------------------


async def test_deleting_a_page(client: AsyncClient, rig: Rig) -> None:
    await login(client)
    page = await pages_crud.create_page(rig.db, name="Temp")
    response = await client.delete(f"{PAGES}/{page.id}")
    assert response.status_code == 204
    assert (await client.get(f"{PAGES}/{page.id}")).status_code == 404


async def test_deleting_the_default_page_is_refused(client: AsyncClient, rig: Rig) -> None:
    await login(client)
    pages = (await client.get(PAGES)).json()["pages"]
    default_page = next(p for p in pages if p["is_default"])
    response = await client.delete(f"{PAGES}/{default_page['id']}")
    assert response.status_code == 422
    assert detail(response)["reason"] == "default_page"


# -- tray, contiguity and duplicates (§21.9) -----------------------------------------------


async def test_a_clean_group_master_renders_a_tray(client: AsyncClient, rig: Rig) -> None:
    await login(client)
    page = await pages_crud.create_page(rig.db, name="Stage")
    await pages_crud.replace_page(
        rig.db,
        page.id,
        page.updated_at,
        name=page.name,
        sort_order=0,
        items=[PageItemInput(kind="group_master", group_id=rig.venue.groups["Row 1"])],
    )
    response = await client.get(f"{PAGES}/{page.id}")
    assert response.json()["items"][0]["tray"] is True


async def test_a_member_duplicated_far_from_its_master_is_not_contiguous(
    client: AsyncClient, rig: Rig
) -> None:
    await login(client)
    page = await pages_crud.create_page(rig.db, name="Stage")
    await pages_crud.replace_page(
        rig.db,
        page.id,
        page.updated_at,
        name=page.name,
        sort_order=0,
        items=[
            PageItemInput(kind="group_master", group_id=rig.venue.groups["Row 1"]),
            PageItemInput(kind="channel", lighting_channel_id=rig.venue.channels["H"]),
            PageItemInput(kind="channel", lighting_channel_id=rig.venue.channels["A"]),
        ],
    )
    response = await client.get(f"{PAGES}/{page.id}")
    group_item = next(i for i in response.json()["items"] if i["kind"] == "group_master")
    assert group_item["tray"] is False

    findings = (await client.get(f"{PAGES}/{page.id}/validate")).json()["findings"]
    codes = {f["code"] for f in findings}
    assert {"duplicate_member", "not_contiguous"} <= codes


async def test_a_member_duplicated_right_beside_its_master_is_contiguous_but_still_duplicate(
    client: AsyncClient, rig: Rig
) -> None:
    await login(client)
    page = await pages_crud.create_page(rig.db, name="Stage")
    await pages_crud.replace_page(
        rig.db,
        page.id,
        page.updated_at,
        name=page.name,
        sort_order=0,
        items=[
            PageItemInput(kind="group_master", group_id=rig.venue.groups["Row 1"]),
            PageItemInput(kind="channel", lighting_channel_id=rig.venue.channels["A"]),
        ],
    )
    findings = (await client.get(f"{PAGES}/{page.id}/validate")).json()["findings"]
    codes = {f["code"] for f in findings}
    assert "duplicate_member" in codes
    assert "not_contiguous" not in codes  # adjacent: still a duplicate, but not scattered


# -- GET /pages/{id}/validate ---------------------------------------------------------------


async def test_validate_finds_a_dead_rule(client: AsyncClient, rig: Rig) -> None:
    await login(client)
    rule = await rules_crud.create_rule(
        rig.db,
        name="Disabled",
        trigger_type="surface",
        action_type="notify",
        message="hi",
        enabled=False,
    )
    page = await pages_crud.create_page(rig.db, name="Room")
    await pages_crud.replace_page(
        rig.db,
        page.id,
        page.updated_at,
        name=page.name,
        sort_order=0,
        items=[
            PageItemInput(
                kind="panel",
                panel_title="Room",
                panel_width=1,
                buttons=(PageButtonInput(col=0, row=0, label="Dead", rule_id=rule.id),),
            )
        ],
    )

    findings = (await client.get(f"{PAGES}/{page.id}/validate")).json()["findings"]
    codes = {f["code"] for f in findings}
    assert codes == {"dead_rule"}


async def test_validate_never_flags_lamp_missing_for_a_button_with_no_lamp(
    client: AsyncClient, rig: Rig
) -> None:
    """``page_buttons.state_id`` is ``ON DELETE SET NULL`` (migration 006), so
    deleting a derived status clears a button's reference to it rather than
    leaving it dangling — the button simply has no lamp, which is not itself
    a finding: most buttons never carry one."""
    await login(client)
    status = await rules_crud.create_derived_status(
        rig.db,
        name="Temp lamp",
        knx_address_id=None,
        source_type="lighting_group_all_at",
        lighting_group_id=rig.venue.groups["Row 1"],
        compare_level=100,
    )
    page = await pages_crud.create_page(rig.db, name="Room")
    await pages_crud.replace_page(
        rig.db,
        page.id,
        page.updated_at,
        name=page.name,
        sort_order=0,
        items=[
            PageItemInput(
                kind="panel",
                panel_title="Room",
                panel_width=1,
                buttons=(
                    PageButtonInput(
                        col=0,
                        row=0,
                        label="Was lamped",
                        rule_id=rig.venue.rules["Stage Bank 1"],
                        state_id=status.id,
                    ),
                ),
            )
        ],
    )
    await rules_crud.delete_derived_status(rig.db, status.id)

    findings = (await client.get(f"{PAGES}/{page.id}/validate")).json()["findings"]
    assert findings == []


async def test_validate_warns_about_lighting_disabled_for_an_assigned_hirer_page(
    client: AsyncClient, rig: Rig, app: FastAPI
) -> None:
    await login(client)
    page = await pages_crud.create_page(rig.db, name="Hire")
    await pages_crud.replace_page(
        rig.db,
        page.id,
        page.updated_at,
        name=page.name,
        sort_order=0,
        items=[PageItemInput(kind="channel", lighting_channel_id=rig.venue.channels["A"])],
    )
    await assign_pages(app, [page.id])
    config = await hirer_crud.get(rig.db)
    await hirer_crud.update(
        rig.db,
        {"lighting_enabled": False},
        expected_updated_at=config.updated_at,
        updated_by=None,
    )

    findings = (await client.get(f"{PAGES}/{page.id}/validate")).json()["findings"]
    codes = {f["code"] for f in findings}
    assert "lighting_disabled_for_hirer" in codes


async def test_validate_is_admin_only(client: AsyncClient, rig: Rig) -> None:
    page = await pages_crud.create_page(rig.db, name="Room")
    response = await client.get(f"{PAGES}/{page.id}/validate")
    assert response.status_code == 401


# -- POST /pages/{id}/buttons/{bid} ------------------------------------------------------


async def _panel_page(rig: Rig, rule_id: int) -> tuple[int, int]:
    page = await pages_crud.create_page(rig.db, name="Room")
    await pages_crud.replace_page(
        rig.db,
        page.id,
        page.updated_at,
        name=page.name,
        sort_order=0,
        items=[
            PageItemInput(
                kind="panel",
                panel_title="Room",
                panel_width=1,
                buttons=(PageButtonInput(col=0, row=0, label="Go", rule_id=rule_id),),
            )
        ],
    )
    full = await pages_crud.get_page(rig.db, page.id)
    assert full is not None
    return page.id, full.items[0].buttons[0].id


async def test_firing_a_button_as_staff(client: AsyncClient, rig: Rig) -> None:
    await login(client)
    rule_id = rig.venue.rules["Stage Bank 1"]
    page_id, button_id = await _panel_page(rig, rule_id)
    response = await client.post(f"{PAGES}/{page_id}/buttons/{button_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["triggered_by"] == f"page:{button_id}"
    assert body["fired"] is True


async def test_firing_an_unknown_button_is_not_found(client: AsyncClient, rig: Rig) -> None:
    await login(client)
    page = await pages_crud.create_page(rig.db, name="Room")
    response = await client.post(f"{PAGES}/{page.id}/buttons/9999")
    assert response.status_code == 404


async def test_a_hirer_fires_a_button_on_an_assigned_page(
    client: AsyncClient, rig: Rig, app: FastAPI
) -> None:
    scene = await add_scene(rig.db, rig.venue, "Hirer scene")
    rule = await rules_crud.create_rule(
        rig.db,
        name="Hirer rule",
        trigger_type="surface",
        action_type="run_scene",
        scene_id=scene,
    )
    await rig.reload()  # the rules engine's in-memory index must see it too
    page_id, button_id = await _panel_page(rig, rule.id)
    await assign_pages(app, [page_id])
    await login(client)
    await enable_hirer(client)

    async with make_client(app) as hirer_http:
        await sign_in_hirer(hirer_http)
        response = await hirer_http.post(f"{PAGES}/{page_id}/buttons/{button_id}")
        assert response.status_code == 200, response.text
    await until(lambda: len(rig.scenes.calls) >= 1)
    assert rig.scenes.calls[-1].hirer_originated is True
    assert rig.scenes.calls[-1].triggered_by == f"page:{button_id}"


async def test_a_hirer_cannot_fire_a_button_on_an_unassigned_page(
    client: AsyncClient, rig: Rig, app: FastAPI
) -> None:
    rule_id = rig.venue.rules["Stage Bank 1"]
    page_id, button_id = await _panel_page(rig, rule_id)
    await login(client)
    await enable_hirer(client)
    async with make_client(app) as hirer_http:
        await sign_in_hirer(hirer_http)
        response = await hirer_http.post(f"{PAGES}/{page_id}/buttons/{button_id}")
        assert response.status_code == 404
        assert code(response) == "not_found"


# -- rule deletion in_use, end to end, confirmed through the API -----------


async def test_deleting_a_rule_bound_to_a_button_is_in_use(client: AsyncClient, rig: Rig) -> None:
    await login(client)
    rule_id = rig.venue.rules["Stage Bank 1"]
    page_id, button_id = await _panel_page(rig, rule_id)
    response = await client.delete(f"{RULES}/{rule_id}")
    assert response.status_code == 409
    assert code(response) == "in_use"
    references = detail(response)["references"]
    assert any(r["id"] == button_id for r in references)


# -- ActionContext.hirer_originated, end to end through the pages API --------------------


async def test_hirer_originated_reaches_the_scene_engines_action_context(
    config: Config,
    db: Database,
    tokens: TokenService,
    limiter: RateLimiter,
) -> None:
    """The full path: a hirer's button press through the pages API, through
    RulesEngine.fire, through the real SceneEngine, to one action's own
    ActionContext (Phase 5 contracts, "Firing a button", Q8b)."""
    from proskenion.core.bus import EventBus

    bus = EventBus()
    await bus.start()
    state = StateStore(config, bus)
    devices = FakeCapabilities()
    scene_engine = SceneEngine(db, state, devices=devices)
    handler = RecordingHandler(outcome=ActionOutcome.confirmed({"state": "on"}))
    scene_engine.handlers.register("projector_power", handler)
    projector = await devices_crud.create(
        db, category="projector", driver_key="pjlink", name="Projector", config={}
    )
    devices.reports[projector.id] = ProjectorCapabilities(
        inputs=("hdmi1",), supports_authentication=False
    )
    scene = await scenes_crud.create_scene(db, name="Hirer button scene")
    await scenes_crud.create_action(
        db, scene_id=scene.id, sort_order=0, domain="projector_power", projector_power="on"
    )
    rule = await rules_crud.create_rule(
        db, name="Scene rule", trigger_type="surface", action_type="run_scene", scene_id=scene.id
    )
    rules_engine = RulesEngine(db, state, bus, scenes=scene_engine)
    await rules_engine.reload()
    page = await pages_crud.create_page(db, name="Hire")
    await pages_crud.replace_page(
        db,
        page.id,
        page.updated_at,
        name=page.name,
        sort_order=0,
        items=[
            PageItemInput(
                kind="panel",
                panel_title="Room",
                panel_width=1,
                buttons=(PageButtonInput(col=0, row=0, label="Go", rule_id=rule.id),),
            )
        ],
    )
    full = await pages_crud.get_page(db, page.id)
    assert full is not None
    button_id = full.items[0].buttons[0].id
    await pages_crud.replace_hirer_pages(db, [page.id])

    app = create_app(config, db=db, tokens=tokens, limiter=limiter, bus=bus, state=state)
    app.state.rules = rules_engine
    await app.state.hirer_permissions.start(db)
    try:
        async with make_client(app) as admin_http:
            await login(admin_http)
            await enable_hirer(admin_http)

        async with make_client(app) as hirer_http:
            await sign_in_hirer(hirer_http)
            response = await hirer_http.post(f"{PAGES}/{page.id}/buttons/{button_id}")
            assert response.status_code == 200, response.text
        await until(lambda: len(handler.calls) >= 1)
        assert handler.calls[0][2].hirer_originated is True

        async with make_client(app) as staff_http:
            await login(staff_http)
            response = await staff_http.post(f"{PAGES}/{page.id}/buttons/{button_id}")
            assert response.status_code == 200, response.text
        await until(lambda: len(handler.calls) >= 2)
        assert handler.calls[1][2].hirer_originated is False
    finally:
        await app.state.hirer_permissions.stop()
        await scene_engine.stop()
        await bus.stop()


# -- a button's devices -------------------------------------------------------


async def _one_action_scene_button(
    db: Database, *, domain: str, **action_fields: Any
) -> tuple[int, int]:
    """A page with one panel button whose rule runs a one-action scene of
    ``domain``. Returns ``(page_id, button_id)``."""
    scene = await scenes_crud.create_scene(db, name=f"{domain} scene")
    await scenes_crud.create_action(
        db, scene_id=scene.id, sort_order=0, domain=domain, **action_fields
    )
    rule = await rules_crud.create_rule(
        db,
        name=f"{domain} rule",
        trigger_type="surface",
        action_type="run_scene",
        scene_id=scene.id,
    )
    page = await pages_crud.create_page(db, name=f"{domain} page")
    await pages_crud.replace_page(
        db,
        page.id,
        page.updated_at,
        name=page.name,
        sort_order=0,
        items=[
            PageItemInput(
                kind="panel",
                panel_title="Room",
                panel_width=1,
                buttons=(PageButtonInput(col=0, row=0, label="Go", rule_id=rule.id),),
            )
        ],
    )
    full = await pages_crud.get_page(db, page.id)
    assert full is not None
    return page.id, full.items[0].buttons[0].id


async def _button_devices(client: AsyncClient, page_id: int, button_id: int) -> list[str]:
    response = await client.get(f"{PAGES}/{page_id}")
    assert response.status_code == 200, response.text
    for item in response.json()["items"]:
        if item["kind"] != "panel":
            continue
        for button in item["buttons"]:
            if button["id"] == button_id:
                devices: list[str] = list(button["devices"])
                return devices
    raise AssertionError("the button was not found on its own page")


@pytest.mark.parametrize(
    ("domain", "fields", "category", "driver_key", "expected"),
    [
        ("mixer_recall", {"mixer_scene_id": None}, "mixer", "stub_mixer", "mixer"),
        (
            "mixer_fader",
            {"mixer_channel_id": None, "mixer_db": -6.0},
            "mixer",
            "stub_mixer",
            "mixer",
        ),
        (
            "mixer_mute",
            {"mixer_channel_id": None, "mixer_muted": True},
            "mixer",
            "stub_mixer",
            "mixer",
        ),
        ("projector_power", {"projector_power": "on"}, "projector", "pjlink", "projector"),
        ("projector_input", {"projector_input": "hdmi1"}, "projector", "pjlink", "projector"),
        (
            "hdmi_source",
            {"hdmi_destination": None, "hdmi_input_id": None},
            "video_matrix",
            "lkv422",
            "hdmi",
        ),
    ],
)
async def test_a_buttons_devices_follow_its_scenes_action_domain(
    client: AsyncClient,
    rig: Rig,
    domain: str,
    fields: dict[str, Any],
    category: str,
    driver_key: str,
    expected: str,
) -> None:
    """The mixer for ``mixer_*``, the projector for ``projector_*``, the
    matrix for ``hdmi_source`` (contract) — the only device of the domain's
    category, since the action names none."""
    await devices_crud.create(
        rig.db, category=category, driver_key=driver_key, name=category.title(), config={}
    )
    page_id, button_id = await _one_action_scene_button(rig.db, domain=domain, **fields)
    await login(client)
    assert await _button_devices(client, page_id, button_id) == [expected]


async def test_a_mixer_actions_device_names_the_explicit_device_id(
    client: AsyncClient, rig: Rig
) -> None:
    """Two mixer devices configured, an action naming one explicitly: still
    resolves — only an action naming *none* is ambiguous."""
    named = await devices_crud.create(
        rig.db, category="mixer", driver_key="stub_mixer", name="Desk A", config={}
    )
    await devices_crud.create(
        rig.db, category="mixer", driver_key="stub_mixer", name="Desk B", config={}
    )
    page_id, button_id = await _one_action_scene_button(
        rig.db, domain="mixer_recall", mixer_scene_id=None, device_id=named.id
    )
    await login(client)
    assert await _button_devices(client, page_id, button_id) == ["mixer"]


async def test_a_mixer_actions_device_is_ambiguous_with_two_devices_and_none_named(
    client: AsyncClient, rig: Rig
) -> None:
    await devices_crud.create(
        rig.db, category="mixer", driver_key="stub_mixer", name="Desk A", config={}
    )
    await devices_crud.create(
        rig.db, category="mixer", driver_key="stub_mixer", name="Desk B", config={}
    )
    page_id, button_id = await _one_action_scene_button(
        rig.db, domain="mixer_recall", mixer_scene_id=None
    )
    await login(client)
    assert await _button_devices(client, page_id, button_id) == []


async def test_a_dmx_actions_device_is_the_lighting_output_it_snapshots(
    client: AsyncClient, rig: Rig
) -> None:
    fixture = rig.venue.channels["A"]  # patched to rig.venue.output (lighting_output)
    page_id, button_id = await _one_action_scene_button(
        rig.db, domain="dmx", dmx_snapshot={str(fixture): {"level": 80.0}}, dmx_fade_ms=0
    )
    await login(client)
    assert await _button_devices(client, page_id, button_id) == ["dmx"]


async def test_a_dmx_action_with_an_empty_snapshot_names_no_device(
    client: AsyncClient, rig: Rig
) -> None:
    page_id, button_id = await _one_action_scene_button(rig.db, domain="dmx", dmx_snapshot={})
    await login(client)
    assert await _button_devices(client, page_id, button_id) == []


async def test_a_knx_actions_device_is_always_knx(client: AsyncClient, rig: Rig) -> None:
    """KNX has no ``devices`` table row (B42): the fixed status-bar slot."""
    address_id = rig.venue.addresses["1/1/20"]
    page_id, button_id = await _one_action_scene_button(
        rig.db, domain="knx", knx_address_id=address_id, knx_value="1", knx_source="literal"
    )
    await login(client)
    assert await _button_devices(client, page_id, button_id) == ["knx"]


async def test_a_lighting_group_rules_button_names_no_device(client: AsyncClient, rig: Rig) -> None:
    """A ``lighting_group`` rule runs no scene: the button still fires and
    lamps normally, but names no device — every wall-panel bank button in
    the rig is one of these."""
    rule_id = rig.venue.rules["Stage Bank 1"]
    page = await pages_crud.create_page(rig.db, name="Room")
    await pages_crud.replace_page(
        rig.db,
        page.id,
        page.updated_at,
        name=page.name,
        sort_order=0,
        items=[
            PageItemInput(
                kind="panel",
                panel_title="Stage",
                panel_width=1,
                buttons=(PageButtonInput(col=0, row=0, label="Bank 1", rule_id=rule_id),),
            )
        ],
    )
    full = await pages_crud.get_page(rig.db, page.id)
    assert full is not None
    await login(client)
    assert await _button_devices(client, page.id, full.items[0].buttons[0].id) == []


async def test_a_scenes_several_action_domains_produce_several_sorted_devices(
    client: AsyncClient, rig: Rig
) -> None:
    await devices_crud.create(
        rig.db, category="mixer", driver_key="stub_mixer", name="Mixer", config={}
    )
    await devices_crud.create(
        rig.db, category="projector", driver_key="pjlink", name="Projector", config={}
    )
    scene = await scenes_crud.create_scene(rig.db, name="Two domains")
    await scenes_crud.create_action(
        rig.db, scene_id=scene.id, sort_order=0, domain="mixer_recall", mixer_scene_id=None
    )
    await scenes_crud.create_action(
        rig.db,
        scene_id=scene.id,
        sort_order=1,
        delay_ms=2000,
        domain="projector_power",
        projector_power="on",
    )
    rule = await rules_crud.create_rule(
        rig.db,
        name="Two domains rule",
        trigger_type="surface",
        action_type="run_scene",
        scene_id=scene.id,
    )
    page = await pages_crud.create_page(rig.db, name="Two domains page")
    await pages_crud.replace_page(
        rig.db,
        page.id,
        page.updated_at,
        name=page.name,
        sort_order=0,
        items=[
            PageItemInput(
                kind="panel",
                panel_title="Room",
                panel_width=1,
                buttons=(PageButtonInput(col=0, row=0, label="Go", rule_id=rule.id),),
            )
        ],
    )
    full = await pages_crud.get_page(rig.db, page.id)
    assert full is not None
    await login(client)
    devices = await _button_devices(client, page.id, full.items[0].buttons[0].id)
    assert devices == ["mixer", "projector"]  # sorted


async def test_a_buttons_devices_are_recomputed_on_scene_action_and_rule_change(
    client: AsyncClient, rig: Rig, app: FastAPI
) -> None:
    """No caching: the very next request after a scene or rule edit already
    reflects it (the ``pages_changed`` frame this also triggers is
    ``proskenion.core.pages.PagesChangedBroadcaster``'s job, tested there)."""
    await devices_crud.create(
        rig.db, category="projector", driver_key="pjlink", name="Projector", config={}
    )
    page_id, button_id = await _one_action_scene_button(
        rig.db, domain="projector_power", projector_power="on"
    )
    await login(client)
    assert await _button_devices(client, page_id, button_id) == ["projector"]

    scene = next(iter(await scenes_crud.list_scenes(rig.db)))
    actions = await scenes_crud.list_actions(rig.db, scene.id)
    await scenes_crud.delete_action(rig.db, actions[0].id)
    assert await _button_devices(client, page_id, button_id) == []
