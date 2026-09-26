"""UDP Art-Net stub node (§22.2): answers ArtPoll with ArtPollReply, records
every ArtDmx it receives with the time it arrived, and emits ArtDmx on demand
from a configurable source address.

A real UDP socket, played from the device end — the tests that use it drive
the ``artnet`` driver over an ordinary :class:`~proskenion.core.transport.udp.UdpTransport`,
exactly as it would run against a real eDMX8 MAX.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from proskenion.core.dmx import artnet


@dataclass
class ReceivedArtDmx:
    at: float  # time.monotonic() when the datagram arrived
    universe: int
    data: bytes
    source: str
    sequence: int = 0


class ArtNetStub:
    """Bind with :meth:`start`, close with :meth:`stop`."""

    def __init__(
        self,
        *,
        short_name: str = "Proskenion stub",
        long_name: str = "Proskenion Art-Net test stub",
        firmware_version: tuple[int, int] = (1, 0),
        ports: tuple[artnet.ArtNetPort, ...] = (
            artnet.ArtNetPort(port_type=0xC0, good_input=0x80, good_output=0x80, sw_in=0, sw_out=0),
        ),
        reply_to_poll: bool = True,
        reply_to: tuple[str, int] | None = None,
    ) -> None:
        self.short_name = short_name
        self.long_name = long_name
        self.firmware_version = firmware_version
        self.ports = ports
        #: Set False to simulate a node that never answers (§11.1 device failure).
        self.reply_to_poll = reply_to_poll
        #: Where ArtPollReply goes. ``None`` answers the poll's source; set
        #: it to a fixed address to behave as the eDMX8 MAX does, which
        #: broadcasts every reply to port 6454 whatever port the poll left from.
        self.reply_to = reply_to
        self.polls: list[tuple[str, int]] = []
        self.received: list[ReceivedArtDmx] = []
        self._transport: asyncio.DatagramTransport | None = None

    @property
    def port(self) -> int:
        assert self._transport is not None
        return int(self._transport.get_extra_info("sockname")[1])

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> None:
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _StubProtocol(self), local_addr=(host, port)
        )

    async def stop(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def _handle(self, data: bytes, addr: tuple[str, int]) -> None:
        opcode = artnet.art_net_opcode(data)
        if opcode == artnet.OP_POLL:
            self.polls.append(addr)
            if not self.reply_to_poll:
                return
            self.send_poll_reply(self.reply_to or addr)
        elif opcode == artnet.OP_DMX:
            frame = artnet.decode_art_dmx(data)
            self.received.append(
                ReceivedArtDmx(
                    at=time.monotonic(),
                    universe=frame.universe,
                    data=frame.data,
                    source=addr[0],
                    sequence=frame.sequence,
                )
            )
        # Anything else (including another ArtPollReply): not this stub's concern.

    def send_poll_reply(self, to: tuple[str, int]) -> None:
        """Send one ArtPollReply to ``to``, polled or not — as a node does
        when another controller on the VLAN polls it."""
        assert self._transport is not None
        local_ip = self._transport.get_extra_info("sockname")[0]
        reply = artnet.encode_art_poll_reply(
            ip_address=local_ip if local_ip not in ("0.0.0.0", "::") else "127.0.0.1",
            firmware_version=self.firmware_version,
            short_name=self.short_name,
            long_name=self.long_name,
            ports=self.ports,
        )
        self._transport.sendto(reply, to)

    async def emit_art_dmx(
        self, universe: int, data: bytes, *, to: tuple[str, int], sequence: int = 0
    ) -> None:
        """Send one ArtDmx frame to ``to``, as though from this stub's bound
        address — used to exercise the controller's ArtDmx-in path."""
        assert self._transport is not None
        packet = artnet.encode_art_dmx(universe, data, sequence=sequence)
        self._transport.sendto(packet, to)


class _StubProtocol(asyncio.DatagramProtocol):
    def __init__(self, stub: ArtNetStub) -> None:
        self._stub = stub

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._stub._handle(data, addr)
