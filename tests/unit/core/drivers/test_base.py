"""The §5.3 run loop: connect versus probe, backoff, maintain, cancellation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from proskenion.core.drivers.base import DeviceStatus, Driver, ProbeResult
from proskenion.core.transport.loopback import LoopbackTransport
from tests.stubs.echo_driver import EchoDriver, RecordingSink, pong_responder


class _SleepRecorder:
    """Stands in for ``asyncio.sleep`` so backoff and probe intervals are instant
    and observable."""

    def __init__(self, progress: asyncio.Event) -> None:
        self.delays: list[float] = []
        self.on_sleep: Callable[[float], None] | None = None
        self.progress = progress

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if self.on_sleep is not None:
            self.on_sleep(delay)
        self.progress.set()
        await asyncio.sleep(0)

    @property
    def backoffs(self) -> list[float]:
        return [d for d in self.delays if d != 0]


def _make(transport: LoopbackTransport) -> tuple[EchoDriver, RecordingSink, _SleepRecorder]:
    sink = RecordingSink()
    driver = EchoDriver(7, transport, {"greeting": "PING"}, sink)
    driver.PROBE_INTERVAL = 0  # type: ignore[misc]  # periodic waits show as 0 in the recorder
    driver.PROBE_TIMEOUT = 0.05  # a loopback that never answers fails fast
    recorder = _SleepRecorder(sink.changed)  # one progress event for reports and sleeps
    driver._sleep = recorder
    return driver, sink, recorder


async def _wait_for(progress: asyncio.Event, condition: Callable[[], bool], limit: float) -> None:
    async with asyncio.timeout(limit):
        while not condition():
            await progress.wait()
            progress.clear()


async def _run_until(
    driver: Driver, sink: RecordingSink, condition: Callable[[], bool], limit: float = 2.0
) -> None:
    task = asyncio.create_task(driver.run())
    try:
        await _wait_for(sink.changed, condition, limit)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_transport_that_opens_but_never_answers_is_a_device_error() -> None:
    transport = LoopbackTransport()  # opens; nobody answers
    driver, sink, recorder = _make(transport)
    await _run_until(driver, sink, lambda: len(recorder.backoffs) >= 1)
    assert (DeviceStatus.ERROR, "device") in sink.statuses
    assert (DeviceStatus.ERROR, "config") not in sink.statuses
    assert DeviceStatus.CONNECTED not in [s for s, _ in sink.statuses]
    error = next(r for r in sink.reports if r[1] is DeviceStatus.ERROR)
    assert error[3] == "no reply to greeting"
    assert not transport.is_open  # disconnected after the failed probe


async def test_transport_whose_open_raises_is_a_config_error() -> None:
    transport = LoopbackTransport(fail_open="permission denied: /dev/ttyUSB0")
    driver, sink, recorder = _make(transport)
    await _run_until(driver, sink, lambda: len(recorder.backoffs) >= 1)
    assert (DeviceStatus.ERROR, "config") in sink.statuses
    assert (DeviceStatus.ERROR, "device") not in sink.statuses
    error = next(r for r in sink.reports if r[1] is DeviceStatus.ERROR)
    assert error[3] == "permission denied: /dev/ttyUSB0"


async def test_answering_transport_is_connected() -> None:
    transport = LoopbackTransport(responder=pong_responder)
    driver, sink, _ = _make(transport)
    await _run_until(driver, sink, lambda: (DeviceStatus.CONNECTED, None) in sink.statuses)
    assert sink.statuses[:2] == [(DeviceStatus.CONNECTING, None), (DeviceStatus.CONNECTED, None)]
    assert transport.sent[0] == b"PING\n"


async def test_backoff_doubles_to_the_cap_without_a_successful_probe() -> None:
    # Accepts connections, never answers: the backoff must never reset.
    driver, sink, recorder = _make(LoopbackTransport())
    await _run_until(driver, sink, lambda: len(recorder.backoffs) >= 9)
    assert recorder.backoffs[:9] == [5, 10, 20, 40, 80, 160, 300, 300, 300]


async def test_backoff_doubles_on_config_failures_too() -> None:
    driver, sink, recorder = _make(LoopbackTransport(fail_open="no route"))
    await _run_until(driver, sink, lambda: len(recorder.backoffs) >= 4)
    assert recorder.backoffs[:4] == [5, 10, 20, 40]


async def test_backoff_resets_only_after_a_successful_probe() -> None:
    answering = True

    def responder(data: bytes) -> bytes | None:
        return pong_responder(data) if answering else None

    transport = LoopbackTransport(responder=responder)
    driver, sink, recorder = _make(transport)

    # Phase 1: three failed attempts (never answers) → 5, 10, 20.
    answering = False
    await _run_until(driver, sink, lambda: len(recorder.backoffs) >= 3)
    assert recorder.backoffs[:3] == [5, 10, 20]
    assert driver._retry_delay == 40

    # Phase 2: the device comes back. The probe succeeds, then we make it fail
    # twice inside maintain so the loop returns to recovery with a reset delay.
    recorder.delays.clear()
    answering = True
    connected = asyncio.Event()

    def on_sleep(delay: float) -> None:
        nonlocal answering
        if (DeviceStatus.CONNECTED, None) in sink.statuses:
            connected.set()
            answering = False  # subsequent periodic probes fail

    recorder.on_sleep = on_sleep
    await _run_until(driver, sink, lambda: connected.is_set() and len(recorder.backoffs) >= 2)
    assert (DeviceStatus.CONNECTED, None) in sink.statuses
    # After maintain returned, the next failed probe backs off from 5 again.
    assert recorder.backoffs[:2] == [5, 10]


async def test_maintain_returns_after_two_consecutive_probe_failures() -> None:
    results = iter([True, False, True, False, False, True])

    class Flaky(EchoDriver):
        async def probe(self) -> ProbeResult:
            return ProbeResult(next(results))

    sink = RecordingSink()
    driver = Flaky(1, LoopbackTransport(), {}, sink)
    recorder = _SleepRecorder(sink.changed)
    driver._sleep = recorder
    await asyncio.wait_for(driver.maintain(), 1.0)
    assert recorder.delays == [30.0] * 5  # PROBE_INTERVAL default from §11.1


async def test_run_is_cancellation_safe() -> None:
    transport = LoopbackTransport(responder=pong_responder)
    driver, sink, _ = _make(transport)
    task = asyncio.create_task(driver.run())
    await _wait_for(sink.changed, lambda: (DeviceStatus.CONNECTED, None) in sink.statuses, 2.0)
    assert transport.is_open
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not transport.is_open


async def test_probe_that_times_out_is_a_dead_probe_not_a_crash() -> None:
    class Raising(EchoDriver):
        async def probe(self) -> ProbeResult:
            raise TimeoutError("no reply")

    sink = RecordingSink()
    driver = Raising(1, LoopbackTransport(), {}, sink)
    recorder = _SleepRecorder(sink.changed)
    driver._sleep = recorder
    await _run_until(driver, sink, lambda: len(recorder.backoffs) >= 1)
    assert (DeviceStatus.ERROR, "device") in sink.statuses


def test_capabilities_is_a_method_not_an_attribute() -> None:
    assert callable(EchoDriver.capabilities)
    assert not isinstance(EchoDriver.__dict__["capabilities"], property)
