from __future__ import annotations

import pytest

from proskenion.core.transport.base import ConfigurationError, TransportClosed
from proskenion.core.transport.udp import UDP_SCHEMA, UdpTransport


def test_udp_schema() -> None:
    assert [f.key for f in UDP_SCHEMA] == ["host", "port", "bind_port", "broadcast"]


async def test_udp_delivers_whole_datagrams() -> None:
    receiver = UdpTransport({"host": "127.0.0.1", "port": 1})
    await receiver.open()
    assert receiver.local_port
    sender = UdpTransport({"host": "127.0.0.1", "port": receiver.local_port})
    await sender.open()
    try:
        await sender.send(b"ArtPoll")
        await sender.send(b"second datagram")
        assert await receiver.receive(1.0) == b"ArtPoll"
        assert await receiver.receive(1.0) == b"second datagram"
        with pytest.raises(TimeoutError):
            await receiver.receive(0.02)
    finally:
        await sender.close()
        await receiver.close()
    with pytest.raises(TransportClosed):
        await sender.send(b"x")


async def test_udp_bind_port_conflict_is_a_configuration_error() -> None:
    first = UdpTransport({"host": "127.0.0.1", "port": 1})
    await first.open()
    try:
        second = UdpTransport({"host": "127.0.0.1", "port": 1, "bind_port": first.local_port})
        with pytest.raises(ConfigurationError, match="cannot bind"):
            await second.open()
    finally:
        await first.close()


async def test_udp_open_proves_nothing_about_the_device() -> None:
    # No connection exists to establish (§5.3): opening succeeds with nobody listening.
    transport = UdpTransport({"host": "127.0.0.1", "port": 6454})
    await transport.open()
    try:
        await transport.send(b"ArtPoll")
    finally:
        await transport.close()
