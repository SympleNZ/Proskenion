"""Allen & Heath CQ-20B mixer driver, MIDI over TCP (spec §7.3, §5.5, §5.3, §12.3).

Registered as ``mixer/cq20b``. The protocol itself — addresses, the NRPN codec,
the fader and pan laws, the inbound parser — is
:mod:`proskenion.core.drivers.cq20b_midi`, taken value by value from
``docs/protocols/cq20b.md``. This module is the connection, the state, and the
ordering of what goes on the wire.

**The connection** (§7.3): TCP 51325, MIDI channel 1, held for as long as the
desk allows. The desk accepts one MIDI client at a time (cq20b.md §1, PDF p.3),
and the three ways a connection fails are told apart, with §7.3's exact words:

- **refused** — :data:`MSG_REFUSED`, amber. Another client holds the MIDI
  connection, almost always MixPad on the wired port. ``connect()`` records it
  and ``probe()`` reports it as a device failure, and :attr:`amber_failure`
  tells the device manager to show it amber.
- **timed out** (or any other failure to reach the desk) — :data:`MSG_OFFLINE`,
  red, as a configuration failure of the §5.3 run loop.
- **reset** — the desk rebooted or dropped us. The read loop ends, the run
  loop reconnects at once, and the connection's first act is a resync.

**A second client is not refused at TCP level — it is accepted and reset.**
Bench-observed on the real desk (10.2.30.248, 21 September 2026, cq20b.md
§15.3 bench question 6): with one MIDI connection open, a second one is
accepted by the TCP stack and then reset at once, while the incumbent keeps
working untouched. So a reset can mean either of two different things, and
:meth:`_loss_message` is the one place that tells them apart: a reset (or
close) that arrives before this connection has produced a single valid reply
means the slot is held elsewhere, same as an outright refusal — amber,
:data:`MSG_REFUSED`. A reset once this connection has already had a reply is
a genuine drop of a connection that was working — unchanged, the reconnection
path above, red if it persists. Both ``probe()`` and :meth:`maintain` — the
two places a lost connection is discovered, whichever happens to notice it
first — report through this same classification, so :attr:`amber_failure`,
the Devices screen, the health table and the status bar always agree.

The base driver's backoff applies to the first two unchanged (§5.3).

**Nothing is written on connecting** (§7.3 *Reconnection*, §12.3): the CQ is
authoritative for its own state. Every connection begins with a sync, which
sends ``get`` queries and nothing else, and the driver writes only when the
service calls a write method.

**State sync** (§7.3 *State synchronisation*) after connecting and after every
recall: ``get`` for Main, then the tracked outputs, then the tracked inputs —
level and mute for each, and pan for an input — one query every
:data:`SYNC_SPACING_S` (7.5 ms, inside §7.3's 5-10 ms). A recall waits the
configured ``recall_wait_ms`` (300 ms by default) before its queries, so they
do not return the outgoing scene. A sync then waits up to
:data:`SYNC_REPLY_TIMEOUT_S` for the replies, so a write made straight after it
cannot be overtaken by a reply to a query sent before it.

**How a reply is recognised is an assumption.** The PDF describes the ``get``
request and not the reply (cq20b.md §8). This driver assumes the desk answers
with the same four-message absolute value it sends for any change, for the
address queried. Nothing else is needed to apply it: a reply and an unsolicited
change are handled identically.

**Tracked references** (§7.3 *Unconfigured channels are not tracked*): the
service passes the configured references to :meth:`CQ20BDriver.set_tracked`.
An inbound value for any other reference is discarded without a trace. The one
exception is a reference being read by :meth:`CQ20BDriver.read_state` or synced,
whose replies are accepted while that exchange lasts.

**Linked outputs** (§7.3 *Linked stereo outputs*) are the admin's statement,
not the desk's: link state is not MIDI-reportable. The admin states it by
choosing a linked-pair reference — ``out12``, ``out34`` or ``out56`` — from
:meth:`CQ20BDriver.available_refs`, which the channel configuration stores as
the channel's ``driver_ref`` and the service passes on in ``set_tracked`` and
in each call. A pair is one stereo :class:`ChannelRef` and uses its odd
output's addresses, exactly as the PDF prints them (cq20b.md §3.2 and §5).

**Change origin** (§7.3 *Change origin tracking*): every change reaches the
listeners as a :class:`~proskenion.core.drivers.capabilities.MixerChange`. A
write of ours is reported once, ``origin="app"``, when it is sent. The desk
echoes every change back, including ours, so each value written is remembered
for :data:`ECHO_WINDOW_S`; an inbound value matching one is an echo and
produces nothing. An inbound value that does not differ from what the driver
last knew produces nothing either. Any other inbound value is one of two:

- ``origin="sync"`` — learned by reading the desk: the reply to a query this
  driver sent (the resync after connecting, after a recall, or in
  :meth:`CQ20BDriver.read_state`), or any value the desk sends while a recall
  this application made is loading and being read back. The service updates
  its state and shows no MixPad badge.
- ``origin="external"`` — an unsolicited push that is not an echo of our own
  write: MixPad, or the desk. This drives the MixPad badge.

A MixPad move that happens to land inside a recall's window is reported as
``"sync"``: the window is a few hundred milliseconds, and the value is applied
either way. The value a relative step lands on is unknown until the desk
reports it, so the first report for that address within the echo window is
taken as the step's result, ``origin="app"``.

**Ordering** (the lesson of both Phase 3 drivers): every group of NRPN messages
goes out in one ``transport.send``, so two coroutines never interleave bytes,
and one per-instance :class:`asyncio.Lock` orders everything that sends: a
write, a recall with its wait and its resync, a sync's paced queries and its
wait for replies. A write made during a resync waits for it and then goes out
whole; it is not lost and not interleaved, and it cannot land while a scene is
still loading, where the desk would overwrite it.

**Metering** is not on MIDI (§7.3 *Metering — two connections*). The driver
composes the native client,
:class:`~proskenion.core.mixer.native.NativeMeterClient`, on port
:data:`NATIVE_PORT` (51326) of the same host and the configured local UDP port,
unless the ``metering`` field turns it off. The client is started by the first
connection attempt of :meth:`CQ20BDriver.run` and stopped when ``run`` ends or
the driver is disconnected from outside it; it is **not** stopped when the
MIDI connection drops and the run loop reconnects. The two connections fail
independently: the native client keeps its own retry state (§7.3), a native
failure never changes this driver's status, and a MIDI failure never stops the
meters. ``supports_metering`` is true only while the client says it is
available (§5.5), through the :class:`MeterSource` protocol it satisfies.
Meter levels are relayed to :meth:`CQ20BDriver.add_meter_listener` callbacks
and never read by this driver: meters never enter the control path (B58).

A throwaway instance that is only connected and probed, such as the Devices
screen's test, never starts the native client: only :meth:`CQ20BDriver.run`
does, so a test never takes one of the desk's two MixPad slots.

**Metering availability** is reported to :meth:`CQ20BDriver.add_metering_listener`
callbacks (§7.3), fed straight from the native client's own
``on_availability`` report — the same transition :meth:`_on_meter_availability`
already logs. Additive, like :meth:`add_change_listener` and
:meth:`add_meter_listener`: nothing in :class:`~proskenion.core.drivers.categories.MixerDriver`
names it. The mixer service uses it to know the moment ``capabilities().supports_metering``
would change, rather than only discovering the change the next time something
happens to call ``capabilities()``.

**A connection's opening sync** is reported to
:meth:`CQ20BDriver.add_sync_listener` callbacks once its replies have landed
(or timed out), after the driver's lock is released so a listener may write
straight away. Only the sync that opens a connection is reported: a recall's
resync and a ``read_state`` are not. The mixer service uses it to know that
what it holds for the desk is the desk's own state again after a
reconnection — the moment hirer ceilings are re-applied
(:mod:`proskenion.core.hirer_enforcement`).

**Also §7.3's bench question 6**, answered by the Devices screen's test
button, lives in :data:`SHARES_CONNECTION` rather than here: the devices API
checks the mixer category, or a driver that declares it, before ever
building a throwaway instance, so this driver need not special-case its own
test path.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any, ClassVar, Literal, Protocol

from proskenion.core.drivers.base import (
    DeviceStatus,
    Driver,
    FirewallPorts,
    ProbeResult,
    StatusSink,
)
from proskenion.core.drivers.capabilities import (
    ChannelRef,
    ChannelState,
    DeskChannel,
    LawPoint,
    MixerCapabilities,
    MixerChange,
)
from proskenion.core.drivers.categories import Category
from proskenion.core.drivers.cq20b_midi import (
    INPUT_COUNT,
    LINKED_PAIRS,
    MAX_DB,
    MIN_DB,
    OUTPUT_COUNT,
    REFS,
    Address,
    MidiParser,
    NrpnValue,
    ParamKind,
    ProgramChange,
    RefInfo,
    db_to_value,
    encode_absolute,
    encode_get,
    encode_mute,
    encode_scene_recall,
    encode_step,
    pan_to_value,
    refs_by_address,
    resolve,
    value_to_db,
    value_to_pan,
)
from proskenion.core.drivers.cq20b_midi import fader_law as _fader_law
from proskenion.core.drivers.fields import Field
from proskenion.core.drivers.registry import register
from proskenion.core.mixer.native import DEFAULT_LOCAL_UDP_PORT, NativeMeterClient
from proskenion.core.transport.base import (
    BaseTransport,
    ConfigurationError,
    Transport,
    TransportClosed,
)
from proskenion.core.transport.tcp import TcpTransport

log = logging.getLogger(__name__)

#: "port 51325" (cq20b.md §1, PDF p.3; §7.3 *Connection*).
CQ_MIDI_PORT = 51325

#: The native protocol's port on the same desk, for metering (§7.3
#: *Metering — two connections*; cq20b-native.md).
NATIVE_PORT = 51326

#: §7.3 *Connection exclusivity*, verbatim. Refused is amber.
MSG_REFUSED = (
    "Another MIDI client is connected. Check that MixPad is using the CQ's WiFi, not Ethernet."
)
#: §7.3 *Connection exclusivity*, verbatim. Timed out is red.
MSG_OFFLINE = "Mixer offline."

#: §7.3's default wait between a recall and its queries, in milliseconds.
DEFAULT_RECALL_WAIT_MS = 300

#: Between one sync query and the next. §7.3: "spaced 5-10 ms apart".
SYNC_SPACING_S = 0.0075

#: How long a sync waits for the last of its replies before giving up on the
#: rest. The PDF says nothing about reply timing (cq20b.md §8 and §14); this
#: bounds how long a desk that never replies holds the lock.
SYNC_REPLY_TIMEOUT_S = 1.0

#: How long a value this driver wrote is remembered as a possible echo. An
#: assumption: the PDF establishes that the desk echoes (cq20b.md §1, PDF p.3)
#: but not how soon. Generous, because matching is also by address and value.
ECHO_WINDOW_S = 1.0

#: How far an echo may differ from the value written and still be an echo. An
#: assumption: that the echo is byte-identical to what was sent (cq20b.md §14).
ECHO_VALUE_TOLERANCE = 0

#: The read loop's receive timeout. A quiet desk is normal; the loop just waits
#: again. Loss of the connection is what ends it.
READ_TIMEOUT_S = 30.0

_KIND: dict[ParamKind, Literal["level", "mute", "pan"]] = {
    ParamKind.LEVEL: "level",
    ParamKind.MUTE: "mute",
    ParamKind.PAN: "pan",
}

ChangeListener = Callable[[MixerChange], Awaitable[None]]
#: Awaited with each meter frame's levels, keyed by the native client's
#: references: one per physical channel (``ip1``, ``st1l``/``st1r``,
#: ``mainl``/``mainr``, ...), since §7.3 meters each side of a stereo source.
MeterListener = Callable[[Mapping[str, float | None]], Awaitable[None]]
#: Awaited whenever metering becomes available or unavailable — see the
#: module docstring's "Metering availability" section. ``reason`` is ``None``
#: exactly when ``available`` is ``True``, mirroring
#: :data:`proskenion.core.mixer.native.AvailabilityCallback`.
MeteringListener = Callable[[bool, str | None], Awaitable[None]]
#: Awaited once a connection's opening sync has landed — see the module
#: docstring's "A connection's opening sync".
SyncListener = Callable[[], Awaitable[None]]
Origin = Literal["app", "external", "sync"]


class MeterSource(Protocol):
    """What this driver needs from the native metering client (§7.3, §5.5).

    Whether metering is available now, and, alongside it, why not — both
    read with no I/O, straight from the client's own memory of its last
    report. Meter frames travel from the native client to the
    state store without passing through this driver: meters never enter the
    control path (B58).
    """

    @property
    def available(self) -> bool: ...

    @property
    def reason(self) -> str | None: ...


def _echo_matches(written: int, received: int) -> bool:
    """Whether ``received`` is the desk's echo of ``written``: equal, within
    :data:`ECHO_VALUE_TOLERANCE`. The one place the echo assumption lives."""
    return abs(written - received) <= ECHO_VALUE_TOLERANCE


def _decode(kind: ParamKind, value: int) -> float | bool | None:
    if kind is ParamKind.LEVEL:
        return value_to_db(value)
    if kind is ParamKind.MUTE:
        return value != 0  # on is VF 01, off VF 00 (cq20b.md §5, PDF p.8)
    return value_to_pan(value)


@register
class CQ20BDriver(Driver):
    """The CQ-20B over MIDI/TCP (§7.3). Registered as ``mixer/cq20b``."""

    key: ClassVar[str] = "cq20b"
    category: ClassVar[Category] = Category.MIXER
    name: ClassVar[str] = "Allen & Heath CQ-20B"

    #: §7.3 bench question 6: the desk refuses a second MIDI client, and a
    #: second connection attempt could knock the running one off. The
    #: Devices screen's test button (``proskenion/api/devices.py``) checks
    #: this — or the ``mixer`` category, which already implies it — before
    #: building a throwaway instance, and reports this driver's own running
    #: status instead. Declared here, not hard-coded in the devices API,
    #: so a future single-connection driver in another category can opt in
    #: the same way without the core naming it by key.
    SHARES_CONNECTION: ClassVar[bool] = True

    SUPPORTED_TRANSPORTS: ClassVar[list[str]] = ["tcp"]
    TRANSPORT_DEFAULTS: ClassVar[dict[str, dict[str, Any]]] = {"tcp": {"port": CQ_MIDI_PORT}}
    CONFIG_SCHEMA: ClassVar[list[Field]] = [
        Field(
            "recall_wait_ms",
            type="int",
            label="Wait after scene recall (ms)",
            default=DEFAULT_RECALL_WAIT_MS,
            min=0,
            max=5000,
            help="Lets the CQ finish loading a scene before its state is read back (§7.3).",
        ),
        Field(
            "metering",
            type="bool",
            label="Metering",
            default=True,
            help="Read meters over the CQ's native connection, which takes one of the "
            "desk's two MixPad slots (§7.3).",
        ),
        Field(
            "meter_udp_port",
            type="port",
            label="Local meter port (UDP)",
            default=DEFAULT_LOCAL_UDP_PORT,
            min=1,
            max=65535,
            help="Where this appliance receives meters. The appliance firewall opens "
            "51327; change both together.",
        ),
    ]

    #: The native protocol's port. A class attribute so a test can aim the
    #: native client at a stub on an OS-assigned port.
    NATIVE_PORT: ClassVar[int] = NATIVE_PORT

    @classmethod
    def firewall_ports(cls, config: Mapping[str, Any]) -> FirewallPorts:
        """The native connection (:data:`NATIVE_PORT`, metering control) and
        the meter return, beyond the MIDI port the ``tcp`` transport config
        already carries (§3.1's mixer entry; ``appliance/bin/auditorium-config-
        apply``'s ``DEFAULT_DEVICES`` had these hard-coded until the devices
        table started being mirrored from live configuration, contracts §4)."""
        raw_port = config.get("meter_udp_port", DEFAULT_LOCAL_UDP_PORT)
        try:
            meter_port = int(raw_port)
        except (TypeError, ValueError):
            meter_port = DEFAULT_LOCAL_UDP_PORT
        return FirewallPorts(
            outbound=(f"tcp/{cls.NATIVE_PORT}", "udp/any"),
            inbound=(f"udp/{meter_port}",),
        )

    def __init__(
        self,
        device_id: int,
        transport: Transport,
        driver_config: dict[str, Any],
        status_sink: StatusSink,
    ) -> None:
        super().__init__(device_id, transport, driver_config, status_sink)
        #: Orders everything that sends (module docstring, *Ordering*).
        self._lock = asyncio.Lock()
        #: Replaced in tests; the echo window is measured on it.
        self._clock: Callable[[], float] = time.monotonic
        self._parser = MidiParser()
        self._index = refs_by_address()
        #: The last value known for each address: 14-bit, as the desk has it.
        self._values: dict[tuple[int, int], int] = {}
        self._tracked: frozenset[str] = frozenset()
        #: References whose replies are accepted during the current sync.
        self._reading: frozenset[str] = frozenset()
        #: Addresses the current sync has queried and not yet heard back from.
        self._awaiting: set[tuple[int, int]] = set()
        self._replies = asyncio.Event()
        #: Values written, per address, with when: the echoes still expected.
        self._written: dict[tuple[int, int], deque[tuple[int, float]]] = {}
        #: Relative steps sent, per address, whose result is still to arrive.
        self._steps: dict[tuple[int, int], deque[float]] = {}
        self._listeners: list[ChangeListener] = []
        #: What ``supports_metering`` reads: the composed native client.
        self._meter_source: MeterSource | None = None
        self._meters: NativeMeterClient | None = None
        self._meter_listeners: list[MeterListener] = []
        self._metering_listeners: list[MeteringListener] = []
        self._sync_listeners: list[SyncListener] = []
        #: True while :meth:`run` is running: its internal reconnects leave
        #: the native client alone.
        self._running = False
        #: True from sending a recall until its resync ends: every value the
        #: desk reports meanwhile is the recall's doing (``origin="sync"``).
        self._recalling = False
        #: True once *this* connection has produced a single valid reply —
        #: any parsed message from the desk, not just one we asked for. Reset
        #: at the start of every ``connect()``. What :meth:`_loss_message`
        #: tells apart a bare accept-then-reset from a drop of a connection
        #: that was actually working (module docstring, *A second client is
        #: not refused at TCP level*).
        self._replied = False
        #: Before the first connection, capabilities are the declared maximum.
        self._connected_once = False
        #: How the latest connection attempt failed, ``None`` once one succeeds.
        #: Kept through the backoff and the next attempt, so ``amber_failure``
        #: describes the failure the operator is looking at; ``probe()``
        #: reports a refusal from it.
        self._failure: str | None = None
        #: How many syncs have finished, replies received or timed out: one
        #: per connection, per recall and per ``read_state``.
        self.syncs_completed = 0

    # -- configuration from the service -------------------------------------

    def set_tracked(self, refs: Iterable[str]) -> None:
        """The references the venue has configured (§7.3). Replaces the
        previous set. Values for every other reference are discarded silently.

        Raises :class:`KeyError` for a reference this driver does not know,
        before changing anything. Performs no I/O: a caller that starts
        tracking a reference while connected reads it with :meth:`read_state`;
        every later connection and recall syncs it automatically.
        """
        refs = frozenset(refs)
        for ref in refs:
            resolve(ref)
        self._tracked = refs

    @property
    def tracked(self) -> frozenset[str]:
        return self._tracked

    def add_sync_listener(self, callback: SyncListener) -> None:
        """Register ``callback``, awaited once each connection's opening sync
        has landed, with the lock released (module docstring). A listener
        that raises is logged and isolated."""
        self._sync_listeners.append(callback)

    async def _notify_synced(self) -> None:
        for listener in list(self._sync_listeners):
            try:
                await listener()
            except Exception:  # a listener's bug must not stop the read loop
                log.exception("device %s: sync listener raised", self.device_id)

    def add_meter_listener(self, callback: MeterListener) -> None:
        """Register ``callback``, awaited with each meter frame's levels in dB
        (``None`` below the floor), keyed by the native client's per-channel
        references. Display only: the driver relays meters and never reads
        them (B58). A listener that raises is logged and isolated."""
        self._meter_listeners.append(callback)

    @property
    def metering_enabled(self) -> bool:
        return bool(self.config.get("metering", True))

    async def _relay_meters(self, levels: Mapping[str, float | None]) -> None:
        for listener in list(self._meter_listeners):
            try:
                await listener(levels)
            except Exception:  # a listener's bug must not stop the meters
                log.exception("device %s: meter listener raised", self.device_id)

    def add_metering_listener(self, callback: MeteringListener) -> None:
        """Register ``callback``, awaited whenever metering becomes available
        or unavailable (§7.3). Additive, like :meth:`add_change_listener`
        and :meth:`add_meter_listener`. A listener that raises is logged and
        isolated, never reaching another listener or the native client."""
        self._metering_listeners.append(callback)

    def known_metering_status(self) -> tuple[bool, str | None]:
        """The native client's currently known availability and reason, with
        no I/O (§7.3) — ``(available, reason)``, ``reason`` ``None``
        exactly when ``available`` is not ``False``, mirroring
        :data:`MeteringListener`'s own contract.

        Closes the same startup-ordering gap :meth:`known_state` closes for
        channel values: the native client's own connect/retry loop starts
        inside :meth:`connect`, before this driver is even the device
        manager's "running" driver, so its first report — a refusal, say —
        can and does arrive before a caller has had a chance to
        :meth:`add_metering_listener`. Reading this once, right after
        registering, is what lets that caller catch up on a report it would
        otherwise never see (:meth:`_report` calls back only on a change,
        never a repeat). ``(True, None)`` before the native client exists at
        all — metering disabled, or no connection attempted yet — matching
        :meth:`capabilities`'s own "declared maximum" default.
        """
        source = self._meter_source
        if source is None:
            return True, None
        return source.available, source.reason

    async def _on_meter_availability(self, available: bool, reason: str | None) -> None:
        log.info(
            "device %s: metering %s%s",
            self.device_id,
            "available" if available else "unavailable",
            f": {reason}" if reason else "",
        )
        for listener in list(self._metering_listeners):
            try:
                await listener(available, reason)
            except Exception:  # a listener's bug must not break reconnection
                log.exception("device %s: metering listener raised", self.device_id)

    def _build_meters(self) -> NativeMeterClient | None:
        """The native client for this desk: the MIDI transport's host,
        :attr:`NATIVE_PORT`, and the configured local UDP port. ``None`` when
        metering is turned off or the MIDI transport has no host to share."""
        if not self.metering_enabled or not isinstance(self.transport, TcpTransport):
            return None
        host = self.transport.config.get("host")
        if not host:
            return None
        port = self.config.get("meter_udp_port")
        return NativeMeterClient(
            TcpTransport({"host": host, "port": self.NATIVE_PORT}),
            on_meters=self._relay_meters,
            on_availability=self._on_meter_availability,
            local_udp_port=DEFAULT_LOCAL_UDP_PORT if port is None else int(port),
        )

    async def _start_meters(self) -> None:
        """Start the native client once per :meth:`run`; it then runs, and
        retries, on its own."""
        if self._meters is not None or not self._running:
            return
        self._meters = self._build_meters()
        self._meter_source = self._meters
        if self._meters is not None:
            await self._meters.start()

    async def _stop_meters(self) -> None:
        meters, self._meters = self._meters, None
        if meters is not None:
            await meters.stop()

    async def run(self) -> None:
        """The §5.3 run loop, with the native client started by the first
        connection attempt and stopped only when the loop ends."""
        self._running = True
        try:
            await super().run()
        finally:
            self._running = False
            await self._stop_meters()

    async def disconnect(self) -> None:
        """Close MIDI. From outside :meth:`run` (the device manager closing
        the driver, or a test connection) the native client stops too; the
        run loop's own reconnects leave it running."""
        await super().disconnect()
        if not self._running:
            await self._stop_meters()

    def add_change_listener(self, callback: ChangeListener) -> None:
        """Register ``callback``, awaited with each :class:`MixerChange` (§7.3).
        A listener that raises is logged and never reaches the desk's read loop
        or another listener."""
        self._listeners.append(callback)

    # -- MixerDriver: enumeration ----------------------------------------------

    def capabilities(self) -> MixerCapabilities:
        """Before the first connection, the declared maximum; after it, what
        the connections achieved (§5.5, B56). Scene recall and pan are MIDI and
        always there; metering follows the attached :class:`MeterSource`."""
        if self._connected_once:
            source = self._meter_source
            metering = source is not None and source.available
        else:
            metering = True
        return MixerCapabilities(
            input_count=INPUT_COUNT,
            output_count=OUTPUT_COUNT,
            supports_scene_recall=True,
            supports_pan=True,
            supports_mute=True,
            supports_metering=metering,
            # §5.5 gives the CQ's meter range; §7.3 its meter points: inputs
            # post-compressor, outputs post-limiter. The field holds one, and
            # the inputs are most of what is metered.
            meter_min_db=-60.0 if metering else None,
            meter_max_db=10.0 if metering else None,
            meter_point="post_comp" if metering else None,
            supports_gain=False,  # MixPad only (§7.3 *What MIDI does not reach*)
            supports_dca=False,  # Deferred (§7.3 *What MIDI reaches*)
            min_db=MIN_DB,
            max_db=MAX_DB,
        )

    def available_refs(self) -> list[ChannelRef]:
        return [ChannelRef(i.ref, i.label, i.kind, i.stereo) for i in REFS.values()]

    def desk_channels(self) -> list[DeskChannel]:
        """The desk's channels, in :meth:`available_refs` order: sixteen mono
        inputs, ST1, ST2, USB and Bluetooth (each one stereo reference), Main
        LR, and Out 1-6 individually.

        The linked pairs are left out: whether a pair is linked is the
        admin's statement (§7.3), so each output starts as a channel of its
        own, and a channel the admin points at ``out12`` covers Out 1 and
        Out 2 both.
        """
        covered_by: dict[str, list[str]] = {}
        for pair, outputs in LINKED_PAIRS.items():
            for output in outputs:
                covered_by.setdefault(output, []).append(pair)
        return [
            DeskChannel(ref, tuple(covered_by.get(ref.ref, ())))
            for ref in self.available_refs()
            if ref.ref not in LINKED_PAIRS
        ]

    def fader_law(self) -> list[LawPoint]:
        """The law from cq20b.md §4 (PDF p.15), unity a detent. Whether the desk
        runs this law is cq20b.md's first bench question."""
        return _fader_law()

    def known_state(self, refs: Iterable[str]) -> dict[str, ChannelState]:
        """What the driver last knew for ``refs``, with no I/O. A reference
        whose level or mute has not been reported yet is left out."""
        return {ref: state for ref in refs if (state := self._state_of(resolve(ref))) is not None}

    def _state_of(self, info: RefInfo) -> ChannelState | None:
        level = self._values.get(info.level.pair)
        mute = self._values.get(info.mute.pair)
        if level is None or mute is None:
            return None
        pan_value = self._values.get(info.pan.pair) if info.pan is not None else None
        return ChannelState(
            db=value_to_db(level),
            muted=mute != 0,
            pan=value_to_pan(pan_value) if pan_value is not None else None,
        )

    # -- §5.3 ------------------------------------------------------------------

    @property
    def amber_failure(self) -> bool:
        """True while the latest failure is §7.3's refusal, shown amber. Read
        by the device manager exactly as it reads PJLink's ``auth_holding``."""
        return self._failure == MSG_REFUSED

    async def connect(self) -> None:
        """Open the MIDI connection and classify a failure (§7.3).

        Refused: recorded for ``probe()`` to report as :data:`MSG_REFUSED`.
        A name that does not resolve keeps the transport's own message, which
        names the address to check. Anything else — timed out, unreachable —
        is :data:`MSG_OFFLINE`, a configuration failure of the run loop. This
        only classifies a failure of the TCP connect itself; the desk's own
        accept-then-reset (module docstring) always gets this far — see
        :meth:`_loss_message`.
        """
        self._parser = MidiParser()  # a fresh byte stream per connection
        self._replied = False  # nothing heard yet on this connection
        await self._start_meters()  # whatever happens to MIDI below
        try:
            await self.transport.open()
        except ConfigurationError as exc:
            cause = exc.__cause__
            if isinstance(cause, ConnectionRefusedError):
                log.info("device %s: MIDI connection refused: %s", self.device_id, exc)
                self._failure = MSG_REFUSED
                return
            if isinstance(cause, socket.gaierror):
                self._failure = str(exc)
                raise
            self._failure = MSG_OFFLINE
            raise ConfigurationError(MSG_OFFLINE) from exc
        self._failure = None
        self._connected_once = True

    async def probe(self) -> ProbeResult:
        """§5.3: for the CQ, the connection being established is the proof of
        life. A refusal recorded by ``connect()`` is reported here, and so is
        a connection the desk accepted and then reset (:meth:`_loss_message`)
        — the same amber, for the same reason."""
        if self._failure is not None:
            return ProbeResult(False, self._failure)
        is_open = self.transport.is_open if isinstance(self.transport, BaseTransport) else True
        if is_open:
            return ProbeResult(True, None)
        self._failure = self._loss_message()
        return ProbeResult(False, self._failure)

    def _loss_message(self) -> str:
        """Classify a connection that is no longer open — the one place this
        driver tells an accept-then-reset apart from a genuine drop (module
        docstring, *A second client is not refused at TCP level*).

        The real desk accepts a second MIDI/TCP connection and resets it at
        once, leaving the incumbent client untouched (bench, 10.2.30.248,
        21 September 2026). Nothing about that is visible from here except
        timing: a connection reset before it has produced a single valid
        reply never really had the desk's MIDI slot, so it reads the same as
        an outright refusal, :data:`MSG_REFUSED`, amber. A reset after a
        reply has already arrived is a connection that was genuinely working
        and has now genuinely dropped, :data:`MSG_OFFLINE`, unchanged."""
        return MSG_REFUSED if not self._replied else MSG_OFFLINE

    async def maintain(self) -> None:
        """Read the desk until the connection is lost, having first synced
        everything tracked. Writes nothing (§7.3 *Reconnection*, §12.3).

        A loss discovered here, rather than by ``probe()`` before ``maintain``
        was ever entered, still goes through :meth:`_loss_message`. It is
        reported immediately only when no reply has arrived yet: the base
        run loop otherwise reconnects silently on a lost transport, with no
        status report in between, which is right for a connection that was
        genuinely working (unchanged — the reconnection path, module
        docstring) but would otherwise let a slot the desk keeps accepting
        and resetting (module docstring, *A second client is not refused at
        TCP level*) go unreported forever: the next ``connect()`` always
        succeeds too, and ``probe()`` can find the fresh connection still
        looking open before the next reset has arrived.
        """
        reader = asyncio.create_task(self._read_loop())
        try:
            async with self._lock:
                await self._sync(self._sync_refs())
            await self._notify_synced()
            await reader
        except (TransportClosed, OSError):
            if not self._replied:
                self._failure = self._loss_message()
                await self._set_status(DeviceStatus.ERROR, kind="device", detail=self._failure)
            raise
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    # -- inbound ---------------------------------------------------------------

    async def _read_loop(self) -> None:
        while True:
            try:
                data = await self.transport.receive(READ_TIMEOUT_S)
            except TimeoutError:
                continue
            events = self._parser.feed(data)
            if events:
                # Proof this connection is genuinely talking to the desk, not
                # merely accepted by it and about to be reset (module
                # docstring; _loss_message).
                self._replied = True
            changes: list[MixerChange] = []
            for event in events:
                if isinstance(event, NrpnValue):
                    changes.extend(self._on_value(event))
                elif isinstance(event, ProgramChange):
                    log.info("device %s: the desk reported scene %s", self.device_id, event.scene)
            await self._notify(changes)

    def _on_value(self, event: NrpnValue) -> list[MixerChange]:
        pair = (event.msb, event.lsb)
        targets = self._index.get(pair)
        if targets is None:
            return []  # an address this driver has no reference for
        queried = pair in self._awaiting
        if queried:
            self._awaiting.discard(pair)
            if not self._awaiting:
                self._replies.set()
        accepted = [(r, k) for r, k in targets if r in self._tracked or r in self._reading]
        if not accepted:
            return []  # untracked: discarded silently (§7.3)
        if self._is_echo(pair, event.value):
            return []
        origin: Origin
        if self._take_step(pair):
            origin = "app"
        elif queried or self._recalling:
            origin = "sync"
        else:
            origin = "external"
        previous = self._values.get(pair)
        self._values[pair] = event.value
        if previous == event.value:
            return []
        return [MixerChange(r, _KIND[k], _decode(k, event.value), origin) for r, k in accepted]

    def _is_echo(self, pair: tuple[int, int], value: int) -> bool:
        """Whether ``value`` is the echo of a recent write to ``pair``. A match
        consumes that write and any older one to the same address, whose echoes
        the desk has evidently sent already or will not send."""
        written = self._written.get(pair)
        if not written:
            return False
        now = self._clock()
        while written and now - written[0][1] > ECHO_WINDOW_S:
            written.popleft()
        for index, (sent, _at) in enumerate(written):
            if _echo_matches(sent, value):
                for _ in range(index + 1):
                    written.popleft()
                return True
        return False

    def _take_step(self, pair: tuple[int, int]) -> bool:
        steps = self._steps.get(pair)
        if not steps:
            return False
        now = self._clock()
        while steps and now - steps[0] > ECHO_WINDOW_S:
            steps.popleft()
        if steps:
            steps.popleft()
            return True
        return False

    async def _notify(self, changes: list[MixerChange]) -> None:
        for change in changes:
            for listener in list(self._listeners):
                try:
                    await listener(change)
                except Exception:  # a listener's bug must not stop the read loop
                    log.exception("device %s: change listener raised", self.device_id)

    # -- sync --------------------------------------------------------------------

    def _sync_refs(self) -> list[str]:
        """Main, the tracked outputs, the tracked inputs (§7.3)."""
        chosen = self._tracked | {"main"}
        return [ref for ref in _SYNC_ORDER if ref in chosen]

    async def _sync(self, refs: list[str]) -> None:
        """Query every parameter of ``refs``, paced, then wait for the replies.
        The caller holds :attr:`_lock`. Sends ``get`` and nothing else."""
        addresses: list[Address] = []
        seen: set[tuple[int, int]] = set()
        for ref in refs:
            info = resolve(ref)
            for address in (info.level, info.mute, info.pan):
                if address is not None and address.pair not in seen:
                    seen.add(address.pair)
                    addresses.append(address)
        self._reading = frozenset(refs)
        self._awaiting = set(seen)
        self._replies.clear()
        try:
            for index, address in enumerate(addresses):
                if index:
                    await self._sleep(SYNC_SPACING_S)
                await self.transport.send(encode_get(address))
            if self._awaiting:
                try:
                    await asyncio.wait_for(self._replies.wait(), SYNC_REPLY_TIMEOUT_S)
                except TimeoutError:
                    log.warning(
                        "device %s: no reply for %d of %d queried addresses",
                        self.device_id,
                        len(self._awaiting),
                        len(addresses),
                    )
        finally:
            self._reading = frozenset()
            self._awaiting = set()
            self.syncs_completed += 1

    # -- MixerDriver: writes -------------------------------------------------------

    async def set_level(self, refs: list[str], db: float | None) -> None:
        """Every reference to ``db`` (``None`` is off), as one write."""
        value = db_to_value(db)
        await self._write_values(
            [(a, encode_absolute(a, value), value) for a in self._addresses(refs, "level")]
        )

    async def set_mute(self, refs: list[str], muted: bool) -> None:
        """Absolute mute on or off for every reference. Never a toggle (§7.3)."""
        value = 1 if muted else 0
        await self._write_values(
            [(a, encode_mute(a, muted), value) for a in self._addresses(refs, "mute")]
        )

    async def set_pan(self, ref: str, pan: float) -> None:
        """Pan to Main LR, -1.0 to 1.0. Inputs only (§7.3 *What MIDI reaches*)."""
        info = resolve(ref)
        if info.pan is None:
            raise ValueError(f"{ref} has no pan on the CQ-20B")
        value = pan_to_value(pan)
        await self._write_values([(info.pan, encode_absolute(info.pan, value), value)])

    async def step_level(self, refs: list[str], up: bool) -> None:
        """A relative 1 dB step for every reference (§7.3 *Relative level*).
        Level addresses only: the same message toggles a mute (cq20b.md §2).
        The level it lands on arrives from the desk, reported ``origin="app"``."""
        addresses = self._addresses(refs, "level")
        async with self._lock:
            now = self._clock()
            for address in addresses:
                self._steps.setdefault(address.pair, deque()).append(now)
            await self.transport.send(b"".join(encode_step(a, up) for a in addresses))

    async def recall_scene(self, scene_ref: str) -> None:
        """Recall CQ scene ``scene_ref`` ("1"-"128"), wait for it to load, then
        resync, all under the lock (§7.3)."""
        try:
            scene = int(scene_ref)
        except ValueError:
            raise ValueError(f"a CQ scene is a number from 1 to 128, not {scene_ref!r}") from None
        message = encode_scene_recall(scene)
        async with self._lock:
            self._recalling = True
            try:
                await self.transport.send(message)
                await self._sleep(self._recall_wait())
                await self._sync(self._sync_refs())
            finally:
                self._recalling = False

    async def read_state(self, refs: list[str]) -> dict[str, ChannelState]:
        """Query ``refs`` from the desk and return what came back. A reference
        whose level or mute did not arrive is left out."""
        infos = [resolve(ref) for ref in refs]
        async with self._lock:
            await self._sync([info.ref for info in infos])
        return {info.ref: s for info in infos if (s := self._state_of(info)) is not None}

    def _recall_wait(self) -> float:
        wait_ms = self.config.get("recall_wait_ms")
        return (DEFAULT_RECALL_WAIT_MS if wait_ms is None else int(wait_ms)) / 1000

    def _addresses(self, refs: list[str], kind: Literal["level", "mute"]) -> list[Address]:
        """The addresses of ``refs`` for ``kind``, each once (a linked pair and
        its odd output share one). Every reference is resolved before anything
        is sent, so an unknown one sends nothing."""
        addresses: dict[tuple[int, int], Address] = {}
        for ref in refs:
            info = resolve(ref)
            address = info.level if kind == "level" else info.mute
            addresses.setdefault(address.pair, address)
        return list(addresses.values())

    async def _write_values(self, writes: list[tuple[Address, bytes, int]]) -> None:
        """One write of every message, under the lock; then each value is the
        driver's own and reported ``origin="app"`` where it changed."""
        if not writes:
            return
        changes: list[MixerChange] = []
        async with self._lock:
            now = self._clock()
            for address, _message, value in writes:
                self._written.setdefault(address.pair, deque()).append((value, now))
            await self.transport.send(b"".join(message for _a, message, _v in writes))
            for address, _message, value in writes:
                previous = self._values.get(address.pair)
                self._values[address.pair] = value
                if previous == value:
                    continue
                changes.extend(
                    MixerChange(ref, _KIND[kind], _decode(kind, value), "app")
                    for ref, kind in self._index[address.pair]
                    if ref in self._tracked
                )
        await self._notify(changes)


#: The sync's order: Main, then the outputs, then the inputs (§7.3).
_SYNC_ORDER: tuple[str, ...] = (
    "main",
    *(r for r, i in REFS.items() if i.kind == "output"),
    *(r for r, i in REFS.items() if i.kind == "input"),
)
