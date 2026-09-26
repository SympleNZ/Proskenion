"""The three ``lighting_output`` drivers (spec §7.2.1, §7.2.4): ``artnet`` and
``sacn`` for the DMXking eDMX8 MAX over Ethernet — the production path — and
``opendmx`` for the Enttec Open DMX USB, the untested emergency fallback.

Every driver here converts the core's plain-integer universe and 0–255 DMX
byte values into the wire format at its own boundary (:mod:`proskenion.core.dmx.artnet`
does the packet work); nothing above this module ever sees Art-Net, sACN or
DMX512 framing (§5.5, B41).

**Why opendmx is here at all.** §7.2.1 records it as "an emergency path exists
on paper" for the night the in-service Art-Net node fails, and §7.2.4 is blunt
about its status: "This driver has never been tested against hardware and
probably never will be." The Enttec Open DMX USB is an FTDI chip with no
microcontroller — the host does all the DMX break timing — and nobody has
plugged one in to find out whether Python's timing survives contact with a
real dimmer. It is specified, not verified. Its :attr:`OpenDmxDriver.name`
says so, and nothing in this module or its tests claims otherwise.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import sys
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar, Protocol

from proskenion.core.dmx import artnet
from proskenion.core.dmx.endpoint import ArtNetEndpoint, ArtNetLease
from proskenion.core.drivers.base import DeviceStatus, Driver, ProbeResult, StatusSink
from proskenion.core.drivers.capabilities import LightingCapabilities
from proskenion.core.drivers.categories import Category
from proskenion.core.drivers.fields import ConfigError, Field
from proskenion.core.drivers.registry import register
from proskenion.core.transport.base import ConfigurationError, Transport, TransportClosed
from proskenion.core.transport.serial import SerialTransport

log = logging.getLogger(__name__)

# -- artnet -----------------------------------------------------------------


class ArtDmxInput(Protocol):
    """Where the ``artnet`` driver delivers the booth input (§7.2.7).

    Implemented by :class:`proskenion.core.dmx.desk.DeskInput`; the device
    manager hands it to each supervised driver that takes one.
    """

    def attach(self, device_id: int, input_universes: frozenset[int]) -> None: ...
    def art_dmx(self, device_id: int, universe: int, data: bytes) -> None: ...


def parse_universes(raw: object) -> frozenset[int]:
    """``"0, 1"`` → ``{0, 1}``; blank or absent → nothing. Raises :class:`ValueError`."""
    if raw is None:
        return frozenset()
    if isinstance(raw, bool) or not isinstance(raw, str | int):
        raise ValueError("must be universe numbers separated by commas")
    text = str(raw).strip()
    if not text:
        return frozenset()
    universes: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part.isdigit():
            raise ValueError("must be universe numbers separated by commas")
        universe = int(part)
        if universe > artnet.MAX_UNIVERSE:
            raise ValueError(
                f"universe {universe} is outside Art-Net's range 0-{artnet.MAX_UNIVERSE}"
            )
        universes.add(universe)
    return frozenset(universes)


def _local_address_towards(host: str) -> str:
    """This machine's address on the route to ``host`` — sends nothing."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect((host, artnet.ARTNET_PORT))
            return str(probe.getsockname()[0])
        except OSError:
            return ""


@register
class ArtnetDriver(Driver):
    """Art-Net output, health and booth input over UDP 6454 (§7.2.1, §7.2.5,
    §7.2.7, §7.2.8) — the production path for the DMXking eDMX8 MAX.

    **One socket.** Everything goes through a lease on the process's one
    Art-Net socket, :class:`~proskenion.core.dmx.endpoint.ArtNetEndpoint`,
    bound to port 6454 — not through a socket of the UDP transport's own. The
    node answers ArtPoll by broadcasting to port 6454 whatever port the poll
    came from, so a socket anywhere else never hears it. The UDP transport
    still owns the node's address (B45): ``host`` is where ArtDmx and ArtPoll
    are sent and the only sender this driver listens to; ``port`` is the
    node's port. The transport's ``bind_port`` and ``broadcast`` settings do
    not apply to this driver.

    **Health (§7.2.8).** ArtPoll every 30 s. Only an ``ArtPollReply`` whose
    *source address* is the node counts: other Art-Net devices share the
    port and the VLAN (:class:`~proskenion.core.dmx.artnet.ArtNetReceiver`).
    A reply from the node counts whoever asked for it. No reply within 10 s
    is amber; two consecutive misses are red. The socket is not the fault in
    either case, so the driver stays up and keeps polling, and the first
    reply turns it green again. The reply's ``GoodInput`` bits go in the
    status detail as a commissioning check and gate nothing (§7.2.7).

    **Booth input (§7.2.7).** ``input_universes`` names the universe(s) the
    node broadcasts its DMX-IN port on. ArtDmx from the node on one of them
    goes to the :class:`ArtDmxInput` the device manager hands this driver;
    nothing configured means detection is off, never "every universe".
    """

    key: ClassVar[str] = "artnet"
    category: ClassVar[Category] = Category.LIGHTING_OUTPUT
    name: ClassVar[str] = "Art-Net (eDMX8 MAX or compatible node)"

    SUPPORTED_TRANSPORTS: ClassVar[list[str]] = ["udp"]
    TRANSPORT_DEFAULTS: ClassVar[dict[str, dict[str, Any]]] = {"udp": {"port": artnet.ARTNET_PORT}}
    CONFIG_SCHEMA: ClassVar[list[Field]] = [
        Field(
            "input_universes",
            type="string",
            label="Booth input universe",
            pattern=r"^\s*\d+(\s*,\s*\d+)*\s*$",
            help=(
                "The universe the node broadcasts its booth DMX input on, for "
                "visiting-desk detection. Several may be separated by commas. "
                "Blank turns detection off"
            ),
        ),
    ]

    #: How long one probe attempt waits for a reply (§7.2.8: 10 s is amber).
    #: PROBE_INTERVAL (inherited, 30 s per §11.1) governs how often one is made.
    POLL_TIMEOUT: ClassVar[float] = 10.0

    def __init__(
        self,
        device_id: int,
        transport: Transport,
        driver_config: dict[str, Any],
        status_sink: StatusSink,
        *,
        endpoint_port: int | None = None,
    ) -> None:
        super().__init__(device_id, transport, driver_config, status_sink)
        self._sequence: dict[int, int] = {}
        self._sent_universes: set[int] = set()
        #: ``None`` binds :attr:`ArtNetEndpoint.default_port` — 6454.
        self._endpoint_port = endpoint_port
        self._lease: ArtNetLease | None = None
        self._remote: tuple[str, int] | None = None
        #: The node's resolved address: the only sender this driver hears.
        self.node_address: str | None = None
        self._reply_seen = asyncio.Event()
        self._input: ArtDmxInput | None = None
        #: The most recent poll reply from the node — name, firmware, ports.
        self.last_reply: artnet.ArtPollReply | None = None
        #: Every page of the node's reply, by ``BindIndex``: the eDMX8 MAX
        #: describes its eight ports in more than one reply.
        self.replies: dict[int, artnet.ArtPollReply] = {}
        try:
            self.input_universes = parse_universes(self.config.get("input_universes"))
        except ValueError:
            self.input_universes = frozenset()  # validate_config reports it

    async def validate_config(self, config: dict[str, Any]) -> list[ConfigError]:
        try:
            parse_universes(config.get("input_universes"))
        except ValueError as exc:
            return [ConfigError("input_universes", str(exc))]
        return []

    # -- the socket ----------------------------------------------------------

    async def connect(self) -> None:
        """Resolve the node and take a lease on the Art-Net socket."""
        config = getattr(self.transport, "config", {})
        host = str(config.get("host") or "")
        port = int(config.get("port") or artnet.ARTNET_PORT)
        if not host:
            raise ConfigurationError("no node address configured")
        loop = asyncio.get_running_loop()
        try:
            resolved = await loop.getaddrinfo(host, port, family=socket.AF_INET)
        except socket.gaierror as exc:
            raise ConfigurationError(f"cannot resolve {host!r}: {exc}") from exc
        if not resolved:
            raise ConfigurationError(f"cannot resolve {host!r}")
        node = str(resolved[0][4][0])
        receiver = artnet.ArtNetReceiver(_local_address_towards(node), node_address=node)
        receiver.on_poll_reply(self._on_poll_reply)
        if self.input_universes:
            receiver.on_art_dmx(self._on_art_dmx, universes=self.input_universes)
        try:
            lease = await ArtNetEndpoint.acquire(receiver.handle_datagram, port=self._endpoint_port)
        except OSError as exc:
            wanted = self._endpoint_port or ArtNetEndpoint.default_port
            raise ConfigurationError(f"cannot bind the Art-Net port {wanted}: {exc}") from exc
        self._lease = lease
        self._remote = (node, port)
        self.node_address = node

    async def disconnect(self) -> None:
        lease, self._lease = self._lease, None
        if lease is not None:
            await lease.release()

    def _send(self, packet: bytes) -> None:
        lease = self._lease
        if lease is None or self._remote is None or lease.endpoint.closing:
            raise TransportClosed("the Art-Net socket is not open")
        lease.send(packet, self._remote)

    # -- health (§7.2.8) -----------------------------------------------------

    async def probe(self) -> ProbeResult:
        """An ArtPoll, answered by an ArtPollReply from the node (§11.1).

        The reply may arrive as a broadcast, and may be one the node sent
        because another controller polled it; either counts, as long as it
        comes from the node's own address.
        """
        self._reply_seen.clear()
        self._send(artnet.encode_art_poll())
        try:
            await asyncio.wait_for(self._reply_seen.wait(), self.POLL_TIMEOUT)
        except TimeoutError:
            return ProbeResult(
                False, f"no ArtPollReply from {self.node_address} within {self.POLL_TIMEOUT:g} s"
            )
        return ProbeResult(True, self.health_detail())

    async def maintain(self) -> None:
        """Poll every 30 s: one miss is amber, two consecutive are red (§7.2.8).

        Returns only when the socket itself is lost, which is the one fault
        a reconnect can mend. While the node answers, the status detail
        follows its reply — name, firmware and ``GoodInput`` — from the
        connecting probe on, and is republished whenever it changes.
        """
        reported = self.health_detail()
        await self._set_status(DeviceStatus.CONNECTED, detail=reported)
        failures = 0
        while True:
            await self._sleep(self.PROBE_INTERVAL)
            try:
                result = await self.probe()
            except TimeoutError as exc:
                result = ProbeResult(False, str(exc) or type(exc).__name__)
            if result.alive:
                if failures or result.detail != reported:
                    reported = result.detail or ""
                    await self._set_status(DeviceStatus.CONNECTED, detail=result.detail)
                failures = 0
                continue
            failures += 1
            log.info("device %s poll missed (%d): %s", self.device_id, failures, result.detail)
            # The device manager shows the first miss amber and the second red.
            await self._set_status(DeviceStatus.ERROR, kind="device", detail=result.detail)

    def _on_poll_reply(self, reply: artnet.ArtPollReply, source: str) -> None:
        self.replies[reply.bind_index] = reply
        self.last_reply = reply
        self._reply_seen.set()

    def health_detail(self) -> str:
        """Node name and firmware, then each input port's ``GoodInput``.

        ``GoodInput`` bit 7 is "data received", refreshed only once per poll,
        so it confirms the booth port is an input and receiving — a
        commissioning check — and is never used to gate control (§7.2.7).
        """
        reply = self.last_reply
        if reply is None:
            return "no ArtPollReply yet"
        fw = f"{reply.firmware_version[0]}.{reply.firmware_version[1]}"
        inputs: list[str] = []
        for page in sorted(self.replies):
            source = self.replies[page]
            for port in source.ports:
                if not port.port_type & 0x40:  # bit 6: the port can input
                    continue
                universe = source.port_universe(port, direction="in")
                state = "receiving" if port.good_input & 0x80 else "no data"
                inputs.append(f"input {universe} {state}")
        detail = f"{reply.short_name} firmware {fw}"
        return f"{detail}; {', '.join(inputs)}" if inputs else detail

    # -- booth input (§7.2.7) ------------------------------------------------

    def set_input_sink(self, sink: ArtDmxInput) -> None:
        """Deliver the booth input's ArtDmx to ``sink`` from now on."""
        self._input = sink
        sink.attach(self.device_id, self.input_universes)

    def _on_art_dmx(self, universe: int, data: bytes, source: str) -> None:
        if self._input is not None:
            self._input.art_dmx(self.device_id, universe, data)

    # -- output ----------------------------------------------------------------

    def capabilities(self) -> LightingCapabilities:
        return LightingCapabilities(
            universe_count=artnet.MAX_UNIVERSE + 1, supports_receive=True, supports_poll=True
        )

    async def send_universe(self, universe: int, data: bytes) -> None:
        sequence = self._next_sequence(universe)
        self._send(artnet.encode_art_dmx(universe, data, sequence=sequence))
        self._sent_universes.add(universe)

    async def blackout(self) -> None:
        zeros = bytes(artnet.UNIVERSE_FRAME_LENGTH)
        for universe in sorted(self._sent_universes):
            await self.send_universe(universe, zeros)

    def _next_sequence(self, universe: int) -> int:
        # 0x00 disables Art-Net sequencing (§7.2.5's reference), so this counter
        # cycles 1..255 and never emits 0 once a universe has started sending.
        current = self._sequence.get(universe, 0)
        nxt = 1 if current >= 255 else current + 1
        self._sequence[universe] = nxt
        return nxt


# -- sacn ---------------------------------------------------------------

PingRunner = Callable[[str], Awaitable[bool]]

DEFAULT_PRIORITY: int = 100
DEFAULT_SOURCE_NAME: str = "Proskenion"


async def _system_ping(host: str, *, budget_s: float = 2.0) -> bool:
    """Reachability by ICMP echo, via the system ``ping`` command (§11.1) —
    a raw ICMP socket needs root, which this appliance does not run as."""
    if sys.platform == "win32":
        args = ["ping", "-n", "1", "-w", str(int(budget_s * 1000)), host]
    else:
        args = ["ping", "-c", "1", "-W", str(max(1, int(budget_s))), host]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        returncode = await asyncio.wait_for(proc.wait(), budget_s + 2.0)
    except (OSError, TimeoutError):
        return False
    return returncode == 0


@register
class SacnDriver(Driver):
    """sACN (E1.31) output over UDP 5568 (§7.2.1, §7.2.5) — the same
    eDMX8 MAX, addressed as an E1.31 receiver instead of an Art-Net node."""

    key: ClassVar[str] = "sacn"
    category: ClassVar[Category] = Category.LIGHTING_OUTPUT
    name: ClassVar[str] = "sACN / E1.31"

    SUPPORTED_TRANSPORTS: ClassVar[list[str]] = ["udp"]
    TRANSPORT_DEFAULTS: ClassVar[dict[str, dict[str, Any]]] = {"udp": {"port": artnet.SACN_PORT}}
    CONFIG_SCHEMA: ClassVar[list[Field]] = [
        Field(
            "priority",
            type="int",
            label="Priority",
            default=DEFAULT_PRIORITY,
            min=0,
            max=artnet.SACN_MAX_PRIORITY,
            help="E1.31 source priority; the higher value wins when another source also sends",
        ),
        Field(
            "source_name",
            type="string",
            label="Source name",
            default=DEFAULT_SOURCE_NAME,
            help="Announced in every packet's framing layer; shown on the node's own diagnostics",
        ),
    ]

    def __init__(
        self,
        device_id: int,
        transport: Transport,
        driver_config: dict[str, Any],
        status_sink: StatusSink,
        *,
        ping: PingRunner | None = None,
    ) -> None:
        super().__init__(device_id, transport, driver_config, status_sink)
        #: Injectable so tests never shell out to the real ``ping`` (§11.1).
        self._ping: PingRunner = ping or _system_ping
        self._cid = artnet.stable_cid(f"proskenion-sacn-device-{device_id}")
        self._sequence: dict[int, int] = {}
        self._sent_universes: set[int] = set()

    async def probe(self) -> ProbeResult:
        """E1.31 has no discovery protocol; ICMP reachability only (§11.1)."""
        config = getattr(self.transport, "config", {})
        host = str(config.get("host") or "")
        if not host:
            return ProbeResult(False, "no host configured")
        alive = await self._ping(host)
        return ProbeResult(alive, None if alive else f"no ICMP reply from {host}")

    def capabilities(self) -> LightingCapabilities:
        return LightingCapabilities(
            universe_count=artnet.SACN_MAX_UNIVERSE, supports_receive=False, supports_poll=False
        )

    async def send_universe(self, universe: int, data: bytes) -> None:
        sequence = self._next_sequence(universe)
        packet = artnet.encode_sacn_dmx(
            universe,
            data,
            cid=self._cid,
            source_name=str(self.config.get("source_name", DEFAULT_SOURCE_NAME)),
            sequence=sequence,
            priority=int(self.config.get("priority", DEFAULT_PRIORITY)),
        )
        await self.transport.send(packet)
        self._sent_universes.add(universe)

    async def blackout(self) -> None:
        zeros = bytes(artnet.UNIVERSE_FRAME_LENGTH)
        for universe in sorted(self._sent_universes):
            await self.send_universe(universe, zeros)

    def _next_sequence(self, universe: int) -> int:
        current = self._sequence.get(universe, 0)
        nxt = (current + 1) % 256
        self._sequence[universe] = nxt
        return nxt


# -- opendmx: unverified (§7.2.4) ----------------------------------------


@register
class OpenDmxDriver(Driver):
    """Enttec Open DMX USB over the serial transport — **unverified**; see
    the module docstring and §7.2.4. Do not present this as working."""

    key: ClassVar[str] = "opendmx"
    category: ClassVar[Category] = Category.LIGHTING_OUTPUT
    name: ClassVar[str] = "Enttec Open DMX USB (unverified — never run against hardware, §7.2.4)"

    SUPPORTED_TRANSPORTS: ClassVar[list[str]] = ["serial"]
    TRANSPORT_DEFAULTS: ClassVar[dict[str, dict[str, Any]]] = {
        "serial": {"baud": 250_000, "bits": 8, "parity": "none", "stop": "2"}
    }
    CONFIG_SCHEMA: ClassVar[list[Field]] = []  # protocol only — nothing to configure

    BREAK_SECONDS: ClassVar[float] = 100e-6  # >= 88 µs (§7.2.4)
    MARK_AFTER_BREAK_SECONDS: ClassVar[float] = 12e-6  # >= 8 µs

    def __init__(
        self,
        device_id: int,
        transport: Transport,
        driver_config: dict[str, Any],
        status_sink: StatusSink,
    ) -> None:
        super().__init__(device_id, transport, driver_config, status_sink)
        self._sent_universes: set[int] = set()

    async def probe(self) -> ProbeResult:
        """No return path (§7.2.4): liveness is the device node's presence,
        already proved by a successful ``connect()`` (§11.1)."""
        if not bool(getattr(self.transport, "is_open", False)):
            return ProbeResult(False, "the serial device node is not open")
        return ProbeResult(True)

    def capabilities(self) -> LightingCapabilities:
        return LightingCapabilities(universe_count=1, supports_receive=False, supports_poll=False)

    async def send_universe(self, universe: int, data: bytes) -> None:
        if len(data) != artnet.UNIVERSE_FRAME_LENGTH:
            raise ValueError(
                f"a universe frame is exactly {artnet.UNIVERSE_FRAME_LENGTH} bytes, got {len(data)}"
            )
        # SUPPORTED_TRANSPORTS declares "serial" only — the registry enforces
        # that at build time, but a driver constructed directly (as tests do)
        # is not guaranteed it, and send_break is a serial-only capability
        # that the generic Transport protocol does not declare (§5.5).
        if not isinstance(self.transport, SerialTransport):
            raise TypeError(
                f"opendmx requires a SerialTransport, got {type(self.transport).__name__}"
            )
        await self.transport.send_break(
            self.BREAK_SECONDS, mark_after_s=self.MARK_AFTER_BREAK_SECONDS
        )
        await self.transport.send(bytes([0x00]) + data)  # start code, then 512 channels
        self._sent_universes.add(universe)

    async def blackout(self) -> None:
        zeros = bytes(artnet.UNIVERSE_FRAME_LENGTH)
        for universe in sorted(self._sent_universes):
            await self.send_universe(universe, zeros)
