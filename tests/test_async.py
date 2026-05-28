"""Tests for AsyncDatabase and AsyncCursor."""
import asyncio
import pytest
from hyperion import AsyncDatabase
from hyperion.errors import (
    NoSuchTableError, TransactionError,
)
from hyperion.executor import ReadOnlyError, TooManyRowsError


def run(coro):
    return asyncio.run(coro)


# ── Basics ────────────────────────────────────────────────────────────────────

class TestAsyncBasics:
    def test_execute_fetchall(self):
        async def go():
            db = AsyncDatabase(":memory:")
            await db.execute("CREATE TABLE t (id INTEGER, name TEXT)")
            await db.execute("INSERT INTO t VALUES (1, 'alice')")
            await db.execute("INSERT INTO t VALUES (2, 'bob')")
            cur = await db.execute("SELECT * FROM t ORDER BY id")
            rows = await cur.fetchall()
            assert len(rows) == 2
            assert rows[0]["name"] == "alice"
            assert rows[1]["name"] == "bob"
            await db.close()
        run(go())

    def test_execute_fetchone(self):
        async def go():
            db = AsyncDatabase(":memory:")
            await db.execute("CREATE TABLE t (id INTEGER)")
            await db.execute("INSERT INTO t VALUES (42)")
            cur = await db.execute("SELECT id FROM t")
            row = await cur.fetchone()
            assert row["id"] == 42
            assert await cur.fetchone() is None
            await db.close()
        run(go())

    def test_execute_fetchmany(self):
        async def go():
            db = AsyncDatabase(":memory:")
            await db.execute("CREATE TABLE t (n INTEGER)")
            for i in range(5):
                await db.execute(f"INSERT INTO t VALUES ({i})")
            cur = await db.execute("SELECT n FROM t ORDER BY n")
            batch = await cur.fetchmany(3)
            assert [r["n"] for r in batch] == [0, 1, 2]
            rest = await cur.fetchmany(10)
            assert [r["n"] for r in rest] == [3, 4]
            await db.close()
        run(go())

    def test_cursor_description(self):
        async def go():
            db = AsyncDatabase(":memory:")
            await db.execute("CREATE TABLE t (id INTEGER, name TEXT)")
            await db.execute("INSERT INTO t VALUES (1, 'x')")
            cur = await db.execute("SELECT id, name FROM t")
            assert cur.description is not None
            col_names = [d[0] for d in cur.description]
            assert col_names == ["id", "name"]
            await db.close()
        run(go())

    def test_cursor_rowcount(self):
        async def go():
            db = AsyncDatabase(":memory:")
            await db.execute("CREATE TABLE t (id INTEGER, v TEXT)")
            cur = await db.execute("INSERT INTO t VALUES (1, 'y')")
            assert cur.rowcount == 1
            await db.close()
        run(go())

    def test_lastrowid(self):
        async def go():
            db = AsyncDatabase(":memory:")
            await db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, v TEXT)")
            cur = await db.execute("INSERT INTO t (v) VALUES ('x')")
            assert cur.lastrowid == 1
            cur2 = await db.execute("INSERT INTO t (v) VALUES ('y')")
            assert cur2.lastrowid == 2
            await db.close()
        run(go())


# ── Async iteration ───────────────────────────────────────────────────────────

class TestAsyncIteration:
    def test_async_for_cursor(self):
        async def go():
            db = AsyncDatabase(":memory:")
            await db.execute("CREATE TABLE t (n INTEGER)")
            for i in range(4):
                await db.execute(f"INSERT INTO t VALUES ({i})")
            cur = await db.execute("SELECT n FROM t ORDER BY n")
            results = []
            async for row in cur:
                results.append(row["n"])
            assert results == [0, 1, 2, 3]
            await db.close()
        run(go())


# ── Transactions ──────────────────────────────────────────────────────────────

class TestAsyncTransactions:
    def test_begin_commit(self):
        async def go():
            db = AsyncDatabase(":memory:")
            await db.execute("CREATE TABLE t (id INTEGER)")
            await db.begin()
            assert db.in_transaction
            await db.execute("INSERT INTO t VALUES (1)")
            await db.commit()
            assert not db.in_transaction
            cur = await db.execute("SELECT COUNT(*) AS n FROM t")
            row = await cur.fetchone()
            assert row["n"] == 1
            await db.close()
        run(go())

    def test_begin_rollback(self):
        async def go():
            db = AsyncDatabase(":memory:")
            await db.execute("CREATE TABLE t (id INTEGER)")
            await db.begin()
            await db.execute("INSERT INTO t VALUES (1)")
            await db.rollback()
            assert not db.in_transaction
            cur = await db.execute("SELECT COUNT(*) AS n FROM t")
            row = await cur.fetchone()
            assert row["n"] == 0
            await db.close()
        run(go())

    def test_double_begin_raises(self):
        async def go():
            db = AsyncDatabase(":memory:")
            await db.begin()
            with pytest.raises(TransactionError):
                await db.begin()
            await db.rollback()
            await db.close()
        run(go())

    def test_commit_without_begin_raises(self):
        async def go():
            db = AsyncDatabase(":memory:")
            with pytest.raises(TransactionError):
                await db.commit()
            await db.close()
        run(go())


# ── Async context manager ─────────────────────────────────────────────────────

class TestAsyncContextManager:
    def test_async_with_commits(self):
        async def go():
            db = AsyncDatabase(":memory:")
            await db.execute("CREATE TABLE t (id INTEGER)")
            async with db:
                await db.execute("INSERT INTO t VALUES (1)")
            assert not db.in_transaction
            cur = await db.execute("SELECT COUNT(*) AS n FROM t")
            row = await cur.fetchone()
            assert row["n"] == 1
            await db.close()
        run(go())

    def test_async_with_rollback_on_error(self):
        async def go():
            db = AsyncDatabase(":memory:")
            await db.execute("CREATE TABLE t (id INTEGER)")
            try:
                async with db:
                    await db.execute("INSERT INTO t VALUES (1)")
                    raise ValueError("intentional")
            except ValueError:
                pass
            assert not db.in_transaction
            cur = await db.execute("SELECT COUNT(*) AS n FROM t")
            row = await cur.fetchone()
            assert row["n"] == 0
            await db.close()
        run(go())


# ── Savepoints ────────────────────────────────────────────────────────────────

class TestAsyncSavepoints:
    def test_savepoint_rollback(self):
        async def go():
            db = AsyncDatabase(":memory:")
            await db.execute("CREATE TABLE t (id INTEGER)")
            await db.begin()
            await db.execute("INSERT INTO t VALUES (1)")
            await db.savepoint("sp")
            await db.execute("INSERT INTO t VALUES (2)")
            await db.rollback_to_savepoint("sp")
            await db.commit()
            cur = await db.execute("SELECT id FROM t")
            rows = await cur.fetchall()
            assert [r["id"] for r in rows] == [1]
            await db.close()
        run(go())

    def test_savepoint_release(self):
        async def go():
            db = AsyncDatabase(":memory:")
            await db.execute("CREATE TABLE t (id INTEGER)")
            await db.begin()
            await db.execute("INSERT INTO t VALUES (1)")
            await db.savepoint("sp")
            await db.execute("INSERT INTO t VALUES (2)")
            await db.release_savepoint("sp")
            await db.commit()
            cur = await db.execute("SELECT id FROM t ORDER BY id")
            rows = await cur.fetchall()
            assert [r["id"] for r in rows] == [1, 2]
            await db.close()
        run(go())


# ── Read-only ─────────────────────────────────────────────────────────────────

class TestAsyncReadOnly:
    def test_readonly_constructor_blocks_writes(self):
        async def go():
            db = AsyncDatabase(":memory:", readonly=True)
            with pytest.raises(ReadOnlyError):
                await db.execute("CREATE TABLE t (id INTEGER)")
            await db.close()
        run(go())

    def test_as_readonly_context_manager(self):
        async def go():
            db = AsyncDatabase(":memory:")
            await db.execute("CREATE TABLE t (id INTEGER)")
            async with db.as_readonly():
                with pytest.raises(ReadOnlyError):
                    await db.execute("INSERT INTO t VALUES (1)")
            # writes allowed again after the block
            await db.execute("INSERT INTO t VALUES (2)")
            cur = await db.execute("SELECT COUNT(*) AS n FROM t")
            row = await cur.fetchone()
            assert row["n"] == 1
            await db.close()
        run(go())

    def test_as_readonly_restores_on_exception(self):
        async def go():
            db = AsyncDatabase(":memory:")
            await db.execute("CREATE TABLE t (id INTEGER)")
            try:
                async with db.as_readonly():
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
            assert not db.readonly
            await db.execute("INSERT INTO t VALUES (1)")
            await db.close()
        run(go())


# ── Max rows ──────────────────────────────────────────────────────────────────

class TestAsyncMaxRows:
    def test_per_query_max_rows(self):
        async def go():
            db = AsyncDatabase(":memory:")
            await db.execute("CREATE TABLE t (n INTEGER)")
            for i in range(5):
                await db.execute(f"INSERT INTO t VALUES ({i})")
            cur = await db.execute("SELECT * FROM t", max_rows=3)
            with pytest.raises(TooManyRowsError):
                await cur.fetchall()
            await db.close()
        run(go())

    def test_connection_max_rows(self):
        async def go():
            db = AsyncDatabase(":memory:")
            db.max_rows = 2
            await db.execute("CREATE TABLE t (n INTEGER)")
            for i in range(5):
                await db.execute(f"INSERT INTO t VALUES ({i})")
            cur = await db.execute("SELECT * FROM t")
            with pytest.raises(TooManyRowsError):
                await cur.fetchall()
            await db.close()
        run(go())


# ── Error propagation ─────────────────────────────────────────────────────────

class TestAsyncErrorPropagation:
    def test_no_such_table_surfaces(self):
        async def go():
            db = AsyncDatabase(":memory:")
            with pytest.raises(NoSuchTableError):
                await db.execute("SELECT * FROM ghost")
            await db.close()
        run(go())

    def test_parse_error_surfaces(self):
        async def go():
            from hyperion.errors import ParseError
            db = AsyncDatabase(":memory:")
            with pytest.raises(ParseError):
                await db.execute("CREATE TABLE x (id NOTATYPE)")
            await db.close()
        run(go())


# ── Concurrency ───────────────────────────────────────────────────────────────

class TestAsyncConcurrency:
    def test_concurrent_inserts_correct_count(self):
        async def go():
            db = AsyncDatabase(":memory:")
            await db.execute("CREATE TABLE t (id INTEGER, val TEXT)")

            async def worker(tid):
                for i in range(20):
                    await db.execute(
                        "INSERT INTO t VALUES (?, ?)",
                        (tid * 100 + i, f"t{tid}r{i}"),
                    )

            await asyncio.gather(*[worker(i) for i in range(5)])
            cur = await db.execute("SELECT COUNT(*) AS n FROM t")
            row = await cur.fetchone()
            assert row["n"] == 100  # 5 workers × 20 rows
            await db.close()
        run(go())

    def test_concurrent_reads_consistent(self):
        async def go():
            db = AsyncDatabase(":memory:")
            await db.execute("CREATE TABLE t (id INTEGER)")
            for i in range(10):
                await db.execute(f"INSERT INTO t VALUES ({i})")

            async def reader():
                cur = await db.execute("SELECT COUNT(*) AS n FROM t")
                row = await cur.fetchone()
                assert row["n"] >= 10

            await asyncio.gather(*[reader() for _ in range(20)])
            await db.close()
        run(go())


# ── executemany ───────────────────────────────────────────────────────────────

class TestAsyncExecutemany:
    def test_executemany(self):
        async def go():
            db = AsyncDatabase(":memory:")
            await db.execute("CREATE TABLE t (id INTEGER, name TEXT)")
            cur = await db.executemany(
                "INSERT INTO t VALUES (?, ?)",
                [(1, "alice"), (2, "bob"), (3, "carol")],
            )
            assert cur.rowcount == 3
            count_cur = await db.execute("SELECT COUNT(*) AS n FROM t")
            row = await count_cur.fetchone()
            assert row["n"] == 3
            await db.close()
        run(go())


# ── User-defined functions ────────────────────────────────────────────────────

class TestAsyncUserFunctions:
    def test_create_function(self):
        async def go():
            db = AsyncDatabase(":memory:")
            db.create_function("triple", 1, lambda x: x * 3)
            cur = await db.execute("SELECT triple(7) AS v")
            row = await cur.fetchone()
            assert row["v"] == 21
            await db.close()
        run(go())
