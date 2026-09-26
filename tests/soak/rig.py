"""The stubs that stand in for the auditorium's devices during stage 1 (D3, §22.5).

They are the ordinary protocol stubs in ``tests/stubs``, started in the
harness's own process on **fixed loopback ports**, so the soak's
configuration can name them before anything is running and the CM5 needs
nothing but loopback:

=======================  ======================  ===============================
Device                   Address                 Stub
=======================  ======================  ===============================
KNX (knxd's TCP port)    127.0.0.1:16720         :class:`KnxdStub`
Art-Net node (eDMX8)     127.0.0.2:16454         :class:`ArtNetStub`
PJLink projector         127.0.0.1:14352         :class:`PJLinkStub`
CQ-20B MIDI              127.0.0.1:51325         :class:`CqMidiStub`
CQ-20B native (meters)   127.0.0.1:51326         :class:`CqNativeStub`
HDMI matrix              —                       the in-app ``stub`` driver
=======================  ======================  ===============================

The node is on **127.0.0.2**, not 127.0.0.1. The application counts desk
frames only from the node's own address and never from its own
(:class:`~proskenion.core.dmx.artnet.ArtNetReceiver`); with the node on
127.0.0.1 the two would be the same address and every simulated desk frame
would be discarded as the controller's own. Linux routes all of 127/8 to the
loopback interface, so 127.0.0.2 needs no configuration.

The CQ-20B's ports are the real ones because the native port is fixed in the
driver (§7.3: 51326 on the same host as MIDI) and the soak changes nothing in
the application to move it. The HDMI matrix is serial on the rig; the soak
uses the application's own loopback ``stub`` matrix driver (Q8) rather than
open the real ``/dev/hdmi-matrix``.

The stubs record everything they receive, which is what a test wants and a
72-hour run cannot afford: Art-Net alone arrives at 25-40 frames a second.
:meth:`SoakStubs.trim` empties the records every few seconds and keeps only
counts.

The mixer cycle (:meth:`kill_mixer`, :meth:`blackhole_mixer`,
:meth:`restore_mixer`) is §7.3's two ways of losing the desk:

* **refused** — nothing listens on 51325, so a connection attempt is answered
  with a reset at once: the driver's "Another MIDI client is connected",
  amber;
* **timed out** — something listens but never accepts: a socket with a
  backlog of one that is already full, so the kernel drops every further
  connection attempt and the driver's connect waits out its timeout: "Mixer
  offline.", red. This is a Linux behaviour (a full accept queue drops the
  SYN); :meth:`blackhole_mixer` reports whether it holds.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from dataclasses import dataclass, field
from typing import Final

from proskenion.core.dmx import artnet
from tests.stubs.artnet_stub import ArtNetStub
from tests.stubs.cq_midi_stub import CqMidiStub, _stub_db_to_value
from tests.stubs.cq_native_stub import CqNativeStub
from tests.stubs.knxd_stub import KnxdStub
from tests.stubs.pjlink_stub import PJLinkStub

HOST: Final = "127.0.0.1"
NODE_HOST: Final = "127.0.0.2"
KNXD_PORT: Final = 16720
NODE_PORT: Final = 16454
PJLINK_PORT: Final = 14352
CQ_MIDI_PORT: Final = 51325
CQ_NATIVE_PORT: Final = 51326
#: The stub projector's PJLink password — the stub's own, not the venue's.
PJLINK_STUB_PASSWORD: Final = "soak-stub"
#: The controller's own Art-Net port: where a node's frames arrive (§7.2.5).
CONTROLLER_ARTNET: Final = (HOST, artnet.ARTNET_PORT)

#: CQ-20B addresses the soak's mixer channels use (docs/protocols/cq20b.md §3, §5).
LEVEL_IP1: Final = (0x40, 0x00)
LEVEL_IP2: Final = (0x40, 0x01)
MUTE_IP1: Final = (0x00, 0x00)
MUTE_IP2: Final = (0x00, 0x01)
LEVEL_MAIN: Final = (0x4F, 0x00)
MUTE_MAIN: Final = (0x00, 0x44)

#: What the desk holds when the soak starts.
DESK_STATE: Final[dict[tuple[int, int], int]] = {
    LEVEL_MAIN: _stub_db_to_value(-2.0),
    MUTE_MAIN: 0,
    LEVEL_IP1: _stub_db_to_value(-12.0),
    MUTE_IP1: 0,
    LEVEL_IP2: _stub_db_to_value(-14.0),
    MUTE_IP2: 0,
}


@dataclass
class Counts:
    """What the stubs received, since the records are emptied as they go."""

    artdmx_frames: int = 0
    artpolls: int = 0
    knx_writes: int = 0
    pjlink_commands: int = 0
    midi_messages: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


@dataclass
class SoakStubs:
    """Every device stub on its fixed port. ``async with`` starts and stops them."""

    knxd: KnxdStub = field(default_factory=lambda: KnxdStub(HOST, KNXD_PORT))
    node: ArtNetStub = field(
        default_factory=lambda: ArtNetStub(short_name="Soak node", long_name="Soak Art-Net node")
    )
    pjlink: PJLinkStub = field(
        default_factory=lambda: PJLinkStub(
            HOST,
            PJLINK_PORT,
            password=PJLINK_STUB_PASSWORD,
            warm_seconds=2.0,
            cool_seconds=2.0,
            initial_power="1",
        )
    )
    cq_midi: CqMidiStub = field(
        default_factory=lambda: CqMidiStub(HOST, CQ_MIDI_PORT, state=dict(DESK_STATE))
    )
    cq_native: CqNativeStub = field(default_factory=lambda: CqNativeStub(HOST, CQ_NATIVE_PORT))
    counts: Counts = field(default_factory=Counts)
    _blackhole: list[socket.socket] = field(default_factory=list)
    _native_running: bool = False

    async def __aenter__(self) -> SoakStubs:
        await self.knxd.start()
        await self.node.start(NODE_HOST, NODE_PORT)
        await self.pjlink.start()
        await self.cq_midi.start()
        await self.cq_native.start()
        self._native_running = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._close_blackhole()
        for stop in (self.cq_midi.stop, self.pjlink.stop, self.node.stop, self.knxd.stop):
            with contextlib.suppress(Exception):
                await stop()
        if self._native_running:
            with contextlib.suppress(Exception):
                await self.cq_native.stop()

    # -- keeping 72 hours of records out of memory ---------------------------------

    def trim(self) -> None:
        """Count and empty every record the stubs keep."""
        self.counts.artdmx_frames += len(self.node.received)
        self.node.received.clear()
        self.counts.artpolls += len(self.node.polls)
        self.node.polls.clear()
        self.counts.knx_writes += len(self.knxd.writes)
        self.knxd.writes.clear()
        self.counts.pjlink_commands += len(self.pjlink.received)
        self.pjlink.received.clear()
        self.counts.midi_messages += len(self.cq_midi.messages)
        self.cq_midi.clear_record()
        self.cq_native.keepalives_received.clear()

    async def trim_forever(self, every_s: float = 5.0) -> None:
        while True:
            await asyncio.sleep(every_s)
            self.trim()

    # -- loads ----------------------------------------------------------------------

    async def knx_telegram(self, group_address: str, value: bool, source: str) -> None:
        await self.knxd.send_telegram(group_address, "1.001", value, source_address=source)

    def desk_frame(self, universe: int, data: bytes, sequence: int) -> None:
        """One ArtDmx frame from the node, as its DMX-IN port broadcasts a desk (§7.2.7)."""
        packet = artnet.encode_art_dmx(universe, data, sequence=sequence)
        transport = self.node._transport
        if transport is not None:
            transport.sendto(packet, CONTROLLER_ARTNET)

    # -- the mixer cycle (§7.3) -------------------------------------------------------

    async def kill_mixer(self) -> None:
        """The desk goes away: MIDI refuses and the metering connection is dropped."""
        await self.cq_midi.refuse_connections()
        if self._native_running:
            await self.cq_native.stop()
            self._native_running = False

    def blackhole_mixer(self) -> bool:
        """Listen on 51325 without ever accepting, so connects time out.

        Returns whether a further connection attempt really does hang — true
        on Linux, where a full accept queue drops the SYN.
        """
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((HOST, CQ_MIDI_PORT))
        listener.listen(0)
        self._blackhole.append(listener)
        # Fill the queue: one connection completes and is never accepted.
        for _ in range(2):
            filler = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            filler.setblocking(False)
            with contextlib.suppress(BlockingIOError, OSError):
                filler.connect((HOST, CQ_MIDI_PORT))
            self._blackhole.append(filler)
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(1.0)
        try:
            probe.connect((HOST, CQ_MIDI_PORT))
        except TimeoutError:
            return True
        except OSError:
            return False
        finally:
            probe.close()
        return False

    def _close_blackhole(self) -> None:
        while self._blackhole:
            with contextlib.suppress(OSError):
                self._blackhole.pop().close()

    async def restore_mixer(self) -> None:
        """The desk comes back, on the same ports."""
        self._close_blackhole()
        await self.cq_midi.accept_connections()
        if not self._native_running:
            self.cq_native = CqNativeStub(HOST, CQ_NATIVE_PORT)
            await self.cq_native.start()
            self._native_running = True

    def change_desk_level(self, address: tuple[int, int], db: float) -> int:
        """Someone at MixPad moves a fader while the controller is away."""
        value = _stub_db_to_value(db)
        self.cq_midi.state[address] = value
        return value
