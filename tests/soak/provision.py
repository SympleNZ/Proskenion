"""Commissioning the throwaway soak database, through the API, as an installer would.

The soak never runs against the venue's own database. The application is
started on a fresh one (``/data/soak/auditorium.db`` on the CM5), and this
module builds the room on it through the same endpoints the admin interface
uses — the first-run wizard, then devices, fixtures, a group, mixer channels,
two scenes and three rules — pointing every device at a stub
(:mod:`tests.soak.rig`). Nothing here writes a row directly.

The room:

* an ``artnet`` lighting output aimed at the node stub, with the booth input
  on universe 0, and four single-channel dimmers on universe 1 in one group;
* a ``cq20b`` mixer aimed at the MIDI stub, metering on, with inputs 1 and 2
  as tracked channels;
* a ``pjlink`` projector and the in-app ``stub`` HDMI matrix;
* KNX: ``1/0/1`` (a wall panel's button, incoming) bound to the group, and
  ``1/1/1`` (an indicator, outgoing) that the scenes write;
* scene A (fixtures to 80 %, input 1 to −10 dB, ``1/1/1`` on) and scene B
  (20 %, −20 dB, off), each fired by a ``schedule`` rule on alternate
  periods (:mod:`tests.soak.plan`).

Every object is looked up by name before it is created, so provisioning a
database that already has the room (a harness restarted with the same
password) reuses it rather than duplicating it.

The admin password is supplied at runtime and held in memory only; the
operator password the wizard insists on is random and discarded at once.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any

from tests.soak import rig
from tests.soak.api import API, AppClient, SoakError
from tests.soak.plan import SoakPlan

#: §15.2's seeded "Single-channel dimmer" profile.
SINGLE_CHANNEL_DIMMER = 1
OUTPUT_UNIVERSE = 1
INPUT_UNIVERSE = 0
FIXTURE_ADDRESSES = (1, 2, 3, 4)
BANK_COMMAND = "1/0/1"
INDICATOR = "1/1/1"
PANEL = "1.1.20"
CONTROLLER = "1.1.250"
PREFIX = "Soak"


@dataclass(frozen=True)
class Room:
    """What the harness needs to drive the load, by id."""

    lighting_output: int
    mixer: int
    projector: int
    matrix: int
    fixtures: tuple[int, ...]
    group: int
    mixer_input_1: int
    mixer_input_2: int
    scene_a: int
    scene_b: int
    rule_a: int
    rule_b: int
    binding: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# -- first run -----------------------------------------------------------------------


async def is_first_run(client: AppClient) -> bool:
    """``GET /auth/session`` without a session: 403 ``first_run_incomplete`` or 401."""
    response = await client.request("GET", f"{API}/auth/session", retry_auth=False)
    if response.status_code != 403:
        return False
    detail = response.json().get("error", {}).get("detail") or {}
    return bool(detail.get("reason") == "first_run_incomplete")


async def walk_the_wizard(client: AppClient, admin_password: str) -> None:
    """§10.4's seven steps. Step 2 signs in; the client adopts that session."""
    setup = f"{API}/setup"
    operator = secrets.token_urlsafe(18)

    async def step(number: int, body: dict[str, Any]) -> Any:
        response = await client.request("POST", f"{setup}/step/{number}", json=body)
        if response.status_code != 200:
            raise SoakError(f"setup step {number}: {response.status_code} {response.text[:300]}")
        client.adopt(response)
        return response.json()

    await step(1, {"locale": "en_NZ.UTF-8", "timezone": "Pacific/Auckland"})
    await step(2, {"password": admin_password, "password_confirm": admin_password})
    await step(3, {"skipped": True})
    await step(4, {"device_ids": [], "skipped": True})
    await step(5, {"password": operator, "password_confirm": operator})
    await step(6, {"option": "self_signed"})
    await step(7, {"reviewed": True})
    await client.json("POST", f"{setup}/complete")
    del operator


# -- lookups -------------------------------------------------------------------------


def _named(rows: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((row for row in rows if row.get("name") == name), None)


async def _one(
    client: AppClient,
    list_path: str,
    key: str | None,
    name: str,
    create: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    listed = await client.get(list_path)
    rows = listed[key] if key is not None else listed
    found = _named(rows, name)
    return found if found is not None else await create()


async def wait_for_device(client: AppClient, device_id: int, timeout_s: float = 60.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    last: Any = None
    while loop.time() < deadline:
        device = await client.get(f"/devices/{device_id}")
        last = device.get("status")
        if last is not None and last.get("status") == "connected":
            return
        await asyncio.sleep(0.5)
    raise SoakError(f"device {device_id} did not connect within {timeout_s:g} s: {last}")


# -- the room --------------------------------------------------------------------------


async def _device(
    client: AppClient, name: str, category: str, driver: str, config: dict[str, Any]
) -> int:
    async def create() -> dict[str, Any]:
        body = {"category": category, "driver_key": driver, "name": name, "config": config}
        created: dict[str, Any] = await client.post("/devices", body, expect=201)
        return created

    row = await _one(client, "/devices", "devices", name, create)
    return int(row["id"])


async def _address(client: AppClient, group_address: str, name: str, direction: str) -> int:
    rows = await client.get("/knx/addresses")
    found = next((r for r in rows if r["group_address"] == group_address), None)
    if found is None:
        found = await client.post(
            "/knx/addresses",
            {"group_address": group_address, "name": name, "dpt": "1.001", "direction": direction},
            expect=201,
        )
    return int(found["id"])


async def _scene(
    client: AppClient,
    name: str,
    fixtures: tuple[int, ...],
    level: float,
    mixer_channel: int,
    db: float,
    indicator: int,
    on: bool,
) -> int:
    async def create() -> dict[str, Any]:
        scene: dict[str, Any] = await client.post(
            "/scenes", {"name": name, "description": "Soak test load (§22.7)"}, expect=201
        )
        actions: list[dict[str, Any]] = [
            {
                "domain": "dmx",
                "sort_order": 0,
                "dmx_snapshot": {str(f): {"level": level} for f in fixtures},
                "dmx_fade_ms": 2000,
            },
            {
                "domain": "mixer_fader",
                "sort_order": 1,
                "mixer_channel_id": mixer_channel,
                "mixer_db": db,
            },
            {
                "domain": "knx",
                "sort_order": 2,
                "knx_address_id": indicator,
                "knx_value": "1" if on else "0",
            },
        ]
        for action in actions:
            await client.post(f"/scenes/{scene['id']}/actions", action, expect=201)
        return scene

    row = await _one(client, "/scenes", "scenes", name, create)
    return int(row["id"])


async def _rule(client: AppClient, name: str, body: dict[str, Any]) -> int:
    async def create() -> dict[str, Any]:
        created: dict[str, Any] = await client.post("/rules", {"name": name, **body}, expect=201)
        return created

    row = await _one(client, "/rules", "rules", name, create)
    if body.get("cron") and row.get("cron") != body["cron"]:
        # A room kept from an earlier run with another compression factor.
        row = await client.put(f"/rules/{row['id']}", {"cron": body["cron"]})
    return int(row["id"])


async def provision(client: AppClient, admin_password: str, plan: SoakPlan) -> Room:
    """Commission the soak room on a fresh database, or find it on one already done."""
    if await is_first_run(client):
        await walk_the_wizard(client, admin_password)
    else:
        await client.login()

    output = await _device(
        client,
        f"{PREFIX} Art-Net node",
        "lighting_output",
        "artnet",
        {
            "transport": {"type": "udp", "host": rig.NODE_HOST, "port": rig.NODE_PORT},
            "driver": {"input_universes": str(INPUT_UNIVERSE)},
        },
    )
    mixer = await _device(
        client,
        f"{PREFIX} CQ-20B",
        "mixer",
        "cq20b",
        {
            "transport": {"type": "tcp", "host": rig.HOST, "port": rig.CQ_MIDI_PORT},
            "driver": {"metering": True},
        },
    )
    projector = await _device(
        client,
        f"{PREFIX} projector",
        "projector",
        "pjlink",
        {
            "transport": {"type": "tcp", "host": rig.HOST, "port": rig.PJLINK_PORT},
            "driver": {"password": rig.PJLINK_STUB_PASSWORD},
        },
    )
    matrix = await _device(
        client,
        f"{PREFIX} HDMI matrix",
        "video_matrix",
        "stub",
        {"transport": {"type": "loopback"}, "driver": {"input_count": 4, "output_count": 2}},
    )
    for device_id in (output, mixer, projector, matrix):
        await wait_for_device(client, device_id)

    command = await _address(client, BANK_COMMAND, f"{PREFIX} panel button", "incoming")
    indicator = await _address(client, INDICATOR, f"{PREFIX} indicator", "outgoing")

    fixtures: list[int] = []
    channels = (await client.get("/lighting/channels"))["channels"]
    for number, address in enumerate(FIXTURE_ADDRESSES, start=1):
        name = f"{PREFIX} fixture {number}"
        found = _named(channels, name)
        if found is None:
            found = await client.post(
                "/lighting/channels",
                {
                    "name": name,
                    "type": "dmx",
                    "profile_id": SINGLE_CHANNEL_DIMMER,
                    "device_id": output,
                    "universe": OUTPUT_UNIVERSE,
                    "address": address,
                },
                expect=201,
            )
        fixtures.append(int(found["id"]))

    async def make_group() -> dict[str, Any]:
        created: dict[str, Any] = await client.post(
            "/lighting/groups", {"name": f"{PREFIX} stage", "channel_ids": fixtures}, expect=201
        )
        return created

    group = int(
        (await _one(client, "/lighting/groups", "groups", f"{PREFIX} stage", make_group))["id"]
    )

    inputs: list[int] = []
    for order, ref in enumerate(("ip1", "ip2")):
        name = f"{PREFIX} input {order + 1}"

        async def make_channel(ref: str = ref, name: str = name, order: int = order) -> Any:
            return await client.post(
                "/mixer/channels",
                {
                    "device_id": mixer,
                    "channel_kind": "input",
                    "name": name,
                    "driver_refs": [ref],
                    "show_pan": False,
                    "tracked": True,
                    "sort_order": order,
                },
                expect=201,
            )

        inputs.append(
            int((await _one(client, "/mixer/channels", "channels", name, make_channel))["id"])
        )

    fixture_ids = tuple(fixtures)
    scene_a = await _scene(
        client, f"{PREFIX} look A", fixture_ids, 80.0, inputs[0], -10.0, indicator, True
    )
    scene_b = await _scene(
        client, f"{PREFIX} look B", fixture_ids, 20.0, inputs[0], -20.0, indicator, False
    )
    rule_a = await _rule(
        client,
        f"{PREFIX} schedule A",
        {
            "trigger_type": "schedule",
            "cron": plan.cron_a,
            "action_type": "run_scene",
            "scene_id": scene_a,
        },
    )
    rule_b = await _rule(
        client,
        f"{PREFIX} schedule B",
        {
            "trigger_type": "schedule",
            "cron": plan.cron_b,
            "action_type": "run_scene",
            "scene_id": scene_b,
        },
    )
    binding = await _rule(
        client,
        f"{PREFIX} panel binding",
        {
            "trigger_type": "knx",
            "knx_address_id": command,
            "match_type": "any",
            "action_type": "lighting_group",
            "lighting_group_id": group,
            "on_level": 60.0,
            "off_level": 0.0,
        },
    )
    return Room(
        lighting_output=output,
        mixer=mixer,
        projector=projector,
        matrix=matrix,
        fixtures=fixture_ids,
        group=group,
        mixer_input_1=inputs[0],
        mixer_input_2=inputs[1],
        scene_a=scene_a,
        scene_b=scene_b,
        rule_a=rule_a,
        rule_b=rule_b,
        binding=binding,
    )
