"""Framing helpers — utilities a driver calls, never transport configuration.

Transports deliver bytes and datagram boundaries (§5.5 *Framing stays with the
driver*). Where a message ends is protocol-specific, so a driver wraps its
transport in a :class:`Framer` and asks for lines, delimited messages or
fixed-length blocks. Bytes read past the requested frame stay buffered for the
next call.
"""

from __future__ import annotations

import asyncio

from proskenion.core.transport.base import Transport


class Framer:
    """Buffered reader over a :class:`Transport`.

    Every read takes an overall ``timeout`` in seconds and raises
    :class:`TimeoutError` when the frame has not completed by then; whatever
    was received stays in the buffer.
    """

    def __init__(self, transport: Transport) -> None:
        self.transport = transport
        self._buffer = bytearray()

    @property
    def buffered(self) -> bytes:
        return bytes(self._buffer)

    def discard(self) -> None:
        """Drop buffered bytes — after a resynchronisation, for instance."""
        self._buffer.clear()

    async def read_until(self, delimiter: bytes, timeout: float) -> bytes:
        """Return bytes up to and excluding ``delimiter``, consuming it."""
        if not delimiter:
            raise ValueError("delimiter must not be empty")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            index = self._buffer.find(delimiter)
            if index >= 0:
                frame = bytes(self._buffer[:index])
                del self._buffer[: index + len(delimiter)]
                return frame
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("no complete frame before timeout")
            self._buffer.extend(await self.transport.receive(remaining))

    async def read_line(self, timeout: float) -> bytes:
        """Return one newline-terminated line without its terminator or a
        trailing carriage return."""
        line = await self.read_until(b"\n", timeout)
        return line[:-1] if line.endswith(b"\r") else line

    async def read_exactly(self, n: int, timeout: float) -> bytes:
        """Return exactly ``n`` bytes."""
        if n < 0:
            raise ValueError("n must not be negative")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while len(self._buffer) < n:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("fewer bytes than requested before timeout")
            self._buffer.extend(await self.transport.receive(remaining))
        frame = bytes(self._buffer[:n])
        del self._buffer[:n]
        return frame
