# Tests for: CREATE TABLE AS SELECT, UPSERT, TRUNCATE, ON DELETE CASCADE/SET NULL
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


def _data_rows(lines: list[str]) -> list[str]:
    """Return rows that appear after the header separator line."""
    past_sep = False
    rows = []
    for line in lines:
        if not past_sep:
            if set(line.replace("+", "").replace("-", "").replace("|", "").strip()) <= set():
                past_sep = True
            elif all(c in "-+| " for c in line):
                past_sep = True
        else:
            if line.startswith("(") and "row" in line:
                continue
            rows.append(line)
    return rows


# ── CREATE TABLE ... AS SELECT ─────────────────────────────────────────────────

class TestCreateTableAsSelect(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE src (id INTEGER, name VARCHAR(32), score REAL)",
            "INSERT INTO src VALUES (1, Alice, 9.5)",
            "INSERT INTO src VALUES (2, Bob, 7.0)",
            "INSERT INTO src VALUES (3, Carol, 8.5)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_creates_table_with_all_rows(self):
        """CREATE TABLE t2 AS SELECT * copies all rows."""
        _, lines = db_run([
            "CREATE TABLE copy AS SELECT * FROM src",
            "SELECT id FROM copy ORDER BY id",
            ".exit",
        ], self.db)
        full = " ".join(lines)
        self.assertIn("1", full)
        self.assertIn("2", full)
        self.assertIn("3", full)

    def test_creates_table_with_where_filter(self):
        """CREATE TABLE t2 AS SELECT ... WHERE ... copies only matching rows."""
        _, lines = db_run([
            "CREATE TABLE high AS SELECT id, name FROM src WHERE score > 8",
            "SELECT name FROM high ORDER BY name",
            ".exit",
        ], self.db)
        full = " ".join(lines)
        self.assertIn("Alice", full)
        self.assertIn("Carol", full)
        self.assertNotIn("Bob", full)

    def test_new_table_is_queryable(self):
        """Copied table supports INSERT and SELECT."""
        _, lines = db_run([
            "CREATE TABLE copy AS SELECT * FROM src",
            "INSERT INTO copy VALUES (4, Dave, 6.0)",
            "SELECT id FROM copy ORDER BY id",
            ".exit",
        ], self.db)
        full = " ".join(lines)
        self.assertIn("4", full)

    def test_if_not_exists_no_error(self):
        """CREATE TABLE IF NOT EXISTS t AS SELECT ... silently skips if table exists."""
        _, lines = db_run([
            "CREATE TABLE copy AS SELECT * FROM src",
            "CREATE TABLE IF NOT EXISTS copy AS SELECT * FROM src",
            ".exit",
        ], self.db)
        self.assertFalse(any("Error" in l for l in lines))

    def test_column_count_matches(self):
        """Copied table has the same number of columns as the source SELECT."""
        _, lines = db_run([
            "CREATE TABLE narrow AS SELECT id, name FROM src",
            "SELECT id, name FROM narrow ORDER BY id",
            ".exit",
        ], self.db)
        self.assertFalse(any("Error" in l for l in lines))
        full = " ".join(lines)
        self.assertIn("Alice", full)


# ── UPSERT — INSERT OR REPLACE / INSERT OR IGNORE / ON CONFLICT ────────────────

class TestInsertOrReplace(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE t (id INTEGER PRIMARY KEY, val VARCHAR(32))",
            "INSERT INTO t VALUES (1, original)",
            "INSERT INTO t VALUES (2, keep)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_replace_conflicting_row(self):
        """INSERT OR REPLACE overwrites the row with the same PK."""
        _, lines = db_run([
            "INSERT OR REPLACE INTO t VALUES (1, replaced)",
            "SELECT val FROM t WHERE id = 1",
            ".exit",
        ], self.db)
        full = " ".join(lines)
        self.assertIn("replaced", full)
        self.assertNotIn("original", full)

    def test_replace_non_conflicting_inserts(self):
        """INSERT OR REPLACE with a new PK simply inserts."""
        _, lines = db_run([
            "INSERT OR REPLACE INTO t VALUES (3, new)",
            "SELECT id FROM t ORDER BY id",
            ".exit",
        ], self.db)
        full = " ".join(lines)
        self.assertIn("3", full)

    def test_replace_preserves_other_rows(self):
        """INSERT OR REPLACE only removes the conflicting row, not others."""
        _, lines = db_run([
            "INSERT OR REPLACE INTO t VALUES (1, replaced)",
            "SELECT id FROM t ORDER BY id",
            ".exit",
        ], self.db)
        full = " ".join(lines)
        self.assertIn("2", full)

    def test_replace_row_count(self):
        """After replacing, table has correct row count."""
        db_run([
            "INSERT OR REPLACE INTO t VALUES (1, replaced)",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT id FROM t", ".exit"], self.db)
        self.assertTrue(any("(2 rows)" in l for l in lines))


class TestInsertOrIgnore(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE t (id INTEGER PRIMARY KEY, val VARCHAR(32))",
            "INSERT INTO t VALUES (1, original)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_ignore_conflicting_row(self):
        """INSERT OR IGNORE silently skips a row that violates UNIQUE."""
        _, lines = db_run([
            "INSERT OR IGNORE INTO t VALUES (1, new)",
            "SELECT val FROM t WHERE id = 1",
            ".exit",
        ], self.db)
        full = " ".join(lines)
        self.assertIn("original", full)
        self.assertNotIn("new", full)

    def test_ignore_no_error_output(self):
        """INSERT OR IGNORE produces no error message on conflict."""
        _, lines = db_run([
            "INSERT OR IGNORE INTO t VALUES (1, new)",
            ".exit",
        ], self.db)
        self.assertFalse(any("Error" in l for l in lines))

    def test_ignore_new_row_inserts(self):
        """INSERT OR IGNORE inserts when there is no conflict."""
        _, lines = db_run([
            "INSERT OR IGNORE INTO t VALUES (2, two)",
            "SELECT id FROM t ORDER BY id",
            ".exit",
        ], self.db)
        full = " ".join(lines)
        self.assertIn("2", full)


class TestOnConflict(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE t (id INTEGER PRIMARY KEY, val VARCHAR(32))",
            "INSERT INTO t VALUES (1, original)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_on_conflict_do_nothing(self):
        """ON CONFLICT DO NOTHING silently skips conflicting row."""
        _, lines = db_run([
            "INSERT INTO t VALUES (1, new) ON CONFLICT DO NOTHING",
            "SELECT val FROM t WHERE id = 1",
            ".exit",
        ], self.db)
        full = " ".join(lines)
        self.assertIn("original", full)
        self.assertNotIn("new", full)

    def test_on_conflict_do_nothing_no_error(self):
        """ON CONFLICT DO NOTHING emits no error."""
        _, lines = db_run([
            "INSERT INTO t VALUES (1, new) ON CONFLICT DO NOTHING",
            ".exit",
        ], self.db)
        self.assertFalse(any("Error" in l for l in lines))

    def test_on_conflict_do_update_set(self):
        """ON CONFLICT DO UPDATE SET updates the conflicting row."""
        _, lines = db_run([
            "INSERT INTO t VALUES (1, updated) ON CONFLICT DO UPDATE SET val = excluded.val",
            "SELECT val FROM t WHERE id = 1",
            ".exit",
        ], self.db)
        full = " ".join(lines)
        self.assertIn("updated", full)
        self.assertNotIn("original", full)

    def test_on_conflict_no_conflict_inserts(self):
        """ON CONFLICT DO NOTHING still inserts when there is no conflict."""
        _, lines = db_run([
            "INSERT INTO t VALUES (2, two) ON CONFLICT DO NOTHING",
            "SELECT id FROM t ORDER BY id",
            ".exit",
        ], self.db)
        full = " ".join(lines)
        self.assertIn("2", full)


# ── TRUNCATE TABLE ─────────────────────────────────────────────────────────────

class TestTruncate(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE t (id INTEGER, val VARCHAR(32))",
            "INSERT INTO t VALUES (1, a)",
            "INSERT INTO t VALUES (2, b)",
            "INSERT INTO t VALUES (3, c)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_truncate_removes_all_rows(self):
        """TRUNCATE TABLE removes all rows."""
        db_run(["TRUNCATE TABLE t", ".exit"], self.db)
        _, lines = db_run(["SELECT id FROM t", ".exit"], self.db)
        self.assertTrue(any("no rows" in l.lower() for l in lines))

    def test_truncate_no_error(self):
        """TRUNCATE TABLE emits no error."""
        _, lines = db_run(["TRUNCATE TABLE t", ".exit"], self.db)
        self.assertFalse(any("Error" in l for l in lines))

    def test_table_usable_after_truncate(self):
        """Table accepts new inserts after TRUNCATE."""
        db_run(["TRUNCATE TABLE t", ".exit"], self.db)
        _, lines = db_run([
            "INSERT INTO t VALUES (10, fresh)",
            "SELECT val FROM t",
            ".exit",
        ], self.db)
        full = " ".join(lines)
        self.assertIn("fresh", full)

    def test_truncate_then_count(self):
        """Row count is 0 immediately after TRUNCATE."""
        db_run(["TRUNCATE TABLE t", ".exit"], self.db)
        _, lines = db_run([
            "SELECT COUNT(*) FROM t",
            ".exit",
        ], self.db)
        full = " ".join(lines)
        self.assertIn("0", full)


# ── ON DELETE CASCADE / SET NULL ───────────────────────────────────────────────

class TestOnDeleteCascade(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE parents (id INTEGER PRIMARY KEY, name VARCHAR(32))",
            "CREATE TABLE children (id INTEGER PRIMARY KEY, parent_id INTEGER "
            "REFERENCES parents(id) ON DELETE CASCADE)",
            "INSERT INTO parents VALUES (1, Alice)",
            "INSERT INTO parents VALUES (2, Bob)",
            "INSERT INTO children VALUES (10, 1)",
            "INSERT INTO children VALUES (11, 1)",
            "INSERT INTO children VALUES (12, 2)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_cascade_deletes_children(self):
        """Deleting a parent cascades to its children."""
        db_run(["DELETE FROM parents WHERE id = 1", ".exit"], self.db)
        _, lines = db_run(["SELECT id FROM children ORDER BY id", ".exit"], self.db)
        full = " ".join(lines)
        self.assertNotIn("10", full)
        self.assertNotIn("11", full)

    def test_cascade_preserves_unrelated_children(self):
        """Cascading delete leaves children of other parents intact."""
        db_run(["DELETE FROM parents WHERE id = 1", ".exit"], self.db)
        _, lines = db_run(["SELECT id FROM children", ".exit"], self.db)
        full = " ".join(lines)
        self.assertIn("12", full)

    def test_cascade_no_error(self):
        """CASCADE delete emits no FK error."""
        _, lines = db_run(["DELETE FROM parents WHERE id = 1", ".exit"], self.db)
        self.assertFalse(any("Error" in l for l in lines))

    def test_parent_also_deleted(self):
        """The parent row itself is removed after CASCADE delete."""
        db_run(["DELETE FROM parents WHERE id = 1", ".exit"], self.db)
        _, lines = db_run(["SELECT id FROM parents ORDER BY id", ".exit"], self.db)
        # Only parent id=2 should remain; extract data rows only
        data = _data_rows(lines)
        self.assertEqual(len(data), 1)
        self.assertIn("2", data[0])


class TestOnDeleteSetNull(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE depts (id INTEGER PRIMARY KEY, name VARCHAR(32))",
            "CREATE TABLE emps (id INTEGER PRIMARY KEY, dept_id INTEGER "
            "REFERENCES depts(id) ON DELETE SET NULL)",
            "INSERT INTO depts VALUES (1, Engineering)",
            "INSERT INTO depts VALUES (2, Marketing)",
            "INSERT INTO emps VALUES (100, 1)",
            "INSERT INTO emps VALUES (101, 1)",
            "INSERT INTO emps VALUES (102, 2)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_set_null_on_parent_delete(self):
        """Deleting a parent sets FK column to NULL in child rows."""
        db_run(["DELETE FROM depts WHERE id = 1", ".exit"], self.db)
        _, lines = db_run([
            "SELECT dept_id FROM emps WHERE id = 100",
            ".exit",
        ], self.db)
        full = " ".join(lines)
        self.assertIn("NULL", full)

    def test_set_null_preserves_child_row(self):
        """Child rows are NOT deleted on SET NULL — only FK column is nulled."""
        db_run(["DELETE FROM depts WHERE id = 1", ".exit"], self.db)
        _, lines = db_run(["SELECT id FROM emps ORDER BY id", ".exit"], self.db)
        full = " ".join(lines)
        self.assertIn("100", full)
        self.assertIn("101", full)

    def test_set_null_unrelated_row_unchanged(self):
        """Child row referencing a different parent is not affected."""
        db_run(["DELETE FROM depts WHERE id = 1", ".exit"], self.db)
        _, lines = db_run(["SELECT dept_id FROM emps WHERE id = 102", ".exit"], self.db)
        full = " ".join(lines)
        self.assertIn("2", full)
        self.assertNotIn("NULL", full)

    def test_set_null_no_error(self):
        """ON DELETE SET NULL emits no FK error."""
        _, lines = db_run(["DELETE FROM depts WHERE id = 1", ".exit"], self.db)
        self.assertFalse(any("Error" in l for l in lines))


class TestOnDeleteRestrict(unittest.TestCase):
    """Verify that the default RESTRICT still works after the CASCADE/SET NULL additions."""

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

    def test_restrict_blocks_delete(self):
        """Default RESTRICT prevents deleting a referenced parent row."""
        _, lines = db_run(["DELETE FROM p WHERE id = 1", ".exit"], self.db)
        self.assertTrue(any("Error" in l or "FOREIGN KEY" in l for l in lines))


if __name__ == "__main__":
    unittest.main()
