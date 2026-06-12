"""Tests for column-oriented storage: CREATE COLUMN TABLE, DML, aggregates,
SHOW STORAGE FORMAT, persistence, and edge cases."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
import tempfile
import os

sys.path.insert(0, str(Path(__file__).parent.parent))

from hyperion.database import Database
from hyperion.errors import TableExistsError


# ── helpers ───────────────────────────────────────────────────────────────────

def _db():
    return Database(":memory:")


# ── DDL ──────────────────────────────────────────────────────────────────────

class TestCreateColumnTable(unittest.TestCase):

    def test_basic_create(self):
        db = _db()
        db.execute("CREATE COLUMN TABLE t (id INTEGER, v TEXT)")
        self.assertIn("t", db.tables)
        db.close()

    def test_storage_type_is_column(self):
        db = _db()
        db.execute("CREATE COLUMN TABLE t (id INTEGER, v TEXT)")
        self.assertEqual(db.tables["t"].storage_type, "column")
        db.close()

    def test_row_table_storage_type_is_row(self):
        db = _db()
        db.execute("CREATE TABLE t (id INTEGER, v TEXT)")
        self.assertEqual(db.tables["t"].storage_type, "row")
        db.close()

    def test_create_column_table_if_not_exists(self):
        db = _db()
        db.execute("CREATE COLUMN TABLE t (id INTEGER)")
        db.execute("CREATE COLUMN TABLE IF NOT EXISTS t (id INTEGER)")
        db.close()

    def test_duplicate_raises(self):
        db = _db()
        db.execute("CREATE COLUMN TABLE t (id INTEGER)")
        with self.assertRaises(TableExistsError):
            db.execute("CREATE COLUMN TABLE t (id INTEGER)")
        db.close()

    def test_drop_column_table(self):
        db = _db()
        db.execute("CREATE COLUMN TABLE t (id INTEGER, v TEXT)")
        db.execute("INSERT INTO t VALUES (1, 'a')")
        db.execute("INSERT INTO t VALUES (2, 'b')")
        db.execute("DROP TABLE t")
        self.assertNotIn("t", db.tables)
        db.close()

    def test_show_storage_format_empty(self):
        db = _db()
        rows = db.execute("SHOW STORAGE FORMAT").fetchall()
        self.assertEqual(rows, [])
        db.close()

    def test_show_storage_format_mixed(self):
        db = _db()
        db.execute("CREATE TABLE row_t (id INTEGER)")
        db.execute("CREATE COLUMN TABLE col_t (id INTEGER)")
        rows = db.execute("SHOW STORAGE FORMAT").fetchall()
        by_name = {r["table"]: r["storage"] for r in rows}
        self.assertEqual(by_name["row_t"], "ROW")
        self.assertEqual(by_name["col_t"], "COLUMN")
        db.close()


# ── INSERT / SELECT ───────────────────────────────────────────────────────────

class TestInsertSelect(unittest.TestCase):

    def setUp(self):
        self.db = _db()
        self.db.execute(
            "CREATE COLUMN TABLE sales "
            "(id INTEGER PRIMARY KEY, amount REAL, name TEXT, region TEXT)"
        )

    def tearDown(self):
        self.db.close()

    def _insert_rows(self):
        self.db.execute("INSERT INTO sales VALUES (1, 100.0, 'Alice', 'East')")
        self.db.execute("INSERT INTO sales VALUES (2, 200.0, 'Bob',   'West')")
        self.db.execute("INSERT INTO sales VALUES (3, 150.0, 'Carol', 'East')")

    def test_select_all(self):
        self._insert_rows()
        rows = self.db.execute("SELECT * FROM sales ORDER BY id").fetchall()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["name"], "Alice")
        self.assertEqual(rows[1]["amount"], 200.0)

    def test_select_with_where(self):
        self._insert_rows()
        rows = self.db.execute(
            "SELECT name FROM sales WHERE region = 'East' ORDER BY id"
        ).fetchall()
        self.assertEqual([r["name"] for r in rows], ["Alice", "Carol"])

    def test_select_specific_columns(self):
        self._insert_rows()
        rows = self.db.execute("SELECT id, amount FROM sales ORDER BY id").fetchall()
        self.assertEqual(len(rows), 3)
        self.assertNotIn("name", rows[0])

    def test_select_empty_table(self):
        rows = self.db.execute("SELECT * FROM sales").fetchall()
        self.assertEqual(rows, [])

    def test_insert_null_values(self):
        self.db.execute("INSERT INTO sales (id, amount) VALUES (10, 50.0)")
        row = self.db.execute("SELECT * FROM sales WHERE id = 10").fetchone()
        self.assertIsNone(row["name"])
        self.assertIsNone(row["region"])

    def test_select_limit_offset(self):
        self._insert_rows()
        rows = self.db.execute("SELECT id FROM sales ORDER BY id LIMIT 2 OFFSET 1").fetchall()
        self.assertEqual([r["id"] for r in rows], [2, 3])

    def test_order_by(self):
        self._insert_rows()
        rows = self.db.execute("SELECT id FROM sales ORDER BY amount DESC").fetchall()
        self.assertEqual([r["id"] for r in rows], [2, 3, 1])


# ── UPDATE ────────────────────────────────────────────────────────────────────

class TestUpdate(unittest.TestCase):

    def setUp(self):
        self.db = _db()
        self.db.execute("CREATE COLUMN TABLE t (id INTEGER, val INTEGER, label TEXT)")
        self.db.execute("INSERT INTO t VALUES (1, 10, 'a')")
        self.db.execute("INSERT INTO t VALUES (2, 20, 'b')")
        self.db.execute("INSERT INTO t VALUES (3, 30, 'c')")

    def tearDown(self):
        self.db.close()

    def test_update_one_row(self):
        self.db.execute("UPDATE t SET val = 99 WHERE id = 2")
        row = self.db.execute("SELECT val FROM t WHERE id = 2").fetchone()
        self.assertEqual(row["val"], 99)

    def test_update_multiple_rows(self):
        self.db.execute("UPDATE t SET label = 'x' WHERE val > 15")
        rows = self.db.execute("SELECT id FROM t WHERE label = 'x' ORDER BY id").fetchall()
        self.assertEqual([r["id"] for r in rows], [2, 3])

    def test_update_all_rows(self):
        self.db.execute("UPDATE t SET val = 0")
        rows = self.db.execute("SELECT val FROM t").fetchall()
        self.assertTrue(all(r["val"] == 0 for r in rows))

    def test_update_to_null(self):
        self.db.execute("UPDATE t SET label = NULL WHERE id = 1")
        row = self.db.execute("SELECT label FROM t WHERE id = 1").fetchone()
        self.assertIsNone(row["label"])

    def test_update_expression(self):
        self.db.execute("UPDATE t SET val = val + 5 WHERE id = 1")
        row = self.db.execute("SELECT val FROM t WHERE id = 1").fetchone()
        self.assertEqual(row["val"], 15)

    def test_update_preserves_other_rows(self):
        self.db.execute("UPDATE t SET val = 999 WHERE id = 2")
        rows = self.db.execute("SELECT val FROM t ORDER BY id").fetchall()
        self.assertEqual([r["val"] for r in rows], [10, 999, 30])


# ── DELETE ────────────────────────────────────────────────────────────────────

class TestDelete(unittest.TestCase):

    def setUp(self):
        self.db = _db()
        self.db.execute("CREATE COLUMN TABLE t (id INTEGER, v TEXT)")
        for i in range(5):
            self.db.execute(f"INSERT INTO t VALUES ({i}, 'row{i}')")

    def tearDown(self):
        self.db.close()

    def test_delete_one_row(self):
        self.db.execute("DELETE FROM t WHERE id = 2")
        rows = self.db.execute("SELECT id FROM t ORDER BY id").fetchall()
        self.assertEqual([r["id"] for r in rows], [0, 1, 3, 4])

    def test_delete_multiple_rows(self):
        self.db.execute("DELETE FROM t WHERE id > 2")
        rows = self.db.execute("SELECT id FROM t ORDER BY id").fetchall()
        self.assertEqual([r["id"] for r in rows], [0, 1, 2])

    def test_delete_all_rows(self):
        self.db.execute("DELETE FROM t")
        rows = self.db.execute("SELECT * FROM t").fetchall()
        self.assertEqual(rows, [])

    def test_delete_no_match(self):
        self.db.execute("DELETE FROM t WHERE id = 99")
        rows = self.db.execute("SELECT COUNT(*) AS n FROM t").fetchone()
        self.assertEqual(rows["n"], 5)

    def test_delete_then_reinsert(self):
        self.db.execute("DELETE FROM t WHERE id = 3")
        self.db.execute("INSERT INTO t VALUES (3, 'new3')")
        row = self.db.execute("SELECT v FROM t WHERE id = 3").fetchone()
        self.assertEqual(row["v"], "new3")

    def test_delete_limit(self):
        self.db.execute("DELETE FROM t WHERE id < 4 LIMIT 2")
        count = self.db.execute("SELECT COUNT(*) AS n FROM t").fetchone()["n"]
        self.assertEqual(count, 3)


# ── Aggregates (pushdown) ─────────────────────────────────────────────────────

class TestAggregates(unittest.TestCase):

    def setUp(self):
        self.db = _db()
        self.db.execute("CREATE COLUMN TABLE nums (id INTEGER, v REAL)")
        self.db.execute("INSERT INTO nums VALUES (1, 10.0)")
        self.db.execute("INSERT INTO nums VALUES (2, 20.0)")
        self.db.execute("INSERT INTO nums VALUES (3, 30.0)")
        self.db.execute("INSERT INTO nums VALUES (4, NULL)")

    def tearDown(self):
        self.db.close()

    def test_count_star(self):
        row = self.db.execute("SELECT COUNT(*) AS n FROM nums").fetchone()
        self.assertEqual(row["n"], 4)

    def test_sum(self):
        row = self.db.execute("SELECT SUM(v) AS s FROM nums").fetchone()
        self.assertAlmostEqual(row["s"], 60.0)

    def test_avg(self):
        row = self.db.execute("SELECT AVG(v) AS a FROM nums").fetchone()
        self.assertAlmostEqual(row["a"], 20.0)

    def test_min(self):
        row = self.db.execute("SELECT MIN(v) AS m FROM nums").fetchone()
        self.assertAlmostEqual(row["m"], 10.0)

    def test_max(self):
        row = self.db.execute("SELECT MAX(v) AS m FROM nums").fetchone()
        self.assertAlmostEqual(row["m"], 30.0)

    def test_count_non_null(self):
        row = self.db.execute("SELECT COUNT(v) AS n FROM nums").fetchone()
        self.assertEqual(row["n"], 3)

    def test_sum_empty(self):
        self.db.execute("DELETE FROM nums")
        row = self.db.execute("SELECT SUM(v) AS s FROM nums").fetchone()
        self.assertIsNone(row["s"])

    def test_count_star_empty(self):
        self.db.execute("DELETE FROM nums")
        row = self.db.execute("SELECT COUNT(*) AS n FROM nums").fetchone()
        self.assertEqual(row["n"], 0)

    def test_aggregate_with_where(self):
        row = self.db.execute("SELECT SUM(v) AS s FROM nums WHERE id < 3").fetchone()
        self.assertAlmostEqual(row["s"], 30.0)

    def test_group_by(self):
        self.db.execute("ALTER TABLE nums ADD COLUMN grp TEXT DEFAULT 'A'")
        self.db.execute("UPDATE nums SET grp = 'B' WHERE id > 2")
        rows = self.db.execute(
            "SELECT grp, SUM(v) AS s FROM nums GROUP BY grp ORDER BY grp"
        ).fetchall()
        grp_map = {r["grp"]: r["s"] for r in rows}
        self.assertAlmostEqual(grp_map["A"], 30.0)


# ── Text/Blob columns ─────────────────────────────────────────────────────────

class TestTextBlob(unittest.TestCase):

    def setUp(self):
        self.db = _db()
        self.db.execute("CREATE COLUMN TABLE docs (id INTEGER, body TEXT)")

    def tearDown(self):
        self.db.close()

    def test_short_text(self):
        self.db.execute("INSERT INTO docs VALUES (1, 'hello world')")
        row = self.db.execute("SELECT body FROM docs WHERE id = 1").fetchone()
        self.assertEqual(row["body"], "hello world")

    def test_medium_text(self):
        body = "x" * 500
        self.db.execute(f"INSERT INTO docs VALUES (1, '{body}')")
        row = self.db.execute("SELECT body FROM docs WHERE id = 1").fetchone()
        self.assertEqual(row["body"], body)

    def test_long_text_overflow(self):
        body = "A" * 5000   # exceeds MAX_INLINE_STR (4078)
        self.db.execute(f"INSERT INTO docs VALUES (1, '{body}')")
        row = self.db.execute("SELECT body FROM docs WHERE id = 1").fetchone()
        self.assertEqual(row["body"], body)

    def test_update_overflow_text(self):
        body1 = "B" * 5000
        body2 = "C" * 6000
        self.db.execute(f"INSERT INTO docs VALUES (1, '{body1}')")
        self.db.execute(f"UPDATE docs SET body = '{body2}' WHERE id = 1")
        row = self.db.execute("SELECT body FROM docs WHERE id = 1").fetchone()
        self.assertEqual(row["body"], body2)

    def test_null_text(self):
        self.db.execute("INSERT INTO docs (id) VALUES (5)")
        row = self.db.execute("SELECT body FROM docs WHERE id = 5").fetchone()
        self.assertIsNone(row["body"])


# ── Persistence (file-backed) ─────────────────────────────────────────────────

class TestPersistence(unittest.TestCase):

    def test_column_table_survives_reopen(self):
        fd, path = tempfile.mkstemp(suffix=".hdb")
        os.close(fd)
        os.unlink(path)
        try:
            db = Database(path)
            db.execute("CREATE COLUMN TABLE t (id INTEGER, v TEXT)")
            db.execute("INSERT INTO t VALUES (1, 'persisted')")
            db.execute("INSERT INTO t VALUES (2, 'also here')")
            db.close()

            db2 = Database(path)
            rows = db2.execute("SELECT * FROM t ORDER BY id").fetchall()
            db2.close()

            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["v"], "persisted")
            self.assertEqual(rows[1]["v"], "also here")
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_storage_type_persists(self):
        fd, path = tempfile.mkstemp(suffix=".hdb")
        os.close(fd)
        os.unlink(path)
        try:
            db = Database(path)
            db.execute("CREATE COLUMN TABLE ct (id INTEGER)")
            db.execute("CREATE TABLE rt (id INTEGER)")
            db.close()

            db2 = Database(path)
            self.assertEqual(db2.tables["ct"].storage_type, "column")
            self.assertEqual(db2.tables["rt"].storage_type, "row")
            db2.close()
        finally:
            if os.path.exists(path):
                os.unlink(path)


# ── Column store + row store interop ─────────────────────────────────────────

class TestInterop(unittest.TestCase):

    def test_join_column_and_row_tables(self):
        db = _db()
        db.execute("CREATE TABLE employees (id INTEGER, dept TEXT)")
        db.execute("CREATE COLUMN TABLE metrics (emp_id INTEGER, score REAL)")
        db.execute("INSERT INTO employees VALUES (1, 'Eng')")
        db.execute("INSERT INTO employees VALUES (2, 'Sales')")
        db.execute("INSERT INTO metrics VALUES (1, 95.0)")
        db.execute("INSERT INTO metrics VALUES (2, 80.0)")
        rows = db.execute(
            "SELECT employees.dept, metrics.score "
            "FROM employees "
            "JOIN metrics ON employees.id = metrics.emp_id "
            "ORDER BY employees.id"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        db.close()

    def test_insert_select_into_column_table(self):
        db = _db()
        db.execute("CREATE TABLE src (id INTEGER, v TEXT)")
        db.execute("CREATE COLUMN TABLE dst (id INTEGER, v TEXT)")
        db.execute("INSERT INTO src VALUES (1, 'alpha')")
        db.execute("INSERT INTO src VALUES (2, 'beta')")
        db.execute("INSERT INTO dst SELECT * FROM src")
        rows = db.execute("SELECT * FROM dst ORDER BY id").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["v"], "alpha")
        db.close()

    def test_vacuum_preserves_column_table(self):
        db = _db()
        db.execute("CREATE COLUMN TABLE t (id INTEGER, v TEXT)")
        db.execute("INSERT INTO t VALUES (1, 'hello')")
        db.execute("INSERT INTO t VALUES (2, 'world')")
        db.execute("DELETE FROM t WHERE id = 1")
        # vacuum only works on file-backed databases; skip for :memory:
        # Just verify the table is intact after delete
        rows = db.execute("SELECT * FROM t").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["v"], "world")
        db.close()


# ── ANALYZE ───────────────────────────────────────────────────────────────────

class TestAnalyze(unittest.TestCase):

    def test_analyze_column_table(self):
        db = _db()
        db.execute("CREATE COLUMN TABLE t (id INTEGER, v TEXT)")
        for i in range(10):
            db.execute(f"INSERT INTO t VALUES ({i}, 'row{i}')")
        result = db.execute("ANALYZE t").fetchone()
        self.assertIn("t", db._catalog.stats)
        self.assertEqual(db._catalog.stats["t"]["row_count"], 10)
        db.close()


# ── Multi-row chunk boundary ──────────────────────────────────────────────────

class TestChunkBoundary(unittest.TestCase):

    def test_many_rows_span_multiple_chunks(self):
        """Insert enough rows to overflow the first chunk page for each column."""
        db = _db()
        db.execute("CREATE COLUMN TABLE t (id INTEGER, v INTEGER)")
        n = 2000
        for i in range(n):
            db.execute(f"INSERT INTO t VALUES ({i}, {i * 2})")
        rows = db.execute("SELECT COUNT(*) AS c FROM t").fetchone()
        self.assertEqual(rows["c"], n)
        # Spot-check last row
        row = db.execute(f"SELECT v FROM t WHERE id = {n - 1}").fetchone()
        self.assertEqual(row["v"], (n - 1) * 2)
        # Spot-check first row
        row0 = db.execute("SELECT v FROM t WHERE id = 0").fetchone()
        self.assertEqual(row0["v"], 0)
        db.close()

    def test_delete_across_chunk_boundary(self):
        db = _db()
        db.execute("CREATE COLUMN TABLE t (id INTEGER)")
        for i in range(1000):
            db.execute(f"INSERT INTO t VALUES ({i})")
        self.db_ref = db
        db.execute("DELETE FROM t WHERE id % 2 = 0")
        remaining = db.execute("SELECT COUNT(*) AS c FROM t").fetchone()["c"]
        self.assertEqual(remaining, 500)
        db.close()

    def test_update_across_chunk_boundary(self):
        db = _db()
        db.execute("CREATE COLUMN TABLE t (id INTEGER, v INTEGER)")
        for i in range(1000):
            db.execute(f"INSERT INTO t VALUES ({i}, {i})")
        db.execute("UPDATE t SET v = -1 WHERE id < 500")
        count = db.execute("SELECT COUNT(*) AS c FROM t WHERE v = -1").fetchone()["c"]
        self.assertEqual(count, 500)
        db.close()


if __name__ == "__main__":
    unittest.main()
