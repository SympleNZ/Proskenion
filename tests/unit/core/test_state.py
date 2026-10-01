"""State store (spec §5.6, §22.2): ownership, dirty set, change events, domains."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pytest

from proskenion.config import Config
from proskenion.core.bus import EventBus
from proskenion.core.events import (
    DeviceStatusChanged,
    Event,
    StateDirty,
    SystemBannerChanged,
    TimerChanged,
)
from proskenion.core.state import (
    Banner,
    DeviceStatusRecord,
    FieldSpec,
    LightingDomain,
    MixerDomain,
    OwnershipError,
    StateStore,
    is_json_value,
)


class Sink:
    """Collects every event of the given types, synchronously enough for these tests."""

    def __init__(self, bus: EventBus, *types: type[Event]) -> None:
        self.events: list[Event] = []
        for t in types:
            bus.subscribe(t, self._on, name=f"sink:{t.TYPE}")

    async def _on(self, event: Event) -> None:
        self.events.append(event)

    def of(self, kind: type[Event]) -> list[Event]:
        return [e for e in self.events if isinstance(e, kind)]


async def drain(bus: EventBus) -> None:
    """Deliver everything queued: start the bus, let consumers run, stop it."""
    await bus.start()
    for _ in range(20):
        await __import__("asyncio").sleep(0)


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def dev(dev_config: Config, bus: EventBus) -> StateStore:
    return StateStore(dev_config, bus)


@pytest.fixture
def prod(prod_config: Config, bus: EventBus) -> StateStore:
    return StateStore(prod_config, bus)


# -- ownership (B39) ---------------------------------------------------------


def test_unregistered_writer_raises_under_development(dev: StateStore) -> None:
    dev.register_owner("timer", "timer_service")
    with pytest.raises(OwnershipError) as excinfo:
        dev.timer.writer("stray")
    assert excinfo.value.domain == "timer"
    assert excinfo.value.owner == "stray"
    assert dev.timer.running is False


def test_unregistered_write_raises_at_write_time_too(dev: StateStore) -> None:
    handle = dev.timer.writer("timer_service")  # first-come: claims the domain
    dev.register_owner("timer", "timer_service")  # idempotent
    stray = handle.__class__(dev.timer, "stray")  # a handle forged past writer()
    with pytest.raises(OwnershipError):
        stray.start()
    assert dev.timer.running is False  # the write did not land


def test_unregistered_writer_logs_and_lands_under_production(
    prod: StateStore, caplog: pytest.LogCaptureFixture
) -> None:
    prod.register_owner("timer", "timer_service")
    with caplog.at_level(logging.ERROR, logger="proskenion.core.state"):
        stray = prod.timer.writer("stray")
        assert stray.start("2026-09-04T14:30:00+12:00") is True
    assert prod.timer.running is True  # let through and recorded, not crashed
    records = [r for r in caplog.records if r.getMessage() == "state write from unregistered owner"]
    assert len(records) == 2  # once for the handle, once for the write
    assert records[0].owner == "stray"  # type: ignore[attr-defined]
    assert records[0].domain == "timer"  # type: ignore[attr-defined]


def test_first_come_claims_an_unowned_domain(dev: StateStore) -> None:
    assert dev.owners("system") == frozenset()
    dev.system.writer("health_monitor")
    assert dev.owners("system") == frozenset({"health_monitor"})
    with pytest.raises(OwnershipError):
        dev.system.writer("someone_else")


def test_register_owner_rejects_a_second_owner_unless_shared(dev: StateStore) -> None:
    dev.register_owner("devices", "device_manager")
    with pytest.raises(ValueError):
        dev.register_owner("devices", "another")
    dev.register_owner("lighting", "fade_engine", allow_multiple=True)
    dev.register_owner("lighting", "scene_engine", allow_multiple=True)
    dev.register_owner("lighting", "knx_status_sync", allow_multiple=True)
    with pytest.raises(ValueError):
        dev.register_owner("lighting", "unshared")  # must say so on every registration
    assert dev.owners("lighting") == frozenset({"fade_engine", "scene_engine", "knx_status_sync"})
    for owner in dev.owners("lighting"):
        dev.lighting.writer(owner)
    with pytest.raises(OwnershipError):
        dev.lighting.writer("art_net_output")


def test_unknown_domain_is_a_programming_error(dev: StateStore) -> None:
    with pytest.raises(KeyError):
        dev.register_owner("nope", "x")
    with pytest.raises(KeyError):
        dev.timer.get("elapsed")  # elapsed is computed, never stored


# -- dirty set and change events ---------------------------------------------


async def test_noop_write_does_not_dirty_or_emit(dev: StateStore, bus: EventBus) -> None:
    sink = Sink(bus, StateDirty, TimerChanged)
    timer = dev.timer.writer("timer_service")
    assert timer.reset() is False  # already stopped and zero
    assert timer.set("accumulated_ms", 0) is False
    assert dev.take_dirty() == {}
    await drain(bus)
    assert sink.events == []


async def test_changing_write_dirties_emits_and_take_dirty_clears(
    dev: StateStore, bus: EventBus
) -> None:
    sink = Sink(bus, StateDirty, TimerChanged)
    timer = dev.timer.writer("timer_service")
    assert timer.start("2026-09-04T14:30:00+12:00") is True
    assert dev.take_dirty() == {"timer": {"running", "started_at"}}
    assert dev.take_dirty() == {}
    await drain(bus)
    assert sink.of(StateDirty) == [StateDirty("timer", frozenset({"running", "started_at"}))]
    assert sink.of(TimerChanged) == [TimerChanged(True, "2026-09-04T14:30:00+12:00", 0)]


async def test_state_dirty_is_emitted_once_per_key_until_drained(
    dev: StateStore, bus: EventBus
) -> None:
    sink = Sink(bus, StateDirty)
    dev.register_owner("lighting", "fade_engine", allow_multiple=True)
    levels = dev.lighting.writer("fade_engine")
    for n in range(40):  # a fader wiggling between frames
        levels.set_item("levels", 7, float(n))
    levels.set_item("levels", 8, 10.0)
    assert dev.take_dirty() == {"lighting": {"levels.7", "levels.8"}}
    levels.set_item("levels", 7, 99.0)  # dirty again after the drain
    await drain(bus)
    assert sink.of(StateDirty) == [
        StateDirty("lighting", frozenset({"levels.7"})),
        StateDirty("lighting", frozenset({"levels.8"})),
        StateDirty("lighting", frozenset({"levels.7"})),
    ]


def test_whole_map_set_is_diffed_item_by_item(dev: StateStore) -> None:
    levels = dev.lighting.writer("fade_engine")
    levels.set("levels", {1: 10.0, 2: 20.0, "3": 30.0})
    assert dev.take_dirty() == {"lighting": {"levels.1", "levels.2", "levels.3"}}
    assert levels.set("levels", {"1": 10.0, "2": 25.0}) is True  # 2 changed, 3 removed
    assert dev.take_dirty() == {"lighting": {"levels.2", "levels.3"}}
    assert dev.lighting.get("levels") == {"1": 10.0, "2": 25.0}
    assert levels.delete_item("levels", 3) is False
    assert levels.delete_item("levels", 1) is True
    assert dev.lighting.get_item("levels", 1) is None
    with pytest.raises(TypeError):
        levels.set("levels", 5)
    with pytest.raises(ValueError):
        levels.set_item("master", "x", 1)


def test_values_must_be_plain_json(dev: StateStore) -> None:
    writer = dev.system.writer("health_monitor")
    with pytest.raises(TypeError):
        writer.set("cert_expiry", datetime(2026, 9, 4))
    with pytest.raises(TypeError):
        writer.set("disk_free", float("nan"))
    assert dev.take_dirty() == {}
    assert is_json_value({"a": [1, 2.5, "x", None, True]}) is True
    assert is_json_value({1: "int key"}) is False


def test_reads_return_copies(dev: StateStore) -> None:
    writer = dev.lighting.writer("fade_engine")
    writer.set_item("levels", 1, 50.0)
    levels = dev.lighting.get("levels")
    assert isinstance(levels, dict)
    levels["1"] = 0.0
    assert dev.lighting.get_item("levels", 1) == 50.0
    snapshot = dev.snapshot()
    assert snapshot["lighting"]["levels"] == {"1": 50.0}
    assert snapshot["lighting"]["master"] == 100.0
    assert set(snapshot) == {
        "devices",
        "system",
        "timer",
        "lighting",
        "mixer",
        "projector",
        "hdmi",
        "scenes",
        "hirer",
        "status",
    }


# -- devices -----------------------------------------------------------------


async def test_device_status_change_emits_discrete_event(dev: StateStore, bus: EventBus) -> None:
    sink = Sink(bus, DeviceStatusChanged, StateDirty)
    devices = dev.devices.writer("device_manager")
    assert devices.set_status("mixer", "connecting") is True
    assert devices.set_status("mixer", "connecting") is False
    assert devices.set_status("mixer", "error", kind="device", detail="probe timed out") is True
    assert devices.update("mixer", host="10.2.30.71", port=51325, latency_ms=3.0) is True
    assert devices.set_status("mixer", "connected") is True
    await drain(bus)

    assert sink.of(DeviceStatusChanged) == [
        DeviceStatusChanged("mixer", "connecting"),
        DeviceStatusChanged("mixer", "error", "device", "probe timed out"),
        DeviceStatusChanged("mixer", "connected"),
    ]
    record = dev.devices.record("mixer")
    assert record is not None
    assert record.status == "connected"
    assert record.kind is None and record.detail is None
    assert record.host == "10.2.30.71" and record.port == 51325
    assert record.latency_ms == 3.0
    assert record.last_seen is not None and "+" in record.last_seen  # ISO 8601 with offset
    assert dev.take_dirty() == {"devices": {"status.mixer"}}
    assert dev.devices.records() == {"mixer": record}
    assert devices.remove("mixer") is True
    assert dev.devices.record("mixer") is None


def test_device_record_round_trips_and_ignores_unknown_keys() -> None:
    record = DeviceStatusRecord(status="degraded", reconnects=2, last_error="EOF")
    assert DeviceStatusRecord.from_dict({**record.as_dict(), "future_field": 1}) == record
    assert DeviceStatusRecord().status == "unconfigured"


# -- system ------------------------------------------------------------------


async def test_banners_emit_set_and_clear_events(dev: StateStore, bus: EventBus) -> None:
    sink = Sink(bus, SystemBannerChanged)
    system = dev.system.writer("health_monitor")
    assert system.set_banner("time_unsynced", "amber", "System time not synchronised") is True
    assert system.set_banner("time_unsynced", "amber", "System time not synchronised") is False
    assert dev.system.banner("time_unsynced") == Banner("amber", "System time not synchronised")
    assert system.clear_banner("time_unsynced") is True
    assert system.clear_banner("time_unsynced") is False
    assert dev.system.banners() == {}
    system.set("time_synced", False)
    system.set("disk_free", 1234)
    assert dev.system.time_synced is False
    assert dev.system.disk_free == 1234.0
    assert dev.system.cert_expiry is None
    await drain(bus)
    assert sink.of(SystemBannerChanged) == [
        SystemBannerChanged("time_unsynced", "amber", "System time not synchronised"),
        SystemBannerChanged("time_unsynced", "amber", None),
    ]


# -- timer -------------------------------------------------------------------


async def test_timer_start_stop_reset_and_computed_elapsed(dev: StateStore, bus: EventBus) -> None:
    sink = Sink(bus, TimerChanged)
    timer = dev.timer.writer("timer_service")
    t0 = datetime.fromisoformat("2026-09-04T19:00:00+12:00")

    assert timer.start(t0.isoformat()) is True
    assert timer.start() is False
    assert dev.timer.elapsed_ms(t0 + timedelta(seconds=90)) == 90_000
    assert timer.stop(t0 + timedelta(seconds=90)) is True
    assert timer.stop() is False
    assert (dev.timer.running, dev.timer.started_at, dev.timer.accumulated_ms) == (
        False,
        None,
        90_000,
    )
    assert dev.timer.elapsed_ms() == 90_000

    t1 = t0 + timedelta(minutes=5)
    timer.start(t1.isoformat())
    assert dev.timer.elapsed_ms(t1 + timedelta(seconds=10)) == 100_000
    assert timer.reset() is True
    assert dev.timer.elapsed_ms() == 0

    await drain(bus)
    assert sink.of(TimerChanged) == [
        TimerChanged(True, t0.isoformat(), 0),
        TimerChanged(False, None, 90_000),
        TimerChanged(True, t1.isoformat(), 90_000),
        TimerChanged(False, None, 0),
    ]
    assert "elapsed" not in dev.timer.SPECS  # computed, never stored (§15.13)


# -- declarations: persistence by construction ------------------------------


def test_display_only_fields_cannot_declare_persistence() -> None:
    assert MixerDomain.DISPLAY_ONLY == frozenset({"meters", "metering"})
    assert LightingDomain.DISPLAY_ONLY == frozenset({"observed"})
    for domain in (MixerDomain, LightingDomain):
        assert not domain.DISPLAY_ONLY & (domain.CONTINUOUS | domain.STATIC | domain.RESTORABLE)
    with pytest.raises(ValueError):
        FieldSpec("meters", "map", persist="continuous", display_only=True)
    with pytest.raises(ValueError):
        FieldSpec("meters", "map", restorable=True, display_only=True)
    with pytest.raises(ValueError):
        FieldSpec("levels", "map", restorable=True)  # restorable needs a persistence class
    with pytest.raises(ValueError):
        FieldSpec("a.b", "scalar")


def test_phase_one_persistence_declarations(dev: StateStore) -> None:
    assert dev.persistence_class("timer", "running") == "static"
    assert dev.persistence_class("timer", "started_at") == "static"
    assert dev.persistence_class("timer", "accumulated_ms") == "continuous"
    assert dev.persistence_class("devices", "status.mixer") == "static"
    assert dev.persistence_class("system", "banners") == "static"
    assert dev.persistence_class("system", "time_synced") is None
    assert dev.persistence_class("lighting", "levels.7") == "continuous"
    assert dev.persistence_class("lighting", "external_control") == "static"
    assert dev.persistence_class("lighting", "master") is None  # resets to 100% (§12.3)
    assert dev.persistence_class("lighting", "observed") is None
    assert dev.persistence_class("mixer", "meters") is None
    assert dev.timer.RESTORABLE == {"running", "started_at", "accumulated_ms"}
    assert dev.lighting.RESTORABLE == {"levels", "colour", "external_control"}
    assert dev.devices.RESTORABLE == frozenset()
    assert dev.mixer.RESTORABLE == frozenset()


def test_skeleton_domains_declare_the_spec_fields(dev: StateStore) -> None:
    assert set(dev.lighting.SPECS) == {
        "levels",
        "colour",
        "master",
        "binding_states",
        "external_control",
        "observed",
    }
    assert set(dev.mixer.SPECS) == {
        "main",
        "outputs",
        "inputs",
        "meters",
        "last_recalled_scene",
        "last_change_source",
        "metering",
    }
    assert set(dev.projector.SPECS) == {"state", "input_ref"}
    assert set(dev.hdmi.SPECS) == {"destinations", "routing"}
    assert set(dev.scenes.SPECS) == {"running", "last_result"}
    assert set(dev.hirer.SPECS) == {"enabled", "token_version", "pages", "permitted_channels"}
    assert dev.lighting.get("external_control") == "off"
    assert dev.hirer.get("pages") == []
