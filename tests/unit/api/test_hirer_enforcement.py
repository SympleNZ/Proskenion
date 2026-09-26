"""Server-side enforcement of a hirer's reach and ceilings, on REST and the socket.

Spec §6.7 (limits are enforced in the handlers, never only in the UI; the
live-effect table), §15.4 (pages decide what, ceilings how far), §16.5, §16.8
(the clamp ``nack``), §6.14 (``permission_denied`` rows) and §22.4 (a value
the UI would never send). The phase-5 contract fixes the answers: a clamped
mixer write is a success — ``200`` with ``"clamped": true`` on REST,
``nack`` ``value_out_of_range`` with the clamped dB on the socket (Q9, B35) —
and only an unreachable target is ``permission_denied``.

The application runs for real under the test client — lifespan, permission
resolver, device manager, the stub mixer driver and the lighting service —
so a hirer's write travels exactly the path it would in production. The
driver's ``set_level`` and the lighting service's writes are recorded, so
"never reaches the driver" is asserted on what was actually called.

The venue: a page assigned to the hirer holds inputs 1 and 2, Main, output
1, lighting channel L1 and the master of group G1 (members L2 and L3). Input
3 and lighting channel L4 (in group G2) are on no page. Ceilings: input 1 at
-10 dB, Main at -6 dB, input 3 at -10 dB (unreachable, so moot); input 2 has
none. Lighting is on, individual fixtures off, colour on.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient, WebSocketTestSession

from proskenion.api.app import API_PREFIX, create_app
from proskenion.config import AppSection, Config, DatabaseSection, LoggingSection, ServerSection
from proskenion.core import setup
from proskenion.core.broadcast import Broadcaster
from proskenion.core.bus import EventBus
from proskenion.core.drivers.stub_mixer import StubMixerDriver
from proskenion.core.hirer_permissions import HirerPermissionResolver
from proskenion.core.lighting import LightingService
from proskenion.core.mixer.service import MixerService
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import hirer as hirer_crud
from proskenion.db.crud import lighting as lighting_crud
from proskenion.db.crud import mixer as mixer_crud
from proskenion.db.crud import pages as pages_crud
from proskenion.db.crud import security_events, system_state
from proskenion.db.crud.pages import PageItemInput
from tests.unit.api.conftest import ADMIN_PASSWORD, HIRER_PIN, OPERATOR_PASSWORD
from tests.unit.api.test_ws import HOSTNAME, ORIGIN, _seed
from tests.unit.api.test_ws_session import QUIET_PING_S, connect, sign_in, wait_for

MIXER = f"{API_PREFIX}/mixer"
LIGHTING = f"{API_PREFIX}/lighting"
STUB_CONFIG: dict[str, Any] = {"transport": {"type": "loopback"}, "driver": {}}


# -- the venue ---------------------------------------------------------------------------


async def _seed_venue(path: Path) -> dict[str, int]:
    await _seed(path)  # staff passwords, the PIN, access enabled
    db = Database()
    await db.open(path)
    try:
        await system_state.set(db, setup.SYSTEM_DOMAIN, setup.FIRST_RUN_COMPLETED_KEY, "true")
        mixer = await devices_crud.create(
            db, category="mixer", driver_key="stub", name="Mixer", config=STUB_CONFIG
        )
        ids: dict[str, int] = {"mixer": mixer.id}
        for key, kind, ref, ceiling in (
            ("main", "main", "main", -6.0),
            ("in1", "input", "in1", -10.0),
            ("in2", "input", "in2", None),
            ("in3", "input", "in3", -10.0),
            ("out1", "output", "out1", None),
        ):
            channel = await mixer_crud.create_channel(
                db, device_id=mixer.id, channel_kind=kind, name=key, hirer_max_db=ceiling
            )
            await mixer_crud.set_channel_refs(db, channel.id, [ref])
            ids[key] = channel.id
        dmx = await devices_crud.create(
            db, category="lighting_output", driver_key="artnet", name="eDMX8", config={}
        )
        for key, address, profile in (("L1", 1, 2), ("L2", 4, 1), ("L3", 5, 1), ("L4", 6, 1)):
            light = await lighting_crud.create_channel(
                db, name=key, type="dmx", profile_id=profile, device_id=dmx.id, address=address
            )
            ids[key] = light.id
        g1 = await lighting_crud.create_group(db, name="Wash")
        await lighting_crud.set_group_members(db, g1.id, [ids["L2"], ids["L3"]])
        g2 = await lighting_crud.create_group(db, name="Cyc")
        await lighting_crud.set_group_members(db, g2.id, [ids["L4"]])
        ids["G1"], ids["G2"] = g1.id, g2.id
        page = await pages_crud.create_page(db, name="Performance", sort_order=1)
        await pages_crud.replace_page(
            db,
            page.id,
            page.updated_at,
            name=page.name,
            sort_order=page.sort_order,
            items=[
                PageItemInput(kind="channel", channel_id=ids["in1"]),
                PageItemInput(kind="channel", channel_id=ids["in2"]),
                PageItemInput(kind="channel", channel_id=ids["main"]),
                PageItemInput(kind="channel", channel_id=ids["out1"]),
                PageItemInput(kind="channel", lighting_channel_id=ids["L1"]),
                PageItemInput(kind="group_master", group_id=ids["G1"]),
            ],
        )
        ids["page"] = page.id
        await pages_crud.replace_hirer_pages(db, [page.id])
        await _set_switches(db, lighting=True, individual=False, colour=True)
        return ids
    finally:
        await db.close()


async def _set_switches(db: Database, *, lighting: bool, individual: bool, colour: bool) -> None:
    row = await hirer_crud.get(db)
    await hirer_crud.update(
        db,
        {"lighting_enabled": lighting, "individual_fixtures": individual, "colour_enabled": colour},
        expected_updated_at=row.updated_at,
        updated_by=None,
    )


@dataclass
class Recorder:
    """What reached the mixer driver and the lighting service."""

    mixer: list[tuple[tuple[str, ...], float | None]] = field(default_factory=list)
    lighting: list[tuple[str, int]] = field(default_factory=list)

    def mixer_refs(self) -> set[str]:
        return {ref for refs, _ in self.mixer for ref in refs}


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    record = Recorder()
    set_level = StubMixerDriver.set_level

    async def recording_set_level(self: StubMixerDriver, refs: list[str], db: float | None) -> None:
        record.mixer.append((tuple(refs), db))
        await set_level(self, refs, db)

    monkeypatch.setattr(StubMixerDriver, "set_level", recording_set_level)
    for name in ("set_level", "set_colour", "set_group_multiplier"):
        original = getattr(LightingService, name)

        def recording(
            self: LightingService,
            target: int,
            *args: Any,
            _name: str = name,
            _o: Any = original,
            **kwargs: Any,
        ) -> Any:
            record.lighting.append((_name, target))
            return _o(self, target, *args, **kwargs)

        monkeypatch.setattr(LightingService, name, recording)
    return record


@dataclass
class Venue:
    app: FastAPI
    client: TestClient
    ids: dict[str, int]
    hirer: str
    operator: str
    admin: str

    @property
    def mixer(self) -> MixerService:
        service: MixerService = self.app.state.mixer
        return service

    @property
    def state(self) -> StateStore:
        store: StateStore = self.app.state.state_store
        return store

    def run(self, work: Callable[[Database], Awaitable[None]]) -> None:
        """Run ``work`` against the application's database, on its loop."""

        async def call() -> None:
            await work(self.app.state.db)

        assert self.client.portal is not None
        self.client.portal.call(call)

    def rebuild(self) -> None:
        resolver: HirerPermissionResolver = self.app.state.hirer_permissions

        async def call() -> None:
            await resolver.rebuild(reason="test")

        assert self.client.portal is not None
        self.client.portal.call(call)

    def post(self, cookie: str, path: str, body: dict[str, Any]) -> Any:
        return self.client.post(path, json=body, headers={"Cookie": cookie})

    def get(self, cookie: str, path: str) -> Any:
        return self.client.get(path, headers={"Cookie": cookie})

    def db_of(self, channel: str) -> float | None:
        live = self.mixer.live(self.ids[channel])
        assert live is not None
        return live.db

    def denials(self) -> list[dict[str, Any]]:
        found: list[security_events.SecurityEvent] = []

        async def read(db: Database) -> None:
            found.extend(await security_events.query(db, event_type="permission_denied"))

        self.run(read)
        return [json.loads(r.detail or "{}") for r in found]


@pytest.fixture
def venue(tmp_path: Path, recorder: Recorder) -> Iterator[Venue]:
    config = Config(
        database=DatabaseSection(path=tmp_path / "auditorium.db"),
        logging=LoggingSection(path=tmp_path / "logs"),
        server=ServerSection(hostname=HOSTNAME),
        app=AppSection(state_dir=tmp_path / "appliance", data_dir=tmp_path / "data"),
    )
    ids = asyncio.run(_seed_venue(config.database.path))
    bus = EventBus()
    state = StateStore(config, bus)
    broadcaster = Broadcaster(state, bus, fps=60.0, ping_interval_s=QUIET_PING_S)
    app = create_app(config, broadcaster=broadcaster)
    with TestClient(app, base_url=ORIGIN) as client:
        hirer = sign_in(client, pin=HIRER_PIN)
        operator = sign_in(client, password=OPERATOR_PASSWORD)
        admin = sign_in(client, password=ADMIN_PASSWORD)
        service: MixerService = app.state.mixer
        wait_for(lambda: service.connected and service.is_configured(ids["in1"]))
        wait_for(lambda: state.hirer.permissions.mixer_reachable(ids["in1"]))
        recorder.mixer.clear()
        recorder.lighting.clear()
        yield Venue(app, client, ids, hirer, operator, admin)


def error_code(response: Any) -> str:
    return str(response.json()["error"]["code"])


def answer(ws: WebSocketTestSession, message: dict[str, Any]) -> dict[str, Any]:
    """Send one ``set`` and return its ``ack`` or ``nack``, skipping state frames."""
    ws.send_json(message)
    for _ in range(50):
        reply: dict[str, Any] = ws.receive_json()
        if reply["type"] in ("ack", "nack"):
            return reply
    raise AssertionError("no ack or nack")


def mixer_set(channel_id: int, value: float | None, token: int) -> dict[str, Any]:
    return {"type": "set", "domain": "mixer", "id": channel_id, "value": value, "token": token}


# -- mixer level over REST -------------------------------------------------------------------


def test_a_hirer_level_within_the_ceiling_is_applied_as_sent(
    venue: Venue, recorder: Recorder
) -> None:
    response = venue.post(venue.hirer, f"{MIXER}/channels/{venue.ids['in1']}/level", {"db": -20.0})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["db"] == -20.0
    assert body["clamped"] is False
    assert recorder.mixer == [(("in1",), -20.0)]


def test_a_level_above_the_ceiling_sent_directly_lands_at_the_ceiling(
    venue: Venue, recorder: Recorder
) -> None:
    """§22.4: a value the interface would never send, straight at the API."""
    response = venue.post(venue.hirer, f"{MIXER}/channels/{venue.ids['in1']}/level", {"db": 6.0})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["clamped"] is True
    assert body["db"] == -10.0
    assert body["channel_id"] == venue.ids["in1"]
    assert recorder.mixer == [(("in1",), -10.0)]  # never the value asked for
    wait_for(lambda: venue.db_of("in1") == -10.0)


def test_main_on_an_assigned_page_is_clamped_to_its_ceiling(
    venue: Venue, recorder: Recorder
) -> None:
    response = venue.post(venue.hirer, f"{MIXER}/channels/{venue.ids['main']}/level", {"db": 0.0})
    assert response.status_code == 200, response.text
    assert response.json()["clamped"] is True
    assert response.json()["db"] == -6.0
    assert recorder.mixer == [(("main",), -6.0)]


def test_a_channel_with_no_ceiling_and_off_are_never_clamped(
    venue: Venue, recorder: Recorder
) -> None:
    loud = venue.post(venue.hirer, f"{MIXER}/channels/{venue.ids['in2']}/level", {"db": 5.0})
    assert loud.status_code == 200 and loud.json()["clamped"] is False
    off = venue.post(venue.hirer, f"{MIXER}/channels/{venue.ids['in1']}/level", {"db": None})
    assert off.status_code == 200 and off.json()["clamped"] is False
    assert off.json()["db"] is None
    assert recorder.mixer == [(("in2",), 5.0), (("in1",), None)]


def test_staff_are_never_held_to_a_hirer_ceiling(venue: Venue, recorder: Recorder) -> None:
    response = venue.post(venue.operator, f"{MIXER}/channels/{venue.ids['in1']}/level", {"db": 6.0})
    assert response.status_code == 200, response.text
    assert "clamped" not in response.json()
    assert recorder.mixer == [(("in1",), 6.0)]


@pytest.mark.parametrize("target", ["in3", "out1", "missing"])
def test_an_unreachable_level_is_refused_audited_and_never_reaches_the_driver(
    venue: Venue, recorder: Recorder, target: str
) -> None:
    """Off every page (in3), an output on an assigned page (out1), or no such
    channel at all: the same answer, so the refusal leaks nothing."""
    channel_id = venue.ids.get(target, 99999)
    response = venue.post(venue.hirer, f"{MIXER}/channels/{channel_id}/level", {"db": -30.0})
    assert response.status_code == 403
    assert error_code(response) == "permission_denied"
    mute = venue.post(venue.hirer, f"{MIXER}/channels/{channel_id}/mute", {"toggle": True})
    assert mute.status_code == 403
    assert recorder.mixer == []
    rows = [d for d in venue.denials() if d.get("id") == channel_id]
    assert len(rows) == 2
    assert {row["domain"] for row in rows} == {"mixer"}
    assert all(row["session_id"] and row["reason"] == "unreachable" for row in rows)


def test_mute_needs_reach_only_and_a_toggle_is_absolute(venue: Venue) -> None:
    first = venue.post(venue.hirer, f"{MIXER}/channels/{venue.ids['in1']}/mute", {"toggle": True})
    assert first.status_code == 200 and first.json()["muted"] is True
    second = venue.post(venue.hirer, f"{MIXER}/channels/{venue.ids['in1']}/mute", {"muted": False})
    assert second.status_code == 200 and second.json()["muted"] is False


def test_pan_is_refused_to_a_hirer_even_on_a_reachable_channel(venue: Venue) -> None:
    response = venue.post(venue.hirer, f"{MIXER}/channels/{venue.ids['in1']}/pan", {"pan": 0.5})
    assert response.status_code == 403
    assert error_code(response) == "permission_denied"


def test_get_mixer_state_is_filtered_exactly_as_the_frame_is(venue: Venue) -> None:
    hirer = venue.get(venue.hirer, f"{MIXER}/state").json()
    assert [c["channel_id"] for c in hirer["inputs"]] == [venue.ids["in1"], venue.ids["in2"]]
    assert hirer["outputs"] == []
    assert hirer["main"]["channel_id"] == venue.ids["main"]
    assert hirer["desk_scenes"] == []
    assert hirer["last_recalled_scene"] is None
    staff = venue.get(venue.operator, f"{MIXER}/state").json()
    assert {c["channel_id"] for c in staff["inputs"]} == {
        venue.ids["in1"],
        venue.ids["in2"],
        venue.ids["in3"],
    }
    assert [c["channel_id"] for c in staff["outputs"]] == [venue.ids["out1"]]


def test_main_is_absent_from_a_hirers_state_when_no_page_places_it(venue: Venue) -> None:
    async def drop_main(db: Database) -> None:
        page = await pages_crud.get_page(db, venue.ids["page"])
        assert page is not None
        await pages_crud.replace_page(
            db,
            page.page.id,
            page.page.updated_at,
            name=page.page.name,
            sort_order=page.page.sort_order,
            items=[PageItemInput(kind="channel", channel_id=venue.ids["in1"])],
        )

    venue.run(drop_main)
    venue.rebuild()
    hirer = venue.get(venue.hirer, f"{MIXER}/state").json()
    assert hirer["main"] is None
    assert [c["channel_id"] for c in hirer["inputs"]] == [venue.ids["in1"]]
    response = venue.post(venue.hirer, f"{MIXER}/channels/{venue.ids['main']}/level", {"db": -30.0})
    assert response.status_code == 403


# -- mixer level over the socket ---------------------------------------------------------------


def test_the_socket_clamps_to_the_ceiling_with_a_value_out_of_range_nack(
    venue: Venue, recorder: Recorder
) -> None:
    ids = venue.ids
    with connect(venue.client, venue.hirer) as phone:
        clamped = answer(phone, mixer_set(ids["in1"], 6.0, 1))
        assert clamped == {
            "type": "nack",
            "token": 1,
            "reason": "value_out_of_range",
            "value": -10.0,
        }
        main = answer(phone, mixer_set(ids["main"], 3.0, 2))
        assert main == {"type": "nack", "token": 2, "reason": "value_out_of_range", "value": -6.0}
        within = answer(phone, mixer_set(ids["in1"], -20.0, 3))
        assert within == {"type": "ack", "token": 3}
    assert recorder.mixer == [(("in1",), -10.0), (("main",), -6.0), (("in1",), -20.0)]


@pytest.mark.parametrize("target", ["in3", "out1", "missing"])
def test_an_unreachable_socket_write_is_refused_audited_and_never_reaches_the_driver(
    venue: Venue, recorder: Recorder, target: str
) -> None:
    channel_id = venue.ids.get(target, 99999)
    with connect(venue.client, venue.hirer) as phone:
        first = answer(phone, mixer_set(channel_id, -30.0, 1))
        # Nothing the hirer cannot see is disclosed: no value.
        assert first == {"type": "nack", "token": 1, "reason": "permission_denied"}
        # A drag keeps sending; each is refused, and audited once.
        second = answer(phone, mixer_set(channel_id, -29.0, 2))
        assert second["reason"] == "permission_denied"
    assert recorder.mixer == []
    rows = [d for d in venue.denials() if d.get("id") == channel_id]
    assert len(rows) == 1
    assert rows[0]["transport"] == "websocket"
    assert rows[0]["domain"] == "mixer"
    assert rows[0]["session_id"]


def test_staff_socket_writes_are_unchanged(venue: Venue, recorder: Recorder) -> None:
    with connect(venue.client, venue.operator) as booth:
        assert answer(booth, mixer_set(venue.ids["in3"], 6.0, 1)) == {"type": "ack", "token": 1}
    assert recorder.mixer == [(("in3",), 6.0)]


# -- lighting over REST ------------------------------------------------------------------------


def test_lighting_writes_follow_reach_and_the_switches(venue: Venue, recorder: Recorder) -> None:
    ids = venue.ids
    ok = venue.post(venue.hirer, f"{LIGHTING}/channels/{ids['L1']}/level", {"level": 50.0})
    assert ok.status_code == 200, ok.text
    group = venue.post(venue.hirer, f"{LIGHTING}/groups/{ids['G1']}/level", {"level": 80.0})
    assert group.status_code == 200, group.text
    colour = venue.post(
        venue.hirer, f"{LIGHTING}/channels/{ids['L1']}/colour", {"r": 255, "g": 0, "b": 0}
    )
    assert colour.status_code == 200, colour.text
    assert recorder.lighting == [
        ("set_level", ids["L1"]),
        ("set_group_multiplier", ids["G1"]),
        ("set_colour", ids["L1"]),
    ]

    recorder.lighting.clear()
    refused = [
        # A tray member while individual fixtures are off, and one on no page.
        (f"{LIGHTING}/channels/{ids['L2']}/level", {"level": 50.0}),
        (f"{LIGHTING}/channels/{ids['L4']}/level", {"level": 50.0}),
        (f"{LIGHTING}/channels/{ids['L2']}/colour", {"r": 1, "g": 2, "b": 3}),
        (f"{LIGHTING}/groups/{ids['G2']}/level", {"level": 50.0}),
        (f"{LIGHTING}/master", {"level": 50.0}),
        (f"{LIGHTING}/blackout", {}),
        (f"{LIGHTING}/levels", {"levels": {str(ids["L1"]): 10.0}}),
    ]
    for path, body in refused:
        response = venue.post(venue.hirer, path, body)
        assert response.status_code == 403, path
        assert error_code(response) == "permission_denied", path
    assert recorder.lighting == []


def test_the_individual_fixtures_and_colour_switches_hold(venue: Venue, recorder: Recorder) -> None:
    ids = venue.ids

    async def switch(db: Database) -> None:
        await _set_switches(db, lighting=True, individual=True, colour=False)

    venue.run(switch)
    venue.rebuild()
    member = venue.post(venue.hirer, f"{LIGHTING}/channels/{ids['L2']}/level", {"level": 40.0})
    assert member.status_code == 200, member.text
    colour = venue.post(
        venue.hirer, f"{LIGHTING}/channels/{ids['L1']}/colour", {"r": 9, "g": 9, "b": 9}
    )
    assert colour.status_code == 403
    assert recorder.lighting == [("set_level", ids["L2"])]
    reasons = {d.get("reason") for d in venue.denials() if d.get("id") == ids["L1"]}
    assert reasons == {"colour_disabled"}

    with connect(venue.client, venue.hirer) as phone:
        reply = answer(
            phone, {"type": "set", "domain": "lighting", "id": ids["L2"], "value": 30.0, "token": 1}
        )
        assert reply == {"type": "ack", "token": 1}


def test_lighting_off_for_hirers_refuses_every_lighting_write_and_empties_the_state(
    venue: Venue, recorder: Recorder
) -> None:
    async def switch(db: Database) -> None:
        await _set_switches(db, lighting=False, individual=True, colour=True)

    venue.run(switch)
    venue.rebuild()
    ids = venue.ids
    for path, body in (
        (f"{LIGHTING}/channels/{ids['L1']}/level", {"level": 50.0}),
        (f"{LIGHTING}/groups/{ids['G1']}/level", {"level": 50.0}),
    ):
        assert venue.post(venue.hirer, path, body).status_code == 403
    assert recorder.lighting == []
    state = venue.get(venue.hirer, f"{LIGHTING}/state").json()
    assert state == {
        "channels": {},
        "groups": {},
        "master": None,
        "external_control": None,
        "observed": None,
    }


def test_get_lighting_state_is_filtered_exactly_as_the_frame_is(venue: Venue) -> None:
    ids = venue.ids
    for channel in ("L1", "L2", "L4"):
        response = venue.post(
            venue.operator, f"{LIGHTING}/channels/{ids[channel]}/level", {"level": 20.0}
        )
        assert response.status_code == 200
    for group in ("G1", "G2"):
        response = venue.post(
            venue.operator, f"{LIGHTING}/groups/{ids[group]}/level", {"level": 90.0}
        )
        assert response.status_code == 200
    hirer = venue.get(venue.hirer, f"{LIGHTING}/state").json()
    assert set(hirer["channels"]) <= {str(ids["L1"]), str(ids["L2"]), str(ids["L3"])}
    assert {str(ids["L1"]), str(ids["L2"])} <= set(hirer["channels"])
    assert set(hirer["groups"]) == {str(ids["G1"])}
    assert "master" in hirer
    staff = venue.get(venue.operator, f"{LIGHTING}/state").json()
    assert str(ids["L4"]) in staff["channels"]
    assert set(staff["groups"]) >= {str(ids["G1"]), str(ids["G2"])}


# -- lighting over the socket --------------------------------------------------------------------


def test_socket_lighting_writes_hold_to_the_same_checks(venue: Venue, recorder: Recorder) -> None:
    ids = venue.ids
    operator_level = venue.post(
        venue.operator, f"{LIGHTING}/channels/{ids['L2']}/level", {"level": 35.0}
    )
    assert operator_level.status_code == 200
    recorder.lighting.clear()

    def lighting(domain: str, target: int | None, value: float, token: int) -> dict[str, Any]:
        return {"type": "set", "domain": domain, "id": target, "value": value, "token": token}

    with connect(venue.client, venue.hirer) as phone:
        assert answer(phone, lighting("lighting", ids["L1"], 60.0, 1)) == {
            "type": "ack",
            "token": 1,
        }
        assert answer(phone, lighting("lighting_group", ids["G1"], 70.0, 2)) == {
            "type": "ack",
            "token": 2,
        }
        # A tray member is visible read-only: refused, with the value to settle at.
        member = answer(phone, lighting("lighting", ids["L2"], 90.0, 3))
        assert member["reason"] == "permission_denied"
        assert member["value"] == 35.0
        unplaced = answer(phone, lighting("lighting", ids["L4"], 90.0, 4))
        assert unplaced == {"type": "nack", "token": 4, "reason": "permission_denied"}
        group = answer(phone, lighting("lighting_group", ids["G2"], 90.0, 5))
        assert group["reason"] == "permission_denied"
        master = answer(phone, lighting("master", None, 10.0, 6))
        assert master["reason"] == "permission_denied"
    assert recorder.lighting == [
        ("set_level", ids["L1"]),
        ("set_group_multiplier", ids["G1"]),
    ]
    domains = {(d.get("domain"), d.get("id")) for d in venue.denials() if d.get("transport")}
    assert domains == {
        ("lighting", ids["L2"]),
        ("lighting", ids["L4"]),
        ("lighting_group", ids["G2"]),
        ("master", None),
    }


# -- the pull-down (§6.7, Q8a) --------------------------------------------------------------


def test_a_lowered_ceiling_pulls_the_fader_down_with_no_hirer_connected(venue: Venue) -> None:
    ids = venue.ids
    raised = venue.post(venue.operator, f"{MIXER}/channels/{ids['in1']}/level", {"db": 0.0})
    assert raised.json()["db"] == 0.0
    untouched = venue.db_of("in2")
    current = venue.get(venue.admin, f"{MIXER}/channels/{ids['in1']}").json()
    response = venue.client.put(
        f"{MIXER}/channels/{ids['in1']}",
        json={"hirer_max_db": -15.0},
        headers={"Cookie": venue.admin, "If-Unmodified-Since-Version": current["updated_at"]},
    )
    assert response.status_code == 200, response.text
    wait_for(lambda: venue.db_of("in1") == -15.0)
    # A channel with no ceiling is left where it is.
    assert venue.db_of("in2") == untouched


def test_enabling_access_pulls_every_reachable_channel_down(venue: Venue) -> None:
    ids = venue.ids
    off = venue.post(venue.admin, f"{API_PREFIX}/hirer/enabled", {"enabled": False})
    assert off.status_code == 200
    for channel, level in (("in1", 0.0), ("main", 0.0), ("in3", 0.0), ("in2", 4.0)):
        response = venue.post(
            venue.operator, f"{MIXER}/channels/{ids[channel]}/level", {"db": level}
        )
        assert response.status_code == 200
    wait_for(lambda: venue.db_of("in3") == 0.0 and venue.db_of("in2") == 4.0)

    on = venue.post(venue.admin, f"{API_PREFIX}/hirer/enabled", {"enabled": True})
    assert on.status_code == 200
    wait_for(lambda: venue.db_of("in1") == -10.0 and venue.db_of("main") == -6.0)
    assert venue.db_of("in3") == 0.0  # on no page: a ceiling that holds no hirer
    assert venue.db_of("in2") == 4.0  # no ceiling
