"""The CQ-20B MIDI driver (§7.3), through the real TCP transport against the
independent :mod:`tests.stubs.cq_midi_stub`.

Expected bytes are written out from ``docs/protocols/cq20b.md`` and the PDF
pages it cites; the stub's own address knowledge is used to judge what reached
a mute, so neither side of a comparison is the driver's own table.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest

from proskenion.core.drivers import registry
from proskenion.core.drivers.base import DeviceStatus
from proskenion.core.drivers.capabilities import ChannelState, MixerChange
from proskenion.core.drivers.categories import Category
from proskenion.core.drivers.cq20b import (
    MSG_OFFLINE,
    MSG_REFUSED,
    CQ20BDriver,
)
from proskenion.core.drivers.cq20b_midi import REFS
from proskenion.core.transport.base import ConfigurationError
from proskenion.core.transport.tcp import TcpTransport
from tests.stubs.cq_midi_stub import MUTE_ADDRESSES, CqMidiStub
from tests.stubs.echo_driver import RecordingSink

# Addresses, from cq20b.md §3, §5 and §6.
IP1_LEVEL, IP1_MUTE, IP1_PAN = (0x40, 0x00), (0x00, 0x00), (0x50, 0x00)
IP2_LEVEL = (0x40, 0x01)
IP5_LEVEL = (0x40, 0x04)
MAIN_LEVEL, MAIN_MUTE = (0x4F, 0x00), (0x00, 0x44)
OUT1_LEVEL, OUT1_MUTE = (0x4F, 0x01), (0x00, 0x45)
ST2_LEVEL, ST2_MUTE, ST2_PAN = (0x40, 0x1A), (0x00, 0x1A), (0x50, 0x1A)

GET = bytes.fromhex("B0 63 00 B0 62 00 B0 60 7F")


def hx(text: str) -> bytes:
    return bytes.fromhex(text)


async def until(condition: Callable[[], bool], limit: float = 3.0) -> None:
    async with asyncio.timeout(limit):
        while not condition():  # noqa: ASYNC110 - conditions span the stub and the driver
            await asyncio.sleep(0.005)


async def settle(seconds: float = 0.1) -> None:
    """Long enough on loopback for an echo or push to reach the driver."""
    await asyncio.sleep(seconds)


class FakeClock:
    """A controllable clock: ``sleep`` advances ``now`` by exactly the delay."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay
        await asyncio.sleep(0)


class RecordingTcp(TcpTransport):
    """The real TCP transport, noting each ``send`` call and when, by ``clock``."""

    def __init__(self, config: dict[str, Any], clock: Callable[[], float]) -> None:
        super().__init__(config)
        self.clock = clock
        self.sent: list[tuple[float, bytes]] = []

    async def send(self, data: bytes) -> None:
        self.sent.append((self.clock(), data))
        await super().send(data)


@dataclass
class Rig:
    driver: CQ20BDriver
    sink: RecordingSink
    transport: RecordingTcp
    changes: list[MixerChange] = field(default_factory=list)


def make_driver(
    stub_port: int,
    *,
    tracked: tuple[str, ...] = ("ip1",),
    config: dict[str, Any] | None = None,
    clock: FakeClock | None = None,
) -> Rig:
    sink = RecordingSink()
    transport = RecordingTcp(
        {"host": "127.0.0.1", "port": stub_port}, clock if clock is not None else time.monotonic
    )
    driver = CQ20BDriver(1, transport, {"metering": False, **(config or {})}, sink)
    if clock is not None:
        driver._sleep = clock.sleep
        driver._clock = clock
    driver.set_tracked(tracked)
    rig = Rig(driver, sink, transport)

    async def record(change: MixerChange) -> None:
        rig.changes.append(change)

    driver.add_change_listener(record)
    return rig


@asynccontextmanager
async def connected(
    stub: CqMidiStub,
    *,
    tracked: tuple[str, ...] = ("ip1",),
    config: dict[str, Any] | None = None,
    clock: FakeClock | None = None,
) -> AsyncIterator[Rig]:
    rig = make_driver(stub.port, tracked=tracked, config=config, clock=clock)
    task = asyncio.create_task(rig.driver.run())
    try:
        await until(lambda: rig.driver.syncs_completed >= 1)
        yield rig
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def gets(stub: CqMidiStub) -> list[tuple[int, int] | None]:
    return [m.address for m in stub.messages_of("get")]


# -- registration ---------------------------------------------------------------


def test_registered_as_mixer_cq20b_on_port_51325() -> None:
    assert registry.get(Category.MIXER, "cq20b") is CQ20BDriver
    assert CQ20BDriver.TRANSPORT_DEFAULTS == {"tcp": {"port": 51325}}
    assert CQ20BDriver.SUPPORTED_TRANSPORTS == ["tcp"]
    fields = {f.key: f for f in CQ20BDriver.CONFIG_SCHEMA}
    assert set(fields) == {"recall_wait_ms", "metering", "meter_udp_port"}
    assert fields["recall_wait_ms"].default == 300
    assert fields["metering"].type == "bool" and fields["metering"].default is True
    assert fields["meter_udp_port"].type == "port" and fields["meter_udp_port"].default == 51327


# -- connecting and syncing -------------------------------------------------------


async def test_connecting_reads_main_then_outputs_then_inputs_and_writes_nothing() -> None:
    state = {MAIN_LEVEL: 10048, MAIN_MUTE: 1, IP1_LEVEL: 12544, ST2_PAN: 16383}
    async with CqMidiStub(state=state) as stub:
        async with connected(stub, tracked=("st2", "ip1", "out12")) as rig:
            assert gets(stub) == [
                MAIN_LEVEL, MAIN_MUTE,
                OUT1_LEVEL, OUT1_MUTE,
                IP1_LEVEL, IP1_MUTE, IP1_PAN,
                ST2_LEVEL, ST2_MUTE, ST2_PAN,
            ]  # fmt: skip
            assert {m.kind for m in stub.messages} == {"get"}
            assert len(stub.received) == 10 * len(GET)
            for start in range(0, len(stub.received), len(GET)):
                group = bytes(stub.received[start : start + len(GET)])
                assert group[:2] == b"\xb0\x63" and group[3:5] == b"\xb0\x62"
                assert group[6:] == b"\xb0\x60\x7f"
            state_now = rig.driver.known_state(["main", "ip1", "st2"])
            assert state_now["main"] == ChannelState(db=-5.0, muted=True, pan=None)
            assert state_now["ip1"] == ChannelState(db=0.0, muted=False, pan=0.0)
            assert state_now["st2"] == ChannelState(db=None, muted=False, pan=1.0)


async def test_a_sync_reports_sync_only_for_what_changed() -> None:
    async with CqMidiStub(state={IP1_LEVEL: 12544}) as stub:
        async with connected(stub) as rig:
            assert MixerChange("ip1", "level", 0.0, "sync") in rig.changes
            rig.changes.clear()
            await rig.driver.read_state(["ip1"])
            assert rig.changes == []
            stub.state[IP1_LEVEL] = 5952  # changed on the desk, not announced
            await rig.driver.read_state(["ip1"])
            assert rig.changes == [MixerChange("ip1", "level", -20.0, "sync")]


async def test_a_reset_reconnects_and_resyncs_writing_nothing() -> None:
    async with CqMidiStub(state={IP1_LEVEL: 12544}) as stub:
        async with connected(stub, tracked=("ip1", "out3")) as rig:
            await rig.driver.set_level(["ip1"], -10.0)
            await rig.driver.set_mute(["out3"], True)
            await until(lambda: len(stub.messages_of("set")) == 2)
            stub.state[IP1_LEVEL] = 5952  # the desk comes back with another value
            stub.clear_record()
            rig.changes.clear()
            # A drop of a connection that has already had a reply is a
            # genuine drop, unaffected by Finding 1 (cq20b.md §16): it must
            # still go through the ordinary reconnect-and-resync path below,
            # never read as another client holding the slot.
            await stub.reset_connection()
            await until(lambda: rig.driver.syncs_completed >= 2)

            assert stub.connections == 2
            assert {m.kind for m in stub.messages} == {"get"}
            for start in range(0, len(stub.received), len(GET)):
                assert bytes(stub.received[start + 6 : start + 9]) == b"\xb0\x60\x7f"
            assert rig.changes == [MixerChange("ip1", "level", -20.0, "sync")]
            connected_reports = [s for s, _k in rig.sink.statuses if s is DeviceStatus.CONNECTED]
            assert len(connected_reports) == 2
            assert rig.driver.amber_failure is False
            assert all(k != "device" for _s, k in rig.sink.statuses)


# -- exclusivity ---------------------------------------------------------------------


async def _instant(_delay: float) -> None:
    await asyncio.sleep(0)


async def test_refused_is_amber_with_the_exact_message_then_recovers() -> None:
    async with CqMidiStub() as stub:
        # MixPad on the wired port, holding the one MIDI connection.
        _reader, interloper = await asyncio.open_connection("127.0.0.1", stub.port)
        await until(lambda: stub.connected and not stub.listening)

        rig = make_driver(stub.port)
        rig.driver._sleep = _instant
        task = asyncio.create_task(rig.driver.run())
        try:
            await until(lambda: len(rig.sink.reports) >= 2, limit=10.0)
            _, status, kind, detail = rig.sink.reports[1]
            assert (status, kind, detail) == (DeviceStatus.ERROR, "device", MSG_REFUSED)
            assert detail == (
                "Another MIDI client is connected. "
                "Check that MixPad is using the CQ's WiFi, not Ethernet."
            )
            assert rig.driver.amber_failure is True

            interloper.close()
            await until(lambda: rig.driver.syncs_completed >= 1, limit=10.0)
            assert rig.driver.amber_failure is False
            assert (DeviceStatus.CONNECTED, None) in rig.sink.statuses
            assert all(k != "config" for _s, k in rig.sink.statuses)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_a_second_connection_is_accepted_then_reset_and_reads_amber_not_red() -> None:
    """Bench, 21 September 2026 (cq20b.md §1, §16, Finding 1): the desk does
    not refuse a second MIDI/TCP connection at the OS level — it accepts it
    and resets it immediately, and the incumbent is untouched. Before the fix,
    ``connect()`` only classified a failure of ``transport.open()`` itself, so
    this accept-then-reset went unclassified there and surfaced later as the
    desk being offline (red) rather than another client holding its one MIDI
    slot (amber) — the exact distinction §7.3 asks for."""
    async with CqMidiStub(state={IP1_LEVEL: 12544}) as stub:
        stub.reset_second_connection = True
        async with connected(stub) as incumbent:
            second = make_driver(stub.port)
            second.driver._sleep = _instant
            task = asyncio.create_task(second.driver.run())
            try:
                await until(lambda: second.driver.amber_failure is True, limit=10.0)
                # Never the red message, at any point along the way there.
                assert all(
                    detail != MSG_OFFLINE
                    for _id, status, kind, detail in second.sink.reports
                    if status is DeviceStatus.ERROR and kind == "device"
                )
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

            # The incumbent never noticed: still connected, still working.
            incumbent.changes.clear()
            await incumbent.driver.set_level(["ip1"], -6.0)
            await until(lambda: stub.value(IP1_LEVEL) == 9600)  # -6 dB, cq20b.md §4
            assert incumbent.driver.amber_failure is False


async def test_timed_out_is_red_mixer_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def never_answers(*_args: Any, **_kwargs: Any) -> Any:
        await asyncio.sleep(3600)

    monkeypatch.setattr(asyncio, "open_connection", never_answers)
    rig = make_driver(9)  # nothing listens; the connect hangs until the timeout
    rig.transport.CONNECT_TIMEOUT = 0.05  # type: ignore[misc]
    with pytest.raises(ConfigurationError) as caught:
        await rig.driver.connect()
    assert str(caught.value) == "Mixer offline."
    assert isinstance(caught.value.__cause__, ConfigurationError)
    assert isinstance(caught.value.__cause__.__cause__, TimeoutError)

    rig.driver._sleep = _instant
    task = asyncio.create_task(rig.driver.run())
    try:
        await until(lambda: len(rig.sink.reports) >= 2)
        _, status, kind, detail = rig.sink.reports[1]
        assert (status, kind, detail) == (DeviceStatus.ERROR, "config", MSG_OFFLINE)
        assert rig.driver.amber_failure is False
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# -- the codec on the wire -------------------------------------------------------------


async def test_writes_on_the_wire_are_the_pdf_examples() -> None:
    cases: list[tuple[Callable[[CQ20BDriver], Awaitable[object]], str]] = [
        # cq20b.md §5 / PDF p.8
        (lambda d: d.set_mute(["ip1"], True), "B0 63 00 B0 62 00 B0 06 00 B0 26 01"),
        (lambda d: d.set_mute(["main"], False), "B0 63 00 B0 62 44 B0 06 00 B0 26 00"),
        # cq20b.md §2 / PDF p.9
        (lambda d: d.set_level(["ip1"], 0.0), "B0 63 40 B0 62 00 B0 06 62 B0 26 00"),
        (lambda d: d.set_level(["ip1"], -20.0), "B0 63 40 B0 62 00 B0 06 2E B0 26 40"),
        (lambda d: d.set_level(["out56"], 5.0), "B0 63 4F B0 62 05 B0 06 73 B0 26 40"),
        # PDF p.11
        (lambda d: d.set_pan("ip1", 0.0), "B0 63 50 B0 62 00 B0 06 40 B0 26 00"),
        # PDF p.10
        (lambda d: d.step_level(["ip1"], up=True), "B0 63 40 B0 62 00 B0 60 00"),
        (lambda d: d.step_level(["ip1"], up=False), "B0 63 40 B0 62 00 B0 61 00"),
        # cq20b.md §7 / PDF p.6
        (lambda d: d.recall_scene("1"), "B0 00 00 C0 00"),
        (lambda d: d.recall_scene("7"), "B0 00 00 C0 06"),
        (lambda d: d.recall_scene("64"), "B0 00 00 C0 3F"),
    ]
    async with CqMidiStub(recall_delay=0.0) as stub:
        async with connected(stub, config={"recall_wait_ms": 0}) as rig:
            for call, expected in cases:
                stub.clear_record()
                await call(rig.driver)
                await until(lambda e=expected: len(stub.received) >= len(hx(e)))
                assert bytes(stub.received).startswith(hx(expected)), expected


async def test_one_intent_is_one_write_and_a_linked_pair_shares_its_odd_output() -> None:
    async with CqMidiStub() as stub:
        async with connected(stub) as rig:
            rig.transport.sent.clear()
            await rig.driver.set_level(["ip1", "ip2", "out12", "out1"], -5.0)
            ((_, data),) = rig.transport.sent
            assert data == (
                hx("B0 63 40 B0 62 00 B0 06 4E B0 26 40")
                + hx("B0 63 40 B0 62 01 B0 06 4E B0 26 40")
                + hx("B0 63 4F B0 62 01 B0 06 4E B0 26 40")  # out12 and out1: 4F 01, once
            )


async def test_unknown_references_and_bad_values_send_nothing() -> None:
    async with CqMidiStub() as stub:
        async with connected(stub) as rig:
            rig.transport.sent.clear()
            with pytest.raises(KeyError):
                await rig.driver.set_level(["ip1", "ip17"], 0.0)
            with pytest.raises(KeyError):
                await rig.driver.set_mute(["out7"], True)
            with pytest.raises(ValueError):
                await rig.driver.set_pan("main", 0.0)  # §7.3: pan is inputs to Main LR
            for scene in ("0", "129", "three"):
                with pytest.raises(ValueError):
                    await rig.driver.recall_scene(scene)
            with pytest.raises(KeyError):
                rig.driver.set_tracked(["ip1", "dca1"])
            assert rig.driver.tracked == frozenset({"ip1"})
            assert rig.transport.sent == []


# -- mute safety ----------------------------------------------------------------------

#: Every public coroutine of the driver this test drives. The §5.3 lifecycle
#: methods are exercised too, by connecting and by the reset below.
EXERCISED = {"set_level", "set_mute", "set_pan", "step_level", "recall_scene", "read_state"}
LIFECYCLE = {"connect", "disconnect", "probe", "maintain", "run", "probe_periodically"}
NO_IO = {"validate_config"}


def test_the_mute_safety_test_covers_every_public_coroutine() -> None:
    public = {
        name
        for name, member in inspect.getmembers(CQ20BDriver, inspect.iscoroutinefunction)
        if not name.startswith("_")
    }
    assert public == EXERCISED | LIFECYCLE | NO_IO, public - (EXERCISED | LIFECYCLE | NO_IO)


def _scan(data: bytes) -> list[tuple[tuple[int, int], int, int]]:
    """Every controller message with the NRPN address latched when it arrived:
    ``(address, controller, value)``. Independent of the driver's and the
    stub's parsers. The driver sends a status byte on every message, so any
    other shape is itself a failure."""
    out: list[tuple[tuple[int, int], int, int]] = []
    msb = lsb = -1
    index = 0
    while index < len(data):
        status = data[index]
        if status == 0xC0:
            index += 2
            continue
        assert status == 0xB0, f"unexpected byte {status:#04x} at {index}"
        controller, value = data[index + 1], data[index + 2]
        index += 3
        if controller == 0x63:
            msb = value
        elif controller == 0x62:
            lsb = value
        else:
            out.append(((msb, lsb), controller, value))
    return out


async def test_nothing_any_public_method_sends_can_toggle_a_mute() -> None:
    every_ref = tuple(REFS)
    async with CqMidiStub(recall_delay=0.0) as stub:
        async with connected(stub, tracked=every_ref, config={"recall_wait_ms": 10}) as rig:
            driver = rig.driver
            for ref in every_ref:
                await driver.set_level([ref], 0.0)
                await driver.set_level([ref], None)
                await driver.set_mute([ref], True)
                await driver.set_mute([ref], False)
                if REFS[ref].pan is not None:
                    await driver.set_pan(ref, -0.5)
                await driver.step_level([ref], up=True)
                await driver.step_level([ref], up=False)
                await driver.read_state([ref])
            await driver.set_level(list(every_ref), -10.0)
            await driver.set_mute(list(every_ref), True)
            await driver.step_level(list(every_ref), up=True)
            await driver.recall_scene("1")
            await driver.read_state(list(every_ref))
            await stub.reset_connection()
            await until(lambda: driver.syncs_completed >= 2 + len(every_ref) + 2)
            await settle()

    scanned = _scan(bytes(stub.received))
    to_mutes = [(a, c, v) for a, c, v in scanned if a in MUTE_ADDRESSES]
    steps = [(a, c, v) for a, c, v in scanned if c in (0x60, 0x61) and v != 0x7F]
    assert steps, "the test must have sent relative steps to prove anything"
    for address, controller, value in to_mutes:
        if controller in (0x60, 0x61):
            assert (controller, value) == (0x60, 0x7F), f"{address}: {controller:#x} {value:#x}"
        elif controller == 0x06:
            assert value == 0x00, address
        elif controller == 0x26:
            assert value in (0x00, 0x01), address
        else:
            raise AssertionError(f"unexpected controller {controller:#x} to mute {address}")
    assert {a for a, c, _v in to_mutes if c == 0x26} == set(MUTE_ADDRESSES)
    assert stub.mute_toggles == []


# -- the inbound stream -----------------------------------------------------------------


async def test_chunked_input_with_running_status_parses() -> None:
    async with CqMidiStub() as stub:
        async with connected(stub) as rig:
            rig.changes.clear()
            stub.chunk_size = 1
            stub.running_status = True
            await stub.push(IP1_LEVEL, 5952)
            await stub.push(IP1_MUTE, 1)
            stub.chunk_size = 5
            await stub.push(IP1_PAN, 0)
            await until(lambda: len(rig.changes) == 3)
            assert rig.changes == [
                MixerChange("ip1", "level", -20.0, "external"),
                MixerChange("ip1", "mute", True, "external"),
                MixerChange("ip1", "pan", -1.0, "external"),
            ]


async def test_a_message_split_across_tcp_writes_parses() -> None:
    async with CqMidiStub() as stub:
        async with connected(stub) as rig:
            rig.changes.clear()
            message = hx("B0 63 40 B0 62 00 B0 06 3E B0 26 00")  # ip1, -10 dB
            await stub.send_raw(message[:4])
            await settle(0.05)
            await stub.send_raw(message[4:10])
            await settle(0.05)
            await stub.send_raw(message[10:])
            await until(lambda: len(rig.changes) == 1)
            assert rig.changes == [MixerChange("ip1", "level", -10.0, "external")]


# -- change origin --------------------------------------------------------------------


async def test_our_own_write_is_reported_once_as_app_and_its_echo_is_silent() -> None:
    async with CqMidiStub() as stub:
        async with connected(stub) as rig:
            rig.changes.clear()
            await rig.driver.set_level(["ip1"], -10.0)
            await rig.driver.set_mute(["ip1"], True)
            await until(lambda: len(stub.messages_of("set")) == 2)
            await settle()
            assert rig.changes == [
                MixerChange("ip1", "level", -10.0, "app"),
                MixerChange("ip1", "mute", True, "app"),
            ]


async def test_a_late_echo_of_an_older_write_does_not_undo_a_newer_one() -> None:
    async with CqMidiStub(echo=False) as stub:
        async with connected(stub) as rig:
            await rig.driver.set_level(["ip1"], -10.0)
            await rig.driver.set_level(["ip1"], -5.0)
            rig.changes.clear()
            await stub.push(IP1_LEVEL, 7936)  # the echo of -10 dB, arriving late
            await stub.push(IP1_LEVEL, 10048)  # the echo of -5 dB
            await settle()
            assert rig.changes == []
            assert rig.driver.known_state(["ip1"])["ip1"].db == -5.0


async def test_mixpad_changes_are_external() -> None:
    async with CqMidiStub() as stub:
        async with connected(stub) as rig:
            rig.changes.clear()
            await stub.push(IP1_LEVEL, 5952)
            await stub.push(IP1_MUTE, 1)
            await stub.push(IP1_PAN, 16383)
            await stub.push(IP1_PAN, 16383)  # the same value again: no change
            await until(lambda: len(rig.changes) >= 3)
            await settle()
            assert rig.changes == [
                MixerChange("ip1", "level", -20.0, "external"),
                MixerChange("ip1", "mute", True, "external"),
                MixerChange("ip1", "pan", 1.0, "external"),
            ]


async def test_a_relative_step_lands_as_app_not_external() -> None:
    async with CqMidiStub(state={IP1_LEVEL: 12544}) as stub:
        async with connected(stub) as rig:
            rig.changes.clear()
            await rig.driver.step_level(["ip1"], up=True)
            await until(lambda: len(rig.changes) == 1)
            assert rig.changes == [MixerChange("ip1", "level", 1.0, "app")]
            assert stub.value(IP1_LEVEL) == 12992


async def test_untracked_references_are_discarded_silently() -> None:
    async with CqMidiStub() as stub:
        async with connected(stub, tracked=("ip1",)) as rig:
            rig.changes.clear()
            await stub.push(IP2_LEVEL, 5952)
            await settle()
            assert rig.changes == []
            assert rig.driver.known_state(["ip2"]) == {}

            rig.driver.set_tracked(["ip1", "ip2"])
            await stub.push(IP2_LEVEL, 7936)
            await until(lambda: len(rig.changes) == 1)
            assert rig.changes == [MixerChange("ip2", "level", -10.0, "external")]


async def test_read_state_reads_an_untracked_reference_for_that_exchange_only() -> None:
    async with CqMidiStub(state={IP5_LEVEL: 7936}) as stub:
        async with connected(stub, tracked=("ip1",)) as rig:
            read = await rig.driver.read_state(["ip5"])
            assert read == {"ip5": ChannelState(db=-10.0, muted=False, pan=0.0)}
            rig.changes.clear()
            await stub.push(IP5_LEVEL, 5952)
            await settle()
            assert rig.changes == []


async def test_a_linked_pair_is_reported_under_its_own_reference() -> None:
    async with CqMidiStub() as stub:
        async with connected(stub, tracked=("out12",)) as rig:
            rig.changes.clear()
            await stub.push(OUT1_LEVEL, 12544)
            await until(lambda: len(rig.changes) == 1)
            assert rig.changes == [MixerChange("out12", "level", 0.0, "external")]


async def test_a_listener_that_raises_does_not_stop_the_others() -> None:
    async with CqMidiStub() as stub:
        async with connected(stub) as rig:

            async def broken(_change: MixerChange) -> None:
                raise RuntimeError("a listener bug")

            rig.driver._listeners.insert(0, broken)
            rig.changes.clear()
            await stub.push(IP1_LEVEL, 5952)
            await stub.push(IP1_LEVEL, 7936)
            await until(lambda: len(rig.changes) == 2)


async def test_what_a_recall_of_ours_changes_is_sync_and_a_later_push_is_external() -> None:
    presets = {4: {IP1_LEVEL: 5952, IP1_MUTE: 1}}
    async with CqMidiStub(presets=presets, recall_delay=0.05, announce_recall=True) as stub:
        async with connected(stub, config={"recall_wait_ms": 200}) as rig:
            rig.changes.clear()
            await rig.driver.recall_scene("4")
            # The desk announced both changes during the wait: still the recall's doing.
            assert rig.changes == [
                MixerChange("ip1", "level", -20.0, "sync"),
                MixerChange("ip1", "mute", True, "sync"),
            ]
            rig.changes.clear()
            await stub.push(IP1_LEVEL, 7936)
            await until(lambda: len(rig.changes) == 1)
            assert rig.changes == [MixerChange("ip1", "level", -10.0, "external")]


async def test_a_push_during_a_sync_for_an_address_not_queried_is_external() -> None:
    inputs = [r for r, i in REFS.items() if i.kind == "input" and r != "ip2"]
    async with CqMidiStub() as stub:
        async with connected(stub, tracked=("ip1", "ip2")) as rig:
            rig.changes.clear()
            sync = asyncio.create_task(rig.driver.read_state(inputs))
            await until(lambda: len(stub.messages_of("get")) >= 3)
            await stub.push(IP2_LEVEL, 5952)  # MixPad, mid-sync, on a channel not being read
            await sync
            assert MixerChange("ip2", "level", -20.0, "external") in rig.changes


# -- pacing and the recall wait ------------------------------------------------------------


async def test_sync_queries_are_paced_5_to_10_ms_apart() -> None:
    clock = FakeClock()
    inputs = tuple(r for r, i in REFS.items() if i.kind == "input")
    async with CqMidiStub() as stub:
        async with connected(stub, tracked=inputs, clock=clock) as rig:
            # The connection's own sync.
            times = [t for t, data in rig.transport.sent if data.endswith(b"\xb0\x60\x7f")]
            assert len(times) == 2 + 3 * len(inputs)
            gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
            assert all(0.005 <= gap <= 0.010 for gap in gaps), gaps

            rig.transport.sent.clear()
            await rig.driver.read_state(list(inputs))
            times = [t for t, _data in rig.transport.sent]
            gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
            assert len(times) == 3 * len(inputs)
            assert all(0.005 <= gap <= 0.010 for gap in gaps), gaps


async def test_a_recall_waits_the_configured_time_before_its_queries() -> None:
    for wait_ms in (300, 750):
        clock = FakeClock()
        async with CqMidiStub(recall_delay=0.0) as stub:
            async with connected(stub, config={"recall_wait_ms": wait_ms}, clock=clock) as rig:
                rig.transport.sent.clear()
                await rig.driver.recall_scene("3")
                (recall_at, recall), (first_get_at, first_get), *_ = rig.transport.sent
                assert recall == hx("B0 00 00 C0 02")
                assert first_get == hx("B0 63 4F B0 62 00 B0 60 7F")  # Main first
                assert first_get_at - recall_at == pytest.approx(wait_ms / 1000)


async def test_the_default_recall_wait_is_300_ms() -> None:
    clock = FakeClock()
    async with CqMidiStub(recall_delay=0.0) as stub:
        async with connected(stub, clock=clock) as rig:
            rig.transport.sent.clear()
            await rig.driver.recall_scene("3")
            (recall_at, _), (first_get_at, _), *_ = rig.transport.sent
            assert first_get_at - recall_at == pytest.approx(0.300)


async def test_after_the_wait_the_queries_return_the_new_scene() -> None:
    presets = {3: {IP1_LEVEL: 5952, MAIN_MUTE: 1}}
    async with CqMidiStub(
        state={IP1_LEVEL: 12544}, presets=presets, recall_delay=0.2, announce_recall=False
    ) as stub:
        async with connected(stub) as rig:
            rig.changes.clear()
            await rig.driver.recall_scene("3")
            (recall,) = stub.messages_of("recall")
            first_get = next(m for m in stub.messages if m.kind == "get" and m.at > recall.at)
            assert first_get.at - recall.at >= 0.3 - 0.02
            assert rig.driver.known_state(["ip1"])["ip1"].db == -20.0
            assert MixerChange("ip1", "level", -20.0, "sync") in rig.changes


async def test_without_the_wait_the_queries_return_the_outgoing_scene() -> None:
    """The control for the test above: the wait is what makes the difference."""
    presets = {3: {IP1_LEVEL: 5952}}
    async with CqMidiStub(
        state={IP1_LEVEL: 12544}, presets=presets, recall_delay=0.2, announce_recall=False
    ) as stub:
        async with connected(stub, config={"recall_wait_ms": 0}) as rig:
            await rig.driver.recall_scene("3")
            assert rig.driver.known_state(["ip1"])["ip1"].db == 0.0


# -- concurrency: each of these fails without the driver's lock ---------------------------


async def test_a_write_during_a_recall_waits_for_the_resync_and_is_not_lost() -> None:
    presets = {2: {IP1_LEVEL: 5952}}
    async with CqMidiStub(presets=presets, recall_delay=0.1, announce_recall=False) as stub:
        async with connected(stub) as rig:
            stub.clear_record()
            recall = asyncio.create_task(rig.driver.recall_scene("2"))
            await until(lambda: bool(stub.messages_of("recall")))
            await rig.driver.set_level(["ip1"], -5.0)
            await recall
            await until(lambda: bool(stub.messages_of("set")))

            kinds = [m.kind for m in stub.messages]
            assert kinds[0] == "recall" and kinds[-1] == "set"
            assert set(kinds[1:-1]) == {"get"}
            assert stub.value(IP1_LEVEL) == 10048  # -5 dB: not overwritten by the scene


async def test_two_syncs_never_interleave_their_queries() -> None:
    first, second = ("ip1", "ip2", "ip3"), ("ip4", "ip5", "ip6")
    async with CqMidiStub() as stub:
        async with connected(stub, tracked=()) as rig:
            stub.clear_record()
            await asyncio.gather(
                rig.driver.read_state(list(first)), rig.driver.read_state(list(second))
            )
            lsbs = [a[1] for a in gets(stub) if a is not None]
            assert lsbs == [0, 0, 0, 1, 1, 1, 2, 2, 2, 3, 3, 3, 4, 4, 4, 5, 5, 5]


async def test_a_write_during_a_sync_goes_out_whole_after_its_queries() -> None:
    inputs = [r for r, i in REFS.items() if i.kind == "input"]
    async with CqMidiStub() as stub:
        async with connected(stub, tracked=()) as rig:
            stub.clear_record()
            sync = asyncio.create_task(rig.driver.read_state(inputs))
            await until(lambda: len(stub.messages_of("get")) >= 3)
            await rig.driver.set_mute(["ip1"], True)
            await sync
            await until(lambda: bool(stub.messages_of("set")))

            kinds = [m.kind for m in stub.messages]
            assert kinds == ["get"] * (3 * len(inputs)) + ["set"]
            assert stub.value(IP1_MUTE) == 1


# -- capabilities and enumeration -----------------------------------------------------------


class _Meters:
    def __init__(self, available: bool) -> None:
        self.available = available


async def test_capabilities_follow_the_meter_source_after_connecting() -> None:
    async with CqMidiStub() as stub:
        rig = make_driver(stub.port)
        declared = rig.driver.capabilities()
        assert declared.supports_metering and declared.meter_min_db == -60.0
        assert declared.supports_scene_recall and declared.supports_pan

        async with connected(stub) as rig:
            caps = rig.driver.capabilities()
            assert caps.supports_scene_recall and caps.supports_pan and caps.supports_mute
            assert not caps.supports_metering
            assert caps.meter_min_db is None and caps.meter_point is None

            meters = _Meters(available=True)
            rig.driver._meter_source = meters
            caps = rig.driver.capabilities()
            assert caps.supports_metering
            assert (caps.meter_min_db, caps.meter_max_db) == (-60.0, 10.0)

            meters.available = False
            assert not rig.driver.capabilities().supports_metering
            assert rig.driver.capabilities().supports_scene_recall

            assert caps.input_count == 20 and caps.output_count == 6
            assert (caps.min_db, caps.max_db) == (-89.0, 10.0)
            assert not caps.supports_gain and not caps.supports_dca


def test_available_refs_label_every_reference() -> None:
    rig = make_driver(9)
    refs = {r.ref: r for r in rig.driver.available_refs()}
    assert list(refs)[:16] == [f"ip{n}" for n in range(1, 17)]
    assert set(refs) == {
        *(f"ip{n}" for n in range(1, 17)),
        "st1", "st2", "usb", "bt", "main",
        *(f"out{n}" for n in range(1, 7)),
        "out12", "out34", "out56",
    }  # fmt: skip
    assert (refs["ip1"].label, refs["ip1"].kind, refs["ip1"].stereo) == ("Input 1", "input", False)
    assert (refs["st2"].kind, refs["st2"].stereo) == ("input", True)
    assert (refs["main"].label, refs["main"].kind, refs["main"].stereo) == ("Main LR", "main", True)
    assert (refs["out12"].label, refs["out12"].kind) == ("Out 1/2 (linked)", "output")
    assert refs["out12"].stereo and not refs["out1"].stereo


def test_desk_channels_are_every_channel_once_with_the_linked_pairs_as_cover() -> None:
    """Twenty inputs, Main LR and six outputs, in ``available_refs()`` order;
    each linked pair covers its two outputs and is not a channel itself."""
    desk = make_driver(9).driver.desk_channels()
    assert [d.ref.ref for d in desk] == [
        *(f"ip{n}" for n in range(1, 17)),
        "st1", "st2", "usb", "bt", "main",
        *(f"out{n}" for n in range(1, 7)),
    ]  # fmt: skip
    covered = {d.ref.ref: d.covered_by for d in desk if d.covered_by}
    assert covered == {
        "out1": ("out12",),
        "out2": ("out12",),
        "out3": ("out34",),
        "out4": ("out34",),
        "out5": ("out56",),
        "out6": ("out56",),
    }


def test_the_fader_law_is_published_with_a_unity_detent() -> None:
    law = make_driver(9).driver.fader_law()
    assert law[0].db is None and law[0].position == 0.0
    (unity,) = [p for p in law if p.detent]
    assert unity.db == 0.0 and unity.position == pytest.approx(12544 / 16320, abs=1e-4)
