"""§23.1 row: "WebSocket control writes — 300/sec sustained — 10 clients
dragging simultaneously." The round-trip target is §23.2's companion row,
"WebSocket control write to ``ack``" (p50 15 ms, p99 60 ms).

Needs an existing mixer channel (discovered read-only by
``tools/perf/rig.py``, or given with ``--mixer-channel-id``) and
:attr:`~tools.perf.client.Safety.allow_device_writes`: this really does move
a fader on whatever device the channel is patched to. The level it settles
on is read back before the burst and restored afterwards — "move a fader and
put it back", the safety brief's own example of a reversible write.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
from dataclasses import dataclass
from itertools import count
from typing import Any

from tools.perf.client import PerfClient, Safety
from tools.perf.report import ScenarioResult
from tools.perf.stats import Measurement, monotonic
from tools.perf.targets import (
    WS_CONTROL_WRITE_ACK_P50_MS,
    WS_CONTROL_WRITE_ACK_P99_MS,
    WS_CONTROL_WRITES,
)

#: Kept a couple of dB either side of a plausible working level, never near
#: 0 dB or the extremes a real driver's fader law clamps at.
SWING_DB = (-18.0, -12.0)
ACK_TIMEOUT_S = 2.0


@dataclass(frozen=True, slots=True)
class Options:
    mixer_channel_id: int | None = None
    duration_s: float = 3.0
    warm_up_s: float = 0.3
    client_count: int = 10


class _Writer:
    """One connected socket, writing ``set`` frames and matching each
    ``ack``/``nack`` back to the token that sent it (§16.8)."""

    def __init__(self, ws: Any, channel_id: int) -> None:
        self.ws = ws
        self.channel_id = channel_id
        self._pending: dict[int, asyncio.Future[float]] = {}
        self._tokens = count(1)
        self._task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        with contextlib.suppress(Exception):
            async for raw in self.ws:
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if message.get("type") not in ("ack", "nack"):
                    continue
                token = message.get("token")
                future = self._pending.pop(token, None)
                if future is not None and not future.done():
                    future.set_result(monotonic())

    async def write(self, value: float) -> float:
        """Send one fader move, await its ack/nack, return the round-trip
        duration in seconds."""
        token = next(self._tokens)
        future: asyncio.Future[float] = asyncio.get_running_loop().create_future()
        self._pending[token] = future
        sent_at = monotonic()
        frame = {
            "type": "set",
            "domain": "mixer",
            "id": self.channel_id,
            "value": value,
            "token": token,
        }
        await self.ws.send(json.dumps(frame))
        received_at = await asyncio.wait_for(future, timeout=ACK_TIMEOUT_S)
        return received_at - sent_at

    async def close(self) -> None:
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        with contextlib.suppress(Exception):
            await self.ws.close()


async def run(client: PerfClient, safety: Safety, options: Options | None = None) -> ScenarioResult:
    options = options or Options()
    if not safety.allow_device_writes:
        return ScenarioResult.skip(
            WS_CONTROL_WRITES,
            "dry-run: pass --allow-device-writes to move a real fader (and put it back) for "
            "this scenario.",
        )
    if options.mixer_channel_id is None:
        return ScenarioResult.skip(
            WS_CONTROL_WRITES,
            "no mixer channel identified — pass --mixer-channel-id, or configure one so "
            "discovery finds it.",
        )
    channel_id = options.mixer_channel_id

    original_db = await _read_level(client, channel_id)

    writers: list[_Writer] = []
    try:
        for _ in range(options.client_count):
            ws = await client.connect_websocket().__aenter__()
            writers.append(_Writer(ws, channel_id))

        measurement = Measurement()
        start = monotonic()
        warm_until = start + options.warm_up_s
        deadline = warm_until + options.duration_s

        async def drag(writer: _Writer) -> None:
            low, high = SWING_DB
            step = 0
            while monotonic() < deadline:
                value = low + (high - low) * (0.5 + 0.5 * math.sin(step / 3.0))
                step += 1
                try:
                    duration = await writer.write(value)
                except Exception:  # noqa: BLE001 - a dropped ack is a failure sample, not a crash
                    measurement.failures += 1
                    continue
                if monotonic() < warm_until:
                    measurement.warm_up_discarded += 1
                    continue
                measurement.record(duration)

        await asyncio.gather(*(drag(w) for w in writers))
        window_s = monotonic() - warm_until
        rate = measurement.rate_per_second(window_s)
        rate_passed = rate >= (WS_CONTROL_WRITES.target_value or 0.0)
        ack_passed = measurement.count == 0 or measurement.p50_ms <= WS_CONTROL_WRITE_ACK_P99_MS
        return ScenarioResult(
            target=WS_CONTROL_WRITES,
            achieved=rate,
            p50_ms=measurement.p50_ms,
            p95_ms=measurement.p95_ms,
            max_ms=measurement.max_ms,
            sample_count=measurement.count,
            passed=rate_passed and ack_passed,
            notes=(
                f"{options.client_count} clients dragging mixer channel {channel_id} for "
                f"{options.duration_s:g} s; {measurement.failures} nack/timeout(s). p50/p95/max "
                f"are set-to-ack round trip (§23.2 target: p50 {WS_CONTROL_WRITE_ACK_P50_MS:g} ms, "
                f"p99 {WS_CONTROL_WRITE_ACK_P99_MS:g} ms)."
            ),
            extra={"channel_id": channel_id, "failures": measurement.failures},
        )
    finally:
        await asyncio.gather(*(w.close() for w in writers), return_exceptions=True)
        if original_db is not False:
            await _restore_level(client, channel_id, original_db)


async def _read_level(client: PerfClient, channel_id: int) -> float | None | bool:
    """The channel's current dB, ``None`` for off, or ``False`` if it could
    not be read — in which case nothing is written back, since a guessed
    "original" value would itself be a configuration change."""
    response = await client.http.get(client.api("/mixer/state"))
    if response.status_code != 200:
        return False
    body = response.json()
    channels = [body.get("main")] if body.get("main") else []
    channels += body.get("outputs", []) + body.get("inputs", [])
    for channel in channels:
        if channel and channel.get("channel_id") == channel_id:
            db = channel.get("db")
            return float(db) if db is not None else None
    return False


async def _restore_level(client: PerfClient, channel_id: int, db: float | None) -> None:
    with contextlib.suppress(Exception):
        await client.http.post(
            client.api(f"/mixer/channels/{channel_id}/level"), json={"db": db}
        )
