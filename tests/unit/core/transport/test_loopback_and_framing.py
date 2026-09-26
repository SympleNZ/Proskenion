from __future__ import annotations

import pytest

from proskenion.core.transport.base import ConfigurationError, Transport, TransportClosed
from proskenion.core.transport.framing import Framer
from proskenion.core.transport.loopback import LoopbackTransport


def test_loopback_is_a_transport() -> None:
    assert isinstance(LoopbackTransport(), Transport)
    assert LoopbackTransport.SCHEMA == []


async def test_loopback_plays_both_ends() -> None:
    transport = LoopbackTransport()
    with pytest.raises(TransportClosed):
        await transport.send(b"x")
    await transport.open()
    await transport.send(b"hello")
    assert await transport.peer_receive(0.1) == b"hello"
    await transport.peer_send(b"world")
    assert await transport.receive(0.1) == b"world"
    assert transport.sent == [b"hello"]
    await transport.close()
    with pytest.raises(TransportClosed):
        await transport.receive(0.1)
    assert await transport.enumerate() is None


async def test_loopback_can_fail_open() -> None:
    transport = LoopbackTransport(fail_open="no such device")
    with pytest.raises(ConfigurationError, match="no such device"):
        await transport.open()
    assert not transport.is_open


async def test_loopback_opens_but_never_answers() -> None:
    transport = LoopbackTransport()
    await transport.open()
    await transport.send(b"PING\n")
    with pytest.raises(TimeoutError):
        await transport.receive(0.02)


async def test_loopback_responder_answers_each_write() -> None:
    transport = LoopbackTransport(responder=lambda data: data.upper())
    await transport.open()
    await transport.send(b"abc")
    assert await transport.receive(0.1) == b"ABC"


async def test_framer_lines_across_chunk_boundaries() -> None:
    transport = LoopbackTransport()
    await transport.open()
    framer = Framer(transport)
    await transport.peer_send(b"PA1")
    await transport.peer_send(b"R\r\nPS2")
    assert await framer.read_line(0.2) == b"PA1R"
    await transport.peer_send(b"1R\n")
    assert await framer.read_line(0.2) == b"PS21R"
    assert framer.buffered == b""


async def test_framer_read_until_and_exactly() -> None:
    transport = LoopbackTransport()
    await transport.open()
    framer = Framer(transport)
    await transport.peer_send(b"abc;defgh")
    assert await framer.read_until(b";", 0.2) == b"abc"
    assert await framer.read_exactly(2, 0.2) == b"de"
    assert framer.buffered == b"fgh"
    with pytest.raises(TimeoutError):
        await framer.read_exactly(5, 0.02)
    assert framer.buffered == b"fgh"  # partial data is kept
    framer.discard()
    assert framer.buffered == b""


async def test_framer_timeout_when_no_delimiter() -> None:
    transport = LoopbackTransport()
    await transport.open()
    framer = Framer(transport)
    await transport.peer_send(b"no terminator")
    with pytest.raises(TimeoutError):
        await framer.read_line(0.02)
    assert framer.buffered == b"no terminator"
