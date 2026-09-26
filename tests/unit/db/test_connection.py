"""Connection model (§15.1): write serialisation, pragmas, pool, chunked deletes."""

import asyncio
import sqlite3
from pathlib import Path

import pytest

from proskenion.db.connection import (
    MEMORY,
    Database,
    DatabaseNotOpenError,
    validate_identifier,
)


async def _count(db: Database, table: str, where: str = "1 = 1") -> int:
    async with db.read() as conn:
        cursor = await conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}")
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


# -- pragmas and lifecycle ---------------------------------------------------


async def test_every_connection_has_foreign_keys_and_busy_timeout(db: Database) -> None:
    async with db.write() as conn:
        fk = await (await conn.execute("PRAGMA foreign_keys")).fetchone()
        bt = await (await conn.execute("PRAGMA busy_timeout")).fetchone()
        assert fk is not None and bt is not None
        assert (int(fk[0]), int(bt[0])) == (1, 5000)
    seen: list[tuple[int, int]] = []
    for _ in range(db.read_pool_size):
        async with db.read() as conn:
            fk = await (await conn.execute("PRAGMA foreign_keys")).fetchone()
            bt = await (await conn.execute("PRAGMA busy_timeout")).fetchone()
            assert fk is not None and bt is not None
            seen.append((int(fk[0]), int(bt[0])))
    assert seen == [(1, 5000)] * db.read_pool_size


async def test_file_database_uses_wal_and_synchronous_normal(tmp_path: Path) -> None:
    db = Database()
    await db.open(tmp_path / "test.db")
    try:
        async with db.write() as conn:
            journal = await (await conn.execute("PRAGMA journal_mode")).fetchone()
            sync = await (await conn.execute("PRAGMA synchronous")).fetchone()
            assert journal is not None and str(journal[0]).lower() == "wal"
            assert sync is not None and int(sync[0]) == 1  # NORMAL
            await conn.execute("CREATE TABLE t (a INTEGER)")
            await conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(100)])
        busy, log_frames, checkpointed = await db.checkpoint(truncate=True)
        assert busy == 0
        assert log_frames == checkpointed
        assert await _count(db, "t") == 100
    finally:
        await db.close()


async def test_read_pool_size_is_bounded() -> None:
    with pytest.raises(ValueError):
        Database(read_pool_size=2)
    with pytest.raises(ValueError):
        Database(read_pool_size=5)
    assert Database(read_pool_size=4).read_pool_size == 4


async def test_units_require_an_open_database() -> None:
    db = Database()
    with pytest.raises(DatabaseNotOpenError):
        async with db.write():
            pass
    with pytest.raises(DatabaseNotOpenError):
        async with db.read():
            pass
    await db.close()  # safe when never opened


async def test_in_memory_databases_are_private_to_each_instance() -> None:
    a, b = Database(), Database()
    await a.open(MEMORY)
    await b.open(MEMORY)
    try:
        async with a.write() as conn:
            await conn.execute("CREATE TABLE only_in_a (x INTEGER)")
        async with b.read() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = 'only_in_a'"
            )
            assert tuple(await cursor.fetchone() or ()) == (0,)
    finally:
        await a.close()
        await b.close()


async def test_read_pool_hands_out_distinct_connections_and_waits_when_exhausted(
    db: Database,
) -> None:
    held = []
    contexts = [db.read() for _ in range(db.read_pool_size)]
    for ctx in contexts:
        held.append(await ctx.__aenter__())
    assert len({id(c) for c in held}) == db.read_pool_size

    fourth_started = asyncio.Event()

    async def fourth() -> None:
        async with db.read():
            fourth_started.set()

    task = asyncio.create_task(fourth())
    await asyncio.sleep(0.01)
    assert not fourth_started.is_set(), "a reader ran while the pool was exhausted"
    for ctx in contexts:
        await ctx.__aexit__(None, None, None)
    await asyncio.wait_for(task, 1)
    assert fourth_started.is_set()


# -- write serialisation (§22.2) ---------------------------------------------


async def test_write_units_cannot_interleave(db: Database) -> None:
    """The second unit's BEGIN happens only after the first's COMMIT."""
    log: list[str] = []
    first_inside = asyncio.Event()

    async def first() -> None:
        async with db.write() as conn:
            log.append("A:begin")
            first_inside.set()
            await conn.execute("INSERT INTO lighting_bars (name, sort_order) VALUES ('A', 1)")
            for _ in range(10):  # give the second unit every chance to interleave
                await asyncio.sleep(0)
            log.append("A:commit")

    async def second() -> None:
        await first_inside.wait()
        log.append("B:waiting")
        async with db.write() as conn:
            log.append("B:begin")
            cursor = await conn.execute("SELECT COUNT(*) FROM lighting_bars WHERE name = 'A'")
            assert tuple(await cursor.fetchone() or ()) == (1,), "A's commit must be visible to B"
            await conn.execute("INSERT INTO lighting_bars (name, sort_order) VALUES ('B', 2)")
            log.append("B:commit")

    await asyncio.gather(first(), second())
    assert log == ["A:begin", "B:waiting", "A:commit", "B:begin", "B:commit"]
    assert await _count(db, "lighting_bars", "name IN ('A', 'B')") == 2


async def test_statement_order_on_the_write_connection(db: Database) -> None:
    """Trace the actual SQL: no BEGIN appears between another unit's BEGIN and COMMIT."""
    statements: list[str] = []
    async with db.write() as conn:
        await conn.set_trace_callback(statements.append)
    statements.clear()  # drop the COMMIT of the unit that installed the callback
    try:
        gate = asyncio.Event()

        async def unit(name: str, wait: bool) -> None:
            if wait:
                await gate.wait()
            async with db.write() as conn:
                gate.set()
                await conn.execute(
                    "INSERT INTO lighting_bars (name, sort_order) VALUES (?, 9)", (name,)
                )
                await asyncio.sleep(0)
                await asyncio.sleep(0)

        await asyncio.gather(unit("one", False), unit("two", True))
    finally:
        async with db.write() as conn:
            await conn.set_trace_callback(None)

    control = [s for s in statements if s.startswith(("BEGIN", "COMMIT", "ROLLBACK"))]
    assert control[:4] == ["BEGIN IMMEDIATE", "COMMIT", "BEGIN IMMEDIATE", "COMMIT"]


async def test_rollback_in_one_unit_does_not_discard_the_other(db: Database) -> None:
    first_inside = asyncio.Event()

    async def committer() -> None:
        async with db.write() as conn:
            first_inside.set()
            await conn.execute("INSERT INTO lighting_bars (name, sort_order) VALUES ('kept', 1)")
            for _ in range(10):
                await asyncio.sleep(0)

    async def failer() -> None:
        await first_inside.wait()
        with pytest.raises(RuntimeError, match="deliberate"):
            async with db.write() as conn:
                await conn.execute(
                    "INSERT INTO lighting_bars (name, sort_order) VALUES ('discarded', 2)"
                )
                raise RuntimeError("deliberate")

    await asyncio.gather(committer(), failer())
    assert await _count(db, "lighting_bars", "name = 'kept'") == 1
    assert await _count(db, "lighting_bars", "name = 'discarded'") == 0

    # And the connection is clean afterwards: the next unit works normally.
    async with db.write() as conn:
        assert conn.in_transaction
        await conn.execute("INSERT INTO lighting_bars (name, sort_order) VALUES ('after', 3)")
    assert await _count(db, "lighting_bars", "name = 'after'") == 1


@pytest.mark.parametrize("unit", ["write", "write_no_fk_enforcement"])
async def test_unit_cancelled_while_begin_is_in_flight_leaves_the_connection_clean(
    db: Database, unit: str
) -> None:
    # A cancelled await does not stop aiosqlite running the statement on its
    # thread, so the BEGIN still opens a transaction. It must be rolled back,
    # or the next unit fails "cannot start a transaction within a transaction"
    # — which is how a writer cancelled at shutdown lost the flush after it.
    async def cancelled_unit() -> None:
        async with getattr(db, unit)():
            await asyncio.sleep(10)

    for attempt in range(20):  # the cancel lands at a different point each time
        task = asyncio.create_task(cancelled_unit())
        for _ in range(attempt % 4):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        async with db.write() as conn:
            await conn.execute("INSERT INTO lighting_bars (name, sort_order) VALUES ('ok', 1)")
    assert await _count(db, "lighting_bars", "name = 'ok'") == 20


async def test_integrity_error_rolls_back_the_unit(db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        async with db.write() as conn:
            await conn.execute("INSERT INTO lighting_bars (name, sort_order) VALUES ('x', 1)")
            await conn.execute(
                "INSERT INTO users (tier, password, updated_at) VALUES ('admin', 'h', 't')"
            )
    assert await _count(db, "lighting_bars", "name = 'x'") == 0


# -- write_no_fk_enforcement (§15.2, §15.10 table rebuilds) -------------------


async def test_write_no_fk_enforcement_disables_and_restores(db: Database) -> None:
    async with db.write_no_fk_enforcement() as conn:
        assert conn.in_transaction
        fk = await (await conn.execute("PRAGMA foreign_keys")).fetchone()
        assert fk is not None and int(fk[0]) == 0
    async with db.write() as conn:
        fk = await (await conn.execute("PRAGMA foreign_keys")).fetchone()
        assert fk is not None and int(fk[0]) == 1, "enforcement must be back on after commit"


async def test_write_no_fk_enforcement_restores_on_rollback(db: Database) -> None:
    with pytest.raises(RuntimeError, match="deliberate"):
        async with db.write_no_fk_enforcement() as conn:
            await conn.execute("INSERT INTO lighting_bars (name, sort_order) VALUES ('x', 1)")
            raise RuntimeError("deliberate")
    async with db.write() as conn:
        fk = await (await conn.execute("PRAGMA foreign_keys")).fetchone()
        assert fk is not None and int(fk[0]) == 1, "enforcement must be back on after rollback"
    assert await _count(db, "lighting_bars", "name = 'x'") == 0


async def test_write_no_fk_enforcement_permits_what_write_refuses(db: Database) -> None:
    """The point of the toggle: an insert that ``write()`` refuses succeeds
    here, and ``PRAGMA foreign_key_check`` — not insert-time enforcement — is
    what a caller must use to catch it (§15.2's table-rebuild procedure)."""
    with pytest.raises(sqlite3.IntegrityError):
        async with db.write() as conn:
            await conn.execute(
                "INSERT INTO matrix_inputs "
                "(device_id, driver_ref, name, sort_order, created_at, updated_at) "
                "VALUES (999999, '1', 'Dangling', 0, 'x', 'x')"
            )
    async with db.write_no_fk_enforcement() as conn:
        await conn.execute(
            "INSERT INTO matrix_inputs "
            "(device_id, driver_ref, name, sort_order, created_at, updated_at) "
            "VALUES (999999, '1', 'Dangling', 0, 'x', 'x')"
        )
        violations = await (await conn.execute("PRAGMA foreign_key_check")).fetchall()
        assert len(violations) == 1


# -- chunked pruning (§15.1, §22.2) ------------------------------------------


async def test_chunked_delete_yields_the_write_lock_between_batches(db: Database) -> None:
    async with db.write() as conn:
        await conn.executemany(
            "INSERT INTO security_events (timestamp, event_type) VALUES (?, 'login')",
            [(f"2026-01-01T00:00:{i:02d}+13:00",) for i in range(2000)],
        )
    assert await _count(db, "security_events") == 2000

    observed: list[int] = []

    async def other_writer() -> None:
        # Keeps taking write units until the prune is done; every count it sees
        # was taken while holding the lock, so a value strictly between 0 and
        # 2000 proves it ran between two batches.
        for _ in range(10_000):
            async with db.write() as conn:
                cursor = await conn.execute("SELECT COUNT(*) FROM security_events")
                row = await cursor.fetchone()
                assert row is not None
                observed.append(int(row[0]))
                await conn.execute(
                    "INSERT INTO lighting_bars (name, sort_order) VALUES ('tick', 5)"
                )
            if observed[-1] == 0:
                return
            await asyncio.sleep(0)
        raise AssertionError("prune never finished")

    deleted, _ = await asyncio.gather(
        db.chunked_delete("security_events", "event_type = ?", ("login",), batch=500),
        other_writer(),
    )
    assert deleted == 2000
    assert await _count(db, "security_events") == 0
    between = [n for n in observed if 0 < n < 2000]
    assert between, f"other writer never ran between batches: {observed}"
    assert all(n % 500 == 0 for n in observed), observed


async def test_chunked_delete_respects_the_where_clause(db: Database) -> None:
    async with db.write() as conn:
        await conn.executemany(
            "INSERT INTO security_events (timestamp, event_type) VALUES (?, ?)",
            [("2026-01-01T00:00:00+13:00", "old")] * 7 + [("2026-02-01T00:00:00+13:00", "new")] * 3,
        )
    deleted = await db.chunked_delete(
        "security_events", "timestamp < ?", ("2026-01-15T00:00:00+13:00",), batch=3
    )
    assert deleted == 7
    assert await _count(db, "security_events", "event_type = 'new'") == 3
    assert await db.chunked_delete("security_events", "event_type = 'nothing'") == 0


def test_validate_identifier_rejects_injection() -> None:
    assert validate_identifier("security_events") == "security_events"
    for bad in ("users; DROP TABLE users", "a b", "1abc", "", "x-y"):
        with pytest.raises(ValueError):
            validate_identifier(bad)
