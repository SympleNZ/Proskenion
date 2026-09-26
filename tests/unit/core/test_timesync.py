"""Time synchronisation and degraded time mode (spec §4.9, §22.2).

Every reading goes through an injected command runner and every wait through
an injected clock, so the thirty-second boot wait and the sixty-second retry
are tested without spending a minute of anyone's life.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path

import pytest

from proskenion.config import Config
from proskenion.core.bus import EventBus
from proskenion.core.events import Event, TimeSyncRecovered
from proskenion.core.platform import CommandResult
from proskenion.core.state import StateStore
from proskenion.core.timesync import (
    BOOT_TIMEOUT_S,
    DEGRADED_BANNER_KEY,
    DEGRADED_BANNER_TEXT,
    UNVERIFIED_PREFIX,
    TimeSyncMonitor,
    UnverifiedTimeLog,
)

SYNCED = "NTPSynchronized=yes\nRTCTimeUSec=1757490131000000\n"
NOT_SYNCED = "NTPSynchronized=no\nRTCTimeUSec=1757490131000000\n"
NO_RTC = "NTPSynchronized=yes\nRTCTimeUSec=0\n"
NOT_SYNCED_NO_RTC = "NTPSynchronized=no\nRTCTimeUSec=0\n"


class FakeSchedule:
    """A monotonic clock that only moves when something sleeps against it."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.slept.append(delay)
        self.now += delay
        await asyncio.sleep(0)


class Runner:
    """A command runner answering ``timedatectl`` from a script of outputs."""

    def __init__(self, *outputs: str | Exception, returncode: int = 0) -> None:
        self.outputs = list(outputs)
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], timeout_s: float) -> CommandResult:
        self.calls.append(list(argv))
        output = self.outputs[min(len(self.calls), len(self.outputs)) - 1]
        if isinstance(output, Exception):
            raise output
        return CommandResult(self.returncode, output, "")


class Collector:
    def __init__(self) -> None:
        self.seen: list[Event] = []

    async def __call__(self, event: Event) -> None:
        self.seen.append(event)


async def settle(rounds: int = 20) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


@pytest.fixture
async def bus() -> AsyncIterator[EventBus]:
    bus = EventBus()
    await bus.start()
    try:
        yield bus
    finally:
        await bus.stop()


@pytest.fixture
def state(dev_config: Config, bus: EventBus) -> StateStore:
    return StateStore(dev_config, bus)


@pytest.fixture
def schedule() -> FakeSchedule:
    return FakeSchedule()


@pytest.fixture
def factory_restored() -> Iterator[None]:
    """The log-record factory is process-global; put it back whatever happens."""
    original = logging.getLogRecordFactory()
    try:
        yield
    finally:
        logging.setLogRecordFactory(original)


def build(
    state: StateStore,
    bus: EventBus,
    schedule: FakeSchedule,
    runner: Runner,
    *,
    marker: Path = Path("/nonexistent/synchronized"),
    boot_timeout_s: float = BOOT_TIMEOUT_S,
) -> TimeSyncMonitor:
    return TimeSyncMonitor(
        state,
        bus,
        runner=runner,
        marker=marker,
        boot_timeout_s=boot_timeout_s,
        sleep=schedule.sleep,
        clock=schedule.clock,
    )


# -- reading ------------------------------------------------------------------


async def test_reads_synchronisation_and_rtc_from_timedatectl(
    state: StateStore, bus: EventBus, schedule: FakeSchedule
) -> None:
    runner = Runner(SYNCED)
    status = await build(state, bus, schedule, runner).read()
    assert status.synced is True
    assert status.rtc_present is True
    assert status.source == "timedatectl"
    assert runner.calls[0][:2] == ["timedatectl", "show"]


async def test_no_rtc_is_reported_as_absent_not_unknown(
    state: StateStore, bus: EventBus, schedule: FakeSchedule
) -> None:
    status = await build(state, bus, schedule, Runner(NO_RTC)).read()
    assert status.synced is True
    assert status.rtc_present is False


async def test_falls_back_to_the_marker_when_timedatectl_is_missing(
    state: StateStore, bus: EventBus, schedule: FakeSchedule, tmp_path: Path
) -> None:
    marker = tmp_path / "synchronized"
    marker.write_text("", encoding="utf-8")
    runner = Runner(FileNotFoundError("timedatectl"))
    status = await build(state, bus, schedule, runner, marker=marker).read()
    assert status.synced is True
    assert status.source == "marker"
    # The marker says nothing about the hardware clock; it must not be guessed.
    assert status.rtc_present is None


async def test_no_evidence_at_all_is_reported_as_unavailable(
    state: StateStore, bus: EventBus, schedule: FakeSchedule
) -> None:
    status = await build(state, bus, schedule, Runner(FileNotFoundError())).read()
    assert status.synced is False
    assert status.source == "unavailable"


# -- trustworthy (§4.9, rules.scheduler's own use) -----------------------------


async def test_synced_is_trustworthy(
    state: StateStore, bus: EventBus, schedule: FakeSchedule
) -> None:
    monitor = build(state, bus, schedule, Runner(SYNCED))
    await monitor.read()
    assert monitor.trustworthy is True


async def test_unsynced_with_an_rtc_is_trustworthy(
    state: StateStore, bus: EventBus, schedule: FakeSchedule
) -> None:
    """§4.9: an RTC keeps reasonable time across a reboot with no network."""
    monitor = build(state, bus, schedule, Runner(NOT_SYNCED))
    await monitor.read()
    assert monitor.synced is False
    assert monitor.trustworthy is True


async def test_unsynced_with_no_rtc_is_not_trustworthy(
    state: StateStore, bus: EventBus, schedule: FakeSchedule
) -> None:
    monitor = build(state, bus, schedule, Runner(NOT_SYNCED_NO_RTC))
    await monitor.read()
    assert monitor.trustworthy is False


async def test_unsynced_with_unknown_rtc_presence_is_not_trustworthy(
    state: StateStore, bus: EventBus, schedule: FakeSchedule, tmp_path: Path
) -> None:
    """An RTC that cannot be confirmed present must not be trusted as if it
    were — §4.9's own caution against guessing, applied here too."""
    marker = tmp_path / "synchronized"
    monitor = build(state, bus, schedule, Runner(FileNotFoundError("timedatectl")), marker=marker)
    await monitor.read()
    assert monitor.status.rtc_present is None
    assert monitor.synced is False
    assert monitor.trustworthy is False


# -- the boot wait (§4.9, §12.1) ----------------------------------------------


async def test_boot_wait_succeeds_immediately_when_already_synchronised(
    state: StateStore, bus: EventBus, schedule: FakeSchedule
) -> None:
    monitor = build(state, bus, schedule, Runner(SYNCED))
    assert await monitor.wait_for_sync() is True
    assert schedule.slept == []
    assert state.system.time_synced is True
    assert monitor.degraded is False


async def test_boot_wait_polls_until_it_succeeds_within_thirty_seconds(
    state: StateStore, bus: EventBus, schedule: FakeSchedule
) -> None:
    runner = Runner(*([NOT_SYNCED] * 4), SYNCED)
    assert await build(state, bus, schedule, runner).wait_for_sync() is True
    assert schedule.slept == [1.0, 1.0, 1.0, 1.0]
    assert schedule.now == 1004.0


async def test_boot_wait_gives_up_after_thirty_seconds(
    state: StateStore, bus: EventBus, schedule: FakeSchedule
) -> None:
    monitor = build(state, bus, schedule, Runner(NOT_SYNCED))
    assert await monitor.wait_for_sync() is False
    assert sum(schedule.slept) == pytest.approx(BOOT_TIMEOUT_S)
    assert state.system.time_synced is None  # nothing claimed either way


# -- degraded time mode (§4.9) -------------------------------------------------


async def test_degraded_mode_raises_the_banner_with_the_spec_wording(
    state: StateStore, bus: EventBus, schedule: FakeSchedule, factory_restored: None
) -> None:
    monitor = build(state, bus, schedule, Runner(NOT_SYNCED))
    assert await monitor.wait_for_sync() is False
    monitor.enter_degraded()
    banner = state.system.banner(DEGRADED_BANNER_KEY)
    assert banner is not None
    assert banner.level == "amber"
    assert banner.text == (
        "System time not synchronised. Check the RTC battery and the NTP server."
    )
    assert banner.text == DEGRADED_BANNER_TEXT
    assert state.system.time_synced is False
    assert monitor.degraded is True
    monitor.close()


async def test_degraded_mode_prefixes_log_records(
    state: StateStore,
    bus: EventBus,
    schedule: FakeSchedule,
    caplog: pytest.LogCaptureFixture,
    factory_restored: None,
) -> None:
    monitor = build(state, bus, schedule, Runner(NOT_SYNCED))
    monitor.enter_degraded()
    with caplog.at_level(logging.INFO, logger="tests.unverified"):
        logging.getLogger("tests.unverified").info("scene %s fired", "house lights")
    assert caplog.records[-1].getMessage() == f"{UNVERIFIED_PREFIX} scene house lights fired"
    monitor.close()
    with caplog.at_level(logging.INFO, logger="tests.unverified"):
        logging.getLogger("tests.unverified").info("back to normal")
    assert caplog.records[-1].getMessage() == "back to normal"


async def test_retry_recovers_after_sixty_seconds(
    state: StateStore, bus: EventBus, schedule: FakeSchedule, factory_restored: None
) -> None:
    collector = Collector()
    bus.subscribe(TimeSyncRecovered, collector, name="test.time_sync")
    # Two seconds of boot wait (three readings), then the retry finds it.
    runner = Runner(NOT_SYNCED, NOT_SYNCED, NOT_SYNCED, SYNCED)
    monitor = build(state, bus, schedule, runner, boot_timeout_s=2.0)

    assert await monitor.wait_for_sync() is False
    monitor.enter_degraded()
    started = schedule.now
    await monitor.run()
    await settle()

    assert schedule.now - started == pytest.approx(60.0)  # one retry, sixty seconds
    assert monitor.degraded is False
    assert state.system.banner(DEGRADED_BANNER_KEY) is None
    assert state.system.time_synced is True
    assert [type(event) for event in collector.seen] == [TimeSyncRecovered]


async def test_recovery_removes_the_log_prefix(
    state: StateStore, bus: EventBus, schedule: FakeSchedule, factory_restored: None
) -> None:
    before = logging.getLogRecordFactory()
    monitor = build(
        state,
        bus,
        schedule,
        Runner(NOT_SYNCED, NOT_SYNCED, SYNCED),
        boot_timeout_s=1.0,
    )
    assert await monitor.wait_for_sync() is False
    monitor.enter_degraded()
    assert logging.getLogRecordFactory() is not before
    await monitor.run()
    assert logging.getLogRecordFactory() is before
    assert monitor.degraded is False


# -- the log-record factory ----------------------------------------------------


def test_prefix_install_is_idempotent_and_reversible(factory_restored: None) -> None:
    original = logging.getLogRecordFactory()
    prefix = UnverifiedTimeLog()
    prefix.install()
    prefix.install()
    assert prefix.installed
    assert logging.getLogRecordFactory() is not original
    prefix.remove()
    assert logging.getLogRecordFactory() is original
    prefix.remove()  # safe twice
    assert not prefix.installed
