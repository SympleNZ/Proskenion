"""The CQ-20B driver composing the native metering client (§7.3 *Metering —
two connections*, §5.5), against both stubs at once: the MIDI stub on one
port and the native stub on another, through the real TCP and UDP transports.

The two connections fail independently: MIDI's status never depends on the
native side, and losing MIDI never stops the meters.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from proskenion.core.drivers.base import DeviceStatus
from proskenion.core.drivers.cq20b import CQ20BDriver
from proskenion.core.mixer.native import ALL_REFS
from proskenion.core.transport.tcp import TcpTransport
from tests.stubs.cq_midi_stub import CqMidiStub
from tests.stubs.cq_native_stub import CqNativeStub
from tests.stubs.echo_driver import RecordingSink

IP1_LEVEL = (0x40, 0x00)


async def until(condition: Callable[[], bool], limit: float = 5.0) -> None:
    async with asyncio.timeout(limit):
        while not condition():  # noqa: ASYNC110 - conditions span both stubs and the driver
            await asyncio.sleep(0.01)


async def _instant(_delay: float) -> None:
    await asyncio.sleep(0)


@dataclass
class Rig:
    driver: CQ20BDriver
    sink: RecordingSink
    frames: list[dict[str, float | None]] = field(default_factory=list)


def make_driver(midi_port: int, native_port: int, config: dict[str, Any] | None = None) -> Rig:
    sink = RecordingSink()
    transport = TcpTransport({"host": "127.0.0.1", "port": midi_port})
    driver = CQ20BDriver(1, transport, dict(config or {}), sink)
    driver.NATIVE_PORT = native_port  # type: ignore[misc]
    driver.set_tracked(["ip1"])
    rig = Rig(driver, sink)

    async def record(levels: Mapping[str, float | None]) -> None:
        rig.frames.append(dict(levels))

    driver.add_meter_listener(record)
    return rig


@asynccontextmanager
async def running(rig: Rig) -> AsyncIterator[asyncio.Task[None]]:
    task = asyncio.create_task(rig.driver.run())
    try:
        yield task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _meters_available(driver: CQ20BDriver) -> bool:
    """The native session is up. Not ``capabilities()``: before the first MIDI
    connection that reports the declared maximum (§5.5)."""
    return driver._meters is not None and driver._meters.available


async def test_meters_flow_to_listeners_keyed_per_physical_channel() -> None:
    async with CqMidiStub() as midi, CqNativeStub() as native:
        rig = make_driver(midi.port, native.port)
        async with running(rig):
            await until(lambda: rig.driver.syncs_completed >= 1)
            await until(lambda: len(rig.frames) >= 2 and _meters_available(rig.driver))
            keys = set().union(*rig.frames)
            assert keys <= ALL_REFS
            assert {"ip1", "st1l", "st1r", "mainl", "mainr", "out1"} <= keys
            caps = rig.driver.capabilities()
            assert caps.supports_metering and caps.meter_min_db == -60.0
            assert native.client_inits == 1


async def test_the_native_side_refused_leaves_midi_working() -> None:
    async with CqMidiStub() as midi, CqNativeStub(max_connections=0) as native:
        rig = make_driver(midi.port, native.port)
        async with running(rig):
            await until(lambda: rig.driver.syncs_completed >= 1)
            await until(lambda: native.overlapping_connections >= 1)
            assert rig.sink.statuses[-1] == (DeviceStatus.CONNECTED, None)
            assert all(status is not DeviceStatus.ERROR for status, _ in rig.sink.statuses)
            assert not rig.driver.capabilities().supports_metering
            assert rig.driver.capabilities().supports_scene_recall

            await rig.driver.set_level(["ip1"], -10.0)
            await until(lambda: midi.value(IP1_LEVEL) == 7936)
            assert rig.frames == []


async def test_losing_midi_leaves_the_meters_running() -> None:
    async with CqMidiStub() as midi, CqNativeStub() as native:
        rig = make_driver(midi.port, native.port)
        rig.driver._sleep = _instant
        async with running(rig):
            await until(lambda: rig.driver.syncs_completed >= 1 and _meters_available(rig.driver))
            meters = rig.driver._meters
            assert meters is not None

            # The desk drops MIDI; the run loop reconnects and resyncs.
            await midi.reset_connection()
            await until(lambda: rig.driver.syncs_completed >= 2)
            assert rig.driver._meters is meters and meters.available
            assert native.client_inits == 1  # the native session was never restarted

            # MIDI gone for good: its status goes to error, the meters keep coming.
            reports_before = len(rig.sink.reports)
            await midi.stop()
            await until(
                lambda: any(
                    status is DeviceStatus.ERROR
                    for _, status, _, _ in rig.sink.reports[reports_before:]
                ),
                limit=10.0,
            )
            before = len(rig.frames)
            await until(lambda: len(rig.frames) > before + 2)
            assert rig.driver._meters is meters and meters.available
            assert rig.driver.capabilities().supports_metering
            assert native.client_inits == 1


async def test_the_meters_stop_when_the_run_loop_ends() -> None:
    async with CqMidiStub() as midi, CqNativeStub() as native:
        rig = make_driver(midi.port, native.port)
        async with running(rig):
            await until(lambda: _meters_available(rig.driver))
            assert native.connected_client_count == 1
        await until(lambda: native.connected_client_count == 0)
        assert rig.driver._meters is None


async def test_connecting_outside_the_run_loop_never_starts_the_meters() -> None:
    """The Devices screen's test connects and probes a throwaway driver: it
    must not take one of the desk's two MixPad slots."""
    async with CqMidiStub() as midi, CqNativeStub() as native:
        rig = make_driver(midi.port, native.port)
        await rig.driver.connect()
        assert (await rig.driver.probe()).alive
        await rig.driver.disconnect()
        await asyncio.sleep(0.2)
        assert native.handshakes == 0 and rig.driver._meters is None


async def test_metering_turned_off_never_opens_the_native_connection() -> None:
    async with CqMidiStub() as midi, CqNativeStub() as native:
        rig = make_driver(midi.port, native.port, {"metering": False})
        async with running(rig):
            await until(lambda: rig.driver.syncs_completed >= 1)
            await asyncio.sleep(0.2)
            assert native.handshakes == 0
            assert not rig.driver.capabilities().supports_metering


async def test_a_meter_listener_that_raises_does_not_stop_the_others() -> None:
    async with CqMidiStub() as midi, CqNativeStub() as native:
        rig = make_driver(midi.port, native.port)

        async def broken(_levels: Mapping[str, float | None]) -> None:
            raise RuntimeError("a listener bug")

        rig.driver._meter_listeners.insert(0, broken)
        async with running(rig):
            await until(lambda: len(rig.frames) >= 3)


# -- add_metering_listener (§7.3) ---------------------------------------------


async def test_metering_availability_listener_reports_unavailable_with_a_reason() -> None:
    """A refused native connection (both MixPad slots taken, in the bench's
    own words) reports ``available=False`` with a reason (§7.3, §9)."""
    async with CqMidiStub() as midi, CqNativeStub(max_connections=0) as native:
        rig = make_driver(midi.port, native.port)
        events: list[tuple[bool, str | None]] = []

        async def record(available: bool, reason: str | None) -> None:
            events.append((available, reason))

        rig.driver.add_metering_listener(record)
        async with running(rig):
            await until(lambda: len(events) >= 1)
            assert events[0][0] is False
            assert events[0][1]  # a non-empty reason, the operator-facing message


async def test_metering_availability_listener_reports_available_with_no_reason() -> None:
    async with CqMidiStub() as midi, CqNativeStub() as native:
        rig = make_driver(midi.port, native.port)
        events: list[tuple[bool, str | None]] = []

        async def record(available: bool, reason: str | None) -> None:
            events.append((available, reason))

        rig.driver.add_metering_listener(record)
        async with running(rig):
            await until(lambda: events and events[-1][0] is True)
            assert events[-1] == (True, None)


async def test_a_metering_listener_that_raises_does_not_break_reconnection() -> None:
    async with CqMidiStub() as midi, CqNativeStub() as native:
        rig = make_driver(midi.port, native.port)
        events: list[tuple[bool, str | None]] = []

        async def broken(_available: bool, _reason: str | None) -> None:
            raise RuntimeError("a listener bug")

        async def record(available: bool, reason: str | None) -> None:
            events.append((available, reason))

        rig.driver.add_metering_listener(broken)
        rig.driver.add_metering_listener(record)
        async with running(rig):
            await until(lambda: events and events[-1][0] is True)
            assert rig.driver.capabilities().supports_metering
