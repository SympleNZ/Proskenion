"""The Phase 4 milestone at the API level (spec §18, ``docs/plans/phase-4.md``).

    "The mixer view stays in step with the CQ-20B, and scenes recall a
     baseline then adjust channels and outputs."

The real ``cq20b`` driver over TCP against :mod:`tests.stubs.cq_midi_stub`, and
its native meter client over TCP and UDP against
:mod:`tests.stubs.cq_native_stub`, behind the real application: the device is
configured through ``POST /devices``, its channels and desk scenes through
the mixer configuration API (:mod:`tests.integration.mixer_rig`), and every
clause is driven through the API, the scene engine or the stub's own pushes.
What the desk received is read from the stub's record; what the interface was
told is read at the broadcaster, as an operator's socket would be sent it
(:class:`tests.integration.av_rig.Frames`).

The clauses:

1. **The view stays in step** — the desk read at connection and never
   written; an app write on the wire and back as a frame with no badge;
   a MixPad change badged on an input, an output and Main, and cleared by the
   next app write; an untracked channel shown and controllable, never badged
   and never re-read; a toggle sent as an absolute mute; meters, and their
   loss; a linked stereo output as one channel.
2. **A baseline recalled, then adjusted** — §8.13's delays, the wire order
   (recall, the paced resync, the adjustments), and the adjustments surviving
   the resync.
3. **"Performance Start"** — Phase 3's scene (``test_phase3_milestone.py``),
   whose mixer actions could not succeed then, re-run with this desk: success.
4. **Restore Venue Default** (§13.5) — follows the designation when it moves.
5. **Degradation** — the stub mixer driver (no recall, no pan, no metering,
   eight channels) refused at save and at the API with ``unsupported``; the
   mixer lost mid-scene, and nothing written when it returns.
6. **Exclusivity** (§7.3) — refused is amber with §7.3's words; timed out is
   red, "Mixer offline."

§8.15's failure policy, §8.16's result rule and §16.1's closed error
vocabulary are asserted where each clause meets them. The browser half is
``tests/e2e/mixer-milestone.spec.ts``.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from proskenion.api.errors import ErrorCode
from proskenion.api.ws import SetResult, parse_set
from proskenion.config import Config, KnxSection
from proskenion.core.auth import TokenClaims
from proskenion.core.dmx.compositor import level_to_dmx
from proskenion.core.drivers.cq20b import MSG_OFFLINE, MSG_REFUSED, CQ20BDriver
from proskenion.core.mixer.native import NativeMeterClient
from proskenion.core.transport.tcp import TcpTransport
from proskenion.db.connection import Database
from tests.integration.av_rig import (
    SHOW_INPUT,
    SIDE_OF_STAGE_REF,
    Appliance,
    AvRoom,
    Frames,
    configure_av,
    projector_reaches,
    rebound,
)
from tests.integration.mixer_rig import (
    CHANNELS,
    DESK_AT_FIRST_CONNECTION,
    LECTURE_REF,
    LEVEL,
    MIXER,
    MONITORS_LEVEL,
    MONITORS_MUTE,
    MUTE,
    MUTED,
    PAN,
    PAN_R50,
    PERFORMANCE_REF,
    PRESETS,
    UNMUTED,
    VENUE_DEFAULT_REF,
    MixerRoom,
    configure_mixer,
    driver_of,
    free_udp_port,
    gets,
    level,
    midi_stub,
    mixer_state,
    mute_steps,
    service_holds,
    sets,
    strip,
)
from tests.integration.rig import (
    CONTROLLER,
    DEVICES,
    HOUSE_DIMMER,
    SCENES,
    Rig,
    build_rig,
    eventually,
    ok,
    until,
)
from tests.integration.test_first_run_flow import wait_for_status, walk_the_wizard

# The Phase 3 room's own fixtures — the projector, the matrix on its serial
# bridge and the compressed probe cadences — so "Performance Start" and
# "Restore Venue Default" run in the same room as they do there.
from tests.integration.test_phase3_milestone import (  # noqa: F401  (pytest fixtures)
    bridged,
    compressed_timing,
    matrix,
    pjlink,
)
from tests.stubs.artnet_stub import ArtNetStub
from tests.stubs.cq_midi_stub import CqMidiStub
from tests.stubs.cq_native_stub import CqNativeStub
from tests.stubs.knxd_stub import KnxdStub
from tests.stubs.lkv422_tcp import LKV422TcpStub
from tests.stubs.pjlink_stub import PJLinkStub

#: How soon the driver retries after a failed connection, compressed from 5 s
#: (§5.3). Only the retry changes; what a failure is classified as does not.
RETRY_S = 0.1
#: The TCP connect timeout for the timed-out case, compressed from the
#: transport's own, so a desk that never answers is known quickly.
CONNECT_TIMEOUT_S = 0.5
#: An address that answers nothing: TEST-NET-1 (RFC 5737), never routed.
UNREACHABLE = "192.0.2.1"

#: §8.13's worked example, compressed as Phase 3's milestone compresses it.
FADE_MS = 500
UNMUTE_AT_MS = 300  # §8.13: after the recall has settled, in a later group
HOUSE_LIGHTS_AT_MS = 700
INPUT_AT_MS = 2000
#: The adjustment made while the recall's scene is still loading — inside
#: the driver's 300 ms wait (§7.3), so it can only reach the desk after it.
EARLY_ADJUST_MS = 100
#: The adjustment made once the recall has long finished.
LATE_ADJUST_MS = 800

STAGE_DMX = level_to_dmx(80)


# -- fixtures -----------------------------------------------------------------------


@pytest.fixture
def knx_section(knxd: KnxdStub) -> KnxSection:
    return KnxSection(host="127.0.0.1", port=knxd.port, individual_address=CONTROLLER)


@pytest.fixture
def fast_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CQ-20B's retry after a failed connection, compressed (§5.3). The
    native metering connection keeps its own, separate retry state (§7.3,
    ``docs/protocols/cq20b-native.md`` §9); compressed here too, so a test
    of metering recovery does not have to wait out its real-world default."""
    monkeypatch.setattr(CQ20BDriver, "INITIAL_RETRY_DELAY", RETRY_S)
    monkeypatch.setattr(NativeMeterClient, "INITIAL_RETRY_DELAY", RETRY_S)


@pytest.fixture
async def midi() -> AsyncIterator[CqMidiStub]:
    """The desk's MIDI port: mid-show, three scenes stored (see mixer_rig)."""
    async with midi_stub() as stub:
        yield stub


@pytest.fixture
async def native(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[CqNativeStub]:
    """The desk's native port, on an OS-assigned port the driver is aimed at.

    ``CQ20BDriver.NATIVE_PORT`` is a class attribute for exactly this: the
    native connection shares the MIDI transport's host (§7.3), and only its
    port differs from the appliance's.
    """
    async with CqNativeStub() as stub:
        monkeypatch.setattr(CQ20BDriver, "NATIVE_PORT", stub.port)
        yield stub


@dataclass
class Desk:
    """The appliance, the configured mixer and both of the desk's ports."""

    appliance: Appliance
    room: MixerRoom
    midi: CqMidiStub
    native: CqNativeStub
    #: Where the stub's record stood when the application last booted.
    boot_mark: int = 0

    async def reboot(self) -> None:
        """Restart the application over the same database, as an installer
        does after commissioning, and wait until it holds the desk again."""
        await self.appliance.shutdown()
        self.boot_mark = len(self.midi.messages)
        await self.appliance.boot()
        await wait_for_status(self.client, self.room.device, "connected")
        await service_holds(self.app, self.room)

    @property
    def client(self) -> AsyncClient:
        return self.appliance.client

    @property
    def app(self) -> FastAPI:
        return self.appliance.app

    def channel(self, key: str) -> int:
        return self.room.channel(key)


@pytest.fixture
async def desk(
    config: Config,
    db: Database,
    midi: CqMidiStub,
    native: CqNativeStub,
    fast_retry: None,
) -> AsyncIterator[Desk]:
    """A fresh appliance, commissioned, with the mixer configured through the API.

    On the way out, one check for the whole milestone: nothing the
    application did, in any clause, sent an increment or decrement to a mute
    address — the desk toggles a mute on either (cq20b.md §2, §5).
    """
    appliance = Appliance(config, db)
    await appliance.boot()
    try:
        await walk_the_wizard(appliance.client)
        room = await configure_mixer(
            appliance.client, appliance.app, midi, meter_udp_port=free_udp_port()
        )
        desk = Desk(appliance, room, midi, native)
        await desk.reboot()
        yield desk
    finally:
        await appliance.shutdown()
    assert mute_steps(midi) == [], "a relative step reached a mute address"


@pytest.fixture
async def frames(desk: Desk) -> AsyncIterator[Frames]:
    """An operator's socket on the desk's appliance, subscribed to the mixer."""
    listening = Frames(desk.app, domains=("mixer",))
    await listening.caught_up()
    try:
        yield listening
    finally:
        await listening.close()


# -- helpers ------------------------------------------------------------------------


def mixer_frames(frames: Frames, since: float = 0.0) -> list[dict[str, Any]]:
    return [
        message
        for at, message in frames.received
        if at >= since and message.get("type") == "mixer_state"
    ]


def entry_of(frame: dict[str, Any], channel_id: int, room: MixerRoom) -> dict[str, Any] | None:
    """One channel's entry in a ``mixer_state`` frame (§16.8), or ``None``."""
    if channel_id == room.main:
        main = frame.get("main")
        return dict(main) if isinstance(main, dict) else None
    for section in ("outputs", "inputs"):
        found = (frame.get(section) or {}).get(str(channel_id))
        if found is not None:
            return dict(found)
    return None


async def frame_for(
    frames: Frames,
    room: MixerRoom,
    channel_id: int,
    predicate: Callable[[dict[str, Any]], bool],
    what: str,
    *,
    since: float = 0.0,
) -> dict[str, Any]:
    """The first ``mixer_state`` entry for ``channel_id`` from ``since`` on that
    satisfies ``predicate``."""

    def probe() -> dict[str, Any] | None:
        for frame in mixer_frames(frames, since):
            entry = entry_of(frame, channel_id, room)
            if entry is not None and predicate(entry):
                return entry
        return None

    return await until(probe, what)


def entries_for(
    frames: Frames, room: MixerRoom, channel_id: int, since: float = 0.0
) -> list[dict[str, Any]]:
    return [
        entry
        for frame in mixer_frames(frames, since)
        if (entry := entry_of(frame, channel_id, room)) is not None
    ]


async def add_action(client: AsyncClient, scene_id: int, body: dict[str, Any]) -> int:
    row = ok(await client.post(f"{SCENES}/{scene_id}/actions", json=body), 201)
    action_id: int = row["id"]
    return action_id


async def newest_log_id(client: AsyncClient, scene_id: int) -> int:
    """The id of the scene's newest execution-log entry, or 0 if it has none."""
    entries: list[dict[str, Any]] = ok(await client.get(f"{SCENES}/{scene_id}/log"))["entries"]
    return int(entries[0]["id"]) if entries else 0


async def completed_run(
    client: AsyncClient, scene_id: int, *, after_id: int = 0
) -> dict[str, Any]:
    """The scene's newest execution-log entry newer than ``after_id``, once
    that run has completed (§8.16). Without ``after_id`` a second run of the
    same scene could be read as the first's entry, still the newest until the
    new run's own entry is written."""

    async def probe() -> dict[str, Any] | None:
        body = ok(await client.get(f"{SCENES}/{scene_id}/log"))
        entries: list[dict[str, Any]] = body["entries"]
        if not entries or int(entries[0]["id"]) <= after_id:
            return None
        return entries[0] if entries[0]["completed_at"] is not None else None

    return await eventually(probe, f"scene {scene_id} to complete", timeout_s=15.0)


async def run_scene(client: AsyncClient, scene_id: int) -> dict[str, Any]:
    before = await newest_log_id(client, scene_id)
    ok(await client.post(f"{SCENES}/{scene_id}/trigger"), 202)
    return await completed_run(client, scene_id, after_id=before)


def lines_by_action(entry: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {line["action_id"]: line for line in entry["action_results"]}


async def written(midi: CqMidiStub, mark: int, count: int) -> list[tuple[Any, int]]:
    """The absolute writes from ``mark`` on, once ``count`` have arrived.

    An API write answers once its bytes are sent, which is before the stub has
    read them; this waits for the stub's record rather than racing it.
    """
    await until(lambda: len(sets(midi, mark)) >= count, f"{count} writes at the desk")
    return sets(midi, mark)


def error_of(response: Any) -> dict[str, Any]:
    body: dict[str, Any] = response.json()
    error: dict[str, Any] = body["error"]
    return error


# == 1. the view stays in step ======================================================


async def test_the_desk_is_read_at_connection_and_never_written(desk: Desk) -> None:
    """§7.3 *Reconnection*, §12.3: read state, write nothing. The view shows
    what the desk holds for every tracked channel, Main included — both when
    the channels were configured against a connected desk and after the
    reboot that follows commissioning."""
    midi, client = desk.midi, desk.client
    assert sets(midi) == [], "the application wrote to the desk on connecting"
    assert midi.messages_of("recall") == []

    state = await mixer_state(client)
    assert state["device_id"] == desk.room.device
    assert state["connected"] is True
    assert state["capabilities"] == {
        "scene_recall": True,
        "pan": True,
        "metering": True,
        "metering_reason": None,
    }
    assert [s["name"] for s in state["desk_scenes"]] == [
        "Venue Default",
        "Lecture Baseline",
        "Performance",
    ]
    assert [s["is_venue_default"] for s in state["desk_scenes"]] == [True, False, False]
    assert state["last_recalled_scene"] is None
    # Configuration order, not the desk's numbering (§5.5).
    assert [i["name"] for i in state["inputs"]] == [s.name for s in CHANNELS if s.kind == "input"]
    assert [o["name"] for o in state["outputs"]] == [s.name for s in CHANNELS if s.kind == "output"]

    def shows(key: str, db: float, muted: bool) -> None:
        channel = strip(state, desk.channel(key))
        assert channel["db"] == pytest.approx(db, abs=0.05), (key, channel)
        assert channel["muted"] is muted, (key, channel)
        assert channel["origin"] is None, (key, channel)

    shows("main", -1.0, False)
    shows("wireless", -9.0, False)
    shows("lectern", -13.0, True)
    shows("hdmi", -17.0, False)
    shows("foldback", -15.0, False)
    shows("monitors", -7.0, False)
    wireless = strip(state, desk.channel("wireless"))
    assert wireless["show_pan"] is True
    assert wireless["pan"] == pytest.approx(0.3, abs=0.01)  # R30% (cq20b.md §6)
    assert strip(state, desk.channel("hdmi"))["stereo"] is True
    assert strip(state, desk.channel("monitors"))["stereo"] is True
    # The untracked channel is shown, with nothing read for it (§21.21).
    spare = strip(state, desk.channel("spare"))
    assert (spare["db"], spare["muted"], spare["origin"]) == (None, False, None)

    # The connection's own sync: Main first, then the outputs, then the
    # inputs (§7.3), and the untracked input's addresses never asked for.
    queried = gets(midi, desk.boot_mark)
    assert queried[:2] == [LEVEL["main"], MUTE["main"]]
    assert LEVEL["ip5"] not in queried and MUTE["ip5"] not in queried
    first_input = min(queried.index(LEVEL[r]) for r in ("ip1", "ip2", "st2"))
    last_output = max(queried.index(LEVEL[r]) for r in ("out1", "out3"))
    assert last_output < first_input, queried


async def test_an_app_write_reaches_the_desk_and_returns_as_a_frame_with_no_badge(
    desk: Desk, frames: Frames
) -> None:
    """Level, a mute toggle and pan: each one absolute write on the wire, each
    back to the interface as ``mixer_state`` with no origin (§16.8)."""
    client, midi, room = desk.client, desk.midi, desk.room
    wireless, lectern = desk.channel("wireless"), desk.channel("lectern")
    mark, t0 = len(midi.messages), time.monotonic()

    body = ok(await client.post(f"{MIXER}/channels/{wireless}/level", json={"db": -5.0}))
    assert (body["db"], body["origin"]) == (-5.0, None)
    await written(midi, mark, 1)
    assert midi.value(LEVEL["ip1"]) == 10048  # −5 dB, cq20b.md §4 (p.15)

    # A toggle, resolved against the known state (the lectern is muted) and
    # sent as an absolute unmute (phase-4-contracts.md; cq20b.md §2).
    body = ok(await client.post(f"{MIXER}/channels/{lectern}/mute", json={"toggle": True}))
    assert body["muted"] is False
    await written(midi, mark, 2)
    assert midi.value(MUTE["ip2"]) == UNMUTED

    body = ok(await client.post(f"{MIXER}/channels/{wireless}/pan", json={"pan": 0.5}))
    assert body["pan"] == pytest.approx(0.5)
    await written(midi, mark, 3)
    assert midi.value(PAN["ip1"]) == PAN_R50

    assert sets(midi, mark) == [
        (LEVEL["ip1"], 10048),
        (MUTE["ip2"], UNMUTED),
        (PAN["ip1"], PAN_R50),
    ]
    assert [m.kind for m in midi.messages[mark:]] == ["set"] * 3

    level_entry = await frame_for(
        frames, room, wireless, lambda e: e["db"] == -5.0, "the level frame", since=t0
    )
    assert "origin" not in level_entry
    mute_entry = await frame_for(
        frames, room, lectern, lambda e: e["muted"] is False, "the unmute frame", since=t0
    )
    assert "origin" not in mute_entry
    pan_entry = await frame_for(
        frames, room, wireless, lambda e: e.get("pan") == 0.5, "the pan frame", since=t0
    )
    assert "origin" not in pan_entry

    # The desk's echoes of all three produced nothing further (§7.3).
    state = await mixer_state(client)
    assert strip(state, wireless)["origin"] is None
    assert strip(state, lectern)["origin"] is None

    # A second toggle mutes again — still absolute.
    ok(await client.post(f"{MIXER}/channels/{lectern}/mute", json={"toggle": True}))
    assert (await written(midi, mark, 4))[-1] == (MUTE["ip2"], MUTED)


async def test_a_mixpad_change_is_badged_on_an_input_an_output_and_main(
    desk: Desk, frames: Frames
) -> None:
    """§7.3 *Change origin tracking*, §21.13: an inbound value that is not an
    echo of ours is MixPad's. Main is badged exactly as an output or an input."""
    client, midi, room = desk.client, desk.midi, desk.room
    pushes = (
        ("wireless", LEVEL["ip1"], level(-20.0), "db", -20.0),
        ("foldback", MUTE["out3"], MUTED, "muted", True),
        ("main", LEVEL["main"], level(-6.0), "db", -6.0),
    )
    for key, address, value, field_name, shown in pushes:
        t0 = time.monotonic()
        await midi.push(address, value)
        entry = await frame_for(
            frames,
            room,
            desk.channel(key),
            lambda e, f=field_name, v=shown: e[f] == v,
            f"{key}'s MixPad frame",
            since=t0,
        )
        assert entry["origin"] == "mixpad", (key, entry)

    state = await mixer_state(client)
    for key, *_ in pushes:
        assert strip(state, desk.channel(key))["origin"] == "mixpad", key

    # The next app write on each clears it — Main included.
    for key, body, path in (
        ("wireless", {"db": -12.0}, "level"),
        ("foldback", {"muted": False}, "mute"),
        ("main", {"db": 0.0}, "level"),
    ):
        t0 = time.monotonic()
        answered = ok(await client.post(f"{MIXER}/channels/{desk.channel(key)}/{path}", json=body))
        assert answered["origin"] is None, (key, answered)
        entry = await frame_for(
            frames,
            room,
            desk.channel(key),
            lambda e: "origin" not in e,
            f"{key}'s cleared badge",
            since=t0,
        )
        assert "origin" not in entry
    state = await mixer_state(client)
    for key, *_ in pushes:
        assert strip(state, desk.channel(key))["origin"] is None, key


async def test_an_untracked_channel_is_controllable_never_badged_and_never_reread(
    desk: Desk, frames: Frames
) -> None:
    """§21.21: shown and controllable; its values are the application's own
    writes; the desk's are neither followed nor re-read, even after a recall."""
    client, midi, room = desk.client, desk.midi, desk.room
    spare, wireless = desk.channel("spare"), desk.channel("wireless")

    # MixPad moves it: discarded silently (§7.3). A tracked push behind it on
    # the same connection proves the first was read and dropped, not pending.
    t0 = time.monotonic()
    await midi.push(LEVEL["ip5"], level(-3.0))
    await midi.push(LEVEL["ip1"], level(-19.0))
    await frame_for(
        frames, room, wireless, lambda e: e["db"] == -19.0, "the tracked push", since=t0
    )
    assert entries_for(frames, room, spare, since=t0) == []
    spare_now = strip(await mixer_state(client), spare)
    assert (spare_now["db"], spare_now["origin"]) == (None, None)

    # Controllable: the write reaches the desk and the view shows it, no badge.
    mark, t0 = len(midi.messages), time.monotonic()
    body = ok(await client.post(f"{MIXER}/channels/{spare}/level", json={"db": -10.0}))
    assert (body["db"], body["origin"]) == (-10.0, None)
    body = ok(await client.post(f"{MIXER}/channels/{spare}/mute", json={"toggle": True}))
    assert body["muted"] is True  # resolved against our own record: unmuted
    assert await written(midi, mark, 2) == [(LEVEL["ip5"], level(-10.0)), (MUTE["ip5"], MUTED)]
    entry = await frame_for(
        frames, room, spare, lambda e: e["db"] == -10.0 and e["muted"], "the spare's write"
    )
    assert "origin" not in entry

    # MixPad again, then a recall and its resync: neither touches it.
    await midi.push(LEVEL["ip5"], level(-1.0))
    mark = len(midi.messages)
    ok(await client.post(f"{MIXER}/desk-scenes/{room.desk_scenes['lecture']}/test"))
    queried = gets(midi, mark)
    assert queried, "the recall was not followed by a resync"
    assert LEVEL["ip5"] not in queried and MUTE["ip5"] not in queried
    spare_now = strip(await mixer_state(client), spare)
    assert (spare_now["db"], spare_now["muted"], spare_now["origin"]) == (-10.0, True, None)


async def test_meters_arrive_per_reference_and_clear_when_metering_is_lost(
    desk: Desk, frames: Frames
) -> None:
    """§7.3 *Metering*, §16.8, B58: one value per reference and two for a
    stereo one, in reference order; when the native connection goes, every
    meter goes with it, and control carries on. The loss (and later the
    recovery) reaches the open view as its own ``mixer_meters`` frame the
    instant it happens — not only ``GET /mixer/state``."""
    client, room, native = desk.client, desk.room, desk.native
    hdmi, wireless, main = desk.channel("hdmi"), desk.channel("wireless"), room.main
    store = desk.app.state.state_store

    # The desk has been sending its capture since boot; nothing moves in it,
    # so nothing is news until something does. Someone speaks, the HDMI
    # source plays a louder left than right, and the room gets louder.
    t0 = time.monotonic()
    native.set_input_level(0, meter_raw(-40.0))  # Ip1
    native.set_input_level(18, meter_raw(-20.0))  # ST2 L
    native.set_input_level(19, meter_raw(-30.0))  # ST2 R
    native.set_output_level(6, meter_raw(-10.0))  # Main L
    native.set_output_level(7, meter_raw(-12.0))  # Main R

    def moved() -> dict[str, list[float | None]] | None:
        seen: dict[str, list[float | None]] = {}
        for at, message in frames.received:
            if at >= t0 and message.get("type") == "mixer_meters":
                seen.update(message["channels"])
        wanted = {str(hdmi): [-20.0, -30.0], str(wireless): [-40.0], str(main): [-10.0, -12.0]}
        return seen if all(seen.get(k) == v for k, v in wanted.items()) else None

    seen = await until(moved, "meter frames for the input, the stereo HDMI input and Main")
    assert len(seen[str(hdmi)]) == 2  # ST2 left, then right (§5.5)
    assert all(
        isinstance(m["at"], float) for _, m in frames.received if m.get("type") == "mixer_meters"
    )
    # Meters never reach the channel state or the control path (B58).
    assert "meters" not in strip(await mixer_state(client), hdmi)
    assert all("meters" not in f for f in mixer_frames(frames))

    # The native connection goes, and — for now — cannot come back: both
    # slots are taken.
    lost_at = time.monotonic()
    native.max_connections = 0
    await native.drop_all_connections()
    await until(lambda: store.mixer.get("meters") == {}, "the meters to be cleared")
    state = await mixer_state(client)
    assert state["capabilities"]["metering"] is False
    assert state["capabilities"]["metering_reason"] == "no_response"
    # Control is untouched: MIDI is a separate connection (§7.3).
    assert state["connected"] is True
    assert state["capabilities"]["scene_recall"] is True
    mark = len(desk.midi.messages)
    ok(await client.post(f"{MIXER}/channels/{wireless}/level", json={"db": -4.0}))
    assert await written(desk.midi, mark, 1) == [(LEVEL["ip1"], level(-4.0))]

    # The loss reached the open view at once — an empty `channels`, but the
    # frame still arrived, which is exactly the bug this fixes: a lone
    # `state.mixer.meters` clear, with no `metering` alongside it, produced
    # no frame at all and left an open view's bars frozen (§21.9).
    _, loss_frame = await frames.first(
        "mixer_meters",
        "the loss to reach the open view",
        since=lost_at,
        metering={"available": False, "reason": "no_response"},
    )
    assert loss_frame["channels"] == {}

    # A slot frees, and metering returns on its own — a separate retry from
    # MIDI's, never disturbing control (§7.3, cq20b-native.md §9) — reported
    # live the instant it happens, not only on the next `GET /mixer/state`.
    recovered_at = time.monotonic()
    native.max_connections = 1
    # `channels` is not asserted here: the desk's own read loop typically
    # has fresh readings by the same tick, so this frame legitimately
    # carries both the recovery and the first channels back at once — the
    # contract only fixes `metering`, never a shape for `channels` here.
    await frames.first(
        "mixer_meters",
        "the recovery to reach the open view",
        since=recovered_at,
        metering={"available": True, "reason": None},
    )
    state = await mixer_state(client)
    assert state["capabilities"]["metering"] is True
    assert state["capabilities"]["metering_reason"] is None


def meter_raw(db: float) -> int:
    """A native meter reading for ``db``: cq20b-native.md §7's conversion,
    ``dB = (raw - 0x8000) / 256 - 18``, inverted."""
    return round(0x8000 + (db + 18.0) * 256)


async def test_a_linked_stereo_output_is_one_channel(desk: Desk, frames: Frames) -> None:
    """§7.3 *Linked stereo outputs*: Out 1/2 linked is one stereo channel on
    Out 1's addresses; Out 2 is never written, and a push to it is not ours."""
    client, midi, room = desk.client, desk.midi, desk.room
    monitors = desk.channel("monitors")
    mark = len(midi.messages)

    ok(await client.post(f"{MIXER}/channels/{monitors}/level", json={"db": -3.0}))
    ok(await client.post(f"{MIXER}/channels/{monitors}/mute", json={"muted": True}))
    assert await written(midi, mark, 2) == [(MONITORS_LEVEL, level(-3.0)), (MONITORS_MUTE, MUTED)]
    assert midi.value(LEVEL["out2"]) == DESK_AT_FIRST_CONNECTION[LEVEL["out2"]]
    assert midi.value(MUTE["out2"]) == DESK_AT_FIRST_CONNECTION[MUTE["out2"]]

    # MixPad moves the pair: it arrives on Out 1's address, as one channel.
    t0 = time.monotonic()
    await midi.push(MONITORS_LEVEL, level(-8.0))
    entry = await frame_for(
        frames, room, monitors, lambda e: e["db"] == -8.0, "the pair's MixPad frame", since=t0
    )
    assert entry["origin"] == "mixpad"
    # A move on Out 2's own address is an unconfigured channel: discarded.
    t0 = time.monotonic()
    await midi.push(LEVEL["out2"], level(-30.0))
    await midi.push(MONITORS_MUTE, UNMUTED)
    await frame_for(
        frames, room, monitors, lambda e: e["muted"] is False, "the pair's unmute", since=t0
    )
    assert all(e["db"] == -8.0 for e in entries_for(frames, room, monitors, since=t0))
    channel = strip(await mixer_state(client), monitors)
    assert (channel["stereo"], channel["db"], channel["muted"]) == (True, -8.0, False)


# == 2. a baseline recalled, then adjusted ===========================================


async def test_a_scene_recalls_a_baseline_then_adjusts_inputs_an_output_and_main(
    desk: Desk, frames: Frames
) -> None:
    """§7.3 *Integration model*, §8.13: recall at t=0; the adjustments at later
    delays reach the desk only after the recall's paced resync, so the resync
    cannot overwrite them; the room ends as the scene says."""
    client, midi, room = desk.client, desk.midi, desk.room
    wireless, lectern = desk.channel("wireless"), desk.channel("lectern")
    monitors = desk.channel("monitors")
    scene = ok(await client.post(SCENES, json={"name": "Lecture"}), 201)
    sid = scene["id"]
    actions = {
        "baseline": await add_action(
            client,
            sid,
            {
                "domain": "mixer_recall",
                "sort_order": 0,
                "delay_ms": 0,
                "mixer_scene_id": room.desk_scenes["lecture"],
            },
        ),
        # While the scene is still loading: an input and the stage monitors.
        "wireless down": await add_action(
            client,
            sid,
            {
                "domain": "mixer_fader",
                "sort_order": 0,
                "delay_ms": EARLY_ADJUST_MS,
                "mixer_channel_id": wireless,
                "mixer_db": -12.0,
            },
        ),
        "monitors on": await add_action(
            client,
            sid,
            {
                "domain": "mixer_mute",
                "sort_order": 1,
                "delay_ms": EARLY_ADJUST_MS,
                "mixer_channel_id": monitors,
                "mixer_muted": False,
            },
        ),
        # Once it has long finished: Main, and the lectern muted.
        "main down": await add_action(
            client,
            sid,
            {
                "domain": "mixer_fader",
                "sort_order": 0,
                "delay_ms": LATE_ADJUST_MS,
                "mixer_channel_id": room.main,
                "mixer_db": -4.0,
            },
        ),
        "lectern off": await add_action(
            client,
            sid,
            {
                "domain": "mixer_mute",
                "sort_order": 1,
                "delay_ms": LATE_ADJUST_MS,
                "mixer_channel_id": lectern,
                "mixer_muted": True,
            },
        ),
    }

    mark = len(midi.messages)
    entry = await run_scene(client, sid)
    adjustments = await written(midi, mark, 4)

    # -- the log (§8.16): every action confirmed, so the run is a success -------
    assert entry["result"] == "success", entry
    lines = lines_by_action(entry)
    baseline = lines[actions["baseline"]]
    assert (baseline["domain"], baseline["result"], baseline["marker"]) == (
        "mixer_recall",
        "confirmed",
        "✓",
    )
    assert baseline["detail"] == {
        "scene_id": room.desk_scenes["lecture"],
        "name": "Lecture Baseline",
    }
    for name, domain, detail in (
        ("wireless down", "mixer_fader", {"channel_id": wireless, "db": -12.0}),
        ("monitors on", "mixer_mute", {"channel_id": monitors, "muted": False}),
        ("main down", "mixer_fader", {"channel_id": room.main, "db": -4.0}),
        ("lectern off", "mixer_mute", {"channel_id": lectern, "muted": True}),
    ):
        line = lines[actions[name]]
        assert (line["domain"], line["result"], line["marker"]) == (domain, "confirmed", "✓")
        assert line["detail"] == detail, name
    assert lines[actions["main down"]]["fired_at_ms"] >= LATE_ADJUST_MS

    # -- the wire: recall, then the resync's gets, then the adjustments ----------
    wire = midi.messages[mark:]
    kinds = [m.kind for m in wire]
    recall_at = kinds.index("recall")
    assert wire[recall_at].value == int(LECTURE_REF)
    first_set = kinds.index("set")
    last_get = max(i for i, k in enumerate(kinds) if k == "get")
    assert recall_at < kinds.index("get") < last_get < first_set, kinds
    assert set(kinds) == {"recall", "get", "set"}, kinds
    # The resync is the whole tracked surface, Main first (§7.3), paced.
    resync = [m for m in wire[recall_at:first_set] if m.kind == "get"]
    assert [m.address for m in resync[:2]] == [LEVEL["main"], MUTE["main"]]
    # Paced at §7.3's 5-10 ms. Timestamps are taken where the stub receives,
    # and loopback TCP can hand two paced queries over in one read, so a
    # single gap may look short; the whole span cannot, less the one delay
    # the first query may have had in arriving.
    span = resync[-1].at - resync[0].at
    floor = (len(resync) - 1) * 0.005 - 0.0075
    assert span >= floor, [round(b.at - a.at, 4) for a, b in zip(resync, resync[1:], strict=False)]
    # The recall's wait: the scene had loaded before the first query went out.
    (loaded_at, scene_number), *_ = midi.recalls_applied
    assert scene_number == int(LECTURE_REF)
    assert loaded_at <= resync[0].at
    # Each delay group's two actions in either order (§8.13: none within a
    # group), the groups in delay order.
    assert len(adjustments) == 4, adjustments
    assert set(adjustments[:2]) == {(LEVEL["ip1"], level(-12.0)), (MONITORS_MUTE, UNMUTED)}
    assert set(adjustments[2:]) == {(LEVEL["main"], level(-4.0)), (MUTE["ip2"], MUTED)}

    # -- the room: the baseline where untouched, the adjustments where made -----
    preset = PRESETS[int(LECTURE_REF)]
    assert midi.value(LEVEL["ip2"]) == preset[LEVEL["ip2"]]
    assert midi.value(LEVEL["st2"]) == preset[LEVEL["st2"]]
    assert midi.value(MONITORS_LEVEL) == preset[MONITORS_LEVEL]
    assert midi.value(LEVEL["ip1"]) == level(-12.0)
    assert midi.value(MONITORS_MUTE) == UNMUTED
    assert midi.value(LEVEL["main"]) == level(-4.0)
    assert midi.value(MUTE["ip2"]) == MUTED

    # -- and the interface, which never badged any of it -------------------------
    state = await mixer_state(client)
    assert state["last_recalled_scene"] == {
        "id": room.desk_scenes["lecture"],
        "name": "Lecture Baseline",
    }
    for key, db, muted in (
        ("wireless", -12.0, False),
        ("lectern", -6.0, True),
        ("hdmi", -8.0, False),
        ("monitors", -4.0, False),
        ("main", -4.0, False),
    ):
        channel = strip(state, desk.channel(key))
        assert channel["db"] == pytest.approx(db, abs=0.05), (key, channel)
        assert (channel["muted"], channel["origin"]) == (muted, None), (key, channel)
    assert all("origin" not in e for f in mixer_frames(frames) for e in _entries(f))


def _entries(frame: dict[str, Any]) -> list[dict[str, Any]]:
    out = [frame["main"]] if isinstance(frame.get("main"), dict) else []
    for section in ("outputs", "inputs"):
        out.extend(dict(v) for v in (frame.get(section) or {}).values())
    return out


# == 3 and 4: the whole room ==========================================================


@dataclass
class Room:
    """Phase 3's room — lighting, KNX, the projector, the matrix — with the desk."""

    appliance: Appliance
    lighting: Rig
    av: AvRoom
    mixer: MixerRoom
    midi: CqMidiStub
    pjlink: PJLinkStub
    matrix: LKV422TcpStub

    @property
    def client(self) -> AsyncClient:
        return self.appliance.client

    @property
    def app(self) -> FastAPI:
        return self.appliance.app


@pytest.fixture
async def room(
    config: Config,
    db: Database,
    knxd: KnxdStub,
    artnet: ArtNetStub,
    pjlink: PJLinkStub,  # noqa: F811
    matrix: LKV422TcpStub,  # noqa: F811
    bridged: None,  # noqa: F811
    compressed_timing: None,  # noqa: F811
    midi: CqMidiStub,
    native: CqNativeStub,
    fast_retry: None,
) -> AsyncIterator[Room]:
    """The whole room commissioned through the API, then rebooted, as Phase 3's
    milestone builds it — with the desk configured as the clauses above have it."""
    appliance = Appliance(config, db)
    await appliance.boot()
    try:
        lighting = await build_rig(appliance.client, appliance.app, knxd, artnet)
        av = await configure_av(appliance.client, pjlink)
        mixer = await configure_mixer(
            appliance.client, appliance.app, midi, meter_udp_port=free_udp_port()
        )
        await appliance.reboot()
        client = appliance.client
        for device in (av.projector, av.matrix, mixer.device):
            await wait_for_status(client, device, "connected")
        await service_holds(appliance.app, mixer)
        await projector_reaches(client, "off")
        yield Room(appliance, rebound(lighting, appliance), av, mixer, midi, pjlink, matrix)
    finally:
        await appliance.shutdown()
    assert mute_steps(midi) == [], "a relative step reached a mute address"


async def test_performance_start_with_the_mixer_is_a_success(room: Room) -> None:
    """Phase 3's "Performance Start" (§8.13's worked example), whose two mixer
    actions failed there for want of a configured mixer, re-run in the same
    room with the desk: every action ✓, so the run is a ``success`` (§8.16)."""
    client, rig, av, mixer, midi = room.client, room.lighting, room.av, room.mixer, room.midi
    scene = ok(await client.post(SCENES, json={"name": "Performance Start"}), 201)
    sid: int = scene["id"]
    actions = {
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
                "mixer_scene_id": mixer.desk_scenes["lecture"],
            },
        ),
        "unmute monitors": await add_action(
            client,
            sid,
            {
                "domain": "mixer_mute",
                "sort_order": 3,
                "delay_ms": UNMUTE_AT_MS,
                "mixer_channel_id": mixer.channels["monitors"],
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

    mark = len(midi.messages)
    entry = await run_scene(client, sid)
    unmuted = await written(midi, mark, 1)

    assert entry["result"] == "success", entry
    lines = lines_by_action(entry)
    assert set(lines) == set(actions.values())
    assert {line["marker"] for line in lines.values()} == {"✓"}, lines
    baseline = lines[actions["lecture baseline"]]
    assert (baseline["domain"], baseline["result"]) == ("mixer_recall", "confirmed")
    assert baseline["detail"] == {
        "scene_id": mixer.desk_scenes["lecture"],
        "name": "Lecture Baseline",
    }
    monitors = lines[actions["unmute monitors"]]
    assert (monitors["domain"], monitors["result"]) == ("mixer_mute", "confirmed")
    assert monitors["detail"] == {"channel_id": mixer.channels["monitors"], "muted": False}

    # The desk: one recall of scene 3, then — a later group, as §8.13 requires
    # of an adjustment that follows a recall — one absolute unmute of the pair
    # on Out 1's address; otherwise the recall's resync.
    wire = midi.messages[mark:]
    assert [m.value for m in wire if m.kind == "recall"] == [int(LECTURE_REF)]
    assert unmuted == [(MONITORS_MUTE, UNMUTED)]
    recall_at = next(i for i, m in enumerate(wire) if m.kind == "recall")
    unmute_at = max(i for i, m in enumerate(wire) if m.kind == "set")
    assert recall_at < unmute_at, "the unmute reached the desk before the recall"
    assert {m.kind for m in wire} == {"recall", "get", "set"}
    # Every other domain did what Phase 3's milestone proves it does.
    await rig.first_frame(lambda values: values == (STAGE_DMX,) * 4, since=0, what="the stage wash")
    assert (room.pjlink.power, room.pjlink.current_input) == ("1", SHOW_INPUT)
    assert room.matrix.routing() == {"1": "2", "2": "2"}


async def test_restore_venue_default_recalls_whichever_desk_scene_is_designated(
    room: Room,
) -> None:
    """§13.5: a protected scene whose recall names no desk scene recalls the
    Venue Default — and follows the designation when it moves — alongside the
    house lighting, the default HDMI source and the projector."""
    client, rig, av, mixer, midi = room.client, room.lighting, room.av, room.mixer, room.midi
    hdmi_matrix = room.matrix
    scene = ok(
        await client.post(
            SCENES,
            json={"name": "Restore Venue Default", "protected": True, "visible_operator": True},
        ),
        201,
    )
    sid: int = scene["id"]
    actions = {
        "desk": await add_action(
            client, sid, {"domain": "mixer_recall", "sort_order": 0, "delay_ms": 0}
        ),
        "stage dark": await add_action(
            client,
            sid,
            {
                "domain": "dmx",
                "sort_order": 1,
                "delay_ms": 0,
                "dmx_snapshot": {str(f): {"level": 0} for f in rig.fixtures},
                "dmx_fade_ms": 0,
            },
        ),
        "house lights": await add_action(
            client,
            sid,
            {
                "domain": "knx",
                "sort_order": 2,
                "delay_ms": 0,
                "knx_address_id": rig.addresses[HOUSE_DIMMER],
                "knx_value": "100",
            },
        ),
        "default source": await add_action(
            client,
            sid,
            {"domain": "hdmi_source", "sort_order": 3, "delay_ms": 0, "hdmi_destination": av.room},
        ),
        "projector off": await add_action(
            client,
            sid,
            {"domain": "projector_power", "sort_order": 4, "delay_ms": 0, "projector_power": "off"},
        ),
    }
    # Someone has been at the desk and the matrix since.
    await midi.push(LEVEL["ip1"], level(-40.0))
    await hdmi_matrix.front_panel("1", "2")
    await hdmi_matrix.front_panel("2", "2")
    # Protected: it cannot be deleted (§13.5).
    refused = await client.delete(f"{SCENES}/{sid}")
    assert refused.status_code == 403, refused.text
    assert error_of(refused)["code"] == "permission_denied"
    assert error_of(refused)["detail"]["reason"] == "protected"

    mark = len(midi.messages)
    entry = await run_scene(client, sid)
    assert entry["result"] == "success", entry
    lines = lines_by_action(entry)
    desk_line = lines[actions["desk"]]
    assert (desk_line["result"], desk_line["marker"]) == ("confirmed", "✓")
    assert desk_line["detail"] == {
        "default": True,
        "scene_id": mixer.desk_scenes["venue_default"],
        "name": "Venue Default",
    }
    assert [m.value for m in midi.messages[mark:] if m.kind == "recall"] == [int(VENUE_DEFAULT_REF)]
    assert sets(midi, mark) == [], "restoring the Venue Default wrote more than a recall"
    for address, value in PRESETS[int(VENUE_DEFAULT_REF)].items():
        assert midi.value(address) == value, address
    assert lines[actions["default source"]]["detail"]["input_id"] == av.side_of_stage
    assert hdmi_matrix.routing() == {"1": SIDE_OF_STAGE_REF, "2": SIDE_OF_STAGE_REF}
    wireless = strip(await mixer_state(client), mixer.channels["wireless"])
    assert (wireless["db"], wireless["origin"]) == (-10.0, None)

    # The engineer re-saves the baseline as another scene and moves the
    # designation to it (§13.5's operational dependency): the button follows.
    current = ok(await client.get(f"{MIXER}/desk-scenes/{mixer.desk_scenes['performance']}"))
    ok(
        await client.put(
            f"{MIXER}/desk-scenes/{mixer.desk_scenes['performance']}",
            json={"is_venue_default": True},
            headers={"If-Unmodified-Since-Version": current["updated_at"]},
        )
    )
    listed = ok(await client.get(f"{MIXER}/desk-scenes"))["desk_scenes"]
    assert [s["name"] for s in listed if s["is_venue_default"]] == ["Performance"]

    mark = len(midi.messages)
    entry = await run_scene(client, sid)
    assert entry["result"] == "success", entry
    desk_line = lines_by_action(entry)[actions["desk"]]
    assert desk_line["detail"] == {
        "default": True,
        "scene_id": mixer.desk_scenes["performance"],
        "name": "Performance",
    }
    assert [m.value for m in midi.messages[mark:] if m.kind == "recall"] == [int(PERFORMANCE_REF)]
    for address, value in PRESETS[int(PERFORMANCE_REF)].items():
        assert midi.value(address) == value, address


# == 5. degradation ===================================================================


@pytest.fixture
async def commissioned(config: Config, db: Database) -> AsyncIterator[Appliance]:
    """A fresh appliance through the wizard, with no mixer yet."""
    appliance = Appliance(config, db)
    await appliance.boot()
    try:
        await walk_the_wizard(appliance.client)
        yield appliance
    finally:
        await appliance.shutdown()


async def test_the_stub_mixer_degrades_to_its_declared_capabilities(
    commissioned: Appliance,
) -> None:
    """§5.5, §18: the stub mixer — no scene recall, no pan, no metering, eight
    channels — configured the same way, and refused with ``unsupported``
    wherever it is asked for what it does not have: at save and at the API."""
    client = commissioned.client
    device = ok(
        await client.post(
            DEVICES,
            json={
                "category": "mixer",
                "driver_key": "stub",
                "name": "Stub mixer",
                "config": {"transport": {"type": "loopback"}, "driver": {}},
            },
        ),
        201,
    )
    await wait_for_status(client, device["id"], "connected")
    refs = ok(await client.get(f"{DEVICES}/{device['id']}/refs"))["refs"]
    assert len(refs) == 8
    # Every one of its eight references became a channel, Main among them.
    listed = ok(await client.get(f"{MIXER}/channels"))["channels"]
    assert [row["driver_refs"] for row in listed] == [[ref["ref"]] for ref in refs]
    (main,) = [row for row in listed if row["channel_kind"] == "main"]
    assert (main["channel_kind"], main["driver_refs"]) == ("main", ["main"])
    law = ok(await client.get(f"{DEVICES}/{device['id']}/fader-law"))["fader_law"]
    assert law[0]["db"] is None and any(p.get("detent") for p in law)

    (input_1,) = [row for row in listed if row["driver_refs"] == ["in1"]]
    mic = ok(
        await client.put(
            f"{MIXER}/channels/{input_1['id']}",
            json={"name": "Mic 1", "show_pan": True},
            headers={"If-Unmodified-Since-Version": input_1["updated_at"]},
        )
    )
    desk_scene = ok(
        await client.post(
            f"{MIXER}/desk-scenes",
            json={"device_id": device["id"], "scene_ref": "1", "name": "Venue Default"},
        ),
        201,
    )
    service = commissioned.app.state.mixer
    await until(lambda: service.is_configured(mic["id"]), "the mixer service to load Mic 1")

    state = await mixer_state(client)
    assert state["capabilities"] == {
        "scene_recall": False,
        "pan": False,
        "metering": False,
        "metering_reason": "unsupported",
    }
    assert state["connected"] is True
    (strip_,) = [entry for entry in state["inputs"] if entry["channel_id"] == mic["id"]]
    assert (strip_["show_pan"], strip_["pan"]) == (True, None)
    # Retained, not deleted, when the driver cannot recall them (§15.6).
    assert [s["name"] for s in state["desk_scenes"]] == ["Venue Default"]

    for path, body in (
        (f"{MIXER}/channels/{mic['id']}/pan", {"pan": 0.5}),
        (f"{MIXER}/desk-scenes/{desk_scene['id']}/recall", None),
        (f"{MIXER}/desk-scenes/{desk_scene['id']}/test", None),
    ):
        refused = await client.post(path, json=body)
        assert refused.status_code == 422, (path, refused.text)
        error = error_of(refused)
        assert (error["code"], error["detail"]["reason"]) == ("validation_failed", "unsupported")

    # What it has works: level and mute (§5.5).
    body = ok(await client.post(f"{MIXER}/channels/{mic['id']}/level", json={"db": -6.0}))
    assert (body["db"], body["origin"]) == (-6.0, None)
    body = ok(await client.post(f"{MIXER}/channels/{mic['id']}/mute", json={"toggle": True}))
    assert body["muted"] is True

    # A scene cannot be given a recall it could never run (§5.5): refused at save.
    scene = ok(await client.post(SCENES, json={"name": "Lecture"}), 201)
    for body in (
        {"domain": "mixer_recall", "delay_ms": 0, "mixer_scene_id": desk_scene["id"]},
        {"domain": "mixer_recall", "delay_ms": 0},
    ):
        refused = await client.post(f"{SCENES}/{scene['id']}/actions", json=body)
        assert refused.status_code == 422, refused.text
        error = error_of(refused)
        assert (error["code"], error["detail"]["reason"]) == ("validation_failed", "unsupported")
    # The fader and the mute are accepted, and run.
    await add_action(
        client,
        scene["id"],
        {"domain": "mixer_fader", "delay_ms": 0, "mixer_channel_id": mic["id"], "mixer_db": -3.0},
    )
    await add_action(
        client,
        scene["id"],
        {
            "domain": "mixer_mute",
            "delay_ms": 0,
            "mixer_channel_id": mic["id"],
            "mixer_muted": False,
        },
    )
    entry = await run_scene(client, scene["id"])
    assert entry["result"] == "success", entry
    strip_ = strip(await mixer_state(client), mic["id"])
    assert (strip_["db"], strip_["muted"]) == (-3.0, False)


async def test_the_mixer_lost_mid_scene_fails_the_action_and_its_return_writes_nothing(
    desk: Desk,
) -> None:
    """§8.15: the scene carries on and the lost action fails with its reason;
    §8.16: something failed and something succeeded, so ``partial``; §7.3
    *Reconnection*: back online, the desk is read and nothing is written —
    not the failed action, not anything else."""
    client, midi = desk.client, desk.midi
    wireless, lectern = desk.channel("wireless"), desk.channel("lectern")
    scene = ok(await client.post(SCENES, json={"name": "Interval"}), 201)
    sid: int = scene["id"]
    #: Long enough after t=0 that the desk has certainly been lost first —
    #: checked below rather than assumed.
    later_ms = 2000
    fader = await add_action(
        client,
        sid,
        {"domain": "mixer_fader", "delay_ms": 0, "mixer_channel_id": wireless, "mixer_db": -12.0},
    )
    mute = await add_action(
        client,
        sid,
        {
            "domain": "mixer_mute",
            "delay_ms": later_ms,
            "mixer_channel_id": lectern,
            "mixer_muted": False,
        },
    )
    lectern_before = midi.value(MUTE["ip2"])
    driver = driver_of(desk.app, desk.room)  # the same driver reconnects (§5.3)

    started = time.monotonic()
    ok(await client.post(f"{SCENES}/{sid}/trigger"), 202)
    await until(lambda: (LEVEL["ip1"], level(-12.0)) in sets(midi), "the t=0 fader on the wire")
    # The desk goes: dropped, and refused from now on.
    await midi.refuse_connections()
    devices = desk.app.state.devices
    await until(lambda: devices.running_driver(desk.room.device) is None, "the mixer to be lost")
    assert time.monotonic() - started < later_ms / 1000, "the desk was lost too late to test"

    # While it is gone: control answers device_unavailable (§16.1), and the
    # view keeps the last known values with connected false (§16.5).
    for path, body in (
        (f"{MIXER}/channels/{wireless}/level", {"db": -3.0}),
        (f"{MIXER}/channels/{wireless}/mute", {"muted": True}),
        (f"{MIXER}/channels/{wireless}/pan", {"pan": 0.0}),
        (f"{MIXER}/desk-scenes/{desk.room.desk_scenes['lecture']}/recall", None),
    ):
        refused = await client.post(path, json=body)
        assert refused.status_code == 503, (path, refused.text)
        assert error_of(refused)["code"] == "device_unavailable"
    state = await mixer_state(client)
    assert state["connected"] is False
    assert strip(state, wireless)["db"] == -12.0

    entry = await completed_run(client, sid)
    assert entry["result"] == "partial", entry
    lines = lines_by_action(entry)
    assert (lines[fader]["result"], lines[fader]["marker"]) == ("confirmed", "✓")
    lost = lines[mute]
    assert (lost["result"], lost["marker"]) == ("failed", "✗")
    assert lost["reason"] == "the mixer is not available"
    assert lost["detail"] == {"channel_id": lectern}
    assert lost["fired_at_ms"] >= later_ms

    # The desk comes back: read, never written.
    syncs = driver.syncs_completed
    mark = len(midi.messages)
    await midi.accept_connections()
    await wait_for_status(client, desk.room.device, "connected")
    await until(lambda: driver.syncs_completed > syncs, "the reconnection's sync")
    await service_holds(desk.app, desk.room)
    wire = midi.messages[mark:]
    assert wire and {m.kind for m in wire} == {"get"}, [m.kind for m in wire]
    assert midi.value(MUTE["ip2"]) == lectern_before
    assert (await mixer_state(client))["connected"] is True


# == 6. exclusivity (§7.3) ============================================================


async def test_another_midi_client_holding_the_desk_is_amber_with_the_reason(
    desk: Desk,
) -> None:
    """Refused: another client has the desk's one MIDI connection. Amber, with
    §7.3's words; the view carries its last values; it clears when freed."""
    client, midi = desk.client, desk.midi
    wireless = desk.channel("wireless")
    await midi.refuse_connections()

    record = await wait_for_status(client, desk.room.device, "degraded")
    assert record["status"]["detail"] == MSG_REFUSED
    assert record["status"]["kind"] == "device"
    listed = ok(await client.get(DEVICES))["devices"]
    (mixer,) = [d for d in listed if d["id"] == desk.room.device]
    assert (mixer["status"]["status"], mixer["status"]["detail"]) == ("degraded", MSG_REFUSED)
    state = await mixer_state(client)
    assert state["connected"] is False
    assert strip(state, wireless)["db"] == pytest.approx(-9.0, abs=0.05)
    # Capabilities as declared while it is out of reach (§5.5).
    assert state["capabilities"]["scene_recall"] is True

    await midi.accept_connections()
    await wait_for_status(client, desk.room.device, "connected")
    assert (await mixer_state(client))["connected"] is True
    assert sets(midi) == []


async def test_a_desk_that_never_answers_is_red_mixer_offline(
    commissioned: Appliance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Timed out, or unreachable: red, "Mixer offline." (§7.3) — not the
    amber refusal, whose remedy is different."""
    monkeypatch.setattr(TcpTransport, "CONNECT_TIMEOUT", CONNECT_TIMEOUT_S)
    client = commissioned.client
    device = ok(
        await client.post(
            DEVICES,
            json={
                "category": "mixer",
                "driver_key": "cq20b",
                "name": "CQ-20B",
                "config": {
                    "transport": {"type": "tcp", "host": UNREACHABLE, "port": 51325},
                    "driver": {"metering": False},
                },
            },
        ),
        201,
    )
    record = await wait_for_status(client, device["id"], "error")
    assert record["status"]["detail"] == MSG_OFFLINE
    assert record["status"]["kind"] == "config"
    state = await mixer_state(client)
    assert (state["device_id"], state["connected"]) == (device["id"], False)
    # Main was still created (§7.3): the desk need not answer for that.
    assert state["main"] is not None and state["main"]["name"] == "Main LR"


# == §22.4: the failure modes the unit suite leaves to the real stack ==================


def operator_claims() -> TokenClaims:
    now = datetime.now(UTC)
    later = now + timedelta(hours=1)
    return TokenClaims(
        tier="operator",
        token_version=1,
        issued_at=now,
        expires_at=later,
        absolute_expires_at=later,
    )


async def ws_set(app: FastAPI, channel_id: int, value: float | None, token: int) -> SetResult:
    """A WebSocket ``set`` (§16.8) for the mixer domain, parsed and routed as
    the ``/ws`` endpoint does after authenticating the socket (httpx's ASGI
    transport carries no WebSocket; the parse and the router are the path)."""
    request, reason, _ = parse_set(
        {"type": "set", "domain": "mixer", "id": channel_id, "value": value, "token": token}
    )
    assert request is not None, reason
    result: SetResult = await app.state.writes.apply(request, operator_claims())
    return result


async def test_the_websocket_set_moves_a_fader_and_nacks_with_the_truth(desk: Desk) -> None:
    """The keyboard's ±1 dB and a fader drag are an absolute ``set`` (contract,
    §24.2): ``ack`` once on the wire; ``nack`` from the closed vocabulary with
    the authoritative dB where there is one."""
    app, midi = desk.app, desk.midi
    wireless = desk.channel("wireless")
    mark = len(midi.messages)

    assert (await ws_set(app, wireless, -8.0, 1)).ok
    assert await written(midi, mark, 1) == [(LEVEL["ip1"], level(-8.0))]
    assert (await ws_set(app, wireless, None, 2)).ok  # off is null (§5.5)
    assert (await written(midi, mark, 2))[-1] == (LEVEL["ip1"], 0)

    too_loud = await ws_set(app, wireless, 20.0, 3)
    assert (too_loud.ok, too_loud.reason, too_loud.value) == (
        False,
        ErrorCode.VALUE_OUT_OF_RANGE,
        10.0,
    )
    assert (await written(midi, mark, 3))[-1] == (LEVEL["ip1"], level(10.0))

    unknown = await ws_set(app, 999_999, -5.0, 4)
    assert (unknown.ok, unknown.reason) == (False, ErrorCode.NOT_FOUND)

    await midi.refuse_connections()
    await until(
        lambda: app.state.devices.running_driver(desk.room.device) is None, "the mixer to be lost"
    )
    offline = await ws_set(app, wireless, -3.0, 5)
    assert (offline.ok, offline.reason, offline.value) == (
        False,
        ErrorCode.DEVICE_UNAVAILABLE,
        10.0,
    )
    assert len(sets(midi, mark)) == 3


async def test_unknown_ids_are_not_found_on_every_control_endpoint(desk: Desk) -> None:
    client = desk.client
    for path, body in (
        (f"{MIXER}/channels/999999/level", {"db": -5.0}),
        (f"{MIXER}/channels/999999/mute", {"toggle": True}),
        (f"{MIXER}/channels/999999/pan", {"pan": 0.0}),
        (f"{MIXER}/desk-scenes/999999/recall", None),
        (f"{MIXER}/desk-scenes/999999/test", None),
    ):
        refused = await client.post(path, json=body)
        assert refused.status_code == 404, (path, refused.text)
        assert error_of(refused)["code"] == "not_found", path
    assert sets(desk.midi) == []


async def test_configuration_answers_conflict_in_use_and_main_immutable(desk: Desk) -> None:
    """§16.1's optimistic concurrency and reference guards, on the mixer's
    configuration (phase-4-contracts.md *Configuration*)."""
    client, room = desk.client, desk.room
    lectern = desk.channel("lectern")

    for path, rename in (
        (f"{MIXER}/channels/{lectern}", "Lectern mic"),
        (f"{MIXER}/desk-scenes/{room.desk_scenes['lecture']}", "Lecture (spring)"),
    ):
        loaded = ok(await client.get(path))
        ok(
            await client.put(
                path,
                json={"name": rename},
                headers={"If-Unmodified-Since-Version": loaded["updated_at"]},
            )
        )
        stale = await client.put(
            path,
            json={"name": "Someone else's edit"},
            headers={"If-Unmodified-Since-Version": loaded["updated_at"]},
        )
        assert stale.status_code == 409, (path, stale.text)
        error = error_of(stale)
        assert error["code"] == "conflict"
        assert error["detail"]["current"]["name"] == rename

    main = ok(await client.get(f"{MIXER}/channels/{room.main}"))
    for response in (
        await client.put(
            f"{MIXER}/channels/{room.main}",
            json={"channel_kind": "input"},
            headers={"If-Unmodified-Since-Version": main["updated_at"]},
        ),
        await client.delete(f"{MIXER}/channels/{room.main}"),
    ):
        assert response.status_code == 422, response.text
        error = error_of(response)
        assert (error["code"], error["detail"]["reason"]) == ("validation_failed", "main_immutable")

    scene = ok(await client.post(SCENES, json={"name": "Lecture"}), 201)
    await add_action(
        client,
        scene["id"],
        {"domain": "mixer_recall", "delay_ms": 0, "mixer_scene_id": room.desk_scenes["lecture"]},
    )
    await add_action(
        client,
        scene["id"],
        {"domain": "mixer_mute", "delay_ms": 0, "mixer_channel_id": lectern, "mixer_muted": True},
    )
    for path in (
        f"{MIXER}/channels/{lectern}",
        f"{MIXER}/desk-scenes/{room.desk_scenes['lecture']}",
    ):
        refused = await client.delete(path)
        assert refused.status_code == 409, (path, refused.text)
        error = error_of(refused)
        assert error["code"] == "in_use"
        assert [r["name"] for r in error["detail"]["references"]] == ["Lecture"], error
