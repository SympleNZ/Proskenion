"""``GET /system/diagnostics`` [admin] — what the soak test cannot read from outside (§22.7).

§22.7 measures, at the start of the soak and every 12 hours, resident memory,
open file descriptors, WebSocket connections, asyncio tasks, SSD writes,
database size and event-loop lag. The harness (``tests/soak``) reads four of
those from outside the process, through ``/proc`` and the filesystem. The
other three exist only inside the event loop, so this endpoint answers them:

``tasks``
    ``len(asyncio.all_tasks())`` — every task on the loop, including the one
    answering this request. §23.3 targets fewer than 30 idle and 80 under load.
``websocket_connections``
    The broadcaster's open connections, the number ``/system/health`` also
    reports as ``application.clients``.
``loop_lag_p50_ms``, ``loop_lag_p99_ms``, ``loop_lag_samples``
    The watchdog task's own measurement (§4.7): it wakes every ten seconds
    and records how late it woke, over a rolling five-minute window. That is
    the periodic probe; nothing new runs here to take a reading. ``null``
    with zero samples before the boot sequence has started it.

It also names the process (``pid``) and how long it has been up, so the
harness reads ``/proc`` for the right process and can tell a restart from a
steady run without trusting its own bookkeeping.

Admin only, like ``/system/health``: it reveals nothing a signed-in admin
cannot already see there, but nobody else has a reason to ask.
"""

from __future__ import annotations

import asyncio
import os
import time

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from proskenion.api.deps import require_admin
from proskenion.core.broadcast import Broadcaster
from proskenion.core.watchdog import WatchdogTask

router = APIRouter(prefix="/system", tags=["system"])


class DiagnosticsResponse(BaseModel):
    """The in-process half of §22.7's measurements."""

    model_config = ConfigDict(extra="forbid")

    pid: int
    uptime_seconds: float
    tasks: int
    websocket_connections: int
    loop_lag_p50_ms: float | None
    loop_lag_p99_ms: float | None
    loop_lag_samples: int


@router.get(
    "/diagnostics",
    response_model=DiagnosticsResponse,
    dependencies=[Depends(require_admin)],
)
async def diagnostics(request: Request) -> DiagnosticsResponse:
    state = request.app.state
    broadcaster: Broadcaster | None = getattr(state, "broadcaster", None)
    lifecycle = getattr(state, "lifecycle", None)
    watchdog: WatchdogTask | None = None if lifecycle is None else lifecycle.watchdog
    started_at: float = state.started_at
    return DiagnosticsResponse(
        pid=os.getpid(),
        uptime_seconds=round(time.monotonic() - started_at, 3),
        tasks=len(asyncio.all_tasks()),
        websocket_connections=0 if broadcaster is None else broadcaster.connection_count,
        loop_lag_p50_ms=None if watchdog is None else watchdog.lag_p50_ms,
        loop_lag_p99_ms=None if watchdog is None else watchdog.lag_p99_ms,
        loop_lag_samples=0 if watchdog is None else watchdog.sample_count,
    )
