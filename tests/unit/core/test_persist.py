"""State persister and restore (spec §5.6, §12.3, §12.4, §15.13)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import pytest

from proskenion.config import Config
from proskenion.core.bus import EventBus
from proskenion.core.persist import CONTINUOUS_INTERVAL_S, NotPersistableError, StatePersister
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import system_state


class TickControl:
    """A sleeper the test drives: the continuous loop ticks only on ``trigger``."""

    def __init__(self) -> None:
        self._wake = asyncio.Event()
        self._back = asyncio.Event()
        self.requested: list[float] = []

    async def sleep(self, delay: float) -> None:
        self.requested.append(delay)
        self._back.set()
        await self._wake.wait()
        self._wake.clear()

    async def trigger(self) -> None:
        """Release one tick and return once the loop has come back for its next sleep."""
        self._back.clear()
        self._wake.set()
        await self._back.wait()


async def settle(rounds: int = 30) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


async def stored(db: Database, domain: str) -> dict[str, object]:
    return {k: json.loads(v) for k, v in (await system_state.get_domain(db, domain)).items()}


@pytest.fixture
def state(dev_config: Config) -> StateStore:
    return StateStore(dev_config, EventBus())


@pytest.fixture
def ticks() -> TickControl:
    return TickControl()


@pytest.fixture
async def persister(
    state: StateStore, db: Database, ticks: TickControl
) -> AsyncIterator[StatePersister]:
    persister = StatePersister(state, db, sleep=ticks.sleep)
    await persister.start()
    try:
        yield persister
    finally:
        await persister.stop()
        persister.close()


def test_interval_matches_spec() -> None:
    assert CONTINUOUS_INTERVAL_S == 0.5
    with pytest.raises(ValueError):
        StatePersister(StateStore.__new__(StateStore), Database(), continuous_interval=0)


async def test_continuous_key_written_once_per_tick_with_latest_value(
    state: StateStore, db: Database, persister: StatePersister, ticks: TickControl
) -> None:
    timer = state.timer.writer("timer_service")
    for n in range(1, 51):  # fifty changes inside one 500 ms window
        timer.set("accumulated_ms", n * 10)
        await asyncio.sleep(0)
    await settle()
    assert await stored(db, "timer") == {}  # nothing until the tick
    assert persister.pending("continuous") == {"timer": {"accumulated_ms"}}

    await ticks.trigger()
    assert persister.transactions == 1
    assert persister.rows_written == 1
    assert await stored(db, "timer") == {"accumulated_ms": 500}
    assert ticks.requested[0] == CONTINUOUS_INTERVAL_S

    await ticks.trigger()  # nothing changed: no write, no lock taken
    assert persister.transactions == 1


async def test_static_key_writes_immediately(
    state: StateStore, db: Database, persister: StatePersister
) -> None:
    system = state.system.writer("health_monitor")
    system.set_banner("time_unsynced", "amber", "System time not synchronised")
    await persister.settled()
    assert persister.transactions == 1
    assert await stored(db, "system") == {
        "banners": {"time_unsynced": {"level": "amber", "text": "System time not synchronised"}}
    }
    row = await system_state.get(db, "system", "banners")
    assert row is not None and row.source == "persister"

    system.clear_banner("time_unsynced")
    await persister.settled()
    assert await stored(db, "system") == {"banners": {}}


async def test_static_and_continuous_timer_fields_take_their_own_paths(
    state: StateStore, db: Database, persister: StatePersister, ticks: TickControl
) -> None:
    timer = state.timer.writer("timer_service")
    t0 = datetime.fromisoformat("2026-09-04T19:00:00+12:00")
    timer.start(t0.isoformat())
    await persister.settled()
    assert await stored(db, "timer") == {"running": True, "started_at": t0.isoformat()}
    timer.stop(t0 + timedelta(seconds=30))
    await persister.settled()
    assert await stored(db, "timer") == {"running": False, "started_at": None}
    await ticks.trigger()
    assert await stored(db, "timer") == {
        "running": False,
        "started_at": None,
        "accumulated_ms": 30_000,
    }


async def test_flush_on_shutdown_writes_everything_pending(
    state: StateStore, db: Database, ticks: TickControl
) -> None:
    persister = StatePersister(state, db, sleep=ticks.sleep)
    await persister.start()
    timer = state.timer.writer("timer_service")
    devices = state.devices.writer("device_manager")
    timer.set("accumulated_ms", 4_200)  # continuous, waiting for a tick that never comes
    devices.set_status("knx", "connected")  # static, but the loop has not run yet
    await persister.stop()  # §12.4 step 3
    assert persister.transactions == 1  # one transaction carrying both classes
    assert await stored(db, "timer") == {"accumulated_ms": 4_200}
    knx = (await stored(db, "devices"))["status"]
    assert isinstance(knx, dict) and knx["knx"]["status"] == "connected"
    assert persister.running is False
    await persister.flush()  # nothing pending: no transaction
    assert persister.transactions == 1
    persister.close()


async def test_meters_and_observed_cannot_be_persisted(
    state: StateStore, db: Database, persister: StatePersister
) -> None:
    assert "meters" not in state.mixer.CONTINUOUS | state.mixer.STATIC
    assert "observed" not in state.lighting.CONTINUOUS | state.lighting.STATIC
    with pytest.raises(NotPersistableError):
        await persister.persist_now("mixer", "meters")
    with pytest.raises(NotPersistableError):
        await persister.persist_now("lighting", "observed")

    # Writing them dirties nothing for the persister and never reaches the table.
    meters = state.mixer.writer("mixer_client")
    meters.set_item("meters", 1, [-12.4])
    observed = state.lighting.writer("artnet_input")
    observed.set_item("observed", 1, 42.0)
    await settle()
    await persister.flush()
    assert persister.pending("continuous") == {} and persister.pending("static") == {}
    assert persister.transactions == 0
    assert await stored(db, "mixer") == {}
    assert await stored(db, "lighting") == {}
    assert state.take_dirty() == {"mixer": {"meters.1"}, "lighting": {"observed.1"}}


async def test_persist_now_writes_a_declared_field_regardless_of_class(
    state: StateStore, db: Database, persister: StatePersister
) -> None:
    state.timer.writer("timer_service").set("accumulated_ms", 7)
    await persister.persist_now("timer", "accumulated_ms")
    assert await stored(db, "timer") == {"accumulated_ms": 7}


async def test_failed_write_is_logged_and_requeued(
    state: StateStore,
    db: Database,
    persister: StatePersister,
    ticks: TickControl,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def broken(*args: object, **kwargs: object) -> int:
        raise RuntimeError("disk full")

    monkeypatch.setattr(system_state, "set_many", broken)
    with caplog.at_level(logging.ERROR, logger="proskenion.core.persist"):
        state.timer.writer("timer_service").start("2026-09-04T19:00:00+12:00")
        await persister.settled()  # returns once the static writer is waiting out the retry
    assert any("re-queued" in r.getMessage() for r in caplog.records)
    assert persister.pending("static") == {"timer": {"running", "started_at"}}
    assert ticks.requested[-1] == CONTINUOUS_INTERVAL_S  # the retry delay
    monkeypatch.undo()
    await persister.flush()
    assert await stored(db, "timer") == {
        "running": True,
        "started_at": "2026-09-04T19:00:00+12:00",
    }


# -- restore (§12.3) ---------------------------------------------------------


async def test_timer_restores_after_restart_and_elapsed_is_computed(
    dev_config: Config, db: Database
) -> None:
    first = StateStore(dev_config, EventBus())
    persister = StatePersister(first, db)
    t0 = datetime.fromisoformat("2026-09-04T19:00:00+12:00")
    first.timer.writer("timer_service").start(t0.isoformat())
    await persister.flush()
    persister.close()

    second = StateStore(dev_config, EventBus())
    restored = await second.restore(db)
    assert restored == {"timer": ["running", "started_at"]}
    assert second.timer.running is True
    assert second.timer.started_at == t0.isoformat()
    assert second.timer.accumulated_ms == 0
    assert second.timer.elapsed_ms(t0 + timedelta(minutes=47, seconds=12)) == 2_832_000
    assert second.take_dirty() == {"timer": {"running", "started_at"}}


async def test_restore_honours_per_domain_declarations(dev_config: Config, db: Database) -> None:
    await system_state.set_many(
        db,
        [
            ("lighting", "levels", json.dumps({"1": 85.0, "2": 78.5})),
            ("lighting", "master", json.dumps(50.0)),  # persisted by nobody; never restored
            ("lighting", "external_control", json.dumps("detected")),  # re-derived, not restored
            ("lighting", "observed", json.dumps({"1": 1.0})),  # must never come back
            ("timer", "accumulated_ms", "not json"),
            ("timer", "running", json.dumps(True)),
            ("devices", "status", json.dumps({"mixer": {"status": "connected"}})),
        ],
    )
    store = StateStore(dev_config, EventBus())
    restored = await store.restore(db)
    assert restored == {"lighting": ["levels"], "timer": ["running"]}
    assert store.lighting.get("levels") == {"1": 85.0, "2": 78.5}
    assert store.lighting.get("master") == 100.0
    assert store.lighting.get("external_control") == "off"
    assert store.lighting.get("observed") == {}
    assert store.timer.accumulated_ms == 0
    assert store.devices.records() == {}

    await system_state.set(db, "lighting", "external_control", json.dumps("manual"))
    assert await store.restore(db, ["lighting"]) == {"lighting": ["external_control", "levels"]}
    assert store.lighting.get("external_control") == "manual"
