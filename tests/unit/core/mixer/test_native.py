"""The CQ-20B native metering client (§7.3), against the real TCP-and-UDP
transport and the independent :mod:`tests.stubs.cq_native_stub`."""

from __future__ import annotations

import ast
import asyncio
import inspect
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from proskenion.core.mixer import native
from proskenion.core.mixer.native import (
    INPUT_REFS,
    MSG_REFUSED,
    OUTPUT_REFS,
    NativeMeterClient,
    _raw_to_db,
)
from proskenion.core.transport.tcp import TcpTransport
from tests.stubs.cq_native_stub import CqNativeStub


class _Recorder:
    """Records every ``on_meters``/``on_availability`` call and wakes
    whoever is waiting on :attr:`progress` — mirrors
    ``tests/unit/core/drivers/test_pjlink.py``'s ``_SleepRecorder``/
    ``_wait_for`` pairing, applied to callbacks instead of sleeps."""

    def __init__(self) -> None:
        self.meter_calls: list[dict[str, float | None]] = []
        self.availability: list[tuple[bool, str | None]] = []
        self.progress = asyncio.Event()

    async def on_meters(self, levels: dict[str, float | None]) -> None:
        self.meter_calls.append(dict(levels))
        self.progress.set()

    async def on_availability(self, available: bool, reason: str | None) -> None:
        self.availability.append((available, reason))
        self.progress.set()


async def _wait_for(
    progress: asyncio.Event, condition: Callable[[], bool], limit: float = 2.0
) -> None:
    async with asyncio.timeout(limit):
        while not condition():
            await progress.wait()
            progress.clear()


async def _poll_until(condition: Callable[[], bool], limit: float = 2.0) -> None:
    """Poll ``condition`` until it holds, or raise past ``limit`` seconds —
    the same shape as ``tests/unit/api/test_hdmi.py``'s ``_wait_until``."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def _client(port: int, recorder: _Recorder) -> NativeMeterClient:
    transport = TcpTransport({"host": "127.0.0.1", "port": port})
    client = NativeMeterClient(
        transport, on_meters=recorder.on_meters, on_availability=recorder.on_availability
    )
    # A five-second protocol timeout and a three-second keep-alive are the
    # right production values (cq20b-native.md §2, §9) but would make every
    # failure-path test slow; tests that specifically exercise the keep-alive
    # cadence or the backoff sequence override these again themselves.
    client.HANDSHAKE_TIMEOUT = 0.5
    return client


# -- handshake, client-init and keep-alive cadence (cq20b-native.md §2, §9) ----------


async def test_handshake_and_client_init_complete_and_report_available() -> None:
    async with CqNativeStub(meter_interval=0.5) as stub:
        recorder = _Recorder()
        client = _client(stub.port, recorder)
        await client.start()
        try:
            await _wait_for(recorder.progress, lambda: recorder.availability)
        finally:
            await client.stop()
        assert stub.handshakes == 1
        assert stub.client_inits == 1
        assert recorder.availability == [(True, None)]
        assert client.available is True


async def test_keepalive_sent_on_its_own_cadence() -> None:
    async with CqNativeStub(meter_interval=1.0) as stub:
        recorder = _Recorder()
        client = _client(stub.port, recorder)
        client.KEEPALIVE_INTERVAL = 0.05  # cq20b-native.md §9's 3 s, scaled for the test
        await client.start()
        try:
            await _poll_until(lambda: len(stub.keepalives_received) >= 3)
            # A stable snapshot, taken before stop(): a keep-alive already on
            # the wire when cancellation begins can still be delivered to the
            # stub a moment later, so reading the live list again afterwards
            # is a race — see the loop's own use of _sleep for what "already
            # sent" means here.
            received = list(stub.keepalives_received)
        finally:
            await client.stop()
        # Consecutive keep-alives land close to the configured interval, not
        # in a burst — loose bounds because CI schedulers are not real-time.
        # strict=False: pairing consecutive elements is deliberately one
        # shorter on the right, not a length mismatch to guard against.
        gaps = [b - a for a, b in zip(received, received[1:], strict=False)]
        assert all(0.0 < gap < 0.5 for gap in gaps)


async def test_first_keepalive_is_sent_immediately_after_the_handshake() -> None:
    """A firewall correctness requirement, not just a protocol nicety: an
    ephemeral local port relied on this client's own first outbound packet
    to form the conntrack entry that admits the mixer's reply traffic, and
    meters begin arriving at §2 step 7 — one step *before* the keep-alive of
    step 8 — so any delay here was a window where real meters would have
    been dropped even with the port fixed and the firewall correct
    otherwise. Interval deliberately long, so a wait-for-the-interval bug
    would make this test time out rather than pass by chance."""
    async with CqNativeStub(meter_interval=0.5) as stub:
        recorder = _Recorder()
        client = _client(stub.port, recorder)
        client.KEEPALIVE_INTERVAL = 5.0
        started_at = time.monotonic()
        await client.start()
        try:
            await _poll_until(lambda: stub.keepalives_received, limit=1.0)
        finally:
            await client.stop()
        assert stub.keepalives_received[0] - started_at < 1.0


# -- the fixed local UDP port (firewall correctness) ----------------------------------


async def test_binds_the_default_local_udp_port_not_an_ephemeral_one() -> None:
    async with CqNativeStub(meter_interval=0.5) as stub:
        recorder = _Recorder()
        client = _client(stub.port, recorder)
        await client.start()
        try:
            await _wait_for(recorder.progress, lambda: recorder.availability)
            assert client._udp_transport is not None  # noqa: SLF001 - internal check
            bound_port = client._udp_transport.get_extra_info("sockname")[1]
        finally:
            await client.stop()
        assert bound_port == native.DEFAULT_LOCAL_UDP_PORT


async def test_local_udp_port_is_a_constructor_parameter() -> None:
    chosen = native.DEFAULT_LOCAL_UDP_PORT + 1
    async with CqNativeStub(meter_interval=0.5) as stub:
        recorder = _Recorder()
        transport = TcpTransport({"host": "127.0.0.1", "port": stub.port})
        client = NativeMeterClient(
            transport,
            on_meters=recorder.on_meters,
            on_availability=recorder.on_availability,
            local_udp_port=chosen,
        )
        client.HANDSHAKE_TIMEOUT = 0.5
        await client.start()
        try:
            await _wait_for(recorder.progress, lambda: recorder.availability)
            assert client._udp_transport is not None  # noqa: SLF001 - internal check
            bound_port = client._udp_transport.get_extra_info("sockname")[1]
        finally:
            await client.stop()
        assert bound_port == chosen
        assert bound_port != native.DEFAULT_LOCAL_UDP_PORT


# -- meter frames parsed into the right references (cq20b-native.md §4, §5, §7) ------


async def test_meters_parsed_into_the_right_references_with_matching_values() -> None:
    async with CqNativeStub(meter_interval=0.02) as stub:
        recorder = _Recorder()
        client = _client(stub.port, recorder)
        await client.start()
        try:
            await _poll_until(lambda: any(len(c) == len(INPUT_REFS) for c in recorder.meter_calls))
            await _poll_until(
                lambda: any(len(c) == len(OUTPUT_REFS) for c in recorder.meter_calls)
            )
        finally:
            await client.stop()

        inputs = next(c for c in recorder.meter_calls if len(c) == len(INPUT_REFS))
        outputs = next(c for c in recorder.meter_calls if len(c) == len(OUTPUT_REFS))

        # Every reference §4/§5's record maps predict, and no others.
        assert set(inputs) == set(INPUT_REFS)
        assert set(outputs) == set(OUTPUT_REFS)

        # Values traced by hand against tools/session.log's first type-8 and
        # type-9 lines (also tests/stubs/cq_native_stub.py's SAMPLE_INPUT_BODY
        # and SAMPLE_OUTPUT_BODY docstrings): record 1 (Ip2) is an active
        # microphone, ST1 R and ST2 L/R are active from an AUX/HDMI feed, the
        # FX returns are unpatched (raw 0), and Main L/R are active outputs.
        assert inputs["ip1"] == pytest.approx(_raw_to_db(4608))
        assert inputs["ip2"] == pytest.approx(_raw_to_db(21082))
        assert inputs["ip3"] == pytest.approx(_raw_to_db(4608))
        assert inputs["st1l"] == pytest.approx(_raw_to_db(4608))
        assert inputs["st1r"] == pytest.approx(_raw_to_db(7377))
        assert inputs["st2l"] == pytest.approx(_raw_to_db(15825))
        assert inputs["st2r"] == pytest.approx(_raw_to_db(15825))
        assert inputs["usbl"] == pytest.approx(_raw_to_db(4608))
        assert inputs["fx1l"] == pytest.approx(_raw_to_db(0))
        assert inputs["fx4r"] == pytest.approx(_raw_to_db(0))

        assert outputs["out1"] == pytest.approx(_raw_to_db(4608))
        assert outputs["out6"] == pytest.approx(_raw_to_db(4608))
        assert outputs["mainl"] == pytest.approx(_raw_to_db(20151))
        assert outputs["mainr"] == pytest.approx(_raw_to_db(20190))


async def test_meter_levels_are_dbfs_style_floats_never_used_for_control() -> None:
    """Sanity check on the unit contract the class docstring states: every
    delivered value is a plain ``float`` (never, say, a raw ``int`` or a
    string), and this client never reads one back — it only ever hands
    values forward to the callback (see the structural test below for the
    stronger, whole-module version of that claim)."""
    async with CqNativeStub(meter_interval=0.02) as stub:
        recorder = _Recorder()
        client = _client(stub.port, recorder)
        await client.start()
        try:
            await _poll_until(lambda: recorder.meter_calls)
        finally:
            await client.stop()
        levels = recorder.meter_calls[0]
        assert levels
        assert all(isinstance(v, float) for v in levels.values())


# -- the slot-full refusal (bench, 2026-09-11; cq20b-native.md §9) -------------------


async def test_slot_full_refusal_reports_the_clear_specific_message() -> None:
    async with CqNativeStub(max_connections=0) as stub:
        recorder = _Recorder()
        client = _client(stub.port, recorder)
        await client.start()
        try:
            await _wait_for(recorder.progress, lambda: recorder.availability)
        finally:
            await client.stop()
        assert recorder.availability == [(False, MSG_REFUSED)]
        assert client.available is False


async def test_garbled_handshake_reply_is_also_treated_as_refused() -> None:
    """§9's bench notes found no way to tell a slot-full refusal apart from
    any other failure before the client-init completes, only that the same
    explanation applies either way — so an unrecognised reply gets the same
    status as an outright refusal, not a different, less useful one."""
    async with CqNativeStub() as stub:
        stub.send_garbage = True
        recorder = _Recorder()
        client = _client(stub.port, recorder)
        await client.start()
        try:
            await _wait_for(recorder.progress, lambda: recorder.availability)
        finally:
            await client.stop()
        assert recorder.availability == [(False, MSG_REFUSED)]


# -- backoff and recovery, this connection's own retry state -------------------------


async def test_backoff_doubles_then_recovers_once_a_slot_frees() -> None:
    async with CqNativeStub(max_connections=0, meter_interval=0.02) as stub:
        recorder = _Recorder()
        client = _client(stub.port, recorder)
        client.INITIAL_RETRY_DELAY = 5.0
        client.MAX_RETRY_DELAY = 60.0
        client._retry_delay = client.INITIAL_RETRY_DELAY  # the constructor already cached it
        delays: list[float] = []

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)
            await asyncio.sleep(0)  # yield, but do not really wait

        client._sleep = fake_sleep  # type: ignore[method-assign]  # documented test hook

        await client.start()
        try:
            await _poll_until(lambda: len(delays) >= 3)
            # 5, 10, 20 — doubling from INITIAL_RETRY_DELAY, capped at
            # MAX_RETRY_DELAY (neither reached yet at the third attempt).
            assert delays[:3] == [5.0, 10.0, 20.0]
            assert recorder.availability[-1] == (False, MSG_REFUSED)

            stub.max_connections = 1  # a MixPad slot frees (bench §9)
            await _wait_for(
                recorder.progress, lambda: recorder.availability[-1] == (True, None)
            )
        finally:
            await client.stop()
        assert client.available is True


async def test_retry_delay_caps_at_max_retry_delay() -> None:
    async with CqNativeStub(max_connections=0) as stub:
        recorder = _Recorder()
        client = _client(stub.port, recorder)
        client.INITIAL_RETRY_DELAY = 1.0
        client.MAX_RETRY_DELAY = 3.0
        client._retry_delay = client.INITIAL_RETRY_DELAY  # the constructor already cached it
        delays: list[float] = []

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)
            await asyncio.sleep(0)

        client._sleep = fake_sleep  # type: ignore[method-assign]

        await client.start()
        try:
            await _poll_until(lambda: len(delays) >= 5)
        finally:
            await client.stop()
        assert delays[:5] == [1.0, 2.0, 3.0, 3.0, 3.0]


# -- garbage over UDP does not crash the session --------------------------------------


async def test_garbled_udp_datagram_is_discarded_and_metering_continues() -> None:
    async with CqNativeStub(meter_interval=0.02) as stub:
        recorder = _Recorder()
        client = _client(stub.port, recorder)
        await client.start()
        try:
            await _poll_until(lambda: recorder.meter_calls)
            recorder.meter_calls.clear()
            stub.send_udp_garbage()
            await _poll_until(lambda: recorder.meter_calls)
        finally:
            await client.stop()
        assert client.available is True


# -- the availability callback fires only on a change (§7.3 *Failure is degradation,
#    not loss*) -------------------------------------------------------------------


async def test_availability_callback_fires_only_on_each_transition() -> None:
    async with CqNativeStub(meter_interval=0.02) as stub:
        recorder = _Recorder()
        client = _client(stub.port, recorder)
        client.INITIAL_RETRY_DELAY = 0.02
        client.MAX_RETRY_DELAY = 0.05
        client._retry_delay = client.INITIAL_RETRY_DELAY  # the constructor already cached it
        await client.start()
        try:
            await _wait_for(recorder.progress, lambda: recorder.availability == [(True, None)])

            await stub.drop_all_connections()
            await _wait_for(
                recorder.progress,
                lambda: len(recorder.availability) >= 2 and recorder.availability[1][0] is False,
            )
            await _wait_for(
                recorder.progress,
                lambda: len(recorder.availability) >= 3
                and recorder.availability[2] == (True, None),
            )
            # Give the loop a moment to have retried at least once more —
            # nothing further should have been reported, because nothing
            # changed: still connected, still available.
            await asyncio.sleep(0.1)
        finally:
            await client.stop()

        assert [a for a, _ in recorder.availability] == [True, False, True]
        lost_reason = recorder.availability[1][1]
        assert lost_reason is not None and "lost" in lost_reason


# -- structural: nothing here writes to the mixer's control state --------------------
#
# Mirrors tests/unit/core/dmx/test_observed_isolation.py's approach: rather
# than trust that no future edit adds a state write, every identifier and
# every non-docstring string literal in the module's own source is scanned
# for the word "state" — this module has no legitimate reason to say it, since
# it never touches proskenion.core.state at all (§7.3 *Meters never enter the
# control path*, B58; CONVENTIONS.md's "Meters never enter the control path").


def _code_identifiers(source_path: Path) -> list[str]:
    """Every name, attribute, argument and non-docstring string literal in a
    module's source."""
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    identifiers: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.append(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.append(node.attr)
        elif isinstance(node, ast.arg):
            identifiers.append(node.arg)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            identifiers.append(node.value)
    return identifiers


def _native_source_path() -> Path:
    path = inspect.getsourcefile(native)
    assert path is not None
    return Path(path)


def test_native_module_never_names_state() -> None:
    offending = [s for s in _code_identifiers(_native_source_path()) if "state" in s.lower()]
    assert offending == [], f"proskenion.core.mixer.native refers to state: {offending}"


def test_native_module_imports_nothing_state_related() -> None:
    tree = ast.parse(_native_source_path().read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    offending = [name for name in imported if "state" in name.lower()]
    assert offending == [], f"native.py imports something state-related: {offending}"


def test_native_module_calls_nothing_that_looks_like_a_state_write() -> None:
    """A narrower, behavioural companion to the two tests above: even
    granting that no identifier says "state", a write could in principle
    reach the store through a differently-named alias. It cannot: every
    attribute this module calls is either this module's own method, one of
    its callback hooks, an ``asyncio``/``struct``/``logging`` primitive, or a
    method of the transport it was given
    (:class:`~proskenion.core.transport.tcp.TcpTransport`, whose public
    surface is ``open``/``close``/``send``/``receive``/``enumerate`` —
    §5.5's transport contract). None of that is a store, a domain or a
    writer, so this is an exhaustive allow-list, not a sample: any call this
    module gains to something outside it must be justified by adding it here
    with a reason, which is the point.
    """
    tree = ast.parse(_native_source_path().read_text(encoding="utf-8"))
    called_attrs = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    known_safe = {
        # §5.5's transport contract.
        "open", "close", "send", "receive", "enumerate",
        # This module's own methods.
        "_run", "_session", "_report", "_open_udp_socket", "_close_udp_socket",
        "_handshake_and_init", "_read_tcp_frame", "_serve", "_watch_tcp",
        "_read_udp", "_handle_datagram", "_keepalive", "_sleep",
        # The two callbacks the constructor is given.
        "_on_meters", "_on_availability",
        # asyncio, struct and logging primitives.
        "get_running_loop", "create_task", "create_datagram_endpoint", "TaskGroup",
        "Queue", "get", "get_nowait", "put_nowait", "full", "sendto",
        "get_extra_info", "cancel", "time", "pack", "unpack_from", "extend",
        "append", "getLogger", "info", "debug", "exception",
    }
    unexpected = called_attrs - known_safe
    assert unexpected == set(), f"unexpected call target(s) — check for a write: {unexpected}"
