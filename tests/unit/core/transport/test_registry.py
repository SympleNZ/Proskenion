from __future__ import annotations

import pytest

from proskenion.core.transport import (
    TRANSPORTS,
    ConfigurationError,
    LoopbackTransport,
    SerialTransport,
    TcpTransport,
    Transport,
    UdpTransport,
    UnixSocketTransport,
    transport_from_config,
)


def test_registry_is_the_spec_table() -> None:
    assert set(TRANSPORTS) == {"tcp", "serial", "udp", "unix_socket", "loopback"}
    for name, cls in TRANSPORTS.items():
        assert cls.TYPE == name
        assert isinstance(cls.SCHEMA, list)


def test_transport_from_config_builds_the_right_class() -> None:
    tcp = transport_from_config({"type": "tcp", "host": "10.2.30.71", "port": 4352})
    assert isinstance(tcp, TcpTransport)
    assert tcp.config == {"host": "10.2.30.71", "port": 4352}
    assert isinstance(tcp, Transport)
    assert isinstance(transport_from_config({"type": "loopback"}), LoopbackTransport)
    assert isinstance(
        transport_from_config({"type": "serial", "device_path": "/dev/x", "baud": 9600}),
        SerialTransport,
    )
    assert isinstance(transport_from_config({"type": "udp", "host": "h", "port": 1}), UdpTransport)
    assert isinstance(
        transport_from_config({"type": "unix_socket", "path": "/run/knxd"}), UnixSocketTransport
    )


def test_unknown_transport_type() -> None:
    with pytest.raises(ConfigurationError, match="unknown transport type"):
        transport_from_config({"type": "carrier_pigeon"})
    with pytest.raises(ConfigurationError):
        transport_from_config({})


def test_only_transports_carry_addressing_fields() -> None:
    addressing = {"host", "device_path"}
    assert {f.type for f in TcpTransport.SCHEMA} & addressing
    assert {f.type for f in SerialTransport.SCHEMA} & addressing
    assert {f.type for f in UdpTransport.SCHEMA} & addressing
    assert {f.type for f in UnixSocketTransport.SCHEMA} & addressing
