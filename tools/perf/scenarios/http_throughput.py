"""§23.1 row 1: "Concurrent HTTP requests — 100/sec sustained — ``wrk``
against representative endpoints."

No ``wrk`` on Windows (the brief), so this drives the same shape of load in
asyncio: several concurrent workers hammering a small set of representative
**read** endpoints — ``GET /health`` (public, unversioned, §16.7) plus every
``GET .../state`` endpoint a dashboard actually polls — for a fixed window,
after a short warm-up.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tools.perf.client import PerfClient, Safety
from tools.perf.report import ScenarioResult
from tools.perf.stats import run_concurrent_workers
from tools.perf.targets import HTTP_THROUGHPUT

#: Representative reads (§23.1's own wording): the public health check
#: nginx and monitoring hit constantly, plus the state endpoints the
#: operator and booth screens poll (spec §16.5, §16.7).
DEFAULT_ENDPOINTS: tuple[str, ...] = (
    "/health",
    "/system/health",
    "/mixer/state",
    "/lighting/state",
)


@dataclass(frozen=True, slots=True)
class Options:
    duration_s: float = 5.0
    warm_up_s: float = 0.5
    concurrency: int = 10
    endpoints: tuple[str, ...] = field(default_factory=lambda: DEFAULT_ENDPOINTS)


async def run(client: PerfClient, safety: Safety, options: Options | None = None) -> ScenarioResult:
    """Always safe: every endpoint here is a ``GET``, and none is gated by
    :class:`~tools.perf.client.Safety` — reading state, however often,
    changes nothing (§16.1)."""
    options = options or Options()
    paths = [p if p == "/health" else client.api(p) for p in options.endpoints]

    async def hit(worker_index: int) -> None:
        path = paths[worker_index % len(paths)]
        response = await client.http.get(path)
        if response.status_code >= 500:
            raise RuntimeError(f"{path}: HTTP {response.status_code}")

    measurement, window_s = await run_concurrent_workers(
        concurrency=options.concurrency,
        duration_s=options.duration_s,
        warm_up_s=options.warm_up_s,
        operation=hit,
    )
    rate = measurement.rate_per_second(window_s)
    passed = rate >= HTTP_THROUGHPUT.target_value if HTTP_THROUGHPUT.target_value else None
    return ScenarioResult(
        target=HTTP_THROUGHPUT,
        achieved=rate,
        p50_ms=measurement.p50_ms,
        p95_ms=measurement.p95_ms,
        max_ms=measurement.max_ms,
        sample_count=measurement.count,
        passed=passed,
        notes=(
            f"{options.concurrency} workers cycling {len(paths)} endpoints "
            f"({', '.join(options.endpoints)}) for {options.duration_s:g} s after a "
            f"{options.warm_up_s:g} s warm-up; {measurement.failures} failure(s)."
        ),
        extra={"endpoints": list(options.endpoints), "failures": measurement.failures},
    )
