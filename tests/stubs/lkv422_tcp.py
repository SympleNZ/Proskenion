"""The LKV422 stub, reachable over TCP, for a serial port the application opens (§7.5, §22.5).

:class:`~tests.stubs.lkv422_stub.LKV422Stub` plays the matrix's end of a
:class:`~proskenion.core.transport.loopback.LoopbackTransport`, which exists
only inside one process. The Phase 3 milestone needs the application's own
``lkv422`` device — built by its device manager from a row configured
through ``POST /devices``, on the real serial transport — to reach that stub,
and in the browser journeys the application is another process altogether.

This module puts the unchanged stub behind a TCP listener. It owns one
loopback pair and plays the *driver's* end of it: bytes arriving on a TCP
connection are handed to the stub exactly as the driver's ``send`` would
hand them, and every reply the stub makes is written back to that
connection. The stub's routing and its record of what it received live as
long as this object, not as long as one connection, so a driver that
reconnects (or an application that reboots) finds the matrix where it left
it — as a real matrix would be.

The other half, making the application's serial transport open this
listener instead of a device node, is :mod:`tests.stubs.serial_bridge`.
Nothing in the application changes: the LKV422 driver, the serial transport
and pyserial's stream interface are the ones that run on the appliance.
"""

from __future__ import annotations

import asyncio
import contextlib

from proskenion.core.transport.loopback import LoopbackTransport
from tests.stubs.lkv422_stub import LKV422Stub, Terminator

#: How long a pump waits for the stub's next reply before looking again
#: whether its connection is still open.
_PUMP_POLL_S = 0.5


class LKV422TcpStub:
    """``async with LKV422TcpStub() as matrix:`` — the stub on a free loopback port."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        terminator: Terminator = "none",
        ack_switches: bool = False,
    ) -> None:
        self._host = host
        self._requested_port = port
        self._loopback = LoopbackTransport()
        self.stub = LKV422Stub(self._loopback, terminator=terminator, ack_switches=ack_switches)
        self._server: asyncio.Server | None = None
        self._connections: set[asyncio.Task[None]] = set()
        #: How many TCP connections have been accepted: one per time the
        #: application's serial transport opened the "port".
        self.opened = 0

    async def __aenter__(self) -> LKV422TcpStub:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    @property
    def port(self) -> int:
        assert self._server is not None and self._server.sockets
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        await self._loopback.open()
        await self.stub.start()
        self._server = await asyncio.start_server(self._serve, self._host, self._requested_port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server.close_clients()
            await self._server.wait_closed()
            self._server = None
        for task in list(self._connections):
            task.cancel()
        if self._connections:
            await asyncio.gather(*self._connections, return_exceptions=True)
        await self.stub.stop()
        await self._loopback.close()

    # -- what a test drives and reads -------------------------------------------------

    async def front_panel(self, output: str, input: str) -> None:  # noqa: A002 - §7.5's word
        """Someone at the front panel or with the IR remote: no serial traffic at all."""
        await self.stub.front_panel(output, input)

    def routing(self) -> dict[str, str]:
        return self.stub.routing()

    def commands(self) -> list[str]:
        """Every command received, in order, as text."""
        return [raw.decode("ascii", errors="replace") for raw in self.stub.received]

    # -- the bridge -----------------------------------------------------------------------

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._connections.add(task)
        self.opened += 1
        # A reply the stub made to a connection that has since closed must not
        # be read by the next one as its own.
        with contextlib.suppress(TimeoutError):
            while True:
                await self._loopback.receive(0)
        replies = asyncio.create_task(self._replies(writer))
        try:
            while chunk := await reader.read(4096):
                await self._loopback.send(chunk)  # the driver's end: to the stub
        except (ConnectionError, OSError):
            pass
        finally:
            replies.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await replies
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()
            self._connections.discard(task)

    async def _replies(self, writer: asyncio.StreamWriter) -> None:
        while not writer.is_closing():
            try:
                reply = await self._loopback.receive(_PUMP_POLL_S)
            except TimeoutError:
                continue
            try:
                writer.write(reply)
                await writer.drain()
            except (ConnectionError, OSError):
                return
