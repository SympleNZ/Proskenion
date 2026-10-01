"""LKV422 driver (§7.5): the terminator-agnostic parser, confirmed switching,
the connection model's distinct failure messages, the routing-poll health
probe and its listener hook, all against :mod:`tests.stubs.lkv422_stub` over
a real :class:`LoopbackTransport`."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest

from proskenion.core.drivers import registry
from proskenion.core.drivers.base import DeviceStatus
from proskenion.core.drivers.capabilities import MatrixCapabilities
from proskenion.core.drivers.categories import Category
from proskenion.core.drivers.lkv422 import LKV422Driver, MatrixError
from proskenion.core.transport.base import ConfigurationError
from proskenion.core.transport.loopback import LoopbackTransport
from proskenion.core.transport.serial import SerialTransport
from tests.stubs.echo_driver import RecordingSink
from tests.stubs.lkv422_stub import LKV422Stub, Terminator


class _SleepRecorder:
    """Stands in for ``asyncio.sleep``: instant, and every delay recorded —
    the same shape ``tests/unit/core/drivers/test_base.py`` uses, including
    the progress event so a waiting test never has to poll."""

    def __init__(self) -> None:
        self.delays: list[float] = []
        self.progress = asyncio.Event()

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        self.progress.set()
        await asyncio.sleep(0)


async def _wait_for(event: asyncio.Event, condition: Callable[[], bool], limit: float) -> None:
    async with asyncio.timeout(limit):
        while not condition():
            await event.wait()
            event.clear()


@asynccontextmanager
async def _pair(
    *, terminator: Terminator = "none", ack_switches: bool = False, settle_ms: int = 0
) -> AsyncIterator[tuple[LKV422Driver, LKV422Stub]]:
    """A driver and a stub joined by a real, already-open loopback transport."""
    transport = LoopbackTransport()
    await transport.open()
    async with LKV422Stub(transport, terminator=terminator, ack_switches=ack_switches) as stub:
        driver = LKV422Driver(1, transport, {"settle_ms": settle_ms}, RecordingSink())
        # Real seconds, but short ones — the "no reply" tests below wait them out.
        driver.READ_TIMEOUT = 0.3
        driver.QUIET_PERIOD_S = 0.03
        driver.DRAIN_TIMEOUT_S = 0.01
        yield driver, stub


@pytest.fixture
async def wired() -> AsyncIterator[tuple[LKV422Driver, LKV422Stub]]:
    async with _pair() as pair:
        yield pair


# -- registration -------------------------------------------------------------


def test_registered_under_video_matrix() -> None:
    assert registry.DRIVERS[(Category.VIDEO_MATRIX, "lkv422")] is LKV422Driver


def test_schema_carries_no_addressing() -> None:
    for field in LKV422Driver.CONFIG_SCHEMA:
        assert field.type not in ("host", "device_path")


def test_transport_defaults_to_the_udev_symlink() -> None:
    defaults = LKV422Driver.TRANSPORT_DEFAULTS["serial"]
    assert defaults["device_path"] == "/dev/hdmi-matrix"
    assert defaults["baud"] == 9600
    assert defaults["bits"] == 8
    assert defaults["parity"] == "none"
    assert defaults["stop"] == "1"


async def test_registry_build_produces_a_ready_driver_pointed_at_a_com_port() -> None:
    # "the device path is configurable, for development on Windows (COMx)"
    driver = await registry.build(
        9,
        Category.VIDEO_MATRIX,
        "lkv422",
        {"transport": {"type": "serial", "device_path": "COM3"}, "driver": {}},
        RecordingSink(),
    )
    assert isinstance(driver, LKV422Driver)
    assert driver.transport.config["device_path"] == "COM3"
    assert driver.transport.config["baud"] == 9600


def test_capabilities_are_the_fixed_4x2_matrix() -> None:
    caps = registry.declared_capabilities(LKV422Driver)
    assert caps == MatrixCapabilities(input_count=4, output_count=2, supports_atomic_route=True)


def test_available_refs_lists_four_inputs_and_two_outputs() -> None:
    driver = LKV422Driver(1, LoopbackTransport(), {}, RecordingSink())
    refs = driver.available_refs()
    assert [r.ref for r in refs.inputs] == ["1", "2", "3", "4"]
    assert [r.ref for r in refs.outputs] == ["1", "2"]
    assert all(r.kind == "input" for r in refs.inputs)
    assert all(r.kind == "output" for r in refs.outputs)


# -- terminator-agnostic parsing (§7.5 *Undetermined until bench testing*) --


@pytest.mark.parametrize("terminator", ["none", "cr", "lf", "crlf"])
async def test_read_routing_parses_every_candidate_terminator(terminator: Terminator) -> None:
    async with _pair(terminator=terminator) as (driver, _stub):
        routing = await driver.read_routing()
        assert routing == {"1": "1", "2": "1"}


@pytest.mark.parametrize("terminator", ["none", "cr", "lf", "crlf"])
async def test_route_confirms_correctly_under_every_terminator(terminator: Terminator) -> None:
    async with _pair(terminator=terminator) as (driver, stub):
        await driver.route(["1", "2"], "3")
        assert stub.routing() == {"1": "3", "2": "3"}


# -- confirmed switching (§7.5) -----------------------------------------------


@pytest.mark.parametrize("ack_switches", [True, False])
async def test_route_succeeds_whether_or_not_switches_acknowledge(ack_switches: bool) -> None:
    async with _pair(ack_switches=ack_switches) as (driver, stub):
        await driver.route(["1", "2"], "2")
        assert stub.routing() == {"1": "2", "2": "2"}


async def test_full_route_sends_the_atomic_command(wired: tuple[LKV422Driver, LKV422Stub]) -> None:
    driver, stub = wired
    await driver.route(["1", "2"], "4")
    switch_commands = [c for c in stub.received if c != b"PAXXR"]
    assert switch_commands == [b"PA4R"]


async def test_full_route_is_atomic_regardless_of_output_order(
    wired: tuple[LKV422Driver, LKV422Stub],
) -> None:
    # "every output" is a set, not a sequence (§7.5): naming both in either
    # order is still the whole matrix, so it still gets the one PA{n}R.
    driver, stub = wired
    await driver.route(["2", "1"], "2")
    switch_commands = [c for c in stub.received if c != b"PAXXR"]
    assert switch_commands == [b"PA2R"]


async def test_partial_route_sends_sequential_ps_commands(
    wired: tuple[LKV422Driver, LKV422Stub],
) -> None:
    driver, stub = wired
    await driver.route(["1"], "3")
    switch_commands = [c for c in stub.received if c != b"PAXXR"]
    assert switch_commands == [b"PS13R"]
    assert stub.routing() == {"1": "3", "2": "1"}  # output 2 untouched


async def test_settle_time_is_awaited_before_confirming(
    wired: tuple[LKV422Driver, LKV422Stub],
) -> None:
    driver, _stub = wired
    driver.config["settle_ms"] = 40
    recorder = _SleepRecorder()
    driver._sleep = recorder
    await driver.route(["1", "2"], "3")
    assert recorder.delays == [0.04]


async def test_a_failed_confirmation_raises(wired: tuple[LKV422Driver, LKV422Stub]) -> None:
    driver, stub = wired
    stub.accept_switches = False  # the switch is silently ignored by the device
    with pytest.raises(MatrixError, match="not confirmed"):
        await driver.route(["1", "2"], "3")


async def test_a_failed_confirmation_of_one_output_in_a_partial_route_raises(
    wired: tuple[LKV422Driver, LKV422Stub],
) -> None:
    driver, stub = wired
    stub.accept_switches = False
    with pytest.raises(MatrixError, match=r"output\(s\) 1"):
        await driver.route(["1"], "3")


async def test_route_rejects_an_input_out_of_range(wired: tuple[LKV422Driver, LKV422Stub]) -> None:
    driver, _stub = wired
    with pytest.raises(ValueError, match="inputs"):
        await driver.route(["1", "2"], "5")


async def test_route_rejects_an_unknown_output(wired: tuple[LKV422Driver, LKV422Stub]) -> None:
    driver, _stub = wired
    with pytest.raises(ValueError, match="outputs"):
        await driver.route(["3"], "1")


# -- ERR handling ---------------------------------------------------------------


async def test_stub_returns_err_for_an_unrecognised_command() -> None:
    transport = LoopbackTransport()
    await transport.open()
    async with LKV422Stub(transport) as stub:
        await transport.send(b"ZZZZR")  # ends in "R", like every real command
        reply = await transport.receive(1.0)
        assert reply == b"ERR"
        assert stub.received == [b"ZZZZR"]


async def test_err_on_paxxr_is_reported_distinctly(wired: tuple[LKV422Driver, LKV422Stub]) -> None:
    driver, stub = wired
    stub.err_on_paxxr = True
    with pytest.raises(MatrixError, match="ERR"):
        await driver.read_routing()


# -- garbage and slow replies ---------------------------------------------------


async def test_garbage_reply_is_reported_as_unexpected(
    wired: tuple[LKV422Driver, LKV422Stub],
) -> None:
    driver, stub = wired
    stub.garbage_reply = b"\x01\x02 not a protocol reply at all"
    with pytest.raises(MatrixError, match="unexpected reply"):
        await driver.read_routing()


@pytest.mark.parametrize("prefix", [b"\x00", b"\x00\x00\x00"])
@pytest.mark.parametrize("gap_s", [0.0, 0.1])
async def test_leading_nul_bytes_are_discarded_before_parsing(
    wired: tuple[LKV422Driver, LKV422Stub], prefix: bytes, gap_s: float
) -> None:
    """A stray power-up byte, even one that arrives alone and ahead of the real
    reply by longer than the quiet period, is line noise rather than an error."""
    driver, stub = wired
    stub.stray_prefix = prefix
    stub.stray_gap_s = gap_s  # 0.1 s is well past driver.QUIET_PERIOD_S (0.03 s)
    assert await driver.read_routing() == {"1": "1", "2": "1"}
    result = await driver.probe()
    assert result.alive, result.detail


async def test_a_lone_nul_is_not_a_reply_and_times_out_as_no_reply(
    wired: tuple[LKV422Driver, LKV422Stub],
) -> None:
    driver, stub = wired
    stub.garbage_reply = b"\x00"
    with pytest.raises(MatrixError, match="no reply"):
        await driver.read_routing()


async def test_nuls_do_not_mask_a_genuinely_malformed_reply(
    wired: tuple[LKV422Driver, LKV422Stub],
) -> None:
    driver, stub = wired
    stub.garbage_reply = b"\x00\x00\x01\x02 not a protocol reply"
    with pytest.raises(MatrixError, match="unexpected reply"):
        await driver.read_routing()


async def test_a_slow_reply_still_arrives_within_the_read_timeout(
    wired: tuple[LKV422Driver, LKV422Stub],
) -> None:
    driver, stub = wired
    stub.reply_delay_s = 0.1  # well under driver.READ_TIMEOUT (0.3 s in this fixture)
    routing = await driver.read_routing()
    assert routing == {"1": "1", "2": "1"}


# -- an unread switch acknowledgement never contaminates the next reply -------


async def test_an_unread_acknowledgement_is_drained_before_the_next_request(
    wired: tuple[LKV422Driver, LKV422Stub],
) -> None:
    driver, stub = wired
    stub.ack_switches = True
    await driver.route(["1", "2"], "3")  # the switch's "OK" is sent and never read
    routing = await driver.read_routing()  # must not be confused by the stale "OK"
    assert routing == {"1": "3", "2": "3"}


# -- concurrency: the lock serialises every exchange on the wire --------------
#
# The base driver's periodic probe and a service-layer route() run as
# independent tasks with nothing else ordering them. Without a lock, a
# probe's PAXXR can land while a switch is settling: _drain() can swallow
# the switch's own confirmation reply, or the confirmation can be read from
# whichever exchange's reply happens to arrive first. Both PAXXR commands
# are byte-identical on the wire, so a test cannot tell them apart by
# content — only by *when* they were sent, hence the stub's received_at.


async def test_a_concurrent_probe_cannot_land_during_a_switchs_settle(
    wired: tuple[LKV422Driver, LKV422Stub],
) -> None:
    driver, stub = wired
    stub.reply_delay_s = 0.03  # a real, slow reply — not instant
    settle_s = 0.15
    driver.config["settle_ms"] = int(settle_s * 1000)

    route_task = asyncio.create_task(driver.route(["1", "2"], "3"))
    await asyncio.sleep(0.02)  # the switch is on the wire; route is now settling
    probe_task = asyncio.create_task(driver.probe())  # the periodic health probe, mid-settle

    await route_task  # must not raise MatrixError("not confirmed") from a stolen reply
    probe_result = await probe_task

    assert probe_result.alive is True
    assert stub.routing() == {"1": "3", "2": "3"}

    assert stub.received[0] == b"PA3R"
    switch_time = stub.received_at[0]
    # Nothing else may reach the stub until the settle has actually elapsed:
    # the lock holds route()'s own confirming PAXXR back that long, and must
    # hold the concurrent probe back too, or its query lands inside the
    # settle window this checks for.
    cutoff = switch_time + settle_s * 0.5
    early = [t for t in stub.received_at[1:] if t < cutoff]
    assert early == [], (
        "a command reached the stub during the switch's settle window: "
        f"{list(zip(stub.received, stub.received_at, strict=True))}"
    )
    # Both PAXXR queries got through eventually, one at a time.
    assert stub.received.count(b"PAXXR") == 2


# -- connection model (§7.5) ---------------------------------------------------


async def test_no_reply_gives_the_wiring_message(wired: tuple[LKV422Driver, LKV422Stub]) -> None:
    driver, stub = wired
    stub.never_reply = True
    result = await driver.probe()
    assert result.alive is False
    assert result.detail is not None
    detail = result.detail.lower()
    assert "wiring" in detail
    assert "tx and rx" in detail or "cross" in detail


async def test_missing_serial_path_is_a_configuration_error_naming_dialout() -> None:
    path = "/dev/serial/by-id/usb-does-not-exist-if00-port0"
    transport = SerialTransport({"device_path": path, "baud": 9600})
    driver = LKV422Driver(1, transport, {}, RecordingSink())
    with pytest.raises(ConfigurationError) as exc_info:
        await driver.connect()
    message = str(exc_info.value)
    assert "dialout" in message.lower()
    assert "udev" in message.lower() or "4.12" in message
    assert path in message  # the path actually configured, not the default


async def test_permission_denied_is_a_configuration_error_naming_dialout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def raise_permission_denied(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("serial_asyncio.open_serial_connection", raise_permission_denied)
    transport = SerialTransport({"device_path": "/dev/hdmi-matrix", "baud": 9600})
    driver = LKV422Driver(1, transport, {}, RecordingSink())
    with pytest.raises(ConfigurationError) as exc_info:
        await driver.connect()
    message = str(exc_info.value).lower()
    assert "dialout" in message
    assert "permission" in message


async def test_open_failure_on_a_com_port_names_the_com_port_not_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # "On a development machine it's a COMx port, and the message should
    # name that" — not proskenion.core.drivers.lkv422.DEFAULT_DEVICE_PATH.
    async def raise_permission_denied(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("serial_asyncio.open_serial_connection", raise_permission_denied)
    transport = SerialTransport({"device_path": "COM7", "baud": 9600})
    driver = LKV422Driver(1, transport, {}, RecordingSink())
    with pytest.raises(ConfigurationError) as exc_info:
        await driver.connect()
    message = str(exc_info.value)
    assert "COM7" in message
    assert "/dev/hdmi-matrix" not in message


async def test_a_valid_paxxr_reply_is_the_connection_event_not_the_open(
    wired: tuple[LKV422Driver, LKV422Stub],
) -> None:
    driver, _stub = wired
    # The transport is already open (`_pair` opens it directly, bypassing
    # driver.connect()) — liveness still depends on a successful probe.
    result = await driver.probe()
    assert result.alive is True
    assert "routing" in (result.detail or "")


# -- the run loop against the stub (§5.3) --------------------------------------


async def test_run_reaches_connected_against_the_stub(
    wired: tuple[LKV422Driver, LKV422Stub],
) -> None:
    driver, _stub = wired
    driver.PROBE_INTERVAL = 3600  # keep the periodic poll out of the way
    sink = driver._status_sink
    assert isinstance(sink, RecordingSink)
    task = asyncio.create_task(driver.run())
    try:
        async with asyncio.timeout(2.0):
            while (DeviceStatus.CONNECTED, None) not in sink.statuses:
                await sink.changed.wait()
                sink.changed.clear()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert sink.statuses[:2] == [(DeviceStatus.CONNECTING, None), (DeviceStatus.CONNECTED, None)]


async def test_backoff_doubles_when_the_device_never_replies() -> None:
    transport = LoopbackTransport()  # opens; nobody ever answers
    await transport.open()
    sink = RecordingSink()
    driver = LKV422Driver(1, transport, {}, sink)
    driver.READ_TIMEOUT = 0.0  # instant "no reply" so backoff dominates the wait
    driver.DRAIN_TIMEOUT_S = 0.0
    recorder = _SleepRecorder()
    driver._sleep = recorder
    task = asyncio.create_task(driver.run())
    try:
        await _wait_for(recorder.progress, lambda: len([d for d in recorder.delays if d]) >= 3, 2.0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    backoffs = [d for d in recorder.delays if d != 0]
    assert backoffs[:3] == [5, 10, 20]
    assert (DeviceStatus.ERROR, "device") in sink.statuses
    assert (DeviceStatus.ERROR, "config") not in sink.statuses


async def test_backoff_doubles_on_a_configuration_failure_too() -> None:
    transport = LoopbackTransport(fail_open="permission denied: /dev/hdmi-matrix")
    sink = RecordingSink()
    driver = LKV422Driver(1, transport, {}, sink)
    recorder = _SleepRecorder()
    driver._sleep = recorder
    task = asyncio.create_task(driver.run())
    try:
        await _wait_for(recorder.progress, lambda: len([d for d in recorder.delays if d]) >= 3, 2.0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    backoffs = [d for d in recorder.delays if d != 0]
    assert backoffs[:3] == [5, 10, 20]
    assert (DeviceStatus.ERROR, "config") in sink.statuses


# -- probe cadence: the routing poll is PAXXR every 25 s (§11.1, tightened) ---


async def test_probe_periodically_polls_paxxr_every_25_seconds(
    wired: tuple[LKV422Driver, LKV422Stub],
) -> None:
    """§11.1 gives 30 s; ``PROBE_INTERVAL`` is 25 s so §18's 30-second bound
    on a front-panel change holds with margin (see the driver's own docstring
    on the constant, and ``docs/phase-3-milestone.md``)."""
    driver, stub = wired
    recorder = _SleepRecorder()
    driver._sleep = recorder
    task = asyncio.create_task(driver.probe_periodically())
    try:
        # Each iteration sleeps *then* probes, so N recorded sleeps only
        # guarantee N-1 completed probes; wait for one extra sleep.
        await _wait_for(recorder.progress, lambda: len(recorder.delays) >= 5, 2.0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert recorder.delays[:5] == [25.0, 25.0, 25.0, 25.0, 25.0]
    assert stub.received.count(b"PAXXR") >= 4


# -- listeners (§7.5 *Destinations*, *Out-of-band control*) -------------------


async def test_listener_is_notified_of_a_front_panel_change(
    wired: tuple[LKV422Driver, LKV422Stub],
) -> None:
    driver, stub = wired
    calls: list[tuple[dict[str, str], dict[str, str]]] = []

    async def listener(new: dict[str, str], previous: dict[str, str]) -> None:
        calls.append((new, previous))

    driver.add_routing_listener(listener)
    await driver.read_routing()  # establishes the initial known routing
    calls.clear()

    await stub.front_panel("2", "4")  # out-of-band — no traffic on the line
    routing = await driver.read_routing()  # stands in for the next routing poll

    assert routing == {"1": "1", "2": "4"}
    assert len(calls) == 1
    new, previous = calls[0]
    assert new == {"1": "1", "2": "4"}
    assert previous == {"1": "1", "2": "1"}


async def test_listener_is_notified_of_the_drivers_own_route(
    wired: tuple[LKV422Driver, LKV422Stub],
) -> None:
    driver, _stub = wired
    calls: list[tuple[dict[str, str], dict[str, str]]] = []

    async def listener(new: dict[str, str], previous: dict[str, str]) -> None:
        calls.append((new, previous))

    driver.add_routing_listener(listener)
    await driver.route(["1", "2"], "3")

    assert len(calls) == 1
    new, previous = calls[0]
    assert new == {"1": "3", "2": "3"}
    assert previous == {}  # no prior read_routing() in this test


async def test_listener_is_not_notified_when_routing_is_unchanged(
    wired: tuple[LKV422Driver, LKV422Stub],
) -> None:
    driver, _stub = wired
    calls: list[tuple[dict[str, str], dict[str, str]]] = []

    async def listener(new: dict[str, str], previous: dict[str, str]) -> None:
        calls.append((new, previous))

    await driver.read_routing()
    driver.add_routing_listener(listener)
    await driver.read_routing()  # same routing again
    assert calls == []


async def test_a_raising_listener_is_isolated(wired: tuple[LKV422Driver, LKV422Stub]) -> None:
    driver, _stub = wired
    calls: list[dict[str, str]] = []

    async def broken(new: dict[str, str], previous: dict[str, str]) -> None:
        raise RuntimeError("a listener that misbehaves")

    async def fine(new: dict[str, str], previous: dict[str, str]) -> None:
        calls.append(new)

    driver.add_routing_listener(broken)
    driver.add_routing_listener(fine)
    routing = await driver.read_routing()  # must not raise, despite `broken`

    assert routing == {"1": "1", "2": "1"}
    assert calls == [{"1": "1", "2": "1"}]
