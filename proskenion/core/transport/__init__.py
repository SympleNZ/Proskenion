"""Transports (spec §5.5 *Transports*).

Every driver goes through one of these — there are no exceptions (§5.5 *No
exceptions*). ``TRANSPORTS`` is the closed set the registry and the Devices
screen know about; it is filled in code, never by scanning.
"""

from __future__ import annotations

from typing import Any

from proskenion.core.transport.base import (
    BaseTransport,
    ConfigurationError,
    PortOption,
    Transport,
    TransportClosed,
)
from proskenion.core.transport.loopback import LoopbackTransport
from proskenion.core.transport.serial import SerialTransport
from proskenion.core.transport.tcp import TcpTransport
from proskenion.core.transport.udp import UdpTransport
from proskenion.core.transport.unix import UnixSocketTransport

TRANSPORTS: dict[str, type[BaseTransport]] = {
    TcpTransport.TYPE: TcpTransport,
    SerialTransport.TYPE: SerialTransport,
    UdpTransport.TYPE: UdpTransport,
    UnixSocketTransport.TYPE: UnixSocketTransport,
    LoopbackTransport.TYPE: LoopbackTransport,
}


def transport_from_config(config: dict[str, Any]) -> BaseTransport:
    """Build a transport from a stored ``{"type": ..., ...}`` block.

    Values are passed through as-is; validate them against the transport's
    ``SCHEMA`` first (the registry's ``build`` does).
    """
    type_name = config.get("type")
    if not isinstance(type_name, str) or type_name not in TRANSPORTS:
        known = ", ".join(sorted(TRANSPORTS))
        raise ConfigurationError(f"unknown transport type {type_name!r}; known: {known}")
    values = {key: value for key, value in config.items() if key != "type"}
    return TRANSPORTS[type_name](values)


__all__ = [
    "TRANSPORTS",
    "BaseTransport",
    "ConfigurationError",
    "LoopbackTransport",
    "PortOption",
    "SerialTransport",
    "TcpTransport",
    "Transport",
    "TransportClosed",
    "UdpTransport",
    "UnixSocketTransport",
    "transport_from_config",
]
