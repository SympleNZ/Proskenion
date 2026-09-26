"""Stub video matrix driver (spec §5.5 *Validating the abstraction*, §7.5).

This driver exists for two reasons, and neither of them is hardware.

The first is to prove the abstraction. The standard failure of a
one-implementation interface is that it quietly becomes "what the LKV422
does"; §5.5 answers that by insisting a deliberately minimal stub is built and
that the interface and the screens above it work against it. Everything here
is therefore expressed in the core's own terms — opaque refs, one call for one
intent, capabilities resolved after ``connect()`` — and never in a wire format.

The second is that the Phase 1 milestone is "device status is displayed", and
displaying it needs a device that reaches ``connected`` on a machine with no
matrix plugged into it. Over the loopback transport this driver connects,
probes and stays connected indefinitely, so the status bar, the state store
and the event bus can all be exercised end to end.

It is not hardware and says so in its :attr:`name`. Its configuration is
protocol only — how many inputs and outputs the imaginary matrix has —
because addressing belongs to the transport (B45).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from proskenion.core.drivers.base import Driver, ProbeResult, StatusSink
from proskenion.core.drivers.capabilities import ChannelRef, MatrixCapabilities, MatrixRefs
from proskenion.core.drivers.categories import Category
from proskenion.core.drivers.fields import Field
from proskenion.core.drivers.registry import register
from proskenion.core.transport.base import Transport

log = logging.getLogger(__name__)

DEFAULT_INPUTS = 4
DEFAULT_OUTPUTS = 2
MAX_PORTS = 16

#: {output_ref: input_ref} — matches :data:`proskenion.core.drivers.lkv422.Routing`.
Routing = dict[str, str]

#: Called whenever this stub's routing changes, with ``(new, previous)`` — the
#: same additive hook :meth:`~proskenion.core.drivers.lkv422.LKV422Driver.add_routing_listener`
#: defines, so a caller can treat every video matrix driver alike (§7.5).
RoutingListener = Callable[[Routing, Routing], Awaitable[None]]


@register
class StubMatrixDriver(Driver):
    """A matrix that exists only in memory. Registered as ``video_matrix/stub``."""

    key: ClassVar[str] = "stub"
    category: ClassVar[Category] = Category.VIDEO_MATRIX
    name: ClassVar[str] = "Stub video matrix (no hardware)"

    SUPPORTED_TRANSPORTS: ClassVar[list[str]] = ["loopback"]
    CONFIG_SCHEMA: ClassVar[list[Field]] = [
        Field(
            "input_count",
            type="int",
            label="Inputs",
            default=DEFAULT_INPUTS,
            min=1,
            max=MAX_PORTS,
            help="How many inputs the simulated matrix presents",
        ),
        Field(
            "output_count",
            type="int",
            label="Outputs",
            default=DEFAULT_OUTPUTS,
            min=1,
            max=MAX_PORTS,
            help="How many outputs the simulated matrix presents",
        ),
    ]

    def __init__(
        self,
        device_id: int,
        transport: Transport,
        driver_config: dict[str, Any],
        status_sink: StatusSink,
    ) -> None:
        super().__init__(device_id, transport, driver_config, status_sink)
        self._routing: dict[str, str] = {
            ref: "1" for ref in self._refs(self.output_count)
        }  # powers on with everything following input 1, as small matrices do
        self._listeners: list[RoutingListener] = []

    # -- configuration ------------------------------------------------------

    @property
    def input_count(self) -> int:
        return int(self.config.get("input_count", DEFAULT_INPUTS))

    @property
    def output_count(self) -> int:
        return int(self.config.get("output_count", DEFAULT_OUTPUTS))

    @staticmethod
    def _refs(count: int) -> list[str]:
        return [str(n) for n in range(1, count + 1)]

    # -- §5.3 contract ------------------------------------------------------

    async def probe(self) -> ProbeResult:
        """Alive whenever the transport is open — there is nothing to ask.

        The loopback transport is the device here, so an open transport really
        is authoritative. A driver over a serial port could not say this
        (§5.3): opening the node proves nothing about what is on the far end.
        """
        if not bool(getattr(self.transport, "is_open", False)):
            return ProbeResult(False, "the stub transport is not open")
        return ProbeResult(True)

    def capabilities(self) -> MatrixCapabilities:
        """Resolved from the configuration, after ``connect()`` like any other (B56)."""
        return MatrixCapabilities(
            input_count=self.input_count,
            output_count=self.output_count,
            supports_atomic_route=True,
        )

    # -- VideoMatrixDriver (§7.5) -------------------------------------------

    async def route(self, outputs: list[str], input: str) -> None:  # noqa: A002 - §7.5 signature
        """Point every reference in ``outputs`` at ``input``.

        One call for one intent, whatever the size of ``outputs`` (B47): the
        whole group is written as a single frame, never one write per output.
        """
        if input not in self._refs(self.input_count):
            raise ValueError(f"stub matrix inputs are 1-{self.input_count}, got {input!r}")
        known = self._refs(self.output_count)
        unknown = [ref for ref in outputs if ref not in known]
        if unknown:
            raise ValueError(
                f"stub matrix outputs are 1-{self.output_count}, got {', '.join(unknown)}"
            )
        await self.transport.send(f"ROUTE {','.join(outputs)}<{input}\n".encode())
        previous = dict(self._routing)
        for ref in outputs:
            self._routing[ref] = input
        await self._notify_routing(dict(self._routing), previous)

    async def read_routing(self) -> dict[str, str]:
        return dict(self._routing)

    # -- out-of-band detection hook (§7.5 *Destinations*, *Out-of-band control*) --
    #
    # Added additively so the video service can register one
    # listener and treat this stub the same way it treats the LKV422 driver.
    # The stub has no front panel or IR remote, so ``route()`` is the only
    # thing that ever changes its routing — there is no out-of-band source to
    # detect here, unlike the real matrix.

    def add_routing_listener(self, listener: RoutingListener) -> None:
        """Register ``listener`` to be awaited with ``(new, previous)`` whenever
        this stub's routing changes. Matches
        :meth:`~proskenion.core.drivers.lkv422.LKV422Driver.add_routing_listener`'s
        semantics: a listener that raises is logged and isolated, never
        reaching another listener or this driver's caller."""
        self._listeners.append(listener)

    async def _notify_routing(self, routing: Routing, previous: Routing) -> None:
        if routing == previous:
            return
        for listener in list(self._listeners):
            try:
                await listener(dict(routing), dict(previous))
            except Exception:
                log.exception("routing listener raised for device %s", self.device_id)

    def available_refs(self) -> MatrixRefs:
        """Refs are opaque to the core; the labels populate admin pickers only (B59)."""
        return MatrixRefs(
            inputs=[
                ChannelRef(ref=ref, label=f"Input {ref}", kind="input", stereo=False)
                for ref in self._refs(self.input_count)
            ],
            outputs=[
                ChannelRef(ref=ref, label=f"Output {ref}", kind="output", stereo=False)
                for ref in self._refs(self.output_count)
            ],
        )
