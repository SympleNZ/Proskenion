"""Driver base class and the §5.3 run loop.

Every protocol client is a persistent asyncio task with a ``run()`` loop that
never exits: it opens its transport, confirms the device is alive, maintains
it, and recovers with exponential backoff. One device going down does not
affect any other.

Connection and liveness are separate. ``connect()`` is the transport's job and
may prove nothing; ``probe()`` is the driver's job and is authoritative. Status
derives from probe, never from connect. The two failure kinds surface
differently because they need different actions (§5.3):

===========  ==============================  ==========================================
kind         meaning                         what the operator is told
===========  ==============================  ==========================================
``config``   connect failed                  check the address, path, or permissions
``device``   connect succeeded, probe failed check the cable, power, or device state
===========  ==============================  ==========================================

Backoff starts at 5 s, doubles, caps at 300 s, and resets **only after a
successful probe** — a device that accepts connections and then fails to
respond does not reset it on every attempt (B38).
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Literal, Protocol

from proskenion.core.drivers.capabilities import Capabilities
from proskenion.core.drivers.categories import Category
from proskenion.core.drivers.fields import ConfigError, Field
from proskenion.core.transport.base import ConfigurationError, Transport, TransportClosed

log = logging.getLogger(__name__)

FailureKind = Literal["config", "device"]


class DeviceStatus(StrEnum):
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"
    DISABLED = "disabled"  # an ``enabled = 0`` row; the supervisor never runs it


@dataclass(frozen=True)
class ProbeResult:
    alive: bool
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class FirewallPorts:
    """Firewall access a device's driver needs beyond the single port its own
    transport config already opens (§3.1's device table; contracts §4, the
    Phase 1 gap: ``proskenion/core/system_config.py`` mirrors the ``devices``
    table into ``system.json``, and ``appliance/bin/auditorium-config-apply``
    renders it into nftables).

    ``outbound`` is this appliance reaching the device on more than its
    transport's own port — the CQ-20B's native metering connection is the
    shipped example, one port beyond the MIDI one its ``tcp`` transport
    config already carries. ``inbound`` is the device reaching this
    appliance on a fixed port of ours — the CQ-20B's meter return. Both are
    the ``"tcp/N"`` / ``"udp/LO-HI"`` / ``"udp/any"`` port-spec strings
    ``auditorium-config-apply`` already expects; a driver never mentions a
    host here (B45) — only its own device's ports, addressed by whatever
    ``address`` the mirror already read from the transport config.
    """

    outbound: tuple[str, ...] = ()
    inbound: tuple[str, ...] = ()


class StatusSink(Protocol):
    """Where a driver reports status. Injected so drivers never touch the state
    store directly."""

    async def set_status(
        self,
        device_id: int,
        status: DeviceStatus,
        kind: FailureKind | None = None,
        detail: str | None = None,
    ) -> None: ...


class Driver(ABC):
    """Base of every shipped driver.

    Class attributes describe the driver to the registry and the Devices
    screen. The constructor must not perform I/O; everything happens in
    ``run()``. Subclasses implement ``probe()`` and ``capabilities()`` and one
    category interface from :mod:`proskenion.core.drivers.categories`; they
    override ``maintain()`` when the protocol has a receive loop, otherwise the
    default periodic probe applies.
    """

    key: ClassVar[str]
    category: ClassVar[Category]
    name: ClassVar[str]
    SUPPORTED_TRANSPORTS: ClassVar[list[str]]
    TRANSPORT_DEFAULTS: ClassVar[dict[str, dict[str, Any]]] = {}
    CONFIG_SCHEMA: ClassVar[list[Field]] = []  # protocol only — no addressing

    #: Seconds between liveness probes while connected (§11.1: 30 s).
    PROBE_INTERVAL: ClassVar[float] = 30.0
    INITIAL_RETRY_DELAY: ClassVar[float] = 5.0
    MAX_RETRY_DELAY: ClassVar[float] = 300.0

    #: True for a driver where a second simultaneous connection could be
    #: refused by the device, or could knock the running one off — the
    #: CQ-20B's single MIDI client (§7.3, bench question 6). The Devices
    #: screen's test button (``proskenion/api/devices.py``) checks this, or
    #: the ``mixer`` category, before building a throwaway instance to test
    #: with; when either is true and the device is already running, the test
    #: reports the running instance's own status instead of opening a second
    #: connection. Declared here, on the base class, so a future driver in
    #: another category with the same constraint opts in without the devices
    #: API naming it by key (B45's spirit: the core does not special-case one
    #: driver).
    SHARES_CONNECTION: ClassVar[bool] = False

    @classmethod
    def firewall_ports(cls, config: Mapping[str, Any]) -> FirewallPorts:
        """Extra firewall ports this device's *stored configuration* implies
        (see :class:`FirewallPorts`). The base declares none — a TCP or UDP
        transport's own host and port are enough for most drivers, and the
        devices-table mirror already opens those. A driver overrides this
        only when its protocol genuinely needs more, as
        :class:`~proskenion.core.drivers.cq20b.CQ20BDriver` does. No I/O:
        this is called against the stored config alone, never a running
        instance."""
        return FirewallPorts()

    def __init__(
        self,
        device_id: int,
        transport: Transport,
        driver_config: dict[str, Any],
        status_sink: StatusSink,
    ) -> None:
        self.device_id = device_id
        self.transport = transport
        self.config: dict[str, Any] = dict(driver_config)
        self._status_sink = status_sink
        self._retry_delay: float = self.INITIAL_RETRY_DELAY
        #: Replaced in tests so backoff and probe intervals do not really wait.
        self._sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    # -- §5.3 contract -------------------------------------------------------

    async def connect(self) -> None:
        """Establish the transport. May prove nothing about the device."""
        await self.transport.open()

    async def disconnect(self) -> None:
        await self.transport.close()

    @abstractmethod
    async def probe(self) -> ProbeResult:
        """Confirm the device is alive and responding. Authoritative."""

    async def maintain(self) -> None:
        """Run until the transport is lost. Returns to trigger recovery.

        Default: probe every ``PROBE_INTERVAL`` seconds and return after two
        consecutive failures (§5.3). Drivers with their own receive loop
        override this, or run :meth:`probe_periodically` alongside it.
        """
        await self.probe_periodically()

    async def probe_periodically(self) -> None:
        failures = 0
        while True:
            await self._sleep(self.PROBE_INTERVAL)
            result = await self._safe_probe()
            if result.alive:
                failures = 0
                continue
            failures += 1
            log.info("device %s probe failed (%d): %s", self.device_id, failures, result.detail)
            if failures >= 2:
                return

    @abstractmethod
    def capabilities(self) -> Capabilities:
        """What this driver supports. Declared maximum before ``connect()``;
        what the connection actually achieved after it (B56)."""

    async def validate_config(self, config: dict[str, Any]) -> list[ConfigError]:
        """Cross-field checks a schema cannot express. Runs on save, before
        connecting (§5.5 *Cross-field validation*)."""
        return []

    # -- run loop, exactly as §5.3 -------------------------------------------

    async def run(self) -> None:
        try:
            while True:
                await self._set_status(DeviceStatus.CONNECTING)
                try:
                    await self.connect()
                except ConfigurationError as exc:  # wrong path, permission, no route
                    await self._set_status(DeviceStatus.ERROR, kind="config", detail=str(exc))
                    await self._backoff()
                    continue

                result = await self._safe_probe()
                if not result.alive:
                    await self._set_status(DeviceStatus.ERROR, kind="device", detail=result.detail)
                    await self.disconnect()
                    await self._backoff()
                    continue

                self._retry_delay = self.INITIAL_RETRY_DELAY  # reset only after a successful probe
                await self._set_status(DeviceStatus.CONNECTED)
                try:
                    await self.maintain()  # blocks until the transport is lost
                except (TransportClosed, OSError) as exc:
                    log.info("device %s transport lost: %s", self.device_id, exc)
                await self.disconnect()
        finally:
            # Cancellation or a programming error: leave the transport closed.
            await self.disconnect()

    async def _safe_probe(self) -> ProbeResult:
        """A probe that times out or loses its transport is a dead probe, not a crash."""
        try:
            return await self.probe()
        except (TransportClosed, TimeoutError, OSError) as exc:
            return ProbeResult(False, str(exc) or type(exc).__name__)

    async def _set_status(
        self,
        status: DeviceStatus,
        kind: FailureKind | None = None,
        detail: str | None = None,
    ) -> None:
        await self._status_sink.set_status(self.device_id, status, kind, detail)

    async def _backoff(self) -> None:
        """Wait the current retry delay, then double it up to the cap."""
        delay = self._retry_delay
        self._retry_delay = min(delay * 2, self.MAX_RETRY_DELAY)
        await self._sleep(delay)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} device={self.device_id} transport={self.transport!r}>"
