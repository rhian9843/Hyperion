# Tests for: ON UPDATE CASCADE/SET NULL, composite PRIMARY KEY,
#            LIMIT in UPDATE/DELETE, RETURNING clause
import os
import sys
import tempfile
import unittest
from pathlib import Path
from subprocess import PIPE, run

sys.path.insert(0, str(Path(__file__).parent.parent))

DATABASE_COMMAND = ["python3", "-m", "hyperion"]


def db_run(commands, db_path):
    result = run(
        DATABASE_COMMAND + [db_path],
        input="\n".join(commands) + "\n",
        stdout=PIPE, stderr=PIPE, encoding="utf-8",
    )
    lines = []
    for line in result.stdout.splitlines():
        stripped = line.removeprefix("H > ").strip()
        if stripped and stripped != "...":
            lines.append(stripped)
    return result.returncode, lines


class TempDB:
    def __enter__(self):
        self._f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._f.close()
        os.unlink(self._f.name)
        return self._f.name

    def __exit__(self, *_):
        try:
            os.unlink(self._f.name)
        except FileNotFoundError:
            pass


# ── ON UPDATE CASCADE ──────────────────────────────────────────────────────────

class TestOnUpdateCascade(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE parents (id INTEGER PRIMARY KEY, name VARCHAR(32))",
            "CREATE TABLE children (id INTEGER PRIMARY KEY, parent_id INTEGER "
            "REFERENCES parents(id) ON UPDATE CASCADE)",
            "INSERT INTO parents VALUES (1, Alice)",
            "INSERT INTO parents VALUES (2, Bob)",
            "INSERT INTO children VALUES (10, 1)",
            "INSERT INTO children VALUES (11, 1)",
            "INSERT INTO children VALUES (12, 2)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_cascade_updates_child_fk(self):
        """Updating the parent PK cascades to child FK columns."""
        db_run(["UPDATE parents SET id = 99 WHERE id = 1", ".exit"], self.db)
        _, lines = db_run(["SELECT parent_id FROM children ORDER BY id", ".exit"], self.db)
        full = " ".join(lines)
        self.assertIn("99", full)
        self.assertNotIn("1", full.replace("10", "").replace("11", "").replace("12", ""))

    def test_cascade_only_affects_related_children(self):
        """Cascade update only touches children of the updated parent."""
        db_run(["UPDATE parents SET id = 99 WHERE id = 1", ".exit"], self.db)
        _, lines = db_run([
            "SELECT parent_id FROM children WHERE id = 12",
            ".exit",
        ], self.db)
        full = " ".join(lines)
        self.assertIn("2", full)

    def test_cascade_no_error(self):
        """ON UPDATE CASCADE emits no FK error."""
        _, lines = db_run(["UPDATE parents SET id = 99 WHERE id = 1", ".exit"], self.db)
        self.assertFalse(any("Error" in l for l in lines))


# ── ON UPDATE SET NULL ─────────────────────────────────────────────────────────

class TestOnUpdateSetNull(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE depts (id INTEGER PRIMARY KEY, name VARCHAR(32))",
            "CREATE TABLE emps (id INTEGER PRIMARY KEY, dept_id INTEGER "
            "REFERENCES depts(id) ON UPDATE SET NULL)",
            "INSERT INTO depts VALUES (1, Engineering)",
            "INSERT INTO depts VALUES (2, Marketing)",
            "INSERT INTO emps VALUES (100, 1)",
            "INSERT INTO emps VALUES (101, 1)",
            "INSERT INTO emps VALUES (102, 2)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_set_null_on_parent_update(self):
        """Updating the parent PK sets FK column to NULL in child rows."""
        db_run(["UPDATE depts SET id = 99 WHERE id = 1", ".exit"], self.db)
        _, lines = db_run([
            "SELECT dept_id FROM emps WHERE id = 100",
            ".exit",
        ], self.db)
        full = " ".join(lines)
        self.assertIn("NULL", full)

    def test_set_null_preserves_child_row(self):
        """Child rows survive SET NULL on update — only FK is nulled."""
        db_run(["UPDATE depts SET id = 99 WHERE id = 1", ".exit"], self.db)
        _, lines = db_run(["SELECT id FROM emps ORDER BY id", ".exit"], self.db)
        full = " ".join(lines)
        self.assertIn("100", full)
        self.assertIn("101", full)

    def test_set_null_unrelated_row_unchanged(self):
        """Child referencing different parent is not affected."""
        db_run(["UPDATE depts SET id = 99 WHERE id = 1", ".exit"], self.db)
        _, lines = db_run(["SELECT dept_id FROM emps WHERE id = 102", ".exit"], self.db)
        full = " ".join(lines)
        self.assertIn("2", full)
        self.assertNotIn("NULL", full)

    def test_set_null_no_error(self):
        """ON UPDATE SET NULL emits no error."""
        _, lines = db_run(["UPDATE depts SET id = 99 WHERE id = 1", ".exit"], self.db)
        self.assertFalse(any("Error" in l for l in lines))


# ── ON UPDATE RESTRICT (default) ───────────────────────────────────────────────

class TestOnUpdateRestrict(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE p (id INTEGER PRIMARY KEY)",
            "CREATE TABLE c (id INTEGER PRIMARY KEY, p_id INTEGER REFERENCES p(id))",
            "INSERT INTO p VALUES (1)",
            "INSERT INTO c VALUES (10, 1)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_restrict_blocks_update(self):
        """Default RESTRICT prevents updating a referenced parent PK."""
        _, lines = db_run(["UPDATE p SET id = 99 WHERE id = 1", ".exit"], self.db)
        self.assertTrue(any("Error" in l or "FOREIGN KEY" in l for l in lines))


# ── Composite PRIMARY KEY ──────────────────────────────────────────────────────

class TestCompositePrimaryKey(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE orders (order_id INTEGER, item_id INTEGER, qty INTEGER, "
            "PRIMARY KEY (order_id, item_id))",
            "INSERT INTO orders VALUES (1, 10, 5)",
            "INSERT INTO orders VALUES (1, 20, 3)",
            "INSERT INTO orders VALUES (2, 10, 7)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_distinct_combinations_allowed(self):
        """Different (order_id, item_id) pairs are both accepted."""
        _, lines = db_run(["SELECT order_id FROM orders ORDER BY order_id", ".exit"], self.db)
        self.assertFalse(any("Error" in l for l in lines))
        full = " ".join(lines)
        self.assertIn("1", full)
        self.assertIn("2", full)

    def test_duplicate_combination_rejected(self):
        """Inserting a duplicate (order_id, item_id) pair raises an error."""
        _, lines = db_run([
            "INSERT INTO orders VALUES (1, 10, 99)",
            ".exit",
        ], self.db)
        self.assertTrue(any("Error" in l or "UNIQUE" in l for l in lines))

    def test_same_order_different_item_allowed(self):
        """Same order_id with a new item_id is accepted."""
        _, lines = db_run([
            "INSERT INTO orders VALUES (1, 30, 1)",
            "SELECT item_id FROM orders WHERE order_id = 1 ORDER BY item_id",
            ".exit",
        ], self.db)
        self.assertFalse(any("Error" in l for l in lines))
        full = " ".join(lines)
        self.assertIn("30", full)

    def test_pk_columns_not_null(self):
        """PK columns enforce NOT NULL — inserting NULL raises an error."""
        _, lines = db_run([
            "INSERT INTO orders VALUES (NULL, 10, 1)",
            ".exit",
        ], self.db)
        self.assertTrue(any("Error" in l or "NOT NULL" in l for l in lines))


# ── LIMIT in UPDATE ────────────────────────────────────────────────────────────

class TestUpdateLimit(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE t (id INTEGER, val VARCHAR(16))",
            "INSERT INTO t VALUES (1, a)",
            "INSERT INTO t VALUES (2, a)",
            "INSERT INTO t VALUES (3, a)",
            "INSERT INTO t VALUES (4, a)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_limit_restricts_rows_updated(self):
        """UPDATE ... LIMIT 2 modifies at most 2 rows."""
        db_run(["UPDATE t SET val = b LIMIT 2", ".exit"], self.db)
        _, lines = db_run(["SELECT val FROM t", ".exit"], self.db)
        b_count = sum(1 for l in lines if l.strip() == "b")
        a_count = sum(1 for l in lines if l.strip() == "a")
        self.assertEqual(b_count, 2)
        self.assertEqual(a_count, 2)

    def test_limit_zero_updates_nothing(self):
        """UPDATE ... LIMIT 0 leaves all rows unchanged."""
        db_run(["UPDATE t SET val = changed LIMIT 0", ".exit"], self.db)
        _, lines = db_run(["SELECT val FROM t", ".exit"], self.db)
        self.assertFalse(any("changed" in l for l in lines))

    def test_limit_larger_than_table_updates_all(self):
        """UPDATE ... LIMIT 100 updates all matching rows when table is smaller."""
        db_run(["UPDATE t SET val = z LIMIT 100", ".exit"], self.db)
        _, lines = db_run(["SELECT val FROM t", ".exit"], self.db)
        z_count = sum(1 for l in lines if "z" in l)
        self.assertEqual(z_count, 4)

    def test_limit_no_error(self):
        """UPDATE ... LIMIT produces no error."""
        _, lines = db_run(["UPDATE t SET val = b LIMIT 1", ".exit"], self.db)
        self.assertFalse(any("Error" in l for l in lines))


# ── LIMIT in DELETE ────────────────────────────────────────────────────────────

class TestDeleteLimit(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE t (id INTEGER, val VARCHAR(16))",
            "INSERT INTO t VALUES (1, x)",
            "INSERT INTO t VALUES (2, x)",
            "INSERT INTO t VALUES (3, x)",
            "INSERT INTO t VALUES (4, x)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_limit_restricts_rows_deleted(self):
        """DELETE ... LIMIT 2 removes exactly 2 rows."""
        db_run(["DELETE FROM t LIMIT 2", ".exit"], self.db)
        _, lines = db_run(["SELECT COUNT(*) FROM t", ".exit"], self.db)
        full = " ".join(lines)
        self.assertIn("2", full)

    def test_limit_with_where(self):
        """DELETE WHERE ... LIMIT 1 removes only 1 matching row."""
        db_run(["DELETE FROM t WHERE val = x LIMIT 1", ".exit"], self.db)
        _, lines = db_run(["SELECT id FROM t ORDER BY id", ".exit"], self.db)
        # 3 rows should remain
        ids = [l.strip() for l in lines if l.strip().isdigit()]
        self.assertEqual(len(ids), 3)

    def test_limit_zero_deletes_nothing(self):
        """DELETE ... LIMIT 0 leaves table untouched."""
        db_run(["DELETE FROM t LIMIT 0", ".exit"], self.db)
        _, lines = db_run(["SELECT COUNT(*) FROM t", ".exit"], self.db)
        full = " ".join(lines)
        self.assertIn("4", full)

    def test_limit_no_error(self):
        """DELETE ... LIMIT produces no error."""
        _, lines = db_run(["DELETE FROM t LIMIT 1", ".exit"], self.db)
        self.assertFalse(any("Error" in l for l in lines))


# ── RETURNING — INSERT ─────────────────────────────────────────────────────────

class TestInsertReturning(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE items (id INTEGER AUTOINCREMENT, name VARCHAR(32))",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_returning_id(self):
        """INSERT ... RETURNING id returns the auto-assigned id."""
        _, lines = db_run([
            "INSERT INTO items (name) VALUES (Widget) RETURNING id",
            ".exit",
        ], self.db)
        full = " ".join(lines)
        self.assertIn("1", full)

    def test_returning_multiple_cols(self):
        """INSERT ... RETURNING id, name returns both columns."""
        _, lines = db_run([
            "INSERT INTO items (name) VALUES (Gadget) RETURNING id, name",
            ".exit",
        ], self.db)
        full = " ".join(lines)
        self.assertIn("Gadget", full)
        self.assertIn("1", full)

    def test_returning_formats_as_table(self):
        """RETURNING output has a column header row."""
        _, lines = db_run([
            "INSERT INTO items (name) VALUES (Foo) RETURNING id",
            ".exit",
        ], self.db)
        self.assertTrue(any("id" in l for l in lines))

    def test_returning_no_error(self):
        """INSERT ... RETURNING produces no error."""
        _, lines = db_run([
            "INSERT INTO items (name) VALUES (Test) RETURNING id",
            ".exit",
        ], self.db)
        self.assertFalse(any("Error" in l for l in lines))


# ── RETURNING — UPDATE ─────────────────────────────────────────────────────────

class TestUpdateReturning(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE t (id INTEGER, val VARCHAR(32))",
            "INSERT INTO t VALUES (1, old)",
            "INSERT INTO t VALUES (2, old)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_returning_new_value(self):
        """UPDATE ... RETURNING val shows the new value."""
        _, lines = db_run([
            "UPDATE t SET val = new WHERE id = 1 RETURNING val",
            ".exit",
        ], self.db)
        full = " ".join(lines)
        self.assertIn("new", full)

    def test_returning_id_and_val(self):
        """UPDATE ... RETURNING id, val returns both columns."""
        _, lines = db_run([
            "UPDATE t SET val = updated WHERE id = 2 RETURNING id, val",
            ".exit",
        ], self.db)
        full = " ".join(lines)
        self.assertIn("2", full)
        self.assertIn("updated", full)

    def test_returning_no_error(self):
        """UPDATE ... RETURNING produces no error."""
        _, lines = db_run([
            "UPDATE t SET val = x WHERE id = 1 RETURNING id",
            ".exit",
        ], self.db)
        self.assertFalse(any("Error" in l for l in lines))


# ── RETURNING — DELETE ─────────────────────────────────────────────────────────

class TestDeleteReturning(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE t (id INTEGER, val VARCHAR(32))",
            "INSERT INTO t VALUES (1, alpha)",
            "INSERT INTO t VALUES (2, beta)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_returning_deleted_value(self):
        """DELETE ... RETURNING val shows the value of the deleted row."""
        _, lines = db_run([
            "DELETE FROM t WHERE id = 1 RETURNING val",
            ".exit",
        ], self.db)
        full = " ".join(lines)
        self.assertIn("alpha", full)

    def test_returning_id(self):
        """DELETE ... RETURNING id returns the id of the deleted row."""
        _, lines = db_run([
            "DELETE FROM t WHERE id = 2 RETURNING id",
            ".exit",
        ], self.db)
        full = " ".join(lines)
        self.assertIn("2", full)

    def test_returning_no_error(self):
        """DELETE ... RETURNING produces no error."""
        _, lines = db_run([
            "DELETE FROM t WHERE id = 1 RETURNING id",
            ".exit",
        ], self.db)
        self.assertFalse(any("Error" in l for l in lines))


if __name__ == "__main__":
    unittest.main()
