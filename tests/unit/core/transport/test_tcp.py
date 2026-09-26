"""TCP transport against a real localhost server, and transport substitution (§22.2)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from proskenion.core.drivers.base import ProbeResult
from proskenion.core.transport.base import ConfigurationError, TransportClosed
from proskenion.core.transport.loopback import LoopbackTransport
from proskenion.core.transport.tcp import TCP_SCHEMA, TcpTransport
from tests.stubs.echo_driver import EchoDriver, RecordingSink, pong_responder


class PongServer:
    """Records every byte received and answers PING with PONG."""

    def __init__(self) -> None:
        self.received: list[bytes] = []
        self.server: asyncio.Server | None = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(1024):
                self.received.append(data)
                reply = pong_responder(data)
                if reply is not None:
                    writer.write(reply)
                    await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    @property
    def port(self) -> int:
        assert self.server is not None
        return int(self.server.sockets[0].getsockname()[1])


@pytest.fixture
async def pong_server() -> AsyncIterator[PongServer]:
    server = PongServer()
    server.server = await asyncio.start_server(server._handle, "127.0.0.1", 0)
    try:
        yield server
    finally:
        server.server.close()
        await server.server.wait_closed()


def test_tcp_schema_owns_addressing() -> None:
    assert [f.key for f in TCP_SCHEMA] == ["host", "port"]
    assert TCP_SCHEMA[0].type == "host" and TCP_SCHEMA[0].required
    assert TcpTransport.SCHEMA is TCP_SCHEMA


async def test_tcp_round_trip(pong_server: PongServer) -> None:
    transport = TcpTransport({"host": "127.0.0.1", "port": pong_server.port})
    assert not transport.is_open
    await transport.open()
    assert transport.is_open
    await transport.send(b"PING\n")
    assert await transport.receive(1.0) == b"PONG\n"
    await transport.close()
    assert not transport.is_open
    with pytest.raises(TransportClosed):
        await transport.send(b"PING\n")


async def test_tcp_refused_is_a_configuration_error() -> None:
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    server.close()
    await server.wait_closed()
    transport = TcpTransport({"host": "127.0.0.1", "port": port})
    with pytest.raises(ConfigurationError):
        await transport.open()


async def test_tcp_receive_timeout_raises_timeout_error_and_leaves_the_transport_open(
    pong_server: PongServer,
) -> None:
    """Defect 1: a read timeout is a quiet socket, not a lost connection — the
    fix for `except OSError` swallowing `asyncio.wait_for`'s `TimeoutError`
    (it is a subclass of `OSError`) ahead of it. `TcpTransport` had the same
    clause as `SerialTransport`, though nothing shipped depended on it."""
    transport = TcpTransport({"host": "127.0.0.1", "port": pong_server.port})
    await transport.open()

    with pytest.raises(TimeoutError):
        await transport.receive(0.05)

    assert transport.is_open
    # the transport still works afterwards — the timeout did not close it
    await transport.send(b"PING\n")
    assert await transport.receive(1.0) == b"PONG\n"
    await transport.close()


async def test_tcp_peer_close_surfaces_as_transport_closed() -> None:
    async def close_immediately(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server = await asyncio.start_server(close_immediately, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    try:
        transport = TcpTransport({"host": "127.0.0.1", "port": port})
        await transport.open()
        with pytest.raises(TransportClosed):
            await transport.receive(1.0)
    finally:
        server.close()
        await server.wait_closed()


async def test_probe_bytes_are_identical_over_tcp_and_loopback(pong_server: PongServer) -> None:
    """Transport substitution (§22.2): the driver's probe does not know its transport."""
    tcp = TcpTransport({"host": "127.0.0.1", "port": pong_server.port})
    over_tcp = EchoDriver(1, tcp, {"greeting": "PING"}, RecordingSink())
    await over_tcp.connect()
    assert await over_tcp.probe() == ProbeResult(True)
    await over_tcp.disconnect()

    loopback = LoopbackTransport(responder=pong_responder)
    over_loopback = EchoDriver(2, loopback, {"greeting": "PING"}, RecordingSink())
    await over_loopback.connect()
    assert await over_loopback.probe() == ProbeResult(True)
    await over_loopback.disconnect()

    assert b"".join(pong_server.received) == b"".join(loopback.sent) == b"PING\n"
