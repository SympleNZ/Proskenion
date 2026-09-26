"""A minimal test driver: probe sends a greeting and expects ``PONG``.

Used to prove the §5.3 connect/probe split, backoff, and transport
substitution without any real hardware or protocol.
"""

from __future__ import annotations

import asyncio
from typing import Any

from proskenion.core.drivers.base import DeviceStatus, Driver, FailureKind, ProbeResult
from proskenion.core.drivers.capabilities import ChannelRef, MatrixCapabilities, MatrixRefs
from proskenion.core.drivers.categories import Category
from proskenion.core.drivers.fields import ConfigError, Field


class RecordingSink:
    """A ``StatusSink`` that keeps every report."""

    def __init__(self) -> None:
        self.reports: list[tuple[int, DeviceStatus, FailureKind | None, str | None]] = []
        #: Set on every report so a test can wait for progress instead of polling.
        self.changed = asyncio.Event()

    async def set_status(
        self,
        device_id: int,
        status: DeviceStatus,
        kind: FailureKind | None = None,
        detail: str | None = None,
    ) -> None:
        self.reports.append((device_id, status, kind, detail))
        self.changed.set()

    @property
    def statuses(self) -> list[tuple[DeviceStatus, FailureKind | None]]:
        return [(status, kind) for _, status, kind, _ in self.reports]


def pong_responder(data: bytes) -> bytes | None:
    return b"PONG\n" if data.strip() == b"PING" else None


class EchoDriver(Driver):
    key = "echo"
    category = Category.VIDEO_MATRIX
    name = "Echo test driver"

    SUPPORTED_TRANSPORTS = ["loopback", "tcp", "serial"]
    TRANSPORT_DEFAULTS = {"tcp": {"port": 7}, "serial": {"baud": 9600}}
    CONFIG_SCHEMA = [
        Field("greeting", type="string", label="Greeting", default="PING", pattern=r"[A-Z]+"),
        Field(
            "retries",
            type="int",
            label="Retries",
            default=1,
            min=0,
            max=5,
            depends_on=("greeting", "PING"),
        ),
    ]

    #: Generous for real I/O (a localhost TCP round trip on a loaded machine);
    #: run-loop tests over the loopback shorten it per instance.
    PROBE_TIMEOUT = 1.0

    async def probe(self) -> ProbeResult:
        await self.transport.send(str(self.config["greeting"]).encode() + b"\n")
        try:
            reply = await self.transport.receive(self.PROBE_TIMEOUT)
        except TimeoutError:
            return ProbeResult(False, "no reply to greeting")
        if reply.strip() == b"PONG":
            return ProbeResult(True)
        return ProbeResult(False, f"unexpected reply {reply!r}")

    def capabilities(self) -> MatrixCapabilities:
        return MatrixCapabilities(input_count=2, output_count=2, supports_atomic_route=True)

    async def validate_config(self, config: dict[str, Any]) -> list[ConfigError]:
        if config.get("greeting") == "BAD":
            return [ConfigError("greeting", "this device rejects BAD")]
        return []

    # -- VideoMatrixDriver ----------------------------------------------------

    async def route(self, outputs: list[str], input: str) -> None:
        await self.transport.send(f"ROUTE {','.join(outputs)}<{input}\n".encode())

    async def read_routing(self) -> dict[str, str]:
        return {}

    def available_refs(self) -> MatrixRefs:
        return MatrixRefs(
            inputs=[ChannelRef("in1", "Input 1", "input", False)],
            outputs=[ChannelRef("out1", "Output 1", "output", False)],
        )
