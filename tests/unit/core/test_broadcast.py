"""The WebSocket broadcaster: batching, the two event classes, tiers and resync.

Spec §16.8 (the wire contract), §5.6 (continuous versus discrete and the
bounded queues), §6.12 (per-tier filtering), §10.7 (resync) and §22.2, which
names backgrounding and the meter rules as tests in their own right.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from types import MappingProxyType

import pytest

from proskenion.config import Config
from proskenion.core.broadcast import (
    BROADCAST_DOMAINS,
    Broadcaster,
    Connection,
    Message,
    banner_message,
    default_visibility,
    device_status_message,
    external_control_message,
    hdmi_source_message,
    progress_message,
    projector_state_message,
    scene_completed_message,
    scene_started_message,
    surface_bank_message,
    surface_touch_message,
    timer_message,
)
from proskenion.core.bus import EventBus
from proskenion.core.hirer_permissions import HirerPermissions
from proskenion.core.state import StateStore

STARTED_AT = "2026-09-10T19:42:11.400+12:00"


# -- fixtures and helpers ------------------------------------------------------------


@pytest.fixture
async def bus() -> AsyncIterator[EventBus]:
    event_bus = EventBus()
    await event_bus.start()
    try:
        yield event_bus
    finally:
        await event_bus.stop()


@pytest.fixture
def state(dev_config: Config, bus: EventBus) -> StateStore:
    return StateStore(dev_config, bus)


@pytest.fixture
async def broadcaster(state: StateStore, bus: EventBus) -> AsyncIterator[Broadcaster]:
    instance = Broadcaster(state, bus)
    await instance.start()
    try:
        yield instance
    finally:
        await instance.stop()


async def take(connection: Connection, count: int, within: float = 1.0) -> list[Message]:
    """The next ``count`` messages queued for ``connection``."""
    messages: list[Message] = []
    async with asyncio.timeout(within):
        async for message in connection.messages():
            messages.append(message)
            if len(messages) >= count:
                break
    return messages


async def take_all(connection: Connection, within: float = 0.05) -> list[Message]:
    """Everything queued for ``connection`` once the loop has settled."""
    messages: list[Message] = []
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(within):
            async for message in connection.messages():
                messages.append(message)
    return messages


async def expect_silence(connection: Connection, within: float = 0.05) -> None:
    assert await take_all(connection, within) == []


# -- batched continuous state (§16.8) ------------------------------------------------


async def test_twenty_changes_in_one_tick_are_one_frame_with_the_latest_values(
    state: StateStore, broadcaster: Broadcaster
) -> None:
    """One message per domain carrying only what changed — never per channel."""
    connection = broadcaster.connect(tier="operator", domains=["lighting"])
    levels = state.lighting.writer("fade_engine")
    for step in range(1, 21):
        levels.set_item("levels", 1 + step % 2, float(step))

    assert broadcaster.tick() == 1
    frames = await take_all(connection)
    assert len(frames) == 1
    assert frames[0]["type"] == "lighting_state"
    # The newest value is the truth; the nineteen before it are worthless.
    assert frames[0]["channels"] == {"1": {"level": 20.0}, "2": {"level": 19.0}}
    assert frames[0]["source"] == "fade"


async def test_a_clean_tick_sends_nothing(state: StateStore, broadcaster: Broadcaster) -> None:
    connection = broadcaster.connect(tier="operator", domains=["lighting", "mixer"])
    assert broadcaster.tick() == 0
    await expect_silence(connection)

    state.lighting.writer("fade_engine").set_item("levels", 3, 40.0)
    assert broadcaster.tick() == 1
    # Draining the dirty set is what makes the next tick free.
    assert broadcaster.tick() == 0
    assert len(await take_all(connection)) == 1


async def test_a_partial_frame_carries_only_the_sections_that_changed(
    state: StateStore, broadcaster: Broadcaster
) -> None:
    connection = broadcaster.connect(tier="operator", domains=["lighting"])
    lighting = state.lighting.writer("fade_engine")
    lighting.set_item("levels", 7, 82.5)
    lighting.set_item("colour", 7, {"r": 255, "g": 120, "b": 0})
    broadcaster.tick()

    frame = (await take_all(connection))[0]
    assert frame["channels"] == {"7": {"level": 82.5, "r": 255, "g": 120, "b": 0}}
    assert "master" not in frame
    assert "groups" not in frame

    lighting.set("master", 90.0)
    broadcaster.tick()
    frame = (await take_all(connection))[0]
    assert frame["master"] == 90.0
    assert "channels" not in frame


async def test_meters_travel_in_their_own_message(
    state: StateStore, broadcaster: Broadcaster
) -> None:
    """Not inside mixer_state: they change at a different rate and carry no control meaning."""
    connection = broadcaster.connect(tier="operator", domains=["mixer"])
    mixer = state.mixer.writer("mixer_client")
    mixer.set_item("inputs", 1, {"db": -5.0, "muted": False, "origin": "mixpad"})
    mixer.set_item("meters", 3, [-18.2, -17.9])
    broadcaster.tick()

    frames = {frame["type"]: frame for frame in await take_all(connection)}
    assert frames["mixer_state"]["inputs"] == {
        "1": {"db": -5.0, "muted": False, "origin": "mixpad"}
    }
    assert "meters" not in frames["mixer_state"]
    assert frames["mixer_meters"]["channels"] == {"3": [-18.2, -17.9]}
    # §5.5 defines `at` as monotonic, for staleness rather than wall-clock time.
    assert isinstance(frames["mixer_meters"]["at"], float)


async def test_a_metering_loss_reaches_an_open_view_with_no_channels(
    state: StateStore, broadcaster: Broadcaster
) -> None:
    """Before this fix, ``MixerService`` cleared ``state.mixer.meters``
    with a whole-field write, which dirties only ``"meters"`` — never
    ``"meters.<id>"`` — so the per-item check the old ``_meter_frame`` used
    never saw it as dirty and built no frame at all. The service now writes
    the scalar ``metering`` field in the same batch, and the frame is keyed
    on *that* having changed, not on ``channels`` being non-empty."""
    connection = broadcaster.connect(tier="operator", domains=["mixer"])
    mixer = state.mixer.writer("mixer_client")
    mixer.set_item("meters", 3, [-18.2, -17.9])
    broadcaster.tick()
    await take_all(connection)  # the ordinary reading; not under test here

    # A loss, exactly as MixerService._set_metering_reason writes it: the
    # whole meters map replaced (never a per-item delete) together with the
    # availability marker, in one batch.
    mixer.set_many({"meters": {}, "metering": {"available": False, "reason": "refused"}})
    broadcaster.tick()

    frames = {frame["type"]: frame for frame in await take_all(connection)}
    assert frames["mixer_meters"] == {
        "type": "mixer_meters",
        "channels": {},
        "metering": {"available": False, "reason": "refused"},
        "at": frames["mixer_meters"]["at"],
    }
    assert isinstance(frames["mixer_meters"]["at"], float)


async def test_metering_recovery_reaches_an_open_view_and_hides_the_notice(
    state: StateStore, broadcaster: Broadcaster
) -> None:
    connection = broadcaster.connect(tier="operator", domains=["mixer"])
    mixer = state.mixer.writer("mixer_client")
    mixer.set_many({"meters": {}, "metering": {"available": False, "reason": "no_response"}})
    broadcaster.tick()
    await take_all(connection)

    mixer.set("metering", {"available": True, "reason": None})
    broadcaster.tick()

    frame = next(f for f in await take_all(connection) if f["type"] == "mixer_meters")
    assert frame["metering"] == {"available": True, "reason": None}
    assert frame["channels"] == {}


async def test_an_ordinary_meter_update_never_carries_metering(
    state: StateStore, broadcaster: Broadcaster
) -> None:
    """A regular reading, with no availability change, looks exactly as
    before — no ``metering`` key at all."""
    connection = broadcaster.connect(tier="operator", domains=["mixer"])
    state.mixer.writer("mixer_client").set_item("meters", 5, [-8.0])
    broadcaster.tick()

    frame = next(f for f in await take_all(connection) if f["type"] == "mixer_meters")
    assert "metering" not in frame
    assert frame["channels"] == {"5": [-8.0]}


async def test_a_client_connecting_after_meters_settle_still_sees_every_channel(
    state: StateStore, broadcaster: Broadcaster
) -> None:
    """The reported bug: on the real rig, silent inputs never showed a
    meter — only Main LR, ST1 and ST2, whose readings kept changing. Their
    single settled write happens before any view is open to see it; the
    state store drops a write whose value is unchanged (§5.6), so nothing
    dirties that channel again, and a client that opens later would get
    nothing from a tick alone, however long it then stayed connected. The
    resync's fresh ``mixer_meters`` catch-up closes that gap."""
    mixer = state.mixer.writer("mixer_client")
    mixer.set_item("meters", 1, [-9.4, -9.6])  # ST1: signal, still moving
    mixer.set_item("meters", 5, [-60.0])  # a silent input, at the floor
    broadcaster.tick()  # drained; nothing was subscribed to receive it

    # The silent channel's next several "readings" are identical, exactly
    # as a genuinely quiet input's would be, and are dropped as no-ops.
    mixer.set_item("meters", 5, [-60.0])
    assert broadcaster.tick() == 0

    connection = broadcaster.connect(tier="operator", domains=["mixer"])
    snapshot = broadcaster.snapshot(["mixer"], connection=connection)
    meters = next(f for f in snapshot if f["type"] == "mixer_meters")
    assert meters["channels"] == {"1": [-9.4, -9.6], "5": [-60.0]}


async def test_scenes_running_changes_batch_to_one_frame(
    state: StateStore, broadcaster: Broadcaster
) -> None:
    """§21.10 needs a reconnecting tablet to show a card executing without

    waiting for the next scene_started/scene_completed — this frame is what
    makes that possible.
    """
    connection = broadcaster.connect(tier="operator", domains=["scenes"])
    running = state.scenes.writer("scene_engine")
    running.set_item(
        "running",
        3,
        {
            "run_id": 1,
            "priority": "normal",
            "triggered_by": "api:operator",
            "started_at": STARTED_AT,
            "channels": [1, 2],
            "locked": False,
        },
    )
    running.set(
        "last_result",
        {"scene_id": 3, "run_id": 1, "result": "success", "completed_at": STARTED_AT},
    )

    assert broadcaster.tick() == 1
    frame = (await take_all(connection))[0]
    assert frame["type"] == "scenes_state"
    assert frame["running"]["3"]["priority"] == "normal"
    assert frame["last_result"]["result"] == "success"
    assert frame["source"] == "state"


async def test_a_scenes_resync_reports_a_run_already_in_progress(
    state: StateStore, broadcaster: Broadcaster
) -> None:
    """The gap this frame closes: previously a mid-scene reconnect saw nothing at all."""
    state.scenes.writer("scene_engine").set_item(
        "running",
        5,
        {
            "run_id": 2,
            "priority": "critical",
            "triggered_by": "knx:1/0/1",
            "started_at": STARTED_AT,
            "channels": [],
            "locked": True,
        },
    )
    connection = broadcaster.connect(tier="operator", domains=["scenes"])
    frame = broadcaster.snapshot(["scenes"], connection=connection)[0]

    assert frame["type"] == "scenes_state"
    assert frame["running"] == {
        "5": {
            "run_id": 2,
            "priority": "critical",
            "triggered_by": "knx:1/0/1",
            "started_at": STARTED_AT,
            "channels": [],
            "locked": True,
        }
    }
    assert frame["source"] == "resync"


async def test_a_scenes_snapshot_is_present_even_when_nothing_is_running(
    state: StateStore, broadcaster: Broadcaster
) -> None:
    """A resync must still tell a reconnecting client that nothing is running,

    rather than staying silent and leaving it to guess (contrast with
    ``mixer_meters``, which is deliberately never replayed).
    """
    connection = broadcaster.connect(tier="operator", domains=["scenes"])
    assert broadcaster.snapshot(["scenes"], connection=connection) == [
        {"type": "scenes_state", "running": {}, "last_result": None, "source": "resync"}
    ]


# -- discrete events (§5.6) ----------------------------------------------------------


async def test_a_device_status_change_is_delivered_without_waiting_for_the_tick(
    state: StateStore, broadcaster: Broadcaster
) -> None:
    connection = broadcaster.connect(tier="operator", domains=["devices"])
    state.devices.writer("probe").set_status("mixer", "connected")

    assert await take(connection, 1) == [
        {"type": "device_status", "device": "mixer", "status": "connected"}
    ]
    # Nothing was waiting on the tick.
    assert broadcaster.tick() == 0


async def test_a_banner_is_delivered_immediately_and_a_clear_carries_a_null_text(
    state: StateStore, broadcaster: Broadcaster
) -> None:
    connection = broadcaster.connect(tier="operator", domains=["system"])
    system = state.system.writer("health")
    system.set_banner("backup_media_absent", "amber", "Backup media not detected")
    system.clear_banner("backup_media_absent")

    assert await take(connection, 2) == [
        {
            "type": "banner",
            "level": "amber",
            "key": "backup_media_absent",
            "text": "Backup media not detected",
        },
        {"type": "banner", "level": "amber", "key": "backup_media_absent", "text": None},
    ]


async def test_the_timer_travels_as_iso_8601_and_never_as_a_running_count(
    state: StateStore, broadcaster: Broadcaster
) -> None:
    """§4.9 fixes the timestamp format; the client computes elapsed (§21.7)."""
    connection = broadcaster.connect(tier="operator", domains=["timer"])
    state.timer.writer("show_timer").start(STARTED_AT)

    assert await take(connection, 1) == [
        {"type": "timer", "running": True, "started_at": STARTED_AT, "accumulated_ms": 0}
    ]


def test_every_declared_discrete_shape_is_available_to_later_phases() -> None:
    """The §16.8 shapes nothing emits yet, so a later phase adds a producer only."""
    assert scene_started_message(3, "knx:1/0/1") == {
        "type": "scene_started",
        "scene_id": 3,
        "triggered_by": "knx:1/0/1",
    }
    assert scene_completed_message(3, "partial")["result"] == "partial"
    assert external_control_message("detected", "booth_input")["state"] == "detected"
    assert hdmi_source_message(1, 2, False) == {
        "type": "hdmi_source",
        "destination_id": 1,
        "input_id": 2,
        "diverged": False,
    }
    assert projector_state_message("warming", "31") == {
        "type": "projector_state",
        "state": "warming",
        "input_ref": "31",
    }
    assert surface_bank_message(0, "Venue Default")["name"] == "Venue Default"
    assert surface_touch_message(3, True)["touched"] is True
    assert progress_message("cert_issue", 2, 4, "Waiting for DNS propagation")["of"] == 4


# -- backgrounding and resync (§16.8, §10.7, §22.2) ----------------------------------


async def test_a_backgrounded_client_receives_no_continuous_frames_but_still_gets_events(
    state: StateStore, broadcaster: Broadcaster
) -> None:
    connection = broadcaster.connect(
        tier="operator", domains=["lighting", "mixer", "devices", "timer"]
    )
    connection.background()

    state.lighting.writer("fade_engine").set_item("levels", 4, 12.0)
    state.mixer.writer("mixer_client").set_item("meters", 1, [-40.0])
    broadcaster.tick()
    await expect_silence(connection)

    state.devices.writer("probe").set_status("knx", "error", kind="device")
    assert await take(connection, 1) == [
        {"type": "device_status", "device": "knx", "status": "error"}
    ]


async def test_resync_returns_a_full_snapshot_and_a_fresh_meter_catch_up(
    state: StateStore, broadcaster: Broadcaster
) -> None:
    """A resync's per-domain snapshot never replays an old queued frame, but
    a ``mixer_meters`` catch-up is still built fresh from live state and
    sent alongside it (see ``_meter_snapshot_frame``) — otherwise a channel
    whose reading has been constant since before this connection existed
    (a silent input) would never reach it at all."""
    lighting = state.lighting.writer("fade_engine")
    lighting.set_item("levels", 1, 85.0)
    lighting.set_item("levels", 2, 78.5)
    mixer = state.mixer.writer("mixer_client")
    mixer.set_item("meters", 1, [-12.4])
    mixer.set("metering", {"available": False, "reason": "no_response"})
    state.devices.writer("probe").set_status("projector", "connected")
    state.timer.writer("show_timer").start(STARTED_AT)
    state.projector.writer("projector_service").set_many({"state": "on", "input_ref": "31"})

    connection = broadcaster.connect(tier="operator", domains=BROADCAST_DOMAINS)
    connection.background()
    snapshot = broadcaster.snapshot(sorted(BROADCAST_DOMAINS), connection=connection)
    by_type = {message["type"]: message for message in snapshot}

    # Every tracked value, not only what changed, and marked as a resync.
    assert by_type["lighting_state"]["channels"] == {"1": {"level": 85.0}, "2": {"level": 78.5}}
    assert "groups" not in by_type["lighting_state"]  # a group has no value of its own
    assert by_type["lighting_state"]["master"] == 100.0
    assert by_type["lighting_state"]["source"] == "resync"
    assert by_type["mixer_state"]["source"] == "resync"
    assert by_type["device_status"]["device"] == "projector"
    assert by_type["timer"]["started_at"] == STARTED_AT
    assert by_type["projector_state"] == {
        "type": "projector_state",
        "state": "on",
        "input_ref": "31",
    }
    # Fresh, not stale — every channel's current reading, plus the current
    # availability, read live at the moment of the resync.
    assert by_type["mixer_meters"]["channels"] == {"1": [-12.4]}
    assert by_type["mixer_meters"]["metering"] == {"available": False, "reason": "no_response"}
    assert isinstance(by_type["mixer_meters"]["at"], float)
    # mixer_state itself still never carries them.
    assert "meters" not in by_type["mixer_state"]


async def test_a_snapshot_is_the_shape_of_a_batched_frame(
    state: StateStore, broadcaster: Broadcaster
) -> None:
    state.lighting.writer("fade_engine").set_item("levels", 1, 85.0)
    connection = broadcaster.connect(tier="operator", domains=["lighting"])
    broadcaster.tick()
    live = (await take_all(connection))[0]
    snapshot = broadcaster.snapshot(["lighting"], connection=connection)[0]

    assert set(snapshot) >= set(live)
    assert snapshot["channels"] == live["channels"]


# -- per-tier filtering (§6.12) ------------------------------------------------------


async def test_a_hirer_never_receives_a_domain_its_tier_may_not_see(
    state: StateStore, broadcaster: Broadcaster
) -> None:
    hirer = broadcaster.connect(
        tier="hirer", session_id="s1", domains=["lighting", "mixer", "timer", "devices", "system"]
    )
    # The subscription itself is narrowed, so the health screen tells the truth.
    assert hirer.domains == frozenset({"lighting", "mixer", "devices"})

    # Nothing is reachable in the fail-safe snapshot, so no level reaches them.
    state.lighting.writer("fade_engine").set_item("levels", 1, 100.0)
    state.mixer.writer("mixer_client").set_item("meters", 1, [-3.0])
    broadcaster.tick()
    state.timer.writer("show_timer").start(STARTED_AT)
    state.system.writer("health").set_banner("disk", "amber", "Disk nearly full")
    await expect_silence(hirer)

    state.devices.writer("probe").set_status("dmx", "connected")
    assert await take(hirer, 1) == [
        {"type": "device_status", "device": "dmx", "status": "connected"}
    ]
    assert broadcaster.snapshot(["timer", "system"], connection=hirer) == []


async def test_the_frame_filter_narrows_what_one_connection_is_sent(
    state: StateStore, dev_config: Config, bus: EventBus
) -> None:
    """The filter hook is per connection; staff frames are untouched."""

    def only_channel_two(connection: Connection, domain: str, message: Message) -> Message | None:
        if connection.tier != "hirer" or message.get("type") != "lighting_state":
            return message
        channels = {k: v for k, v in message.get("channels", {}).items() if k == "2"}
        return {**message, "channels": channels}

    def everything(connection: Connection, domain: str) -> bool:
        return True

    store = StateStore(dev_config, bus)
    broadcaster = Broadcaster(store, bus, visibility=everything, restrict=only_channel_two)
    await broadcaster.start()
    try:
        hirer = broadcaster.connect(tier="hirer", session_id="s1", domains=["lighting"])
        staff = broadcaster.connect(tier="operator", domains=["lighting"])
        levels = store.lighting.writer("fade_engine")
        levels.set_item("levels", 1, 10.0)
        levels.set_item("levels", 2, 20.0)
        broadcaster.tick()

        assert (await take_all(hirer))[0]["channels"] == {"2": {"level": 20.0}}
        assert (await take_all(staff))[0]["channels"] == {
            "1": {"level": 10.0},
            "2": {"level": 20.0},
        }
    finally:
        await broadcaster.stop()


def test_the_default_hooks_by_tier() -> None:
    hirer = Connection(1, tier="hirer", session_id="s1")
    operator = Connection(2, tier="operator")
    stranger = Connection(3, tier="guest")
    assert {d for d in BROADCAST_DOMAINS if default_visibility(hirer, d)} == {
        "mixer",
        "lighting",
        "devices",
        "status",
    }
    assert all(default_visibility(operator, d) for d in BROADCAST_DOMAINS)
    assert not any(default_visibility(stranger, d) for d in BROADCAST_DOMAINS)


def test_writing_is_checked_per_target_against_the_live_snapshot(
    state: StateStore, bus: EventBus
) -> None:
    """Seeing a domain is not writing it: a hirer writes exactly the targets
    ``state.hirer`` lets them reach, and the lighting master never."""
    broadcaster = Broadcaster(state, bus)
    hirer = Connection(1, tier="hirer", session_id="s1")
    operator = Connection(2, tier="operator")
    stranger = Connection(3, tier="guest")
    assert not broadcaster.may_write(hirer, "mixer", 5)  # nothing reachable yet
    assert broadcaster.may_write(operator, "mixer", 5)
    assert broadcaster.may_write(operator, "master", None)
    assert not broadcaster.may_write(stranger, "mixer", 5)

    state.register_owner("hirer", "test")
    state.hirer.writer("test").set_permissions(
        HirerPermissions(
            mixer_ceilings=MappingProxyType({5: -3.0}),
            lighting_enabled=True,
            lighting_channels=frozenset({7, 8}),
            writable_lighting_channels=frozenset({7}),
            groups=frozenset({2}),
        )
    )
    assert broadcaster.may_write(hirer, "mixer", 5)
    assert not broadcaster.may_write(hirer, "mixer", 6)
    assert broadcaster.may_write(hirer, "lighting", 7)
    assert not broadcaster.may_write(hirer, "lighting", 8)  # a tray member, read-only
    assert broadcaster.may_write(hirer, "lighting_group", 2)
    assert not broadcaster.may_write(hirer, "lighting_group", 3)
    # A group's BUMP is reachable exactly as its fader is (owner decision 2026-10-01).
    assert broadcaster.may_write(hirer, "lighting_bump", 2)
    assert not broadcaster.may_write(hirer, "lighting_bump", 3)
    assert broadcaster.may_write(operator, "lighting_bump", 3)
    assert not broadcaster.may_write(hirer, "master", None)
    assert not broadcaster.may_write(hirer, "nonsense", 5)


# -- bounded queues (§5.6) -----------------------------------------------------------


async def test_a_full_queue_drops_continuous_frames_and_counts_them(
    state: StateStore, bus: EventBus, dev_config: Config
) -> None:
    store = StateStore(dev_config, bus)
    broadcaster = Broadcaster(store, bus, queue_size=4)
    await broadcaster.start()
    try:
        connection = broadcaster.connect(tier="operator", domains=["lighting"])
        levels = store.lighting.writer("fade_engine")
        for step in range(10):
            levels.set_item("levels", 1, float(step))
            broadcaster.tick()

        assert connection.queued == 4
        assert connection.drops == 6
        assert connection.health().drops == 6
        # Drop *oldest*: what survives is the newest, which is the truth.
        frames = await take_all(connection)
        assert [frame["channels"]["1"]["level"] for frame in frames] == [6.0, 7.0, 8.0, 9.0]
        assert not connection.closed
    finally:
        await broadcaster.stop()


async def test_a_queue_full_of_discrete_messages_closes_the_connection(
    state: StateStore, bus: EventBus, dev_config: Config
) -> None:
    """A discrete event is never dropped; a client that cannot keep up is broken."""
    store = StateStore(dev_config, bus)
    broadcaster = Broadcaster(store, bus, queue_size=2)
    await broadcaster.start()
    try:
        connection = broadcaster.connect(tier="operator", domains=["devices"])
        for index in range(3):
            broadcaster.publish(device_status_message(f"device{index}", "connected"))

        assert connection.closed
        assert connection.discrete_overflows == 1
        assert connection.drops == 0
        assert connection.close_code == 1008
    finally:
        await broadcaster.stop()


async def test_a_discrete_message_evicts_a_continuous_one_rather_than_closing(
    state: StateStore, bus: EventBus, dev_config: Config
) -> None:
    store = StateStore(dev_config, bus)
    broadcaster = Broadcaster(store, bus, queue_size=2)
    await broadcaster.start()
    try:
        connection = broadcaster.connect(tier="operator", domains=["lighting", "devices"])
        levels = store.lighting.writer("fade_engine")
        for step in range(2):
            levels.set_item("levels", 1, float(step))
            broadcaster.tick()
        assert connection.queued == 2

        broadcaster.publish(device_status_message("knx", "connected"))
        assert not connection.closed
        assert connection.drops == 1
        assert [message["type"] for message in await take_all(connection)] == [
            "lighting_state",
            "device_status",
        ]
    finally:
        await broadcaster.stop()


# -- connections and health ----------------------------------------------------------


async def test_connection_count_and_health_report_each_connection(
    broadcaster: Broadcaster,
) -> None:
    assert broadcaster.connection_count == 0
    first = broadcaster.connect(tier="operator", domains=["devices"])
    second = broadcaster.connect(tier="hirer", session_id="s1", domains=["devices"])
    assert broadcaster.connection_count == 2

    rows = {row.id: row for row in broadcaster.health()}
    assert rows[first.id].tier == "operator"
    assert rows[first.id].domains == ("devices",)
    assert rows[second.id].backgrounded is False

    broadcaster.disconnect(first)
    broadcaster.disconnect(first)  # idempotent
    assert broadcaster.connection_count == 1
    assert first.closed


async def test_an_unknown_domain_is_refused_rather_than_subscribed(
    broadcaster: Broadcaster,
) -> None:
    connection = broadcaster.connect(tier="operator", domains=["devices", "nonsense", 7])  # type: ignore[list-item]
    assert connection.domains == frozenset({"devices"})


async def test_stop_closes_every_connection(state: StateStore, bus: EventBus) -> None:
    broadcaster = Broadcaster(state, bus)
    await broadcaster.start()
    connection = broadcaster.connect(tier="operator", domains=["devices"])
    await broadcaster.stop()

    assert connection.closed
    assert broadcaster.connection_count == 0
    # The bus subscriptions are gone with it, so a later event reaches nobody.
    assert [s for s in bus.subscriptions() if s.name.startswith("broadcast.")] == []


async def test_publish_refuses_a_message_with_no_domain(broadcaster: Broadcaster) -> None:
    with pytest.raises(ValueError, match="no state domain"):
        broadcaster.publish({"type": "invented"})


async def test_messages_stop_once_the_connection_is_closed_and_drained(
    broadcaster: Broadcaster,
) -> None:
    connection = broadcaster.connect(tier="operator", domains=["devices"])
    connection.send(device_status_message("knx", "connected"))
    connection.close("shutting down")

    assert [message["type"] async for message in connection.messages()] == ["device_status"]
    assert connection.send(banner_message("info", "k", "t")) is False


def test_a_timer_message_never_carries_elapsed() -> None:
    assert set(timer_message(True, STARTED_AT, 0)) == {
        "type",
        "running",
        "started_at",
        "accumulated_ms",
    }
