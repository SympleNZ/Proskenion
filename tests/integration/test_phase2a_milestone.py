"""The Phase 2 slice A milestone at the API level (``docs/plans/phase-2a.md``).

    "Against the knxd and Art-Net stubs, on a fresh database: An admin imports a
     KNX address list, patches four DMX fixtures and a KNX dimmer, groups them
     into a bank, and configures its binding and derived status. A telegram on
     the bank's command address brings it up at exactly its on-level with its
     group fader left at 40%; the frame reaches the Art-Net stub; the status
     address is written 1 after the frame is sent. Dragging one fixture down in
     the web interface writes the status 0. Setting external control manually
     suspends DMX output while the KNX dimmer keeps working, writes the bank's
     status 0 and the external-control status 1; clearing it resumes from the
     controller's own levels. A scene with a DMX fade and a KNX write runs end to
     end and logs per-action results."

One test per clause, each on its own fresh database and its own room
(:func:`tests.integration.rig.build_rig`), so a clause that breaks is named by
its test rather than hidden behind the first failure of one long journey. The
application is the real one — the lifespan, the KNX subsystem, the lighting
service, the scene engine and the rules engine — and the only stubs are the
knxd and the Art-Net node at the far end of their sockets.

Three behaviours await the project owner's decision and are deliberately not
asserted either way: how a fixture with both a dimmer and colour channels is
scaled (the rig uses single-channel dimmers), wall-panel dimming with the
master below full (the master stays at 100 %), and how a scene run's overall
result is worked out (per-action outcomes are asserted, the run's result is
not).

The two defects the milestone found on the way are tested at the end of this
module; both are fixed (docs/phase-2a-milestone.md).

The browser half — a fixture dragged down on the Lighting view, and the
manual external-control toggle — is ``tests/e2e/lighting-milestone.spec.ts``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from proskenion.config import KnxSection
from proskenion.core.dmx.compositor import level_to_dmx
from proskenion.core.dmx.renderer import DEFAULT_KEEPALIVE_S
from proskenion.core.state import Change
from tests.integration.rig import (
    BANK_COMMAND,
    BANK_STATUS,
    CONTROLLER,
    DERIVED,
    DEVICES,
    EXTERNAL_CONTROL_STATUS,
    FOYER_SIGN,
    HOUSE_DIMMER,
    KNX,
    LIGHTING,
    ON_DMX,
    ON_LEVEL,
    RULES,
    SCENES,
    Rig,
    build_rig,
    ok,
    until,
)
from tests.integration.test_first_run_flow import wait_for_status, walk_the_wizard
from tests.stubs.artnet_stub import ArtNetStub
from tests.stubs.knxd_stub import KnxdStub

ALL_ON = (ON_DMX,) * 4


@pytest.fixture
def knx_section(knxd: KnxdStub) -> KnxSection:
    """The application's KNX subsystem, aimed at the stub knxd, knowing its own address."""
    return KnxSection(host="127.0.0.1", port=knxd.port, individual_address=CONTROLLER)


@pytest.fixture
async def rig(client: AsyncClient, app: FastAPI, knxd: KnxdStub, artnet: ArtNetStub) -> Rig:
    return await build_rig(client, app, knxd, artnet)


def binding_row(states: dict[str, Any], rule_id: int) -> dict[str, Any]:
    row: dict[str, Any] = next(r for r in states["rules"] if r["id"] == rule_id)
    return row


# -- an admin imports, patches, groups and configures -------------------------------------


async def test_the_room_is_configured_through_the_api(rig: Rig) -> None:
    """The import, the patch, the bank, its binding and its statuses, as the API reports them."""
    client = rig.client
    library = ok(await client.get(f"{KNX}/addresses"))
    assert {row["group_address"]: row["dpt"] for row in library} == {
        "1/0/1": "1.001",
        "1/0/2": "1.001",
        "1/0/9": "1.001",
        "1/3/1": "1.001",
        "2/1/1": "5.001",
    }
    channels = ok(await client.get(f"{LIGHTING}/channels"))["channels"]
    assert sorted(c["type"] for c in channels) == ["dmx"] * 4 + ["knx_dimmer"]
    assert all(c["group_ids"] == [rig.bank] for c in channels)

    binding = binding_row(ok(await client.get(f"{RULES}/state")), rig.binding)
    assert binding["state"] is False  # the bank is off
    assert binding["suppressed"] is False
    assert binding["fires_automatically"] is True

    readings = {r["id"]: r for r in ok(await client.get(f"{DERIVED}/state"))["statuses"]}
    assert readings[rig.bank_status]["group_address"] == BANK_STATUS
    assert readings[rig.bank_status]["written"] is False
    assert readings[rig.external_status]["group_address"] == EXTERNAL_CONTROL_STATUS
    assert readings[rig.external_status]["written"] is False


# -- a telegram brings the bank up ------------------------------------------------------


async def test_a_panel_telegram_brings_the_bank_to_exactly_its_on_level(rig: Rig) -> None:
    """Q1: the recall forces the group multiplier to 1.0 by a write; the master applies."""
    # The bank's group fader is left at 40 % by an operator.
    group = ok(await rig.client.post(f"{LIGHTING}/groups/{rig.bank}/level", json={"level": 40}))
    assert group["level"] == pytest.approx(40.0)
    assert (await rig.lighting_state())["groups"][str(rig.bank)] == pytest.approx(0.4)

    frames, writes = rig.frame_mark(), rig.write_mark()
    await rig.press(True)

    # The frame reaches the Art-Net stub with every fixture at 80 % — 204, not
    # the 82 a group left at 40 % would give — and no frame between dark and it.
    on = await rig.first_frame(
        lambda values: values == ALL_ON, since=frames, what="the bank at its on level"
    )
    arrived = rig.frames(frames)
    assert all(rig.fixture_values(f) == (0, 0, 0, 0) for f in arrived[: arrived.index(on)])

    # The KNX dimmer in the bank is sent the same 80 %, once, on its command address.
    await rig.first_write(
        HOUSE_DIMMER,
        "5.001",
        lambda value: value == pytest.approx(ON_LEVEL, abs=0.5),
        since=writes,
        what="the house dimmer written 80 %",
    )
    assert len(rig.writes(HOUSE_DIMMER, writes)) == 1

    # The store agrees: levels at 80, the group multiplier forced to 1.0, the
    # master untouched at 100.
    state = await rig.lighting_state()
    assert state["groups"][str(rig.bank)] == pytest.approx(1.0)
    assert state["master"] == pytest.approx(100.0)
    for channel in (*rig.fixtures, rig.dimmer):
        assert state["channels"][str(channel)]["level"] == pytest.approx(ON_LEVEL)
    fired = await rig.rule_logged(rig.binding, "success")
    assert fired["triggered_by"] == f"knx:{BANK_COMMAND}"
    assert fired["detail"]["level"] == pytest.approx(ON_LEVEL)


async def test_the_status_is_written_1_after_the_frame_is_sent(rig: Rig) -> None:
    """§7.1: completion means the frame has been sent — the panel lights after the room."""
    frames, writes = rig.frame_mark(), rig.write_mark()
    await rig.press(True)
    on = await rig.first_frame(
        lambda values: values == ALL_ON, since=frames, what="the bank at its on level"
    )
    lit = await rig.status_written(BANK_STATUS, True, since=writes)
    assert on.at < lit.received_at, "the indicator was written before the frame arrived"
    # One status telegram for the change (§8.6: written only on a change of value).
    assert [w.value("1.001") for w in rig.writes(BANK_STATUS, writes)] == [True]
    binding = binding_row(ok(await rig.client.get(f"{RULES}/state")), rig.binding)
    assert binding["state"] is True


# -- dragging a fixture down ------------------------------------------------------------


async def test_dragging_one_fixture_down_writes_the_status_0(rig: Rig) -> None:
    """B51: the status is derived from state, so any path that lowers a level clears it.

    A drag in the browser is a WebSocket ``set``, which the browser journey
    drives. Here the same change arrives through ``POST
    /lighting/channels/{id}/level``, which funnels into the same fade engine.
    """
    await rig.bank_on()
    frames, writes = rig.frame_mark(), rig.write_mark()
    down = ok(
        await rig.client.post(f"{LIGHTING}/channels/{rig.fixtures[0]}/level", json={"level": 30})
    )
    assert down["level"] == pytest.approx(30.0)
    dropped = await rig.first_frame(
        lambda values: values == (level_to_dmx(30), ON_DMX, ON_DMX, ON_DMX),
        since=frames,
        what="the first fixture at 30 %",
    )
    dark = await rig.status_written(BANK_STATUS, False, since=writes)
    assert dropped.at < dark.received_at, "the indicator went out before the frame arrived"
    binding = binding_row(ok(await rig.client.get(f"{RULES}/state")), rig.binding)
    assert binding["state"] is False


# -- external control -------------------------------------------------------------------


async def test_manual_external_control_suspends_dmx_but_not_the_knx_dimmer(rig: Rig) -> None:
    """§7.2.7 *Behaviour while active*, entered through the manual flag."""
    await rig.bank_on()
    writes = rig.write_mark()

    toggled = await rig.set_external_control(True)
    assert toggled["state"] == "manual" and toggled["active"] is True

    # The bank's indicator goes out and the external-control indicator lights.
    await rig.status_written(BANK_STATUS, False, since=writes)
    await rig.status_written(EXTERNAL_CONTROL_STATUS, True, since=writes)
    # Every frame from here on would be output the controller should not send.
    frames = rig.frame_mark()

    # The panel's stage button is suppressed: its telegram is logged, not acted on.
    await rig.press(False)
    await rig.rule_logged(rig.binding, "suppressed")
    binding = binding_row(ok(await rig.client.get(f"{RULES}/state")), rig.binding)
    assert binding["suppressed"] is True

    # The KNX house dimmer keeps working: composite_knx() is never gated.
    ok(await rig.client.post(f"{LIGHTING}/channels/{rig.dimmer}/level", json={"level": 50}))
    await rig.first_write(
        HOUSE_DIMMER,
        "5.001",
        lambda value: value == pytest.approx(50.0, abs=0.5),
        since=writes,
        what="the house dimmer written 50 % under external control",
    )

    # DMX output is suspended: not a frame, not even a keepalive. Absence can
    # only be observed over a window; this one is half as long again as the
    # longest gap the renderer leaves between frames while it is sending.
    await asyncio.sleep(DEFAULT_KEEPALIVE_S * 1.5)
    assert rig.frames(frames) == []
    # The suppressed press changed nothing in the model either.
    state = await rig.lighting_state()
    for channel in rig.fixtures:
        assert state["channels"][str(channel)]["level"] == pytest.approx(ON_LEVEL)


async def test_clearing_external_control_resumes_from_the_controllers_own_levels(
    rig: Rig,
) -> None:
    """§7.2.7 *Resuming*: output resumes from the controller's model, not its last frame.

    One fixture is dragged down to 20 % and sent back up to 80 % over two
    seconds, and external control is set while that fade is under way. The
    fade runs on in the level store with nothing sent, so the last frame the
    node received shows the fixture part-way, while the model ends at 80 %.
    """
    await rig.bank_on()
    ok(await rig.client.post(f"{LIGHTING}/channels/{rig.fixtures[1]}/level", json={"level": 20}))
    await rig.first_frame(
        lambda values: values[1] == level_to_dmx(20), since=0, what="the fixture at 20 %"
    )
    writes = rig.write_mark()
    ok(
        await rig.client.post(
            f"{LIGHTING}/channels/{rig.fixtures[1]}/level", json={"level": 80, "fade_ms": 2000}
        )
    )
    await rig.set_external_control(True)
    await rig.status_written(EXTERNAL_CONTROL_STATUS, True, since=writes)
    suspended = rig.frame_mark()
    last_sent = rig.fixture_values(rig.frames()[-1])
    assert last_sent[1] < ON_DMX, "the fade had finished before external control was set"

    # The fade finishes in the level store, more than a keepalive interval
    # later, and nothing is sent meanwhile.
    store = rig.app.state.state_store
    await until(
        lambda: store.lighting.get_item("levels", rig.fixtures[1]) == ON_LEVEL,
        "the fade to finish in the level store",
    )
    assert rig.frames(suspended) == []
    # Every member is now at 80 % in the store, but the room is not the
    # controller's to report: the bank's indicator is not lit while a desk has it.
    assert all(w.value("1.001") is False for w in rig.writes(BANK_STATUS, writes))

    frames, writes = rig.frame_mark(), rig.write_mark()
    cleared = await rig.set_external_control(False)
    assert cleared["state"] == "off" and cleared["active"] is False

    resumed = await rig.first_frame(lambda values: True, since=frames, what="output to resume")
    assert rig.fixture_values(resumed) == ALL_ON
    assert rig.fixture_values(resumed) != last_sent
    # Bindings respond again and their statuses are rewritten, after the frame;
    # the external-control indicator is cleared.
    lit = await rig.status_written(BANK_STATUS, True, since=writes)
    assert resumed.at < lit.received_at
    await rig.status_written(EXTERNAL_CONTROL_STATUS, False, since=writes)
    binding = binding_row(ok(await rig.client.get(f"{RULES}/state")), rig.binding)
    assert binding["suppressed"] is False and binding["state"] is True


# -- a scene ----------------------------------------------------------------------------


async def test_a_scene_with_a_dmx_fade_and_a_knx_write_runs_and_logs_each_action(
    rig: Rig,
) -> None:
    """§8.11–§8.16: triggered, executed, completed and logged action by action.

    The run's overall ``result`` is not asserted: how it is worked out is a
    decision still open. Each action's own outcome is.
    """
    client = rig.client
    scene = ok(await client.post(SCENES, json={"name": "Show start"}), 201)
    fade = ok(
        await client.post(
            f"{SCENES}/{scene['id']}/actions",
            json={
                "domain": "dmx",
                "sort_order": 0,
                "delay_ms": 0,
                "dmx_snapshot": {str(f): {"level": 60} for f in rig.fixtures},
                "dmx_fade_ms": 1000,
            },
        ),
        201,
    )
    sign = ok(
        await client.post(
            f"{SCENES}/{scene['id']}/actions",
            json={
                "domain": "knx",
                "sort_order": 1,
                "delay_ms": 0,
                "knx_address_id": rig.addresses[FOYER_SIGN],
                "knx_value": "1",
            },
        ),
        201,
    )

    frames, writes = rig.frame_mark(), rig.write_mark()
    started = ok(await client.post(f"{SCENES}/{scene['id']}/trigger"), 202)
    assert started["scene_id"] == scene["id"]

    # The KNX write reaches the bus …
    await rig.status_written(FOYER_SIGN, True, since=writes)
    # … and the DMX action reaches the node as a fade: frames between dark
    # and the target, then the target.
    target = level_to_dmx(60)
    await rig.first_frame(lambda values: values == (target,) * 4, since=frames, what="the look")
    steps = sorted({rig.fixture_values(f)[0] for f in rig.frames(frames)})
    assert any(0 < step < target for step in steps), f"no intermediate frame: {steps}"

    # The execution log carries one line per action (§8.16).
    entry = await rig.completed_run(scene["id"])
    assert entry["triggered_by"] == "api:admin"
    results = {a["action_id"]: a for a in entry["action_results"]}
    assert set(results) == {fade["id"], sign["id"]}

    dmx = results[fade["id"]]
    assert (dmx["domain"], dmx["result"], dmx["marker"]) == ("dmx", "sent", "✓")
    assert dmx["detail"]["fade_ms"] == 1000
    assert sorted(dmx["detail"]["channels"]) == sorted(rig.fixtures)

    knx = results[sign["id"]]
    assert (knx["domain"], knx["result"], knx["marker"]) == ("knx", "sent", "✓")
    assert knx["detail"]["group_address"] == FOYER_SIGN


# -- defects found by the milestone, fixed -------------------------------------------------


async def test_a_knx_dimmer_write_leaves_the_knxd_connection_up(rig: Rig) -> None:
    """The KNX subsystem stays connected through a recall that writes the house dimmer.

    The same recall as the clauses above; the bank includes the dimmer, so the
    KNX pass writes it. What must not happen is anything else on the KNX
    side: the ``knx`` status stays ``connected``, the dimmer write appears in
    the telegram monitor as sent (§21.19), and the panel's indicator follows
    the frame without waiting for a reconnection.
    """
    store = rig.app.state.state_store
    transitions: list[str] = []

    def watch(change: Change) -> None:
        if change.domain == "devices" and change.item == "knx":
            record = store.devices.record("knx")
            transitions.append("gone" if record is None else record.status)

    store.add_listener(watch)
    try:
        writes = rig.write_mark()
        await rig.press(True)
        await rig.first_write(
            HOUSE_DIMMER, "5.001", lambda _: True, since=writes, what="the dimmer write"
        )
        await rig.status_written(BANK_STATUS, True, since=writes)
    finally:
        store.remove_listener(watch)
    assert transitions == [], f"the knx status changed: {transitions}"
    sent = [m for m in rig.app.state.knx.monitor.recent() if m.direction == "outgoing"]
    assert HOUSE_DIMMER in [m.group_address for m in sent]


async def test_configuring_a_device_keeps_the_knx_status(
    client: AsyncClient, app: FastAPI, artnet: ArtNetStub
) -> None:
    """KNX reports into the ``devices`` domain under its own key (§5.5, B42); adding a
    device must leave that record alone."""
    await walk_the_wizard(client)
    store = app.state.state_store
    knx = store.devices.record("knx")
    assert knx is not None and knx.status == "connected"

    output = ok(
        await client.post(
            DEVICES,
            json={
                "category": "lighting_output",
                "driver_key": "artnet",
                "name": "eDMX8 MAX",
                "config": {
                    "transport": {"type": "udp", "host": "127.0.0.1", "port": artnet.port},
                    "driver": {},
                },
            },
        ),
        201,
    )
    await wait_for_status(client, output["id"], "connected")

    after = store.devices.record("knx")
    assert after is not None and after.status == "connected", f"knx record: {after}"
    await app.state.health.poll()
    health = ok(await client.get("/api/v1/system/health"))
    assert "knx" in [row["key"] for row in health["devices"]]
