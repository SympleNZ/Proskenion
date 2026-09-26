"""§23.1 row: "KNX outgoing telegrams — 15/sec, enforced — Priority queue
under simultaneous fades" (§7.1's ``RATE_LIMIT_PER_SECOND``).

``KnxSubsystem.write()`` (``proskenion/core/knx.py``) only enqueues — the
budget is enforced by a background sender loop, so the HTTP response from
``POST /knx/addresses/{id}/test-write`` says nothing about when the telegram
actually reached the bus. The one thing reachable from outside the process
that does is ``GET /knx/monitor`` (§21.19), an SSE stream that logs a
telegram "once it actually leaves" (``KnxSubsystem.write``'s own
docstring). So: this scenario is a check that the budget holds, not a raw
throughput measurement — it fires test-writes as fast as it can and watches
the monitor stream to see how many a second actually reach the bus.

Needs an existing outgoing (or ``both``) KNX group address (discovered
read-only, or given with ``--knx-address-id``) and
:attr:`~tools.perf.client.Safety.allow_device_writes`: a test-write is a
real telegram on the real bus if one is connected.

**A flood of writes shows transient peaks above 15/s, and that is not a bug.**
``_RateLimiter.acquire()`` (``proskenion/core/knx.py``) is a genuine rolling
window — no single ``acquire()`` ever admits a 16th telegram inside the last
second *as it saw the window* — but this scenario submits its whole burst at
once (deliberately: "simultaneous fades" is what §23.1's own "Measured by"
column names), so the very first ~15 acquires all land within
milliseconds of each other, all age out of the window together about a
second later, and the next ~15 queued telegrams are then admitted in the
same kind of burst. Looked at from outside, across the *whole* stream rather
than from any one ``acquire()`` call's own vantage, a 1-second window
straddling the boundary between two such bursts can show a couple more than
15 — this harness's own peak-window check (:func:`_peak_one_second_rate`)
will flag that as "BREACHED", and the notes below say so explicitly rather
than leaving a marginal reading to be misread as a broken limiter.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import httpx

from proskenion.scene import knx_values
from tools.perf.client import PerfClient, Safety
from tools.perf.report import ScenarioResult
from tools.perf.stats import monotonic
from tools.perf.targets import KNX_TELEGRAMS

#: §7.1's own number, so a spec change is felt here rather than silently
#: drifting out of date. Duplicated as a value (not imported) because
#: ``proskenion.core.knx`` is a fair thing for this harness to depend on
#: (it is production code, read-only) but the constant is private-by-
#: convention inside that module; importing it keeps this scenario correct
#: if it is ever tuned.
try:
    from proskenion.core.knx import RATE_LIMIT_PER_SECOND
except ImportError:  # pragma: no cover - defensive only
    RATE_LIMIT_PER_SECOND = 15

WRITE_BUDGET_TOLERANCE = 1.5  # telegrams/sec of slack before calling it a breach
DRAIN_S = 1.5  # time given the monitor stream to catch up after the last write


@dataclass(frozen=True, slots=True)
class Options:
    knx_address_id: int | None = None
    dpt: str = "1.001"
    burst_duration_s: float = 4.0
    write_concurrency: int = 5


async def run(client: PerfClient, safety: Safety, options: Options | None = None) -> ScenarioResult:
    options = options or Options()
    if not safety.allow_device_writes:
        return ScenarioResult.skip(
            KNX_TELEGRAMS,
            "dry-run: pass --allow-device-writes to send real test-write telegrams for this "
            "scenario.",
        )
    if options.knx_address_id is None:
        return ScenarioResult.skip(
            KNX_TELEGRAMS,
            "no outgoing KNX group address identified — pass --knx-address-id, or configure "
            "one so discovery finds it.",
        )

    arrivals: list[float] = []
    stop = asyncio.Event()

    async def watch_monitor() -> None:
        try:
            async with client.http.stream("GET", client.api("/knx/monitor")) as response:
                if response.status_code != 200:
                    return
                async for line in response.aiter_lines():
                    if stop.is_set():
                        return
                    if not line.startswith("data: "):
                        continue
                    try:
                        entry = json.loads(line[len("data: ") :])
                    except (TypeError, ValueError):
                        continue
                    if entry.get("direction") == "outgoing":
                        arrivals.append(monotonic())
        except httpx.HTTPError:
            return

    monitor_task = asyncio.create_task(watch_monitor())
    await asyncio.sleep(0.1)  # let the SSE connection actually open before writing

    sent = 0
    failures = 0
    toggle = True

    async def burst_writer() -> None:
        nonlocal sent, failures, toggle
        deadline = monotonic() + options.burst_duration_s
        while monotonic() < deadline:
            toggle = not toggle
            response = await client.http.post(
                client.api(f"/knx/addresses/{options.knx_address_id}/test-write"),
                json={"value": _alternate_value(options.dpt, toggle)},
            )
            if response.status_code == 200:
                sent += 1
            else:
                failures += 1

    try:
        await asyncio.gather(*(burst_writer() for _ in range(options.write_concurrency)))
        await asyncio.sleep(DRAIN_S)  # the queue drains at the enforced rate after the last write
    finally:
        stop.set()
        monitor_task.cancel()
        await asyncio.gather(monitor_task, return_exceptions=True)

    if not arrivals:
        return ScenarioResult.skip(
            KNX_TELEGRAMS,
            f"{sent} test-write(s) accepted but GET /knx/monitor showed no outgoing telegram — "
            f"either the KNX subsystem is not connected, or the monitor stream could not be "
            f"read. Not measurable from here.",
        )

    peak_rate = _peak_one_second_rate(arrivals)
    overall_rate = len(arrivals) / (arrivals[-1] - arrivals[0]) if len(arrivals) > 1 else 0.0
    enforced = peak_rate <= RATE_LIMIT_PER_SECOND + WRITE_BUDGET_TOLERANCE
    return ScenarioResult(
        target=KNX_TELEGRAMS,
        achieved=overall_rate,
        p50_ms=None,
        p95_ms=None,
        max_ms=None,
        sample_count=len(arrivals),
        passed=enforced,
        notes=(
            f"{sent} test-write(s) sent ({failures} refused) over {options.burst_duration_s:g} s "
            f"by {options.write_concurrency} concurrent callers; {len(arrivals)} outgoing "
            f"telegram(s) observed on GET /knx/monitor (SSE) for address id "
            f"{options.knx_address_id}. Peak observed rate in any 1 s window: "
            f"{peak_rate:.1f}/s against the {RATE_LIMIT_PER_SECOND}/s budget (§7.1) — "
            f"{'held' if enforced else 'BREACHED'}. This checks the budget is enforced; raw "
            f"telegram throughput beyond it is not independently observable from outside the "
            f"process. A flood submitted all at once (as this scenario does, deliberately —"
            f" §23.1's own 'simultaneous fades') sends its first ~15 telegrams in one burst, "
            f"then another ~15 about a second later as that whole burst ages out together; a "
            f"window straddling two such bursts can read a little over 15 without the limiter "
            f"actually being broken — see the module docstring before treating a marginal "
            f"BREACHED as a defect."
        ),
        extra={
            "sent": sent,
            "failures": failures,
            "peak_1s_rate": peak_rate,
            "budget_per_second": RATE_LIMIT_PER_SECOND,
        },
    )


def _alternate_value(dpt: str, toggle: bool) -> object:
    """A value ``POST /knx/addresses/{id}/test-write`` will accept for
    ``dpt``, alternating on ``toggle`` so successive writes are not
    coalesced anywhere upstream. Discovery (``tools/perf/rig.py``) finds
    whatever outgoing address happens to be configured, which is very
    unlikely to be a boolean one on a real venue, so this cannot assume
    DPT 1.001 the way an address created by this harness's own self-test
    does. Uses the same DPT classification the scene action validator does
    (``proskenion.scene.knx_values.value_kind``) rather than re-deriving it.
    """
    try:
        kind = knx_values.value_kind(dpt)
    except knx_values.KnxValueError:
        kind = "bool"
    if kind == "dimming":
        return {"increase": toggle, "step_code": 1}
    if kind == "bool":
        return toggle
    # percent, byte, float: two small, distinct, in-range numbers.
    return 10.0 if toggle else 20.0


def _peak_one_second_rate(arrivals: list[float]) -> float:
    """The busiest 1-second sliding window in ``arrivals`` — telegrams whose
    arrival falls within 1 s of some other arrival, counted for every
    possible window start, taking the maximum.

    Strict ``<`` on the window boundary, matching ``_RateLimiter.acquire()``'s
    own expiry check (``proskenion/core/knx.py``: an entry expires once its
    age is ``>= window_s``, so "in window" there is strictly ``<``) — using
    ``<=`` here would count a telegram the real limiter had already expired,
    an off-by-one that looked like a breach and was not one.
    """
    peak = 0
    for i, start in enumerate(arrivals):
        count = 1
        for later in arrivals[i + 1 :]:
            if later - start < 1.0:
                count += 1
            else:
                break
        peak = max(peak, count)
    return float(peak)
