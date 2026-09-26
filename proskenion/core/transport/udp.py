"""UDP transport — Art-Net, sACN, RTP-MIDI (§5.5).

There is no connection to establish, so ``open()`` only binds a local socket;
whether anything is listening is the driver's ``probe()`` to find out
(ArtPoll → ArtPollReply, §5.3). ``receive()`` returns whole datagrams — the
boundary is the frame, which is exactly what these protocols rely on.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any, ClassVar

from proskenion.core.drivers.fields import Field
from proskenion.core.transport.base import BaseTransport, ConfigurationError, TransportClosed

UDP_SCHEMA: list[Field] = [
    Field("host", type="host", label="IP address", required=True),
    Field("port", type="port", label="Port", required=True, min=1, max=65535),
    Field(
        "bind_port",
        type="port",
        label="Local port",
        min=1,
        max=65535,
        help="Leave blank for any free port; protocols that reply to a fixed port need it",
    ),
    Field("broadcast", type="bool", label="Broadcast", default=False),
]


class _DatagramProtocol(asyncio.DatagramProtocol):
    def __init__(self, inbound: asyncio.Queue[bytes | None]) -> None:
        self._inbound = inbound

    def datagram_received(self, data: bytes, addr: Any) -> None:
        if self._inbound.full():
            # Continuous data: keep the newest, drop the oldest (§5.6 spirit).
            try:
                self._inbound.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self._inbound.put_nowait(data)

    def error_received(self, exc: Exception) -> None:
        # ICMP unreachable and friends surface here; the probe decides what it means.
        return None

    def connection_lost(self, exc: Exception | None) -> None:
        self._inbound.put_nowait(None)


class UdpTransport(BaseTransport):
    TYPE: ClassVar[str] = "udp"
    SCHEMA: ClassVar[list[Field]] = UDP_SCHEMA

    #: Datagrams buffered before the oldest is dropped.
    QUEUE_SIZE: ClassVar[int] = 256

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._transport: asyncio.DatagramTransport | None = None
        self._inbound: asyncio.Queue[bytes | None] = asyncio.Queue(self.QUEUE_SIZE)
        self._remote: tuple[str, int] | None = None

    @property
    def is_open(self) -> bool:
        return self._transport is not None and not self._transport.is_closing()

    @property
    def local_port(self) -> int | None:
        if self._transport is None:
            return None
        sockname = self._transport.get_extra_info("sockname")
        return int(sockname[1]) if sockname else None

    async def open(self) -> None:
        if self.is_open:
            return
        host = str(self.config["host"])
        port = int(self.config["port"])
        bind_port = int(self.config.get("bind_port") or 0)
        broadcast = bool(self.config.get("broadcast", False))
        loop = asyncio.get_running_loop()
        try:
            resolved = await loop.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
        except socket.gaierror as exc:
            raise ConfigurationError(f"cannot resolve {host!r}: {exc}") from exc
        if not resolved:
            raise ConfigurationError(f"cannot resolve {host!r}")
        family = resolved[0][0]
        self._remote = (str(resolved[0][4][0]), port)
        local_host = "::" if family == socket.AF_INET6 else "0.0.0.0"
        self._inbound = asyncio.Queue(self.QUEUE_SIZE)
        try:
            self._transport, _ = await loop.create_datagram_endpoint(
                lambda: _DatagramProtocol(self._inbound),
                local_addr=(local_host, bind_port),
                family=family,
                allow_broadcast=broadcast,
            )
        except OSError as exc:
            raise ConfigurationError(f"cannot bind UDP port {bind_port or 'any'}: {exc}") from exc

    async def close(self) -> None:
        transport, self._transport = self._transport, None
        if transport is not None:
            transport.close()

    async def send(self, data: bytes) -> None:
        if self._transport is None or self._transport.is_closing() or self._remote is None:
            raise TransportClosed("UDP socket is not open")
        try:
            self._transport.sendto(data, self._remote)
        except OSError as exc:
            raise TransportClosed(f"UDP send failed: {exc}") from exc

    async def receive(self, timeout: float) -> bytes:
        """Return the next whole datagram."""
        if self._transport is None:
            raise TransportClosed("UDP socket is not open")
        datagram = await asyncio.wait_for(self._inbound.get(), timeout)
        if datagram is None:
            raise TransportClosed("UDP socket closed")
        return datagram
