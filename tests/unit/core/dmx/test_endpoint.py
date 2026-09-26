"""The one Art-Net socket (spec §7.2.5): shared, never doubled, rebound cleanly.

Every test here uses real UDP sockets on localhost. The root conftest points
:attr:`ArtNetEndpoint.default_port` at an OS-chosen port, so nothing here
competes for the real 6454; a test that needs a fixed port picks a free one.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from proskenion.core.dmx import artnet
from proskenion.core.dmx.endpoint import ArtNetEndpoint
from proskenion.core.transport.base import TransportClosed


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("0.0.0.0", 0))
        return int(probe.getsockname()[1])


def _ignore(data: bytes, source: str) -> None:
    return None


def test_production_binds_the_art_net_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()  # the root conftest's test-suite port
    assert ArtNetEndpoint.default_port == artnet.ARTNET_PORT == 6454


async def test_two_holders_share_one_socket() -> None:
    first = await ArtNetEndpoint.acquire(_ignore)
    second = await ArtNetEndpoint.acquire(_ignore)
    try:
        assert first.endpoint is second.endpoint
        assert first.local_port == second.local_port
        assert len(ArtNetEndpoint.open_endpoints()) == 1
        assert first.endpoint.lease_count == 2
    finally:
        await first.release()
        assert ArtNetEndpoint.open_endpoints() != {}  # the second still holds it
        await second.release()
    assert ArtNetEndpoint.open_endpoints() == {}


async def test_concurrent_acquires_bind_once() -> None:
    port = free_udp_port()
    leases = await asyncio.gather(*(ArtNetEndpoint.acquire(_ignore, port=port) for _ in range(5)))
    try:
        assert len({id(lease.endpoint) for lease in leases}) == 1
        assert leases[0].local_port == port
    finally:
        for lease in leases:
            await lease.release()


async def test_release_and_rebind_the_same_fixed_port_repeatedly() -> None:
    # A device edit or a driver restart releases and re-acquires at once.
    port = free_udp_port()
    for _ in range(3):
        lease = await ArtNetEndpoint.acquire(_ignore, port=port)
        assert lease.local_port == port
        await lease.release()
    assert ArtNetEndpoint.open_endpoints() == {}


async def test_release_right_after_a_send_never_hangs() -> None:
    # Windows' proactor loop never reports a datagram transport lost when it
    # is closed with a send in flight; the release must finish regardless,
    # or a driver restart hangs. The shutdown blackout is exactly this case.
    port = free_udp_port()
    for _ in range(20):
        lease = await ArtNetEndpoint.acquire(_ignore, port=port)
        for _ in range(5):
            lease.send(artnet.encode_art_poll(), ("127.0.0.1", 9))
        await asyncio.wait_for(lease.release(), 3.0)
    assert ArtNetEndpoint.open_endpoints() == {}


async def test_release_is_idempotent_and_a_released_lease_cannot_send() -> None:
    lease = await ArtNetEndpoint.acquire(_ignore)
    await lease.release()
    await lease.release()
    with pytest.raises(TransportClosed):
        lease.send(artnet.encode_art_poll(), ("127.0.0.1", 9))


async def test_a_port_another_program_holds_is_an_error_and_leaves_nothing() -> None:
    blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    blocker.bind(("0.0.0.0", 0))
    try:
        with pytest.raises(OSError):
            await ArtNetEndpoint.acquire(_ignore, port=blocker.getsockname()[1])
        assert ArtNetEndpoint.open_endpoints() == {}
    finally:
        blocker.close()


async def test_every_holder_hears_every_datagram_with_its_sender() -> None:
    heard_a: list[tuple[bytes, str]] = []
    heard_b: list[tuple[bytes, str]] = []

    def broken(data: bytes, source: str) -> None:
        raise RuntimeError("a faulty holder")

    a = await ArtNetEndpoint.acquire(lambda d, s: heard_a.append((d, s)))
    faulty = await ArtNetEndpoint.acquire(broken)
    b = await ArtNetEndpoint.acquire(lambda d, s: heard_b.append((d, s)))
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sender.bind(("127.0.0.1", 0))
        sender.sendto(b"hello", ("127.0.0.1", a.local_port))
        for _ in range(200):
            if heard_a and heard_b:
                break
            await asyncio.sleep(0.005)
        assert heard_a == [(b"hello", "127.0.0.1")]
        assert heard_b == [(b"hello", "127.0.0.1")]
    finally:
        sender.close()
        for lease in (a, faulty, b):
            await lease.release()


class _Collector(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.received: list[tuple[bytes, tuple[str, int]]] = []

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.received.append((data, addr))


async def test_sends_leave_from_the_art_net_port() -> None:
    loop = asyncio.get_running_loop()
    transport, collector = await loop.create_datagram_endpoint(
        _Collector, local_addr=("127.0.0.1", 0)
    )
    lease = await ArtNetEndpoint.acquire(_ignore)
    try:
        lease.send(artnet.encode_art_poll(), transport.get_extra_info("sockname"))
        for _ in range(200):
            if collector.received:
                break
            await asyncio.sleep(0.005)
        [(data, source)] = collector.received
        assert artnet.is_art_poll(data)
        assert source[1] == lease.local_port
    finally:
        await lease.release()
        transport.close()


async def test_the_socket_refuses_to_send_an_art_poll_reply() -> None:
    # B6/B48: the controller never announces itself as an Art-Net node.
    lease = await ArtNetEndpoint.acquire(_ignore)
    try:
        reply = artnet.encode_art_poll_reply(
            ip_address="127.0.0.1", firmware_version=(1, 0), short_name="x", long_name="x"
        )
        with pytest.raises(ValueError):
            lease.send(reply, ("127.0.0.1", 9))
    finally:
        await lease.release()
