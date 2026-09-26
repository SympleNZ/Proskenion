"""The hirer PIN and kill switch over REST, the certificate field, and the PIN limiter.

Spec §6.4, §6.6, §6.8, §6.14, §6.16 and §16.3, with the phase-5 contract's
``POST /hirer/pin``, ``POST /hirer/enabled`` and ``GET /auth/session``'s
``certificate``. Q7: disabling bumps token_version, enabling never does.
Q11: the PIN is exactly six digits.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from proskenion.api.app import API_PREFIX
from proskenion.api.deps import current_session
from proskenion.config import AppSection, Config, Environment, ServerSection
from proskenion.core.auth import TokenClaims, TokenService
from proskenion.core.broadcast import CLOSE_ACCESS_REVOKED, Broadcaster
from proskenion.core.hirer_access import HirerAccess
from proskenion.core.ratelimit import (
    CLEAR_LOCKOUTS_FILENAME,
    LoginFailuresExceeded,
    RateLimiter,
)
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import hirer as hirer_crud
from proskenion.db.crud import mixer as mixer_crud
from proskenion.db.crud import pages as pages_crud
from proskenion.db.crud import rules as rules_crud
from proskenion.db.crud import scenes as scenes_crud
from proskenion.db.crud import security_events
from proskenion.db.crud.pages import PageButtonInput, PageItemInput
from proskenion.db.crud.users import PLACEHOLDER_HASH_PREFIX
from tests.unit.api.conftest import (
    ADMIN_PASSWORD,
    HIRER_PIN,
    OPERATOR_PASSWORD,
    FakeClock,
    build_app,
    make_client,
)
from tests.unit.core.test_certs_trust import write_ca_issued, write_self_signed

AUTH = f"{API_PREFIX}/auth"
HIRER = f"{API_PREFIX}/hirer"
WHOAMI = f"{API_PREFIX}/probe/whoami"
HIRER_WRITE = f"{API_PREFIX}/probe/hirer-write"
VERSION_HEADER = "If-Unmodified-Since-Version"
WAIT_S = 5.0


# -- helpers --------------------------------------------------------------------------


def error(response: Response) -> dict[str, Any]:
    body: dict[str, Any] = response.json()["error"]
    return body


async def until(condition: Callable[[], bool]) -> None:
    """Wait for ``condition`` against a deadline — never a fixed sleep."""
    async with asyncio.timeout(WAIT_S):
        while not condition():  # noqa: ASYNC110 - conditions span the app and the clients
            await asyncio.sleep(0.001)


async def rows(db: Database, event_type: str) -> list[security_events.SecurityEvent]:
    return await security_events.query(db, event_type=event_type)


def access_of(app: FastAPI) -> HirerAccess:
    access: HirerAccess = app.state.hirer_access
    return access


def broadcaster_of(app: FastAPI) -> Broadcaster:
    broadcaster: Broadcaster = app.state.broadcaster
    return broadcaster


def client_from(app: ASGIApp, host: str) -> AsyncClient:
    """A client whose TCP peer is ``host`` — nginx is ``127.0.0.1``; anyone else is not."""
    transport = ASGITransport(app=app, raise_app_exceptions=False, client=(host, 40000))
    return AsyncClient(transport=transport, base_url="https://test")


@pytest.fixture
async def admin(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with make_client(app) as http:
        response = await http.post(f"{AUTH}/login", json={"password": ADMIN_PASSWORD})
        assert response.status_code == 200
        yield http


async def enable(admin: AsyncClient) -> None:
    response = await admin.post(f"{HIRER}/enabled", json={"enabled": True})
    assert response.status_code == 200, response.text


async def sign_in_hirer(http: AsyncClient, pin: str = HIRER_PIN) -> None:
    response = await http.post(f"{AUTH}/hirer", json={"pin": pin})
    assert response.status_code == 200, response.text


# -- who may call them ------------------------------------------------------------------


async def test_only_the_admin_may_change_the_pin_or_the_switch(
    app: FastAPI, admin: AsyncClient, client: AsyncClient
) -> None:
    await enable(admin)
    async with make_client(app) as operator:
        await operator.post(f"{AUTH}/login", json={"password": OPERATOR_PASSWORD})
        for path, body in (
            (f"{HIRER}/pin", {"generate": True}),
            (f"{HIRER}/enabled", {"enabled": False}),
        ):
            response = await operator.post(path, json=body)
            assert response.status_code == 403
            assert error(response)["code"] == "permission_denied"
    await sign_in_hirer(client)
    response = await client.post(f"{HIRER}/enabled", json={"enabled": False})
    assert response.status_code == 403
    assert access_of(app).enabled  # the hirer did not switch themselves off


async def test_anonymous_calls_are_unauthenticated(client: AsyncClient) -> None:
    response = await client.post(f"{HIRER}/enabled", json={"enabled": False})
    assert response.status_code == 401


# -- POST /hirer/pin ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"pin": "12345"},
        {"pin": "1234567"},
        {"pin": "12a456"},
        {"pin": "١٢٣٤٥٦"},
        {"pin": 123456},
        {},
        {"generate": False},
        {"pin": "123456", "generate": True},
        {"pin": "123456", "extra": 1},
    ],
)
async def test_the_pin_is_exactly_six_digits_or_generated(
    admin: AsyncClient, db: Database, body: dict[str, Any]
) -> None:
    before = await hirer_crud.get(db)
    response = await admin.post(f"{HIRER}/pin", json=body)
    assert response.status_code == 422
    assert error(response)["code"] == "validation_failed"
    assert await hirer_crud.get(db) == before


async def test_a_set_pin_works_and_the_old_one_does_not(
    admin: AsyncClient, client: AsyncClient, db: Database
) -> None:
    await enable(admin)
    response = await admin.post(f"{HIRER}/pin", json={"pin": "135790"})
    assert response.status_code == 200
    assert response.json() == {"sessions_closed": 0}  # never echoes a chosen PIN
    old = await client.post(f"{AUTH}/hirer", json={"pin": HIRER_PIN})
    assert old.status_code == 401
    await sign_in_hirer(client, "135790")
    [changed] = await rows(db, "pin_changed")
    assert changed.user_ident == "admin"
    assert json.loads(changed.detail or "{}")["generated"] is False


async def test_a_generated_pin_is_shown_once_and_works(
    admin: AsyncClient, client: AsyncClient
) -> None:
    await enable(admin)
    response = await admin.post(f"{HIRER}/pin", json={"generate": True})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"pin", "sessions_closed"}
    pin = body["pin"]
    assert isinstance(pin, str) and len(pin) == 6 and pin.isascii() and pin.isdigit()
    await sign_in_hirer(client, pin)


async def test_a_pin_change_closes_hirer_sockets_and_logs_each_session(
    app: FastAPI, admin: AsyncClient, db: Database
) -> None:
    await enable(admin)
    phone = broadcaster_of(app).connect(tier="hirer", session_id="phone", address="10.0.0.31")
    booth = broadcaster_of(app).connect(tier="operator", address="10.0.0.5")
    response = await admin.post(f"{HIRER}/pin", json={"pin": "135790"})
    assert response.json() == {"sessions_closed": 1}
    assert phone.closed and phone.close_code == CLOSE_ACCESS_REVOKED
    assert not booth.closed
    [logout] = await rows(db, "forced_logout")
    assert logout.ip_address == "10.0.0.31"
    assert json.loads(logout.detail or "{}")["session_id"] == "phone"


# -- POST /hirer/enabled --------------------------------------------------------------------


async def test_enabling_over_the_placeholder_pin_is_refused(
    admin: AsyncClient, db: Database
) -> None:
    await hirer_crud.set_pin_hash(db, PLACEHOLDER_HASH_PREFIX + "seed", updated_by=None)
    response = await admin.post(f"{HIRER}/enabled", json={"enabled": True})
    assert response.status_code == 422
    assert error(response)["code"] == "validation_failed"
    assert error(response)["detail"]["reason"] == "placeholder_pin"
    assert (await hirer_crud.get(db)).enabled is False


@pytest.mark.parametrize("body", [{}, {"enabled": "false"}, {"enabled": 0}, {"enabled": None}])
async def test_the_switch_takes_a_real_boolean(admin: AsyncClient, body: dict[str, Any]) -> None:
    response = await admin.post(f"{HIRER}/enabled", json=body)
    assert response.status_code == 422


async def test_the_switch_answers_and_audits(
    app: FastAPI, admin: AsyncClient, db: Database
) -> None:
    await enable(admin)
    before = access_of(app).token_version
    phone = broadcaster_of(app).connect(tier="hirer", session_id="phone", address="10.0.0.31")
    tablet = broadcaster_of(app).connect(tier="hirer", session_id="tablet", address="10.0.0.32")

    response = await admin.post(f"{HIRER}/enabled", json={"enabled": False})
    assert response.status_code == 200
    assert response.json() == {"enabled": False, "sessions_closed": 2}
    assert phone.close_code == tablet.close_code == CLOSE_ACCESS_REVOKED
    assert access_of(app).token_version == before + 1  # Q7
    assert (await hirer_crud.get(db)).token_version == before + 1

    again = await admin.post(f"{HIRER}/enabled", json={"enabled": True})
    assert again.json() == {"enabled": True, "sessions_closed": 0}
    assert access_of(app).token_version == before + 1  # enabling never bumps

    toggles = [json.loads(r.detail or "{}")["enabled"] for r in await rows(db, "access_toggled")]
    assert sorted(toggles) == [False, True, True]  # the fixture's enable, then these two
    logouts = await rows(db, "forced_logout")
    assert {json.loads(r.detail or "{}")["session_id"] for r in logouts} == {"phone", "tablet"}
    assert {r.ip_address for r in logouts} == {"10.0.0.31", "10.0.0.32"}


async def test_disabling_refuses_the_next_rest_call_and_new_sign_ins(
    admin: AsyncClient, client: AsyncClient
) -> None:
    await enable(admin)
    await sign_in_hirer(client)
    assert (await client.get(WHOAMI)).status_code == 200
    await admin.post(f"{HIRER}/enabled", json={"enabled": False})
    response = await client.get(WHOAMI)
    assert response.status_code == 401
    assert error(response)["detail"]["reason"] == "hirer_revoked"
    refused = await client.post(f"{AUTH}/hirer", json={"pin": HIRER_PIN})
    assert refused.status_code == 403
    assert error(refused)["detail"]["reason"] == "hirer_disabled"


async def test_rest_checks_do_not_read_the_database(
    app: FastAPI,
    admin: AsyncClient,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§6.4: a hirer's session is checked from state.hirer, not a query."""
    await enable(admin)
    await sign_in_hirer(client)

    async def no_reads(_: Database) -> hirer_crud.HirerConfig:
        raise AssertionError("the hirer session check read the database")

    monkeypatch.setattr(hirer_crud, "get", no_reads)
    for _ in range(3):
        assert (await client.get(WHOAMI)).status_code == 200


# -- the race over REST ---------------------------------------------------------------------


class RecordResponseStart:
    """Notes the moment the switch's response starts to leave the server."""

    def __init__(self, app: ASGIApp, order: list[str]) -> None:
        self.app = app
        self.order = order

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def recording(message: Message) -> None:
            if message["type"] == "http.response.start" and scope["path"] == f"{HIRER}/enabled":
                self.order.append("switch answered")
            await send(message)

        await self.app(scope, receive, recording)


async def test_a_rest_write_racing_the_switch_lands_before_its_answer_or_not_at_all(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    app = build_app(config, db, tokens, limiter)
    order: list[str] = []
    applying = asyncio.Event()
    finish = asyncio.Event()

    @app.post(HIRER_WRITE)
    async def hirer_write(
        claims: Annotated[TokenClaims, Depends(current_session)],
    ) -> dict[str, str]:
        applying.set()
        await finish.wait()  # a write still being applied when the switch lands
        order.append("hirer write applied")
        return {"tier": claims.tier}

    recorded = RecordResponseStart(app, order)
    access = access_of(app)
    async with (
        client_from(recorded, "127.0.0.1") as admin,
        client_from(recorded, "127.0.0.1") as hirer,
    ):
        await admin.post(f"{AUTH}/login", json={"password": ADMIN_PASSWORD})
        await enable(admin)
        await sign_in_hirer(hirer)
        order.clear()  # the enable above answered on the same path

        write = asyncio.create_task(hirer.post(HIRER_WRITE))
        await applying.wait()
        assert access.in_flight == 1
        switch = asyncio.create_task(admin.post(f"{HIRER}/enabled", json={"enabled": False}))
        await until(lambda: not access.enabled)  # the switch has landed

        # A request arriving now is refused outright...
        late = await hirer.post(HIRER_WRITE)
        assert late.status_code == 401
        assert error(late)["detail"]["reason"] == "hirer_revoked"
        # ...and the switch cannot answer while the admitted write is running:
        # it reaches the drain and waits there.
        await until(lambda: access.draining or switch.done())
        assert not switch.done()
        assert access.draining
        assert order == []

        finish.set()
        assert (await write).status_code == 200
        assert (await switch).status_code == 200
    assert order == ["hirer write applied", "switch answered"]


# -- GET /auth/session's certificate (§6.16, Q10) ----------------------------------------


async def session_certificate(app: FastAPI) -> str:
    async with make_client(app) as http:
        await http.post(f"{AUTH}/login", json={"password": ADMIN_PASSWORD})
        response = await http.get(f"{AUTH}/session")
        assert response.status_code == 200
        value: str = response.json()["certificate"]
        return value


def with_app(config: Config, **app: Any) -> Config:
    return config.model_copy(update={"app": config.app.model_copy(update=app)})


async def test_certificate_without_one_installed_is_self_signed_in_production(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter, tmp_path: Path
) -> None:
    production = with_app(config, data_dir=tmp_path / "data")
    assert await session_certificate(build_app(production, db, tokens, limiter)) == "self_signed"


async def test_certificate_on_a_development_machine_is_trusted(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter, tmp_path: Path
) -> None:
    development = with_app(config, environment=Environment.DEVELOPMENT, data_dir=tmp_path)
    assert await session_certificate(build_app(development, db, tokens, limiter)) == "trusted"


@pytest.mark.parametrize(("issuer", "expected"), [("self", "self_signed"), ("ca", "trusted")])
async def test_certificate_follows_what_nginx_serves(
    config: Config,
    db: Database,
    tokens: TokenService,
    limiter: RateLimiter,
    tmp_path: Path,
    issuer: str,
    expected: str,
) -> None:
    hostname = "av.school.nz"
    served = config.model_copy(
        update={
            "server": ServerSection(hostname=hostname),
            "app": AppSection(state_dir=config.app.state_dir, data_dir=tmp_path / "data"),
        }
    )
    write = write_self_signed if issuer == "self" else write_ca_issued
    write(tmp_path / "data", hostname)
    assert await session_certificate(build_app(served, db, tokens, limiter)) == expected


# -- the PIN limiter under forged forwarding headers (§6.8, §4.13) -------------------------


FORGED = [
    {"X-Forwarded-For": "198.51.100.1"},
    {"X-Real-IP": "198.51.100.2"},
    {"X-Forwarded-For": "198.51.100.3", "X-Real-IP": "198.51.100.4"},
    {"X-Forwarded-For": "127.0.0.1"},
    {"X-Real-IP": "127.0.0.1", "X-Forwarded-For": "10.0.0.1, 10.0.0.2"},
]


async def test_forged_headers_from_an_untrusted_peer_cannot_dodge_the_lockout(
    app: FastAPI, admin: AsyncClient, clock: FakeClock, db: Database
) -> None:
    await enable(admin)
    async with client_from(app, "203.0.113.9") as attacker:
        for headers in FORGED[:3]:
            wrong = await attacker.post(f"{AUTH}/hirer", json={"pin": "000000"}, headers=headers)
            assert wrong.status_code == 401
        # Every later attempt, whatever it claims to be, is the same address.
        for headers in FORGED:
            locked = await attacker.post(f"{AUTH}/hirer", json={"pin": HIRER_PIN}, headers=headers)
            assert locked.status_code == 429
            assert error(locked)["code"] == "rate_limited"
        clock.advance(minutes=29)
        still = await attacker.post(f"{AUTH}/hirer", json={"pin": HIRER_PIN}, headers=FORGED[1])
        assert still.status_code == 429
        clock.advance(minutes=1)
        lifted = await attacker.post(f"{AUTH}/hirer", json={"pin": HIRER_PIN}, headers=FORGED[2])
        assert lifted.status_code == 200
    [lockout] = await rows(db, "lockout")
    assert lockout.ip_address == "203.0.113.9"
    failures = await rows(db, "login_failure")
    assert {f.ip_address for f in failures} == {"203.0.113.9"}


async def test_a_forged_address_cannot_reset_a_lockout(app: FastAPI, admin: AsyncClient) -> None:
    """A correct PIN from a locked-out peer, claiming another address, clears nothing."""
    await enable(admin)
    async with client_from(app, "203.0.113.9") as attacker:
        for _ in range(3):
            await attacker.post(f"{AUTH}/hirer", json={"pin": "000000"})
        for headers in FORGED:
            response = await attacker.post(
                f"{AUTH}/hirer", json={"pin": HIRER_PIN}, headers=headers
            )
            assert response.status_code == 429
        # Still locked: nothing above counted as a success for this address.
        response = await attacker.post(f"{AUTH}/hirer", json={"pin": HIRER_PIN})
        assert response.status_code == 429


async def test_behind_nginx_the_client_cannot_forge_its_way_out(
    app: FastAPI, admin: AsyncClient, db: Database
) -> None:
    """nginx sets X-Real-IP from $remote_addr and appends to X-Forwarded-For:
    whatever the client put in X-Forwarded-For, the real address decides."""
    await enable(admin)
    async with client_from(app, "127.0.0.1") as via_nginx:
        for forged in ("10.9.9.1", "10.9.9.2", "10.9.9.3"):
            wrong = await via_nginx.post(
                f"{AUTH}/hirer",
                json={"pin": "000000"},
                headers={"X-Real-IP": "198.51.100.7", "X-Forwarded-For": f"{forged}, 198.51.100.7"},
            )
            assert wrong.status_code == 401
        locked = await via_nginx.post(
            f"{AUTH}/hirer",
            json={"pin": HIRER_PIN},
            headers={"X-Real-IP": "198.51.100.7", "X-Forwarded-For": "10.9.9.4, 198.51.100.7"},
        )
        assert locked.status_code == 429
        # Without X-Real-IP, the rightmost entry nginx appended is the address.
        for forged in ("10.8.8.1", "10.8.8.2", "10.8.8.3"):
            await via_nginx.post(
                f"{AUTH}/hirer",
                json={"pin": "000000"},
                headers={"X-Forwarded-For": f"{forged}, 198.51.100.8"},
            )
        locked = await via_nginx.post(
            f"{AUTH}/hirer",
            json={"pin": HIRER_PIN},
            headers={"X-Forwarded-For": "10.8.8.4, 198.51.100.8"},
        )
        assert locked.status_code == 429
    assert {r.ip_address for r in await rows(db, "lockout")} == {"198.51.100.7", "198.51.100.8"}


async def test_the_hourly_alert_fires_once_and_forged_headers_do_not_split_it(
    config: Config, db: Database, tokens: TokenService, clock: FakeClock
) -> None:
    alerts: list[LoginFailuresExceeded] = []
    limiter = RateLimiter(
        clock=clock.monotonic,
        on_alert=alerts.append,
        signal_path=config.app.state_dir / CLEAR_LOCKOUTS_FILENAME,
    )
    app = build_app(config, db, tokens, limiter)
    await hirer_crud.set_enabled(db, True, updated_by=None)
    async with client_from(app, "203.0.113.9") as attacker:

        async def fail(path: str, body: dict[str, str], n: int) -> None:
            for i in range(n):
                forged = FORGED[i % len(FORGED)]
                response = await attacker.post(f"{AUTH}/{path}", json=body, headers=forged)
                assert response.status_code == 401

        await fail("hirer", {"pin": "000000"}, 3)  # then locked out for 30 minutes
        await fail("login", {"password": "wrong"}, 5)  # then locked out for 15 minutes
        assert alerts == []
        clock.advance(minutes=16)
        await fail("login", {"password": "wrong"}, 2)  # ten failures: not yet
        assert alerts == []
        await fail("login", {"password": "wrong"}, 1)  # the eleventh within the hour
        assert alerts == [LoginFailuresExceeded(ip="203.0.113.9", count=11)]
        clock.advance(minutes=16)
        await fail("login", {"password": "wrong"}, 2)
        assert len(alerts) == 1  # once per address per hour


# -- GET/PUT /hirer/config and GET /hirer/conflicts (Phase 5 contracts) -----------------


async def _seed_reachable(db: Database) -> dict[str, int]:
    """A mixer device with an input, Main and an output, one page assigned to
    the hirer holding the input and Main (never the output, Q4), a second,
    unassigned page holding a second input, and the generated default page."""
    ids: dict[str, int] = {}
    device = await devices_crud.create(
        db, category="mixer", driver_key="stub", name="Desk", config={}
    )
    ids["device"] = device.id
    for key, kind, ceiling in (
        ("in1", "input", -6.0),
        ("in2", "input", None),
        ("main", "main", 0.0),
        ("out1", "output", None),
    ):
        channel = await mixer_crud.create_channel(
            db, device_id=device.id, channel_kind=kind, name=key, hirer_max_db=ceiling
        )
        ids[key] = channel.id
    page = await pages_crud.create_page(db, name="Performance", sort_order=1)
    ids["page"] = page.id
    await pages_crud.replace_page(
        db,
        page.id,
        page.updated_at,
        name=page.name,
        sort_order=page.sort_order,
        items=[
            PageItemInput(kind="channel", channel_id=ids["in1"]),
            PageItemInput(kind="channel", channel_id=ids["main"]),
            PageItemInput(kind="channel", channel_id=ids["out1"]),
        ],
    )
    other = await pages_crud.create_page(db, name="Other", sort_order=2)
    ids["other_page"] = other.id
    await pages_crud.replace_page(
        db,
        other.id,
        other.updated_at,
        name=other.name,
        sort_order=other.sort_order,
        items=[PageItemInput(kind="channel", channel_id=ids["in2"])],
    )
    default = await pages_crud.regenerate_default_page(db)
    ids["default_page"] = default.page.id
    await pages_crud.replace_hirer_pages(db, [page.id])
    return ids


async def _add_scene_button(
    db: Database, page_id: int, *, label: str, rule_id: int, col: int
) -> None:
    """Add one more panel button to a page that may already have a panel —
    each button needs its own panel item here since the page has no panel yet."""
    page = await pages_crud.get_page(db, page_id)
    assert page is not None
    panels = [item for item in page.items if item.kind == "panel"]
    other_items = [
        PageItemInput(
            kind=item.kind,
            channel_id=item.channel_id,
            lighting_channel_id=item.lighting_channel_id,
            group_id=item.group_id,
            expanded=item.expanded,
            panel_title=item.panel_title,
            panel_width=item.panel_width,
            buttons=tuple(
                PageButtonInput(
                    col=b.col,
                    row=b.row,
                    label=b.label,
                    rule_id=b.rule_id,
                    state_id=b.state_id,
                    colour=b.colour,
                    confirm=b.confirm,
                )
                for b in item.buttons
            ),
        )
        for item in page.items
        if item.kind != "panel"
    ]
    existing_buttons = [b for item in panels for b in item.buttons]
    buttons = (
        *(
            PageButtonInput(
                col=b.col,
                row=b.row,
                label=b.label,
                rule_id=b.rule_id,
                state_id=b.state_id,
                colour=b.colour,
                confirm=b.confirm,
            )
            for b in existing_buttons
        ),
        PageButtonInput(col=col, row=0, label=label, rule_id=rule_id),
    )
    await pages_crud.replace_page(
        db,
        page_id,
        page.page.updated_at,
        name=page.page.name,
        sort_order=page.page.sort_order,
        items=[
            *other_items,
            PageItemInput(kind="panel", panel_title="Room", panel_width=4, buttons=buttons),
        ],
    )


async def _seed_conflicts(db: Database) -> dict[str, int]:
    """:func:`_seed_reachable`, plus a desk scene recalled once above its
    ceiling, one never recalled, and a scene action above its ceiling — one
    button per scene, on the assigned page (Q2: reach is button -> rule ->
    scene)."""
    ids = await _seed_reachable(db)

    band = await mixer_crud.create_desk_scene(
        db, device_id=ids["device"], scene_ref="2", name="Band"
    )
    ids["band_scene"] = band.id
    never = await mixer_crud.create_desk_scene(
        db, device_id=ids["device"], scene_ref="3", name="Unchecked"
    )
    ids["never_scene"] = never.id

    recall_band = await scenes_crud.create_scene(db, name="Recall Band")
    await scenes_crud.create_action(
        db, scene_id=recall_band.id, sort_order=0, domain="mixer_recall", mixer_scene_id=band.id
    )
    recall_never = await scenes_crud.create_scene(db, name="Recall Unchecked")
    await scenes_crud.create_action(
        db, scene_id=recall_never.id, sort_order=0, domain="mixer_recall", mixer_scene_id=never.id
    )
    fader_scene = await scenes_crud.create_scene(db, name="Interval")
    fader_action = await scenes_crud.create_action(
        db,
        scene_id=fader_scene.id,
        sort_order=0,
        domain="mixer_fader",
        mixer_channel_id=ids["in1"],
        mixer_db=0.0,  # above the -6.0 dB ceiling
    )
    ids["fader_scene"], ids["fader_action"] = fader_scene.id, fader_action.id

    rule_band = await rules_crud.create_rule(
        db, name="Band", trigger_type="surface", action_type="run_scene", scene_id=recall_band.id
    )
    rule_never = await rules_crud.create_rule(
        db,
        name="Unchecked",
        trigger_type="surface",
        action_type="run_scene",
        scene_id=recall_never.id,
    )
    rule_interval = await rules_crud.create_rule(
        db,
        name="Interval",
        trigger_type="surface",
        action_type="run_scene",
        scene_id=fader_scene.id,
    )
    await _add_scene_button(db, ids["page"], label="Band", rule_id=rule_band.id, col=0)
    await _add_scene_button(db, ids["page"], label="Unchecked", rule_id=rule_never.id, col=1)
    await _add_scene_button(db, ids["page"], label="Interval", rule_id=rule_interval.id, col=2)
    return ids


def _ceiling_map(config: dict[str, Any]) -> dict[int, float | None]:
    return {c["channel_id"]: c["hirer_max_db"] for c in config["ceilings"]}


# -- tier gating --------------------------------------------------------------------


async def test_only_the_admin_may_reach_config_or_conflicts(
    app: FastAPI, admin: AsyncClient, client: AsyncClient
) -> None:
    put_body = {
        "pages": [],
        "ceilings": [],
        "lighting_enabled": False,
        "individual_fixtures": False,
        "colour_enabled": False,
    }
    async with make_client(app) as operator:
        await operator.post(f"{AUTH}/login", json={"password": OPERATOR_PASSWORD})
        for method, path, body in (
            ("GET", f"{HIRER}/config", None),
            ("PUT", f"{HIRER}/config", put_body),
            ("GET", f"{HIRER}/conflicts", None),
        ):
            response = await operator.request(method, path, json=body)
            assert response.status_code == 403
            assert error(response)["code"] == "permission_denied"
    for method, path, body in (
        ("GET", f"{HIRER}/config", None),
        ("PUT", f"{HIRER}/config", put_body),
        ("GET", f"{HIRER}/conflicts", None),
    ):
        response = await client.request(method, path, json=body)
        assert response.status_code == 401


# -- GET /hirer/config ----------------------------------------------------------------


async def test_get_config_lists_the_assigned_page_and_reachable_ceilings(
    admin: AsyncClient, db: Database
) -> None:
    ids = await _seed_reachable(db)
    response = await admin.get(f"{HIRER}/config")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enabled"] is False
    assert body["pin_is_placeholder"] is False
    assert body["pages"] == [ids["page"]]
    assert _ceiling_map(body) == {ids["in1"]: -6.0, ids["main"]: 0.0}  # never the output
    ceilings_by_id = {c["channel_id"]: c for c in body["ceilings"]}
    assert ceilings_by_id[ids["in1"]]["channel_kind"] == "input"
    assert ceilings_by_id[ids["main"]]["channel_kind"] == "main"
    assert body["lighting_enabled"] is False
    assert isinstance(body["updated_at"], str) and body["updated_at"]


# -- PUT /hirer/config ------------------------------------------------------------------


async def test_put_config_writes_pages_ceilings_and_switches_in_one_transaction(
    admin: AsyncClient, db: Database
) -> None:
    ids = await _seed_reachable(db)
    current = (await admin.get(f"{HIRER}/config")).json()
    body = {
        "pages": [ids["other_page"]],
        "ceilings": [{"channel_id": ids["in2"], "hirer_max_db": -10.0}],
        "lighting_enabled": True,
        "individual_fixtures": True,
        "colour_enabled": False,
    }
    response = await admin.put(
        f"{HIRER}/config", json=body, headers={VERSION_HEADER: current["updated_at"]}
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["pages"] == [ids["other_page"]]
    assert _ceiling_map(result) == {ids["in2"]: -10.0}
    assert result["lighting_enabled"] is True
    assert result["individual_fixtures"] is True
    assert result["colour_enabled"] is False
    assert result["updated_at"] != current["updated_at"]

    stored = await hirer_crud.get(db)
    assert (stored.lighting_enabled, stored.individual_fixtures, stored.colour_enabled) == (
        True,
        True,
        False,
    )
    assert (await mixer_crud.get_channel(db, ids["in2"])).hirer_max_db == -10.0  # type: ignore[union-attr]
    assert await pages_crud.list_hirer_page_ids(db) == [ids["other_page"]]

    [audit] = await rows(db, "config_changed")
    assert audit.user_ident == "admin"


async def test_put_config_publishes_hirer_config_changed_and_the_resolver_rebuilds(
    app: FastAPI, admin: AsyncClient, db: Database
) -> None:
    """§6.7: a live session sees the change at once, with no re-login. The
    ``app`` fixture does not run the lifespan (other tests in this module
    exercise ``HirerAccess`` directly, which needs none of it), so the bus
    and the resolver are started here, the same explicit way
    ``tests/unit/api/test_mixer.py``'s ``_running_app`` starts its own
    services without the full lifespan."""
    ids = await _seed_reachable(db)
    bus = app.state.bus
    resolver = app.state.hirer_permissions
    await bus.start()
    await resolver.start(db)
    try:
        current = (await admin.get(f"{HIRER}/config")).json()
        body = {
            "pages": [ids["other_page"]],
            "ceilings": [{"channel_id": ids["in2"], "hirer_max_db": -10.0}],
            "lighting_enabled": True,
            "individual_fixtures": True,
            "colour_enabled": False,
        }
        response = await admin.put(
            f"{HIRER}/config", json=body, headers={VERSION_HEADER: current["updated_at"]}
        )
        assert response.status_code == 200, response.text

        await until(lambda: access_of(app).permissions.pages == (ids["other_page"],))
        await until(lambda: access_of(app).permissions.ceiling_db(ids["in2"]) == -10.0)
    finally:
        await resolver.stop()
        await bus.stop()


async def test_put_config_requires_the_version_header(admin: AsyncClient) -> None:
    response = await admin.put(
        f"{HIRER}/config",
        json={
            "pages": [],
            "ceilings": [],
            "lighting_enabled": False,
            "individual_fixtures": False,
            "colour_enabled": False,
        },
    )
    assert response.status_code == 422
    assert error(response)["code"] == "validation_failed"


async def test_put_config_conflict_reports_the_current_configuration(
    admin: AsyncClient, db: Database
) -> None:
    ids = await _seed_reachable(db)
    current = (await admin.get(f"{HIRER}/config")).json()
    stale = current["updated_at"]
    await hirer_crud.update(
        db, {"colour_enabled": True}, expected_updated_at=stale, updated_by=None
    )
    body = {
        "pages": [ids["page"]],
        "ceilings": [],
        "lighting_enabled": False,
        "individual_fixtures": False,
        "colour_enabled": False,
    }
    response = await admin.put(f"{HIRER}/config", json=body, headers={VERSION_HEADER: stale})
    assert response.status_code == 409
    assert error(response)["code"] == "conflict"
    assert error(response)["detail"]["current"]["colour_enabled"] is True
    # Nothing else from the rejected write took effect either (one transaction).
    assert await pages_crud.list_hirer_page_ids(db) == [ids["page"]]


async def test_put_config_refuses_the_default_page(admin: AsyncClient, db: Database) -> None:
    ids = await _seed_reachable(db)
    current = (await admin.get(f"{HIRER}/config")).json()
    body = {
        "pages": [ids["default_page"]],
        "ceilings": [],
        "lighting_enabled": False,
        "individual_fixtures": False,
        "colour_enabled": False,
    }
    response = await admin.put(
        f"{HIRER}/config", json=body, headers={VERSION_HEADER: current["updated_at"]}
    )
    assert response.status_code == 422
    assert error(response)["detail"]["reason"] == "default_page"
    assert await pages_crud.list_hirer_page_ids(db) == [ids["page"]]  # unchanged


async def test_put_config_refuses_an_unknown_page(admin: AsyncClient, db: Database) -> None:
    current = (await admin.get(f"{HIRER}/config")).json()
    body = {
        "pages": [999],
        "ceilings": [],
        "lighting_enabled": False,
        "individual_fixtures": False,
        "colour_enabled": False,
    }
    response = await admin.put(
        f"{HIRER}/config", json=body, headers={VERSION_HEADER: current["updated_at"]}
    )
    assert response.status_code == 422
    assert error(response)["detail"]["field"] == "pages"


async def test_put_config_refuses_a_page_assigned_twice(admin: AsyncClient, db: Database) -> None:
    ids = await _seed_reachable(db)
    current = (await admin.get(f"{HIRER}/config")).json()
    body = {
        "pages": [ids["page"], ids["page"]],
        "ceilings": [],
        "lighting_enabled": False,
        "individual_fixtures": False,
        "colour_enabled": False,
    }
    response = await admin.put(
        f"{HIRER}/config", json=body, headers={VERSION_HEADER: current["updated_at"]}
    )
    assert response.status_code == 422
    assert error(response)["detail"]["field"] == "pages"


@pytest.mark.parametrize("target", ["in2", "out1"])
async def test_put_config_refuses_a_ceiling_not_reachable_through_the_posted_pages(
    admin: AsyncClient, db: Database, target: str
) -> None:
    """``in2`` is not on the posted page at all; ``out1`` is on it but is an
    output — never reachable, whatever the page holds (Q4)."""
    ids = await _seed_reachable(db)
    current = (await admin.get(f"{HIRER}/config")).json()
    body = {
        "pages": [ids["page"]],
        "ceilings": [{"channel_id": ids[target], "hirer_max_db": -1.0}],
        "lighting_enabled": False,
        "individual_fixtures": False,
        "colour_enabled": False,
    }
    response = await admin.put(
        f"{HIRER}/config", json=body, headers={VERSION_HEADER: current["updated_at"]}
    )
    assert response.status_code == 422
    assert error(response)["detail"]["field"] == "ceilings"
    assert error(response)["detail"]["channel_id"] == ids[target]
    assert (await mixer_crud.get_channel(db, ids[target])).hirer_max_db != -1.0  # type: ignore[union-attr]


async def test_put_config_refuses_a_channel_ceilinged_twice(
    admin: AsyncClient, db: Database
) -> None:
    ids = await _seed_reachable(db)
    current = (await admin.get(f"{HIRER}/config")).json()
    body = {
        "pages": [ids["page"]],
        "ceilings": [
            {"channel_id": ids["in1"], "hirer_max_db": -1.0},
            {"channel_id": ids["in1"], "hirer_max_db": -2.0},
        ],
        "lighting_enabled": False,
        "individual_fixtures": False,
        "colour_enabled": False,
    }
    response = await admin.put(
        f"{HIRER}/config", json=body, headers={VERSION_HEADER: current["updated_at"]}
    )
    assert response.status_code == 422
    assert error(response)["detail"]["field"] == "ceilings"


# -- GET /hirer/conflicts ---------------------------------------------------------------


async def test_conflicts_is_empty_with_no_ceilinged_reachable_channel(
    admin: AsyncClient, db: Database
) -> None:
    device = await devices_crud.create(
        db, category="mixer", driver_key="stub", name="Desk", config={}
    )
    channel = await mixer_crud.create_channel(
        db, device_id=device.id, channel_kind="input", name="in1", hirer_max_db=None
    )
    page = await pages_crud.create_page(db, name="Page")
    await pages_crud.replace_page(
        db,
        page.id,
        page.updated_at,
        name=page.name,
        sort_order=page.sort_order,
        items=[PageItemInput(kind="channel", channel_id=channel.id)],
    )
    await pages_crud.replace_hirer_pages(db, [page.id])
    response = await admin.get(f"{HIRER}/conflicts")
    assert response.status_code == 200
    assert response.json() == {"conflicts": []}


async def test_conflicts_reports_each_kind(admin: AsyncClient, db: Database) -> None:
    ids = await _seed_conflicts(db)
    # The Band desk scene was recalled once: in1 came back above its
    # ceiling, Main came back within it.
    await mixer_crud.replace_observed_levels(
        db, ids["band_scene"], {ids["in1"]: 0.0, ids["main"]: -1.0}
    )

    response = await admin.get(f"{HIRER}/conflicts")
    assert response.status_code == 200, response.text
    conflicts = response.json()["conflicts"]

    band = [
        c
        for c in conflicts
        if c["source"].get("kind") == "desk_scene"
        and c["source"]["desk_scene_id"] == ids["band_scene"]
    ]
    assert band == [
        {
            "channel_id": ids["in1"],
            "channel_name": "in1",
            "ceiling_db": -6.0,
            "level_db": 0.0,
            "source": {"kind": "desk_scene", "desk_scene_id": ids["band_scene"], "name": "Band"},
        }
    ]

    unchecked = [
        c
        for c in conflicts
        if c["source"].get("kind") == "desk_scene"
        and c["source"]["desk_scene_id"] == ids["never_scene"]
    ]
    assert {c["channel_id"] for c in unchecked} == {ids["in1"], ids["main"]}
    for row in unchecked:
        assert row["level_db"] is None
        assert row["observed"] is False
        assert row["source"] == {
            "kind": "desk_scene",
            "desk_scene_id": ids["never_scene"],
            "name": "Unchecked",
        }

    action = [c for c in conflicts if c["source"].get("kind") == "scene_action"]
    assert action == [
        {
            "channel_id": ids["in1"],
            "channel_name": "in1",
            "ceiling_db": -6.0,
            "level_db": 0.0,
            "source": {
                "kind": "scene_action",
                "scene_id": ids["fader_scene"],
                "action_id": ids["fader_action"],
                "name": "Interval",
            },
        }
    ]

    assert len(conflicts) == len(band) + len(unchecked) + len(action)


async def test_conflicts_omits_a_desk_scene_recalled_and_within_ceiling(
    admin: AsyncClient, db: Database
) -> None:
    ids = await _seed_conflicts(db)
    await mixer_crud.replace_observed_levels(
        db, ids["band_scene"], {ids["in1"]: -8.0, ids["main"]: -1.0}
    )
    await mixer_crud.replace_observed_levels(db, ids["never_scene"], {ids["in1"]: -8.0})

    response = await admin.get(f"{HIRER}/conflicts")
    conflicts = response.json()["conflicts"]
    desk_scene_conflicts = [c for c in conflicts if c["source"].get("kind") == "desk_scene"]
    assert desk_scene_conflicts == []  # both scenes are within ceiling once observed
