"""Background work that outlives one failure (§5.6 isolation, §11.4).

The appliance runs unattended, so a background task that dies takes its duty
with it and nobody notices until the room needs it. On 24 September 2026 the
first real commissioning lost device-red alerting for the DMX node exactly
that way: a ``PermissionError`` writing the SMTP fallback escaped
``AlertSink.send``, ended the task ``alerts:device-red:dmx``, and asyncio said
only "Task exception was never retrieved" — at shutdown, if at all.

Three rules, one module, used everywhere a long-running watcher lives
(alerts, the backup status watcher, the health monitors, the reset-tool and
update watches):

* :func:`spawn` — every fire-and-forget task gets a done-callback that logs
  what escaped it, by task name, the moment it happens. Nothing is ever
  "never retrieved".
* :func:`contained` — one unit of work (one poll, one alert) is awaited with
  every ``Exception`` caught and logged. Cancellation is never swallowed:
  shutdown must still be able to stop the watcher.
* :func:`every` — the loop the pollers share: do one :func:`contained` step,
  sleep, repeat, until cancelled. One failed iteration costs that iteration,
  never the watcher.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, NoReturn

log = logging.getLogger(__name__)

Sleeper = Callable[[float], Awaitable[None]]


def report_failure(task: asyncio.Task[Any]) -> None:
    """Done-callback: log whatever escaped ``task``. Cancellation is not a failure."""
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        log.error(
            "background task %s failed",
            task.get_name(),
            exc_info=error,
            extra={"task": task.get_name()},
        )


def spawn[T](coroutine: Coroutine[Any, Any, T], *, name: str) -> asyncio.Task[T]:
    """``create_task`` with :func:`report_failure` attached. See the module docstring."""
    task = asyncio.get_running_loop().create_task(coroutine, name=name)
    task.add_done_callback(report_failure)
    return task


async def contained[T](what: str, work: Awaitable[T]) -> T | None:
    """Await ``work``; on any ``Exception`` log it against ``what`` and return
    ``None``. ``CancelledError`` (a ``BaseException``) always propagates."""
    try:
        return await work
    except Exception:
        log.exception("%s failed; carrying on", what)
        return None


async def every(
    what: str,
    step: Callable[[], Awaitable[object]],
    *,
    interval_s: float,
    sleep: Sleeper | None = None,
    delay_first: bool = False,
) -> NoReturn:
    """Run ``step`` every ``interval_s`` seconds until cancelled.

    ``sleep`` is resolved when the loop runs, not when this module is
    imported, so a test that replaces ``asyncio.sleep`` still reaches it.
    ``delay_first`` sleeps before the first step, for a watch whose first
    look is not wanted at start-up.
    """
    pause = sleep or asyncio.sleep
    if delay_first:
        await pause(interval_s)
    while True:
        await contained(what, step())
        await pause(interval_s)


__all__ = ["Sleeper", "contained", "every", "report_failure", "spawn"]
