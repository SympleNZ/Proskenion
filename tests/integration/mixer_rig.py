"""A CQ-20B commissioned through the API, against the MIDI and native stubs.

The Phase 4 milestone (spec §18, ``docs/plans/phase-4.md``):

    "The mixer view stays in step with the CQ-20B, and scenes recall a
     baseline then adjust channels and outputs."

Every clause of it starts from the same desk, configured the way an installer
configures one — through the API, never by writing rows:

1. a ``cq20b`` mixer device through ``POST /devices``, its TCP transport aimed
   at :class:`~tests.stubs.cq_midi_stub.CqMidiStub`, which also creates a
   channel for every desk channel (§7.3), of which this venue keeps only
   Main; metering on, the native client aimed at
   :class:`~tests.stubs.cq_native_stub.CqNativeStub` on the same host, with
   its meters coming back to a local UDP port of the test's choosing
2. the channels a small venue exposes, through ``POST /mixer/channels``:
   two inputs with pan on one, the HDMI audio on the stereo ST2 input, a mono
   foldback output on Out 3, the stage monitors on the linked Out 1/2 pair (§7.3
   *Linked stereo outputs*, and §8.13's "Unmute Out 1/2, stage monitors"),
   and one **untracked** input (§21.21)
3. three desk scenes through ``POST /mixer/desk-scenes``, the first of them
   the Venue Default (§13.5)

The desk is not empty when the application first meets it: every address the
room uses holds a value that is neither the stub's default nor any preset's,
so a view that shows the desk's values has read them, and a view still
showing an old value has not.

What a clause asserts about the desk is what the stub recorded arriving —
:attr:`CqMidiStub.messages` and :attr:`CqMidiStub.state` — never what the
application says it sent. Addresses and values are this module's own reading
of ``docs/protocols/cq20b.md`` (§3.1, §3.2, §5, §6 and the p.15 law in §4),
with the stub's own law for dB, so nothing here shares the driver's tables.
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient

from proskenion.api.app import API_PREFIX
from proskenion.core.drivers.cq20b import CQ20BDriver
from tests.integration.rig import DEVICES, ok, until
from tests.integration.test_first_run_flow import wait_for_status
from tests.stubs.cq_midi_stub import (
    MUTE_ADDRESSES,
    CqMidiStub,
    StubMessage,
    _stub_db_to_value,
)

MIXER = f"{API_PREFIX}/mixer"

Address = tuple[int, int]

# -- the desk's addresses, from cq20b.md -------------------------------------------

#: Level to Main LR (cq20b.md §3.1) and master level (§3.2).
LEVEL: dict[str, Address] = {
    "main": (0x4F, 0x00),
    "out1": (0x4F, 0x01),
    "out2": (0x4F, 0x02),
    "out3": (0x4F, 0x03),
    "ip1": (0x40, 0x00),
    "ip2": (0x40, 0x01),
    "ip5": (0x40, 0x04),
    "st2": (0x40, 0x1A),
}
#: Mutes (cq20b.md §5). Out 1/2 linked uses Out 1's address; Out 2 has its own.
MUTE: dict[str, Address] = {
    "main": (0x00, 0x44),
    "out1": (0x00, 0x45),
    "out2": (0x00, 0x46),
    "out3": (0x00, 0x47),
    "ip1": (0x00, 0x00),
    "ip2": (0x00, 0x01),
    "ip5": (0x00, 0x04),
    "st2": (0x00, 0x1A),
}
#: Pan to Main LR (cq20b.md §6). Inputs only.
PAN: dict[str, Address] = {"ip1": (0x50, 0x00), "ip2": (0x50, 0x01), "st2": (0x50, 0x1A)}
#: cq20b.md §6's published points: centre and R50%.
PAN_CENTRE = 8192
PAN_R50 = 12287
#: The linked pair's odd output carries it (cq20b.md §3.2, §5).
MONITORS_LEVEL = LEVEL["out1"]
MONITORS_MUTE = MUTE["out1"]

MUTED, UNMUTED = 1, 0


def level(db: float | None) -> int:
    """A dB as the desk holds it, by the stub's own copy of the p.15 law."""
    return _stub_db_to_value(db)


# -- the venue ---------------------------------------------------------------------

#: What the desk holds before the application ever connects: a show mid-way,
#: nothing at a default, nothing equal to any preset below.
DESK_AT_FIRST_CONNECTION: dict[Address, int] = {
    LEVEL["main"]: level(-1.0),
    MUTE["main"]: UNMUTED,
    LEVEL["out1"]: level(-7.0),
    MUTE["out1"]: UNMUTED,
    LEVEL["out2"]: level(-11.0),
    MUTE["out2"]: MUTED,
    LEVEL["out3"]: level(-15.0),
    MUTE["out3"]: UNMUTED,
    LEVEL["ip1"]: level(-9.0),
    MUTE["ip1"]: UNMUTED,
    PAN["ip1"]: 10648,  # R30% (cq20b.md §6)
    LEVEL["ip2"]: level(-13.0),
    MUTE["ip2"]: MUTED,
    LEVEL["st2"]: level(-17.0),
    MUTE["st2"]: UNMUTED,
    LEVEL["ip5"]: level(-23.0),
    MUTE["ip5"]: MUTED,
}

#: The desk's scenes (cq20b.md §7: 1-128), by number: what each loads.
VENUE_DEFAULT_REF = "1"
LECTURE_REF = "3"
PERFORMANCE_REF = "5"
PRESETS: dict[int, dict[Address, int]] = {
    int(VENUE_DEFAULT_REF): {
        LEVEL["main"]: level(0.0),
        MUTE["main"]: UNMUTED,
        LEVEL["ip1"]: level(-10.0),
        MUTE["ip1"]: UNMUTED,
        LEVEL["ip2"]: level(-10.0),
        MUTE["ip2"]: UNMUTED,
        MONITORS_LEVEL: level(-10.0),
        MONITORS_MUTE: MUTED,
    },
    # "Lecture Baseline": microphones up, the stage monitors muted — the
    # scene a presentation starts from, which the room then adjusts.
    int(LECTURE_REF): {
        LEVEL["main"]: level(-2.0),
        MUTE["main"]: UNMUTED,
        LEVEL["ip1"]: level(-5.0),
        MUTE["ip1"]: UNMUTED,
        LEVEL["ip2"]: level(-6.0),
        MUTE["ip2"]: UNMUTED,
        MONITORS_LEVEL: level(-4.0),
        MONITORS_MUTE: MUTED,
        LEVEL["st2"]: level(-8.0),
    },
    int(PERFORMANCE_REF): {
        LEVEL["main"]: level(-3.0),
        MUTE["main"]: UNMUTED,
        LEVEL["ip1"]: level(0.0),
        MUTE["ip1"]: UNMUTED,
        MONITORS_LEVEL: level(-2.0),
        MONITORS_MUTE: UNMUTED,
    },
}


@dataclass(frozen=True)
class ChannelSpec:
    key: str
    kind: str
    name: str
    refs: tuple[str, ...]
    show_pan: bool = False
    tracked: bool = True


#: The venue's virtual surface (§5.5): configuration, in sort order.
CHANNELS: tuple[ChannelSpec, ...] = (
    ChannelSpec("wireless", "input", "Wireless 1", ("ip1",), show_pan=True),
    ChannelSpec("lectern", "input", "Lectern", ("ip2",)),
    ChannelSpec("hdmi", "input", "HDMI audio", ("st2",)),
    ChannelSpec("spare", "input", "Spare mic", ("ip5",), tracked=False),
    ChannelSpec("foldback", "output", "Foldback", ("out3",)),
    ChannelSpec("monitors", "output", "Stage monitors", ("out12",)),
)

DESK_SCENES: tuple[tuple[str, str, str, bool], ...] = (
    ("venue_default", VENUE_DEFAULT_REF, "Venue Default", True),
    ("lecture", LECTURE_REF, "Lecture Baseline", False),
    ("performance", PERFORMANCE_REF, "Performance", False),
)


def free_udp_port() -> int:
    """A UDP port nothing is bound to, for the native client's meter socket.

    The appliance uses a fixed port (51327, which its firewall opens); a test
    must not, so two runs, or a real client on the same machine, never meet.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


def midi_stub() -> CqMidiStub:
    """The desk: mid-show, three scenes stored, answering gets and echoing."""
    return CqMidiStub(state=dict(DESK_AT_FIRST_CONNECTION), presets=PRESETS)


@dataclass
class MixerRoom:
    """The configured mixer: the device, its channels and desk scenes, by id."""

    device: int
    main: int
    channels: dict[str, int]
    desk_scenes: dict[str, int]

    def channel(self, key: str) -> int:
        return self.main if key == "main" else self.channels[key]


async def _delete_channel(client: AsyncClient, channel_id: int) -> None:
    """``DELETE`` a channel, trying again while its pre-change snapshot fails.

    The test database is in memory, in SQLite's shared-cache mode, where a
    reader takes table locks that ``busy_timeout`` does not wait on: the
    snapshot's ``VACUUM INTO`` can meet the mixer service still reading the
    previous delete's configuration change and fail outright. An appliance's
    database is a file in WAL mode, where readers never block it.
    """
    for _ in range(100):
        response = await client.delete(f"{MIXER}/channels/{channel_id}")
        if response.status_code == 204:
            return
        body = response.json()
        assert body["error"]["detail"].get("reason") == "snapshot_failed", response.text
        await asyncio.sleep(0.02)
    raise AssertionError(f"mixer channel {channel_id} could not be deleted")


async def configure_mixer(
    client: AsyncClient,
    app: FastAPI,
    midi: CqMidiStub,
    *,
    meter_udp_port: int,
    metering: bool = True,
) -> MixerRoom:
    """The desk, its channels and its scene library — see the module docstring.

    Returns once the device is connected and the mixer service holds every
    channel, with the desk's values read for every tracked one.
    """
    device = ok(
        await client.post(
            DEVICES,
            json={
                "category": "mixer",
                "driver_key": "cq20b",
                "name": "CQ-20B",
                "config": {
                    "transport": {"type": "tcp", "host": "127.0.0.1", "port": midi.port},
                    "driver": {"metering": metering, "meter_udp_port": meter_udp_port},
                },
            },
        ),
        201,
    )
    device_id: int = device["id"]
    await wait_for_status(client, device_id, "connected")

    # §7.3: POST /devices created a channel for every desk channel, Main
    # among them. This small venue keeps Main and replaces the rest with its
    # own few, below.
    listed = ok(await client.get(f"{MIXER}/channels"))["channels"]
    assert len(listed) == 27, [row["driver_refs"] for row in listed]
    (main,) = [row for row in listed if row["channel_kind"] == "main"]
    assert (main["channel_kind"], main["driver_refs"]) == ("main", ["main"]), main
    # The service reads all of them from the desk once it sees the
    # connection; let that finish before the configuration changes under it.
    every_ref = [row["driver_refs"][0] for row in listed]

    def all_read() -> bool:
        driver = app.state.devices.running_driver(device_id)
        return driver is not None and len(driver.known_state(every_ref)) == len(every_ref)

    await until(all_read, "the mixer service to read every desk channel")
    for row in listed:
        if row["id"] != main["id"]:
            await _delete_channel(client, row["id"])

    channels: dict[str, int] = {}
    for order, spec in enumerate(CHANNELS):
        row = ok(
            await client.post(
                f"{MIXER}/channels",
                json={
                    "device_id": device_id,
                    "channel_kind": spec.kind,
                    "name": spec.name,
                    "driver_refs": list(spec.refs),
                    "show_pan": spec.show_pan,
                    "tracked": spec.tracked,
                    "sort_order": order,
                },
            ),
            201,
        )
        channels[spec.key] = row["id"]
    desk_scenes: dict[str, int] = {}
    for order, (key, ref, name, venue_default) in enumerate(DESK_SCENES):
        row = ok(
            await client.post(
                f"{MIXER}/desk-scenes",
                json={
                    "device_id": device_id,
                    "scene_ref": ref,
                    "name": name,
                    "is_venue_default": venue_default,
                    "sort_order": order,
                },
            ),
            201,
        )
        desk_scenes[key] = row["id"]

    room = MixerRoom(device_id, main["id"], channels, desk_scenes)
    await service_holds(app, room)
    return room


def driver_of(app: FastAPI, room: MixerRoom) -> CQ20BDriver:
    driver = app.state.devices.running_driver(room.device)
    assert isinstance(driver, CQ20BDriver), driver
    return driver


async def service_holds(app: FastAPI, room: MixerRoom) -> None:
    """Wait until the mixer service has loaded every channel, the driver tracks
    exactly the tracked ones, and each tracked channel shows what the driver
    has read from the desk — level, mute and, where shown, pan, which arrive
    as separate replies.

    Configuration writes reach the service as ``MixerConfigChanged``, off the
    request path, so this waits for the event to have been handled rather than
    racing it.
    """
    service = app.state.mixer
    tracked = {"main"} | {r for s in CHANNELS if s.tracked for r in s.refs}
    displayed = [("main", "main", False)] + [
        (s.key, s.refs[0], s.show_pan) for s in CHANNELS if s.tracked
    ]

    def loaded() -> bool:
        if not all(service.is_configured(room.channel(s.key)) for s in CHANNELS):
            return False
        driver = app.state.devices.running_driver(room.device)
        if driver is None or set(driver.tracked) != tracked:
            return False
        for key, ref, pan in displayed:
            known = driver.known_state([ref]).get(ref)
            live = service.live(room.channel(key))
            if known is None or live is None or (live.db, live.muted) != (known.db, known.muted):
                return False
            if pan and (known.pan is None or live.pan != known.pan):
                return False
        return True

    await until(loaded, "the mixer service to load the channels and read the desk")


# -- the wire --------------------------------------------------------------------


def since(midi: CqMidiStub, mark: int) -> list[StubMessage]:
    return midi.messages[mark:]


def sets(midi: CqMidiStub, mark: int = 0) -> list[tuple[Address, int]]:
    """Every absolute value written from ``mark`` on, as ``(address, value)``."""
    return [
        (m.address, m.value)
        for m in midi.messages[mark:]
        if m.kind == "set" and m.address is not None and m.value is not None
    ]


def gets(midi: CqMidiStub, mark: int = 0) -> list[Address]:
    return [m.address for m in midi.messages[mark:] if m.kind == "get" and m.address is not None]


def mute_steps(midi: CqMidiStub) -> list[StubMessage]:
    """Every increment or decrement that reached a mute address: the desk
    toggles on either (cq20b.md §2, §5), so this must always be empty."""
    return [
        m
        for m in midi.messages
        if m.kind in ("increment", "decrement") and m.address in MUTE_ADDRESSES
    ]


# -- the API -----------------------------------------------------------------------


async def mixer_state(client: AsyncClient) -> dict[str, Any]:
    body: dict[str, Any] = ok(await client.get(f"{MIXER}/state"))
    return body


def strip(state: dict[str, Any], channel_id: int) -> dict[str, Any]:
    """One channel object from ``GET /mixer/state``, whichever section it is in."""
    main = state["main"]
    if main is not None and main["channel_id"] == channel_id:
        found: dict[str, Any] = main
        return found
    for entry in (*state["outputs"], *state["inputs"]):
        if entry["channel_id"] == channel_id:
            return dict(entry)
    raise AssertionError(f"channel {channel_id} is not in the mixer state")
