"""P7-T8: the performance harness (spec §23.1, §23.2).

A standalone tool, not part of the ``proskenion`` application, that measures
each row of the §23.1 throughput table against a running Proskenion server —
the dev server, the in-process app started with the device stubs in
``tests/stubs/`` (see ``tests/unit/tools/test_perf_harness.py``), or the real
CM5 appliance.

Run it with ``uv run python -m tools.perf`` — see ``tools/perf/README.md``
for how Simon runs it from a Windows laptop against
``https://auditorium.obhs.school.nz``.

Layout
------
``targets.py``
    The §23.1 targets and the §23.2 rows used as supporting latency
    references, transcribed once with their citations.
``stats.py``
    Honest measurement: monotonic timing, percentiles, warm-up.
``client.py``
    Login (credentials never stored or printed) and a thin REST/WebSocket
    client wrapper.
``scenarios/``
    One module per §23.1 row.
``report.py``
    The readable table and the JSON file for WORKLOG.
``cli.py`` / ``__main__.py``
    ``uv run python -m tools.perf ...``.
"""

from __future__ import annotations
