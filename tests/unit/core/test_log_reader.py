"""Reading the structured application log (spec §21.24, §16.7, §4.10).

Real temporary files stand in for ``/data/logs/`` — the live file, a
delaycompress-style rotated plain file and a gzip-compressed one, exactly
the three shapes ``appliance/etc/logrotate.d/auditorium`` produces — so the
tests exercise the actual file-format handling rather than a mock of it.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from proskenion.core import log_reader
from proskenion.logging import APPLICATION_LOG_NAME, REDACTED


def _line(
    *,
    timestamp: str,
    level: str = "INFO",
    logger: str = "proskenion.core.mixer",
    message: str = "a message",
    **extra: object,
) -> str:
    payload = {
        "timestamp": timestamp,
        "level": level,
        "logger": logger,
        "message": message,
        **extra,
    }
    return json.dumps(payload)


def _write(path: Path, lines: Iterable[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_gz(path: Path, lines: Iterable[str]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


# -- discovery, including rotated files -------------------------------------------------


def test_discover_log_files_orders_live_file_first_then_rotation_newest_first(
    tmp_path: Path,
) -> None:
    live = tmp_path / APPLICATION_LOG_NAME
    older_gz = tmp_path / f"{APPLICATION_LOG_NAME}-20250101-030000.gz"
    newer_plain = tmp_path / f"{APPLICATION_LOG_NAME}-20250201-030000"
    _write(live, [_line(timestamp="2025-03-01T09:00:00+13:00")])
    _write(newer_plain, [_line(timestamp="2025-02-01T03:00:00+13:00")])
    _write_gz(older_gz, [_line(timestamp="2025-01-01T03:00:00+13:00")])
    # A stray file that does not match logrotate's naming is ignored.
    (tmp_path / "application.log.bak").write_text("junk", encoding="utf-8")

    files = log_reader.discover_log_files(tmp_path)

    assert files == [live, newer_plain, older_gz]


def test_discover_log_files_is_empty_before_the_live_file_exists(tmp_path: Path) -> None:
    assert log_reader.discover_log_files(tmp_path) == []


# -- reading across files, both orders --------------------------------------------------


def test_iter_entries_newest_first_spans_the_live_and_rotated_files(tmp_path: Path) -> None:
    live = tmp_path / APPLICATION_LOG_NAME
    rotated = tmp_path / f"{APPLICATION_LOG_NAME}-20250101-030000.gz"
    _write(
        live,
        [
            _line(timestamp="2025-03-01T09:00:00+13:00", message="live-1"),
            _line(timestamp="2025-03-01T09:00:01+13:00", message="live-2"),
        ],
    )
    _write_gz(
        rotated,
        [
            _line(timestamp="2025-01-01T03:00:00+13:00", message="rotated-1"),
            _line(timestamp="2025-01-01T03:00:01+13:00", message="rotated-2"),
        ],
    )

    messages = [e.message for e in log_reader.iter_entries_newest_first(tmp_path)]

    assert messages == ["live-2", "live-1", "rotated-2", "rotated-1"]


def test_iter_entries_chronological_is_the_reverse_order_for_the_export(tmp_path: Path) -> None:
    live = tmp_path / APPLICATION_LOG_NAME
    rotated = tmp_path / f"{APPLICATION_LOG_NAME}-20250101-030000"
    _write(live, [_line(timestamp="2025-03-01T09:00:00+13:00", message="live-1")])
    _write(rotated, [_line(timestamp="2025-01-01T03:00:00+13:00", message="rotated-1")])

    messages = [e.message for e in log_reader.iter_entries_chronological(tmp_path)]

    assert messages == ["rotated-1", "live-1"]


def test_a_large_file_reads_correctly_backwards_across_several_chunks(tmp_path: Path) -> None:
    """The reverse reader walks a file in fixed chunks (§21.24) — this exercises several."""
    live = tmp_path / APPLICATION_LOG_NAME
    lines = [
        _line(
            timestamp=f"2025-03-01T{9 + i // 60:02d}:{i % 60:02d}:00+13:00",
            message=f"line-{i}",
            padding="x" * 400,
        )
        for i in range(300)
    ]
    _write(live, lines)
    assert live.stat().st_size > 130_000  # several times the reverse reader's 64 KB chunk size

    messages = [e.message for e in log_reader.iter_entries_newest_first(tmp_path, level=None)]

    assert messages == [f"line-{i}" for i in reversed(range(300))]


# -- malformed lines: skipped, not fatal -------------------------------------------------


def test_a_malformed_line_is_skipped_not_fatal(tmp_path: Path) -> None:
    live = tmp_path / APPLICATION_LOG_NAME
    _write(
        live,
        [
            _line(timestamp="2025-03-01T09:00:00+13:00", message="good-1"),
            "this is not json at all {{{",
            '{"not": "a log line shape but valid json"}',
            _line(timestamp="2025-03-01T09:00:02+13:00", message="good-2"),
        ],
    )

    messages = [e.message for e in log_reader.iter_entries_newest_first(tmp_path)]

    # good-2 (newest), then the valid-JSON-but-shapeless line (message defaults to ""),
    # then good-1 — the actually-malformed line never appears at all.
    assert "good-1" in messages
    assert "good-2" in messages
    assert len(messages) == 3


# -- filters: level, module, date range --------------------------------------------------


def test_level_filter_is_at_or_above(tmp_path: Path) -> None:
    live = tmp_path / APPLICATION_LOG_NAME
    _write(
        live,
        [
            _line(timestamp="2025-03-01T09:00:00+13:00", level="DEBUG", message="d"),
            _line(timestamp="2025-03-01T09:00:01+13:00", level="INFO", message="i"),
            _line(timestamp="2025-03-01T09:00:02+13:00", level="WARNING", message="w"),
            _line(timestamp="2025-03-01T09:00:03+13:00", level="ERROR", message="e"),
            _line(timestamp="2025-03-01T09:00:04+13:00", level="CRITICAL", message="c"),
        ],
    )

    messages = {e.message for e in log_reader.iter_entries_newest_first(tmp_path, level="WARNING")}

    assert messages == {"w", "e", "c"}


def test_module_filter_matches_the_logger_prefix_not_a_substring(tmp_path: Path) -> None:
    live = tmp_path / APPLICATION_LOG_NAME
    _write(
        live,
        [
            _line(
                timestamp="2025-03-01T09:00:00+13:00",
                logger="proskenion.core.mixer",
                message="in-scope",
            ),
            _line(
                timestamp="2025-03-01T09:00:01+13:00",
                logger="proskenion.core",
                message="exact-match",
            ),
            _line(
                timestamp="2025-03-01T09:00:02+13:00",
                logger="proskenion.corex",
                message="not-a-prefix",
            ),
        ],
    )

    messages = {
        e.message for e in log_reader.iter_entries_newest_first(tmp_path, module="proskenion.core")
    }

    assert messages == {"in-scope", "exact-match"}


def test_date_range_filters_by_the_parsed_instant(tmp_path: Path) -> None:
    live = tmp_path / APPLICATION_LOG_NAME
    _write(
        live,
        [
            _line(timestamp="2025-03-01T08:00:00+13:00", message="too-early"),
            _line(timestamp="2025-03-01T09:00:00+13:00", message="in-range"),
            _line(timestamp="2025-03-01T10:00:00+13:00", message="too-late"),
        ],
    )

    since = datetime.fromisoformat("2025-03-01T08:30:00+13:00")
    until = datetime.fromisoformat("2025-03-01T09:30:00+13:00")
    messages = [
        e.message for e in log_reader.iter_entries_newest_first(tmp_path, since=since, until=until)
    ]

    assert messages == ["in-range"]


# -- redaction, as defence in depth -------------------------------------------------------


def test_context_values_under_a_sensitive_key_are_redacted_on_read(tmp_path: Path) -> None:
    live = tmp_path / APPLICATION_LOG_NAME
    # A value that should never have reached disk unredacted; read-time
    # redaction catches it anyway (§21.24 "never return secrets").
    _write(live, [_line(timestamp="2025-03-01T09:00:00+13:00", password="hunter2", device="mixer")])

    [entry] = list(log_reader.iter_entries_newest_first(tmp_path))

    assert entry.context["password"] == REDACTED
    assert entry.context["device"] == "mixer"


# -- pagination ---------------------------------------------------------------------------


def test_query_entries_paginates_newest_first_and_reports_has_more(tmp_path: Path) -> None:
    live = tmp_path / APPLICATION_LOG_NAME
    _write(
        live,
        [_line(timestamp=f"2025-03-01T09:00:{i:02d}+13:00", message=f"m{i}") for i in range(5)],
    )

    page1, more1 = log_reader.query_entries(
        tmp_path, level=None, module=None, since=None, until=None, limit=2, offset=0
    )
    page2, more2 = log_reader.query_entries(
        tmp_path, level=None, module=None, since=None, until=None, limit=2, offset=2
    )
    page3, more3 = log_reader.query_entries(
        tmp_path, level=None, module=None, since=None, until=None, limit=2, offset=4
    )

    assert [e.message for e in page1] == ["m4", "m3"]
    assert more1 is True
    assert [e.message for e in page2] == ["m2", "m1"]
    assert more2 is True
    assert [e.message for e in page3] == ["m0"]
    assert more3 is False


# -- export -------------------------------------------------------------------------------


def test_export_lines_are_chronological_plain_text_and_redacted(tmp_path: Path) -> None:
    live = tmp_path / APPLICATION_LOG_NAME
    _write(
        live,
        [
            _line(timestamp="2025-03-01T09:00:00+13:00", message="first", device="mixer"),
            _line(timestamp="2025-03-01T09:00:01+13:00", message="second", pin="1234"),
        ],
    )

    lines = list(log_reader.export_lines(tmp_path, level=None, module=None, since=None, until=None))

    assert len(lines) == 2
    assert "first" in lines[0]
    assert "device=mixer" in lines[0]
    assert "second" in lines[1]
    assert REDACTED in lines[1]
    assert "1234" not in lines[1]
