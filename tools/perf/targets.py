"""The §23.1 throughput table and the §23.2 rows this harness also checks.

Transcribed once, verbatim, with their citation, so every scenario cites the
same text rather than repeating it. From ``docs/proskenion-spec-v3.1.html``,
heading ``23.1 Throughput`` (search for that heading text; do not read the
spec whole):

    | Concern                     | Target                | Measured by                     |
    |------------------------------|-----------------------|----------------------------------|
    | Concurrent WebSocket clients | 20 sustained, 50 peak | 50 connections holding the view |
    | Concurrent HTTP requests     | 100/sec sustained     | wrk against representative endpoints|
    | WebSocket control writes     | 300/sec sustained     | 10 clients dragging simultaneously |
    | Database writes              | 100 inserts/sec       | Scene execution log under load  |
    | KNX outgoing telegrams       | 15/sec, enforced      | Priority queue, simultaneous fades|
    | DMX frame rate               | 25-40 fps, stable     | Second receiver on the universe |

``23.2 Latency`` supplies one more row this harness uses as the round-trip
target for WebSocket control writes:

    | Operation                         | p50   | p99   |
    |-------------------------------------|-------|-------|
    | WebSocket control write to ``ack``  | 15 ms | 60 ms |

§7.1 *Outgoing writes* is the KNX budget itself: 15 telegrams/second,
enforced by a rolling-window admission gate (``proskenion/core/knx.py``,
``RATE_LIMIT_PER_SECOND``) — this harness does not hardcode that number
separately; :mod:`tools.perf.scenarios.knx_budget` imports it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

#: What kind of number "target" and "achieved" are, for the report renderer.
Unit = Literal["req_per_s", "clients", "writes_per_s", "inserts_per_s", "telegrams_per_s", "fps"]


@dataclass(frozen=True, slots=True)
class Target:
    """One row of §23.1 (or, for the control-write round trip, §23.2)."""

    row_id: str
    concern: str
    target_text: str
    citation: str
    measured_by: str
    unit: Unit
    #: The numeric target this harness checks "achieved" against. ``None``
    #: for a row with no single number (the WebSocket client-count row has
    #: two: sustained and peak, held separately below).
    target_value: float | None


HTTP_THROUGHPUT = Target(
    row_id="http_throughput",
    concern="Concurrent HTTP requests",
    target_text="100/sec sustained",
    citation="§23.1",
    measured_by="wrk against representative endpoints",
    unit="req_per_s",
    target_value=100.0,
)

WS_CLIENTS = Target(
    row_id="ws_clients",
    concern="Concurrent WebSocket clients",
    target_text="20 sustained, 50 peak",
    citation="§23.1",
    measured_by="50 connections holding the lighting view",
    unit="clients",
    target_value=50.0,
)
#: The two numbers §23.1's single cell actually names.
WS_CLIENTS_SUSTAINED = 20
WS_CLIENTS_PEAK = 50

WS_CONTROL_WRITES = Target(
    row_id="ws_control_writes",
    concern="WebSocket control writes",
    target_text="300/sec sustained",
    citation="§23.1",
    measured_by="10 clients dragging simultaneously",
    unit="writes_per_s",
    target_value=300.0,
)
#: §23.2's companion latency row for the same write path.
WS_CONTROL_WRITE_ACK_P50_MS = 15.0
WS_CONTROL_WRITE_ACK_P99_MS = 60.0
WS_CONTROL_WRITE_ACK_CITATION = "§23.2, \"WebSocket control write to ack\""

DB_INSERTS = Target(
    row_id="db_inserts",
    concern="Database writes",
    target_text="100 inserts/sec sustained",
    citation="§23.1",
    measured_by="Scene execution log under scripted load",
    unit="inserts_per_s",
    target_value=100.0,
)

KNX_TELEGRAMS = Target(
    row_id="knx_telegrams",
    concern="KNX outgoing telegrams",
    target_text="15/sec, enforced",
    citation="§23.1, §7.1 (RATE_LIMIT_PER_SECOND)",
    measured_by="Priority queue under simultaneous fades",
    unit="telegrams_per_s",
    target_value=15.0,
)

DMX_FRAME_RATE = Target(
    row_id="dmx_frame_rate",
    concern="DMX frame rate",
    target_text="25-40 fps, stable",
    citation="§23.1",
    measured_by="Second receiver on the universe",
    unit="fps",
    target_value=None,  # a range, not a floor or a ceiling — see the scenario
)
DMX_FRAME_RATE_MIN_FPS = 25.0
DMX_FRAME_RATE_MAX_FPS = 40.0
#: Measurement tolerance on §23.1's inclusive 25-40 range. The renderer runs a
#: 25 ms grid (exactly 40 fps) and timer jitter puts a measured mean a hair
#: over 40 (40.1 on the CM5, 1 Oct 2026, with no gaps and 24.1-25.8 ms intervals).
DMX_FRAME_RATE_TOLERANCE_FPS = 0.5

ALL_TARGETS: tuple[Target, ...] = (
    HTTP_THROUGHPUT,
    WS_CLIENTS,
    WS_CONTROL_WRITES,
    DB_INSERTS,
    KNX_TELEGRAMS,
    DMX_FRAME_RATE,
)
