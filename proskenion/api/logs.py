"""The System tab of Admin → System → Logs (spec §21.24 "Logs", §16.7, §4.10).

Reads ``<logging.path>/application.log`` and its rotated files
(``appliance/etc/logrotate.d/auditorium``) through
:mod:`proskenion.core.log_reader` — streamed, never loaded whole into
memory, and passed through :func:`proskenion.logging.redact` again on the
way out as defence in depth.

A separate module from ``proskenion/api/system.py`` — already the largest
file in the package — so the two can be worked on independently; both are
mounted under the same ``/system`` prefix.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from proskenion.api.deps import get_config, require_admin
from proskenion.config import Config
from proskenion.core.log_reader import MAX_LIMIT, export_lines, query_entries
from proskenion.db.crud.base import AUCKLAND

router = APIRouter(prefix="/system", tags=["system"])


class LogLevelFilter(StrEnum):
    """The closed set of levels §4.10 writes; a level filter is "at or above" one of these."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class SystemLogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: str
    level: str
    logger: str
    message: str
    context: dict[str, Any]


class SystemLogResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[SystemLogEntry]
    #: Whether another page exists beyond this one (offset + limit).
    has_more: bool


def _instant(value: datetime | None) -> datetime | None:
    """A query parameter as an aware instant; a value with no offset is Auckland time (§4.9)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=AUCKLAND)
    return value


@router.get("/logs", response_model=SystemLogResponse, dependencies=[Depends(require_admin)])
async def get_logs(
    config: Annotated[Config, Depends(get_config)],
    level: LogLevelFilter | None = None,
    module: str | None = None,
    since: Annotated[datetime | None, Query(alias="from")] = None,
    until: Annotated[datetime | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SystemLogResponse:
    """The raw structured application log, filtered, newest first — admin only (§21.24 "Logs").

    Reading is synchronous file I/O, so it runs off the event loop (§5.3).
    """
    entries, has_more = await asyncio.to_thread(
        query_entries,
        config.logging.path,
        level=level.value if level is not None else None,
        module=module,
        since=_instant(since),
        until=_instant(until),
        limit=limit,
        offset=offset,
    )
    return SystemLogResponse(
        entries=[
            SystemLogEntry(
                timestamp=e.timestamp,
                level=e.level,
                logger=e.logger,
                message=e.message,
                context=e.context,
            )
            for e in entries
        ],
        has_more=has_more,
    )


def _export_body(
    log_dir: Any,
    *,
    level: str | None,
    module: str | None,
    since: datetime | None,
    until: datetime | None,
) -> Iterator[bytes]:
    """The export generator — synchronous, so Starlette streams it off the event loop for us."""
    for line in export_lines(log_dir, level=level, module=module, since=since, until=until):
        yield line.encode("utf-8")


@router.get("/logs/export", dependencies=[Depends(require_admin)])
async def export_logs(
    config: Annotated[Config, Depends(get_config)],
    level: LogLevelFilter | None = None,
    module: str | None = None,
    since: Annotated[datetime | None, Query(alias="from")] = None,
    until: Annotated[datetime | None, Query(alias="to")] = None,
) -> StreamingResponse:
    """Filtered plain text (§16.7) as a streamed download — never the whole file in memory."""
    body = _export_body(
        config.logging.path,
        level=level.value if level is not None else None,
        module=module,
        since=_instant(since),
        until=_instant(until),
    )
    return StreamingResponse(
        body,
        media_type="text/plain",
        headers={"Content-Disposition": 'attachment; filename="application-log.txt"'},
    )
