"""SQLite connection model (spec §15.1).

SQLite permits one writer at a time, WAL included. aiosqlite serialises the
statements queued to a single connection, but it does *not* stop two coroutines
interleaving ``BEGIN``/``COMMIT`` on that connection — and when they do, one
coroutine's rollback discards the other's work. The model here is therefore:

* **Exactly one write connection**, guarded by an :class:`asyncio.Lock` that is
  held for the whole of each transactional unit (``async with db.write()``),
  never per statement. Everything that writes goes through it: the state
  persister, the scene execution log, security events, API handlers, pruning
  and the migration runner. Nothing may open its own write connection.
* **A pool of 3–4 read connections** (``async with db.read()``). Under WAL,
  readers never block the writer and the writer never blocks readers.
* Per connection: ``PRAGMA foreign_keys = ON`` and ``PRAGMA busy_timeout = 5000``.
* Once at open: ``journal_mode = WAL`` and ``synchronous = NORMAL`` (B30).

Transactions are explicit. Connections are opened with ``autocommit=True`` so
that Python's ``sqlite3`` performs no implicit transaction control of its own —
in particular ``executescript`` (used by the migration runner) then runs inside
the transaction the unit opened rather than committing it first.

``async with db.write_no_fk_enforcement()`` is the one exception: it toggles
``PRAGMA foreign_keys`` off before its ``BEGIN`` and back on after its
``COMMIT``/``ROLLBACK``, for a migration that rebuilds a table to add or
remove a foreign key and must follow SQLite's documented twelve-step
procedure, whose first step cannot run once inside a transaction (§15.2).

In-memory databases
-------------------
``Database.open(":memory:")`` opens a private, named, shared-cache in-memory
database (``file:<name>?mode=memory&cache=shared``) so that the writer and the
read pool share one database through the same code path as production. Two
consequences worth knowing:

* SQLite has no WAL for in-memory databases; ``journal_mode`` reports
  ``memory``. Readers in shared-cache mode take table locks instead of WAL
  snapshots, so a reader would fail with "database table is locked" while a
  write unit is open. Read connections on an in-memory database therefore set
  ``PRAGMA read_uncommitted = ON``. Tests may observe uncommitted writes from a
  concurrently open unit; production never does.
* The database lives as long as at least one connection to it is open. The
  write connection is opened first and closed last.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

MEMORY = ":memory:"
"""Pass to :meth:`Database.open` for a private in-memory database."""

BUSY_TIMEOUT_MS = 5000
DEFAULT_READ_POOL_SIZE = 3
MIN_READ_POOL_SIZE = 3
MAX_READ_POOL_SIZE = 4
DEFAULT_DELETE_BATCH = 500

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class DatabaseNotOpenError(RuntimeError):
    """Raised when a unit is requested before :meth:`Database.open` or after close."""


def validate_identifier(name: str) -> str:
    """Return ``name`` if it is a plain SQL identifier, else raise ``ValueError``.

    Table and column names in this package come from code, never from users,
    but every helper that interpolates one into SQL still checks it.
    """
    if not _IDENTIFIER.match(name):
        raise ValueError(f"not a plain SQL identifier: {name!r}")
    return name


class Database:
    """One write connection behind a lock, plus a small read pool (§15.1)."""

    def __init__(self, *, read_pool_size: int = DEFAULT_READ_POOL_SIZE) -> None:
        if not MIN_READ_POOL_SIZE <= read_pool_size <= MAX_READ_POOL_SIZE:
            raise ValueError(
                f"read_pool_size must be {MIN_READ_POOL_SIZE}–{MAX_READ_POOL_SIZE}, "
                f"got {read_pool_size}"
            )
        self._read_pool_size = read_pool_size
        self._write_lock = asyncio.Lock()
        self._write: aiosqlite.Connection | None = None
        self._readers: list[aiosqlite.Connection] = []
        self._pool: asyncio.Queue[aiosqlite.Connection] | None = None
        self._database = ""
        self._uri = False
        self._in_memory = False

    # -- lifecycle ---------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._write is not None

    @property
    def in_memory(self) -> bool:
        return self._in_memory

    @property
    def read_pool_size(self) -> int:
        return self._read_pool_size

    @property
    def location(self) -> tuple[str, bool]:
        """What :meth:`open` connected to, and whether it is a URI.

        For a short-lived read-only connection that must not share a pooled
        one: ``VACUUM INTO`` refuses to run on a connection with any statement
        still in progress, which a pooled reader cannot promise (§15.3's
        pre-change snapshots, :mod:`proskenion.core.snapshots`).
        """
        return self._database, self._uri

    async def open(self, path: str | Path) -> None:
        """Open the write connection and the read pool.

        ``path`` is a filesystem path, or :data:`MEMORY` for a private
        in-memory database (see the module docstring).
        """
        if self._write is not None:
            raise RuntimeError("database is already open")
        if str(path) == MEMORY:
            self._in_memory = True
            self._uri = True
            self._database = f"file:proskenion-{uuid.uuid4().hex}?mode=memory&cache=shared"
        else:
            self._in_memory = False
            self._uri = False
            self._database = str(path)

        try:
            writer = await self._connect()
            # Database-wide settings, applied once through the only writer.
            # journal_mode persists in the file; synchronous is a per-connection
            # setting, but only this connection ever writes.
            await writer.execute("PRAGMA journal_mode = WAL")
            await writer.execute("PRAGMA synchronous = NORMAL")
            self._write = writer

            pool: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue()
            for _ in range(self._read_pool_size):
                reader = await self._connect()
                if self._in_memory:
                    await reader.execute("PRAGMA read_uncommitted = ON")
                self._readers.append(reader)
                pool.put_nowait(reader)
            self._pool = pool
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        """Close every connection. Safe to call on a database that is not open."""
        readers, self._readers = self._readers, []
        self._pool = None
        for reader in readers:
            await reader.close()
        writer, self._write = self._write, None
        if writer is not None:
            await writer.close()

    async def _connect(self) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(self._database, uri=self._uri, autocommit=True)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        return conn

    def _require_writer(self) -> aiosqlite.Connection:
        if self._write is None:
            raise DatabaseNotOpenError("database is not open")
        return self._write

    def _require_pool(self) -> asyncio.Queue[aiosqlite.Connection]:
        if self._pool is None:
            raise DatabaseNotOpenError("database is not open")
        return self._pool

    # -- units -------------------------------------------------------------

    @asynccontextmanager
    async def write(self) -> AsyncIterator[aiosqlite.Connection]:
        """One transactional unit on the single write connection.

        Acquires the write lock, runs ``BEGIN IMMEDIATE``, yields the
        connection, and ``COMMIT``s on normal exit or ``ROLLBACK``s if the body
        raises. The lock is held until the transaction has ended, so two units
        can never interleave. The body must not issue its own ``BEGIN``,
        ``COMMIT`` or ``ROLLBACK``.
        """
        conn = self._require_writer()
        async with self._write_lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                yield conn
            except BaseException:
                await _rollback(conn)
                raise
            else:
                if conn.in_transaction:
                    await conn.execute("COMMIT")

    @asynccontextmanager
    async def write_no_fk_enforcement(self) -> AsyncIterator[aiosqlite.Connection]:
        """One transactional unit with foreign key enforcement disabled around it.

        For a migration that rebuilds a table to add or remove a foreign key
        (SQLite has no ``ALTER TABLE ... ADD CONSTRAINT``) and must follow
        SQLite's documented twelve-step procedure
        (https://www.sqlite.org/lang_altertable.html, "Making Other Kinds Of
        Table Schema Changes"), whose first step — ``PRAGMA foreign_keys =
        OFF`` — has to run *before* ``BEGIN``. That pragma is a documented
        no-op once a transaction is open, so it cannot be issued from inside
        the script :meth:`write` runs: that script only starts executing
        after :meth:`write` has already issued ``BEGIN IMMEDIATE``, by which
        point the toggle would silently do nothing and enforcement would stay
        on throughout.

        This method issues the toggle first, then ``BEGIN IMMEDIATE``, then
        yields, then ``COMMIT``s or ``ROLLBACK``s exactly as :meth:`write`
        does, then restores enforcement to on — all four steps under the same
        held write lock, so no other unit ever runs with enforcement off. The
        caller is responsible for the procedure's own remaining steps: it
        must run ``PRAGMA foreign_key_check`` before the block ends and raise
        if it finds anything, so that a rebuild which broke a constraint rolls
        back instead of committing (steps 10–11); this method does not run
        that check itself; not every no-enforcement operation is a rebuild.
        """
        conn = self._require_writer()
        async with self._write_lock:
            await conn.execute("PRAGMA foreign_keys = OFF")
            try:
                try:
                    await conn.execute("BEGIN IMMEDIATE")
                    yield conn
                except BaseException:
                    await _rollback(conn)
                    raise
                else:
                    if conn.in_transaction:
                        await conn.execute("COMMIT")
            finally:
                await conn.execute("PRAGMA foreign_keys = ON")

    @asynccontextmanager
    async def read(self) -> AsyncIterator[aiosqlite.Connection]:
        """Borrow a connection from the read pool for the duration of the block."""
        pool = self._require_pool()
        conn = await pool.get()
        try:
            yield conn
        finally:
            pool.put_nowait(conn)

    # -- maintenance -------------------------------------------------------

    async def checkpoint(self, *, truncate: bool = False) -> tuple[int, int, int]:
        """Run a WAL checkpoint on the write connection.

        ``truncate=True`` is what the nightly backup job runs while the system
        is quiet, before taking the archive (§15.1). Returns SQLite's
        ``(busy, log_frames, checkpointed_frames)`` triple.
        """
        conn = self._require_writer()
        mode = "TRUNCATE" if truncate else "PASSIVE"
        async with self._write_lock:
            cursor = await conn.execute(f"PRAGMA wal_checkpoint({mode})")
            row = await cursor.fetchone()
        if row is None:  # pragma: no cover - SQLite always returns one row
            return (0, 0, 0)
        return (int(row[0]), int(row[1]), int(row[2]))

    async def chunked_delete(
        self,
        table: str,
        where_sql: str,
        params: Sequence[Any] = (),
        *,
        batch: int = DEFAULT_DELETE_BATCH,
    ) -> int:
        """Delete matching rows in bounded batches by rowid range (§15.1).

        Each batch is its own write unit, so the write lock is released between
        batches and control traffic interleaves with a long prune instead of
        stalling behind it. ``where_sql`` is a trusted SQL fragment written by
        the caller; ``params`` are its bound parameters. Returns the number of
        rows deleted.
        """
        validate_identifier(table)
        if batch < 1:
            raise ValueError("batch must be at least 1")
        bound = list(params)
        upper_sql = (
            f"SELECT MAX(rowid) FROM "
            f"(SELECT rowid FROM {table} WHERE ({where_sql}) ORDER BY rowid LIMIT ?)"
        )
        delete_sql = f"DELETE FROM {table} WHERE rowid <= ? AND ({where_sql})"
        deleted = 0
        while True:
            async with self.write() as conn:
                cursor = await conn.execute(upper_sql, [*bound, batch])
                row = await cursor.fetchone()
                upper = row[0] if row is not None else None
                if upper is None:
                    return deleted
                cursor = await conn.execute(delete_sql, [upper, *bound])
                deleted += cursor.rowcount
            # The lock is fair, but yield explicitly so a waiter always runs first.
            await asyncio.sleep(0)


async def _rollback(conn: aiosqlite.Connection) -> None:
    """End whatever transaction a failed or cancelled unit left open.

    A cancelled ``await`` does not stop the statement: aiosqlite still runs it
    on its thread. So a unit cancelled while ``BEGIN IMMEDIATE`` is in flight
    opens a transaction nobody ends, and ``conn.in_transaction`` read from here
    can still say False because the thread has not got to it yet. The next
    unit's ``BEGIN`` then fails with "cannot start a transaction within a
    transaction" — which lost the shutdown flush of state (§12.4 step 3).
    aiosqlite runs statements in order, so a ``ROLLBACK`` queued now runs
    after the ``BEGIN`` and before the next unit's; it is shielded so a second
    cancellation cannot stop it being queued. Where there was nothing to undo
    (``BEGIN`` itself failed, or SQLITE_FULL and I/O errors that roll back on
    their own), SQLite says so and that is ignored.
    """
    try:
        await asyncio.shield(conn.execute("ROLLBACK"))
    except sqlite3.OperationalError as exc:
        if "no transaction is active" not in str(exc):
            raise
