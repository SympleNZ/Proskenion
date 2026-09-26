"""§21.26's device-offline and no-Venue-Default banners, from state changes.

The device statuses here are written the way the device manager writes them
— through the ``devices`` domain's one writer — and the banners are read back
out of ``state.system``, where the broadcaster finds them. What is being
proved is the table's condition and its clearing rule: red is offline, amber
is not, a retry does not end an outage, and exactly one of the two device
banners is up at a time.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

import pytest

from proskenion.config import Config
from proskenion.core.banners import (
    DEVICE_OFFLINE_KEY,
    DEVICES_OFFLINE_KEY,
    VENUE_DEFAULT_KEY,
    VENUE_DEFAULT_TEXT,
    DeviceOfflineBanner,
    VenueDefaultBanner,
    device_offline_text,
)
from proskenion.core.bus import EventBus
from proskenion.core.devices import OWNER
from proskenion.core.events import DeviceStatus, MixerConfigChanged
from proskenion.core.state import Banner, DevicesWriter, StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import mixer as mixer_crud


async def eventually(condition: Callable[[], bool]) -> None:
    """Wait for the bus to deliver: each subscriber drains its own queue."""
    for _ in range(400):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("the condition never became true")


async def settle() -> None:
    """Give every queued delivery a chance to run, for asserting an absence."""
    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.sleep(0.02)


@pytest.fixture
async def bus() -> AsyncIterator[EventBus]:
    events = EventBus()
    await events.start()
    try:
        yield events
    finally:
        await events.stop()


@pytest.fixture
def state(dev_config: Config, bus: EventBus) -> StateStore:
    return StateStore(dev_config, bus)


@pytest.fixture
def devices(state: StateStore) -> DevicesWriter:
    state.register_owner("devices", OWNER)
    return state.devices.writer(OWNER)


def banners(state: StateStore) -> dict[str, Banner]:
    return state.system.banners()


# -- device offline -----------------------------------------------------------------


@pytest.fixture
async def offline(state: StateStore, bus: EventBus) -> AsyncIterator[DeviceOfflineBanner]:
    names = {"projector:4": "Foyer projector", "projector:5": "Hall projector"}
    monitor = DeviceOfflineBanner(state, bus, name_of=names.get)
    await monitor.start()
    try:
        yield monitor
    finally:
        await monitor.stop()


async def test_a_red_device_raises_the_amber_banner_in_the_spec_s_words(
    state: StateStore, devices: DevicesWriter, offline: DeviceOfflineBanner
) -> None:
    devices.set_status("mixer", "error", kind="config", detail="cannot connect")
    await eventually(lambda: DEVICE_OFFLINE_KEY in banners(state))
    assert banners(state)[DEVICE_OFFLINE_KEY] == Banner(
        "amber", "Mixer offline — audio controls unavailable"
    )
    assert DEVICES_OFFLINE_KEY not in banners(state)


@pytest.mark.parametrize("status", ["degraded", "unconfigured", "connecting", "connected"])
async def test_amber_grey_and_green_are_not_offline(
    state: StateStore,
    devices: DevicesWriter,
    offline: DeviceOfflineBanner,
    status: DeviceStatus,
) -> None:
    """Amber is still connected or deliberately held (a projector held by
    another controller, the PJLink hold); grey is turned off or not set up."""
    devices.set_status("projector", status, kind="device" if status == "degraded" else None)
    await settle()
    assert DEVICE_OFFLINE_KEY not in banners(state)
    assert DEVICES_OFFLINE_KEY not in banners(state)


async def test_a_retry_does_not_end_the_outage_and_a_recovery_does(
    state: StateStore, devices: DevicesWriter, offline: DeviceOfflineBanner
) -> None:
    devices.set_status("dmx", "error", kind="config", detail="no route to host")
    await eventually(lambda: DEVICE_OFFLINE_KEY in banners(state))

    devices.set_status("dmx", "connecting")
    await settle()
    assert DEVICE_OFFLINE_KEY in banners(state)  # still unreachable, only retrying

    devices.set_status("dmx", "connected")
    await eventually(lambda: DEVICE_OFFLINE_KEY not in banners(state))


@pytest.mark.parametrize("recovered", ["degraded", "unconfigured"])
async def test_amber_or_turned_off_ends_the_outage(
    state: StateStore,
    devices: DevicesWriter,
    offline: DeviceOfflineBanner,
    recovered: DeviceStatus,
) -> None:
    devices.set_status("hdmi", "error", kind="device", detail="no reply")
    await eventually(lambda: DEVICE_OFFLINE_KEY in banners(state))
    devices.set_status("hdmi", recovered)
    await eventually(lambda: DEVICE_OFFLINE_KEY not in banners(state))


async def test_two_devices_offline_is_one_banner_with_the_count(
    state: StateStore, devices: DevicesWriter, offline: DeviceOfflineBanner
) -> None:
    devices.set_status("mixer", "error", kind="config")
    await eventually(lambda: DEVICE_OFFLINE_KEY in banners(state))
    devices.set_status("knx", "error", kind="config")
    await eventually(lambda: DEVICES_OFFLINE_KEY in banners(state))
    assert banners(state)[DEVICES_OFFLINE_KEY] == Banner(
        "amber", "2 devices offline — tap for details"
    )
    assert DEVICE_OFFLINE_KEY not in banners(state)

    devices.set_status("mixer", "connected")
    await eventually(lambda: DEVICES_OFFLINE_KEY not in banners(state))
    assert banners(state)[DEVICE_OFFLINE_KEY] == Banner(
        "amber", "KNX offline — house lighting controls unavailable"
    )

    devices.set_status("knx", "connected")
    await eventually(lambda: not banners(state))


async def test_a_removed_device_is_no_longer_offline(
    state: StateStore, devices: DevicesWriter, offline: DeviceOfflineBanner
) -> None:
    devices.set_status("projector", "error", kind="config")
    await eventually(lambda: DEVICE_OFFLINE_KEY in banners(state))
    devices.remove("projector")
    await eventually(lambda: DEVICE_OFFLINE_KEY not in banners(state))


async def test_one_of_several_devices_of_a_category_is_named(
    state: StateStore, devices: DevicesWriter, offline: DeviceOfflineBanner
) -> None:
    devices.set_status("projector:4", "connected")
    devices.set_status("projector:5", "error", kind="config")
    await eventually(lambda: DEVICE_OFFLINE_KEY in banners(state))
    assert banners(state)[DEVICE_OFFLINE_KEY].text == (
        "Hall projector offline — projector controls unavailable"
    )


async def test_a_device_already_red_when_the_monitor_starts_is_offline(
    state: StateStore, bus: EventBus, devices: DevicesWriter
) -> None:
    devices.set_status("mixer", "error", kind="config")
    monitor = DeviceOfflineBanner(state, bus)
    await monitor.start()
    try:
        assert banners(state)[DEVICE_OFFLINE_KEY].text == device_offline_text("mixer")
    finally:
        await monitor.stop()


# -- no Venue Default desk scene ----------------------------------------------------


@pytest.fixture
async def venue_default(
    db: Database, state: StateStore, bus: EventBus
) -> AsyncIterator[VenueDefaultBanner]:
    monitor = VenueDefaultBanner(db, state, bus)
    await monitor.start()
    try:
        yield monitor
    finally:
        await monitor.stop()


async def _mixer(db: Database, *, enabled: bool = True) -> devices_crud.Device:
    return await devices_crud.create(
        db,
        category="mixer",
        driver_key="stub",
        name="Mixer",
        config={"transport": {"type": "loopback"}, "driver": {}},
        enabled=enabled,
    )


async def test_no_mixer_means_no_venue_default_banner(
    state: StateStore, venue_default: VenueDefaultBanner
) -> None:
    assert VENUE_DEFAULT_KEY not in banners(state)


async def test_a_mixer_with_no_venue_default_raises_it_and_designating_one_clears_it(
    db: Database,
    state: StateStore,
    bus: EventBus,
    devices: DevicesWriter,
    venue_default: VenueDefaultBanner,
) -> None:
    mixer = await _mixer(db)
    devices.set_status("mixer", "connecting")  # the device manager starting it
    await eventually(lambda: VENUE_DEFAULT_KEY in banners(state))
    assert banners(state)[VENUE_DEFAULT_KEY] == Banner("amber", VENUE_DEFAULT_TEXT)

    scene = await mixer_crud.create_desk_scene(
        db, device_id=mixer.id, scene_ref="1", name="Venue Default", is_venue_default=True
    )
    bus.emit(MixerConfigChanged(reason="desk_scene_created"))
    await eventually(lambda: VENUE_DEFAULT_KEY not in banners(state))

    await mixer_crud.update_desk_scene(
        db, scene.id, scene.updated_at, is_venue_default=False
    )
    bus.emit(MixerConfigChanged(reason="desk_scene_updated"))
    await eventually(lambda: VENUE_DEFAULT_KEY in banners(state))


async def test_a_turned_off_mixer_needs_no_venue_default(
    db: Database, state: StateStore, bus: EventBus, venue_default: VenueDefaultBanner
) -> None:
    await _mixer(db, enabled=False)
    bus.emit(MixerConfigChanged(reason="main_channel_created"))
    await settle()
    assert VENUE_DEFAULT_KEY not in banners(state)


async def test_removing_the_mixer_clears_it(
    db: Database,
    state: StateStore,
    devices: DevicesWriter,
    venue_default: VenueDefaultBanner,
) -> None:
    mixer = await _mixer(db)
    devices.set_status("mixer", "connecting")
    await eventually(lambda: VENUE_DEFAULT_KEY in banners(state))
    await devices_crud.delete(db, mixer.id)
    devices.remove("mixer")
    await eventually(lambda: VENUE_DEFAULT_KEY not in banners(state))
