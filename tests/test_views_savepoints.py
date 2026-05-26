# Tests for: CREATE VIEW / DROP VIEW, SAVEPOINT / RELEASE / ROLLBACK TO SAVEPOINT
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


# ── CREATE VIEW ────────────────────────────────────────────────────────────────

class TestCreateView(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE employees (id INTEGER, name VARCHAR(32), dept VARCHAR(16), salary INTEGER)",
            "INSERT INTO employees VALUES (1, Alice, Engineering, 90000)",
            "INSERT INTO employees VALUES (2, Bob, Marketing, 70000)",
            "INSERT INTO employees VALUES (3, Carol, Engineering, 85000)",
            "INSERT INTO employees VALUES (4, Dave, Marketing, 60000)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_select_from_view(self):
        """SELECT from a view returns rows matching the view's WHERE."""
        db_run([
            "CREATE VIEW engineers AS SELECT id, name FROM employees WHERE dept = Engineering",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT name FROM engineers", ".exit"], self.db)
        full = " ".join(lines)
        self.assertIn("Alice", full)
        self.assertIn("Carol", full)
        self.assertNotIn("Bob", full)
        self.assertNotIn("Dave", full)

    def test_view_no_error(self):
        """CREATE VIEW produces no error."""
        _, lines = db_run([
            "CREATE VIEW v AS SELECT id FROM employees",
            ".exit",
        ], self.db)
        self.assertFalse(any("Error" in l for l in lines))

    def test_view_persists_across_sessions(self):
        """A created view is still queryable in a new session."""
        db_run([
            "CREATE VIEW hi_sal AS SELECT name FROM employees WHERE salary > 80000",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT name FROM hi_sal", ".exit"], self.db)
        full = " ".join(lines)
        self.assertIn("Alice", full)

    def test_view_if_not_exists(self):
        """CREATE VIEW IF NOT EXISTS does not error when view already exists."""
        db_run(["CREATE VIEW v AS SELECT id FROM employees", ".exit"], self.db)
        _, lines = db_run([
            "CREATE VIEW IF NOT EXISTS v AS SELECT name FROM employees",
            ".exit",
        ], self.db)
        self.assertFalse(any("Error" in l for l in lines))

    def test_view_duplicate_errors_without_if_not_exists(self):
        """Creating a duplicate view without IF NOT EXISTS raises an error."""
        db_run(["CREATE VIEW v AS SELECT id FROM employees", ".exit"], self.db)
        _, lines = db_run(["CREATE VIEW v AS SELECT name FROM employees", ".exit"], self.db)
        self.assertTrue(any("Error" in l or "exists" in l.lower() for l in lines))

    def test_view_with_outer_where(self):
        """Applying a WHERE on top of a view filters rows correctly."""
        db_run([
            "CREATE VIEW all_emp AS SELECT id, name, dept FROM employees",
            ".exit",
        ], self.db)
        _, lines = db_run([
            "SELECT name FROM all_emp WHERE dept = Marketing",
            ".exit",
        ], self.db)
        full = " ".join(lines)
        self.assertIn("Bob", full)
        self.assertNotIn("Alice", full)

    def test_create_or_replace_view(self):
        """CREATE OR REPLACE VIEW replaces an existing view definition."""
        db_run([
            "CREATE VIEW v AS SELECT id FROM employees WHERE dept = Engineering",
            ".exit",
        ], self.db)
        db_run([
            "CREATE OR REPLACE VIEW v AS SELECT id FROM employees WHERE dept = Marketing",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT id FROM v", ".exit"], self.db)
        full = " ".join(lines)
        # Marketing employees are id=2 and id=4; Engineering are 1 and 3
        self.assertIn("2", full)
        self.assertNotIn("1", full.replace("2", "").replace("4", ""))


# ── DROP VIEW ──────────────────────────────────────────────────────────────────

class TestDropView(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE t (id INTEGER, val VARCHAR(16))",
            "INSERT INTO t VALUES (1, hello)",
            "CREATE VIEW v AS SELECT id FROM t",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_drop_view_no_error(self):
        """DROP VIEW succeeds without error."""
        _, lines = db_run(["DROP VIEW v", ".exit"], self.db)
        self.assertFalse(any("Error" in l for l in lines))

    def test_drop_view_removes_it(self):
        """After DROP VIEW, querying the view raises an error."""
        db_run(["DROP VIEW v", ".exit"], self.db)
        _, lines = db_run(["SELECT id FROM v", ".exit"], self.db)
        self.assertTrue(any("Error" in l or "No such" in l for l in lines))

    def test_drop_view_if_exists(self):
        """DROP VIEW IF EXISTS on a non-existent view does not error."""
        _, lines = db_run(["DROP VIEW IF EXISTS nonexistent", ".exit"], self.db)
        self.assertFalse(any("Error" in l for l in lines))

    def test_drop_view_nonexistent_errors(self):
        """DROP VIEW on a non-existent view raises an error."""
        _, lines = db_run(["DROP VIEW nonexistent", ".exit"], self.db)
        self.assertTrue(any("Error" in l for l in lines))


# ── SAVEPOINT / RELEASE ────────────────────────────────────────────────────────

class TestSavepoint(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE t (id INTEGER, val VARCHAR(16))",
            "INSERT INTO t VALUES (1, original)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_rollback_to_savepoint_undoes_changes(self):
        """ROLLBACK TO SAVEPOINT reverts inserts made after the savepoint."""
        db_run([
            "BEGIN",
            "SAVEPOINT sp1",
            "INSERT INTO t VALUES (2, added)",
            "ROLLBACK TO SAVEPOINT sp1",
            "COMMIT",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT COUNT(*) FROM t", ".exit"], self.db)
        full = " ".join(lines)
        self.assertIn("1", full)

    def test_release_savepoint_keeps_changes(self):
        """RELEASE SAVEPOINT merges changes into the outer transaction."""
        db_run([
            "BEGIN",
            "SAVEPOINT sp1",
            "INSERT INTO t VALUES (2, kept)",
            "RELEASE SAVEPOINT sp1",
            "COMMIT",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT val FROM t WHERE id = 2", ".exit"], self.db)
        full = " ".join(lines)
        self.assertIn("kept", full)

    def test_savepoint_no_error(self):
        """SAVEPOINT / RELEASE emits no error."""
        _, lines = db_run([
            "BEGIN",
            "SAVEPOINT sp1",
            "RELEASE SAVEPOINT sp1",
            "COMMIT",
            ".exit",
        ], self.db)
        self.assertFalse(any("Error" in l for l in lines))

    def test_rollback_to_savepoint_no_error(self):
        """ROLLBACK TO SAVEPOINT emits no error."""
        _, lines = db_run([
            "BEGIN",
            "SAVEPOINT sp1",
            "ROLLBACK TO SAVEPOINT sp1",
            "COMMIT",
            ".exit",
        ], self.db)
        self.assertFalse(any("Error" in l for l in lines))

    def test_nested_savepoints(self):
        """Outer savepoint rollback undoes changes made in nested savepoints."""
        db_run([
            "BEGIN",
            "SAVEPOINT outer",
            "INSERT INTO t VALUES (2, inner1)",
            "SAVEPOINT inner",
            "INSERT INTO t VALUES (3, inner2)",
            "RELEASE SAVEPOINT inner",
            "ROLLBACK TO SAVEPOINT outer",
            "COMMIT",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT COUNT(*) FROM t", ".exit"], self.db)
        full = " ".join(lines)
        self.assertIn("1", full)

    def test_partial_rollback_nested(self):
        """Inner rollback undoes inner changes; outer changes survive."""
        db_run([
            "BEGIN",
            "SAVEPOINT sp1",
            "INSERT INTO t VALUES (2, outer_change)",
            "SAVEPOINT sp2",
            "INSERT INTO t VALUES (3, inner_change)",
            "ROLLBACK TO SAVEPOINT sp2",
            "COMMIT",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT val FROM t ORDER BY id", ".exit"], self.db)
        full = " ".join(lines)
        self.assertIn("outer_change", full)
        self.assertNotIn("inner_change", full)

    def test_nonexistent_savepoint_errors(self):
        """ROLLBACK TO a savepoint that was never set raises an error."""
        _, lines = db_run([
            "BEGIN",
            "ROLLBACK TO SAVEPOINT ghost",
            "ROLLBACK",
            ".exit",
        ], self.db)
        self.assertTrue(any("Error" in l or "savepoint" in l.lower() for l in lines))


if __name__ == "__main__":
    unittest.main()
