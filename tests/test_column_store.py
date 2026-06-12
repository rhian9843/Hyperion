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


class TestCreateColumnTableAsSelect(unittest.TestCase):

    def test_basic_ctas_column(self):
        db = _db()
        db.execute("CREATE TABLE src (id INTEGER, name TEXT, score REAL)")
        db.execute("INSERT INTO src VALUES (1, 'Alice', 9.5)")
        db.execute("INSERT INTO src VALUES (2, 'Bob',   8.0)")
        db.execute("CREATE COLUMN TABLE dst AS SELECT * FROM src")

        fmt = {r["table"]: r["storage"]
               for r in db.execute("SHOW STORAGE FORMAT").fetchall()}
        self.assertEqual(fmt["dst"], "COLUMN")

        rows = db.execute("SELECT * FROM dst ORDER BY id").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["name"], "Alice")
        self.assertAlmostEqual(rows[1]["score"], 8.0)
        db.close()

    def test_ctas_column_aggregates_work(self):
        db = _db()
        db.execute("CREATE TABLE src (region TEXT, revenue REAL)")
        db.execute("INSERT INTO src VALUES ('North', 1200.0)")
        db.execute("INSERT INTO src VALUES ('South',  850.0)")
        db.execute("INSERT INTO src VALUES ('North', 2000.0)")
        db.execute(
            "CREATE COLUMN TABLE summary AS "
            "SELECT region, SUM(revenue) AS total FROM src GROUP BY region"
        )
        fmt = {r["table"]: r["storage"]
               for r in db.execute("SHOW STORAGE FORMAT").fetchall()}
        self.assertEqual(fmt["summary"], "COLUMN")

        total = db.execute("SELECT SUM(total) AS grand FROM summary").fetchone()
        self.assertAlmostEqual(total["grand"], 4050.0)
        db.close()

    def test_ctas_column_if_not_exists(self):
        db = _db()
        db.execute("CREATE TABLE src (id INTEGER)")
        db.execute("INSERT INTO src VALUES (1)")
        db.execute("CREATE COLUMN TABLE dst AS SELECT * FROM src")
        # Second time with IF NOT EXISTS must not raise
        db.execute("CREATE COLUMN TABLE IF NOT EXISTS dst AS SELECT * FROM src")
        count = db.execute("SELECT COUNT(*) AS n FROM dst").fetchone()["n"]
        self.assertEqual(count, 1)
        db.close()

    def test_ctas_column_empty_source(self):
        db = _db()
        db.execute("CREATE TABLE src (id INTEGER, name TEXT)")
        db.execute("CREATE COLUMN TABLE dst AS SELECT * FROM src")
        fmt = {r["table"]: r["storage"]
               for r in db.execute("SHOW STORAGE FORMAT").fetchall()}
        self.assertEqual(fmt["dst"], "COLUMN")
        count = db.execute("SELECT COUNT(*) AS n FROM dst").fetchone()["n"]
        self.assertEqual(count, 0)
        db.close()


class TestFKColumnParent(unittest.TestCase):
    """FK actions (CASCADE DELETE, ON UPDATE CASCADE, SET NULL) where the parent
    is a column store table and the child is a row store table, and vice-versa."""

    def _setup_cascade_delete(self):
        db = _db()
        db.execute(
            "CREATE COLUMN TABLE col_parent "
            "(id INTEGER PRIMARY KEY, name TEXT)"
        )
        db.execute(
            "CREATE TABLE row_child "
            "(id INTEGER PRIMARY KEY, parent_id INTEGER, "
            "FOREIGN KEY (parent_id) REFERENCES col_parent(id) ON DELETE CASCADE)"
        )
        db.execute("INSERT INTO col_parent VALUES (1, 'Alpha')")
        db.execute("INSERT INTO col_parent VALUES (2, 'Beta')")
        db.execute("INSERT INTO row_child VALUES (10, 1)")
        db.execute("INSERT INTO row_child VALUES (11, 1)")
        db.execute("INSERT INTO row_child VALUES (12, 2)")
        return db

    def test_on_delete_cascade_column_parent(self):
        db = self._setup_cascade_delete()
        db.execute("DELETE FROM col_parent WHERE id = 1")
        parent_rows = db.execute("SELECT COUNT(*) AS n FROM col_parent").fetchone()["n"]
        child_rows  = db.execute("SELECT COUNT(*) AS n FROM row_child").fetchone()["n"]
        self.assertEqual(parent_rows, 1)
        self.assertEqual(child_rows, 1)   # only child of parent 2 survives
        survivor = db.execute("SELECT parent_id FROM row_child").fetchone()
        self.assertEqual(survivor["parent_id"], 2)
        db.close()

    def test_on_delete_cascade_column_parent_delete_all(self):
        db = self._setup_cascade_delete()
        db.execute("DELETE FROM col_parent")
        self.assertEqual(
            db.execute("SELECT COUNT(*) AS n FROM col_parent").fetchone()["n"], 0)
        self.assertEqual(
            db.execute("SELECT COUNT(*) AS n FROM row_child").fetchone()["n"], 0)
        db.close()

    def test_on_update_cascade_column_parent(self):
        db = _db()
        db.execute(
            "CREATE COLUMN TABLE col_parent (id INTEGER PRIMARY KEY, name TEXT)"
        )
        db.execute(
            "CREATE TABLE row_child "
            "(id INTEGER PRIMARY KEY, parent_id INTEGER, "
            "FOREIGN KEY (parent_id) REFERENCES col_parent(id) ON UPDATE CASCADE)"
        )
        db.execute("INSERT INTO col_parent VALUES (1, 'Alpha')")
        db.execute("INSERT INTO row_child VALUES (10, 1)")
        db.execute("INSERT INTO row_child VALUES (11, 1)")
        db.execute("UPDATE col_parent SET id = 99 WHERE id = 1")
        children = db.execute(
            "SELECT parent_id FROM row_child ORDER BY id").fetchall()
        self.assertEqual(children[0]["parent_id"], 99)
        self.assertEqual(children[1]["parent_id"], 99)
        db.close()

    def test_set_null_column_parent(self):
        db = _db()
        db.execute(
            "CREATE COLUMN TABLE col_parent (id INTEGER PRIMARY KEY, name TEXT)"
        )
        db.execute(
            "CREATE TABLE row_child "
            "(id INTEGER PRIMARY KEY, parent_id INTEGER, "
            "FOREIGN KEY (parent_id) REFERENCES col_parent(id) ON DELETE SET NULL)"
        )
        db.execute("INSERT INTO col_parent VALUES (1, 'Alpha')")
        db.execute("INSERT INTO row_child VALUES (10, 1)")
        db.execute("INSERT INTO row_child VALUES (11, 1)")
        db.execute("DELETE FROM col_parent WHERE id = 1")
        children = db.execute(
            "SELECT parent_id FROM row_child ORDER BY id").fetchall()
        self.assertIsNone(children[0]["parent_id"])
        self.assertIsNone(children[1]["parent_id"])
        db.close()

    def test_on_delete_cascade_column_child(self):
        """Column-store child table cascaded from a row-store parent."""
        db = _db()
        db.execute("CREATE TABLE row_parent (id INTEGER PRIMARY KEY, name TEXT)")
        db.execute(
            "CREATE COLUMN TABLE col_child "
            "(id INTEGER PRIMARY KEY, parent_id INTEGER, score REAL, "
            "FOREIGN KEY (parent_id) REFERENCES row_parent(id) ON DELETE CASCADE)"
        )
        db.execute("INSERT INTO row_parent VALUES (1, 'P1')")
        db.execute("INSERT INTO row_parent VALUES (2, 'P2')")
        db.execute("INSERT INTO col_child VALUES (10, 1, 9.5)")
        db.execute("INSERT INTO col_child VALUES (11, 1, 8.0)")
        db.execute("INSERT INTO col_child VALUES (12, 2, 7.5)")
        db.execute("DELETE FROM row_parent WHERE id = 1")
        remaining = db.execute("SELECT * FROM col_child ORDER BY id").fetchall()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["parent_id"], 2)
        db.close()

    def test_cascade_delete_uses_index_on_column_child(self):
        """Cascade delete on a column-store child that has an index on the FK column
        must produce the correct result (index path, not full-scan fallback)."""
        db = _db()
        db.execute("CREATE TABLE row_parent (id INTEGER PRIMARY KEY, name TEXT)")
        db.execute(
            "CREATE COLUMN TABLE col_child "
            "(id INTEGER PRIMARY KEY, parent_id INTEGER, val REAL, "
            "FOREIGN KEY (parent_id) REFERENCES row_parent(id) ON DELETE CASCADE)"
        )
        db.execute("CREATE INDEX idx_child_pid ON col_child(parent_id)")
        for i in range(1, 4):
            db.execute(f"INSERT INTO row_parent VALUES ({i}, 'P{i}')")
        for i in range(10):
            db.execute(f"INSERT INTO col_child VALUES ({i}, {(i % 3) + 1}, {i * 1.5})")
        # Delete parent 1 — cascades to children with parent_id = 1 (rows 0, 3, 6, 9)
        db.execute("DELETE FROM row_parent WHERE id = 1")
        remaining = db.execute("SELECT id FROM col_child ORDER BY id").fetchall()
        ids = [r["id"] for r in remaining]
        self.assertNotIn(0, ids)
        self.assertNotIn(3, ids)
        self.assertNotIn(6, ids)
        self.assertNotIn(9, ids)
        self.assertEqual(len(ids), 6)
        # Aggregates must still be correct
        cnt = db.execute("SELECT COUNT(*) AS n FROM col_child").fetchone()["n"]
        self.assertEqual(cnt, 6)
        db.close()

    def test_restrict_column_parent_raises(self):
        db = _db()
        db.execute(
            "CREATE COLUMN TABLE col_parent (id INTEGER PRIMARY KEY, name TEXT)"
        )
        db.execute(
            "CREATE TABLE row_child "
            "(id INTEGER, parent_id INTEGER, "
            "FOREIGN KEY (parent_id) REFERENCES col_parent(id))"
        )
        db.execute("INSERT INTO col_parent VALUES (1, 'Alpha')")
        db.execute("INSERT INTO row_child VALUES (10, 1)")
        from hyperion.errors import ForeignKeyConstraintError
        with self.assertRaises(ForeignKeyConstraintError):
            db.execute("DELETE FROM col_parent WHERE id = 1")
        db.close()


class TestExplainQueryPlan(unittest.TestCase):

    def setUp(self):
        self.db = _db()
        self.db.execute("CREATE COLUMN TABLE col_t (id INTEGER, region TEXT, revenue REAL)")
        self.db.execute("CREATE TABLE row_t (id INTEGER, name TEXT)")
        for i in range(5):
            self.db.execute(f"INSERT INTO col_t VALUES ({i}, 'North', {i * 100.0})")
            self.db.execute(f"INSERT INTO row_t VALUES ({i}, 'row{i}')")

    def tearDown(self):
        self.db.close()

    def _plan(self, sql):
        return [r["detail"] for r in
                self.db.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()]

    def test_column_table_scan_labelled(self):
        plan = self._plan("SELECT * FROM col_t")
        self.assertTrue(any("[COLUMN STORE]" in d for d in plan),
                        f"Expected [COLUMN STORE] tag, got: {plan}")

    def test_row_table_scan_not_labelled(self):
        plan = self._plan("SELECT * FROM row_t")
        self.assertFalse(any("[COLUMN STORE]" in d for d in plan),
                         f"Unexpected [COLUMN STORE] on row table: {plan}")

    def test_aggregate_pushdown_labelled(self):
        plan = self._plan("SELECT SUM(revenue) FROM col_t")
        self.assertTrue(any("[AGGREGATE PUSHDOWN]" in d for d in plan),
                        f"Expected [AGGREGATE PUSHDOWN], got: {plan}")

    def test_count_star_pushdown_labelled(self):
        plan = self._plan("SELECT COUNT(*) FROM col_t")
        self.assertTrue(any("[AGGREGATE PUSHDOWN]" in d for d in plan),
                        f"Expected [AGGREGATE PUSHDOWN], got: {plan}")

    def test_group_by_pushdown_labelled(self):
        plan = self._plan("SELECT region, SUM(revenue) FROM col_t GROUP BY region")
        self.assertTrue(any("[AGGREGATE PUSHDOWN]" in d for d in plan),
                        f"Expected [AGGREGATE PUSHDOWN] for GROUP BY, got: {plan}")

    def test_plain_select_no_pushdown_label(self):
        plan = self._plan("SELECT id, region FROM col_t")
        self.assertFalse(any("[AGGREGATE PUSHDOWN]" in d for d in plan),
                         f"Unexpected [AGGREGATE PUSHDOWN] on plain select: {plan}")

    def test_distinct_aggregate_no_pushdown_label(self):
        plan = self._plan("SELECT COUNT(DISTINCT region) FROM col_t")
        self.assertFalse(any("[AGGREGATE PUSHDOWN]" in d for d in plan),
                         f"DISTINCT should not show [AGGREGATE PUSHDOWN]: {plan}")

    def test_join_column_table_labelled(self):
        plan = self._plan(
            "SELECT col_t.revenue, row_t.name "
            "FROM col_t JOIN row_t ON col_t.id = row_t.id"
        )
        self.assertTrue(any("[COLUMN STORE]" in d for d in plan),
                        f"Expected [COLUMN STORE] in JOIN plan, got: {plan}")


class TestIntegrityCheck(unittest.TestCase):

    def test_integrity_check_passes_on_clean_column_table(self):
        fd, path = tempfile.mkstemp(suffix=".hdb")
        os.close(fd); os.unlink(path)
        try:
            db = Database(path)
            db.execute("CREATE COLUMN TABLE t (id INTEGER, name TEXT, score REAL)")
            for i in range(20):
                db.execute(f"INSERT INTO t VALUES ({i}, 'row{i}', {i * 1.5})")
            result = db.execute("PRAGMA integrity_check").fetchall()
            db.close()
            statuses = [r["integrity_check"] for r in result]
            self.assertEqual(statuses, ["ok"])
        finally:
            if os.path.exists(path): os.unlink(path)

    def test_integrity_check_passes_mixed_tables(self):
        fd, path = tempfile.mkstemp(suffix=".hdb")
        os.close(fd); os.unlink(path)
        try:
            db = Database(path)
            db.execute("CREATE TABLE row_t (id INTEGER, val TEXT)")
            db.execute("CREATE COLUMN TABLE col_t (id INTEGER, val REAL)")
            for i in range(10):
                db.execute(f"INSERT INTO row_t VALUES ({i}, 'r{i}')")
                db.execute(f"INSERT INTO col_t VALUES ({i}, {i * 2.0})")
            result = db.execute("PRAGMA integrity_check").fetchall()
            db.close()
            self.assertEqual([r["integrity_check"] for r in result], ["ok"])
        finally:
            if os.path.exists(path): os.unlink(path)

    def test_integrity_check_after_updates_and_deletes(self):
        fd, path = tempfile.mkstemp(suffix=".hdb")
        os.close(fd); os.unlink(path)
        try:
            db = Database(path)
            db.execute("CREATE COLUMN TABLE t (id INTEGER, v INTEGER)")
            for i in range(50):
                db.execute(f"INSERT INTO t VALUES ({i}, {i})")
            db.execute("DELETE FROM t WHERE id % 3 = 0")
            db.execute("UPDATE t SET v = -1 WHERE id % 5 = 0")
            result = db.execute("PRAGMA integrity_check").fetchall()
            db.close()
            self.assertEqual([r["integrity_check"] for r in result], ["ok"])
        finally:
            if os.path.exists(path): os.unlink(path)

    def test_integrity_check_detects_chunk_page_corruption(self):
        """Manually corrupt a chunk page byte and verify integrity_check catches it."""
        fd, path = tempfile.mkstemp(suffix=".hdb")
        os.close(fd); os.unlink(path)
        try:
            db = Database(path)
            db.execute("CREATE COLUMN TABLE t (id INTEGER, v REAL)")
            for i in range(10):
                db.execute(f"INSERT INTO t VALUES ({i}, {i * 1.0})")
            # Find the chunk page numbers by reading known pages list
            col_pages = db._collect_column_store_pages(db.tables["t"].root_page)
            db.close()

            # Corrupt a data byte in the first chunk page (not the checksum itself)
            chunk_pn = next(p for p in col_pages if p != col_pages[0])
            with open(path, "r+b") as f:
                f.seek(chunk_pn * 4096 + 20)  # inside entry data area
                f.write(b'\xFF\xFF\xFF\xFF')

            db2 = Database(path)
            result = db2.execute("PRAGMA integrity_check").fetchall()
            db2.close()
            statuses = [r["integrity_check"] for r in result]
            self.assertNotEqual(statuses, ["ok"])
            self.assertTrue(any("corrupt" in s.lower() or "page" in s.lower()
                                for s in statuses))
        finally:
            if os.path.exists(path): os.unlink(path)


class TestAggregateWhereFilter(unittest.TestCase):
    """Aggregates with WHERE on column tables stream in O(1) memory via scan_aggregate."""

    def setUp(self):
        self.db = _db()
        self.db.execute("CREATE COLUMN TABLE nums (id INTEGER, v REAL, tag TEXT)")
        for i in range(1, 11):
            tag = "even" if i % 2 == 0 else "odd"
            self.db.execute(f"INSERT INTO nums VALUES ({i}, {i * 10.0}, '{tag}')")

    def tearDown(self):
        self.db.close()

    def test_sum_with_where(self):
        row = self.db.execute(
            "SELECT SUM(v) AS s FROM nums WHERE id > 5"
        ).fetchone()
        # ids 6..10 → 60+70+80+90+100 = 400
        self.assertAlmostEqual(row["s"], 400.0)

    def test_count_star_with_where(self):
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM nums WHERE tag = 'even'"
        ).fetchone()
        self.assertEqual(row["n"], 5)

    def test_avg_with_where(self):
        row = self.db.execute(
            "SELECT AVG(v) AS a FROM nums WHERE id <= 4"
        ).fetchone()
        # ids 1..4 → 10+20+30+40 = 100, avg = 25
        self.assertAlmostEqual(row["a"], 25.0)

    def test_min_max_with_where(self):
        row = self.db.execute(
            "SELECT MIN(v) AS lo, MAX(v) AS hi FROM nums WHERE id % 2 = 0"
        ).fetchone()
        self.assertAlmostEqual(row["lo"], 20.0)
        self.assertAlmostEqual(row["hi"], 100.0)

    def test_multiple_aggregates_with_where(self):
        row = self.db.execute(
            "SELECT COUNT(*) AS n, SUM(v) AS s, AVG(v) AS a "
            "FROM nums WHERE v >= 50.0"
        ).fetchone()
        # ids 5..10 → 6 rows, sum=50+60+..+100=450, avg=75
        self.assertEqual(row["n"], 6)
        self.assertAlmostEqual(row["s"], 450.0)
        self.assertAlmostEqual(row["a"], 75.0)

    def test_where_matches_nothing(self):
        row = self.db.execute(
            "SELECT COUNT(*) AS n, SUM(v) AS s FROM nums WHERE id > 100"
        ).fetchone()
        self.assertEqual(row["n"], 0)
        self.assertIsNone(row["s"])

    def test_no_where_still_uses_fast_path(self):
        """Without WHERE, COUNT(*) must still use the O(1) header read."""
        row = self.db.execute("SELECT COUNT(*) AS n FROM nums").fetchone()
        self.assertEqual(row["n"], 10)


class TestGroupByPushdown(unittest.TestCase):
    """GROUP BY aggregates on column tables should be pushed down to scan_aggregate_grouped."""

    def setUp(self):
        self.db = _db()
        self.db.execute(
            "CREATE COLUMN TABLE sales "
            "(id INTEGER, region TEXT, product TEXT, revenue REAL, units INTEGER)"
        )
        rows = [
            (1,  "North", "Widget",   1200.0, 60),
            (2,  "South", "Gadget",    850.5, 34),
            (3,  "East",  "Widget",   2340.0, 117),
            (4,  "West",  "Gadget",    430.25, 17),
            (5,  "North", "Doohickey", 990.0, 33),
            (6,  "South", "Widget",   1750.0, 70),
            (7,  "East",  "Doohickey", 660.0, 22),
            (8,  "West",  "Widget",   3100.0, 155),
            (9,  "North", "Gadget",   1050.75, 42),
            (10, "South", "Doohickey", 220.0, 11),
        ]
        for r in rows:
            self.db.execute(f"INSERT INTO sales VALUES {r}")

    def tearDown(self):
        self.db.close()

    def test_group_by_sum(self):
        rows = self.db.execute(
            "SELECT region, SUM(revenue) AS rev FROM sales GROUP BY region ORDER BY region"
        ).fetchall()
        by_region = {r["region"]: r["rev"] for r in rows}
        self.assertAlmostEqual(by_region["North"], 3240.75)
        self.assertAlmostEqual(by_region["South"], 2820.5)
        self.assertAlmostEqual(by_region["East"],  3000.0)
        self.assertAlmostEqual(by_region["West"],  3530.25)

    def test_group_by_count_star(self):
        rows = self.db.execute(
            "SELECT region, COUNT(*) AS n FROM sales GROUP BY region ORDER BY region"
        ).fetchall()
        by_region = {r["region"]: r["n"] for r in rows}
        self.assertEqual(by_region["North"], 3)
        self.assertEqual(by_region["South"], 3)
        self.assertEqual(by_region["East"],  2)
        self.assertEqual(by_region["West"],  2)

    def test_group_by_avg(self):
        rows = self.db.execute(
            "SELECT product, AVG(revenue) AS avg_rev FROM sales GROUP BY product"
        ).fetchall()
        by_product = {r["product"]: r["avg_rev"] for r in rows}
        self.assertAlmostEqual(by_product["Widget"],    (1200+2340+1750+3100) / 4)
        self.assertAlmostEqual(by_product["Gadget"],    (850.5+430.25+1050.75) / 3)
        self.assertAlmostEqual(by_product["Doohickey"], (990+660+220) / 3)

    def test_group_by_min_max(self):
        rows = self.db.execute(
            "SELECT region, MIN(revenue) AS lo, MAX(revenue) AS hi "
            "FROM sales GROUP BY region ORDER BY region"
        ).fetchall()
        by_region = {r["region"]: r for r in rows}
        self.assertAlmostEqual(by_region["North"]["lo"], 990.0)
        self.assertAlmostEqual(by_region["North"]["hi"], 1200.0)
        self.assertAlmostEqual(by_region["West"]["lo"],  430.25)
        self.assertAlmostEqual(by_region["West"]["hi"],  3100.0)

    def test_group_by_multiple_aggregates(self):
        rows = self.db.execute(
            "SELECT region, COUNT(*) AS orders, SUM(units) AS total_units "
            "FROM sales GROUP BY region ORDER BY region"
        ).fetchall()
        north = next(r for r in rows if r["region"] == "North")
        self.assertEqual(north["orders"], 3)
        self.assertEqual(north["total_units"], 60 + 33 + 42)

    def test_group_by_with_having(self):
        rows = self.db.execute(
            "SELECT region, SUM(revenue) AS rev FROM sales "
            "GROUP BY region HAVING SUM(revenue) > 3000"
        ).fetchall()
        regions = {r["region"] for r in rows}
        self.assertIn("North", regions)   # 3240.75
        self.assertIn("West", regions)    # 3530.25
        self.assertNotIn("South", regions)  # 2820.5
        self.assertNotIn("East", regions)   # exactly 3000, not > 3000

    def test_group_by_falls_back_with_where(self):
        """GROUP BY with WHERE must still return correct results (scan_rows fallback)."""
        rows = self.db.execute(
            "SELECT region, SUM(revenue) AS rev FROM sales "
            "WHERE units > 30 GROUP BY region ORDER BY region"
        ).fetchall()
        by_region = {r["region"]: r["rev"] for r in rows}
        # Only rows with units > 30: ids 1,2,3,5,6,8,9
        self.assertAlmostEqual(by_region["North"], 1200 + 990 + 1050.75)
        self.assertAlmostEqual(by_region["South"], 850.5 + 1750)
        self.assertIn("East", by_region)
        self.assertIn("West", by_region)


class TestVacuum(unittest.TestCase):

    def test_vacuum_preserves_column_table_data(self):
        """VACUUM must copy column table rows via scan_rows, not deserialize_row."""
        fd, path = tempfile.mkstemp(suffix=".hdb")
        os.close(fd)
        os.unlink(path)
        try:
            db = Database(path)
            db.execute("CREATE COLUMN TABLE sales (id INTEGER, region TEXT, revenue REAL)")
            db.execute("INSERT INTO sales VALUES (1, 'North', 1200.0)")
            db.execute("INSERT INTO sales VALUES (2, 'South',  850.5)")
            db.execute("INSERT INTO sales VALUES (3, 'East',  2340.0)")
            db.execute("VACUUM")

            rows = db.execute("SELECT * FROM sales ORDER BY id").fetchall()
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0]["region"], "North")
            self.assertAlmostEqual(rows[0]["revenue"], 1200.0)
            self.assertEqual(rows[1]["region"], "South")
            self.assertAlmostEqual(rows[1]["revenue"], 850.5)
            self.assertEqual(rows[2]["region"], "East")
            self.assertAlmostEqual(rows[2]["revenue"], 2340.0)
            db.close()
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_vacuum_preserves_mixed_row_and_column_tables(self):
        """VACUUM with both row and column tables must preserve both correctly."""
        fd, path = tempfile.mkstemp(suffix=".hdb")
        os.close(fd)
        os.unlink(path)
        try:
            db = Database(path)
            db.execute("CREATE TABLE row_t (id INTEGER, name TEXT)")
            db.execute("CREATE COLUMN TABLE col_t (id INTEGER, score REAL)")
            db.execute("INSERT INTO row_t VALUES (1, 'Alice')")
            db.execute("INSERT INTO row_t VALUES (2, 'Bob')")
            db.execute("INSERT INTO col_t VALUES (1, 95.0)")
            db.execute("INSERT INTO col_t VALUES (2, 80.5)")
            db.execute("VACUUM")

            row_rows = db.execute("SELECT * FROM row_t ORDER BY id").fetchall()
            col_rows = db.execute("SELECT * FROM col_t ORDER BY id").fetchall()
            self.assertEqual(len(row_rows), 2)
            self.assertEqual(row_rows[0]["name"], "Alice")
            self.assertEqual(len(col_rows), 2)
            self.assertAlmostEqual(col_rows[1]["score"], 80.5)

            fmt = db.execute("SHOW STORAGE FORMAT").fetchall()
            fmt_map = {r["table"]: r["storage"] for r in fmt}
            self.assertEqual(fmt_map["row_t"], "ROW")
            self.assertEqual(fmt_map["col_t"], "COLUMN")
            db.close()
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_vacuum_preserves_aggregate_results_for_column_table(self):
        """Aggregates on a column table must return same results before and after VACUUM."""
        fd, path = tempfile.mkstemp(suffix=".hdb")
        os.close(fd)
        os.unlink(path)
        try:
            db = Database(path)
            db.execute("CREATE COLUMN TABLE nums (id INTEGER, v REAL)")
            for i in range(1, 11):
                db.execute(f"INSERT INTO nums VALUES ({i}, {i * 10.0})")

            before = db.execute(
                "SELECT COUNT(*) AS n, SUM(v) AS s, AVG(v) AS a FROM nums"
            ).fetchone()

            db.execute("VACUUM")

            after = db.execute(
                "SELECT COUNT(*) AS n, SUM(v) AS s, AVG(v) AS a FROM nums"
            ).fetchone()

            self.assertEqual(before["n"], after["n"])
            self.assertAlmostEqual(before["s"], after["s"])
            self.assertAlmostEqual(before["a"], after["a"])
            db.close()
        finally:
            if os.path.exists(path):
                os.unlink(path)


if __name__ == "__main__":
    unittest.main()
