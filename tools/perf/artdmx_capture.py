"""On-the-box measurement of the real DMX frame rate (§23.1 "DMX frame rate").

On the appliance the application sends ArtDmx by **unicast** to the eDMX8's
own address, so this tool's :mod:`~tools.perf.artnet_listener` (a UDP socket
bound on 6454) never sees it: the kernel puts those datagrams on the wire,
not into a local socket. The only vantage that sees what actually leaves is
the egress interface itself, via a raw ``AF_PACKET`` socket (Linux, root or
``CAP_NET_RAW``). That is what this module opens.

Three parts, deliberately separable so the first two are testable with
crafted frames and no privileges:

* :func:`parse_ethernet_artdmx` - one Ethernet frame to an :class:`ArtDmxPacket`
  (or ``None`` if it is not IPv4/UDP to port 6454 carrying an ArtDmx);
* :func:`analyse` - interval statistics per (source, destination, universe)
  stream: median/min/max/mean/p95 interval, gaps, frames per second;
* :class:`ArtDmxCapture` - the asyncio-driven socket (``loop.add_reader``;
  no threads, CONVENTIONS.md "all async").

Timestamps are the kernel's receive time (``SO_TIMESTAMPNS``) where
available, so event-loop scheduling jitter does not pollute the intervals;
falling back to :func:`time.time` at read time.
"""

from __future__ import annotations

import asyncio
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Any

from proskenion.core.dmx import artnet
from tools.perf.stats import percentile
from tools.perf.targets import DMX_FRAME_RATE_MIN_FPS

ETH_P_ALL = 0x0003
ETH_P_IPV4 = 0x0800
ETH_P_VLAN = 0x8100
IPPROTO_UDP = 17
#: ``SO_TIMESTAMPNS`` (Linux): not exposed by every Python build's ``socket``.
SO_TIMESTAMPNS: int = getattr(socket, "SO_TIMESTAMPNS", 35)

#: Anything longer than this between two frames is below the 25 fps floor,
#: so it is counted as a gap whatever the median is.
GAP_FLOOR_MS = 1000.0 / DMX_FRAME_RATE_MIN_FPS


class CaptureUnavailable(Exception):
    """The raw capture cannot be opened here; the message says why and what to do."""


@dataclass(frozen=True, slots=True)
class ArtDmxPacket:
    at: float  # seconds; kernel receive time where known (differences only)
    src: str
    dst: str
    universe: int
    sequence: int

    @property
    def stream(self) -> tuple[str, str, int]:
        return (self.src, self.dst, self.universe)


def parse_ethernet_artdmx(frame: bytes, *, at: float = 0.0) -> ArtDmxPacket | None:
    """Decode an Ethernet frame carrying IPv4/UDP to port 6454 whose payload
    is ``Art-Net\\0`` + OpCode 0x5000 (ArtDmx). Anything else is ``None``."""
    if len(frame) < 14:
        return None
    ethertype = struct.unpack_from("!H", frame, 12)[0]
    offset = 14
    if ethertype == ETH_P_VLAN:  # 802.1Q tag, if the NIC did not strip it
        if len(frame) < 18:
            return None
        ethertype = struct.unpack_from("!H", frame, 16)[0]
        offset = 18
    if ethertype != ETH_P_IPV4 or len(frame) < offset + 20:
        return None
    version_ihl = frame[offset]
    if version_ihl >> 4 != 4:
        return None
    ihl = (version_ihl & 0x0F) * 4
    if ihl < 20 or frame[offset + 9] != IPPROTO_UDP:
        return None
    if struct.unpack_from("!H", frame, offset + 6)[0] & 0x1FFF:  # not the first fragment
        return None
    src = socket.inet_ntoa(frame[offset + 12 : offset + 16])
    dst = socket.inet_ntoa(frame[offset + 16 : offset + 20])
    udp = offset + ihl
    if len(frame) < udp + 8:
        return None
    dport = struct.unpack_from("!H", frame, udp + 2)[0]
    if dport != artnet.ARTNET_PORT:
        return None
    payload = frame[udp + 8 :]
    if len(payload) < 18 or payload[:8] != artnet.ARTNET_ID:
        return None
    if struct.unpack_from("<H", payload, 8)[0] != artnet.OP_DMX:
        return None
    return ArtDmxPacket(
        at=at,
        src=src,
        dst=dst,
        universe=(payload[15] << 8) | payload[14],
        sequence=payload[12],
    )


@dataclass(frozen=True, slots=True)
class StreamStats:
    """Interval statistics for one source/destination/universe stream."""

    src: str
    dst: str
    universe: int
    frames: int
    span_s: float
    fps: float
    median_ms: float | None
    min_ms: float | None
    max_ms: float | None
    mean_ms: float | None
    p95_ms: float | None
    gap_threshold_ms: float | None
    gaps: int
    sequence_skips: int
    intervals_ms: list[float] = field(default_factory=list, repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "src": self.src,
            "dst": self.dst,
            "universe": self.universe,
            "frames": self.frames,
            "span_s": self.span_s,
            "fps": self.fps,
            "interval_median_ms": self.median_ms,
            "interval_min_ms": self.min_ms,
            "interval_max_ms": self.max_ms,
            "interval_mean_ms": self.mean_ms,
            "interval_p95_ms": self.p95_ms,
            "gap_threshold_ms": self.gap_threshold_ms,
            "gaps": self.gaps,
            "sequence_skips": self.sequence_skips,
        }


def analyse(packets: list[ArtDmxPacket]) -> list[StreamStats]:
    """Per-stream interval statistics, busiest stream first.

    ``fps`` is ``(frames - 1) / span`` (first to last frame), so the window's
    edges do not count a frame that was never timed. A *gap* is an interval
    over ``max(2 x median, 40 ms)`` - 40 ms being the 25 fps floor of the
    target. ``sequence_skips`` counts jumps in the ArtDmx sequence byte
    (1-255, 0 = disabled): frames the sender numbered that this capture never
    saw.
    """
    streams: dict[tuple[str, str, int], list[ArtDmxPacket]] = {}
    for packet in packets:
        streams.setdefault(packet.stream, []).append(packet)
    stats: list[StreamStats] = []
    for (src, dst, universe), unsorted in streams.items():
        frames = sorted(unsorted, key=lambda p: p.at)
        intervals = [(frames[i].at - frames[i - 1].at) * 1000.0 for i in range(1, len(frames))]
        span = frames[-1].at - frames[0].at
        skips = 0
        for previous, current in zip(frames, frames[1:], strict=False):
            if previous.sequence and current.sequence:
                if current.sequence != previous.sequence % 255 + 1:
                    skips += 1
        if not intervals:
            stats.append(
                StreamStats(
                    src, dst, universe, len(frames), 0.0, 0.0,
                    None, None, None, None, None, None, 0, skips,
                )
            )
            continue
        median = percentile(intervals, 50.0)
        threshold = max(2.0 * median, GAP_FLOOR_MS)
        stats.append(
            StreamStats(
                src=src,
                dst=dst,
                universe=universe,
                frames=len(frames),
                span_s=span,
                fps=(len(frames) - 1) / span if span > 0 else 0.0,
                median_ms=median,
                min_ms=min(intervals),
                max_ms=max(intervals),
                mean_ms=sum(intervals) / len(intervals),
                p95_ms=percentile(intervals, 95.0),
                gap_threshold_ms=threshold,
                gaps=sum(1 for i in intervals if i > threshold),
                sequence_skips=skips,
                intervals_ms=intervals,
            )
        )
    return sorted(stats, key=lambda s: s.frames, reverse=True)


class ArtDmxCapture:
    """Raw ``AF_PACKET`` capture of ArtDmx crossing ``iface``.

    ``start()`` raises :class:`CaptureUnavailable` with an explanation if this
    is not Linux or the process lacks ``CAP_NET_RAW`` (run under ``sudo``).
    :attr:`packets` grows while running; take ``len(capture.packets)`` as a
    mark to window a measurement.
    """

    def __init__(self, iface: str = "eth0") -> None:
        self.iface = iface
        self.packets: list[ArtDmxPacket] = []
        self._sock: socket.socket | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.timestamps = "kernel"

    def start(self) -> None:
        af_packet = getattr(socket, "AF_PACKET", None)
        if af_packet is None:
            raise CaptureUnavailable(
                "raw packet capture needs Linux (AF_PACKET); run this on the appliance"
            )
        try:
            sock = socket.socket(af_packet, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
        except PermissionError as exc:
            raise CaptureUnavailable(
                "opening a raw packet socket needs root (CAP_NET_RAW): re-run this under "
                "sudo, e.g. `sudo /opt/auditorium/venv/bin/python -m tools.perf ...`"
            ) from exc
        except OSError as exc:
            raise CaptureUnavailable(f"could not open a raw packet socket: {exc}") from exc
        try:
            sock.bind((self.iface, 0))
        except OSError as exc:
            sock.close()
            raise CaptureUnavailable(
                f"could not bind the capture to interface {self.iface!r}: {exc} "
                "(pass --dmx-capture-iface)"
            ) from exc
        sock.setblocking(False)
        try:
            sock.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPNS, 1)
        except OSError:
            self.timestamps = "read-time"
        self._sock = sock
        self._loop = asyncio.get_running_loop()
        self._loop.add_reader(sock.fileno(), self._readable)

    def stop(self) -> None:
        if self._sock is not None and self._loop is not None:
            self._loop.remove_reader(self._sock.fileno())
        if self._sock is not None:
            self._sock.close()
        self._sock = None

    def _readable(self) -> None:
        assert self._sock is not None
        # Drain what is queued; one wake-up may carry several frames.
        for _ in range(256):
            try:
                data, ancdata, _flags, _addr = self._sock.recvmsg(65535, 256)
            except OSError:  # BlockingIOError: queue drained
                return
            at = _kernel_timestamp(ancdata)
            if at is None:
                at = time.time()
            packet = parse_ethernet_artdmx(data, at=at)
            if packet is not None:
                self.packets.append(packet)


def _kernel_timestamp(ancdata: list[tuple[int, int, bytes]]) -> float | None:
    for level, kind, data in ancdata:
        if level == socket.SOL_SOCKET and kind == SO_TIMESTAMPNS and len(data) >= 16:
            seconds, nanos = struct.unpack_from("@ll", data)
            return float(seconds) + nanos / 1e9
    return None


__all__ = [
    "ArtDmxCapture",
    "ArtDmxPacket",
    "CaptureUnavailable",
    "StreamStats",
    "analyse",
    "parse_ethernet_artdmx",
]
