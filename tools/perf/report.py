"""The readable table and the JSON file (§19's "is it fast enough?" answer,
recorded in WORKLOG.md by whoever runs this against the CM5 — P7-T11).

No table-formatting dependency is added: the report is six rows, and a
handful of ``str.ljust`` calls reads more plainly than a dependency would.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tools.perf.targets import Target

#: True/False/None ("not measurable here — see notes").
Verdict = bool | None


@dataclass(slots=True)
class ScenarioResult:
    """One §23.1 row's outcome. ``achieved`` is the number the target's own
    unit is measured in (requests/sec, clients connected, telegrams/sec,
    fps, ...); ``p50_ms``/``p95_ms``/``max_ms`` are the per-operation
    latency distribution where the scenario has one (every row except DMX
    frame rate, which reports inter-frame interval separately in ``notes``)."""

    target: Target
    achieved: float | None
    p50_ms: float | None
    p95_ms: float | None
    max_ms: float | None
    sample_count: int
    passed: Verdict
    notes: str
    skipped: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def skip(cls, target: Target, reason: str) -> ScenarioResult:
        """Dry-run safe, or off-VLAN, or no fixture identified — see
        ``tools/perf/README.md``'s "what it touches"."""
        return cls(
            target=target,
            achieved=None,
            p50_ms=None,
            p95_ms=None,
            max_ms=None,
            sample_count=0,
            passed=None,
            notes=reason,
            skipped=True,
        )


def _fmt(value: float | None, *, digits: int = 1) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "-"
    return f"{value:.{digits}f}"


def _fmt_verdict(passed: Verdict) -> str:
    return {True: "PASS", False: "FAIL", None: "n/a"}[passed]


_COLUMNS = ("Row", "Target", "Achieved", "p50 ms", "p95 ms", "max ms", "n", "Verdict")


def render_table(results: list[ScenarioResult]) -> str:
    """A fixed-width plain-text table — readable in a terminal, an SSH
    session or pasted into WORKLOG.md."""
    rows: list[tuple[str, ...]] = [_COLUMNS]
    for result in results:
        rows.append(
            (
                result.target.concern,
                result.target.target_text,
                _fmt(result.achieved, digits=2) if result.achieved is not None else "-",
                _fmt(result.p50_ms),
                _fmt(result.p95_ms),
                _fmt(result.max_ms),
                str(result.sample_count),
                _fmt_verdict(result.passed),
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(_COLUMNS))]
    lines = []
    for row_index, row in enumerate(rows):
        line = "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))
        lines.append(line.rstrip())
        if row_index == 0:
            lines.append("  ".join("-" * widths[i] for i in range(len(_COLUMNS))))
    return "\n".join(lines)


def render_notes(results: list[ScenarioResult]) -> str:
    """The per-row notes table doesn't have room for — citation, what it
    measured, what it could not measure from here."""
    lines = []
    for result in results:
        prefix = "SKIPPED: " if result.skipped else ""
        lines.append(
            f"{result.target.concern} ({result.target.citation}, {result.target.measured_by}):\n"
            f"  {prefix}{result.notes}"
        )
    return "\n".join(lines)


def to_json(
    results: list[ScenarioResult], *, base_url: str, tier: str | None, safety: dict[str, bool]
) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "base_url": base_url,
        "signed_in_as": tier,
        "safety": safety,
        "spec": "docs/proskenion-spec-v3.1.html §23.1, §23.2",
        "results": [
            {
                "row_id": r.target.row_id,
                "concern": r.target.concern,
                "citation": r.target.citation,
                "target_text": r.target.target_text,
                "measured_by": r.target.measured_by,
                "unit": r.target.unit,
                "achieved": r.achieved,
                "p50_ms": r.p50_ms,
                "p95_ms": r.p95_ms,
                "max_ms": r.max_ms,
                "sample_count": r.sample_count,
                "passed": r.passed,
                "skipped": r.skipped,
                "notes": r.notes,
                "extra": r.extra,
            }
            for r in results
        ],
    }


def write_json(
    path: Path,
    results: list[ScenarioResult],
    *,
    base_url: str,
    tier: str | None,
    safety: dict[str, bool],
) -> None:
    payload = to_json(results, base_url=base_url, tier=tier, safety=safety)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")


__all__ = [
    "ScenarioResult",
    "render_table",
    "render_notes",
    "to_json",
    "write_json",
]
