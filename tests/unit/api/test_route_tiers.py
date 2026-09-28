"""Every route's admitted tiers, against an explicit table of decisions (spec §6.2, §16.5).

The hirer tier is admitted on exactly the hirer routes of §16.5 as the
phase-5 contract narrows them (Q2: no direct scene, rule or desk-scene route)
and nothing else. This file enumerates **every** route the application
serves and compares its gate with :data:`EXPECTED`, so a route added without
a decision fails here rather than shipping open or shut by accident.

Then, adversarially: a real hirer session calls every route the table refuses
it, and each must answer ``permission_denied`` with a ``permission_denied``
audit row naming the route and the session (§6.14).

The hirer routes the pages API adds (``GET /pages``, ``GET /pages/{id}`` and
``POST /pages/{id}/buttons/{bid}``) are added to the table where that router
is mounted.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any, Final

import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, iter_route_contexts
from httpx import AsyncClient

from proskenion.api.app import API_PREFIX, create_app
from proskenion.api.deps import admitted_tiers
from proskenion.config import Config
from proskenion.core.auth import TokenService
from proskenion.core.ratelimit import RateLimiter
from proskenion.db.connection import Database
from proskenion.db.crud import security_events
from tests.unit.api.conftest import ADMIN_PASSWORD, HIRER_PIN, make_client

ADMIN: Final = frozenset({"admin"})
STAFF: Final = frozenset({"admin", "operator"})
#: Staff, and a hirer held to ``state.hirer`` target by target.
CONTROL: Final = frozenset({"admin", "operator", "hirer"})
#: No gate: the route authenticates itself, or needs no session at all.
PUBLIC: Final = frozenset[str]()

#: ``(method, path) -> admitted tiers``: one decision per route.
EXPECTED: Final[dict[tuple[str, str], frozenset[str]]] = {
    ("GET", "/health"): PUBLIC,
    # -- auth: sign-in and the session check authenticate themselves
    ("POST", "/auth/login"): PUBLIC,
    ("POST", "/auth/hirer"): PUBLIC,
    ("POST", "/auth/logout"): PUBLIC,
    ("GET", "/auth/session"): PUBLIC,
    ("POST", "/auth/change-password"): STAFF,
    ("POST", "/auth/operator-password"): ADMIN,
    ("GET", "/auth/password-status"): ADMIN,
    # -- first run: gated by the wizard's own state (§10.4)
    ("GET", "/setup/state"): PUBLIC,
    ("POST", "/setup/step/{number}"): PUBLIC,
    ("POST", "/setup/complete"): PUBLIC,
    # -- drivers and devices
    ("GET", "/drivers"): ADMIN,
    ("GET", "/drivers/serial-ports"): ADMIN,
    ("GET", "/devices"): ADMIN,
    ("POST", "/devices"): ADMIN,
    ("GET", "/devices/{device_id}"): ADMIN,
    ("PUT", "/devices/{device_id}"): ADMIN,
    ("DELETE", "/devices/{device_id}"): ADMIN,
    ("POST", "/devices/{device_id}/test"): ADMIN,
    ("GET", "/devices/{device_id}/capabilities"): STAFF,
    ("GET", "/devices/{device_id}/refs"): ADMIN,
    ("GET", "/devices/{device_id}/manifest"): ADMIN,
    # The fader law and meter scale are how any fader, a hirer's included,
    # turns a position into dB (§5.5); they carry no venue data.
    ("GET", "/devices/{device_id}/fader-law"): CONTROL,
    ("GET", "/devices/{device_id}/meter-scale"): CONTROL,
    ("GET", "/devices/{device_id}/remap"): ADMIN,
    ("POST", "/devices/{device_id}/remap"): ADMIN,
    # -- HDMI
    ("GET", "/hdmi/state"): STAFF,
    ("POST", "/hdmi/destinations/{destination_id}/source"): STAFF,
    ("GET", "/hdmi/inputs"): ADMIN,
    ("GET", "/hdmi/inputs/{input_id}"): ADMIN,
    ("POST", "/hdmi/inputs"): ADMIN,
    ("PUT", "/hdmi/inputs/{input_id}"): ADMIN,
    ("DELETE", "/hdmi/inputs/{input_id}"): ADMIN,
    ("GET", "/hdmi/outputs"): ADMIN,
    ("GET", "/hdmi/outputs/{output_id}"): ADMIN,
    ("POST", "/hdmi/outputs"): ADMIN,
    ("PUT", "/hdmi/outputs/{output_id}"): ADMIN,
    ("DELETE", "/hdmi/outputs/{output_id}"): ADMIN,
    ("GET", "/hdmi/destinations"): ADMIN,
    ("GET", "/hdmi/destinations/{destination_id}"): ADMIN,
    ("POST", "/hdmi/destinations"): ADMIN,
    ("PUT", "/hdmi/destinations/{destination_id}"): ADMIN,
    ("DELETE", "/hdmi/destinations/{destination_id}"): ADMIN,
    # -- hirer access and configuration
    ("POST", "/hirer/pin"): ADMIN,
    ("POST", "/hirer/enabled"): ADMIN,
    ("GET", "/hirer/config"): ADMIN,
    ("PUT", "/hirer/config"): ADMIN,
    ("GET", "/hirer/conflicts"): ADMIN,
    # -- KNX
    ("GET", "/knx/addresses"): ADMIN,
    ("POST", "/knx/addresses"): ADMIN,
    ("GET", "/knx/addresses/{address_id}"): ADMIN,
    ("PUT", "/knx/addresses/{address_id}"): ADMIN,
    ("DELETE", "/knx/addresses/{address_id}"): ADMIN,
    ("GET", "/knx/addresses/{address_id}/references"): ADMIN,
    ("GET", "/knx/device-groups"): ADMIN,
    ("POST", "/knx/device-groups"): ADMIN,
    ("GET", "/knx/device-groups/{group_id}"): ADMIN,
    ("PUT", "/knx/device-groups/{group_id}"): ADMIN,
    ("DELETE", "/knx/device-groups/{group_id}"): ADMIN,
    ("GET", "/knx/device-groups/{group_id}/references"): ADMIN,
    ("POST", "/knx/addresses/{address_id}/test-write"): ADMIN,
    ("GET", "/knx/monitor"): ADMIN,
    ("GET", "/knx/unsupported"): ADMIN,
    ("POST", "/knx/import"): ADMIN,
    ("GET", "/knx/export"): ADMIN,
    # -- lighting control: the hirer's four, then what a hirer never has
    ("GET", "/lighting/state"): CONTROL,
    ("POST", "/lighting/channels/{channel_id}/level"): CONTROL,
    ("POST", "/lighting/channels/{channel_id}/colour"): CONTROL,
    ("POST", "/lighting/groups/{group_id}/level"): CONTROL,
    ("POST", "/lighting/channels/{channel_id}/test"): ADMIN,
    ("POST", "/lighting/master"): STAFF,
    ("POST", "/lighting/blackout"): STAFF,
    ("POST", "/lighting/levels"): STAFF,
    ("GET", "/lighting/external-control"): STAFF,
    ("POST", "/lighting/external-control"): STAFF,
    ("POST", "/lighting/snapshot"): ADMIN,
    # -- lighting configuration
    ("GET", "/lighting/channels"): STAFF,
    ("GET", "/lighting/channels/{channel_id}"): STAFF,
    ("POST", "/lighting/channels"): ADMIN,
    ("PUT", "/lighting/channels/{channel_id}"): ADMIN,
    ("DELETE", "/lighting/channels/{channel_id}"): ADMIN,
    ("GET", "/lighting/channels/{channel_id}/references"): ADMIN,
    ("GET", "/lighting/groups"): STAFF,
    ("GET", "/lighting/groups/{group_id}"): STAFF,
    ("POST", "/lighting/groups"): ADMIN,
    ("PUT", "/lighting/groups/{group_id}"): ADMIN,
    ("DELETE", "/lighting/groups/{group_id}"): ADMIN,
    ("GET", "/lighting/bars"): STAFF,
    ("GET", "/lighting/bars/{bar_id}"): STAFF,
    ("POST", "/lighting/bars"): ADMIN,
    ("PUT", "/lighting/bars/{bar_id}"): ADMIN,
    ("DELETE", "/lighting/bars/{bar_id}"): ADMIN,
    ("GET", "/lighting/presets"): ADMIN,
    ("GET", "/lighting/presets/{preset_id}"): ADMIN,
    ("POST", "/lighting/presets"): ADMIN,
    ("PUT", "/lighting/presets/{preset_id}"): ADMIN,
    ("DELETE", "/lighting/presets/{preset_id}"): ADMIN,
    ("GET", "/lighting/profiles"): ADMIN,
    ("GET", "/lighting/profiles/{profile_id}"): ADMIN,
    ("POST", "/lighting/profiles"): ADMIN,
    ("PUT", "/lighting/profiles/{profile_id}"): ADMIN,
    ("DELETE", "/lighting/profiles/{profile_id}"): ADMIN,
    ("GET", "/lighting/patch/conflicts"): STAFF,
    # -- mixer control: the hirer's three, then pan, recall and test
    ("GET", "/mixer/state"): CONTROL,
    ("POST", "/mixer/channels/{channel_id}/level"): CONTROL,
    ("POST", "/mixer/channels/{channel_id}/mute"): CONTROL,
    ("POST", "/mixer/channels/{channel_id}/pan"): STAFF,
    ("POST", "/mixer/desk-scenes/{scene_id}/recall"): STAFF,
    ("POST", "/mixer/desk-scenes/{scene_id}/test"): ADMIN,
    # -- mixer configuration
    ("GET", "/mixer/channels"): ADMIN,
    ("GET", "/mixer/channels/{channel_id}"): ADMIN,
    ("POST", "/mixer/channels"): ADMIN,
    ("PUT", "/mixer/channels/{channel_id}"): ADMIN,
    ("DELETE", "/mixer/channels/{channel_id}"): ADMIN,
    ("GET", "/mixer/devices/{device_id}/missing-channels"): ADMIN,
    ("POST", "/mixer/devices/{device_id}/missing-channels"): ADMIN,
    ("GET", "/mixer/desk-scenes"): ADMIN,
    ("GET", "/mixer/desk-scenes/{scene_id}"): ADMIN,
    ("POST", "/mixer/desk-scenes"): ADMIN,
    ("PUT", "/mixer/desk-scenes/{scene_id}"): ADMIN,
    ("DELETE", "/mixer/desk-scenes/{scene_id}"): ADMIN,
    # -- pages: the everyday surface (§15.12). A hirer's admission here is
    # only the tier gate; button_reachable and page assignment are checked
    # inside the handler (proskenion/api/pages.py), the same two-step shape
    # CONTROL already names for mixer and lighting.
    ("GET", "/pages"): CONTROL,
    ("GET", "/pages/{page_id}"): CONTROL,
    ("POST", "/pages"): ADMIN,
    ("PUT", "/pages/{page_id}"): ADMIN,
    ("DELETE", "/pages/{page_id}"): ADMIN,
    ("GET", "/pages/{page_id}/validate"): ADMIN,
    ("POST", "/pages/{page_id}/buttons/{button_id}"): CONTROL,
    # -- projector
    ("GET", "/projector/state"): STAFF,
    ("POST", "/projector/power"): STAFF,
    ("POST", "/projector/input"): STAFF,
    # -- the shared show timer (§16, §21.7): hirers see the clock, not the timer
    ("POST", "/timer/start"): STAFF,
    ("POST", "/timer/stop"): STAFF,
    ("POST", "/timer/reset"): STAFF,
    # -- rules and derived status: a hirer fires a rule only through a page button
    ("GET", "/rules"): STAFF,
    ("POST", "/rules"): ADMIN,
    ("GET", "/rules/state"): STAFF,
    ("GET", "/rules/log"): STAFF,
    ("GET", "/rules/{rule_id}"): STAFF,
    ("PUT", "/rules/{rule_id}"): ADMIN,
    ("DELETE", "/rules/{rule_id}"): ADMIN,
    ("POST", "/rules/{rule_id}/fire"): STAFF,
    ("POST", "/rules/{rule_id}/test"): ADMIN,
    ("GET", "/derived-status"): ADMIN,
    ("POST", "/derived-status"): ADMIN,
    ("GET", "/derived-status/state"): STAFF,
    ("GET", "/derived-status/monitor"): ADMIN,
    ("GET", "/derived-status/{status_id}"): ADMIN,
    ("PUT", "/derived-status/{status_id}"): ADMIN,
    ("DELETE", "/derived-status/{status_id}"): ADMIN,
    # -- scenes: no direct hirer route at all (Q2)
    ("GET", "/scenes/log"): STAFF,
    ("GET", "/scenes/domains"): ADMIN,
    ("GET", "/scenes"): STAFF,
    ("POST", "/scenes"): ADMIN,
    ("GET", "/scenes/{scene_id}"): ADMIN,
    ("PUT", "/scenes/{scene_id}"): ADMIN,
    ("DELETE", "/scenes/{scene_id}"): ADMIN,
    ("GET", "/scenes/{scene_id}/references"): ADMIN,
    ("GET", "/scenes/{scene_id}/actions"): ADMIN,
    ("POST", "/scenes/{scene_id}/actions"): ADMIN,
    ("GET", "/scenes/{scene_id}/actions/{action_id}"): ADMIN,
    ("PUT", "/scenes/{scene_id}/actions/{action_id}"): ADMIN,
    ("DELETE", "/scenes/{scene_id}/actions/{action_id}"): ADMIN,
    ("POST", "/scenes/{scene_id}/trigger"): STAFF,
    ("POST", "/scenes/{scene_id}/test"): ADMIN,
    ("POST", "/scenes/{scene_id}/test-group"): ADMIN,
    ("GET", "/scenes/{scene_id}/log"): STAFF,
    # -- system
    ("GET", "/system/health"): ADMIN,
    ("GET", "/system/time"): ADMIN,
    ("GET", "/system/version"): STAFF,
    # §22.7: what the soak harness cannot read from /proc (tasks, sockets, loop lag).
    ("GET", "/system/diagnostics"): ADMIN,
    ("GET", "/system/email"): ADMIN,
    ("PUT", "/system/email"): ADMIN,
    ("DELETE", "/system/email"): ADMIN,
    ("POST", "/system/email/test"): ADMIN,
    # -- logs (§6.14, §21.24 "Logs")
    ("GET", "/system/security-log"): ADMIN,
    ("GET", "/system/debug-logging"): ADMIN,
    ("PUT", "/system/debug-logging"): ADMIN,
    ("GET", "/system/logs"): ADMIN,
    ("GET", "/system/logs/export"): ADMIN,
    # -- certificates (contracts §5, §6.16): the token and issuance are
    # admin-only; the download is public — a device deciding whether to
    # trust this controller cannot authenticate to it first.
    ("GET", "/system/certs/token"): ADMIN,
    ("PUT", "/system/certs/token"): ADMIN,
    ("POST", "/system/certs/token/test"): ADMIN,
    ("POST", "/system/certs/issue"): ADMIN,
    ("POST", "/system/certs/self-signed"): ADMIN,
    ("GET", "/system/certs/history"): ADMIN,
    ("GET", "/system/certs/download"): PUBLIC,
    # -- backup (contracts §5): every route is admin-only, including the SFTP
    # public key — it is not sensitive, but nothing marks it public and the
    # default is admin.
    ("GET", "/system/backup/status"): ADMIN,
    ("POST", "/system/backup/run"): ADMIN,
    ("POST", "/system/backup/verify"): ADMIN,
    ("GET", "/system/backup/history"): ADMIN,
    ("GET", "/system/backup/{archive_id}/download"): ADMIN,
    ("GET", "/system/backup/destinations"): ADMIN,
    ("PUT", "/system/backup/destinations"): ADMIN,
    ("GET", "/system/backup/sftp-key"): ADMIN,
    ("GET", "/system/backup/snapshots"): ADMIN,
    # -- backup restore (contracts §5, §13.2, Q15): replacing the database of
    # a running appliance from unsigned input. Admin, and nothing below it.
    ("POST", "/system/backup/restore"): ADMIN,
    ("POST", "/system/backup/restore/acknowledge"): ADMIN,
    # -- network (contracts §4, §5, §10.8): the whole reconnection flow is
    # admin-only. The static /reconnect page itself is nginx-served, not an
    # API route — see appliance/nginx/auditorium.conf.
    ("GET", "/system/network"): ADMIN,
    ("POST", "/system/network"): ADMIN,
    ("POST", "/system/network/confirm"): ADMIN,
    ("GET", "/system/network/state"): ADMIN,
    # -- system images (contracts §5, §13.6, Q13): capture reads a raw
    # partition through the privileged helper, and restore reboots the
    # appliance into the standby slot on trial exactly as an OS upgrade does
    # — neither is an operator's decision or business.
    ("GET", "/system/images"): ADMIN,
    ("POST", "/system/images/capture"): ADMIN,
    ("POST", "/system/images/{image_id}/restore"): ADMIN,
    ("DELETE", "/system/images/{image_id}"): ADMIN,
    # -- application updates (contracts §5, §14.2-§14.5): admin throughout.
    # Applying restarts the appliance and rolling back replaces the database,
    # so neither is anything an operator holds the authority for, and the
    # verified manifest names the signing key.
    ("POST", "/system/update"): ADMIN,
    ("DELETE", "/system/update"): ADMIN,
    ("POST", "/system/update/apply"): ADMIN,
    ("POST", "/system/update/rollback"): ADMIN,
    ("GET", "/system/update/status"): ADMIN,
    # -- OS upgrade (contracts §5, §14.4): a slot change reboots the appliance
    # into a different operating system, and the reading names slots, versions
    # and the trial's deadline. Neither is an operator's decision or business.
    ("GET", "/system/os"): ADMIN,
    ("POST", "/system/os/rollback"): ADMIN,
    # -- restart and reboot (contracts §2, §5): both go through the helper
    # and take the whole appliance offline for everyone, staff and hirer
    # alike, so neither is an operator's decision.
    ("POST", "/system/restart"): ADMIN,
    ("POST", "/system/reboot"): ADMIN,
    # -- venue baseline (contracts §5, §13.5): capture is "deliberate and
    # admin-only", and compare exposes the whole configuration.
    ("GET", "/system/baseline"): ADMIN,
    ("POST", "/system/baseline"): ADMIN,
    ("GET", "/system/baseline/compare"): ADMIN,
    ("POST", "/system/baseline/restore"): ADMIN,
}

#: Framework routes that are not the API: the generated documentation.
FRAMEWORK: Final = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})

#: The WebSocket authenticates at the upgrade and checks every ``set`` per
#: target (``test_hirer_enforcement.py``), so it has no tier gate to list.
WEBSOCKET: Final = "/ws"


def _gates(dependant: Dependant) -> Iterator[frozenset[str]]:
    tiers = admitted_tiers(dependant.call)
    if tiers is not None:
        yield tiers
    for sub in dependant.dependencies:
        yield from _gates(sub)


def _served(app: FastAPI) -> dict[tuple[str, str], frozenset[str]]:
    """Every HTTP route the application serves, with the tiers its gates admit.

    Several gates on one route admit only what they all admit; no gate is
    :data:`PUBLIC`.
    """
    served: dict[tuple[str, str], frozenset[str]] = {}
    others: set[str] = set()
    for route in iter_route_contexts(app.routes):
        if not isinstance(route.original_route, APIRoute):
            others.add(str(getattr(route.original_route, "path", "")))
            continue
        path = str(route.path)
        dependant: Dependant = route.dependant
        gates = list(_gates(dependant))
        tiers = frozenset.intersection(*gates) if gates else PUBLIC
        short = path.removeprefix(API_PREFIX)
        for method in sorted(route.methods or ()):
            served[(method, short)] = tiers
    # Nothing else is served: the generated documentation and the socket.
    assert others == FRAMEWORK | {WEBSOCKET}
    return served


@pytest.fixture
def served_app(config: Config, db: Database, tokens: TokenService, limiter: RateLimiter) -> FastAPI:
    """The application exactly as production builds it: no test-only routes."""
    return create_app(config, db=db, tokens=tokens, limiter=limiter)


def test_every_route_has_a_decision_and_the_gate_matches_it(served_app: FastAPI) -> None:
    served = _served(served_app)
    undecided = sorted(set(served) - set(EXPECTED))
    assert not undecided, f"routes with no tier decision in EXPECTED: {undecided}"
    gone = sorted(set(EXPECTED) - set(served))
    assert not gone, f"EXPECTED names routes the application does not serve: {gone}"
    wrong = {key: (sorted(served[key]), sorted(tiers)) for key, tiers in EXPECTED.items()}
    wrong = {key: pair for key, pair in wrong.items() if pair[0] != pair[1]}
    assert not wrong, f"(served, expected) differ: {wrong}"


def test_the_hirer_routes_are_exactly_the_contracts(served_app: FastAPI) -> None:
    hirer_routes = {key for key, tiers in _served(served_app).items() if "hirer" in tiers}
    assert hirer_routes == {
        ("GET", "/mixer/state"),
        ("POST", "/mixer/channels/{channel_id}/level"),
        ("POST", "/mixer/channels/{channel_id}/mute"),
        ("GET", "/lighting/state"),
        ("POST", "/lighting/channels/{channel_id}/level"),
        ("POST", "/lighting/channels/{channel_id}/colour"),
        ("POST", "/lighting/groups/{group_id}/level"),
        ("GET", "/devices/{device_id}/fader-law"),
        ("GET", "/devices/{device_id}/meter-scale"),
        ("GET", "/pages"),
        ("GET", "/pages/{page_id}"),
        ("POST", "/pages/{page_id}/buttons/{button_id}"),
    }


# -- a real hirer session against every refused route ------------------------------


def _refused_to_hirers() -> list[tuple[str, str]]:
    return sorted(
        key for key, tiers in EXPECTED.items() if tiers is not PUBLIC and "hirer" not in tiers
    )


def _concrete(path: str) -> str:
    """A path template with every parameter set to 1."""
    parts = [
        ("1" if part.startswith("{") and part.endswith("}") else part) for part in path.split("/")
    ]
    return API_PREFIX + "/".join(parts)


@pytest.fixture
async def hirer(app: FastAPI) -> Any:
    async with make_client(app) as admin:
        response = await admin.post(f"{API_PREFIX}/auth/login", json={"password": ADMIN_PASSWORD})
        assert response.status_code == 200
        response = await admin.post(f"{API_PREFIX}/hirer/enabled", json={"enabled": True})
        assert response.status_code == 200, response.text
    async with make_client(app) as http:
        response = await http.post(f"{API_PREFIX}/auth/hirer", json={"pin": HIRER_PIN})
        assert response.status_code == 200, response.text
        yield http


async def test_a_hirer_is_refused_every_route_the_table_refuses_and_each_is_audited(
    hirer: AsyncClient, db: Database
) -> None:
    refused = _refused_to_hirers()
    assert len(refused) > 100  # the table really is being walked
    before = len(await security_events.query(db, event_type="permission_denied", limit=1000))
    failures: list[str] = []
    for method, path in refused:
        url = _concrete(path)
        response = await hirer.request(method, url, json={})
        body: dict[str, Any] = response.json() if response.content else {}
        code = body.get("error", {}).get("code")
        if response.status_code != 403 or code != "permission_denied":
            failures.append(f"{method} {path}: {response.status_code} {code}")
    assert not failures, failures

    rows = await security_events.query(db, event_type="permission_denied", limit=1000)
    assert len(rows) - before == len(refused)
    audited = {
        (json.loads(r.detail or "{}")["method"], json.loads(r.detail or "{}")["path"]) for r in rows
    }
    for method, path in refused:
        assert (method, _concrete(path)) in audited
    session_ids = {json.loads(r.detail or "{}").get("session_id") for r in rows}
    assert len(session_ids) == 1 and None not in session_ids
    assert {r.user_ident for r in rows} == {"hirer"}
