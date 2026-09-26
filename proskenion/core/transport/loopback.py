"""Loopback transport — stub drivers and tests (§5.5).

An in-memory pair. The driver talks through the normal ``send``/``receive``
face; a stub or a test plays the device end through ``peer_send`` and
``peer_receive``, or hands in a ``responder`` that answers each write.

It can be told to fail ``open`` with :class:`ConfigurationError` (the *config*
failure kind) or simply to open and never answer (the *device* kind) — which
is exactly the distinction §5.3 requires every driver to surface.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, ClassVar

from proskenion.core.drivers.fields import Field
from proskenion.core.transport.base import BaseTransport, ConfigurationError, TransportClosed

Responder = Callable[[bytes], bytes | None]


class LoopbackTransport(BaseTransport):
    TYPE: ClassVar[str] = "loopback"
    SCHEMA: ClassVar[list[Field]] = []

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        fail_open: str | None = None,
        responder: Responder | None = None,
    ) -> None:
        super().__init__(config)
        self.fail_open = fail_open
        self.responder = responder
        self.sent: list[bytes] = []
        self.open_count = 0
        self._open = False
        self._to_driver: asyncio.Queue[bytes] = asyncio.Queue()
        self._to_peer: asyncio.Queue[bytes] = asyncio.Queue()

    @property
    def is_open(self) -> bool:
        return self._open

    # -- driver end ---------------------------------------------------------

    async def open(self) -> None:
        self.open_count += 1
        if self.fail_open is not None:
            raise ConfigurationError(self.fail_open)
        self._open = True

    async def close(self) -> None:
        self._open = False

    async def send(self, data: bytes) -> None:
        if not self._open:
            raise TransportClosed("loopback is not open")
        self.sent.append(data)
        await self._to_peer.put(data)
        if self.responder is not None:
            reply = self.responder(data)
            if reply is not None:
                await self._to_driver.put(reply)

    async def receive(self, timeout: float) -> bytes:
        if not self._open:
            raise TransportClosed("loopback is not open")
        return await asyncio.wait_for(self._to_driver.get(), timeout)

    # -- device end ---------------------------------------------------------

    async def peer_send(self, data: bytes) -> None:
        """Deliver bytes to the driver as though the device had sent them."""
        await self._to_driver.put(data)

    async def peer_receive(self, timeout: float) -> bytes:
        """Take the next bytes the driver sent."""
        return await asyncio.wait_for(self._to_peer.get(), timeout)
