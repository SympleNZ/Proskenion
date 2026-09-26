"""The device manager and the stub video matrix (spec §5.5, §5.3, §12.1).

The drivers below are deliberately unhelpful in specific ways — one refuses to
connect, one opens and never answers, one follows a script — because those are
exactly the failures §5.3 asks the manager to tell apart.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any, ClassVar

import pytest

from proskenion.config import Config
from proskenion.core.bus import EventBus
from proskenion.core.devices import OWNER, DeviceManager, slot_name, state_keys
from proskenion.core.drivers import load_shipped_drivers, registry
from proskenion.core.drivers.base import Driver, ProbeResult
from proskenion.core.drivers.capabilities import MatrixCapabilities
from proskenion.core.drivers.categories import Category
from proskenion.core.drivers.cq20b import MSG_REFUSED as CQ_MSG_REFUSED
from proskenion.core.drivers.cq20b import CQ20BDriver
from proskenion.core.drivers.fields import Field
from proskenion.core.drivers.pjlink import MSG_AUTH_WRONG_PASSWORD, MSG_BUSY, PJLinkDriver
from proskenion.core.drivers.stub_matrix import StubMatrixDriver
from proskenion.core.events import DeviceStatusChanged
from proskenion.core.state import StateStore
from proskenion.core.transport.base import ConfigurationError
from proskenion.core.transport.loopback import LoopbackTransport
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from tests.stubs.cq_midi_stub import CqMidiStub
from tests.stubs.pjlink_stub import PJLinkStub

LOOPBACK: dict[str, Any] = {"transport": {"type": "loopback"}, "driver": {}}


# -- test drivers ---------------------------------------------------------------


class _Fast:
    """Tiny intervals so a run loop's recovery happens inside a test."""

    PROBE_INTERVAL: ClassVar[float] = 0.01
    INITIAL_RETRY_DELAY: ClassVar[float] = 0.005
    MAX_RETRY_DELAY: ClassVar[float] = 0.02


class RefusingDriver(_Fast, Driver):
    """``connect()`` fails the way a wrong address does — failure kind ``config``."""

    key = "refuse"
    category = Category.VIDEO_MATRIX
    name = "Refusing test driver"
    SUPPORTED_TRANSPORTS = ["loopback"]

    async def connect(self) -> None:
        raise ConfigurationError("no route to host")

    async def probe(self) -> ProbeResult:  # pragma: no cover - never reached
        return ProbeResult(True)

    def capabilities(self) -> MatrixCapabilities:
        return MatrixCapabilities(1, 1, supports_atomic_route=False)


class SilentDriver(_Fast, Driver):
    """The transport opens and nothing ever answers — failure kind ``device``."""

    key = "silent"
    category = Category.VIDEO_MATRIX
    name = "Silent test driver"
    SUPPORTED_TRANSPORTS = ["loopback"]

    async def probe(self) -> ProbeResult:
        return ProbeResult(False, "no reply to PAXXR")

    def capabilities(self) -> MatrixCapabilities:
        return MatrixCapabilities(1, 1, supports_atomic_route=False)


@dataclass
class Script:
    """What the scripted driver should do next; reset before every test."""

    connect_failures: int = 0
    probes: list[bool] = field(default_factory=list)  # consumed in order, then alive


SCRIPT = Script()


class ScriptedDriver(_Fast, Driver):
    """Connects and probes according to :data:`SCRIPT`."""

    key = "scripted"
    category = Category.VIDEO_MATRIX
    name = "Scripted test driver"
    SUPPORTED_TRANSPORTS = ["loopback"]

    async def connect(self) -> None:
        if SCRIPT.connect_failures > 0:
            SCRIPT.connect_failures -= 1
            raise ConfigurationError("no route to host")
        await super().connect()

    async def probe(self) -> ProbeResult:
        alive = SCRIPT.probes.pop(0) if SCRIPT.probes else True
        return ProbeResult(alive, None if alive else "no reply to PAXXR")

    def capabilities(self) -> MatrixCapabilities:
        return MatrixCapabilities(1, 1, supports_atomic_route=False)


class FastStubDriver(StubMatrixDriver):
    """The shipped stub, probing often enough to prove probes emit no events."""

    key = "faststub"
    name = "Stub video matrix (fast probe)"
    PROBE_INTERVAL = 0.005


class CountingStubDriver(StubMatrixDriver):
    """Counts calls, to prove one intent is one call (B47)."""

    key = "countstub"
    name = "Stub video matrix (counting)"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.route_calls = 0

    async def route(self, outputs: list[str], input: str) -> None:
        self.route_calls += 1
        await super().route(outputs, input)


TEST_DRIVERS: tuple[type[Driver], ...] = (
    RefusingDriver,
    SilentDriver,
    ScriptedDriver,
    FastStubDriver,
    CountingStubDriver,
)


# -- fixtures -------------------------------------------------------------------


@pytest.fixture(autouse=True)
def drivers(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[tuple[Category, str], type[Driver]]]:
    """The shipped registry plus the drivers above, for one test."""
    load_shipped_drivers()  # before the patch, so the shipped ones land in the real table
    table = dict(registry.DRIVERS)
    for driver_cls in TEST_DRIVERS:
        table[(driver_cls.category, driver_cls.key)] = driver_cls
    monkeypatch.setattr(registry, "DRIVERS", table)
    SCRIPT.connect_failures = 0
    SCRIPT.probes = []
    yield table


@dataclass
class Harness:
    bus: EventBus
    state: StateStore
    manager: DeviceManager
    events: list[DeviceStatusChanged]

    def statuses(self, device: str | None = None) -> list[str]:
        return [e.status for e in self.events if device is None or e.device == device]


@pytest.fixture
async def harness(db: Database, dev_config: Config) -> AsyncIterator[Harness]:
    bus = EventBus()
    await bus.start()
    state = StateStore(dev_config, bus)
    events: list[DeviceStatusChanged] = []

    async def collect(event: DeviceStatusChanged) -> None:
        events.append(event)

    bus.subscribe(DeviceStatusChanged, collect, name="test-collector")
    manager = DeviceManager(
        db, state, bus, dev_config, connect_timeout=2.0, probe_timeout=0.5, stop_timeout=2.0
    )
    try:
        yield Harness(bus, state, manager, events)
    finally:
        await manager.stop()
        await bus.stop()


async def add_device(
    db: Database,
    *,
    category: str = "video_matrix",
    driver_key: str = "stub",
    name: str = "Matrix",
    config: dict[str, Any] | None = None,
    enabled: bool = True,
) -> devices_crud.Device:
    return await devices_crud.create(
        db,
        category=category,
        driver_key=driver_key,
        name=name,
        config=LOOPBACK if config is None else config,
        enabled=enabled,
    )


async def settle(seconds: float = 0.05) -> None:
    """Let the bus deliver and the run loops make progress."""
    await asyncio.sleep(seconds)


async def wait_for_event(harness: Harness, status: str, limit_s: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + limit_s
    while status not in harness.statuses():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"{status!r} never arrived; saw {harness.statuses()}")
        await asyncio.sleep(0.005)


# -- the stub driver ------------------------------------------------------------


def test_stub_is_registered_and_says_it_is_not_hardware() -> None:
    load_shipped_drivers()
    driver_cls = registry.get(Category.VIDEO_MATRIX, "stub")
    assert driver_cls is StubMatrixDriver
    assert "no hardware" in StubMatrixDriver.name.lower()
    assert StubMatrixDriver.SUPPORTED_TRANSPORTS == ["loopback"]


async def test_stub_capabilities_and_refs_follow_its_configuration() -> None:
    config = {"input_count": 6, "output_count": 3}
    driver = StubMatrixDriver(1, LoopbackTransport(), config, _sink())
    capabilities = driver.capabilities()
    assert capabilities == MatrixCapabilities(6, 3, supports_atomic_route=True)
    refs = driver.available_refs()
    assert [r.ref for r in refs.inputs] == ["1", "2", "3", "4", "5", "6"]
    assert [r.label for r in refs.outputs] == ["Output 1", "Output 2", "Output 3"]


async def test_stub_probe_is_alive_only_while_the_transport_is_open() -> None:
    transport = LoopbackTransport()
    driver = StubMatrixDriver(1, transport, {}, _sink())
    assert (await driver.probe()).alive is False
    await driver.connect()
    assert (await driver.probe()).alive is True


async def test_routing_four_outputs_is_one_call_and_one_write() -> None:
    """§5.5's single-intent rule: the core never issues N calls for one operation."""
    transport = LoopbackTransport()
    driver = CountingStubDriver(1, transport, {"input_count": 4, "output_count": 4}, _sink())
    await driver.connect()

    await driver.route(["1", "2", "3", "4"], "2")

    assert driver.route_calls == 1
    assert len(transport.sent) == 1
    assert await driver.read_routing() == {"1": "2", "2": "2", "3": "2", "4": "2"}


async def test_stub_rejects_references_it_does_not_have() -> None:
    driver = StubMatrixDriver(1, LoopbackTransport(), {}, _sink())
    await driver.connect()
    with pytest.raises(ValueError, match="inputs are 1-4"):
        await driver.route(["1"], "9")
    with pytest.raises(ValueError, match="outputs are 1-2"):
        await driver.route(["7"], "1")


def _sink() -> Any:
    class _Null:
        async def set_status(self, *args: Any, **kwargs: Any) -> None:
            return None

    return _Null()


# -- state keys -----------------------------------------------------------------


def test_state_keys_use_the_subsystem_slot_and_disambiguate_extra_instances() -> None:
    rows = [
        _row(1, "video_matrix"),
        _row(2, "mixer"),
        _row(3, "mixer"),
    ]
    assert slot_name("video_matrix") == "hdmi"
    assert state_keys(rows) == {1: "hdmi", 2: "mixer:2", 3: "mixer:3"}


def _row(device_id: int, category: str) -> devices_crud.Device:
    return devices_crud.Device(
        id=device_id,
        category=category,
        driver_key="stub",
        name=f"device {device_id}",
        enabled=True,
        config={},
        created_at="2026-09-10T12:00:00+12:00",
        updated_at="2026-09-10T12:00:00+12:00",
    )


# -- the manager ----------------------------------------------------------------


async def test_configured_stub_connects_and_state_carries_it(
    db: Database, harness: Harness
) -> None:
    device = await add_device(db, name="Auditorium matrix")
    await harness.manager.start()

    record = await harness.manager.wait_for_connection(device.id)

    assert record is not None
    assert record.status == "connected"
    assert record.protocol == StubMatrixDriver.name
    assert record.last_seen is not None
    assert record.reconnects == 0
    assert harness.manager.state_key(device.id) == "hdmi"
    assert harness.state.devices.record("hdmi") == record
    assert harness.state.owners("devices") == frozenset({OWNER})


async def test_status_changes_are_emitted_per_transition_not_per_probe(
    db: Database, harness: Harness
) -> None:
    device = await add_device(db, driver_key="faststub")
    await harness.manager.start()
    await harness.manager.wait_for_connection(device.id)

    await settle(0.1)  # many probe intervals for the fast stub

    assert harness.statuses("hdmi") == ["connecting", "connected"]


async def test_a_transport_that_will_not_open_is_error_kind_config_and_isolated(
    db: Database, harness: Harness
) -> None:
    refusing = await add_device(db, driver_key="refuse", name="Broken")
    working = await add_device(db, driver_key="stub", name="Working")
    await harness.manager.start()

    bad = await harness.manager.wait_for_connection(refusing.id)
    good = await harness.manager.wait_for_connection(working.id)

    assert bad is not None and bad.status == "error"
    assert bad.kind == "config"
    assert bad.detail == "no route to host"
    assert bad.last_error == "no route to host"
    assert good is not None and good.status == "connected"


async def test_a_transport_that_opens_but_never_answers_is_kind_device(
    db: Database, harness: Harness
) -> None:
    silent = await add_device(db, driver_key="silent", name="Silent")
    working = await add_device(db, driver_key="stub", name="Working")
    await harness.manager.start()

    bad = await harness.manager.wait_for_connection(silent.id)
    good = await harness.manager.wait_for_connection(working.id)

    assert bad is not None and bad.status == "error"
    assert bad.kind == "device"  # connect succeeded; the device did not reply
    assert good is not None and good.status == "connected"


async def test_a_lost_device_is_degraded_before_it_is_error(
    db: Database, harness: Harness
) -> None:
    """§7.5: one missed reply shows the last-known reading in amber; two are red."""
    SCRIPT.probes = [True, False, False, False, False, False, False]
    device = await add_device(db, driver_key="scripted")
    await harness.manager.start()

    await harness.manager.wait_for_connection(device.id)
    await wait_for_event(harness, "error")

    statuses = harness.statuses("hdmi")
    assert statuses[:2] == ["connecting", "connected"]
    assert statuses.index("degraded") < statuses.index("error")


# -- the §7.4 amber authentication hold ---------------------------------


async def _no_sleep(_seconds: float) -> None:
    """Stands in for a driver's ``_sleep`` so its backoff (up to 60 s) does
    not really wait; still yields once, so the run loop is not a tight spin."""
    await asyncio.sleep(0)


async def test_pjlink_auth_failures_are_red_then_amber_during_the_hold(
    db: Database, harness: Harness
) -> None:
    """§7.4: the first failures show red; the 60 s hold shows amber, with its
    exact message — a password that has never worked still gets amber, even
    though it has no successful connection to be "degraded" from."""
    async with PJLinkStub(password="the-real-password", require_auth=True) as stub:
        device = await add_device(
            db,
            category="projector",
            driver_key="pjlink",
            name="Projector",
            config={
                "transport": {"type": "tcp", "host": "127.0.0.1", "port": stub.port},
                "driver": {"password": "wrong-password"},
            },
        )
        await harness.manager.start()
        # The supervised task has not run yet (nothing has awaited since
        # create_task): safe to swap its sleep before its first backoff.
        runtime = harness.manager._runtimes[device.id]  # noqa: SLF001
        driver = runtime.driver
        assert isinstance(driver, PJLinkDriver)
        driver._sleep = _no_sleep  # noqa: SLF001

        # The run loop cycles as fast as it can with no real backoff, so the
        # live snapshot can already have moved past a status by the time a
        # test reads it; the append-only event history is not racy the same
        # way and is what every assertion below reads instead.
        await wait_for_event(harness, "degraded")
        projector_events = [e for e in harness.events if e.device == "projector"]
        statuses = [e.status for e in projector_events]
        assert statuses.index("error") < statuses.index("degraded")

        first_error = next(e for e in projector_events if e.status == "error")
        assert first_error.kind == "device"  # red: no hold yet
        assert first_error.detail == MSG_AUTH_WRONG_PASSWORD

        held = next(e for e in projector_events if e.status == "degraded")
        assert held.kind == "device"  # amber: the hold
        assert held.detail == MSG_AUTH_WRONG_PASSWORD
        assert driver.auth_holding is True


async def test_pjlink_busy_connect_is_amber_never_red(
    db: Database, harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """docs/protocols/pjlink.md §8: the projector's one slot held elsewhere
    reads amber ("busy"), never red — even though, unlike the CQ-20B's
    refusal above, nothing about this ever raises out of ``connect()``. And
    because it never reaches ``status == "error"``,
    ``DeviceRedAlertMonitor`` (which only watches that status) never starts
    a device-red alert for it."""
    monkeypatch.setattr(PJLinkDriver, "COMMAND_TIMEOUT", 0.2)  # keep retries fast
    async with PJLinkStub() as stub:
        stub.overlap_mode = "hold"
        _reader, interloper = await asyncio.open_connection("127.0.0.1", stub.port)
        try:
            device = await add_device(
                db,
                category="projector",
                driver_key="pjlink",
                name="Projector",
                config={
                    "transport": {"type": "tcp", "host": "127.0.0.1", "port": stub.port},
                    "driver": {},
                },
            )
            await harness.manager.start()
            driver = harness.manager._runtimes[device.id].driver  # noqa: SLF001
            assert isinstance(driver, PJLinkDriver)
            driver._sleep = _no_sleep  # noqa: SLF001

            await wait_for_event(harness, "degraded", limit_s=10.0)
            projector_events = [e for e in harness.events if e.device == "projector"]
            assert "error" not in [e.status for e in projector_events]
            held = next(e for e in projector_events if e.status == "degraded")
            assert held.kind == "device"
            assert held.detail == MSG_BUSY
            assert driver.connect_busy is True
        finally:
            interloper.close()


async def test_cq20b_refused_is_amber_from_the_first_failure(
    db: Database, harness: Harness
) -> None:
    """§7.3: a refused MIDI connection is amber with its exact message, even
    though nothing has ever connected — another client holds the desk, which is
    a two-second fix, not an outage."""
    async with CqMidiStub() as stub:
        _reader, interloper = await asyncio.open_connection("127.0.0.1", stub.port)
        try:
            device = await add_device(
                db,
                category="mixer",
                driver_key="cq20b",
                name="Mixer",
                config={
                    "transport": {"type": "tcp", "host": "127.0.0.1", "port": stub.port},
                    "driver": {},
                },
            )
            await harness.manager.start()
            driver = harness.manager._runtimes[device.id].driver  # noqa: SLF001
            assert isinstance(driver, CQ20BDriver)
            driver._sleep = _no_sleep  # noqa: SLF001

            await wait_for_event(harness, "degraded", limit_s=10.0)
            mixer_events = [e for e in harness.events if e.device == "mixer"]
            assert "error" not in [e.status for e in mixer_events]
            held = next(e for e in mixer_events if e.status == "degraded")
            assert held.kind == "device"
            assert held.detail == CQ_MSG_REFUSED
        finally:
            interloper.close()


async def test_a_recovered_device_counts_one_reconnect(db: Database, harness: Harness) -> None:
    SCRIPT.probes = [True, False, False, False, True]
    device = await add_device(db, driver_key="scripted")
    await harness.manager.start()

    await harness.manager.wait_for_connection(device.id)
    await wait_for_event(harness, "degraded")
    deadline = asyncio.get_running_loop().time() + 2.0
    record = harness.manager.status(device.id)
    while (record is None or record.status != "connected") and (
        asyncio.get_running_loop().time() < deadline
    ):
        await asyncio.sleep(0.005)
        record = harness.manager.status(device.id)

    assert record is not None and record.status == "connected"
    assert record.reconnects == 1


async def test_an_unknown_driver_key_does_not_prevent_start(
    db: Database, harness: Harness
) -> None:
    unknown = await add_device(db, driver_key="not-a-shipped-driver", name="Not shipped")
    working = await add_device(db, driver_key="stub", name="Working")

    await harness.manager.start()

    record = harness.manager.status(unknown.id)
    assert record is not None and record.status == "error"
    assert record.kind == "config"
    assert "not-a-shipped-driver" in (record.detail or "")
    good = await harness.manager.wait_for_connection(working.id)
    assert good is not None and good.status == "connected"
    assert harness.manager._runtimes[unknown.id].task is None  # nothing was started for it


async def test_invalid_stored_config_does_not_prevent_start(
    db: Database, harness: Harness
) -> None:
    broken = await add_device(
        db,
        config={"transport": {"type": "loopback"}, "driver": {"input_count": 99}},
        name="Too many inputs",
    )
    working = await add_device(db, driver_key="stub", name="Working")

    await harness.manager.start()

    record = harness.manager.status(broken.id)
    assert record is not None and record.status == "error"
    assert record.kind == "config"
    assert "input_count" in (record.detail or "")
    good = await harness.manager.wait_for_connection(working.id)
    assert good is not None and good.status == "connected"


async def test_a_disabled_row_is_unconfigured_and_starts_no_task(
    db: Database, harness: Harness
) -> None:
    device = await add_device(db, enabled=False)

    await harness.manager.start()

    record = harness.manager.status(device.id)
    assert record is not None and record.status == "unconfigured"
    assert record.kind is None
    # No supervised task exists for a row that is turned off.
    assert harness.manager._runtimes[device.id].task is None


async def test_reload_restarts_the_device_from_the_current_row(
    db: Database, harness: Harness
) -> None:
    device = await add_device(db)
    await harness.manager.start()
    await harness.manager.wait_for_connection(device.id)

    await devices_crud.update(
        db,
        device.id,
        device.updated_at,
        config={"transport": {"type": "loopback"}, "driver": {"output_count": 8}},
    )
    await harness.manager.reload(device.id)
    await harness.manager.wait_for_connection(device.id)

    report = await harness.manager.capabilities(device.id)
    assert report.as_connected is True
    assert report.capabilities == MatrixCapabilities(4, 8, supports_atomic_route=True)


async def test_capabilities_fall_back_to_the_declared_maximum(
    db: Database, harness: Harness
) -> None:
    device = await add_device(db, enabled=False)
    await harness.manager.start()

    report = await harness.manager.capabilities(device.id)

    assert report.as_connected is False  # nothing is running to ask
    assert report.capabilities == MatrixCapabilities(4, 2, supports_atomic_route=True)


async def test_stop_cancels_every_task_and_closes_the_transports(
    db: Database, harness: Harness
) -> None:
    device = await add_device(db)
    await harness.manager.start()
    await harness.manager.wait_for_connection(device.id)
    driver = harness.manager.running_driver(device.id)
    assert driver is not None
    transport = driver.transport
    assert isinstance(transport, LoopbackTransport)

    await harness.manager.stop()

    assert transport.is_open is False
    assert harness.manager.status(device.id) is None


async def test_deleting_a_row_removes_its_state_record(db: Database, harness: Harness) -> None:
    device = await add_device(db)
    await harness.manager.start()
    await harness.manager.wait_for_connection(device.id)

    await devices_crud.delete(db, device.id)
    await harness.manager.reload(device.id)

    assert harness.state.devices.record("hdmi") is None
    assert harness.manager.state_key(device.id) is None


async def test_held_paths_name_the_device_holding_each_serial_port(
    db: Database, harness: Harness
) -> None:
    await add_device(
        db,
        driver_key="stub",
        name="Matrix",
        config={
            "transport": {"type": "loopback", "device_path": "/dev/serial/by-id/usb-FTDI"},
            "driver": {},
        },
    )
    await harness.manager.start()

    assert harness.manager.held_paths() == {"/dev/serial/by-id/usb-FTDI": "Matrix"}


# -- test (§5.3's two stages) ---------------------------------------------------


async def test_test_reports_connect_and_probe_separately(db: Database, harness: Harness) -> None:
    await harness.manager.start()

    good = await harness.manager.test(Category.VIDEO_MATRIX, "stub", LOOPBACK)
    silent = await harness.manager.test(Category.VIDEO_MATRIX, "silent", LOOPBACK)
    refused = await harness.manager.test(Category.VIDEO_MATRIX, "refuse", LOOPBACK)

    assert (good.connect.ok, good.probe.ok, good.ok) == (True, True, True)
    assert (silent.connect.ok, silent.probe.ok) == (True, False)
    assert "did not reply" in silent.message
    assert silent.probe.detail == "no reply to PAXXR"
    assert (refused.connect.ok, refused.probe.ok) == (False, False)
    assert refused.probe.attempted is False
    assert "Check the address" in refused.message


async def test_an_unexpected_driver_fault_is_contained(db: Database, harness: Harness) -> None:
    """A driver that raises is logged and restarted, not left to kill the task."""

    class ExplodingDriver(_Fast, Driver):
        key = "explode"
        category = Category.VIDEO_MATRIX
        name = "Exploding test driver"
        SUPPORTED_TRANSPORTS = ["loopback"]
        CONFIG_SCHEMA: ClassVar[list[Field]] = []

        async def probe(self) -> ProbeResult:
            raise RuntimeError("the driver is broken")

        def capabilities(self) -> MatrixCapabilities:
            return MatrixCapabilities(1, 1, supports_atomic_route=False)

    registry.DRIVERS[(Category.VIDEO_MATRIX, "explode")] = ExplodingDriver
    device = await add_device(db, driver_key="explode")
    await harness.manager.start()
    await settle(0.1)

    record = harness.manager.status(device.id)
    assert record is not None and record.status == "error"
    assert "driver fault" in (record.detail or "")
    runtime = harness.manager._runtimes[device.id]
    assert runtime.task is not None and not runtime.task.done()  # still supervised
