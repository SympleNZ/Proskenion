"""Reading the structured application log for the admin System tab (spec §21.24, §16.7, §4.10).

The log itself is written by :mod:`proskenion.logging`: one JSON object per
line in ``<logging.path>/application.log``, rotated by logrotate into
``application.log-YYYYMMDD-HHMMSS[.gz]`` (§4.10,
``appliance/etc/logrotate.d/auditorium``). Nothing here rotates or writes;
this module only reads, and it never opens a whole file into memory —
logs can be 100 MB (§21.24).

Two access patterns, two orders:

* :func:`query_entries` — the admin viewer's pagination. Files are visited
  newest to oldest, and each is read backwards in bounded chunks, so
  "newest first, with a cap" never needs the file's later lines to have
  been read first.
* :func:`export_lines` — the download. A plain streamed read forwards
  through each file in turn, oldest to newest: the order the lines were
  originally written in.

A line that is not valid JSON is skipped, not fatal (a torn write during a
crash, say) — logged once at WARNING and left out of both streams. Every
entry is passed back through :func:`proskenion.logging.redact` before it
leaves this module, as defence in depth: :mod:`proskenion.logging` already
redacts at write time, but a value under a sensitive-looking key should
never reach an admin screen or a downloaded file twice over, once here too.
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from proskenion.logging import APPLICATION_LOG_NAME, redact

log = logging.getLogger(__name__)

#: Standard library level names, in ascending severity (§4.10).
LEVELS: Final[tuple[str, ...]] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_LEVEL_RANK: Final[dict[str, int]] = {name: rank for rank, name in enumerate(LEVELS)}

#: The most rows one page returns (mirrors ``security_events.MAX_LIMIT``).
MAX_LIMIT: Final = 1000

#: ``application.log-YYYYMMDD-HHMMSS[.gz]`` — logrotate's own ``dateext``/
#: ``dateformat`` naming (``appliance/etc/logrotate.d/auditorium``).
_ROTATED_RE: Final[re.Pattern[str]] = re.compile(
    r"\A" + re.escape(APPLICATION_LOG_NAME) + r"-(\d{8})-(\d{6})(\.gz)?\Z"
)

#: Bytes read per chunk when a file is walked backwards — bounds peak memory
#: to one chunk plus the longest line, regardless of file size.
_CHUNK_SIZE: Final = 65536


@dataclass(frozen=True, slots=True)
class LogEntry:
    """One parsed, redacted line of the application log."""

    timestamp: str
    level: str
    logger: str
    message: str
    context: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _LogFile:
    path: Path
    #: The rotation instant read from the filename (local, naive — the wall
    #: clock logrotate ran on); ``None`` for the live file, which is always
    #: the newest regardless.
    rotated_at: datetime | None


def _rotated_at(name: str) -> datetime | None:
    match = _ROTATED_RE.match(name)
    if not match:
        return None
    date_part, time_part, _gz = match.groups()
    try:
        return datetime.strptime(date_part + time_part, "%Y%m%d%H%M%S")
    except ValueError:
        return None


def discover_log_files(log_dir: Path) -> list[Path]:
    """Every application log file, newest first: the live file, then rotated ones.

    Only ``application.log`` itself and files matching logrotate's own
    ``application.log-YYYYMMDD-HHMMSS[.gz]`` naming are returned — a stray
    file left in the directory is ignored rather than guessed at.
    """
    return [f.path for f in _discover(log_dir)]


def _discover(log_dir: Path) -> list[_LogFile]:
    files: list[_LogFile] = []
    live = log_dir / APPLICATION_LOG_NAME
    if live.is_file():
        files.append(_LogFile(live, None))
    if log_dir.is_dir():
        for candidate in log_dir.iterdir():
            if candidate == live or not candidate.is_file():
                continue
            rotated_at = _rotated_at(candidate.name)
            if rotated_at is not None:
                files.append(_LogFile(candidate, rotated_at))
    # The live file (rotated_at is None) sorts as the newest of all; rotated
    # files then sort by their own rotation instant, most recent first.
    files.sort(key=lambda f: f.rotated_at or datetime.max, reverse=True)
    return files


# -- reading one file, forwards and backwards -----------------------------------------


def _reverse_lines_plain(path: Path, chunk_size: int = _CHUNK_SIZE) -> Iterator[bytes]:
    """``path``'s lines, most recent (last in the file) first.

    Reads backwards in fixed-size chunks from the end so a 100 MB file is
    never loaded at once.
    """
    with path.open("rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        trailing = b""
        while position > 0:
            read_size = min(chunk_size, position)
            position -= read_size
            handle.seek(position)
            chunk = handle.read(read_size) + trailing
            lines = chunk.split(b"\n")
            # lines[0] may be a partial line continuing into the previous
            # (earlier) chunk; hold it over rather than yield it now.
            trailing = lines[0]
            for line in reversed(lines[1:]):
                if line:
                    yield line
        if trailing:
            yield trailing


def _reverse_lines_gz(path: Path) -> Iterator[bytes]:
    """A rotated ``.gz`` file's lines, most recent first.

    gzip gives no random access, so the archive is decompressed once into a
    temporary file — streamed across in chunks, never held whole in
    memory — and that temporary file is what is actually read backwards.
    Written and closed before it is reopened, and always removed after: on
    Windows a still-open handle cannot be reopened by a second one.
    """
    fd, tmp_name = tempfile.mkstemp(prefix="proskenion-log-", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with open(fd, "wb") as spool, gzip.open(path, "rb") as source:
            while True:
                block = source.read(_CHUNK_SIZE)
                if not block:
                    break
                spool.write(block)
        yield from _reverse_lines_plain(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _reverse_lines(path: Path) -> Iterator[bytes]:
    if path.suffix == ".gz":
        yield from _reverse_lines_gz(path)
    else:
        yield from _reverse_lines_plain(path)


def _forward_lines(path: Path) -> Iterator[bytes]:
    """``path``'s lines in the order they were written, streamed — no reversal."""
    if path.suffix == ".gz":
        with gzip.open(path, "rb") as handle:
            for raw in handle:
                yield raw.rstrip(b"\n")
    else:
        with path.open("rb") as handle:
            for raw in handle:
                yield raw.rstrip(b"\n")


# -- parsing, redaction and filtering ---------------------------------------------------


def _parse_line(raw: bytes) -> LogEntry | None:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        log.warning("a line in the application log was not valid JSON; skipping it")
        return None
    if not isinstance(parsed, dict):
        log.warning("a line in the application log was not a JSON object; skipping it")
        return None
    redacted: dict[str, Any] = redact(parsed)
    timestamp = str(redacted.pop("timestamp", ""))
    level = str(redacted.pop("level", ""))
    logger_name = str(redacted.pop("logger", ""))
    message = str(redacted.pop("message", ""))
    return LogEntry(
        timestamp=timestamp, level=level, logger=logger_name, message=message, context=redacted
    )


def _parse_instant(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _meets_level(entry_level: str, minimum: str | None) -> bool:
    if minimum is None:
        return True
    threshold = _LEVEL_RANK.get(minimum.upper())
    if threshold is None:
        # An unrecognised filter admits everything rather than silently
        # emptying the result; the API layer validates against LEVELS first.
        return True
    rank = _LEVEL_RANK.get(entry_level.upper())
    return rank is not None and rank >= threshold


def _meets_module(logger_name: str, module: str | None) -> bool:
    if not module:
        return True
    return logger_name == module or logger_name.startswith(f"{module}.")


def _within_range(entry_timestamp: str, since: datetime | None, until: datetime | None) -> bool:
    if since is None and until is None:
        return True
    parsed = _parse_instant(entry_timestamp)
    if parsed is None:
        return False
    if since is not None and parsed < since:
        return False
    if until is not None and parsed > until:
        return False
    return True


def _matches(
    entry: LogEntry,
    *,
    level: str | None,
    module: str | None,
    since: datetime | None,
    until: datetime | None,
) -> bool:
    return (
        _meets_level(entry.level, level)
        and _meets_module(entry.logger, module)
        and _within_range(entry.timestamp, since, until)
    )


# -- the two public streams ---------------------------------------------------------------


def iter_entries_newest_first(
    log_dir: Path,
    *,
    level: str | None = None,
    module: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Iterator[LogEntry]:
    """Every matching entry, most recent first — the admin viewer's order."""
    for log_file in _discover(log_dir):
        for raw in _reverse_lines(log_file.path):
            entry = _parse_line(raw)
            if entry is not None and _matches(
                entry, level=level, module=module, since=since, until=until
            ):
                yield entry


def iter_entries_chronological(
    log_dir: Path,
    *,
    level: str | None = None,
    module: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Iterator[LogEntry]:
    """Every matching entry, oldest first — the order the export is written in."""
    for log_file in reversed(_discover(log_dir)):
        for raw in _forward_lines(log_file.path):
            entry = _parse_line(raw)
            if entry is not None and _matches(
                entry, level=level, module=module, since=since, until=until
            ):
                yield entry


def query_entries(
    log_dir: Path,
    *,
    level: str | None,
    module: str | None,
    since: datetime | None,
    until: datetime | None,
    limit: int,
    offset: int,
) -> tuple[list[LogEntry], bool]:
    """Up to ``limit`` entries starting at ``offset``, newest first, and whether more remain.

    Run this off the event loop (``asyncio.to_thread``) — reading is
    synchronous file I/O (§5.3).
    """
    page: list[LogEntry] = []
    has_more = False
    skipped = 0
    entries = iter_entries_newest_first(
        log_dir, level=level, module=module, since=since, until=until
    )
    for entry in entries:
        if skipped < offset:
            skipped += 1
            continue
        if len(page) >= limit:
            has_more = True
            break
        page.append(entry)
    return page, has_more


def format_line(entry: LogEntry) -> str:
    """One entry as a plain text line, for the export."""
    parts = [entry.timestamp, entry.level, f"{entry.logger}:", entry.message]
    if entry.context:
        parts.append(" ".join(f"{key}={_plain(value)}" for key, value in entry.context.items()))
    return " ".join(parts)


def _plain(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str, ensure_ascii=False)


def export_lines(
    log_dir: Path,
    *,
    level: str | None,
    module: str | None,
    since: datetime | None,
    until: datetime | None,
) -> Iterator[str]:
    """Filtered plain text, oldest first, one line per entry, each ending in ``\\n``.

    A generator, consumed lazily by the streaming response — nothing here
    materialises the whole export in memory.
    """
    entries = iter_entries_chronological(
        log_dir, level=level, module=module, since=since, until=until
    )
    for entry in entries:
        yield format_line(entry) + "\n"
