"""TCP transport — CQ-20B, PJLink, Ethernet matrices (§5.5).

``TCP_SCHEMA`` is defined once here and shared by every TCP driver; a driver's
own schema never carries a host.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any, ClassVar

from proskenion.core.drivers.fields import Field
from proskenion.core.transport.base import BaseTransport, ConfigurationError, TransportClosed

TCP_SCHEMA: list[Field] = [
    Field(
        "host",
        type="host",
        label="IP address",
        required=True,
        help="Static address; must match the switch reservation",
    ),
    Field("port", type="port", label="Port", required=True, min=1, max=65535),
]


class TcpTransport(BaseTransport):
    TYPE: ClassVar[str] = "tcp"
    SCHEMA: ClassVar[list[Field]] = TCP_SCHEMA

    #: Seconds allowed for the connection to establish before ``open`` gives up.
    CONNECT_TIMEOUT: ClassVar[float] = 10.0
    #: Largest single read handed back by ``receive``.
    READ_SIZE: ClassVar[int] = 4096

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    @property
    def is_open(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def open(self) -> None:
        if self.is_open:
            return
        host = str(self.config["host"])
        port = int(self.config["port"])
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), self.CONNECT_TIMEOUT
            )
        except TimeoutError as exc:
            raise ConfigurationError(f"no answer from {host}:{port} — check the address") from exc
        except socket.gaierror as exc:
            raise ConfigurationError(f"cannot resolve {host!r}: {exc}") from exc
        except OSError as exc:
            # Refused, unreachable, no route — all configuration territory (§5.3).
            raise ConfigurationError(f"cannot connect to {host}:{port}: {exc}") from exc

    async def close(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is None:
            return
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass

    async def send(self, data: bytes) -> None:
        if self._writer is None or self._writer.is_closing():
            raise TransportClosed("TCP connection is not open")
        try:
            self._writer.write(data)
            await self._writer.drain()
        except (OSError, ConnectionError) as exc:
            raise TransportClosed(f"TCP connection lost: {exc}") from exc

    async def receive(self, timeout: float) -> bytes:
        if self._reader is None:
            raise TransportClosed("TCP connection is not open")
        try:
            data = await asyncio.wait_for(self._reader.read(self.READ_SIZE), timeout)
        except TimeoutError:
            # A quiet socket, not a dropped connection: asyncio.wait_for's
            # TimeoutError is a subclass of OSError, so it must be let
            # through here, ahead of the OSError clause below, or every
            # timed-out read is reported as a lost connection (see the
            # transport contract).
            raise
        except (OSError, ConnectionError) as exc:
            raise TransportClosed(f"TCP connection lost: {exc}") from exc
        if not data:
            raise TransportClosed("TCP connection closed by peer")
        return data
