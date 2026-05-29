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


# ── Buffered fetch (large result sets) ───────────────────────────────────────

class TestAsyncBufferedFetch:
    """AsyncCursor must return correct rows at all sizes, including chunk boundaries."""

    def _make_db_with_rows(self, n: int):
        """Return an AsyncDatabase(:memory:) loaded with n rows in table t(n INTEGER)."""
        import asyncio
        from hyperion import AsyncDatabase

        async def setup():
            db = AsyncDatabase(":memory:")
            await db.execute("CREATE TABLE t (n INTEGER)")
            await db.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(n)])
            return db

        return asyncio.run(setup())

    def test_fetchall_large(self):
        """fetchall on 1000 rows (>_FETCH_CHUNK) returns all rows."""
        async def go():
            from hyperion import AsyncDatabase
            db = AsyncDatabase(":memory:")
            await db.execute("CREATE TABLE t (n INTEGER)")
            await db.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(1000)])
            cur = await db.execute("SELECT n FROM t ORDER BY n")
            rows = await cur.fetchall()
            assert len(rows) == 1000
            assert [r["n"] for r in rows] == list(range(1000))
            await db.close()
        run(go())

    def test_async_for_large(self):
        """async for over 500 rows (close to 2× _FETCH_CHUNK) returns all rows."""
        async def go():
            from hyperion import AsyncDatabase
            db = AsyncDatabase(":memory:")
            await db.execute("CREATE TABLE t (n INTEGER)")
            await db.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(500)])
            cur = await db.execute("SELECT n FROM t ORDER BY n")
            results = []
            async for row in cur:
                results.append(row["n"])
            assert results == list(range(500))
            await db.close()
        run(go())

    def test_async_for_exact_chunk_boundary(self):
        """async for over exactly _FETCH_CHUNK rows hits the boundary cleanly."""
        from hyperion.async_db import _FETCH_CHUNK

        async def go():
            from hyperion import AsyncDatabase
            db = AsyncDatabase(":memory:")
            await db.execute("CREATE TABLE t (n INTEGER)")
            await db.executemany(
                "INSERT INTO t VALUES (?)", [(i,) for i in range(_FETCH_CHUNK)]
            )
            cur = await db.execute("SELECT n FROM t ORDER BY n")
            results = []
            async for row in cur:
                results.append(row["n"])
            assert results == list(range(_FETCH_CHUNK))
            await db.close()
        run(go())

    def test_fetchmany_spans_chunk_boundary(self):
        """fetchmany with size > _FETCH_CHUNK loads multiple chunks correctly."""
        from hyperion.async_db import _FETCH_CHUNK

        async def go():
            from hyperion import AsyncDatabase
            n = _FETCH_CHUNK + 10
            db = AsyncDatabase(":memory:")
            await db.execute("CREATE TABLE t (n INTEGER)")
            await db.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(n)])
            cur = await db.execute("SELECT n FROM t ORDER BY n")
            batch = await cur.fetchmany(_FETCH_CHUNK + 5)
            assert len(batch) == _FETCH_CHUNK + 5
            rest = await cur.fetchmany(100)
            assert len(rest) == 5  # remaining rows
            assert await cur.fetchone() is None
            await db.close()
        run(go())

    def test_interleaved_fetchone_fetchall(self):
        """fetchone drains buffer; fetchall returns the remainder without repetition."""
        async def go():
            from hyperion import AsyncDatabase
            db = AsyncDatabase(":memory:")
            await db.execute("CREATE TABLE t (n INTEGER)")
            await db.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(10)])
            cur = await db.execute("SELECT n FROM t ORDER BY n")
            first = await cur.fetchone()
            assert first["n"] == 0
            rest = await cur.fetchall()
            assert [r["n"] for r in rest] == list(range(1, 10))
            assert await cur.fetchone() is None
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
