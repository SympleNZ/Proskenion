"""The one Art-Net socket: UDP 6454, both directions (spec §7.2.5, §7.2.8).

§7.2.5 specifies "one ``asyncio.DatagramProtocol`` bound to UDP 6454, both
directions". This module is that socket, and the only code in the
application that binds the Art-Net port.

Why the port is fixed, and why it is shared
-------------------------------------------
Art-Net nodes do not answer to the port a poll came from. The DMXking eDMX8
MAX in the auditorium sends every ``ArtPollReply`` as a broadcast to
``<VLAN broadcast>:6454``, whoever polled; it broadcasts its DMX-IN ports as
``ArtDmx`` to the same place. A controller whose poll leaves from an
ephemeral port never hears the reply, which is how the node came to show
*not connected* on the real rig while answering every poll. So the socket
is bound to ``0.0.0.0:6454`` with ``SO_BROADCAST`` — and everything Art-Net
the controller does goes through it: ArtDmx out, ArtPoll out, ArtPollReply
in (health, §7.2.8) and ArtDmx in (desk detection, §7.2.7).

Who owns it
-----------
Neither the ``artnet`` driver nor the lighting service. A driver instance is
rebuilt on every device edit, and the Devices screen's *Test* button builds
a throwaway instance while the supervised one is still running (both
:class:`~proskenion.core.devices.DeviceManager`); two artnet devices may be
configured for two nodes. If each of those opened its own socket on 6454,
the second would fail with "address in use" — or, on Linux with
``SO_REUSEADDR``, silently split the node's replies between them. The
lighting service does not know which device is the node, and holding the
socket there would couple it to one driver.

So the socket belongs to this module, and callers hold a
:class:`ArtNetLease` on it. :meth:`ArtNetEndpoint.acquire` returns a lease on
the endpoint already bound to the port in this event loop, and binds one
only when none exists: two sockets on the Art-Net port cannot be opened by
construction. Every lease hears every datagram, with its sender's address,
and filters for itself (the driver keeps only its own node's,
:class:`~proskenion.core.dmx.artnet.ArtNetReceiver`). Releasing the last
lease closes the socket and waits until it is closed, so a driver restarted
at once — a device edit, a reload, the §5.3 recovery loop — rebinds cleanly
rather than meeting its own predecessor.

The transport layer (:mod:`proskenion.core.transport.udp`) is left as it is
for the ``sacn`` driver and anything else on UDP; the ``artnet`` driver
still takes its node's address from its UDP transport configuration (B45).

``SO_REUSEADDR`` is set on Linux, where it lets a rebind succeed while a
closed socket's port is still being released. It is not set on Windows,
where it would let a second socket take a port that is already bound.

The controller never sends an ``ArtPollReply`` (§7.2.5, B6, B48):
:meth:`ArtNetLease.send` refuses one, as a second line behind
:mod:`proskenion.core.dmx.artnet` having no code that builds one.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import sys
import weakref
from collections.abc import Callable
from typing import Any, ClassVar

from proskenion.core.dmx.artnet import ARTNET_PORT, OP_POLL_REPLY, art_net_opcode
from proskenion.core.transport.base import TransportClosed

log = logging.getLogger(__name__)

DatagramHandler = Callable[[bytes, str], None]
"""Called with each datagram's bytes and its sender's IP address."""

#: How long a graceful close may take before the socket is aborted.
CLOSE_TIMEOUT_S = 0.5


class ArtNetLease:
    """One holder's share of the endpoint. Obtain with :meth:`ArtNetEndpoint.acquire`."""

    def __init__(self, endpoint: ArtNetEndpoint, handler: DatagramHandler) -> None:
        self.endpoint = endpoint
        self.handler = handler
        self.released = False

    @property
    def local_port(self) -> int:
        return self.endpoint.local_port

    def send(self, data: bytes, address: tuple[str, int]) -> None:
        """Send one datagram from the Art-Net port to ``address``."""
        if self.released:
            raise TransportClosed("the Art-Net lease has been released")
        self.endpoint.send(data, address)

    async def release(self) -> None:
        """Give the share back; the last release closes the socket. Idempotent."""
        if self.released:
            return
        self.released = True
        await self.endpoint.release(self)


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, endpoint: ArtNetEndpoint) -> None:
        self._endpoint = endpoint

    def datagram_received(self, data: bytes, addr: Any) -> None:
        self._endpoint.dispatch(data, str(addr[0]))

    def error_received(self, exc: Exception) -> None:
        # ICMP port-unreachable after a send to a node that is off, and the
        # like: the missing ArtPollReply is what reports it (§7.2.8).
        return None

    def connection_lost(self, exc: Exception | None) -> None:
        self._endpoint.closed()


class ArtNetEndpoint:
    """The process's Art-Net socket (see the module docstring)."""

    #: The port every endpoint binds unless told otherwise. Art-Net fixes it
    #: at 6454; the test suite points it at a free port so a test run never
    #: competes for the real one with other software on the machine. The
    #: end-to-end harness does the same for each appliance it starts as its
    #: own process, so two running under concurrent Playwright workers never
    #: contend for 6454 with each other — see ``proskenion.main.apply_test_hooks``.
    default_port: ClassVar[int] = ARTNET_PORT

    #: One endpoint per port, per event loop. Weakly keyed so a finished
    #: loop (a test's) takes its entries with it.
    _open: ClassVar[weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[int, ArtNetEndpoint]]]
    _open = weakref.WeakKeyDictionary()

    def __init__(self, port: int) -> None:
        self.requested_port = port
        self._leases: list[ArtNetLease] = []
        self._transport: asyncio.DatagramTransport | None = None
        self._opened: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._closed = asyncio.Event()
        self._bind_task: asyncio.Task[None] | None = None
        self.closing = False

    # -- the registry ------------------------------------------------------

    @classmethod
    async def acquire(cls, handler: DatagramHandler, *, port: int | None = None) -> ArtNetLease:
        """A lease on this loop's endpoint for ``port``, binding it if needed.

        Raises :class:`OSError` when the port cannot be bound — another
        program holds it — and leaves nothing behind.
        """
        wanted = cls.default_port if port is None else port
        loop = asyncio.get_running_loop()
        endpoints = cls._open.setdefault(loop, {})
        endpoint = endpoints.get(wanted)
        while endpoint is not None and endpoint.closing:
            await endpoint._closed.wait()
            endpoint = endpoints.get(wanted)
        if endpoint is None:
            endpoint = cls(wanted)
            endpoints[wanted] = endpoint
            endpoint._bind_task = loop.create_task(
                endpoint._bind(), name=f"artnet-endpoint-{wanted}"
            )
        lease = ArtNetLease(endpoint, handler)
        endpoint._leases.append(lease)
        try:
            await asyncio.shield(endpoint._opened)
        except BaseException:
            lease.released = True
            await endpoint.release(lease)
            raise
        return lease

    @classmethod
    def open_endpoints(cls) -> dict[int, ArtNetEndpoint]:
        """The endpoints open in the running loop, by requested port."""
        return dict(cls._open.get(asyncio.get_running_loop(), {}))

    def _forget(self) -> None:
        endpoints = self._open.get(asyncio.get_running_loop(), {})
        if endpoints.get(self.requested_port) is self:
            del endpoints[self.requested_port]

    # -- the socket --------------------------------------------------------

    @property
    def local_port(self) -> int:
        if self._transport is None:
            raise TransportClosed("the Art-Net socket is not open")
        return int(self._transport.get_extra_info("sockname")[1])

    @property
    def lease_count(self) -> int:
        return len(self._leases)

    async def _bind(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            sock = _bound_socket(self.requested_port)
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _Protocol(self), sock=sock
            )
        except OSError as exc:
            self._forget()
            self._closed.set()
            if not self._opened.done():
                self._opened.set_exception(exc)
            return
        if self.closing:
            # Every holder gave up while the bind was in flight.
            transport.close()
            return
        self._transport = transport
        if not self._opened.done():
            self._opened.set_result(None)
        log.info("Art-Net socket bound", extra={"port": self.local_port})

    def send(self, data: bytes, address: tuple[str, int]) -> None:
        if art_net_opcode(data) == OP_POLL_REPLY:
            raise ValueError("the controller never sends an ArtPollReply (§7.2.5)")
        if self._transport is None or self._transport.is_closing():
            raise TransportClosed("the Art-Net socket is not open")
        try:
            self._transport.sendto(data, address)
        except OSError as exc:
            raise TransportClosed(f"Art-Net send failed: {exc}") from exc

    def dispatch(self, data: bytes, source: str) -> None:
        """Hand one datagram to every lease. A holder that raises harms no other."""
        for lease in list(self._leases):
            try:
                lease.handler(data, source)
            except Exception:
                log.exception("Art-Net datagram handler raised", extra={"source": source})

    async def release(self, lease: ArtNetLease) -> None:
        if lease in self._leases:
            self._leases.remove(lease)
        if self._leases or self.closing:
            if self.closing:
                await self._closed.wait()
            return
        self.closing = True
        transport, self._transport = self._transport, None
        if transport is None:
            # Never bound: the bind failed, or is still in flight and will
            # close what it binds when it sees ``closing``.
            if not self._opened.done():
                self._opened.cancel()
            self._forget()
            self._closed.set()
            return
        # A graceful close lets a datagram still being written — the
        # shutdown blackout (§12.4) — go out first. It must not be able to
        # hang a restart, though: Windows' proactor loop never reports a
        # datagram transport lost if it was closed with a send in flight. So
        # the wait is bounded, and an abort finishes the job.
        transport.close()
        try:
            await asyncio.wait_for(self._closed.wait(), CLOSE_TIMEOUT_S)
        except TimeoutError:
            log.debug("Art-Net socket close did not complete; aborting it")
            transport.abort()
            await self._closed.wait()

    def closed(self) -> None:
        """The socket is gone: forget this endpoint so the next acquire rebinds."""
        self.closing = True
        self._transport = None
        self._forget()
        self._closed.set()


def _bound_socket(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if sys.platform != "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("0.0.0.0", port))
        sock.setblocking(False)
    except OSError:
        sock.close()
        raise
    return sock


__all__ = ["ArtNetEndpoint", "ArtNetLease", "DatagramHandler"]
