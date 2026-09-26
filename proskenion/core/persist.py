"""Dirty-flag state persister (spec §5.6, §15.13).

Writes selected state-store fields to the ``system_state`` table through
:func:`proskenion.db.crud.system_state.set_many`, one transaction per batch.

Which fields, and when, comes solely from each domain's field declarations
(:class:`~proskenion.core.state.FieldSpec`): a *continuous* field (levels,
fader positions, group multipliers, timer accumulated) is written at most
once per :data:`CONTINUOUS_INTERVAL_S` tick, in one transaction carrying every
continuous value that changed in that window, so active fader use cannot
saturate the WAL; a *static* field (external control toggle, device status,
timer running and started_at, banners) is written as soon as the write loop
gets the event loop. A field with no persistence class — ``mixer.meters``,
``lighting.observed``, anything a later phase leaves undeclared — is never
written: there is no code path here that takes a value from the store
without first consulting its declaration, and :meth:`StatePersister.persist_now`
refuses an undeclared field outright.

Values are stored as JSON text. A map field is one row holding the whole
map; the field's *current* value is serialised at write time, so a burst of
fifty changes yields one row carrying the last.

:meth:`StatePersister.flush` writes everything pending in one transaction —
step 3 of graceful shutdown (§12.4); :meth:`StatePersister.stop` calls it.
A failed write is logged and its keys re-queued, never raised into the store.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from proskenion.core.state import Change, PersistClass, StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import system_state

log = logging.getLogger(__name__)

#: Continuous fields are written at most once per this interval (§15.13).
CONTINUOUS_INTERVAL_S = 0.5
DEFAULT_SOURCE = "persister"

Sleeper = Callable[[float], Awaitable[None]]
Pending = dict[str, set[str]]


class NotPersistableError(ValueError):
    """The field has no persistence class — it must never reach ``system_state``."""

    def __init__(self, domain: str, field: str) -> None:
        super().__init__(f"{domain}.{field} is not declared persistable")
        self.domain = domain
        self.field = field


class StatePersister:
    """Persists declared fields of ``state`` to ``db``. One per process."""

    def __init__(
        self,
        state: StateStore,
        db: Database,
        *,
        continuous_interval: float = CONTINUOUS_INTERVAL_S,
        source: str = DEFAULT_SOURCE,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        if continuous_interval <= 0:
            raise ValueError("continuous_interval must be positive")
        self._state = state
        self._db = db
        self._interval = continuous_interval
        self._source = source
        self._sleep = sleep
        self._pending: dict[PersistClass, Pending] = {"continuous": {}, "static": {}}
        self._static_wake = asyncio.Event()
        self._static_idle = False
        self._in_flight = 0
        self._settled = asyncio.Event()
        self._settled.set()
        self._tasks: list[asyncio.Task[None]] = []
        self._lock = asyncio.Lock()
        self.transactions = 0
        """Transactions committed — what a test or the health screen counts."""
        self.rows_written = 0
        state.add_listener(self._on_change)

    # -- change intake -----------------------------------------------------

    def _on_change(self, change: Change) -> None:
        cls = self._state.persistence_class(change.domain, change.field)
        if cls is None:
            return
        self._pending[cls].setdefault(change.domain, set()).add(change.field)
        if cls == "static":
            self._static_wake.set()
            self._settled.clear()

    def pending(self, cls: PersistClass) -> Pending:
        """A copy of the fields waiting to be written for one class."""
        return {domain: set(fields) for domain, fields in self._pending[cls].items()}

    async def settled(self) -> None:
        """Wait until no write is in flight and the static writer is idle.

        Idle means waiting for a change, or waiting out the retry delay after
        a failed write — in which case :meth:`pending` still holds the keys.
        Continuous keys waiting for their tick do not count: they are not
        late until the tick. For tests and orderly shutdown sequencing.
        """
        await self._settled.wait()

    def _refresh_settled(self) -> None:
        if self._static_idle and not self._static_wake.is_set() and self._in_flight == 0:
            self._settled.set()

    # -- lifecycle ---------------------------------------------------------

    @property
    def running(self) -> bool:
        return bool(self._tasks)

    async def start(self) -> None:
        """Start the continuous tick and the static writer. Idempotent."""
        if self._tasks:
            return
        loop = asyncio.get_running_loop()
        self._tasks = [
            loop.create_task(self._continuous_loop(), name="persister-continuous"),
            loop.create_task(self._static_loop(), name="persister-static"),
        ]

    async def stop(self) -> None:
        """Stop the loops and flush everything pending (§12.4 step 3)."""
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.flush()

    def close(self) -> None:
        """Detach from the store without flushing. For tests and error paths."""
        self._state.remove_listener(self._on_change)

    async def _continuous_loop(self) -> None:
        while True:
            await self._sleep(self._interval)
            await self.tick()

    async def _static_loop(self) -> None:
        while True:
            self._static_idle = True
            self._refresh_settled()
            await self._static_wake.wait()
            self._static_wake.clear()
            self._static_idle = False
            if not await self._write(self._take("static")):
                # The keys were re-queued; retry after a delay rather than
                # spinning against a database that is refusing writes.
                self._static_idle = True
                self._refresh_settled()
                await self._sleep(self._interval)
                self._static_wake.set()

    # -- writing -----------------------------------------------------------

    async def tick(self) -> None:
        """One continuous tick: write every continuous field that changed."""
        await self._write(self._take("continuous"))

    async def flush(self) -> None:
        """Write everything pending, both classes, in one transaction."""
        pending = self._take("static")
        for domain, fields in self._take("continuous").items():
            pending.setdefault(domain, set()).update(fields)
        await self._write(pending)

    async def persist_now(self, domain: str, field: str) -> None:
        """Write one field immediately, regardless of its class.

        Raises :class:`NotPersistableError` if the field is not declared
        persistable — ``mixer.meters`` and ``lighting.observed`` included.
        """
        if self._state.persistence_class(domain, field) is None:
            raise NotPersistableError(domain, field)
        await self._write({domain: {field}}, requeue_on_failure=False)

    def _take(self, cls: PersistClass) -> Pending:
        taken, self._pending[cls] = self._pending[cls], {}
        return taken

    def _requeue(self, pending: Pending) -> None:
        """Put taken keys back so the next tick or the flush writes them."""
        for domain_name, fields in pending.items():
            for field in fields:
                cls = self._state.persistence_class(domain_name, field)
                if cls is not None:
                    self._pending[cls].setdefault(domain_name, set()).add(field)

    async def _write(self, pending: Pending, *, requeue_on_failure: bool = True) -> bool:
        """Write ``pending`` in one transaction. False if the write failed."""
        if not pending:
            return True
        rows: list[tuple[str, str, str]] = []
        for domain_name, fields in pending.items():
            domain = self._state.domain(domain_name)
            for field in sorted(fields):
                # The declaration is the gate: an undeclared field never
                # becomes a row, whatever put it in ``pending``.
                if domain.persistence_class(field) is None:
                    raise NotPersistableError(domain_name, field)
                rows.append((domain_name, field, json.dumps(domain.get(field))))
        self._in_flight += 1
        try:
            async with self._lock:
                written = await system_state.set_many(self._db, rows, source=self._source)
        except asyncio.CancelledError:
            # stop() cancels the writer tasks and then flushes. A write cancelled
            # in flight has already taken its keys, and its transaction rolls
            # back; put them back so the flush writes them, or the last change
            # before shutdown is lost (§12.4 step 3). Writing a value twice is
            # harmless, so this holds even if the commit landed first.
            self._requeue(pending)
            raise
        except Exception:
            log.exception(
                "state persistence failed; keys re-queued",
                extra={"rows": len(rows), "requeued": requeue_on_failure},
            )
            if requeue_on_failure:
                self._requeue(pending)
            return False
        finally:
            self._in_flight -= 1
            self._refresh_settled()
        self.transactions += 1
        self.rows_written += written
        return True
