"""The Phase 3 milestone at the API level (spec §18, ``docs/plans/phase-3.md``).

    "A 'Performance Start' scene executes end to end across every domain, and a
     source change made on the matrix's front panel appears in the interface
     within 30 seconds."

As Simon settled it (Q1): "every domain" is lighting (DMX), KNX, the projector
and the HDMI matrix. The scene also carries §8.13's mixer actions: Phase 4
registers real handlers for them, but this rig's mixer device is
created straight through the CRUD layer rather than through ``POST
/devices`` (see :func:`performance_start`), so the device manager never
builds a runtime for it and the two mixer actions report ``✗ failed`` —
"device N is not configured" — the same way a projector or HDMI action
fails when its own device cannot be resolved
(:meth:`proskenion.scene.engine.SceneEngine._dispatch`). By decision C
(§8.16) the run is still ``partial``: something failed and something else
succeeded.

Each test commissions its own room on a fresh database through the API
(:mod:`tests.integration.av_rig`), against the knxd, Art-Net, PJLink and LKV422
stubs at the far end of real sockets. The LKV422 is the real ``lkv422`` device
on the real serial transport; its port opens onto the stub through
:mod:`tests.stubs.serial_bridge`, the one test double, at the operating
system's boundary.

Delays, the projector's warm-up and the matrix's probe interval are compressed;
:func:`test_the_production_probe_interval_bounds_a_front_panel_change_at_25_s`
proves the production interval separately, so the 25-second interval (§11.1
gives 30; tightened so §18's 30-second bound holds with margin — see
``LKV422Driver.PROBE_INTERVAL``) is confirmed without a 25-second-long test.
The browser half is ``tests/e2e/av-milestone.spec.ts``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from proskenion.config import Config, KnxSection
from proskenion.core.dmx.compositor import level_to_dmx
from proskenion.core.drivers.base import Driver
from proskenion.core.drivers.lkv422 import LKV422Driver
from proskenion.core.drivers.pjlink import PJLinkDriver
from proskenion.core.transport.loopback import LoopbackTransport
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import mixer as mixer_crud
from tests.integration.av_rig import (
    BACK_OF_HOUSE_REF,
    MATRIX_PATH,
    PJLINK_PASSWORD,
    PROJECTOR,
    PROJECTOR_INPUTS,
    SHOW_INPUT,
    SIDE_OF_STAGE_REF,
    Appliance,
    AvRoom,
    Frames,
    configure_av,
    destination,
    hdmi_state,
    projector_reaches,
    projector_state,
    rebound,
)
from tests.integration.rig import (
    CONTROLLER,
    HOUSE_DIMMER,
    SCENES,
    Rig,
    build_rig,
    eventually,
    ok,
    until,
)
from tests.integration.test_first_run_flow import wait_for_status, walk_the_wizard
from tests.stubs.artnet_stub import ArtNetStub
from tests.stubs.echo_driver import RecordingSink
from tests.stubs.knxd_stub import KnxdStub
from tests.stubs.lkv422_stub import LKV422Stub
from tests.stubs.lkv422_tcp import LKV422TcpStub
from tests.stubs.pjlink_stub import PJLinkStub
from tests.stubs.serial_bridge import bridge_serial_ports

#: The matrix's probe — which is its routing poll (§7.5, §11.1) — compressed from 30 s.
MATRIX_PROBE_S = 0.25
#: The projector's probe cadence while warming or cooling, compressed from 5 s.
PROJECTOR_TRANSITION_S = 0.1
#: The stub projector's warm-up, compressed from a lamp's minute or two.
WARM_UP_S = 1.0

#: §8.13's worked example, compressed: the stage fade, the house lights once
#: it has finished, the projector's input once it has warmed.
FADE_MS = 500
UNMUTE_AT_MS = 300  # §8.13: after the recall has settled, in a later group
HOUSE_LIGHTS_AT_MS = 700
INPUT_AT_MS = 2000
#: How late a delay group may fire and still be "at" its delay. The engine
#: fires on a monotonic deadline; this guards against a hang, not jitter.
FIRING_SLACK_MS = 250.0

#: 80 % as a DMX slot value, by the compositor's own conversion (§9.2).
STAGE_DMX = level_to_dmx(80)

#: What a PJLink command that changes something looks like — anything else the
#: stub records is a query ("%1POWR ?", "%1INST ?", "%1INPT ?").
SETTING = ("%1POWR 0", "%1POWR 1", "%1INPT ")


def settings(pjlink: PJLinkStub, since: int = 0) -> list[str]:
    """Every command from ``since`` on that would change the projector."""
    return [
        r.command
        for r in pjlink.received[since:]
        if r.command.startswith(SETTING) and not r.command.endswith("?")
    ]


# -- fixtures -------------------------------------------------------------------------


@pytest.fixture
def knx_section(knxd: KnxdStub) -> KnxSection:
    return KnxSection(host="127.0.0.1", port=knxd.port, individual_address=CONTROLLER)


@pytest.fixture
def compressed_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The matrix's routing poll and the projector's transition cadence, shortened.

    Class attributes, patched before the drivers are built, as the drivers'
    own tests do. Only the two intervals the milestone waits on change; the
    projector's 30 s on and 5 min off cadences stay as they are (§7.4).
    """
    monkeypatch.setattr(LKV422Driver, "PROBE_INTERVAL", MATRIX_PROBE_S)
    monkeypatch.setattr(PJLinkDriver, "TRANSITION_INTERVAL", PROJECTOR_TRANSITION_S)


@pytest.fixture
async def pjlink() -> AsyncIterator[PJLinkStub]:
    """The projector: off, authentication on, a compressed warm-up (§7.4)."""
    async with PJLinkStub(
        password=PJLINK_PASSWORD,
        inputs=PROJECTOR_INPUTS,
        initial_power="0",
        warm_seconds=WARM_UP_S,
        cool_seconds=WARM_UP_S,
    ) as stub:
        yield stub


@pytest.fixture
async def matrix() -> AsyncIterator[LKV422TcpStub]:
    """The LKV422: powers on with both outputs on input 1 (side of stage)."""
    async with LKV422TcpStub() as stub:
        yield stub


@pytest.fixture
def bridged(matrix: LKV422TcpStub) -> Iterator[None]:
    """``/dev/hdmi-matrix`` opens onto the matrix stub."""
    with bridge_serial_ports({MATRIX_PATH: ("127.0.0.1", matrix.port)}):
        yield


@pytest.fixture
async def appliance(
    config: Config,
    db: Database,
    knxd: KnxdStub,
    artnet: ArtNetStub,
    pjlink: PJLinkStub,
    bridged: None,
    compressed_timing: None,
) -> AsyncIterator[Appliance]:
    """The application, booted over a database that has never been commissioned.

    Depends on every stub and patch, so pytest tears the application down first.
    """
    appliance = Appliance(config, db)
    await appliance.boot()
    try:
        yield appliance
    finally:
        await appliance.shutdown()


@dataclass
class Room:
    appliance: Appliance
    lighting: Rig
    av: AvRoom
    pjlink: PJLinkStub
    matrix: LKV422TcpStub

    @property
    def client(self) -> AsyncClient:
        return self.appliance.client

    @property
    def app(self) -> FastAPI:
        return self.appliance.app

    async def reboot(self) -> None:
        """Restart the application over the same database, and wait for its devices."""
        await self.appliance.reboot()
        self.lighting = rebound(self.lighting, self.appliance)
        await wait_for_status(self.client, self.av.projector, "connected")
        await wait_for_status(self.client, self.av.matrix, "connected")
        # §12.2's boot read, applied: the room's routing is in state.hdmi.
        hdmi = self.app.state.state_store.hdmi
        await until(
            lambda: hdmi.get_item("destinations", str(self.av.room)) is not None,
            "the video service's boot read of the matrix",
        )


@pytest.fixture
async def room(
    appliance: Appliance,
    knxd: KnxdStub,
    artnet: ArtNetStub,
    pjlink: PJLinkStub,
    matrix: LKV422TcpStub,
) -> Room:
    """The whole room, commissioned through the API — and then the appliance rebooted,
    as an installer does after commissioning."""
    lighting = await build_rig(appliance.client, appliance.app, knxd, artnet)
    av = await configure_av(appliance.client, pjlink)
    room = Room(appliance, lighting, av, pjlink, matrix)
    await room.reboot()
    await projector_reaches(room.client, "off")
    return room


@pytest.fixture
async def frames(room: Room) -> AsyncIterator[Frames]:
    """An operator's socket on the room's appliance, subscribed to hdmi and projector."""
    listening = Frames(room.app)
    await listening.caught_up()
    try:
        yield listening
    finally:
        await listening.close()


# -- helpers ------------------------------------------------------------------------------


async def add_action(client: AsyncClient, scene_id: int, body: dict[str, Any]) -> int:
    row = ok(await client.post(f"{SCENES}/{scene_id}/actions", json=body), 201)
    action_id: int = row["id"]
    return action_id


async def performance_start(room: Room) -> tuple[int, dict[str, int]]:
    """§8.13's worked example as "Performance Start", compressed, with an HDMI source.

    Returns the scene's id and its actions' ids by what each does.
    """
    client, rig, av = room.client, room.lighting, room.av
    # Phase 4 registers real handlers for the mixer domains, but this
    # rig never gives the mixer device to the device manager — created
    # directly through the CRUD layer, deliberately bypassing `POST
    # /devices`, because there is no real desk on this bench (Phase 4's Q2)
    # and the point of this milestone is the four domains Q1 names, not the
    # mixer's own. scene_actions.mixer_scene_id and mixer_channel_id are
    # real foreign keys from 005_mixer.sql onwards, so the actions below
    # still need rows to point at.
    mixer_device = await devices_crud.create(
        room.appliance.db, category="mixer", driver_key="cq20b", name="CQ-20B", config={}
    )
    desk_scene = await mixer_crud.create_desk_scene(
        room.appliance.db, device_id=mixer_device.id, scene_ref="1", name="Lecture Baseline"
    )
    monitors = await mixer_crud.create_channel(
        room.appliance.db, device_id=mixer_device.id, name="Monitors"
    )
    scene = ok(await client.post(SCENES, json={"name": "Performance Start"}), 201)
    sid: int = scene["id"]
    actions = {
        # t = 0: the stage fade, the projector on, the desk's baseline and its
        # monitors (Phase 4's domains), and the room's source for the show.
        "stage wash": await add_action(
            client,
            sid,
            {
                "domain": "dmx",
                "sort_order": 0,
                "delay_ms": 0,
                "dmx_snapshot": {str(f): {"level": 80} for f in rig.fixtures},
                "dmx_fade_ms": FADE_MS,
            },
        ),
        "projector on": await add_action(
            client,
            sid,
            {"domain": "projector_power", "sort_order": 1, "delay_ms": 0, "projector_power": "on"},
        ),
        "lecture baseline": await add_action(
            client,
            sid,
            {
                "domain": "mixer_recall",
                "sort_order": 2,
                "delay_ms": 0,
                "mixer_scene_id": desk_scene.id,
            },
        ),
        "unmute monitors": await add_action(
            client,
            sid,
            {
                "domain": "mixer_mute",
                "sort_order": 3,
                "delay_ms": UNMUTE_AT_MS,
                "mixer_channel_id": monitors.id,
                "mixer_muted": False,
            },
        ),
        "show source": await add_action(
            client,
            sid,
            {
                "domain": "hdmi_source",
                "sort_order": 4,
                "delay_ms": 0,
                "hdmi_destination": av.room,
                "hdmi_input_id": av.back_of_house,
            },
        ),
        # Once the stage fade has finished: the house lights off.
        "house lights off": await add_action(
            client,
            sid,
            {
                "domain": "knx",
                "sort_order": 0,
                "delay_ms": HOUSE_LIGHTS_AT_MS,
                "knx_address_id": rig.addresses[HOUSE_DIMMER],
                "knx_value": "0",
            },
        ),
        # Once the projector has warmed: its input.
        "projector input": await add_action(
            client,
            sid,
            {
                "domain": "projector_input",
                "sort_order": 0,
                "delay_ms": INPUT_AT_MS,
                "projector_input": SHOW_INPUT,
            },
        ),
    }
    return sid, actions


async def completed_run(client: AsyncClient, scene_id: int) -> dict[str, Any]:
    """The scene's newest execution-log entry, once the run has completed (§8.16)."""

    async def probe() -> dict[str, Any] | None:
        body = ok(await client.get(f"{SCENES}/{scene_id}/log"))
        entries: list[dict[str, Any]] = body["entries"]
        return entries[0] if entries and entries[0]["completed_at"] is not None else None

    return await eventually(probe, f"scene {scene_id} to complete", timeout_s=15.0)


def assert_fired_at(line: dict[str, Any], delay_ms: int) -> None:
    """The action fired at its delay (§8.13), give or take the engine's wake-up."""
    fired = line["fired_at_ms"]
    assert fired is not None, line
    assert delay_ms <= fired < delay_ms + FIRING_SLACK_MS, line


# -- the room -----------------------------------------------------------------------------


async def test_the_room_is_commissioned_through_the_api(room: Room) -> None:
    """The projector and the matrix, configured and connected, as the API reports them."""
    projector = await projector_state(room.client)
    assert projector["device_id"] == room.av.projector
    assert projector["state"] == "off"
    assert [i["ref"] for i in projector["inputs"]] == list(PROJECTOR_INPUTS)
    assert {"ref": SHOW_INPUT, "label": "Digital 1"} in projector["inputs"]

    state = await hdmi_state(room.client)
    assert state["device_id"] == room.av.matrix
    assert state["supports_atomic_route"] is True
    (the_room,) = state["destinations"]
    assert the_room["name"] == "The room"
    assert the_room["default_input_id"] == room.av.side_of_stage
    assert [o["id"] for o in the_room["outputs"]] == list(room.av.outputs)
    # §12.2: the matrix's routing was read at boot, never set.
    assert the_room["input_id"] == room.av.side_of_stage
    assert the_room["diverged"] is False
    assert set(room.matrix.commands()) == {"PAXXR"}


# -- clause 1: "Performance Start" end to end across every domain --------------------------


async def test_performance_start_runs_end_to_end_across_every_domain(
    room: Room, frames: Frames
) -> None:
    """Every action's outcome and fired time, what each device received, the log and the result."""
    client, rig, av, pjlink, matrix = room.client, room.lighting, room.av, room.pjlink, room.matrix
    scene_id, actions = await performance_start(room)

    art_mark, knx_mark = rig.frame_mark(), rig.write_mark()
    pj_mark, mx_mark = len(pjlink.received), len(matrix.commands())
    t0 = time.monotonic()
    started = ok(await client.post(f"{SCENES}/{scene_id}/trigger"), 202)
    assert started["scene_id"] == scene_id

    entry = await completed_run(client, scene_id)

    # -- the execution log (§8.16): one line per action, and the run's result ----
    assert entry["triggered_by"] == "api:admin"
    # Decision C: the mixer's two ✗ (its device was never given to the
    # device manager, see performance_start's own comment) sit alongside
    # five ✓, so the run is partial, not success and not failed.
    assert entry["result"] == "partial"
    lines = {line["action_id"]: line for line in entry["action_results"]}
    assert set(lines) == set(actions.values())

    wash = lines[actions["stage wash"]]
    assert (wash["domain"], wash["result"], wash["marker"]) == ("dmx", "sent", "✓")
    assert wash["detail"]["fade_ms"] == FADE_MS
    assert sorted(wash["detail"]["channels"]) == sorted(rig.fixtures)
    assert_fired_at(wash, 0)

    power = lines[actions["projector on"]]
    assert (power["domain"], power["result"], power["marker"]) == (
        "projector_power",
        "confirmed",
        "✓",
    )
    assert power["detail"] == {"state": "warming"}
    assert_fired_at(power, 0)

    for name, domain, delay in (
        ("lecture baseline", "mixer_recall", 0),
        ("unmute monitors", "mixer_mute", UNMUTE_AT_MS),
    ):
        mixer = lines[actions[name]]
        assert (mixer["domain"], mixer["result"], mixer["marker"]) == (domain, "failed", "✗")
        assert mixer["reason"] is not None and "is not configured" in mixer["reason"]
        assert_fired_at(mixer, delay)

    source = lines[actions["show source"]]
    assert (source["domain"], source["result"], source["marker"]) == (
        "hdmi_source",
        "confirmed",
        "✓",
    )
    assert source["detail"] == {"destination_id": av.room, "input_id": av.back_of_house}
    assert_fired_at(source, 0)

    house = lines[actions["house lights off"]]
    assert (house["domain"], house["result"], house["marker"]) == ("knx", "sent", "✓")
    assert house["detail"]["group_address"] == HOUSE_DIMMER
    assert_fired_at(house, HOUSE_LIGHTS_AT_MS)

    show_input = lines[actions["projector input"]]
    assert (show_input["domain"], show_input["result"], show_input["marker"]) == (
        "projector_input",
        "confirmed",
        "✓",
    )
    assert show_input["detail"] == {"input_ref": SHOW_INPUT}
    assert_fired_at(show_input, INPUT_AT_MS)

    # -- what each device received -------------------------------------------------
    # Lighting: a fade from dark to 80 % on all four fixtures.
    await rig.first_frame(
        lambda values: values == (STAGE_DMX,) * 4, since=art_mark, what="the stage wash"
    )
    steps = sorted({rig.fixture_values(f)[0] for f in rig.frames(art_mark)})
    assert any(0 < step < STAGE_DMX for step in steps), f"no intermediate frame: {steps}"

    # KNX: the house lights off, written at its delay.
    off = await rig.first_write(
        HOUSE_DIMMER,
        "5.001",
        lambda value: value == pytest.approx(0.0, abs=0.5),
        since=knx_mark,
        what="the house lights written 0 %",
    )
    assert off.received_at - t0 >= HOUSE_LIGHTS_AT_MS / 1000

    # The projector: power on at once, the input once warmed — two commands,
    # and nothing else that changes it.
    assert settings(pjlink, pj_mark) == ["%1POWR 1", f"%1INPT {SHOW_INPUT}"]
    received = pjlink.received[pj_mark:]
    on_at = next(r.received_at for r in received if r.command == "%1POWR 1")
    input_at = next(r.received_at for r in received if r.command == f"%1INPT {SHOW_INPUT}")
    assert on_at - t0 < FIRING_SLACK_MS / 1000
    assert input_at - on_at >= WARM_UP_S
    assert (pjlink.power, pjlink.current_input) == ("1", SHOW_INPUT)

    # The matrix: one switch for the destination's two outputs (§5.5, §7.5),
    # confirmed by PAXXR, and nothing else sent but routing queries.
    sent = matrix.commands()[mx_mark:]
    switch = f"PA{BACK_OF_HOUSE_REF}R"
    assert [c for c in sent if c != "PAXXR"] == [switch]
    assert "PAXXR" in sent[sent.index(switch) :]
    assert matrix.routing() == {"1": BACK_OF_HOUSE_REF, "2": BACK_OF_HOUSE_REF}

    # -- what the interface was told -----------------------------------------------
    await frames.first(
        "hdmi_source",
        "the room's new source",
        destination_id=av.room,
        input_id=av.back_of_house,
        diverged=False,
    )
    await frames.first("projector_state", "the projector warming", state="warming")
    await frames.first(
        "projector_state", "the projector on, on the show input", state="on", input_ref=SHOW_INPUT
    )
    projector = await projector_state(client)
    assert (projector["state"], projector["input_ref"]) == ("on", SHOW_INPUT)
    the_room = await destination(client, av.room)
    assert (the_room["input_id"], the_room["diverged"]) == (av.back_of_house, False)


# -- clause 2: a front-panel change appears in the interface within 30 s ------------------


async def test_a_front_panel_change_reaches_the_interface_through_the_routing_poll(
    room: Room, frames: Frames
) -> None:
    """Probe → routing listener → ``state.hdmi`` → ``hdmi_source`` → ``GET /hdmi/state``.

    With the probe compressed to :data:`MATRIX_PROBE_S`; the production
    interval is the next test's.
    """
    client, av, matrix = room.client, room.av, room.matrix
    store = room.app.state.state_store
    mark = len(matrix.commands())
    #: One probe interval, then one PAXXR exchange: the drain before it and the reply.
    worst = MATRIX_PROBE_S + LKV422Driver.DRAIN_TIMEOUT_S + LKV422Driver.READ_TIMEOUT

    # Someone at the matrix switches output 2 to back of house: the outputs disagree.
    changed = time.monotonic()
    await matrix.front_panel("2", BACK_OF_HOUSE_REF)

    # The driver's probe reads it, and the video service's listener writes state.hdmi …
    await until(
        lambda: store.hdmi.get("routing") == {"1": SIDE_OF_STAGE_REF, "2": BACK_OF_HOUSE_REF},
        "the routing poll to read the front-panel change",
    )
    assert store.hdmi.get_item("destinations", str(av.room)) == {
        "input_id": av.side_of_stage,
        "diverged": True,
    }
    # … the operator's socket is sent the frame, within one probe and one exchange …
    at, frame = await frames.first(
        "hdmi_source", "the divergence frame", since=changed, destination_id=av.room
    )
    assert frame == {
        "type": "hdmi_source",
        "destination_id": av.room,
        "input_id": av.side_of_stage,  # the first output is authoritative (§15.10)
        "diverged": True,
    }
    assert at - changed <= worst, f"{at - changed:.3f} s"
    # … and a page loaded now reads the same.
    the_room = await destination(client, av.room)
    assert (the_room["input_id"], the_room["diverged"]) == (av.side_of_stage, True)
    assert [o["input_id"] for o in the_room["outputs"]] == [av.side_of_stage, av.back_of_house]
    # Nothing was sent to the matrix but routing queries: the poll found it.
    assert set(matrix.commands()[mark:]) == {"PAXXR"}

    # The front panel then switches output 1 too: converged again, on the new source.
    changed = time.monotonic()
    await matrix.front_panel("1", BACK_OF_HOUSE_REF)
    at, frame = await frames.first(
        "hdmi_source", "the converged frame", since=changed, destination_id=av.room, diverged=False
    )
    assert frame["input_id"] == av.back_of_house
    assert at - changed <= worst, f"{at - changed:.3f} s"
    the_room = await destination(client, av.room)
    assert (the_room["input_id"], the_room["diverged"]) == (av.back_of_house, False)


async def test_the_production_probe_interval_bounds_a_front_panel_change_at_25_s() -> None:
    """The routing poll as shipped: every 25 s (§11.1 gives 30 s; tightened so
    §18's 30-second bound holds with margin — see ``LKV422Driver.PROBE_INTERVAL``'s
    own docstring), and each a ``PAXXR`` read that tells the listeners of a change.

    With the previous test, which proves the path from a probe to the
    interface, this bounds the time from a front-panel change to the
    interface at one probe interval plus one ``PAXXR`` exchange. The driver
    runs over the loopback transport with its sleep recorded, not slept, so
    the production interval is observed without waiting it out.
    """
    assert LKV422Driver.PROBE_INTERVAL == 25.0
    # The LKV422 inherits the base loop (§5.3): sleep PROBE_INTERVAL, then probe.
    assert "maintain" not in vars(LKV422Driver)
    assert "probe_periodically" not in vars(LKV422Driver)
    assert LKV422Driver.maintain is Driver.maintain

    transport = LoopbackTransport()
    await transport.open()
    async with LKV422Stub(transport) as stub:
        driver = LKV422Driver(1, transport, {}, RecordingSink())
        seen: list[tuple[dict[str, str], dict[str, str]]] = []

        async def listener(new: dict[str, str], previous: dict[str, str]) -> None:
            seen.append((new, previous))

        driver.add_routing_listener(listener)
        slept: list[float] = []

        class Enough(Exception):
            pass

        async def sleep(seconds: float) -> None:
            slept.append(seconds)
            if len(slept) == 2:
                # Between the first probe and the second, someone at the panel.
                await stub.front_panel("2", BACK_OF_HOUSE_REF)
            if len(slept) == 3:
                raise Enough

        driver._sleep = sleep
        with pytest.raises(Enough):
            await driver.maintain()

    assert slept == [LKV422Driver.PROBE_INTERVAL] * 3
    assert stub.received == [b"PAXXR", b"PAXXR"]
    assert seen == [
        ({"1": "1", "2": "1"}, {}),
        ({"1": "1", "2": BACK_OF_HOUSE_REF}, {"1": "1", "2": "1"}),
    ]


# -- B52 end to end ---------------------------------------------------------------------


async def test_a_command_during_warm_up_is_refused_with_the_reason_and_never_queued(
    room: Room, frames: Frames
) -> None:
    """§7.4, B52: ``device_unavailable`` with the state; nothing sent, then or later."""
    client, pjlink = room.client, room.pjlink
    turned_on = ok(await client.post(f"{PROJECTOR}/power", json={"on": True}))
    assert turned_on["state"] == "warming"
    await frames.first("projector_state", "the warming frame", state="warming")
    mark = len(pjlink.received)

    for path, body in (("power", {"on": False}), ("input", {"input": SHOW_INPUT})):
        refused = await client.post(f"{PROJECTOR}/{path}", json=body)
        assert refused.status_code == 503, refused.text
        error = refused.json()["error"]
        assert error["code"] == "device_unavailable"
        assert error["detail"] == {"state": "warming", "reason": "transitioning"}
    assert settings(pjlink, mark) == [], "a refused command reached the projector"

    # Warmed: still nothing — a queued command would be sent now.
    await projector_reaches(client, "on")
    await frames.first("projector_state", "the on frame", state="on")
    await asyncio.sleep(0.5)
    assert settings(pjlink, mark) == []
    assert (pjlink.power, pjlink.current_input) == ("1", PROJECTOR_INPUTS[0])


# -- Restore Venue Default (§13.5, §7.5) ----------------------------------------------------


async def test_restore_venue_default_restores_the_default_input_after_a_front_panel_change(
    room: Room, frames: Frames
) -> None:
    """An ``hdmi_source`` action with no input routes the destination's default (§13.5)."""
    client, av, matrix = room.client, room.av, room.matrix
    scene = ok(await client.post(SCENES, json={"name": "Restore Venue Default"}), 201)
    restore = await add_action(
        client,
        scene["id"],
        {"domain": "hdmi_source", "sort_order": 0, "delay_ms": 0, "hdmi_destination": av.room},
    )

    # Someone at the front panel: output 1 to back of house, output 2 to input 3.
    await matrix.front_panel("1", BACK_OF_HOUSE_REF)
    await matrix.front_panel("2", "3")
    await frames.first("hdmi_source", "the drift", destination_id=av.room, diverged=True)

    mark = len(matrix.commands())
    ok(await client.post(f"{SCENES}/{scene['id']}/trigger"), 202)
    entry = await completed_run(client, scene["id"])

    assert entry["result"] == "success"
    (line,) = entry["action_results"]
    assert line["action_id"] == restore
    assert (line["result"], line["marker"]) == ("confirmed", "✓")
    assert line["detail"] == {
        "destination_id": av.room,
        "default": True,
        "input_id": av.side_of_stage,
    }
    assert [c for c in matrix.commands()[mark:] if c != "PAXXR"] == [f"PA{SIDE_OF_STAGE_REF}R"]
    assert matrix.routing() == {"1": SIDE_OF_STAGE_REF, "2": SIDE_OF_STAGE_REF}
    await frames.first(
        "hdmi_source",
        "the default restored",
        destination_id=av.room,
        input_id=av.side_of_stage,
        diverged=False,
    )
    the_room = await destination(client, av.room)
    assert (the_room["input_id"], the_room["diverged"]) == (av.side_of_stage, False)


# -- discovery at boot (§7.4, §12.2) --------------------------------------------------------


async def test_at_boot_a_projector_already_on_is_discovered_and_sent_nothing(room: Room) -> None:
    """§7.4: query, never overwrite. §12.2: the matrix's routing is read, never set."""
    pjlink, matrix = room.pjlink, room.matrix
    # Mid-show when the controller restarts: the projector on, on another
    # input; the matrix routed to back of house at its panel.
    pjlink.set_power_immediately("1")
    pjlink.current_input = "32"
    await matrix.front_panel("1", BACK_OF_HOUSE_REF)
    await matrix.front_panel("2", BACK_OF_HOUSE_REF)
    pj_mark, mx_mark = len(pjlink.received), len(matrix.commands())

    await room.reboot()
    await projector_reaches(room.client, "on")
    projector = await projector_state(room.client)
    assert projector["input_ref"] == "32"

    # The shutdown and the boot between them sent the projector queries only.
    assert settings(pjlink, pj_mark) == []
    assert {r.command for r in pjlink.received[pj_mark:]} <= {"%1POWR ?", "%1INST ?", "%1INPT ?"}
    assert (pjlink.power, pjlink.current_input) == ("1", "32")
    # And the matrix routing queries only; the interface shows what it found.
    assert set(matrix.commands()[mx_mark:]) == {"PAXXR"}
    the_room = await destination(room.client, room.av.room)
    assert (the_room["input_id"], the_room["diverged"]) == (room.av.back_of_house, False)


# -- device rediscovery: a projector configured after boot (§7.4, §12.1) -------------------


async def test_a_projector_configured_after_boot_is_controllable_without_a_reboot(
    appliance: Appliance, pjlink: PJLinkStub
) -> None:
    """Defect 2, fixed: ``ProjectorService`` re-resolves its device on every
    ``connected`` transition at the projector's slot, not only at ``start()``,
    so a projector added after boot is usable without a restart."""
    await walk_the_wizard(appliance.client)
    av = await configure_av(appliance.client, pjlink, wait_for_matrix=False)
    client = appliance.client

    state = await projector_state(client)
    assert state["device_id"] == av.projector, state
    turned_on = await client.post(f"{PROJECTOR}/power", json={"on": True})
    assert turned_on.status_code == 200, turned_on.text
    assert settings(pjlink) == ["%1POWR 1"]
