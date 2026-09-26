"""Unix-domain socket transport — knxd (§5.5).

Type name ``unix_socket`` as the §5.5 transport table has it.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from proskenion.core.drivers.fields import Field
from proskenion.core.transport.base import BaseTransport, ConfigurationError, TransportClosed

UNIX_SOCKET_SCHEMA: list[Field] = [
    Field("path", type="device_path", label="Socket path", required=True),
]


class UnixSocketTransport(BaseTransport):
    TYPE: ClassVar[str] = "unix_socket"
    SCHEMA: ClassVar[list[Field]] = UNIX_SOCKET_SCHEMA

    CONNECT_TIMEOUT: ClassVar[float] = 10.0
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
        path = str(self.config["path"])
        opener = getattr(asyncio, "open_unix_connection", None)
        if opener is None:
            raise ConfigurationError("Unix-domain sockets are not available on this platform")
        try:
            self._reader, self._writer = await asyncio.wait_for(
                opener(path), self.CONNECT_TIMEOUT
            )
        except TimeoutError as exc:
            raise ConfigurationError(f"no answer on socket {path}") from exc
        except OSError as exc:
            raise ConfigurationError(f"cannot connect to socket {path}: {exc}") from exc

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
            raise TransportClosed("socket is not open")
        try:
            self._writer.write(data)
            await self._writer.drain()
        except (OSError, ConnectionError) as exc:
            raise TransportClosed(f"socket lost: {exc}") from exc

    async def receive(self, timeout: float) -> bytes:
        if self._reader is None:
            raise TransportClosed("socket is not open")
        try:
            data = await asyncio.wait_for(self._reader.read(self.READ_SIZE), timeout)
        except (OSError, ConnectionError) as exc:
            raise TransportClosed(f"socket lost: {exc}") from exc
        if not data:
            raise TransportClosed("socket closed by peer")
        return data
