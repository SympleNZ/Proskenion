from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from proskenion.core.transport.base import ConfigurationError
from proskenion.core.transport.unix import UNIX_SOCKET_SCHEMA, UnixSocketTransport


def test_unix_schema() -> None:
    assert [f.key for f in UNIX_SOCKET_SCHEMA] == ["path"]
    assert UnixSocketTransport.TYPE == "unix_socket"


async def test_missing_socket_is_a_configuration_error(tmp_path: Path) -> None:
    transport = UnixSocketTransport({"path": str(tmp_path / "knxd")})
    with pytest.raises(ConfigurationError):
        await transport.open()
    assert not transport.is_open


@pytest.mark.skipif(sys.platform == "win32", reason="Unix-domain sockets need POSIX")
async def test_unix_round_trip(tmp_path: Path) -> None:
    path = str(tmp_path / "knxd")

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(await reader.read(64))
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(echo, path)
    try:
        transport = UnixSocketTransport({"path": path})
        await transport.open()
        await transport.send(b"hello knxd")
        assert await transport.receive(1.0) == b"hello knxd"
        await transport.close()
    finally:
        server.close()
        await server.wait_closed()
