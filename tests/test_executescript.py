"""Tests for executescript SELECT-result capture."""
import asyncio
import pytest
from hyperion import Database, AsyncDatabase


# ── Sync ──────────────────────────────────────────────────────────────────────

class TestExecutescriptSync:
    def test_select_result_accessible_via_fetchall(self):
        db = Database(":memory:")
        db.execute("CREATE TABLE t (id INTEGER)")
        db.execute("INSERT INTO t VALUES (1)")
        db.execute("INSERT INTO t VALUES (2)")
        cur = db.executescript("SELECT id FROM t ORDER BY id")
        rows = cur.fetchall()
        assert [r["id"] for r in rows] == [1, 2]

    def test_select_after_ddl_and_dml(self):
        db = Database(":memory:")
        cur = db.executescript(
            "CREATE TABLE t (id INTEGER, name TEXT);"
            "INSERT INTO t VALUES (1, 'alice');"
            "INSERT INTO t VALUES (2, 'bob');"
            "SELECT name FROM t ORDER BY id;"
        )
        rows = cur.fetchall()
        assert [r["name"] for r in rows] == ["alice", "bob"]

    def test_description_set_from_last_select(self):
        db = Database(":memory:")
        cur = db.executescript(
            "CREATE TABLE t (x INTEGER, y TEXT);"
            "INSERT INTO t VALUES (1, 'a');"
            "SELECT x, y FROM t;"
        )
        assert cur.description is not None
        col_names = [d[0] for d in cur.description]
        assert col_names == ["x", "y"]

    def test_script_results_collects_all_selects(self):
        db = Database(":memory:")
        db.execute("CREATE TABLE a (n INTEGER)")
        db.execute("CREATE TABLE b (v TEXT)")
        db.execute("INSERT INTO a VALUES (10)")
        db.execute("INSERT INTO b VALUES ('hello')")
        cur = db.executescript(
            "SELECT n FROM a;"
            "SELECT v FROM b;"
        )
        assert len(cur.script_results) == 2
        assert cur.script_results[0][0]["n"] == 10
        assert cur.script_results[1][0]["v"] == "hello"

    def test_fetchall_returns_last_select(self):
        db = Database(":memory:")
        db.execute("CREATE TABLE t (id INTEGER)")
        for i in range(3):
            db.execute(f"INSERT INTO t VALUES ({i})")
        cur = db.executescript(
            "SELECT id FROM t WHERE id = 0;"
            "SELECT id FROM t ORDER BY id DESC;"
        )
        rows = cur.fetchall()
        # last SELECT is ORDER BY id DESC
        assert [r["id"] for r in rows] == [2, 1, 0]

    def test_no_select_returns_empty(self):
        db = Database(":memory:")
        cur = db.executescript(
            "CREATE TABLE t (id INTEGER);"
            "INSERT INTO t VALUES (99);"
        )
        assert cur.fetchall() == []
        assert cur.script_results == []
        assert cur.description is None

    def test_select_only_no_dml(self):
        db = Database(":memory:")
        db.execute("CREATE TABLE t (id INTEGER)")
        db.execute("INSERT INTO t VALUES (7)")
        cur = db.executescript("SELECT id FROM t")
        rows = cur.fetchall()
        assert rows[0]["id"] == 7

    def test_fetchone_works_on_script_result(self):
        db = Database(":memory:")
        db.execute("CREATE TABLE t (id INTEGER)")
        db.execute("INSERT INTO t VALUES (5)")
        db.execute("INSERT INTO t VALUES (6)")
        cur = db.executescript("SELECT id FROM t ORDER BY id")
        assert cur.fetchone()["id"] == 5
        assert cur.fetchone()["id"] == 6
        assert cur.fetchone() is None

    def test_fetchmany_works_on_script_result(self):
        db = Database(":memory:")
        db.execute("CREATE TABLE t (n INTEGER)")
        for i in range(5):
            db.execute(f"INSERT INTO t VALUES ({i})")
        cur = db.executescript("SELECT n FROM t ORDER BY n")
        batch = cur.fetchmany(3)
        assert [r["n"] for r in batch] == [0, 1, 2]

    def test_script_results_empty_select(self):
        """A SELECT that matches no rows still records an empty list entry."""
        db = Database(":memory:")
        db.execute("CREATE TABLE t (id INTEGER)")
        cur = db.executescript("SELECT id FROM t")
        assert cur.script_results == [[]]
        assert cur.fetchall() == []

    def test_dml_rowcount_not_overwritten_when_last_stmt_is_dml(self):
        """When the script ends with DML (no SELECT), rowcount is not -1."""
        db = Database(":memory:")
        cur = db.executescript(
            "CREATE TABLE t (id INTEGER);"
            "INSERT INTO t VALUES (1);"
        )
        # No SELECT — rowcount is -1 (cursor reset), description is None
        assert cur.description is None

    def test_select_inside_transaction_script(self):
        db = Database(":memory:")
        db.execute("CREATE TABLE t (id INTEGER)")
        cur = db.executescript(
            "BEGIN;"
            "INSERT INTO t VALUES (42);"
            "COMMIT;"
            "SELECT id FROM t;"
        )
        rows = cur.fetchall()
        assert rows[0]["id"] == 42

    def test_multiple_selects_script_results_order(self):
        db = Database(":memory:")
        db.execute("CREATE TABLE t (n INTEGER)")
        for i in range(4):
            db.execute(f"INSERT INTO t VALUES ({i})")
        cur = db.executescript(
            "SELECT n FROM t WHERE n < 2 ORDER BY n;"
            "SELECT n FROM t WHERE n >= 2 ORDER BY n;"
        )
        assert len(cur.script_results) == 2
        assert [r["n"] for r in cur.script_results[0]] == [0, 1]
        assert [r["n"] for r in cur.script_results[1]] == [2, 3]


# ── Async ─────────────────────────────────────────────────────────────────────

def run(coro):
    return asyncio.run(coro)


class TestExecutescriptAsync:
    def test_async_select_result_via_fetchall(self):
        async def go():
            db = AsyncDatabase(":memory:")
            await db.execute("CREATE TABLE t (id INTEGER)")
            await db.execute("INSERT INTO t VALUES (1)")
            cur = await db.executescript("SELECT id FROM t")
            rows = await cur.fetchall()
            assert rows[0]["id"] == 1
            await db.close()
        run(go())

    def test_async_script_results_multiple_selects(self):
        async def go():
            db = AsyncDatabase(":memory:")
            cur = await db.executescript(
                "CREATE TABLE t (n INTEGER);"
                "INSERT INTO t VALUES (10);"
                "INSERT INTO t VALUES (20);"
                "SELECT n FROM t WHERE n = 10;"
                "SELECT n FROM t WHERE n = 20;"
            )
            assert len(cur.script_results) == 2
            assert cur.script_results[0][0]["n"] == 10
            assert cur.script_results[1][0]["n"] == 20
            await db.close()
        run(go())

    def test_async_no_select_empty_results(self):
        async def go():
            db = AsyncDatabase(":memory:")
            cur = await db.executescript(
                "CREATE TABLE t (id INTEGER);"
                "INSERT INTO t VALUES (1);"
            )
            assert await cur.fetchall() == []
            assert cur.script_results == []
            await db.close()
        run(go())
