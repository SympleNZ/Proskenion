"""A passive Art-Net frame listener for the §23.1 DMX frame-rate scenario.

This is **not** a copy of ``tests/stubs/artnet_stub.py``: that stub also
plays the device end of a driver test (poll replies on demand, frames
injected on request) and lives under ``tests/`` on purpose, so a production-
adjacent tool never depends on test-only code. This module only receives —
it answers ArtPoll (so the application's ``artnet`` driver, which polls
before it trusts a destination, sees a node there) and records every ArtDmx
frame's arrival time, universe and sequence number. Decoding is
:mod:`proskenion.core.dmx.artnet`, the same module the real driver uses, so
a framing bug would show up identically in both.

**What this can and cannot see** (see ``tools/perf/README.md``): the
application sends ArtDmx to wherever the ``artnet`` lighting output device
is configured to reach — the eDMX8 MAX's own address on the real rig, a
loopback port in the self-test rig. Binding ``0.0.0.0:6454`` here only
receives that traffic when this process is on the same host or the same
broadcast domain as that destination. A laptop off the VLAN sees nothing,
which is expected, not a bug in this listener.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from proskenion.core.dmx import artnet

#: Art-Net's fixed port (spec §9.5, matching ``tests/stubs/artnet_stub.py``).
DEFAULT_PORT = 6454


@dataclass(slots=True)
class ReceivedFrame:
    at: float  # time.monotonic() on arrival — this listener's own clock
    universe: int
    sequence: int


class ArtNetListener:
    """Bind with :meth:`start`, close with :meth:`stop`; ``async with`` does
    both. :attr:`frames` accumulates for as long as it is running."""

    def __init__(self) -> None:
        self.frames: list[ReceivedFrame] = []
        self._transport: asyncio.DatagramTransport | None = None

    async def start(self, host: str, port: int) -> None:
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _Protocol(self), local_addr=(host, port), allow_broadcast=True
        )

    async def stop(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    @property
    def port(self) -> int:
        assert self._transport is not None
        return int(self._transport.get_extra_info("sockname")[1])

    async def __aenter__(self) -> ArtNetListener:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    def _handle(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            opcode = artnet.art_net_opcode(data)
        except Exception:  # noqa: BLE001 - a malformed or unrelated UDP datagram
            return
        if opcode == artnet.OP_POLL:
            self._reply_to_poll(addr)
        elif opcode == artnet.OP_DMX:
            frame = artnet.decode_art_dmx(data)
            self.frames.append(
                ReceivedFrame(at=time.monotonic(), universe=frame.universe, sequence=frame.sequence)
            )

    def _reply_to_poll(self, addr: tuple[str, int]) -> None:
        """Answer ArtPoll so a driver that gates on a probe reply (§7.5, and
        the wizard's own ``wait_for_status``) sees a node at this address —
        needed only for the self-test rig, which points the device straight
        at this listener; a real eDMX8 MAX answers its own polls."""
        assert self._transport is not None
        local_ip = self._transport.get_extra_info("sockname")[0]
        reply = artnet.encode_art_poll_reply(
            ip_address=local_ip if local_ip not in ("0.0.0.0", "::") else "127.0.0.1",
            firmware_version=(1, 0),
            short_name="perf harness listener",
            long_name="Proskenion perf harness Art-Net listener (tools/perf)",
            ports=(
                artnet.ArtNetPort(
                    port_type=0xC0, good_input=0x80, good_output=0x80, sw_in=0, sw_out=0
                ),
            ),
        )
        self._transport.sendto(reply, addr)

    def frames_for(self, universe: int, *, since: float = 0.0) -> list[ReceivedFrame]:
        return [f for f in self.frames if f.universe == universe and f.at >= since]


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, listener: ArtNetListener) -> None:
        self._listener = listener

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._listener._handle(data, addr)
