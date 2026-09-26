"""The application's side of the privileged helper (contracts §2, Q2).

The application runs unprivileged under ``NoNewPrivileges=yes`` (§4.11), so
anything needing root — restarting a service, rebooting, applying an update,
writing a root slot — is asked for rather than done. This module writes the
request file, watches the status file the helper writes beside it, and turns
each step into a ``progress`` frame (§16.8: real steps, never an
indeterminate spinner).

Nothing here is a security boundary. ``auditorium-helper`` validates every
argument itself and re-verifies every package, because the request comes from
this process and this process is the one an attacker would already be inside.
What this module owes the operator is different: an answer, or a clear failure.
A helper that never runs — a path unit that was not enabled, a unit that
failed before it wrote anything, a machine that rebooted mid-operation — must
surface as a timeout naming the operation, not as a spinner that turns
forever.

Usage::

    helper = HelperClient(data_dir, progress=broadcaster_progress)
    await helper.run("restart-core")
    await helper.run("apply-update", package=str(upload), version="v1.3.0")

and, for the one verb whose answer never arrives because the machine goes
away under it::

    await helper.run("reboot", mode="tryboot", settle="running")
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal

log = logging.getLogger(__name__)

#: Where requests and statuses live, under the configured data directory.
HELPER_DIR_PARTS: Final[tuple[str, ...]] = ("run", "helper")

#: The §16.8 progress operation each verb is relayed as (contracts §6). The
#: vocabulary is closed: a verb missing from here reports no progress rather
#: than inventing an operation name the interface does not know.
OPERATIONS: Final[Mapping[str, str]] = {
    "apply-network": "network_apply",
    "apply-update": "update_apply",
    "write-slot": "os_write",
    "stage-slot": "os_stage",
    "capture-image": "image_capture",
    "backup-now": "backup_run",
}

#: How long to wait for the helper to answer at all. An update extracts a
#: package, snapshots the database and holds the §4.7 watchdog window open for
#: 60 s, so the bound is per operation.
DEFAULT_TIMEOUTS: Final[Mapping[str, float]] = {
    "apply-update": 900.0,
    "write-slot": 3600.0,
    "capture-image": 3600.0,
    "backup-now": 3600.0,
}
DEFAULT_TIMEOUT_S: Final[float] = 120.0

#: The helper writes its first status as soon as it picks a request up. If
#: nothing appears within this, the path unit is not running.
DEFAULT_ACKNOWLEDGE_S: Final[float] = 30.0
DEFAULT_POLL_S: Final[float] = 0.2

#: §4.7 and §14.5: the watchdog window is widened for the first sixty seconds
#: after an update, so a slow first start on a version that has never run here
#: before is not mistaken for a hang. This is what the application asks for;
#: the helper bounds it, and the helper alone decides what the drop-in
#: contains — a request says how long, never how much.
DEFAULT_WATCHDOG_WINDOW_S: Final[int] = 60

Settle = Literal["done", "running"]

#: ``progress(operation, step, of, message)``.
ProgressSink = Callable[[str, int, int, str], None]

#: ``await sleep(seconds)``. Injected so a test drives the poll loop on its
#: own clock rather than waiting for one.
Sleeper = Callable[[float], Awaitable[None]]


class HelperError(RuntimeError):
    """The privileged operation did not succeed."""


class HelperUnavailable(HelperError):
    """The helper never answered: it is not running, or it died silently."""


class HelperRefused(HelperError):
    """The helper refused or failed the request, and said why."""


@dataclass(frozen=True, slots=True)
class HelperStatus:
    """One reading of ``<uuid>.status.json`` (contracts §2)."""

    id: str
    state: str
    step: int
    of: int
    message: str
    error: str | None
    finished_at: str | None

    @property
    def finished(self) -> bool:
        return self.state in ("done", "failed")

    @classmethod
    def from_json(cls, data: Mapping[str, Any], *, request_id: str) -> HelperStatus | None:
        if str(data.get("id", "")) != request_id:
            return None
        state = str(data.get("state", ""))
        if state not in ("running", "done", "failed"):
            return None
        return cls(
            id=request_id,
            state=state,
            step=_int(data.get("step")),
            of=max(1, _int(data.get("of"))),
            message=str(data.get("message") or ""),
            error=str(data["error"]) if data.get("error") else None,
            finished_at=str(data["finished_at"]) if data.get("finished_at") else None,
        )


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


class HelperClient:
    """Writes requests, follows statuses, relays progress."""

    def __init__(
        self,
        data_dir: Path,
        *,
        progress: ProgressSink | None = None,
        poll_interval_s: float = DEFAULT_POLL_S,
        acknowledge_s: float = DEFAULT_ACKNOWLEDGE_S,
        timeouts: Mapping[str, float] = DEFAULT_TIMEOUTS,
        default_timeout_s: float = DEFAULT_TIMEOUT_S,
        sleep: Sleeper = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], str] | None = None,
    ) -> None:
        self.directory = data_dir.joinpath(*HELPER_DIR_PARTS)
        self._progress = progress
        self._poll = poll_interval_s
        self._acknowledge = acknowledge_s
        self._timeouts = dict(timeouts)
        self._default_timeout = default_timeout_s
        self._sleep = sleep
        self._clock = clock
        self._now = now or (lambda: datetime.now().astimezone().isoformat(timespec="seconds"))

    # -- the request -------------------------------------------------------

    def submit_sync(self, verb: str, **args: Any) -> str:
        """Write a request and return its id. Blocking; the file is tiny."""
        request_id = str(uuid.uuid4())
        body = {
            "verb": verb,
            "id": request_id,
            "requested_at": self._now(),
            "args": {key: value for key, value in args.items() if value is not None},
        }
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{request_id}.json"
        tmp = self.directory / f".{request_id}.tmp"
        # 0600 and complete before it is visible: the helper refuses a request
        # readable beyond its owner, and a path unit fires on a name appearing,
        # which must never be a half-written file.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(body, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return request_id

    async def submit(self, verb: str, **args: Any) -> str:
        return await asyncio.to_thread(lambda: self.submit_sync(verb, **args))

    # -- the answer --------------------------------------------------------

    def read_status(self, request_id: str) -> HelperStatus | None:
        path = self.directory / f"{request_id}.status.json"
        try:
            raw = path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError as exc:
            log.debug("helper status unreadable", extra={"id": request_id, "error": str(exc)})
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            # A status being rewritten is replaced by rename, so this is a
            # truncated read rather than a corrupt file: try again next poll.
            return None
        if not isinstance(data, dict):
            return None
        return HelperStatus.from_json(data, request_id=request_id)

    async def wait(
        self,
        request_id: str,
        verb: str,
        *,
        settle: Settle = "done",
        timeout_s: float | None = None,
    ) -> HelperStatus:
        """Follow the status file until the operation settles, or give up.

        Each new step is relayed as a ``progress`` frame. Two deadlines apply:
        the helper must acknowledge the request at all within
        ``acknowledge_s``, and the whole operation must settle within the
        verb's timeout. Both failures raise :class:`HelperUnavailable` naming
        the verb, because "the appliance did not answer" is what the operator
        needs to read.

        The terminal read is relayed too, not only the ``running`` ones in
        between. ``auditorium-helper`` writes its last few steps back to back
        — there is no work left to do between them — so on a poll interval
        this wide (``DEFAULT_POLL_S``) it is routine, not exceptional, for a
        read to land on the status file already in its ``done`` state, having
        skipped every intermediate ``running`` write between the last one
        this loop actually saw and the end. A client gates "still running" on
        the last progress frame's ``step < of``; if the ``done`` transition
        were never relayed, that gate would never close on its own — stuck
        exactly the way it was found on the real appliance (a backup whose
        history already read "succeeded" while its progress panel stayed on
        "Running the backup job"). Relaying ``done`` (written with
        ``step == of`` by ``auditorium-helper``'s ``handle()``) guarantees
        the gate is told, however much of the middle it missed.
        """
        operation = OPERATIONS.get(verb)
        limit = timeout_s if timeout_s is not None else self._timeouts.get(
            verb, self._default_timeout
        )
        started = self._clock()
        last: tuple[int, str, str] | None = None
        status: HelperStatus | None = None
        while True:
            status = await asyncio.to_thread(self.read_status, request_id)
            if status is not None:
                key = (status.step, status.message, status.state)
                if key != last:
                    last = key
                    if operation is not None and status.state in ("running", "done", "failed"):
                        self._emit(operation, status)
                if status.state == "failed":
                    raise HelperRefused(
                        status.error or f"{verb} failed without saying why"
                    )
                if status.state == "done" or (settle == "running" and status.state == "running"):
                    return status
            elapsed = self._clock() - started
            if status is None and elapsed >= self._acknowledge:
                self._discard(request_id)
                raise HelperUnavailable(
                    f"the appliance helper did not pick up {verb} within "
                    f"{self._acknowledge:g} s; auditorium-helper.path may not be running"
                )
            if elapsed >= limit:
                self._discard(request_id)
                raise HelperUnavailable(
                    f"{verb} did not finish within {limit:g} s "
                    f"(last step: {status.message if status else 'none'})"
                )
            await self._sleep(self._poll)

    def _emit(self, operation: str, status: HelperStatus) -> None:
        if self._progress is None:
            return
        try:
            self._progress(operation, status.step, status.of, status.message)
        except Exception:  # a subscriber never breaks the operation (§5.6)
            log.exception("progress relay failed", extra={"operation": operation})

    def _discard(self, request_id: str) -> None:
        """Take back a request nobody answered, so it cannot be acted on later.

        The helper refuses anything older than ten minutes anyway; removing it
        closes the window in between, where a helper starting late would carry
        out an operation the operator has already been told failed.
        """
        try:
            (self.directory / f"{request_id}.json").unlink(missing_ok=True)
        except OSError as exc:
            log.warning(
                "could not withdraw a helper request",
                extra={"id": request_id, "error": str(exc)},
            )

    async def run(
        self,
        verb: str,
        *,
        settle: Settle = "done",
        timeout_s: float | None = None,
        **args: Any,
    ) -> HelperStatus:
        request_id = await self.submit(verb, **args)
        log.info("helper request", extra={"verb": verb, "id": request_id})
        return await self.wait(request_id, verb, settle=settle, timeout_s=timeout_s)

    async def restart_core(
        self,
        *,
        watchdog_window_s: int | None = None,
        settle: Settle = "running",
    ) -> HelperStatus:
        """Restart the application, optionally behind §4.7's widened window.

        ``settle="running"`` by default because this restart kills the process
        that asked for it: the helper's final status is written to a reader
        that no longer exists, so waiting for it would mean waiting out the
        timeout on every single restart.

        ``watchdog_window_s`` is how long the widened window stays open, not
        how wide it is. The helper refuses anything outside its own bounds and
        reads the drop-in's contents from the read-only root, so the worst a
        compromised application can ask for is a window it was going to get
        anyway.
        """
        timeout_s = None
        if settle == "done" and watchdog_window_s:
            # The helper holds the window open before it reports done, so the
            # deadline has to clear it rather than expire inside it.
            timeout_s = self._default_timeout + watchdog_window_s
        return await self.run(
            "restart-core",
            settle=settle,
            timeout_s=timeout_s,
            watchdog_window_s=watchdog_window_s,
        )
