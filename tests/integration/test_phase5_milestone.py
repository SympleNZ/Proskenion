"""The Phase 5 milestone at the API level (spec §18, ``docs/plans/phase-5.md``).

    "A hirer signs in with a PIN, controls only what they are permitted,
     cannot exceed configured limits, and can be cut off instantly."

This module attacks that sentence rather than demonstrating it. The room is
:mod:`tests.integration.hirer_rig`'s: the real application served by uvicorn
on a loopback port, the real ``cq20b`` driver over TCP to the MIDI stub (with
its meters from the native stub), the real KNX subsystem and ``artnet``
driver against the knxd and Art-Net stubs, all configured through the API,
with hirers signing in by PIN over real HTTP and holding real WebSockets.
What the desk or the lights received is read from the stubs' records.

One test per row of the plan's security-properties table, plus §22.4's two
named scenarios and Q3's switches:

1. a hirer token reaches no staff endpoint (every route, with real ids)
2. nothing off the assigned pages is reachable on REST or the socket
3. ceilings hold on both transports, for values the UI would never send
4. a lowered ceiling pulls the fader down; enabling access does too
5. a hirer's recall is clamped after it lands; the same scene by staff is not
6. a page edit takes effect on the next broadcast, with no re-login
7. the kill switch and a PIN change cut a hirer off at once, and a write
   racing the switch is applied before it answers or never
8. the absolute expiry, on REST and an open socket
9. the PIN lockout per real address, through nginx, with forged headers
10. the audit trail
11. Q3's three switches
12. Q4 as amended: outputs never, Main only on an assigned page

Across every test, the fixture checks the ledger: no frame a hirer socket
was sent, and no body a hirer's REST call was answered with, ever named
something a hirer can never reach (:attr:`HirerRoom.forbidden`).
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.routing import APIRoute, iter_route_contexts

from proskenion.api.app import API_PREFIX
from proskenion.api.deps import admitted_tiers
from proskenion.config import Config
from proskenion.core.dmx.compositor import level_to_dmx
from proskenion.db.connection import Database
from proskenion.db.crud import security_events
from tests.integration.hirer_rig import (
    ATTACKER,
    AUTH,
    BAND_ADJUST_MS,
    BOOTH,
    CYC_ADDRESS,
    HIRER,
    PAGES,
    PHONE,
    PIN,
    SECOND_PHONE,
    VERSION_HEADER,
    WORK_LIGHT_ADDRESS,
    Clock,
    HirerRoom,
    HirerSocket,
    LiveAppliance,
    Phone,
    build_hirer_room,
    dmx,
    dmx_values_seen,
    nginx_forwards_as_modelled,
)
from tests.integration.mixer_rig import (
    LEVEL,
    MIXER,
    MUTE,
    PERFORMANCE_REF,
    PRESETS,
    level,
    mute_steps,
    sets,
)
from tests.integration.rig import DEVICES, LIGHTING, RULES, SCENES, ok, until
from tests.integration.test_first_run_flow import wait_for_status

# The Phase 4 milestone's desk fixtures — the MIDI and native stubs, the
# compressed retry — and its KNX section aimed at the stub knxd.
from tests.integration.test_phase4_milestone import (  # noqa: F401  (pytest fixtures)
    fast_retry,
    knx_section,
    midi,
    native,
)
from tests.stubs.artnet_stub import ArtNetStub
from tests.stubs.cq_midi_stub import CqMidiStub
from tests.stubs.cq_native_stub import CqNativeStub
from tests.stubs.knxd_stub import KnxdStub
from tests.stubs.pjlink_stub import PJLinkStub

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# -- fixtures -----------------------------------------------------------------------


@pytest.fixture
async def room(
    config: Config,
    db: Database,
    knxd: KnxdStub,
    artnet: ArtNetStub,
    midi: CqMidiStub,  # noqa: F811
    native: CqNativeStub,  # noqa: F811
    fast_retry: None,  # noqa: F811
) -> AsyncIterator[HirerRoom]:
    """The hired room (see :mod:`tests.integration.hirer_rig`), served for real.

    On the way out, the whole test's record is checked: no hirer was ever
    shown anything a hirer can never reach, and nothing stepped a mute.
    """
    appliance = LiveAppliance(config, db, Clock())
    await appliance.boot()
    hired: HirerRoom | None = None
    try:
        hired = await build_hirer_room(appliance, knxd, artnet, midi, native)
        yield hired
        leaks = hired.ledger.leaks(hired.forbidden)
        assert not leaks, "a hirer was shown something out of reach:\n" + "\n".join(leaks[:20])
        assert hired.ledger.entries, "the ledger recorded nothing — the check proved nothing"
    finally:
        if hired is not None:
            await hired.close()
        await appliance.shutdown()
    assert mute_steps(midi) == [], "a relative step reached a mute address"


# -- helpers ------------------------------------------------------------------------


def error(response: Any) -> dict[str, Any]:
    body: dict[str, Any] = response.json()
    return dict(body["error"])


def refused(response: Any, status: int = 403, code: str = "permission_denied") -> None:
    assert response.status_code == status, (response.request.url, response.text)
    assert error(response)["code"] == code, response.text


async def events(db: Database, event_type: str) -> list[security_events.SecurityEvent]:
    return await security_events.query(db, event_type=event_type, limit=1000)


def detail(row: security_events.SecurityEvent) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(row.detail or "{}")
    return parsed


def mixer_entry(frame: dict[str, Any], channel_id: int, main_id: int) -> dict[str, Any] | None:
    """One channel's entry in a ``mixer_state`` frame; Main travels unkeyed."""
    if channel_id == main_id:
        main = frame.get("main")
        return dict(main) if isinstance(main, dict) else None
    for section in ("inputs", "outputs"):
        entry = (frame.get(section) or {}).get(str(channel_id))
        if entry is not None:
            return dict(entry)
    return None


async def desk_holds(room: HirerRoom, address: tuple[int, int], value: int, what: str) -> None:
    await until(lambda: room.midi.value(address) == value, what)


# == 0. the room is what the milestone says it is ====================================


async def test_a_hirer_signs_in_with_the_pin_and_sees_only_the_assigned_pages(
    room: HirerRoom,
) -> None:
    """The first clause, plainly: PIN in, the two assigned pages out, each
    showing only what the hirer may reach, with the hirer's own fields."""
    phone = room.phone()
    answer = ok(await phone.sign_in())
    assert answer["tier"] == "hirer"
    listed = ok(await phone.get(PAGES))["pages"]
    assert [p["id"] for p in listed] == [room.pages["performance"], room.pages["foyer"]]
    assert all("hirer" not in p for p in listed)

    performance = ok(await phone.get(f"{PAGES}/{room.pages['performance']}"))
    mixer_items = [i for i in performance["items"] if i.get("source") == "mixer"]
    assert [i["channel_id"] for i in mixer_items] == [
        room.channel("wireless"),
        room.channel("lectern"),
        room.mixer.main,
    ]  # the Foldback output on this page is omitted (Q4)
    assert [i["channel"]["ceiling_db"] for i in mixer_items] == [-6.0, -10.0, -4.0]
    (group,) = [i for i in performance["items"] if i["kind"] == "group_master"]
    assert group["group_id"] == room.lighting.bank
    assert group["members_writable"] is False
    (panel,) = [i for i in performance["items"] if i["kind"] == "panel"]
    assert [b["label"] for b in panel["buttons"]] == ["Band start", "Stage wash"]

    for page_id in (room.pages["crew"], await default_page(room)):
        refused(await phone.get(f"{PAGES}/{page_id}"), 404, "not_found")

    socket = await phone.open_socket()
    resync = socket.of_type("mixer_state")[0]
    assert set(resync["inputs"]) == {str(room.channel(k)) for k in ("wireless", "lectern", "hdmi")}
    assert resync["outputs"] == {}
    assert resync["main"] is not None  # Main is on an assigned page (Q4 as amended)
    assert socket.of_type("status")[0]["lamps"].keys() == {str(room.lamps["wash"])}


async def default_page(room: HirerRoom) -> int:
    """The generated default page's id, as staff see it (§21.9)."""
    listed = ok(await room.admin.get(PAGES))["pages"]
    (page,) = [p for p in listed if p["is_default"]]
    return int(page["id"])


def frames_with(
    socket: HirerSocket, room: HirerRoom, key: str, since: float
) -> list[dict[str, Any]]:
    """Every ``mixer_state`` entry for ``key`` the socket was sent from ``since`` on."""
    channel_id = room.channel(key)
    return [
        entry
        for frame in socket.of_type("mixer_state", since)
        if (entry := mixer_entry(frame, channel_id, room.mixer.main)) is not None
    ]


async def frame_value(
    socket: HirerSocket,
    room: HirerRoom,
    key: str,
    predicate: Any,
    what: str,
    *,
    since: float,
) -> dict[str, Any]:
    """The first ``mixer_state`` entry for ``key`` from ``since`` on satisfying ``predicate``."""

    def probe() -> dict[str, Any] | None:
        return next((e for e in frames_with(socket, room, key, since) if predicate(e)), None)

    return await until(probe, what)


def token_claims(phone: Phone) -> dict[str, Any]:
    """The claims in a phone's token — identity only (B31)."""
    token = phone.cookie
    assert token is not None
    segment = token.split(".")[1]
    claims: dict[str, Any] = json.loads(
        base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    )
    return claims


def phone_session(phone: Phone) -> str:
    return str(token_claims(phone)["sid"])


# == 1. a hirer token reaches no staff endpoint =======================================


def _real_ids(room: HirerRoom, path: str) -> dict[str, int | str]:
    """A real id for every path parameter, so a refusal is the gate's and
    never a 404 for an id that happens not to exist."""
    mixer_path = path.startswith("/mixer")
    return {
        "archive_id": "auditorium-20260920-0300",
        "image_id": "auditorium-v1.3.0-20260920-0300",
        "device_id": room.mixer.device,
        "channel_id": room.channel("wireless") if mixer_path else room.cyc,
        "group_id": room.lighting.bank if path.startswith("/lighting") else 1,
        "scene_id": (room.mixer.desk_scenes["performance"] if mixer_path else room.scenes["band"]),
        "rule_id": room.rules["band"],
        "page_id": room.pages["performance"],
        "button_id": room.buttons["Band start"][1],
        "status_id": room.lamps["wash"],
        "address_id": room.lighting.addresses["1/0/1"],
        "action_id": 1,
        "destination_id": 1,
        "input_id": 1,
        "output_id": 1,
        "bar_id": 1,
        "preset_id": 1,
        "profile_id": 1,
        "number": 2,
    }


def _body(room: HirerRoom, method: str, path: str) -> Any:
    """What a staff client would send to the routes the table names, so a
    refusal is the gate's and never a validation error standing in for it."""
    bodies: dict[tuple[str, str], Any] = {
        ("POST", "/rules/{rule_id}/fire"): {},
        ("POST", "/mixer/channels/{channel_id}/pan"): {"pan": 0.5},
        ("POST", "/lighting/master"): {"level": 100.0},
        ("POST", "/lighting/blackout"): None,
        ("POST", "/lighting/levels"): {"levels": {str(room.cyc): 100.0}},
        ("POST", "/hirer/pin"): {"pin": "000000"},
        ("POST", "/hirer/enabled"): {"enabled": False},
        ("PUT", "/hirer/config"): {
            "pages": [room.pages["crew"]],
            "ceilings": [],
            "lighting_enabled": True,
            "individual_fixtures": True,
            "colour_enabled": True,
        },
        ("POST", "/auth/change-password"): {
            "current_password": PIN,
            "new_password": "hijacked-by-a-hirer",
        },
    }
    return bodies.get((method, path), {})


def _served(room: HirerRoom) -> dict[tuple[str, str], frozenset[str] | None]:
    """Every API route the running application serves, with the tiers its
    gates admit (``None``: no gate) — read from the application, not a list."""
    served: dict[tuple[str, str], frozenset[str] | None] = {}
    for context in iter_route_contexts(room.app.routes):
        if not isinstance(context.original_route, APIRoute):
            continue
        gates: list[frozenset[str]] = []
        stack = [context.dependant]
        while stack:
            dependant = stack.pop()
            tiers = admitted_tiers(dependant.call)
            if tiers is not None:
                gates.append(tiers)
            stack.extend(dependant.dependencies)
        admitted = frozenset.intersection(*gates) if gates else None
        for method in context.original_route.methods or ():
            served[(method, str(context.path).removeprefix(API_PREFIX))] = admitted
    return served


async def test_a_hirer_token_reaches_no_staff_endpoint(room: HirerRoom, db: Database) -> None:
    """§6.2, §16.5 as Q2 narrows it: every gated route the application serves
    that does not admit a hirer is called with a live hirer session and real
    ids — rule fire, scene trigger, desk-scene recall and test, the master,
    blackout, pan, bulk levels and every configuration route among them —
    and each answers ``permission_denied``, is audited against the session,
    and reaches nothing: not the desk, not the lights, not the bus, not the
    hire's own configuration. The ungated wizard routes are tried too."""
    phone = await room.signed_in_phone()
    config_before = await room.hirer_config()
    midi_mark, knx_mark = len(room.midi.messages), len(room.knxd.writes)
    dmx_mark = len(room.artnet.received)
    denied_before = len(await events(db, "permission_denied"))

    served = _served(room)
    staff_only = sorted(
        key for key, tiers in served.items() if tiers is not None and "hirer" not in tiers
    )
    assert len(staff_only) > 100, "the route table is not being walked"
    for key in (
        ("POST", "/rules/{rule_id}/fire"),
        ("POST", "/scenes/{scene_id}/trigger"),
        ("POST", "/mixer/desk-scenes/{scene_id}/recall"),
        ("POST", "/mixer/desk-scenes/{scene_id}/test"),
        ("POST", "/lighting/master"),
        ("POST", "/lighting/blackout"),
        ("POST", "/lighting/levels"),
        ("POST", "/mixer/channels/{channel_id}/pan"),
        ("PUT", "/hirer/config"),
        ("POST", "/hirer/pin"),
        ("POST", "/hirer/enabled"),
        ("PUT", "/pages/{page_id}"),
        ("DELETE", "/devices/{device_id}"),
    ):
        assert key in staff_only, key

    failures: list[str] = []
    for method, path in staff_only:
        url = API_PREFIX + path.format(**_real_ids(room, path))
        body = _body(room, method, path)
        response = await phone.request(method, url, json=body)
        if response.status_code != 403 or error(response)["code"] != "permission_denied":
            failures.append(f"{method} {path}: {response.status_code} {response.text[:120]}")
    assert not failures, failures

    # The ungated routes: the wizard is over, so none of it is a way back in.
    for path, body in (
        (
            "/setup/step/2",
            {"password": "hijacked-admin-pw", "password_confirm": "hijacked-admin-pw"},
        ),
        ("/setup/step/5", {"password": "hijacked-oper-pw", "password_confirm": "hijacked-oper-pw"}),
        ("/setup/complete", {}),
    ):
        response = await phone.post(API_PREFIX + path, json=body)
        assert response.status_code >= 400, (path, response.text)
    assert ok(await room.admin.get(f"{AUTH}/session"))["tier"] == "admin"

    # Audited, one row per refused route, all against this session (§6.14).
    rows = await events(db, "permission_denied")
    assert len(rows) - denied_before == len(staff_only)
    new_rows = rows[: len(staff_only)]
    assert {r.user_ident for r in new_rows} == {"hirer"}
    assert {r.ip_address for r in new_rows} == {PHONE}
    assert {detail(r)["session_id"] for r in new_rows} == {phone_session(phone)}

    # Nothing reached anything: the desk was only ever read, the bus never
    # written, no light moved, and the hire is exactly as the admin left it.
    assert sets(room.midi, midi_mark) == []
    assert room.midi.messages_of("recall") == []
    assert room.knxd.writes[knx_mark:] == []
    for address in (1, 2, 3, 4, CYC_ADDRESS, CYC_ADDRESS + 1, CYC_ADDRESS + 2, WORK_LIGHT_ADDRESS):
        assert dmx_values_seen(room, address, dmx_mark) <= {0}, address
    assert await room.hirer_config() == config_before
    assert ok(await phone.get(f"{AUTH}/session"))["tier"] == "hirer"  # the PIN still holds


# == 2. nothing off the assigned pages ================================================


async def test_anything_not_on_an_assigned_page_is_refused_on_both_transports(
    room: HirerRoom, db: Database
) -> None:
    """§6.7, §15.4: pages decide *what*. Every target off the hirer's pages —
    an output on an assigned page, a staff-only output, an input on an
    unassigned page, a fixture and a group only the crew page holds, a group
    whose members are reachable but whose master is on no page, the lighting
    master, an id that does not exist — is refused on REST (403, audited)
    and on the socket (``nack`` ``permission_denied``, audited, carrying no
    value for what the hirer cannot see), and nothing reaches the desk or
    the lights. A button on an unassigned page cannot be fired either way."""
    phone = await room.signed_in_phone()
    socket = await phone.open_socket()
    midi_mark, dmx_mark = len(room.midi.messages), len(room.artnet.received)
    crew_runs_before = await room.newest_run(room.scenes["crew"])
    nowhere = 987_654

    for channel_id in [room.channel(k) for k in ("foldback", "monitors", "spare")] + [nowhere]:
        refused(await phone.post(f"{MIXER}/channels/{channel_id}/level", json={"db": 0.0}))
        refused(await phone.post(f"{MIXER}/channels/{channel_id}/mute", json={"muted": True}))
        refused(await phone.post(f"{MIXER}/channels/{channel_id}/mute", json={"toggle": True}))
        answer = await socket.set("mixer", channel_id, 0.0)
        assert answer is not None and answer["reason"] == "permission_denied", answer
        assert "value" not in answer, answer
    for channel_id in (room.work_light, nowhere):
        refused(await phone.post(f"{LIGHTING}/channels/{channel_id}/level", json={"level": 100}))
        refused(
            await phone.post(
                f"{LIGHTING}/channels/{channel_id}/colour", json={"r": 255, "g": 0, "b": 0}
            )
        )
        answer = await socket.set("lighting", channel_id, 100.0)
        assert answer is not None and answer["reason"] == "permission_denied", answer
        assert "value" not in answer, answer
    for group_id in (room.booth, room.wash_group, nowhere):
        refused(await phone.post(f"{LIGHTING}/groups/{group_id}/level", json={"level": 100}))
        answer = await socket.set("lighting_group", group_id, 100.0)
        assert answer is not None and answer["reason"] == "permission_denied", answer
    answer = await socket.set("master", None, 0.0)
    assert answer is not None and answer["reason"] == "permission_denied", answer

    # The crew page's button: not through its own page, not through a page
    # the hirer holds, and never by its rule directly.
    crew_page, crew_button = room.buttons["Crew check"]
    refused(await phone.post(f"{PAGES}/{crew_page}/buttons/{crew_button}"), 404, "not_found")
    response = await phone.post(f"{PAGES}/{room.pages['performance']}/buttons/{crew_button}")
    assert response.status_code in (403, 404), response.text
    refused(await phone.post(f"{API_PREFIX}/rules/{room.rules['crew']}/fire", json={}))

    # Reads: an unassigned page does not exist, for a hirer; the snapshots
    # hold only what the pages reach.
    refused(await phone.get(f"{PAGES}/{crew_page}"), 404, "not_found")
    state = ok(await phone.get(f"{MIXER}/state"))
    assert {i["channel_id"] for i in state["inputs"]} == {
        room.channel(k) for k in ("wireless", "lectern", "hdmi")
    }
    assert state["outputs"] == []
    lighting_state = ok(await phone.get(f"{LIGHTING}/state"))
    assert str(room.work_light) not in (lighting_state.get("channels") or {})

    # Nothing moved: the desk was only read, the work light never lit, and
    # the crew scene never ran.
    assert sets(room.midi, midi_mark) == []
    assert dmx_values_seen(room, WORK_LIGHT_ADDRESS, dmx_mark) <= {0}
    assert await room.newest_run(room.scenes["crew"]) == crew_runs_before

    # Both transports audited, the socket's with its transport named.
    rows = await events(db, "permission_denied")
    socket_rows = [r for r in rows if detail(r).get("transport") == "websocket"]
    rest_rows = [r for r in rows if "path" in detail(r)]
    assert {(detail(r)["domain"], detail(r)["id"]) for r in socket_rows} >= {
        ("mixer", room.channel("foldback")),
        ("lighting", room.work_light),
        ("lighting_group", room.booth),
        ("master", None),
    }
    foldback_level = f"/channels/{room.channel('foldback')}/level"
    assert any(detail(r)["path"].endswith(foldback_level) for r in rest_rows)
    assert {r.ip_address for r in socket_rows + rest_rows} == {PHONE}


async def test_a_page_button_fires_its_rule_and_only_the_hirers_own_lamps_are_sent(
    room: HirerRoom,
) -> None:
    """§21.9 and the ``status`` frame: the hirer presses "Stage wash", its
    scene runs, and its lamp lights on the hirer's socket.
    The crew lamp, lit by staff meanwhile, never reaches the hirer."""
    phone = await room.signed_in_phone()
    socket = await phone.open_socket()
    t0 = time.monotonic()
    page_id, button_id = room.buttons["Stage wash"]
    fired = ok(await phone.post(f"{PAGES}/{page_id}/buttons/{button_id}"))
    assert fired["fired"] is True and fired["triggered_by"] == f"page:{button_id}", fired
    run = await room.completed_run(room.scenes["wash"], 0)
    assert run["result"] == "success", run
    lamp = str(room.lamps["wash"])
    await socket.first(
        "status", lambda m: m["lamps"].get(lamp, {}).get("on") is True, "the lamp", since=t0
    )

    # Staff light the crew lamp: the work light at 50 %.
    ok(await room.admin.post(f"{LIGHTING}/channels/{room.work_light}/level", json={"level": 50}))
    await until(lambda: dmx(room, WORK_LIGHT_ADDRESS) == level_to_dmx(50), "the work light at 50 %")

    def crew_lit() -> bool:
        lamps = room.app.state.state_store.status.get("lamps") or {}
        return bool((lamps.get(str(room.lamps["crew"])) or {}).get("on") is True)

    await until(crew_lit, "the crew lamp to light")
    # A resync carries every lamp this socket may see: the wash's only.
    t1 = time.monotonic()
    await socket.send({"type": "resync", "domains": ["status"]})
    resync = await socket.first("status", lambda m: True, "the status resync", since=t1)
    assert set(resync["lamps"]) == {lamp}


# == 3. ceilings on both transports ===================================================


async def test_a_ceiling_holds_on_rest_and_the_socket_for_values_the_ui_never_sends(
    room: HirerRoom,
) -> None:
    """§22.4's named scenario, "ceiling clamping of a value the UI would not
    send": a hirer's client drawn to stop at the ceiling is bypassed, and the
    server holds. Applied *at* the ceiling and answered per Q9 — REST ``200``
    with ``clamped: true`` and the applied dB; the socket ``nack``
    ``value_out_of_range`` with the clamped dB — and what reaches the desk is
    the ceiling, never more. Main on an assigned page is clamped like an
    input; an output on an assigned page is refused outright; a level under
    the ceiling, and off, pass untouched."""
    phone = await room.signed_in_phone()
    socket = await phone.open_socket()
    wireless, main, lectern = room.channel("wireless"), room.mixer.main, room.channel("lectern")

    # REST, far above: +10 dB on Wireless 1 (ceiling −6).
    answer = ok(await phone.post(f"{MIXER}/channels/{wireless}/level", json={"db": 10.0}))
    assert (answer["db"], answer["clamped"]) == (-6.0, True)
    await desk_holds(room, LEVEL["ip1"], level(-6.0), "Wireless 1 at its ceiling")

    # Values no slider produces, as raw JSON: none may get past the ceiling.
    for raw in ('{"db": 1e308}', '{"db": Infinity}', '{"db": NaN}', '{"db": 6}', '{"db": "10"}'):
        mark = len(room.midi.messages)
        response = await phone.post(
            f"{MIXER}/channels/{wireless}/level",
            content=raw,
            headers={"content-type": "application/json"},
        )
        if response.status_code == 200:
            assert (response.json()["db"], response.json()["clamped"]) == (-6.0, True), raw
        else:
            assert response.status_code == 422, (raw, response.text)
        assert all(v <= level(-6.0) for a, v in sets(room.midi, mark) if a == LEVEL["ip1"]), raw
    assert room.midi.value(LEVEL["ip1"]) == level(-6.0)

    # The socket: the clamp is a nack carrying where the fader settles (Q9).
    for value in (0.0, 9.5, 1e300):
        answer = await socket.set("mixer", wireless, value)
        assert answer == {
            "type": "nack",
            "token": answer["token"] if answer else None,
            "reason": "value_out_of_range",
            "value": -6.0,
        }, answer
    assert room.midi.value(LEVEL["ip1"]) == level(-6.0)
    assert max(v for a, v in sets(room.midi) if a == LEVEL["ip1"]) == level(-6.0)

    # Main on an assigned page: clamped at its own ceiling (−4), both ways.
    answer = ok(await phone.post(f"{MIXER}/channels/{main}/level", json={"db": 6.0}))
    assert (answer["db"], answer["clamped"]) == (-4.0, True)
    answer = await socket.set("mixer", main, 0.0)
    assert answer is not None and (answer["reason"], answer["value"]) == (
        "value_out_of_range",
        -4.0,
    )
    await desk_holds(room, LEVEL["main"], level(-4.0), "Main at its ceiling")
    assert max(v for a, v in sets(room.midi, room.enabled_mark) if a == LEVEL["main"]) == level(
        -4.0
    )

    # At, under, and off: untouched, and acknowledged as such.
    answer = ok(await phone.post(f"{MIXER}/channels/{lectern}/level", json={"db": -10.0}))
    assert (answer["db"], answer["clamped"]) == (-10.0, False)
    answer = await socket.set("mixer", wireless, -20.0)
    assert answer is not None and answer["type"] == "ack", answer
    await desk_holds(room, LEVEL["ip1"], level(-20.0), "Wireless 1 at −20 dB")
    answer = await socket.set("mixer", wireless, None)
    assert answer is not None and answer["type"] == "ack", answer
    await desk_holds(room, LEVEL["ip1"], level(None), "Wireless 1 off")
    # No ceiling, no clamp: HDMI audio's full travel.
    answer = ok(
        await phone.post(f"{MIXER}/channels/{room.channel('hdmi')}/level", json={"db": 5.0})
    )
    assert (answer["db"], answer["clamped"]) == (5.0, False)
    # A mute has no ceiling: reach only, sent absolute (§7.3).
    ok(await phone.post(f"{MIXER}/channels/{main}/mute", json={"toggle": True}))
    await desk_holds(room, MUTE["main"], 1, "Main muted")

    # An output on an assigned page is never reachable (Q4 as amended).
    foldback = room.channel("foldback")
    refused(await phone.post(f"{MIXER}/channels/{foldback}/level", json={"db": -20.0}))
    answer = await socket.set("mixer", foldback, -20.0)
    assert answer is not None and answer["reason"] == "permission_denied", answer
    assert LEVEL["out3"] not in {a for a, _ in sets(room.midi)}


# == 4. a lowered ceiling pulls down ==================================================


async def test_a_lowered_ceiling_pulls_the_fader_down_and_is_live_at_once(
    room: HirerRoom,
) -> None:
    """§6.7's live-effect table, Q8a. Enabling access already pulled Main
    from the desk's −1 dB to its −4 dB ceiling. Lowering Wireless 1's ceiling
    under where it stands writes the new ceiling to the desk and moves the
    hirer's fader, on the socket they already have; the very next write, even
    one sent the instant the admin's save returns, is held to the new
    ceiling; raising it again gives the travel back. A lowered ceiling above
    where a fader stands moves nothing."""
    assert (LEVEL["main"], level(-4.0)) in sets(room.midi, room.enabled_mark)
    assert room.midi.value(LEVEL["main"]) == level(-4.0)

    phone = await room.signed_in_phone()
    socket = await phone.open_socket()
    wireless = room.channel("wireless")
    ok(await phone.post(f"{MIXER}/channels/{wireless}/level", json={"db": -6.0}))
    await desk_holds(room, LEVEL["ip1"], level(-6.0), "Wireless 1 at −6 dB")

    t0, mark = time.monotonic(), len(room.midi.messages)
    await room.put_hirer_config(ceilings=room.ceilings(wireless=-12.0, lectern=-20.0))
    # Nobody touched a fader: the server wrote the new ceiling to the desk.
    await desk_holds(room, LEVEL["ip1"], level(-12.0), "Wireless 1 pulled down to −12 dB")
    await frame_value(
        socket,
        room,
        "wireless",
        lambda e: e["db"] == -12.0,
        "the pull-down on the socket",
        since=t0,
    )
    assert {v for a, v in sets(room.midi, mark) if a == LEVEL["ip1"]} == {level(-12.0)}
    # The Lectern stood at −13 dB, above its new −20 dB ceiling, so it came
    # down too; Main, whose ceiling did not move, did not.
    await desk_holds(room, LEVEL["ip2"], level(-20.0), "Lectern pulled down to −20 dB")
    assert LEVEL["main"] not in {a for a, _ in sets(room.midi, mark)}
    answer = await socket.set("mixer", wireless, -8.0)
    assert answer is not None and (answer["reason"], answer["value"]) == (
        "value_out_of_range",
        -12.0,
    )

    # Lowered again, and written to the instant the admin's save returns: the
    # write is held to the new ceiling, never the one it replaced.
    await room.put_hirer_config(ceilings=room.ceilings(wireless=-15.0, lectern=-20.0))
    answer = ok(await phone.post(f"{MIXER}/channels/{wireless}/level", json={"db": -6.0}))
    assert (answer["db"], answer["clamped"]) == (-15.0, True)
    await desk_holds(room, LEVEL["ip1"], level(-15.0), "Wireless 1 at −15 dB")
    assert all(v <= level(-12.0) for a, v in sets(room.midi, mark) if a == LEVEL["ip1"])
    # The same through the mixer's own configuration route, where a ceiling
    # is also edited (Phase 4's ceiling control): held from the next write.
    channel = ok(await room.admin.get(f"{MIXER}/channels/{wireless}"))
    ok(
        await room.admin.put(
            f"{MIXER}/channels/{wireless}",
            json={"hirer_max_db": -18.0},
            headers={"If-Unmodified-Since-Version": channel["updated_at"]},
        )
    )
    answer = ok(await phone.post(f"{MIXER}/channels/{wireless}/level", json={"db": -6.0}))
    assert (answer["db"], answer["clamped"]) == (-18.0, True)
    await desk_holds(room, LEVEL["ip1"], level(-18.0), "Wireless 1 at −18 dB")
    assert not socket.closed.is_set()

    # Raised: the fader gains travel, on the same socket, with no re-login.
    await room.put_hirer_config(ceilings=room.ceilings(wireless=-3.0))
    answer = await socket.set("mixer", wireless, -3.0)
    assert answer is not None and answer["type"] == "ack", answer
    await desk_holds(room, LEVEL["ip1"], level(-3.0), "Wireless 1 at −3 dB")

    # A new ceiling above where a fader stands moves nothing: HDMI audio, at
    # the desk's −17 dB, gains a +6 dB ceiling where it had none.
    mark = len(room.midi.messages)
    await room.put_hirer_config(ceilings=room.ceilings(wireless=-3.0, hdmi=6.0))
    await until(
        lambda: (
            room.app.state.state_store.hirer.permissions.ceiling_db(room.channel("hdmi")) == 6.0
        ),
        "the new HDMI ceiling in force",
    )
    assert LEVEL["st2"] not in {a for a, _ in sets(room.midi, mark)}


# == 5. the clamp after a hirer's recall ==============================================


async def test_a_hirer_recall_is_clamped_after_it_lands_and_a_staff_one_is_not(
    room: HirerRoom,
) -> None:
    """Q8b and Q8c. "Band start" recalls the desk's Performance scene
    (Wireless 1 to 0 dB, Main to −3 dB — both above their ceilings) and, a
    later action, pushes the Lectern to 0 dB. Fired from the hirer's page
    button: the recall lands as stored, then every reachable channel above
    its ceiling is pulled down to it, and the Lectern's fader action is
    clamped before it is sent. The same scene run by staff — by trigger or
    by the same page button — is never clamped."""
    phone = await room.signed_in_phone()
    band = room.scenes["band"]
    page_id, button_id = room.buttons["Band start"]
    preset = PRESETS[int(PERFORMANCE_REF)]
    assert preset[LEVEL["ip1"]] > level(-6.0) and preset[LEVEL["main"]] > level(-4.0)

    before, mark = await room.newest_run(band), len(room.midi.messages)
    fired = ok(await phone.post(f"{PAGES}/{page_id}/buttons/{button_id}"))
    assert fired["fired"] is True, fired
    run = await room.completed_run(band, before)
    await desk_holds(room, LEVEL["ip2"], level(-10.0), "the Lectern clamped at −10 dB")

    wire = room.midi.messages[mark:]
    recall_at = next(i for i, m in enumerate(wire) if m.kind == "recall")
    assert wire[recall_at].value == int(PERFORMANCE_REF)
    after_recall = [(m.address, m.value) for m in wire[recall_at:] if m.kind == "set"]
    assert (LEVEL["ip1"], level(-6.0)) in after_recall
    assert (LEVEL["main"], level(-4.0)) in after_recall
    assert (LEVEL["ip2"], level(-10.0)) in after_recall
    # Never above the ceiling on anything the scene *sent*: the only values
    # above it the desk held came from its own stored scene.
    assert all(v <= level(-10.0) for a, v in after_recall if a == LEVEL["ip2"])
    assert room.midi.value(LEVEL["ip1"]) == level(-6.0)
    assert room.midi.value(LEVEL["main"]) == level(-4.0)

    lines = {line["domain"]: line for line in run["action_results"]}
    assert lines["mixer_recall"]["detail"]["clamped"] == {
        str(room.channel("wireless")): -6.0,
        str(room.mixer.main): -4.0,
    }
    assert lines["mixer_fader"]["detail"] == {
        "channel_id": room.channel("lectern"),
        "db": -10.0,
        "clamped": True,
    }
    assert lines["mixer_fader"]["fired_at_ms"] >= BAND_ADJUST_MS

    # Staff, by trigger: exactly as stored, nothing clamped.
    before = await room.newest_run(band)
    ok(await room.admin.post(f"{SCENES}/{band}/trigger"), 202)
    run = await room.completed_run(band, before)
    await desk_holds(room, LEVEL["ip2"], level(0.0), "the Lectern at 0 dB")
    assert room.midi.value(LEVEL["ip1"]) == preset[LEVEL["ip1"]]
    assert room.midi.value(LEVEL["main"]) == preset[LEVEL["main"]]
    assert "clamped" not in {k for line in run["action_results"] for k in line["detail"]}

    # Staff, by the hirer's own page button: the button is not what clamps.
    await room.midi.push(LEVEL["ip1"], level(-30.0))
    await until(lambda: room.app.state.mixer.live(room.channel("wireless")).db == -30.0, "the push")
    before = await room.newest_run(band)
    ok(await room.admin.post(f"{PAGES}/{page_id}/buttons/{button_id}"))
    run = await room.completed_run(band, before)
    assert room.midi.value(LEVEL["ip1"]) == preset[LEVEL["ip1"]]
    assert room.midi.value(LEVEL["ip2"]) == level(0.0)
    assert "clamped" not in {k for line in run["action_results"] for k in line["detail"]}


# == 6. a page edit is live ===========================================================


async def test_removing_a_channel_from_a_page_takes_effect_on_the_next_broadcast(
    room: HirerRoom,
) -> None:
    """§6.7, B31: a channel removed from a page disappears on the next
    broadcast, and is refused from the next write — even one sent the
    instant the admin's save returns — with no re-login; a channel added
    appears, on the same socket. Unassigning a whole page does the same, and
    the ``pages_changed`` frame names only what the hirer still holds."""
    phone = await room.signed_in_phone()
    socket = await phone.open_socket()
    lectern = room.channel("lectern")
    items = await room.page_items("performance")
    without = [i for i in items if i.get("channel_id") != lectern]
    assert len(without) == len(items) - 1

    t0, mark = time.monotonic(), len(room.midi.messages)
    await room.put_page("performance", without)
    # The next write, straight after the save: refused on both transports.
    refused(await phone.post(f"{MIXER}/channels/{lectern}/level", json={"db": -12.0}))
    answer = await socket.set("mixer", lectern, -12.0)
    assert answer is not None and answer["reason"] == "permission_denied", answer
    assert LEVEL["ip2"] not in {a for a, _ in sets(room.midi, mark)}

    # The next broadcast: a resync without the Lectern, then silence about it.
    resync = await socket.first(
        "mixer_state",
        lambda m: m.get("source") == "resync" and str(lectern) not in m.get("inputs", {}),
        "a resync without the Lectern",
        since=t0,
    )
    assert str(room.channel("wireless")) in resync["inputs"]
    t1 = time.monotonic()
    await room.midi.push(LEVEL["ip2"], level(-25.0))
    await room.midi.push(LEVEL["ip1"], level(-26.0))
    await frame_value(
        socket, room, "wireless", lambda e: e["db"] == -26.0, "a later frame", since=t1
    )
    assert frames_with(socket, room, "lectern", t1) == []
    page = ok(await phone.get(f"{PAGES}/{room.pages['performance']}"))
    assert lectern not in {i.get("channel_id") for i in page["items"]}
    await socket.first(
        "pages_changed",
        lambda m: m["page_ids"] == [room.pages["performance"], room.pages["foyer"]],
        "pages_changed",
        since=t0,
    )

    # Added back: it appears, on the same socket, with its ceiling.
    t2 = time.monotonic()
    await room.put_page("performance", items)
    answer = ok(await phone.post(f"{MIXER}/channels/{lectern}/level", json={"db": 0.0}))
    assert (answer["db"], answer["clamped"]) == (-10.0, True)
    await frame_value(socket, room, "lectern", lambda e: True, "the Lectern back", since=t2)

    # A whole page unassigned: its channels go, and its id with them.
    t3 = time.monotonic()
    await room.put_hirer_config(
        pages=[room.pages["performance"]],
        ceilings=[c for c in room.ceilings() if c["channel_id"] != room.channel("hdmi")],
    )
    refused(await phone.post(f"{MIXER}/channels/{room.channel('hdmi')}/level", json={"db": -30.0}))
    refused(await phone.get(f"{PAGES}/{room.pages['foyer']}"), 404, "not_found")
    await socket.first(
        "pages_changed",
        lambda m: m["page_ids"] == [room.pages["performance"]],
        "the page gone",
        since=t3,
    )
    await socket.first(
        "mixer_state",
        lambda m: (
            m.get("source") == "resync" and str(room.channel("hdmi")) not in m.get("inputs", {})
        ),
        "a resync without HDMI audio",
        since=t3,
    )
    assert [p["id"] for p in ok(await phone.get(PAGES))["pages"]] == [room.pages["performance"]]
    assert not socket.closed.is_set()
    assert len(phone.sockets) == 1


# == 7. cut off at once ================================================================


class AppliedLevels:
    """Every ``MixerService.set_level`` the application completed, with when.

    Wraps the running service's method, so each entry is the moment the level
    was handed to the driver — "applied" — as the kill switch's promise is
    worded: applied before the switch answers, or never.
    """

    def __init__(self, room: HirerRoom, monkeypatch: pytest.MonkeyPatch) -> None:
        self.done: list[tuple[float, int, float | None]] = []
        service = room.app.state.mixer
        real = service.set_level

        async def recording(channel_id: int, db: float | None, *args: Any, **kwargs: Any) -> Any:
            applied = await real(channel_id, db, *args, **kwargs)
            self.done.append((time.monotonic(), channel_id, db))
            return applied

        monkeypatch.setattr(service, "set_level", recording)


async def _racing_socket(socket: HirerSocket, channel_id: int, answers: list[Any]) -> None:
    """Drag a fader as fast as the socket takes it until the socket is gone."""
    step = 0
    while not socket.closed.is_set():
        step += 1
        answer = await socket.set("mixer", channel_id, -20.0 - (step % 40) * 0.25)
        answers.append(answer)
        if answer is None:
            return


async def _racing_rest(phone: Phone, channel_id: int, answers: list[int]) -> None:
    """Post levels back to back until one is refused."""
    step = 0
    while True:
        step += 1
        response = await phone.post(
            f"{MIXER}/channels/{channel_id}/level", json={"db": -20.0 - (step % 40) * 0.25}
        )
        answers.append(response.status_code)
        if response.status_code != 200:
            return


async def _cut_off(
    room: HirerRoom,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    body: dict[str, Any],
) -> tuple[list[Phone], dict[str, Any]]:
    """Two hirers mid-drag — one on the socket (with a second socket open on
    the same session), one over REST — when the admin throws ``path``.

    Asserts the promise and returns the phones and the admin's answer:
    every hirer socket is closed with 4003 by the time the answer arrives;
    every hirer level the application applied, it applied before the answer;
    and nothing reached the desk after it (a staff write sent after the
    answer is the sentinel: TCP keeps the driver's order, so a hirer write
    arriving after the sentinel was sent after the answer).
    """
    applied = AppliedLevels(room, monkeypatch)
    first, second = await room.signed_in_phone(PHONE), await room.signed_in_phone(SECOND_PHONE)
    dragging, spare = await first.open_socket(), await first.open_socket()
    watching = await second.open_socket()
    socket_answers: list[Any] = []
    rest_answers: list[int] = []
    races = [
        asyncio.create_task(_racing_socket(dragging, room.channel("wireless"), socket_answers)),
        asyncio.create_task(_racing_rest(second, room.channel("lectern"), rest_answers)),
    ]
    await until(
        lambda: sum(1 for a in socket_answers if a is not None) >= 5 and len(rest_answers) >= 5,
        "both hirers mid-drag",
    )

    answer = ok(await room.admin.post(f"{HIRER}/{path}", json=body))
    answered_at = time.monotonic()
    broadcaster = room.app.state.broadcaster
    assert [c for c in broadcaster.connections() if c.tier == "hirer" and not c.closed] == []
    assert answer["sessions_closed"] == 2, answer

    await asyncio.wait_for(asyncio.gather(*races), 15.0)
    for socket in (dragging, spare, watching):
        assert await socket.closed_with() == 4003
    hirer_channels = {room.channel("wireless"), room.channel("lectern")}
    late = [(at, c, v) for at, c, v in applied.done if c in hirer_channels and at > answered_at]
    assert late == [], f"applied after the switch answered: {late}"
    assert rest_answers[-1] == 401, rest_answers[-3:]
    assert any(a is not None and a["type"] == "ack" for a in socket_answers)

    # The sentinel, then nothing of the hirers' behind it.
    sentinel = level(-40.0)
    ok(
        await room.admin.post(
            f"{MIXER}/channels/{room.channel('wireless')}/level", json={"db": -40.0}
        )
    )
    await desk_holds(room, LEVEL["ip1"], sentinel, "the staff sentinel")
    wire = [(m.address, m.value) for m in room.midi.messages if m.kind == "set"]
    at = len(wire) - 1 - wire[::-1].index((LEVEL["ip1"], sentinel))
    behind = [w for w in wire[at + 1 :] if w[0] in (LEVEL["ip1"], LEVEL["ip2"])]
    assert behind == [], behind

    # Refused from here: REST, the socket upgrade, and the old socket's token.
    for phone in (first, second):
        response = await phone.get(PAGES)
        refused(response, 401, "unauthenticated")
        assert error(response)["detail"]["reason"] == "hirer_revoked"
        assert await phone.upgrade_status() == 401

    # One forced_logout per session, not per socket, each naming its own.
    rows = [r for r in await events(db, "forced_logout")]
    by_session = {detail(r)["session_id"]: r for r in rows}
    assert set(by_session) == {phone_session(first), phone_session(second)}
    assert detail(by_session[phone_session(first)])["connections"] == 2
    assert by_session[phone_session(first)].ip_address == PHONE
    assert by_session[phone_session(second)].ip_address == SECOND_PHONE
    return [first, second], answer


async def test_disabling_access_cuts_every_hirer_off_at_once(
    room: HirerRoom, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§6.6: "The enabled toggle is an instant kill switch — disabling
    prevents new logins and drops existing hirer WebSocket connections
    immediately." Every socket closes 4003 before the admin is answered; a
    write racing the switch lands before the answer or not at all; the next
    REST call and upgrade are refused; so is a new sign-in, even with the
    right PIN; and re-enabling for the next hire does not revive this one's
    phones (Q7)."""
    phones, answer = await _cut_off(room, db, monkeypatch, "enabled", {"enabled": False})
    assert answer["enabled"] is False
    newcomer = room.phone(PHONE, "newcomer")
    response = await newcomer.sign_in()
    refused(response, 403, "permission_denied")
    assert error(response)["detail"]["reason"] == "hirer_disabled"
    toggled = (await events(db, "access_toggled"))[0]
    assert (detail(toggled)["enabled"], detail(toggled)["sessions_closed"]) == (False, 2)
    assert (toggled.user_ident, toggled.ip_address) == ("admin", BOOTH)
    assert {detail(r)["reason"] for r in await events(db, "forced_logout")} == {"access_disabled"}

    ok(await room.admin.post(f"{HIRER}/enabled", json={"enabled": True}))
    for phone in phones:
        refused(await phone.get(PAGES), 401, "unauthenticated")
    ok(await newcomer.sign_in())
    ok(await newcomer.get(PAGES))


async def test_a_pin_change_invalidates_every_token(
    room: HirerRoom, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§22.4's named scenario, "token invalidation on PIN change" (§6.4):
    the admin sets a new PIN between hires, and every token issued under the
    old one is dead — sockets closed 4003 at once, a racing write applied
    before the answer or never, REST and upgrades refused — while access
    stays enabled; the old PIN no longer signs in, and the new one does."""
    phones, answer = await _cut_off(room, db, monkeypatch, "pin", {"pin": "271828"})
    assert "pin" not in answer  # a PIN the admin chose is never echoed
    changed = (await events(db, "pin_changed"))[0]
    assert detail(changed)["sessions_closed"] == 2 and detail(changed)["generated"] is False
    assert {detail(r)["reason"] for r in await events(db, "forced_logout")} == {"pin_changed"}
    assert (await room.hirer_config())["enabled"] is True

    refused(await phones[0].sign_in(PIN), 401, "unauthenticated")
    ok(await phones[0].sign_in("271828"))
    ok(await phones[0].get(PAGES))
    # A generated PIN is shown once, and closes the new session in turn.
    socket = await phones[0].open_socket()
    generated = ok(await room.admin.post(f"{HIRER}/pin", json={"generate": True}))
    assert len(generated["pin"]) == 6 and generated["pin"].isdigit()
    assert generated["sessions_closed"] == 1
    assert await socket.closed_with() == 4003
    ok(await phones[1].sign_in(generated["pin"]))


# == 8. the absolute expiry ============================================================


async def test_the_absolute_expiry_closes_rest_and_an_open_socket_and_reissue_never_extends_it(
    room: HirerRoom,
) -> None:
    """§6.4: 12 hours absolute, every tier, "nothing lives past it". A
    hirer's phone that stays in the foreground re-issues its session every
    twenty minutes for twelve hours; each re-issue carries ``aexp`` forward
    unchanged; at ``aexp`` the open socket closes with 4002 and REST
    refuses. Meanwhile (B65) the idle expiry is REST's alone: a phone left
    hidden past thirty minutes loses REST but keeps its socket."""
    phone = await room.signed_in_phone()
    socket = await phone.open_socket()
    aexp = ok(await phone.get(f"{AUTH}/session"))["absolute_expires_at"]
    first_claims = token_claims(phone)

    for _ in range(35):  # 35 × 20 minutes: eleven hours and forty minutes
        room.clock.advance(minutes=20)
        session = ok(await phone.get(f"{AUTH}/session"))
        assert session["absolute_expires_at"] == aexp
        assert token_claims(phone)["aexp"] == first_claims["aexp"]
    assert not socket.closed.is_set()
    t0 = time.monotonic()
    await socket.send({"type": "ping"})
    await socket.first("pong", lambda m: True, "the socket alive at 11 h 40", since=t0)
    ok(await phone.post(f"{MIXER}/channels/{room.channel('wireless')}/level", json={"db": -20.0}))

    room.clock.advance(minutes=20, seconds=1)  # past aexp
    assert await socket.closed_with() == 4002
    refused(await phone.get(PAGES), 401, "unauthenticated")
    refused(await phone.get(f"{AUTH}/session"), 401, "unauthenticated")
    assert await phone.upgrade_status() == 401

    # A fresh sign-in is a fresh session, with its own twelve hours.
    ok(await phone.sign_in())
    assert token_claims(phone)["aexp"] > first_claims["aexp"]

    # B65: hidden for 45 minutes, REST has expired; the socket has not.
    hidden = await phone.open_socket()
    room.clock.advance(minutes=45)
    refused(await phone.get(PAGES), 401, "unauthenticated")
    t1 = time.monotonic()
    await hidden.send({"type": "ping"})
    await hidden.first("pong", lambda m: True, "the socket alive past the idle expiry", since=t1)
    assert not hidden.closed.is_set()


# == 9. the PIN lockout ================================================================


async def test_the_pin_lockout_holds_per_real_address_whatever_the_headers_claim(
    room: HirerRoom, db: Database
) -> None:
    """§6.8: three PIN attempts in ten minutes, then thirty minutes locked,
    per real client address — the one nginx saw (§4.13). An attacker who
    forges ``X-Forwarded-For`` and ``X-Real-IP`` on every attempt is still
    one address: the right PIN on the fourth attempt is refused
    ``rate_limited``, the hire's own phone on another address is not, and
    the lock lifts at thirty minutes, not before. More than ten failures an
    hour from one address, across both sign-ins, raise the alert once."""
    assert nginx_forwards_as_modelled(), "the nginx configuration no longer forwards as modelled"
    attacker = room.phone(ATTACKER, "attacker")

    def forged(i: int) -> dict[str, str]:
        return {"X-Forwarded-For": f"10.9.{i}.1, 10.8.{i}.2", "X-Real-IP": f"10.7.{i}.3"}

    for i, pin in enumerate(("000000", "111111", "222222")):
        refused(await attacker.sign_in(pin, headers=forged(i)), 401, "unauthenticated")
    locked = await attacker.sign_in(PIN, headers=forged(3))
    refused(locked, 429, "rate_limited")
    assert 1700 <= error(locked)["detail"]["retry_after"] <= 1800
    # The hire's own phone, elsewhere in the room: unaffected.
    ok(await room.phone(PHONE).sign_in())

    failures = [r for r in await events(db, "login_failure") if r.ip_address == ATTACKER]
    assert len(failures) == 3 and {detail(r)["reason"] for r in failures} == {"pin"}
    (lockout,) = await events(db, "lockout")
    assert (lockout.ip_address, detail(lockout)["lockout_seconds"]) == (ATTACKER, 1800)
    assert {r.ip_address for r in await events(db, "login_failure")} == {ATTACKER}

    room.clock.advance(minutes=29)
    refused(await attacker.sign_in(PIN, headers=forged(4)), 429, "rate_limited")
    room.clock.advance(minutes=1, seconds=1)
    ok(await attacker.sign_in(PIN, headers=forged(5)))

    # The alert: a second address, eleven failures inside the hour — three
    # PINs (then locked), five staff passwords (then locked), and after the
    # staff lock lifts three more — each under a different forged address.
    prober = room.phone("203.0.113.67", "prober")
    for i in range(3):
        refused(await prober.sign_in(f"99999{i}", headers=forged(10 + i)), 401, "unauthenticated")
    for i in range(5):
        response = await prober.post(
            f"{AUTH}/login", json={"password": f"wrong-{i}-password"}, headers=forged(20 + i)
        )
        refused(response, 401, "unauthenticated")
    assert room.appliance.alerts == []
    room.clock.advance(minutes=16)
    for i in range(3):
        response = await prober.post(
            f"{AUTH}/login", json={"password": f"again-{i}-password"}, headers=forged(30 + i)
        )
        refused(response, 401, "unauthenticated")
    assert [(a.ip, a.count) for a in room.appliance.alerts] == [("203.0.113.67", 11)]


# == 10. the audit trail ===============================================================


async def test_every_hirer_security_event_is_audited_against_the_real_address(
    room: HirerRoom, db: Database
) -> None:
    """§6.14's closed list, as a hire produces it: sign-in success, failure
    and lockout; ``permission_denied`` on both transports; ``config_changed``;
    ``pin_changed`` and ``access_toggled``; one ``forced_logout`` per session.
    Every row carries the address nginx saw — never the proxy's, never a
    forged one — and the hirer rows the session they belong to."""
    forged = {"X-Forwarded-For": "10.66.0.1", "X-Real-IP": "10.66.0.2"}
    phone = room.phone(PHONE)
    refused(await phone.sign_in("000000", headers=forged), 401, "unauthenticated")
    ok(await phone.sign_in(headers=forged))
    sid = phone_session(phone)
    socket = await phone.open_socket()
    refused(await phone.post(f"{MIXER}/channels/{room.channel('foldback')}/level", json={"db": 0}))
    answer = await socket.set("mixer", room.channel("monitors"), 0.0)
    assert answer is not None and answer["reason"] == "permission_denied"
    await room.put_hirer_config(colour_enabled=False)
    ok(await room.admin.post(f"{HIRER}/pin", json={"pin": "161803"}))
    assert await socket.closed_with() == 4003
    ok(await phone.sign_in("161803"))
    second_sid = phone_session(phone)
    second = await phone.open_socket()
    ok(await room.admin.post(f"{HIRER}/enabled", json={"enabled": False}))
    assert await second.closed_with() == 4003
    attacker = room.phone(ATTACKER, "attacker")
    ok(await room.admin.post(f"{HIRER}/enabled", json={"enabled": True}))
    for pin in ("000001", "000002", "000003"):
        await attacker.sign_in(pin, headers=forged)

    rows = await security_events.query(db, limit=1000)
    seen = {r.event_type for r in rows}
    assert seen >= {
        "login_success",
        "login_failure",
        "lockout",
        "permission_denied",
        "config_changed",
        "pin_changed",
        "access_toggled",
        "forced_logout",
    }, seen
    # Real addresses only: the phone's, the attacker's, the booth's.
    assert {r.ip_address for r in rows if r.ip_address} <= {PHONE, ATTACKER, BOOTH}, {
        r.ip_address for r in rows
    }
    successes = [r for r in rows if r.event_type == "login_success" and r.user_ident == "hirer"]
    assert {detail(r)["session_id"] for r in successes} == {sid, second_sid}
    denied = [r for r in rows if r.event_type == "permission_denied"]
    assert {detail(r).get("transport", "rest") for r in denied} == {"websocket", "rest"}
    assert {detail(r)["session_id"] for r in denied} == {sid}
    changed = next(r for r in rows if r.event_type == "config_changed")  # newest first
    assert (changed.user_ident, changed.ip_address, detail(changed)["colour_enabled"]) == (
        "admin",
        BOOTH,
        False,
    )
    logouts = [r for r in rows if r.event_type == "forced_logout"]
    assert sorted((detail(r)["session_id"], detail(r)["reason"]) for r in logouts) == sorted(
        [(sid, "pin_changed"), (second_sid, "access_disabled")]
    )
    assert {r.ip_address for r in logouts} == {PHONE}
    toggles = [detail(r)["enabled"] for r in rows if r.event_type == "access_toggled"]
    assert toggles[:2] == [True, False]  # newest first: off, then on for the next hire
    (lockout,) = [r for r in rows if r.event_type == "lockout"]
    assert lockout.ip_address == ATTACKER


# == 11. Q3's switches =================================================================


async def test_the_three_switches_narrow_what_lighting_a_hirer_reaches(room: HirerRoom) -> None:
    """Q3, §21.15. Individual fixtures off: the bank's members are shown
    read-only — refused on both transports, the socket's ``nack`` carrying
    the value the tray shows — and only the master moves. On: the members
    move too. Colour off: colour is refused and the cyc keeps its colour,
    while its level still moves. Lighting off: nothing lighting is reachable,
    shown, or sent."""
    phone = await room.signed_in_phone()
    socket = await phone.open_socket()
    fixture = room.lighting.fixtures[0]
    page_id = room.pages["performance"]

    # Staff bring the bank up; the hirer has only the master.
    ok(
        await room.admin.post(
            f"{LIGHTING}/levels", json={"levels": {str(f): 80.0 for f in room.lighting.fixtures}}
        )
    )
    await until(lambda: dmx(room, 1) == level_to_dmx(80), "the bank at 80 %")
    mark = len(room.artnet.received)
    refused(await phone.post(f"{LIGHTING}/channels/{fixture}/level", json={"level": 10}))
    answer = await socket.set("lighting", fixture, 10.0)
    assert answer is not None and (answer["reason"], answer["value"]) == ("permission_denied", 80.0)
    assert dmx_values_seen(room, 1, mark) <= {level_to_dmx(80)}
    assert dmx(room, 1) == level_to_dmx(80)
    answer = await socket.set("lighting_group", room.lighting.bank, 50.0)
    assert answer is not None and answer["type"] == "ack", answer
    # A group fader sets its members' levels (owner decision 2026-09-30).
    await until(lambda: dmx(room, 1) == level_to_dmx(50), "the bank's master at 50 %")
    ok(await phone.post(f"{LIGHTING}/groups/{room.lighting.bank}/level", json={"level": 80}))
    await until(lambda: dmx(room, 1) == level_to_dmx(80), "the bank's master at 80 %")

    # Individual fixtures on: the tray is writable.
    await room.put_hirer_config(individual_fixtures=True)
    group = next(
        i for i in ok(await phone.get(f"{PAGES}/{page_id}"))["items"] if i["kind"] == "group_master"
    )
    assert group["members_writable"] is True
    ok(await phone.post(f"{LIGHTING}/channels/{fixture}/level", json={"level": 20}))
    await until(lambda: dmx(room, 1) == level_to_dmx(20), "the member at 20 %")
    answer = await socket.set("lighting", fixture, 30.0)
    assert answer is not None and answer["type"] == "ack", answer

    # Colour: on, the cyc (placed directly) takes it; off, it keeps it.
    ok(await phone.post(f"{LIGHTING}/channels/{room.cyc}/level", json={"level": 100}))
    ok(await phone.post(f"{LIGHTING}/channels/{room.cyc}/colour", json={"r": 255, "g": 0, "b": 0}))
    await until(
        lambda: (dmx(room, CYC_ADDRESS), dmx(room, CYC_ADDRESS + 1)) == (255, 0), "the cyc red"
    )
    await room.put_hirer_config(colour_enabled=False)
    mark = len(room.artnet.received)
    refused(
        await phone.post(f"{LIGHTING}/channels/{room.cyc}/colour", json={"r": 0, "g": 0, "b": 255})
    )
    ok(await phone.post(f"{LIGHTING}/channels/{room.cyc}/level", json={"level": 50}))
    await until(lambda: dmx(room, CYC_ADDRESS) not in (None, 255), "the cyc dimmed")
    assert dmx_values_seen(room, CYC_ADDRESS + 2, mark) == {0}, "blue reached the cyc"

    # Lighting off: nothing lighting, anywhere.
    await room.put_hirer_config(lighting_enabled=False)
    await until(
        lambda: not room.app.state.state_store.hirer.permissions.lighting_enabled,
        "lighting off for hirers",
    )
    mark = len(room.artnet.received)
    for page in (page_id, room.pages["foyer"]):
        items = ok(await phone.get(f"{PAGES}/{page}"))["items"]
        assert all(i.get("source") != "lighting" and i["kind"] != "group_master" for i in items)
    refused(await phone.post(f"{LIGHTING}/groups/{room.lighting.bank}/level", json={"level": 0}))
    refused(await phone.post(f"{LIGHTING}/channels/{room.cyc}/level", json={"level": 0}))
    refused(await phone.post(f"{LIGHTING}/channels/{fixture}/level", json={"level": 0}))
    for domain, target in (("lighting_group", room.lighting.bank), ("lighting", room.cyc)):
        answer = await socket.set(domain, target, 0.0)
        assert answer is not None and answer["reason"] == "permission_denied", answer
    state = ok(await phone.get(f"{LIGHTING}/state"))
    assert not state.get("channels") and not state.get("groups"), state
    # Staff move the lights: the hirer's socket hears nothing of it.
    t1, before = time.monotonic(), dmx(room, CYC_ADDRESS)
    ok(await room.admin.post(f"{LIGHTING}/channels/{room.cyc}/level", json={"level": 10}))
    await until(lambda: dmx(room, CYC_ADDRESS) != before, "the staff change on the wire")
    await socket.send({"type": "ping"})
    await socket.first("pong", lambda m: True, "a round trip after the staff change", since=t1)
    assert socket.of_type("lighting_state", t1) == []
    assert dmx_values_seen(room, 1, mark) <= {level_to_dmx(30)}
    assert dmx(room, 1) == level_to_dmx(30)


# == 12. Q4: outputs never, Main only when placed ======================================


async def test_outputs_are_never_reachable_and_main_only_on_an_assigned_page(
    room: HirerRoom,
) -> None:
    """Q4 as amended. The Foldback output sits on an assigned page and is
    still never reachable: omitted from the page, refused on both
    transports, absent from Hirer Access's ceilings, refused a ceiling, and
    flagged by the page validator. Main is reachable while it is on an
    assigned page, clamped at its ceiling, and not once it is taken off."""
    phone = await room.signed_in_phone()
    socket = await phone.open_socket()
    foldback, main = room.channel("foldback"), room.mixer.main

    config = await room.hirer_config()
    assert foldback not in {c["channel_id"] for c in config["ceilings"]}
    assert main in {c["channel_id"] for c in config["ceilings"]}
    rejected = await room.admin.put(
        f"{HIRER}/config",
        json={
            "pages": config["pages"],
            "ceilings": [*room.ceilings(), {"channel_id": foldback, "hirer_max_db": -10.0}],
            "lighting_enabled": True,
            "individual_fixtures": False,
            "colour_enabled": True,
        },
        headers={"If-Unmodified-Since-Version": config["updated_at"]},
    )
    refused(rejected, 422, "validation_failed")
    findings = ok(await room.admin.get(f"{PAGES}/{room.pages['performance']}/validate"))["findings"]
    assert "output_on_hirer_page" in {f["code"] for f in findings}

    mark = len(room.midi.messages)
    refused(await phone.post(f"{MIXER}/channels/{foldback}/level", json={"db": -30.0}))
    refused(await phone.post(f"{MIXER}/channels/{foldback}/mute", json={"muted": True}))
    answer = await socket.set("mixer", foldback, -30.0)
    assert answer is not None and answer["reason"] == "permission_denied"
    assert {a for a, _ in sets(room.midi, mark)} & {LEVEL["out3"], MUTE["out3"]} == set()

    answer = ok(await phone.post(f"{MIXER}/channels/{main}/level", json={"db": 3.0}))
    assert (answer["db"], answer["clamped"]) == (-4.0, True)

    # Main taken off the page: gone, and refused, on the same socket.
    t0 = time.monotonic()
    items = await room.page_items("performance")
    await room.put_page("performance", [i for i in items if i.get("channel_id") != main])
    refused(await phone.post(f"{MIXER}/channels/{main}/level", json={"db": -30.0}))
    answer = await socket.set("mixer", main, -30.0)
    assert answer is not None and answer["reason"] == "permission_denied", answer
    await socket.first(
        "mixer_state",
        lambda m: m.get("source") == "resync" and m.get("main") is None,
        "a resync with Main gone",
        since=t0,
    )
    state = ok(await phone.get(f"{MIXER}/state"))
    assert state["main"] is None
    assert main not in {c["channel_id"] for c in (await room.hirer_config())["ceilings"]}
    assert room.midi.value(LEVEL["main"]) == level(-4.0)


# == 13. Settling on lighting edits, and a button's devices ==================


async def test_removing_a_fixture_from_a_group_is_refused_the_instant_the_save_answers(
    room: HirerRoom,
) -> None:
    """Settling on lighting edits (§6.7): a lighting channel or group
    edit waits for the hirer permission rebuild before answering, exactly as
    page, hirer-configuration and mixer-channel edits already do. An admin
    removes a fixture from the hirer-reachable bank; the hirer's write to it,
    sent the instant the save returns, is refused rather than racing the
    resolver's own asynchronous rebuild off the event bus."""
    phone = await room.signed_in_phone()
    socket = await phone.open_socket()
    await room.put_hirer_config(individual_fixtures=True)
    fixture = room.lighting.fixtures[0]

    ok(await phone.post(f"{LIGHTING}/channels/{fixture}/level", json={"level": 40}))

    group = ok(await room.admin.get(f"{LIGHTING}/groups/{room.lighting.bank}"))
    remaining = [c for c in group["channel_ids"] if c != fixture]
    ok(
        await room.admin.put(
            f"{LIGHTING}/groups/{room.lighting.bank}",
            json={"channel_ids": remaining},
            headers={VERSION_HEADER: group["updated_at"]},
        )
    )
    # The instant the save answers, the fixture is already unreachable — no
    # window in which a write racing the save still lands (§6.7's table).
    refused(await phone.post(f"{LIGHTING}/channels/{fixture}/level", json={"level": 60}))
    answer = await socket.set("lighting", fixture, 70.0)
    assert answer is not None and answer["reason"] == "permission_denied", answer


async def test_a_dead_projector_raises_the_hirers_banner_pipeline_for_a_button_that_powers_it(
    room: HirerRoom,
) -> None:
    """A milestone gap once found: the offline banner for a
    device behind a *button* (§21.15), not just a channel item. A panel
    button's rule runs a scene with a ``projector_power`` action; the
    projector is offline before the hirer ever signs in. Proves both halves
    of the pipeline the hirer surface's `HirerDeviceBanner` relies on: the
    button names "projector" in its own `devices`, and the
    `device_status` frame for it reaches the hirer's socket regardless of
    page assignment (`devices` is a hirer domain, unfiltered by content)."""
    # A PJLink projector whose port nothing is listening on: connect() fails
    # at once, no need to wait through the driver's own probe cadence.
    probe = PJLinkStub()
    await probe.start()
    dead_port = probe.port
    await probe.stop()
    projector = ok(
        await room.admin.post(
            DEVICES,
            json={
                "category": "projector",
                "driver_key": "pjlink",
                "name": "Dead projector",
                "config": {
                    "transport": {"type": "tcp", "host": "127.0.0.1", "port": dead_port},
                    "driver": {},
                },
            },
        ),
        201,
    )
    await wait_for_status(room.admin, projector["id"], "error")

    scene = ok(await room.admin.post(SCENES, json={"name": "Power projector"}), 201)["id"]
    ok(
        await room.admin.post(
            f"{SCENES}/{scene}/actions",
            json={
                "domain": "projector_power",
                "sort_order": 0,
                "delay_ms": 0,
                "projector_power": "on",
            },
        ),
        201,
    )
    rule = ok(
        await room.admin.post(
            RULES,
            json={
                "name": "Page: projector",
                "trigger_type": "surface",
                "action_type": "run_scene",
                "scene_id": scene,
            },
        ),
        201,
    )["id"]

    items = await room.page_items("foyer")
    button = {"col": 0, "row": 0, "label": "Screen up", "rule_id": rule, "confirm": False}
    items.append(
        {"kind": "panel", "panel_title": "Room", "panel_width": 1, "buttons": [button]}
    )
    await room.put_page("foyer", items)

    phone = await room.signed_in_phone()
    page = ok(await phone.get(f"{PAGES}/{room.pages['foyer']}"))
    panel = next(i for i in page["items"] if i["kind"] == "panel")
    assert panel["buttons"][0]["devices"] == ["projector"]

    # A fresh socket's resync already carries the offline status (it was set
    # before the hirer ever signed in) — the same "devices" domain the
    # banner's own `useDeviceStatus` reads, sent unfiltered to a hirer.
    socket = await phone.open_socket()
    await socket.first(
        "device_status",
        lambda m: m.get("device") == "projector" and m.get("status") == "error",
        "the dead projector's status reaching the hirer",
    )
