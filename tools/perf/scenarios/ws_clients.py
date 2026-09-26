"""§23.1 row: "Concurrent WebSocket clients — 20 sustained, 50 peak — 50
connections holding the lighting view."

Opens ``options.client_count`` connections to ``/ws?v=1`` (§16.8), holds them
open for ``options.hold_s`` (the "sustained" half of the row), then triggers
one real state change and times how long each connection takes to see it
(the "measure delivery latency of a state change to all clients" the brief
asks for) — the "peak" half, since every connection is open at once when it
happens.

The trigger is ``POST /lighting/external-control`` — a software flag
(§8.8's manual/detected external-control state), toggled on and then back
off, never a device write. It is not behind
:class:`~tools.perf.client.Safety`'s ``allow_device_writes`` for that reason:
nothing it does moves a fixture, and it is exactly the "reversible write"
the brief allows by default (a fader moved and put back is the named
example; this is the same shape). It does briefly suppress ``lighting_group``
rule firings on a real, commissioned room (CONVENTIONS.md, §8.8) for the
few hundred milliseconds this scenario takes — see ``tools/perf/README.md``
"what it touches".
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from typing import Any

import websockets

from tools.perf.client import PerfClient, Safety
from tools.perf.report import ScenarioResult
from tools.perf.stats import Measurement, monotonic
from tools.perf.targets import WS_CLIENTS, WS_CLIENTS_PEAK

DELIVERY_TIMEOUT_S = 5.0


@dataclass(frozen=True, slots=True)
class Options:
    client_count: int = WS_CLIENTS_PEAK
    hold_s: float = 2.0


class _Held:
    """One held connection: a background reader watching for the frame this
    scenario is about to trigger."""

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.seen_at: float | None = None
        self.seen = asyncio.Event()
        self._task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            async for raw in self.ws:
                if self.seen_at is not None:
                    continue
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                # The wire carries external control as a field of the
                # continuous "lighting_state" frame
                # (proskenion/core/broadcast.py's ``_lighting_frame``), not
                # as its own message type — ``external_control_message()``
                # in that module is unused in production.
                if message.get("type") == "lighting_state" and message.get(
                    "external_control"
                ) == "manual":
                    self.seen_at = monotonic()
                    self.seen.set()
        except websockets.ConnectionClosed:
            pass

    async def close(self) -> None:
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        with contextlib.suppress(Exception):
            await self.ws.close()


async def run(client: PerfClient, safety: Safety, options: Options | None = None) -> ScenarioResult:
    options = options or Options()
    connect_failures = 0

    async def connect_one() -> _Held | None:
        nonlocal connect_failures
        try:
            ws = await client.connect_websocket().__aenter__()
            # A fresh connection subscribes to nothing (Broadcaster.connect's
            # own default, proskenion/core/broadcast.py) — "holding the
            # lighting view" (§23.1's own wording) means subscribed to the
            # lighting domain, which is what a real dashboard's first
            # message on this socket does.
            await ws.send(json.dumps({"type": "subscribe", "domains": ["lighting"]}))
        except Exception:  # noqa: BLE001 - counted, not raised: a refused 51st client is data
            connect_failures += 1
            return None
        return _Held(ws)

    results = await asyncio.gather(*(connect_one() for _ in range(options.client_count)))
    held = [h for h in results if h is not None]

    try:
        if not held:
            return ScenarioResult.skip(
                WS_CLIENTS, f"no connection succeeded (0 of {options.client_count}); see notes"
            )

        await asyncio.sleep(options.hold_s)  # the "sustained" half of the row

        trigger_at = monotonic()
        turned_on = await client.http.post(
            client.api("/lighting/external-control"), json={"manual": True}
        )
        if turned_on.status_code != 200:
            return ScenarioResult.skip(
                WS_CLIENTS,
                f"could not trigger a state change to observe delivery on: "
                f"HTTP {turned_on.status_code} from POST /lighting/external-control",
            )
        try:
            await asyncio.wait_for(
                asyncio.gather(*(_wait_for(h) for h in held)), timeout=DELIVERY_TIMEOUT_S
            )
        except TimeoutError:
            pass  # partial delivery is data, not a harness failure
        finally:
            await client.http.post(client.api("/lighting/external-control"), json={"manual": False})

        measurement = Measurement()
        for h in held:
            if h.seen_at is not None:
                measurement.record(h.seen_at - trigger_at)
        delivered = measurement.count
        connected = len(held)
        passed = (
            connected >= options.client_count
            and connect_failures == 0
            and delivered == connected
        )
        return ScenarioResult(
            target=WS_CLIENTS,
            achieved=float(connected),
            p50_ms=measurement.p50_ms if delivered else None,
            p95_ms=measurement.p95_ms if delivered else None,
            max_ms=measurement.max_ms if delivered else None,
            sample_count=delivered,
            passed=passed,
            notes=(
                f"{connected} of {options.client_count} requested connections held for "
                f"{options.hold_s:g} s; {connect_failures} failed to open; {delivered} of "
                f"{connected} received the broadcast within {DELIVERY_TIMEOUT_S:g} s of the "
                f"trigger. p50/p95/max above are delivery latency, not a §23.1 target on their "
                f"own (§23.2 has no row for this frame specifically)."
            ),
            extra={"connect_failures": connect_failures, "delivered": delivered},
        )
    finally:
        await asyncio.gather(*(h.close() for h in held), return_exceptions=True)


async def _wait_for(held: _Held) -> None:
    await held.seen.wait()
