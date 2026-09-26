"""The PJLink projector driver (§7.4), against the real TCP transport and the
independent :mod:`tests.stubs.pjlink_stub`."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable

import pytest

from proskenion.core.drivers import registry
from proskenion.core.drivers.base import DeviceStatus, ProbeResult
from proskenion.core.drivers.capabilities import ProjectorCapabilities, ProjectorState
from proskenion.core.drivers.categories import Category
from proskenion.core.drivers.pjlink import (
    AUTH_FAILURE_THRESHOLD,
    AUTH_HOLD_SECONDS,
    MSG_AUTH_NONE_CONFIGURED,
    MSG_AUTH_WRONG_PASSWORD,
    MSG_BUSY,
    PJLinkCommandError,
    PJLinkDriver,
    ProjectorBusyError,
)
from proskenion.core.transport.base import ConfigurationError
from proskenion.core.transport.tcp import TcpTransport
from tests.stubs.echo_driver import RecordingSink
from tests.stubs.pjlink_stub import PJLinkStub

FAKE_PASSWORD = "not-a-real-password"  # test-only; never a credential of any device


class _SleepRecorder:
    """Stands in for ``asyncio.sleep`` so backoff and probe intervals are
    instant and observable (mirrors ``tests/unit/core/drivers/test_base.py``)."""

    def __init__(self) -> None:
        self.delays: list[float] = []
        self.progress = asyncio.Event()

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        self.progress.set()
        await asyncio.sleep(0)


async def _wait_for(progress: asyncio.Event, condition: Callable[[], bool], limit: float) -> None:
    async with asyncio.timeout(limit):
        while not condition():
            await progress.wait()
            progress.clear()


def _driver(port: int, *, password: str | None = None) -> tuple[PJLinkDriver, RecordingSink]:
    transport = TcpTransport({"host": "127.0.0.1", "port": port})
    sink = RecordingSink()
    driver = PJLinkDriver(1, transport, {"password": password}, sink)
    return driver, sink


# -- registration and configuration --------------------------------------


def test_registered_in_the_projector_category() -> None:
    assert registry.DRIVERS[(Category.PROJECTOR, "pjlink")] is PJLinkDriver


def test_config_schema_carries_no_addressing() -> None:
    for f in PJLinkDriver.CONFIG_SCHEMA:
        assert f.type not in ("host", "device_path")
    assert PJLinkDriver.TRANSPORT_DEFAULTS["tcp"]["port"] == 4352


def test_password_field_is_encrypted_and_optional() -> None:
    (password_field,) = PJLinkDriver.CONFIG_SCHEMA
    assert password_field.key == "password"
    assert password_field.type == "password"
    assert password_field.encrypted is True
    assert password_field.required is False


def test_capabilities_is_a_method_not_an_attribute() -> None:
    assert inspect.isfunction(inspect.getattr_static(PJLinkDriver, "capabilities"))


def test_declared_capabilities_before_connect_have_no_inputs_yet() -> None:
    info = registry.declared_capabilities(PJLinkDriver)
    assert isinstance(info, ProjectorCapabilities)
    assert info.inputs == ()


# -- authentication (§7.4) -------------------------------------------------


async def test_no_authentication_required_connects_and_discovers() -> None:
    async with PJLinkStub(require_auth=False) as stub:
        stub.set_power_immediately("1")
        driver, sink = _driver(stub.port)
        await driver.connect()
        result = await driver.probe()
        assert result.alive
        assert driver.capabilities().inputs == stub.inputs
        assert driver._state is ProjectorState.ON  # noqa: SLF001 - internal state assertion


async def test_password_configured_but_not_required_is_ignored() -> None:
    # §7.4's third mismatch case: a password is set, the projector does not
    # ask for one — the connection must proceed exactly as if it were blank.
    async with PJLinkStub(password=FAKE_PASSWORD, require_auth=False) as stub:
        driver, _ = _driver(stub.port, password="something else entirely")
        await driver.connect()
        result = await driver.probe()
        assert result.alive


async def test_authentication_required_but_not_configured() -> None:
    async with PJLinkStub(password=FAKE_PASSWORD, require_auth=True) as stub:
        driver, _ = _driver(stub.port, password=None)
        await driver.connect()
        result = await driver.probe()
        assert result.alive is False
        assert result.detail == MSG_AUTH_NONE_CONFIGURED


async def test_authentication_required_wrong_password() -> None:
    async with PJLinkStub(password=FAKE_PASSWORD, require_auth=True) as stub:
        driver, _ = _driver(stub.port, password="wrong-password")
        await driver.connect()
        result = await driver.probe()
        assert result.alive is False
        assert result.detail == MSG_AUTH_WRONG_PASSWORD


async def test_authentication_required_correct_password_connects() -> None:
    async with PJLinkStub(password=FAKE_PASSWORD, require_auth=True) as stub:
        driver, _ = _driver(stub.port, password=FAKE_PASSWORD)
        await driver.connect()
        result = await driver.probe()
        assert result.alive
        assert driver.capabilities().inputs == stub.inputs


async def test_three_auth_failures_hold_at_a_fixed_sixty_seconds() -> None:
    async with PJLinkStub(password=FAKE_PASSWORD, require_auth=True) as stub:
        driver, sink = _driver(stub.port, password="wrong-password")
        recorder = _SleepRecorder()
        driver._sleep = recorder  # noqa: SLF001 - the documented test hook

        task = asyncio.create_task(driver.run())
        try:
            await _wait_for(recorder.progress, lambda: len(recorder.delays) >= 5, 2.0)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert recorder.delays[:5] == [
            5,
            10,
            AUTH_HOLD_SECONDS,
            AUTH_HOLD_SECONDS,
            AUTH_HOLD_SECONDS,
        ]
        assert driver._consecutive_auth_failures >= AUTH_FAILURE_THRESHOLD  # noqa: SLF001
        assert (DeviceStatus.ERROR, "device") in sink.statuses
        detail = next(r for r in sink.reports if r[1] is DeviceStatus.ERROR)[3]
        assert detail == MSG_AUTH_WRONG_PASSWORD
        # §7.4's 60 s hold: the projector status UI reads this to show amber instead of red.
        assert driver.auth_holding is True


async def test_auth_holding_becomes_true_only_at_the_threshold() -> None:
    """Each ``connect()`` is one attempt, exactly as the run loop retries it —
    the hold begins only once :data:`AUTH_FAILURE_THRESHOLD` have failed."""
    async with PJLinkStub(password=FAKE_PASSWORD, require_auth=True) as stub:
        driver, _ = _driver(stub.port, password="wrong-password")
        assert driver.auth_holding is False
        for _ in range(AUTH_FAILURE_THRESHOLD - 1):
            await driver.connect()
            assert driver.auth_holding is False
        await driver.connect()
        assert driver._consecutive_auth_failures == AUTH_FAILURE_THRESHOLD  # noqa: SLF001
        assert driver.auth_holding is True


# -- the four power states --------------------------------------------------


@pytest.mark.parametrize(
    ("stub_power", "expected"),
    [
        ("0", ProjectorState.OFF),
        ("1", ProjectorState.ON),
        ("2", ProjectorState.COOLING),
        ("3", ProjectorState.WARMING),
    ],
)
async def test_power_states_map_from_powr(stub_power: str, expected: ProjectorState) -> None:
    async with PJLinkStub() as stub:
        stub.set_power_immediately(stub_power)
        driver, _ = _driver(stub.port)
        state = await driver.read_state()
        assert state is expected


# -- ERR3 while warming or cooling (B52) ------------------------------------


async def test_err3_while_warming_raises_busy_error_with_state() -> None:
    async with PJLinkStub() as stub:
        stub.set_power_immediately("3")  # already warming
        driver, _ = _driver(stub.port)
        await driver.connect()  # discovers "warming"
        with pytest.raises(ProjectorBusyError) as excinfo:
            await driver.set_power(True)
        assert excinfo.value.state is ProjectorState.WARMING


async def test_err3_while_cooling_raises_busy_error_with_state() -> None:
    async with PJLinkStub() as stub:
        stub.set_power_immediately("2")
        driver, _ = _driver(stub.port)
        await driver.connect()
        with pytest.raises(ProjectorBusyError) as excinfo:
            await driver.set_input("31")
        assert excinfo.value.state is ProjectorState.COOLING


# -- ERR1, ERR2, ERR4, ERRA mapped ------------------------------------------


async def test_err2_out_of_parameter_on_set_input() -> None:
    async with PJLinkStub() as stub:
        driver, _ = _driver(stub.port)
        await driver.connect()
        with pytest.raises(PJLinkCommandError) as excinfo:
            await driver.set_input("99")  # not in the stub's input list -> ERR2
        assert excinfo.value.code == "ERR2"


async def test_err1_undefined_command() -> None:
    # No public method sends a verb the stub does not know; exercised via
    # the same private helper set_power/set_input use, to cover the
    # ERR1 branch of the reply mapping (§7.4).
    async with PJLinkStub() as stub:
        driver, _ = _driver(stub.port)
        await driver.connect()
        with pytest.raises(PJLinkCommandError) as excinfo:
            await driver._write_command("%1ZZZZ", "?")  # noqa: SLF001
        assert excinfo.value.code == "ERR1"


async def test_err4_projector_failure_on_set_power() -> None:
    async with PJLinkStub() as stub:
        driver, _ = _driver(stub.port)
        await driver.connect()
        stub.force_err4 = True
        with pytest.raises(PJLinkCommandError) as excinfo:
            await driver.set_power(True)
        assert excinfo.value.code == "ERR4"


async def test_err4_on_probe_maps_to_error_state_but_device_is_alive() -> None:
    async with PJLinkStub() as stub:
        driver, _ = _driver(stub.port)
        await driver.connect()
        stub.force_err4 = True
        result = await driver.probe()
        assert result.alive is True  # the projector answered — it is reachable
        assert driver._state is ProjectorState.ERROR  # noqa: SLF001


async def test_erra_forced_regardless_of_digest_is_an_authentication_error() -> None:
    async with PJLinkStub(require_auth=False) as stub:
        driver, _ = _driver(stub.port)
        await driver.connect()
        stub.force_erra = True
        result = await driver.probe()
        assert result.alive is False
        assert result.detail == MSG_AUTH_WRONG_PASSWORD


# -- current_state and read_input (§7.4, for the projector service) --------


async def test_current_state_is_the_last_discovered_value_with_no_io() -> None:
    async with PJLinkStub() as stub:
        stub.set_power_immediately("1")
        driver, _ = _driver(stub.port)
        assert driver.current_state() is ProjectorState.UNREACHABLE  # nothing known yet
        await driver.connect()
        received_before = len(stub.received)
        assert driver.current_state() is ProjectorState.ON
        assert len(stub.received) == received_before  # no command sent


async def test_read_input_queries_inpt_on_demand() -> None:
    async with PJLinkStub(inputs=("11", "21", "31")) as stub:
        stub.current_input = "21"
        driver, _ = _driver(stub.port)
        await driver.connect()

        ref = await driver.read_input()

        assert ref == "21"
        assert stub.received[-1].command == "%1INPT ?"


async def test_read_input_maps_an_error_reply_to_a_command_error() -> None:
    async with PJLinkStub() as stub:
        driver, _ = _driver(stub.port)
        await driver.connect()
        stub.force_err4 = True
        with pytest.raises(PJLinkCommandError) as excinfo:
            await driver.read_input()
        assert excinfo.value.code == "ERR4"


# -- capabilities from INST ? ------------------------------------------------


async def test_capabilities_inputs_come_from_inst() -> None:
    async with PJLinkStub(inputs=("11", "21", "51")) as stub:
        driver, _ = _driver(stub.port)
        await driver.connect()
        assert driver.capabilities().inputs == ("11", "21", "51")
        assert driver.capabilities().supports_authentication is True


# -- probe cadence by state (§7.4, §11.1) ------------------------------------


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (ProjectorState.ON, PJLinkDriver.ON_INTERVAL),
        (ProjectorState.OFF, PJLinkDriver.OFF_INTERVAL),
        (ProjectorState.WARMING, PJLinkDriver.TRANSITION_INTERVAL),
        (ProjectorState.COOLING, PJLinkDriver.TRANSITION_INTERVAL),
        (ProjectorState.ERROR, PJLinkDriver.ERROR_INTERVAL),
    ],
)
def test_interval_for_state(state: ProjectorState, expected: float) -> None:
    driver, _ = _driver(4352)
    assert driver._interval_for(state) == expected  # noqa: SLF001


async def test_maintain_uses_the_cadence_for_the_current_state() -> None:
    driver, _ = _driver(4352)
    driver._state = ProjectorState.WARMING  # noqa: SLF001

    async def always_alive() -> ProbeResult:
        return ProbeResult(True)

    driver.probe = always_alive  # type: ignore[method-assign]
    recorder = _SleepRecorder()
    driver._sleep = recorder  # noqa: SLF001

    task = asyncio.create_task(driver.maintain())
    await _wait_for(recorder.progress, lambda: len(recorder.delays) >= 3, 2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert recorder.delays[:3] == [PJLinkDriver.TRANSITION_INTERVAL] * 3


async def test_a_state_change_cuts_the_off_cadence_wait_short() -> None:
    """A projector off when the wait began is still followed through its warm-up.

    The off cadence stays at five minutes; the warm-up must be seen through to
    *on* within two seconds, which only a wait cut short by the change allows.
    """
    async with PJLinkStub(warm_seconds=0.2) as stub:
        stub.set_power_immediately("0")
        driver, _ = _driver(stub.port)
        driver.TRANSITION_INTERVAL = 0.02  # the cadence under test is OFF's
        on = asyncio.Event()

        async def on_change(new: ProjectorState, previous: ProjectorState) -> None:
            if new is ProjectorState.ON:
                on.set()

        driver.add_state_listener(on_change)
        await driver.connect()
        assert driver.current_state() is ProjectorState.OFF

        task = asyncio.create_task(driver.maintain())
        try:
            await asyncio.sleep(0.05)  # maintain() is now waiting out five minutes
            await driver.set_power(True)
            await driver.read_state()  # what the service does after a command
            async with asyncio.timeout(2.0):
                await on.wait()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


# -- serialised exchanges: the health probe races the service layer ---------


async def test_concurrent_probe_and_set_power_are_serialised() -> None:
    """The base driver's periodic probe and a service-layer call
    (``set_power``/``set_input``/``read_state``) can land at the same moment.
    The stub accepts only one connection at a time, as a real PJLink
    projector is believed to (docs/protocols/pjlink.md) — without
    ``PJLinkDriver._lock`` serialising the whole of ``_run_command``, one of
    the two would race the other's open connection, and either the loser
    fails outright or, on a driver that shared one transport object without
    the lock, the two exchanges corrupt each other. Both must succeed, and
    the stub must never see them overlap.
    """
    async with PJLinkStub() as stub:
        stub.set_power_immediately("0")
        driver, _ = _driver(stub.port)
        await driver.connect()

        probe_result, _ = await asyncio.gather(driver.probe(), driver.set_power(True))

        assert probe_result.alive is True
        assert stub.overlapping_connections == 0


# -- a connect timeout is busy, not offline (§8) ---------------------


class _ConnectTimeoutTransport:
    """A transport whose ``open()`` reproduces exactly what
    ``TcpTransport.open()`` raises for a TCP connect that never completes
    (``proskenion/core/transport/tcp.py``): a :class:`ConfigurationError`
    caused by a bare :class:`TimeoutError`. No real socket is needed to prove
    the driver classifies this shape as busy rather than offline
    (docs/protocols/pjlink.md §8) — that is a pure classification decision
    inside :meth:`PJLinkDriver.connect`/:meth:`~PJLinkDriver.probe`.
    """

    is_open = False

    async def open(self) -> None:
        message = "no answer from 127.0.0.1:4352 — check the address"
        raise ConfigurationError(message) from TimeoutError()

    async def close(self) -> None:
        return None

    async def send(self, data: bytes) -> None:  # pragma: no cover - open() always raises first
        raise AssertionError("never reached: open() always raises")

    async def receive(self, read_timeout: float) -> bytes:  # pragma: no cover - ditto
        raise AssertionError("never reached: open() always raises")

    async def enumerate(self) -> None:
        return None


class _RefusedTransport(_ConnectTimeoutTransport):
    """The other shape ``TcpTransport.open()`` raises for: an outright
    refusal, indistinguishable from "unplugged" — must stay red."""

    async def open(self) -> None:
        message = "cannot connect to 127.0.0.1:4352: refused"
        raise ConfigurationError(message) from ConnectionRefusedError()


async def test_a_connect_that_never_completes_is_busy_not_offline() -> None:
    """The bench-confirmed shape (docs/protocols/pjlink.md §8): a second
    client's TCP connect itself never completes while the projector's one
    slot is held elsewhere. ``connect()`` must swallow this — recording it
    on ``connect_busy`` for ``probe()`` to report as a *device*-kind failure
    — rather than let ``ConfigurationError`` propagate, which the run loop
    always shows red (``kind="config"``, no amber path exists for it)."""
    driver = PJLinkDriver(1, _ConnectTimeoutTransport(), {}, RecordingSink())
    await driver.connect()  # must not raise
    assert driver.connect_busy is True
    result = await driver.probe()
    assert result.alive is False
    assert result.detail == MSG_BUSY


async def test_a_refused_connect_still_raises_and_stays_offline() -> None:
    """The fix must not blur "busy" into "any connect failure": an outright
    refusal keeps propagating as a config-kind failure, red, unchanged."""
    driver = PJLinkDriver(1, _RefusedTransport(), {}, RecordingSink())
    with pytest.raises(ConfigurationError):
        await driver.connect()
    assert driver.connect_busy is False


async def test_a_held_second_connection_probes_as_busy_not_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other bench-confirmed shape: the TCP connect itself completes but
    the greeting never arrives, because the slot is held elsewhere — a real
    socket, unlike the two tests above, reproduced with
    ``PJLinkStub(overlap_mode="hold")``. The driver must read this the same
    way: busy, not offline, and a *later* probe (not just the first
    ``connect()``) is what actually hits it here, matching the site finding
    that the legacy controller at ``10.2.30.250`` keeps polling the same
    projector (docs/protocols/pjlink.md §8)."""
    monkeypatch.setattr(PJLinkDriver, "COMMAND_TIMEOUT", 0.2)  # keep the test fast
    async with PJLinkStub() as stub:
        stub.overlap_mode = "hold"
        stub.set_power_immediately("1")
        driver, _ = _driver(stub.port)
        await driver.connect()
        assert driver.connect_busy is False
        first = await driver.probe()
        assert first.alive is True

        # A second client takes the projector's only slot and holds it.
        _reader, interloper = await asyncio.open_connection("127.0.0.1", stub.port)
        try:
            result = await driver.probe()
        finally:
            interloper.close()

        assert result.alive is False
        assert result.detail == MSG_BUSY
        assert driver.connect_busy is True
        assert stub.overlapping_connections >= 1
        # The last-known power state stands; a busy slot is not "unreachable".
        assert driver._state is ProjectorState.ON  # noqa: SLF001 - internal state assertion


# -- listeners ---------------------------------------------------------------


async def test_listeners_called_once_per_change_with_new_and_previous() -> None:
    async with PJLinkStub() as stub:
        stub.set_power_immediately("0")
        driver, _ = _driver(stub.port)
        changes: list[tuple[ProjectorState, ProjectorState]] = []

        async def on_change(new: ProjectorState, previous: ProjectorState) -> None:
            changes.append((new, previous))

        driver.add_state_listener(on_change)
        await driver.connect()  # off -> discovered as off: first-ever value
        assert changes == [(ProjectorState.OFF, ProjectorState.UNREACHABLE)]

        await driver.read_state()  # still off: no repeated notification
        assert len(changes) == 1

        stub.set_power_immediately("3")
        await driver.read_state()
        stub.set_power_immediately("1")
        await driver.read_state()

        assert changes == [
            (ProjectorState.OFF, ProjectorState.UNREACHABLE),
            (ProjectorState.WARMING, ProjectorState.OFF),
            (ProjectorState.ON, ProjectorState.WARMING),
        ]


async def test_a_listener_that_raises_does_not_break_the_others() -> None:
    async with PJLinkStub() as stub:
        stub.set_power_immediately("1")
        driver, _ = _driver(stub.port)
        calls: list[ProjectorState] = []

        async def bad(new: ProjectorState, previous: ProjectorState) -> None:
            raise RuntimeError("boom")

        async def good(new: ProjectorState, previous: ProjectorState) -> None:
            calls.append(new)

        driver.add_state_listener(bad)
        driver.add_state_listener(good)
        await driver.connect()
        assert calls == [ProjectorState.ON]


# -- connect discovers, never a power command --------------------------------


async def test_connect_discovers_and_sends_no_power_command() -> None:
    async with PJLinkStub() as stub:
        stub.set_power_immediately("1")
        driver, _ = _driver(stub.port)
        await driver.connect()

        commands = [r.command for r in stub.received]
        assert "%1POWR ?" in commands
        assert "%1INST ?" in commands
        assert not any(c.startswith("%1POWR 0") or c.startswith("%1POWR 1") for c in commands)


# -- unreachable, and recovery with backoff ----------------------------------


async def test_unreachable_then_recovers_with_backoff_reset() -> None:
    # Learn a free loopback port, then release it before pointing the driver
    # at it, so the first phase finds nothing listening.
    probe = PJLinkStub()
    await probe.start()
    port = probe.port
    await probe.stop()

    driver, sink = _driver(port)
    recorder = _SleepRecorder()
    driver._sleep = recorder  # noqa: SLF001

    task = asyncio.create_task(driver.run())
    try:
        # A refused connection on this platform takes real wall-clock time to
        # come back (observed ~2 s on Windows loopback) — the fake ``_sleep``
        # only removes the *backoff* wait, not that.
        await _wait_for(recorder.progress, lambda: len(recorder.delays) >= 2, 15.0)
        assert (DeviceStatus.ERROR, "config") in sink.statuses
        assert recorder.delays[:2] == [5, 10]

        stub = PJLinkStub(port=port)
        await stub.start()
        try:
            await _wait_for(
                sink.changed, lambda: (DeviceStatus.CONNECTED, None) in sink.statuses, 15.0
            )
            assert driver._retry_delay == driver.INITIAL_RETRY_DELAY  # noqa: SLF001
        finally:
            await stub.stop()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# -- test connection (§7.4, via the framework's connect-then-probe) ---------
#
# ``DeviceManager.test()`` (proskenion/core/devices.py) is exactly this
# sequence with a timeout wrapped around each stage and encrypted fields
# resolved first (§6.10, outside this driver's scope) — what matters at the
# driver level is that connect()-then-probe() surfaces the exact §7.4
# message through ``ProbeResult.detail``, which is what that endpoint reads.


async def test_test_connection_reports_the_exact_auth_message() -> None:
    async with PJLinkStub(password=FAKE_PASSWORD, require_auth=True) as stub:
        driver = await registry.build(
            0,
            Category.PROJECTOR,
            "pjlink",
            {
                "transport": {"type": "tcp", "host": "127.0.0.1", "port": stub.port},
                "driver": {"password": "wrong-password"},
            },
            RecordingSink(),
        )
        await driver.connect()  # does not raise: the TCP connection itself succeeded
        result = await driver.probe()
        assert result.alive is False
        assert result.detail == MSG_AUTH_WRONG_PASSWORD
