"""Art-Net and sACN (E1.31) packet handling (spec §7.2.5).

Implemented directly rather than through a third-party library. The candidate
library, ``pyartnet``, is send-only — no receive path and no ArtPoll — and
this design needs both: ArtDmx input for external desk detection and
observation (§7.2.7), and ArtPoll for health monitoring (§7.2.8). This module
is pure packet encoding and decoding — no sockets. The one
``asyncio.DatagramProtocol`` bound to UDP 6454 that carries both Art-Net
directions is :class:`proskenion.core.dmx.endpoint.ArtNetEndpoint`; this
module only turns bytes into meaning and back.

**The controller never answers ArtPoll.** There is no function here that
builds a reply to one — :class:`ArtNetReceiver` decodes ``ArtPollReply`` and
``ArtDmx`` only, and silently ignores everything else, ``ArtPoll`` included.
A visiting desk's discovery scan sees the DMX node, never this controller.

Reference: the Art-Net 4 Protocol Specification (Artistic Licence, Release
V1.4, Document Revision 1.4dp), the ``ArtDmx``, ``ArtPoll`` and
``ArtPollReply`` packet definition tables, and the ANSI E1.31-2016 (sACN)
Root, Framing and DMP layer definitions.

Universe mapping
-----------------
The core's universe is a plain integer, 0 to 32767 (Art-Net's 15-bit
Port-Address space). It decomposes as ``Net`` (bits 14-8, 7 bits), ``SubNet``
(bits 7-4) and ``Universe`` (bits 3-0): ``net = universe >> 8``,
``sub_uni_byte = universe & 0xFF`` where the low nibble of that byte is the
Art-Net ``Universe`` and the high nibble is ``SubNet``. Universe 15 and 16 are
the boundary: 15 is the last universe of sub-net 0, 16 rolls over into
sub-net 1.
"""

from __future__ import annotations

import socket
import struct
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

# -- Shared framing ------------------------------------------------------

ARTNET_ID: bytes = b"Art-Net\x00"
ARTNET_PORT: int = 6454
PROTOCOL_VERSION: int = 14

OP_POLL: int = 0x2000
OP_POLL_REPLY: int = 0x2100
OP_DMX: int = 0x5000

MAX_UNIVERSE: int = 0x7FFF  # Art-Net's 15-bit Port-Address space
UNIVERSE_FRAME_LENGTH: int = 512  # §7.2.2: every packet carries the full universe


def _opcode_bytes(opcode: int) -> bytes:
    return struct.pack("<H", opcode)  # "transmitted low byte first"


def art_net_opcode(packet: bytes) -> int | None:
    """The packet's OpCode, or ``None`` when it is not an Art-Net packet at all."""
    if len(packet) < 10 or packet[:8] != ARTNET_ID:
        return None
    return int(struct.unpack_from("<H", packet, 8)[0])


def _require_universe(universe: int) -> None:
    if not 0 <= universe <= MAX_UNIVERSE:
        raise ValueError(f"universe {universe} is outside Art-Net's range 0-{MAX_UNIVERSE}")


def _require_full_frame(data: bytes) -> None:
    if len(data) != UNIVERSE_FRAME_LENGTH:
        raise ValueError(
            f"a universe frame is exactly {UNIVERSE_FRAME_LENGTH} bytes, got {len(data)}"
        )


# -- ArtDmx ----------------------------------------------------------------


@dataclass(frozen=True)
class ArtDmxFrame:
    """One decoded ``ArtDmx`` packet."""

    universe: int
    sequence: int
    physical: int
    data: bytes


def encode_art_dmx(universe: int, data: bytes, *, sequence: int = 0, physical: int = 0) -> bytes:
    """Build one ``ArtDmx`` packet carrying the full 512-byte frame for ``universe``."""
    _require_universe(universe)
    _require_full_frame(data)
    net = (universe >> 8) & 0x7F
    sub_uni = universe & 0xFF
    return (
        ARTNET_ID
        + _opcode_bytes(OP_DMX)
        + bytes([0, PROTOCOL_VERSION, sequence & 0xFF, physical & 0xFF, sub_uni, net])
        + struct.pack(">H", len(data))
        + data
    )


def decode_art_dmx(packet: bytes) -> ArtDmxFrame:
    """Parse one ``ArtDmx`` packet. Raises :class:`ValueError` for anything else."""
    if art_net_opcode(packet) != OP_DMX:
        raise ValueError("not an ArtDmx packet")
    if len(packet) < 18:
        raise ValueError("ArtDmx packet is shorter than its own header")
    sequence = packet[12]
    physical = packet[13]
    sub_uni = packet[14]
    net = packet[15]
    length = struct.unpack_from(">H", packet, 16)[0]
    data = packet[18 : 18 + length]
    return ArtDmxFrame(
        universe=(net << 8) | sub_uni, sequence=sequence, physical=physical, data=data
    )


# -- ArtPoll -----------------------------------------------------------------


def encode_art_poll(*, talk_to_me: int = 0, priority: int = 0) -> bytes:
    """Build one ``ArtPoll`` packet. The controller only ever sends this —
    it never builds a reply to one (module docstring)."""
    flags = bytes([0, PROTOCOL_VERSION, talk_to_me & 0xFF, priority & 0xFF])
    return ARTNET_ID + _opcode_bytes(OP_POLL) + flags


def is_art_poll(packet: bytes) -> bool:
    return art_net_opcode(packet) == OP_POLL


# -- ArtPollReply --------------------------------------------------------

_SHORT_NAME_LEN = 18
_LONG_NAME_LEN = 64
_NODE_REPORT_LEN = 64
_REPLY_LENGTH = 239  # ID..Filler, per the packet definition table


@dataclass(frozen=True)
class ArtNetPort:
    """One entry of an ``ArtPollReply``'s four parallel per-port arrays."""

    port_type: int  # bit7 output-capable, bit6 input-capable, bits0-5 protocol
    good_input: int  # bit7 "data received" — used as commissioning corroboration (§7.2.8)
    good_output: int
    sw_in: int  # low nibble of the input port's Universe
    sw_out: int  # low nibble of the output port's Universe


@dataclass(frozen=True)
class ArtPollReply:
    """What §7.2.5 and §7.2.8 need from a node's ``ArtPollReply``: its name,
    firmware version, and per-port state including ``GoodInput``."""

    ip_address: str
    firmware_version: tuple[int, int]  # (high byte, low byte)
    short_name: str
    long_name: str
    net_switch: int
    sub_switch: int
    ports: tuple[ArtNetPort, ...] = field(default_factory=tuple)
    #: Which page of a node's ports this reply describes. A node with more
    #: than four ports (the eDMX8 MAX has eight) sends one reply per four,
    #: each with its own ``BindIndex``; 0 and 1 both mean the root device.
    bind_index: int = 0

    def port_universe(self, port: ArtNetPort, *, direction: str) -> int:
        """The 15-bit universe of one port: ``direction`` is ``"in"`` or ``"out"``."""
        low = port.sw_in if direction == "in" else port.sw_out
        return (self.net_switch << 8) | (self.sub_switch << 4) | (low & 0x0F)


def _decode_cstr(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="replace")


def _pad(raw: bytes, length: int) -> bytes:
    return raw[: length - 1].ljust(length, b"\x00")


def encode_art_poll_reply(
    *,
    ip_address: str,
    firmware_version: tuple[int, int],
    short_name: str,
    long_name: str,
    net_switch: int = 0,
    sub_switch: int = 0,
    ports: Sequence[ArtNetPort] = (),
    style: int = 0,  # StNode
    mac: bytes = b"\x00" * 6,
    bind_index: int = 0,
) -> bytes:
    """Build one 239-byte ``ArtPollReply``. Used by test stubs (§22.2) — the
    controller itself never sends this (module docstring)."""
    ip_bytes = socket.inet_aton(ip_address)
    port_count = min(len(ports), 4)
    port_types = bytes(p.port_type & 0xFF for p in ports).ljust(4, b"\x00")[:4]
    good_input = bytes(p.good_input & 0xFF for p in ports).ljust(4, b"\x00")[:4]
    good_output = bytes(p.good_output & 0xFF for p in ports).ljust(4, b"\x00")[:4]
    sw_in = bytes(p.sw_in & 0x0F for p in ports).ljust(4, b"\x00")[:4]
    sw_out = bytes(p.sw_out & 0x0F for p in ports).ljust(4, b"\x00")[:4]

    packet = bytearray()
    packet += ARTNET_ID
    packet += _opcode_bytes(OP_POLL_REPLY)
    packet += ip_bytes
    packet += struct.pack("<H", ARTNET_PORT)  # Port is always 0x1936, low byte first
    packet += bytes([firmware_version[0] & 0xFF, firmware_version[1] & 0xFF])
    packet += bytes([net_switch & 0x7F, sub_switch & 0x0F])
    packet += bytes([0, 0])  # OemHi, OemLo
    packet += bytes([0])  # UbeaVersion
    packet += bytes([0])  # Status1
    packet += bytes([0, 0])  # EstaManLo, EstaManHi
    packet += _pad(short_name.encode("ascii", errors="replace"), _SHORT_NAME_LEN)
    packet += _pad(long_name.encode("ascii", errors="replace"), _LONG_NAME_LEN)
    packet += bytes(_NODE_REPORT_LEN)  # NodeReport — not modelled, transmitted empty
    packet += bytes([0, port_count])  # NumPortsHi, NumPortsLo
    packet += port_types
    packet += good_input
    packet += good_output
    packet += sw_in
    packet += sw_out
    packet += bytes([0])  # AcnPriority
    packet += bytes([0])  # SwMacro
    packet += bytes([0])  # SwRemote
    packet += bytes(3)  # Spare x3
    packet += bytes([style & 0xFF])
    packet += mac[:6].ljust(6, b"\x00")
    packet += ip_bytes  # BindIp — root device, no binding modelled
    packet += bytes([bind_index & 0xFF])  # BindIndex
    packet += bytes([0])  # Status2
    packet += bytes(4)  # GoodOutputB
    packet += bytes([0])  # Status3
    packet += bytes(6)  # DefaultRespUID
    packet += bytes(2)  # User
    packet += bytes(2)  # RefreshRate
    packet += bytes([0])  # BackgroundQueuePolicy
    packet += bytes(10)  # Filler
    assert len(packet) == _REPLY_LENGTH, (
        f"ArtPollReply must be {_REPLY_LENGTH} bytes, built {len(packet)}"
    )
    return bytes(packet)


def decode_art_poll_reply(packet: bytes) -> ArtPollReply:
    """Parse node name, firmware version and per-port state, including
    ``GoodInput``, from an ``ArtPollReply`` (§7.2.5, §7.2.8)."""
    if art_net_opcode(packet) != OP_POLL_REPLY:
        raise ValueError("not an ArtPollReply packet")
    if len(packet) < _REPLY_LENGTH:
        raise ValueError(f"ArtPollReply is shorter than {_REPLY_LENGTH} bytes")
    ip_address = socket.inet_ntoa(packet[10:14])
    firmware_version = (packet[16], packet[17])
    net_switch = packet[18] & 0x7F
    sub_switch = packet[19] & 0x0F
    short_name = _decode_cstr(packet[26:44])
    long_name = _decode_cstr(packet[44:108])
    port_count = min(packet[173], 4)
    port_types = packet[174:178]
    good_input = packet[178:182]
    good_output = packet[182:186]
    sw_in = packet[186:190]
    sw_out = packet[190:194]
    ports = tuple(
        ArtNetPort(
            port_type=port_types[i],
            good_input=good_input[i],
            good_output=good_output[i],
            sw_in=sw_in[i] & 0x0F,
            sw_out=sw_out[i] & 0x0F,
        )
        for i in range(port_count)
    )
    return ArtPollReply(
        ip_address=ip_address,
        firmware_version=firmware_version,
        short_name=short_name,
        long_name=long_name,
        net_switch=net_switch,
        sub_switch=sub_switch,
        ports=ports,
        bind_index=packet[_BIND_INDEX_OFFSET],
    )


_BIND_INDEX_OFFSET = 211  # after Style (200), MAC (201-206) and BindIp (207-210)


# -- ArtDmx in: dispatch, filter, own-source rejection ------------------------

ArtDmxCallback = Callable[[int, bytes, str], None]
ArtPollReplyCallback = Callable[[ArtPollReply, str], None]


class ArtNetReceiver:
    """Decodes inbound Art-Net datagrams and dispatches them to registered
    callbacks (§7.2.5's *ArtDmx input* row).

    Not a socket — the ``artnet`` driver feeds it every datagram the shared
    Art-Net endpoint (:mod:`proskenion.core.dmx.endpoint`) hears, together
    with the sender's address. Frames from ``own_address`` are rejected
    outright, so a controller that also sends ArtDmx (say, to the eDMX8 MAX)
    never observes its own output as a visiting desk.

    **Only the configured node is heard.** When ``node_address`` is given,
    every datagram from any other address is dropped before it is decoded —
    ``ArtPollReply`` and ``ArtDmx`` alike. This is a deliberate tightening of
    §7.2.7, which says only "ArtDmx seen on the input universe". Port 6454 is
    shared by every Art-Net device on the VLAN, and the auditorium runs other
    sources alongside the node for months at a time: the previous node
    broadcasts its own DMX input as ArtDmx on universe 0, which is also one
    of the eDMX8 MAX's input universes, and answers ArtPoll with its own
    ``ArtPollReply``. Without the filter that node would hold the room under
    external control indefinitely, and its replies would report our node
    healthy when it is not. A visiting desk reaches the controller through
    the node's DMX-IN port, which the node broadcasts from its own address,
    so the filter loses nothing §7.2.7 relies on. A reply the node sends
    because some other controller polled it still comes from the node's
    address, and is accepted.

    ``ArtPoll`` and anything unrecognised are silently ignored: there is no
    code path here, or anywhere in this module, that constructs a reply to
    an ``ArtPoll`` (module docstring).
    """

    def __init__(self, own_address: str, *, node_address: str | None = None) -> None:
        self.own_address = own_address
        #: The one sender this receiver listens to; ``None`` listens to all.
        self.node_address = node_address
        self._art_dmx: ArtDmxCallback | None = None
        self._universes: frozenset[int] | None = None
        self._poll_reply: ArtPollReplyCallback | None = None

    def on_art_dmx(
        self, callback: ArtDmxCallback, *, universes: Iterable[int] | None = None
    ) -> None:
        """Register the callback for inbound ``ArtDmx``. ``universes`` restricts
        delivery to those universes; ``None`` (the default) accepts all."""
        self._art_dmx = callback
        self._universes = frozenset(universes) if universes is not None else None

    def on_poll_reply(self, callback: ArtPollReplyCallback) -> None:
        """Register the callback for ``ArtPollReply`` (health monitoring, §7.2.8)."""
        self._poll_reply = callback

    def handle_datagram(self, data: bytes, source: str) -> None:
        if self.node_address is not None and source != self.node_address:
            return  # another Art-Net device on the VLAN (class docstring)
        opcode = art_net_opcode(data)
        if opcode == OP_DMX:
            if source == self.own_address:
                return  # own-source rejection
            try:
                frame = decode_art_dmx(data)
            except ValueError:
                return
            if self._universes is not None and frame.universe not in self._universes:
                return
            if self._art_dmx is not None:
                self._art_dmx(frame.universe, frame.data, source)
        elif opcode == OP_POLL_REPLY:
            if self._poll_reply is None:
                return
            try:
                reply = decode_art_poll_reply(data)
            except ValueError:
                return
            self._poll_reply(reply, source)
        # OP_POLL and anything else: never answered, never dispatched.


# -- sACN (E1.31) output -------------------------------------------------

SACN_PORT: int = 5568
SACN_MIN_UNIVERSE: int = 1
SACN_MAX_UNIVERSE: int = 63999
SACN_MAX_PRIORITY: int = 200

_SACN_PACKET_ID: bytes = b"ASC-E1.17\x00\x00\x00"
_VECTOR_ROOT_E131_DATA = 0x00000004
_VECTOR_E131_DATA_PACKET = 0x00000002
_VECTOR_DMP_SET_PROPERTY = 0x02
_SOURCE_NAME_LEN = 64
_FLAGS = 0x7000  # top nibble of every layer's 16-bit Flags-and-Length field


def stable_cid(seed: str) -> bytes:
    """A 16-byte CID that is the same every time for the same ``seed`` (§7.2.5) —
    a source stays recognisable across restarts without persisting anything."""
    return uuid.uuid5(uuid.NAMESPACE_DNS, seed).bytes


def sacn_multicast_address(universe: int) -> str:
    """The universe's multicast group, per E1.31: ``239.255.<hi>.<lo>``."""
    if not SACN_MIN_UNIVERSE <= universe <= SACN_MAX_UNIVERSE:
        raise ValueError(f"sACN universe {universe} is outside 1-{SACN_MAX_UNIVERSE}")
    return f"239.255.{(universe >> 8) & 0xFF}.{universe & 0xFF}"


def encode_sacn_dmx(
    universe: int,
    data: bytes,
    *,
    cid: bytes,
    source_name: str,
    sequence: int,
    priority: int = 100,
) -> bytes:
    """Build one E1.31 data packet: root, framing and DMP layers (§7.2.5)."""
    if not SACN_MIN_UNIVERSE <= universe <= SACN_MAX_UNIVERSE:
        raise ValueError(f"sACN universe {universe} is outside 1-{SACN_MAX_UNIVERSE}")
    _require_full_frame(data)
    if len(cid) != 16:
        raise ValueError("CID must be 16 bytes")
    if not 0 <= priority <= SACN_MAX_PRIORITY:
        raise ValueError(f"priority must be 0-{SACN_MAX_PRIORITY}")

    name_bytes = _pad(source_name.encode("utf-8", errors="replace"), _SOURCE_NAME_LEN)
    property_values = bytes([0]) + data  # DMX start code + 512 channels

    dmp_len = 2 + 1 + 1 + 2 + 2 + 2 + len(property_values)
    dmp = (
        struct.pack(">H", _FLAGS | dmp_len)
        + bytes([_VECTOR_DMP_SET_PROPERTY, 0xA1])
        + struct.pack(">H", 0)  # First Property Address
        + struct.pack(">H", 1)  # Address Increment
        + struct.pack(">H", len(property_values))
        + property_values
    )

    framing_len = 2 + 4 + _SOURCE_NAME_LEN + 1 + 2 + 1 + 1 + 2 + len(dmp)
    framing = (
        struct.pack(">H", _FLAGS | framing_len)
        + struct.pack(">I", _VECTOR_E131_DATA_PACKET)
        + name_bytes
        + bytes([priority & 0xFF])
        + struct.pack(">H", 0)  # Synchronization Address: none
        + bytes([sequence & 0xFF])
        + bytes([0])  # Options
        + struct.pack(">H", universe)
        + dmp
    )

    root_len = 2 + 4 + 16 + len(framing)
    return (
        struct.pack(">H", 0x0010)  # Preamble Size
        + struct.pack(">H", 0)  # Postamble Size
        + _SACN_PACKET_ID
        + struct.pack(">H", _FLAGS | root_len)
        + struct.pack(">I", _VECTOR_ROOT_E131_DATA)
        + cid
        + framing
    )
