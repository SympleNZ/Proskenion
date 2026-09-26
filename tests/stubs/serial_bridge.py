"""Open a configured serial port as a TCP connection to a stub, for tests (§7.5, §22.5).

A development machine has no LKV422 and no null-modem pair to put a stub on
the far end of — Windows has no ``pty``, and ``com0com``/``socat`` are not
part of the toolchain. What it does have is the application's own serial
transport (:class:`~proskenion.core.transport.serial.SerialTransport`),
which opens its port with ``serial_asyncio.open_serial_connection`` and from
then on reads and writes nothing but the ``StreamReader``/``StreamWriter``
pair that call returns.

:func:`bridge_serial_ports` replaces that one call, for the device paths it
is given, with ``asyncio.open_connection`` to a TCP listener —
:class:`~tests.stubs.lkv422_tcp.LKV422TcpStub`. Everything above the
stream pair is the application's own: the device row configured through
``POST /devices`` with its ``/dev/hdmi-matrix`` path, its validation, the
device manager, the transport's ``send``/``receive`` and their error
handling, the ``LKV422Driver``, its parser, its lock and its probe. A path
not in the map opens as it always would.

It is a test double at the operating system's boundary — the thing a
virtual null-modem would be — and nothing in ``proskenion/`` knows it
exists. The application change that would make it unnecessary, accepting
pyserial's own ``socket://host:port`` URLs as a ``device_path`` in
development, is proposed in ``docs/phase-3-milestone.md`` and not made.

Two ways in:

- in process, :func:`bridge_serial_ports` as a context manager around the
  application (``tests/integration/``)
- out of process, :func:`install_from_environment`, called by
  :mod:`tests.stubs.bridged_app` before the application starts, reading
  :data:`BRIDGE_ENV` (``/dev/hdmi-matrix=127.0.0.1:50123``; several are
  separated by ``;``)
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import serial_asyncio

#: ``path=host:port[;path=host:port...]`` — read by :func:`install_from_environment`.
BRIDGE_ENV = "PROSKENION_TEST_SERIAL_BRIDGE"

_original_open = serial_asyncio.open_serial_connection


def parse_bridges(text: str) -> dict[str, tuple[str, int]]:
    """``"/dev/hdmi-matrix=127.0.0.1:50123"`` -> ``{"/dev/hdmi-matrix": ("127.0.0.1", 50123)}``."""
    bridges: dict[str, tuple[str, int]] = {}
    for entry in filter(None, (part.strip() for part in text.split(";"))):
        path, _, address = entry.partition("=")
        host, _, port = address.rpartition(":")
        if not path or not host or not port.isdigit():
            raise ValueError(f"not a serial bridge: {entry!r} (want path=host:port)")
        bridges[path] = (host, int(port))
    return bridges


def _install(bridges: Mapping[str, tuple[str, int]]) -> None:
    table = dict(bridges)

    async def open_serial_connection(
        *, url: str, **options: Any
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        target = table.get(url)
        if target is None:
            reader, writer = await _original_open(url=url, **options)
            return reader, writer
        return await asyncio.open_connection(*target)

    serial_asyncio.open_serial_connection = open_serial_connection


def _uninstall() -> None:
    serial_asyncio.open_serial_connection = _original_open


@contextmanager
def bridge_serial_ports(bridges: Mapping[str, tuple[str, int]]) -> Iterator[None]:
    """Open each configured ``device_path`` in ``bridges`` as TCP to ``(host, port)``."""
    _install(bridges)
    try:
        yield
    finally:
        _uninstall()


def install_from_environment() -> dict[str, tuple[str, int]]:
    """Install what :data:`BRIDGE_ENV` asks for, and return the bridges installed."""
    bridges = parse_bridges(os.environ.get(BRIDGE_ENV, ""))
    if bridges:
        _install(bridges)
    return bridges
