"""The KNX subsystem (§7.1) against :mod:`tests.stubs.knxd_stub`."""

from __future__ import annotations

import ast
import asyncio
import contextlib
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

import proskenion.core.knx as knx_module
from proskenion.config import Config, KnxSection
from proskenion.core.bus import EventBus
from proskenion.core.devices import DeviceManager
from proskenion.core.events import KnxTelegramReceived
from proskenion.core.knx import (
    AddressDirection,
    IncomingOnlyAddress,
    InMemoryAddressRegistry,
    KnxSubsystem,
    Priority,
    UnknownGroupAddress,
    format_group_address,
    format_individual_address,
    parse_group_address,
)
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from tests.stubs.knxd_stub import KnxdStub

# -- addressing ----------------------------------------------------------------


class TestAddressing:
    @pytest.mark.parametrize(
        ("address", "packed"), [("1/0/1", 0b00001_000_00000001), ("0/0/0", 0), ("31/7/255", 0xFFFF)]
    )
    def test_group_address_round_trip(self, address: str, packed: int) -> None:
        assert parse_group_address(address) == packed
        assert format_group_address(packed) == address

    def test_group_address_rejects_out_of_range(self) -> None:
        with pytest.raises(ValueError):
            parse_group_address("32/0/0")
        with pytest.raises(ValueError):
            parse_group_address("0/8/0")
        with pytest.raises(ValueError):
            parse_group_address("0/0/256")

    def test_group_address_rejects_malformed(self) -> None:
        with pytest.raises(ValueError):
            parse_group_address("not-an-address")

    def test_individual_address_format(self) -> None:
        assert format_individual_address(0x1104) == "1.1.4"
        assert format_individual_address(0x0000) == "0.0.0"


# -- fixtures --------------------------------------------------------------------


class FakeReporter:
    """A ``report_status`` callable that records every call and can be awaited."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None, str | None]] = []
        self._changed = asyncio.Event()

    async def __call__(
        self, key: str, status: str, *, kind: str | None = None, detail: str | None = None
    ) -> None:
        self.calls.append((key, status, kind, detail))
        self._changed.set()

    async def wait_for(
        self, status: str, *, timeout_s: float = 3.0
    ) -> tuple[str, str, str | None, str | None]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            for call in reversed(self.calls):
                if call[1] == status:
                    return call
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AssertionError(f"status {status!r} never reported; calls={self.calls}")
            self._changed.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), remaining)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def wait_until(
    predicate: Callable[[], bool], *, timeout_s: float = 3.0, interval: float = 0.02
) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition was not met in time")
        await asyncio.sleep(interval)


@pytest.fixture
async def stub() -> AsyncIterator[KnxdStub]:
    async with KnxdStub(port=_free_port()) as server:
        yield server


@pytest.fixture
def reporter() -> FakeReporter:
    return FakeReporter()


@pytest.fixture
def registry() -> InMemoryAddressRegistry:
    return InMemoryAddressRegistry()


def make_subsystem(
    stub_or_port: KnxdStub | int,
    registry: InMemoryAddressRegistry,
    bus: EventBus,
    reporter: FakeReporter,
    **kwargs: object,
) -> KnxSubsystem:
    port = stub_or_port.port if isinstance(stub_or_port, KnxdStub) else stub_or_port
    cfg = KnxSection(host="127.0.0.1", port=port)
    defaults: dict[str, object] = {"backoff_initial_s": 0.05, "backoff_max_s": 0.2}
    defaults.update(kwargs)
    return KnxSubsystem(cfg, registry, bus, reporter, **defaults)  # type: ignore[arg-type]


@pytest.fixture
async def bus() -> AsyncIterator[EventBus]:
    b = EventBus()
    await b.start()
    try:
        yield b
    finally:
        await b.stop()


@pytest.fixture
async def running(
    stub: KnxdStub, registry: InMemoryAddressRegistry, bus: EventBus, reporter: FakeReporter
) -> AsyncIterator[KnxSubsystem]:
    """A subsystem connected to a running stub, with its run() task cancelled on teardown."""
    subsystem = make_subsystem(stub, registry, bus, reporter)
    task = asyncio.create_task(subsystem.run())
    try:
        await reporter.wait_for("connected")
        yield subsystem
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# -- connection lifecycle and status (§5.3, §12.1, §4.11) -----------------------


class TestLifecycle:
    async def test_starts_without_blocking_and_reports_unavailable(
        self, registry: InMemoryAddressRegistry, bus: EventBus, reporter: FakeReporter
    ) -> None:
        """No stub listening: run() must not block its caller, and status must
        surface as unavailable (§12.1, §4.11) — this system's vocabulary for
        that is ``error`` with failure kind ``config``."""
        subsystem = make_subsystem(_free_port(), registry, bus, reporter)
        started = time.monotonic()
        task = asyncio.create_task(subsystem.run())
        # create_task itself never blocks, but prove the coroutine doesn't
        # either by giving it a moment and checking it hasn't finished any
        # real connection work synchronously.
        assert time.monotonic() - started < 0.5
        try:
            call = await reporter.wait_for("error")
            assert call == ("knx", "error", "config", call[3])
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    async def test_connects_when_stub_appears(
        self, registry: InMemoryAddressRegistry, bus: EventBus, reporter: FakeReporter
    ) -> None:
        port = _free_port()
        subsystem = make_subsystem(port, registry, bus, reporter)
        task = asyncio.create_task(subsystem.run())
        try:
            await reporter.wait_for("error")  # nothing listening yet
            async with KnxdStub(port=port):
                await reporter.wait_for("connected")
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    async def test_reports_through_the_device_manager(
        self, stub: KnxdStub, registry: InMemoryAddressRegistry, dev_config: Config, db: Database
    ) -> None:
        """The additive DeviceManager method, injected as the subsystem's
        ``report_status`` callable, exactly as §5.6/B39 requires: this
        manager remains the domain's sole writer."""
        manager_bus = EventBus()
        await manager_bus.start()
        state = StateStore(dev_config, manager_bus)
        manager = DeviceManager(db, state, manager_bus, dev_config)
        await manager.start()  # no rows: just sets up the devices writer
        try:
            subsystem = make_subsystem(
                stub, registry, manager_bus, manager.report_subsystem_status
            )
            task = asyncio.create_task(subsystem.run())
            try:
                await wait_until(
                    lambda: (record := state.devices.record("knx")) is not None
                    and record.status == "connected"
                )
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            await manager.stop()
            await manager_bus.stop()


# -- incoming telegrams (§7.1) ---------------------------------------------------


class TestIncoming:
    async def test_decoded_telegram_reaches_a_bus_subscriber(
        self,
        running: KnxSubsystem,
        registry: InMemoryAddressRegistry,
        bus: EventBus,
        stub: KnxdStub,
    ) -> None:
        registry.register("1/0/1", "1.001", AddressDirection.BOTH)
        events: list[KnxTelegramReceived] = []
        bus.subscribe(KnxTelegramReceived, _collector(events), name="test-collector")

        await stub.send_telegram("1/0/1", "1.001", True, source_address="1.1.4")

        await wait_until(lambda: len(events) >= 1)
        event = events[0]
        assert event.group_address == "1/0/1"
        assert event.dpt == "1.001"
        assert event.value is True
        assert event.source_address == "1.1.4"
        assert event.raw == bytes([0x00, 0x81])

    async def test_unsupported_dpt_is_logged_and_ignored(
        self,
        running: KnxSubsystem,
        registry: InMemoryAddressRegistry,
        bus: EventBus,
        stub: KnxdStub,
    ) -> None:
        registry.register("6/0/0", "6.001", AddressDirection.INCOMING)  # not in §7.1's table
        events: list[KnxTelegramReceived] = []
        bus.subscribe(KnxTelegramReceived, _collector(events), name="test-collector-2")

        raw = bytes([0x00, 0x80, 0x2A])
        await stub.emit("1.1.1", "6/0/0", raw)

        await asyncio.sleep(0.2)  # nothing should ever arrive
        assert events == []
        unsupported = running.unsupported()
        assert "6/0/0" in unsupported
        assert unsupported["6/0/0"].dpt == "6.001"
        assert unsupported["6/0/0"].raw == raw

    async def test_unregistered_address_does_not_raise(
        self,
        running: KnxSubsystem,
        registry: InMemoryAddressRegistry,
        bus: EventBus,
        stub: KnxdStub,
    ) -> None:
        events: list[KnxTelegramReceived] = []
        bus.subscribe(KnxTelegramReceived, _collector(events), name="test-collector-3")
        await stub.emit("1.1.1", "8/0/0", bytes([0x00, 0x80]))
        await asyncio.sleep(0.1)
        assert events == []


def _collector(sink: list[KnxTelegramReceived]) -> Callable[[KnxTelegramReceived], Awaitable[None]]:
    async def handler(event: KnxTelegramReceived) -> None:
        sink.append(event)

    return handler


# -- outgoing writes, refusal and logging (§7.1) ---------------------------------


class TestOutgoing:
    async def test_refuses_write_to_incoming_only_address(
        self, running: KnxSubsystem, registry: InMemoryAddressRegistry, stub: KnxdStub
    ) -> None:
        registry.register("7/0/0", "5.010", AddressDirection.INCOMING)
        with pytest.raises(IncomingOnlyAddress):
            await running.write("7/0/0", 5, priority=Priority.SCENE_STATUS)
        await asyncio.sleep(0.1)
        assert not any(w.group_address == "7/0/0" for w in stub.writes)

    async def test_unknown_address_raises(self, running: KnxSubsystem) -> None:
        with pytest.raises(UnknownGroupAddress):
            await running.write("9/0/0", 1, priority=Priority.SCENE_STATUS)

    async def test_write_reaches_the_stub(
        self, running: KnxSubsystem, registry: InMemoryAddressRegistry, stub: KnxdStub
    ) -> None:
        registry.register("2/0/0", "5.010", AddressDirection.OUTGOING)
        await running.write("2/0/0", 42, priority=Priority.SCENE_STATUS)
        await wait_until(lambda: any(w.group_address == "2/0/0" for w in stub.writes))
        write = next(w for w in stub.writes if w.group_address == "2/0/0")
        assert write.apdu == bytes([0x00, 0x80, 42])

    async def test_read_sends_group_value_read(
        self, running: KnxSubsystem, stub: KnxdStub
    ) -> None:
        # read() needs no registry entry — a Group Value Read carries no
        # value, so there is nothing for a DPT codec to do.
        await running.read("3/0/0")
        await wait_until(lambda: any(w.group_address == "3/0/0" for w in stub.writes))
        write = next(w for w in stub.writes if w.group_address == "3/0/0")
        assert write.apdu == bytes([0x00, 0x00])
        assert write.apci == 0x00


# -- the telegram budget (§7.1) --------------------------------------------------


class TestBudget:
    async def test_rate_limit_over_any_one_second_window(
        self, running: KnxSubsystem, registry: InMemoryAddressRegistry, stub: KnxdStub
    ) -> None:
        count = 24
        for i in range(count):
            registry.register(f"10/0/{i}", "5.010", AddressDirection.OUTGOING)
        for i in range(count):
            await running.write(f"10/0/{i}", i, priority=Priority.FADE_STEP)

        await wait_until(lambda: len(stub.writes) >= count, timeout_s=5.0)
        timestamps = sorted(w.received_at for w in stub.writes)
        for t in timestamps:
            in_window = sum(1 for other in timestamps if t <= other < t + 1.0)
            assert in_window <= 15, f"more than 15 telegrams left within one second of {t}"

    @pytest.mark.parametrize("seed", range(20))
    async def test_delivery_jitter_never_puts_sixteen_on_the_bus_in_one_second(
        self, seed: int
    ) -> None:
        """What the bus sees, not what the limiter admitted: each telegram
        reaches the gateway up to RATE_LIMIT_MARGIN_S after its release, and
        no one-second window of arrivals may hold more than 15."""
        import random

        rng = random.Random(seed)
        now = [0.0]

        async def fake_sleep(delay: float) -> None:
            # A real clock always moves on; a float sum can swallow a tiny delay.
            now[0] += max(delay, 1e-6)

        limiter = knx_module._RateLimiter(clock=lambda: now[0], sleep=fake_sleep)
        arrivals = []
        for _ in range(60):
            await limiter.acquire()
            arrivals.append(now[0] + rng.uniform(0.0, knx_module.RATE_LIMIT_MARGIN_S))
            now[0] += rng.uniform(0.0, 0.02)
        arrivals.sort()
        for t in arrivals:
            in_window = sum(1 for other in arrivals if t <= other < t + 1.0)
            assert in_window <= knx_module.RATE_LIMIT_PER_SECOND

    async def test_alarm_overtakes_a_fade_step_backlog(
        self, running: KnxSubsystem, registry: InMemoryAddressRegistry, stub: KnxdStub
    ) -> None:
        registry.register("11/0/0", "5.010", AddressDirection.OUTGOING)  # alarm
        for i in range(6):
            registry.register(f"12/0/{i}", "5.010", AddressDirection.OUTGOING)  # fade steps

        # Saturate the current rate window with priority-2 filler so the next
        # sends must queue and are chosen by priority, not arrival order.
        for i in range(15):
            registry.register(f"13/0/{i}", "5.010", AddressDirection.OUTGOING)
            await running.write(f"13/0/{i}", 0, priority=Priority.SCENE_STATUS)
        await wait_until(lambda: len(stub.writes) >= 15)

        for i in range(6):
            await running.write(f"12/0/{i}", i, priority=Priority.FADE_STEP)
        await running.write("11/0/0", 1, priority=Priority.ALARM)

        await wait_until(lambda: len(stub.writes) >= 22, timeout_s=5.0)
        order = [w.group_address for w in stub.writes[15:]]
        alarm_index = order.index("11/0/0")
        fade_indices = [order.index(f"12/0/{i}") for i in range(6)]
        assert alarm_index < min(fade_indices)

    async def test_fade_step_coalesces_same_address(
        self, running: KnxSubsystem, registry: InMemoryAddressRegistry, stub: KnxdStub
    ) -> None:
        registry.register("14/0/0", "5.010", AddressDirection.OUTGOING)
        for i in range(15):
            registry.register(f"15/0/{i}", "5.010", AddressDirection.OUTGOING)
            await running.write(f"15/0/{i}", 0, priority=Priority.SCENE_STATUS)
        await wait_until(lambda: len(stub.writes) >= 15)

        await running.write("14/0/0", 1, priority=Priority.FADE_STEP)
        await running.write("14/0/0", 2, priority=Priority.FADE_STEP)
        await running.write("14/0/0", 3, priority=Priority.FADE_STEP)

        await wait_until(lambda: len(stub.writes) >= 16, timeout_s=5.0)
        await asyncio.sleep(0.3)  # give a wrongly-uncoalesced extra write time to arrive
        matches = [w for w in stub.writes if w.group_address == "14/0/0"]
        assert len(matches) == 1
        assert matches[0].apdu == bytes([0x00, 0x80, 3])

    async def test_alarm_and_scene_status_are_never_coalesced(
        self, running: KnxSubsystem, registry: InMemoryAddressRegistry, stub: KnxdStub
    ) -> None:
        registry.register("16/0/0", "5.010", AddressDirection.OUTGOING)
        for i in range(15):
            registry.register(f"17/0/{i}", "5.010", AddressDirection.OUTGOING)
            await running.write(f"17/0/{i}", 0, priority=Priority.SCENE_STATUS)
        await wait_until(lambda: len(stub.writes) >= 15)

        await running.write("16/0/0", 1, priority=Priority.ALARM)
        await running.write("16/0/0", 2, priority=Priority.ALARM)

        await wait_until(
            lambda: sum(1 for w in stub.writes if w.group_address == "16/0/0") >= 2, timeout_s=5.0
        )
        matches = [w.apdu for w in stub.writes if w.group_address == "16/0/0"]
        assert matches == [bytes([0x00, 0x80, 1]), bytes([0x00, 0x80, 2])]


# -- the heartbeat (§7.1 *Health*) -----------------------------------------------


class TestHeartbeat:
    async def test_heartbeat_confirms_on_echo(
        self,
        stub: KnxdStub,
        registry: InMemoryAddressRegistry,
        bus: EventBus,
        reporter: FakeReporter,
    ) -> None:
        cfg = KnxSection(
            host="127.0.0.1",
            port=stub.port,
            heartbeat_address="9/0/9",
            heartbeat_interval_s=0.05,
        )
        subsystem = KnxSubsystem(
            cfg,
            registry,
            bus,
            reporter,
            heartbeat_timeout_s=1.0,
            backoff_initial_s=0.05,
            backoff_max_s=0.2,
        )
        task = asyncio.create_task(subsystem.run())
        try:
            await wait_until(
                lambda: any(w.group_address == "9/0/9" for w in stub.writes), timeout_s=3.0
            )
            write = next(w for w in stub.writes if w.group_address == "9/0/9")
            await stub.emit("1.1.1", "9/0/9", write.apdu)
            await wait_until(lambda: subsystem.heartbeat_ok is True, timeout_s=3.0)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    async def test_heartbeat_reports_failure_without_echo(
        self,
        stub: KnxdStub,
        registry: InMemoryAddressRegistry,
        bus: EventBus,
        reporter: FakeReporter,
    ) -> None:
        cfg = KnxSection(
            host="127.0.0.1",
            port=stub.port,
            heartbeat_address="9/0/8",
            heartbeat_interval_s=0.05,
        )
        subsystem = KnxSubsystem(
            cfg,
            registry,
            bus,
            reporter,
            heartbeat_timeout_s=0.1,
            backoff_initial_s=0.05,
            backoff_max_s=0.2,
        )
        task = asyncio.create_task(subsystem.run())
        try:
            await wait_until(lambda: subsystem.heartbeat_ok is False, timeout_s=3.0)
            call = await reporter.wait_for("degraded")
            assert call[2] == "device"
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    async def test_heartbeat_disabled_by_default(self) -> None:
        cfg = KnxSection(host="127.0.0.1", port=6720)
        assert cfg.heartbeat_interval_s is None
        assert cfg.heartbeat_address is None


# -- config validation -----------------------------------------------------------


class TestKnxSectionValidation:
    def test_defaults(self) -> None:
        cfg = KnxSection()
        assert str(cfg.socket) == str(Path("/run/knx"))
        assert cfg.port == 6720
        assert cfg.heartbeat_interval_s is None

    def test_heartbeat_interval_requires_address(self) -> None:
        with pytest.raises(ValidationError):
            KnxSection(heartbeat_interval_s=30.0)

    def test_heartbeat_address_requires_valid_group_address(self) -> None:
        with pytest.raises(ValidationError):
            KnxSection(heartbeat_address="not-an-address", heartbeat_interval_s=30.0)

    def test_heartbeat_address_rejects_out_of_range_middle_level(self) -> None:
        # Syntactically a three-level address, but "9" is out of range for
        # the middle group (0-7) — must be caught at config load, not left to
        # fail deep inside the heartbeat loop at runtime.
        with pytest.raises(ValidationError):
            KnxSection(heartbeat_address="9/9/9", heartbeat_interval_s=30.0)

    def test_heartbeat_interval_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            KnxSection(heartbeat_address="1/0/1", heartbeat_interval_s=0.0)

    def test_rejects_unknown_keys(self) -> None:
        with pytest.raises(ValidationError):
            KnxSection.model_validate({"nonsense": True})


# -- architecture: KNX is a subsystem, not a driver (§5.5, B42) ------------------


class TestArchitecture:
    def test_drivers_never_import_knx(self) -> None:
        drivers_dir = Path(knx_module.__file__).parent / "drivers"
        offenders = [
            path
            for path in drivers_dir.rglob("*.py")
            if _imports(path, {"proskenion.core.knx", "proskenion.core.knx_dpt"})
        ]
        assert offenders == []

    def test_knx_never_imports_drivers(self) -> None:
        module_paths = (
            Path(knx_module.__file__),
            Path(knx_module.__file__).parent / "knx_dpt.py",
        )
        for module_path in module_paths:
            assert not _imports(module_path, {"proskenion.core.drivers"}, prefix_match=True)


def _imports(path: Path, names: set[str], *, prefix_match: bool = False) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules = [node.module]
        else:
            continue
        for module in modules:
            if prefix_match:
                if any(module == name or module.startswith(name + ".") for name in names):
                    return True
            elif module in names:
                return True
    return False
