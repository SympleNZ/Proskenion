"""Transport contract (spec §5.5 *Transports*).

Transport and protocol are orthogonal. A driver declares which transports it
works over; the transport owns addressing. A transport delivers bytes and
datagram boundaries only — where a message ends is the driver's business
(see :mod:`proskenion.core.transport.framing`).

``open()`` raises :class:`ConfigurationError` for the things a wrong
configuration causes — bad path, permission denied, no route. Anything the
device does or fails to do after that is the driver's ``probe()`` to judge
(§5.3): a serial node that opens proves nothing about what is attached.

``receive(timeout)`` raises the built-in :class:`TimeoutError` when nothing
arrives within ``timeout`` — a quiet line, not a lost connection — and
:class:`TransportClosed` when the transport itself is gone (not open, or
lost while waiting). ``TimeoutError`` is a subclass of :class:`OSError`, so
a transport must let it propagate ahead of any ``except OSError`` clause in
its own ``receive``, or a timeout is reported as a dropped port. A driver
that waits for a quiet line before every request — the LKV422's ``_drain``,
for one — depends on this distinction.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol, runtime_checkable

from proskenion.core.drivers.fields import Field


class ConfigurationError(Exception):
    """``open()`` failed for a reason configuration explains: wrong path,
    permission, no route (§5.3 failure kind ``config``)."""


class TransportClosed(Exception):
    """The transport is not open, or was lost while in use."""


@dataclass(frozen=True)
class PortOption:
    """One enumerated port for the serial picker (§5.5 *Serial port enumeration*)."""

    path: str  # what is stored — the by-id path when one exists
    label: str
    vendor_id: str | None = None  # hex, e.g. "0403"
    product_id: str | None = None  # hex, e.g. "6001"
    serial: str | None = None
    in_use: bool = False
    stable: bool = True  # False: ttyUSB* fallback; the path will move on replug
    in_use_by: str | None = None


@runtime_checkable
class Transport(Protocol):
    """The contract every driver talks through — verbatim from §5.5."""

    SCHEMA: ClassVar[list[Field]]

    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def send(self, data: bytes) -> None: ...
    async def receive(self, timeout: float) -> bytes: ...
    async def enumerate(self) -> list[PortOption] | None: ...  # None = not enumerable


class BaseTransport(ABC):
    """Shared shape of the shipped transports.

    Concrete transports are constructed from their validated configuration
    values and must not perform I/O until ``open()``. ``TYPE`` is the name the
    stored ``{"transport": {"type": ...}}`` block uses.
    """

    TYPE: ClassVar[str]
    SCHEMA: ClassVar[list[Field]] = []

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config: dict[str, Any] = dict(config or {})

    @property
    @abstractmethod
    def is_open(self) -> bool: ...

    @abstractmethod
    async def open(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def send(self, data: bytes) -> None: ...

    @abstractmethod
    async def receive(self, timeout: float) -> bytes: ...

    async def enumerate(self) -> list[PortOption] | None:
        return None

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.TYPE} {self.config!r}>"
