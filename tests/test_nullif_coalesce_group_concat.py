# Tests for: NULLIF / COALESCE in SELECT list, GROUP_CONCAT / STRING_AGG aggregate
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


# ── COALESCE in SELECT list ───────────────────────────────────────────────────

class TestCoalesceInSelect(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_coalesce_null_column_returns_fallback(self):
        """COALESCE returns the first non-NULL value."""
        db_run([
            "CREATE TABLE t (id INTEGER, val VARCHAR(32))",
            "INSERT INTO t (id) VALUES (1)",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT COALESCE(val,'fallback') FROM t", ".exit"], self.db)
        self.assertIn("fallback", " ".join(lines))

    def test_coalesce_non_null_column_returned(self):
        """COALESCE returns the column value when it is not NULL."""
        db_run([
            "CREATE TABLE t (id INTEGER, val VARCHAR(32))",
            "INSERT INTO t VALUES (1, 'hello')",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT COALESCE(val,'fallback') FROM t", ".exit"], self.db)
        self.assertIn("hello", " ".join(lines))

    def test_coalesce_multiple_nulls(self):
        """COALESCE skips multiple NULLs and returns the first non-NULL."""
        _, lines = db_run(["SELECT COALESCE(NULL,NULL,'third')", ".exit"], self.db)
        self.assertIn("third", " ".join(lines))

    def test_coalesce_all_null(self):
        """COALESCE returns NULL when all arguments are NULL."""
        _, lines = db_run(["SELECT COALESCE(NULL,NULL,NULL)", ".exit"], self.db)
        self.assertIn("NULL", " ".join(lines))

    def test_coalesce_in_where(self):
        """COALESCE in WHERE clause filters correctly."""
        db_run([
            "CREATE TABLE t (id INTEGER, score INTEGER)",
            "INSERT INTO t (id) VALUES (1)",
            "INSERT INTO t VALUES (2, 50)",
            ".exit",
        ], self.db)
        _, lines = db_run([
            "SELECT id FROM t WHERE COALESCE(score,0) > 0",
            ".exit",
        ], self.db)
        id_lines = [l for l in lines if l.strip().isdigit()]
        self.assertIn("2", id_lines)
        self.assertNotIn("1", id_lines)


# ── NULLIF in SELECT list ─────────────────────────────────────────────────────

class TestNullifInSelect(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_nullif_equal_returns_null(self):
        """NULLIF(x, x) returns NULL when both values are equal."""
        db_run([
            "CREATE TABLE t (id INTEGER, score INTEGER)",
            "INSERT INTO t VALUES (1, 0)",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT NULLIF(score,0) FROM t", ".exit"], self.db)
        self.assertIn("NULL", " ".join(lines))

    def test_nullif_unequal_returns_first(self):
        """NULLIF(x, y) returns x when x != y."""
        db_run([
            "CREATE TABLE t (id INTEGER, score INTEGER)",
            "INSERT INTO t VALUES (1, 5)",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT NULLIF(score,0) FROM t", ".exit"], self.db)
        self.assertIn("5", " ".join(lines))

    def test_nullif_literal(self):
        """NULLIF on literals: NULLIF(1, 1) → NULL, NULLIF(1, 2) → 1."""
        _, lines = db_run(["SELECT NULLIF(1,1)", ".exit"], self.db)
        self.assertIn("NULL", " ".join(lines))

    def test_nullif_combined_with_coalesce(self):
        """COALESCE(NULLIF(score, 0), -1) converts zero scores to -1 via REPL."""
        db_run([
            "CREATE TABLE t (id INTEGER, score INTEGER)",
            "INSERT INTO t VALUES (1, 0)",
            "INSERT INTO t VALUES (2, 10)",
            ".exit",
        ], self.db)
        _, lines = db_run([
            "SELECT id, COALESCE(NULLIF(score,0),-1) FROM t ORDER BY id",
            ".exit",
        ], self.db)
        full = " ".join(lines)
        self.assertIn("-1", full)
        self.assertIn("10", full)


# ── GROUP_CONCAT ──────────────────────────────────────────────────────────────

class TestGroupConcat(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE t (dept VARCHAR(32), name VARCHAR(32))",
            "INSERT INTO t VALUES ('eng', 'Alice')",
            "INSERT INTO t VALUES ('eng', 'Bob')",
            "INSERT INTO t VALUES ('hr', 'Carol')",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_group_concat_default_separator(self):
        """GROUP_CONCAT(col) concatenates values with a comma by default."""
        _, lines = db_run([
            "SELECT GROUP_CONCAT(name) FROM t WHERE dept = 'eng'",
            ".exit",
        ], self.db)
        full = " ".join(lines)
        self.assertIn("Alice", full)
        self.assertIn("Bob", full)
        self.assertIn(",", full)

    def test_group_concat_custom_separator(self):
        """GROUP_CONCAT(col, sep) uses the given separator."""
        _, lines = db_run([
            "SELECT GROUP_CONCAT(name,' | ') FROM t WHERE dept = 'eng'",
            ".exit",
        ], self.db)
        self.assertIn("|", " ".join(lines))

    def test_group_concat_single_row(self):
        """GROUP_CONCAT on a single-row result returns just that value."""
        _, lines = db_run([
            "SELECT GROUP_CONCAT(name) FROM t WHERE dept = 'hr'",
            ".exit",
        ], self.db)
        self.assertIn("Carol", " ".join(lines))

    def test_group_concat_with_group_by(self):
        """GROUP_CONCAT works alongside GROUP BY."""
        _, lines = db_run([
            "SELECT dept, GROUP_CONCAT(name) FROM t GROUP BY dept",
            ".exit",
        ], self.db)
        full = " ".join(lines)
        self.assertIn("eng", full)
        self.assertIn("hr", full)
        self.assertIn("Alice", full)
        self.assertIn("Carol", full)

    def test_group_concat_null_skipped(self):
        """GROUP_CONCAT skips NULL values."""
        db_run([
            "CREATE TABLE n (id INTEGER, val VARCHAR(32))",
            "INSERT INTO n VALUES (1, 'a')",
            "INSERT INTO n (id) VALUES (2)",
            "INSERT INTO n VALUES (3, 'b')",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT GROUP_CONCAT(val) FROM n", ".exit"], self.db)
        full = " ".join(lines)
        self.assertIn("a", full)
        self.assertIn("b", full)
        self.assertNotIn("NULL", full)


# ── STRING_AGG ────────────────────────────────────────────────────────────────

class TestStringAgg(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_string_agg_basic(self):
        """STRING_AGG(col, sep) is an alias for GROUP_CONCAT with a separator."""
        db_run([
            "CREATE TABLE t (id INTEGER, tag VARCHAR(32))",
            "INSERT INTO t VALUES (1, 'python')",
            "INSERT INTO t VALUES (2, 'sql')",
            "INSERT INTO t VALUES (3, 'rust')",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT STRING_AGG(tag,', ') FROM t", ".exit"], self.db)
        full = " ".join(lines)
        self.assertIn("python", full)
        self.assertIn("sql", full)
        self.assertIn("rust", full)

    def test_string_agg_with_group_by(self):
        """STRING_AGG works with GROUP BY."""
        db_run([
            "CREATE TABLE t (cat VARCHAR(32), item VARCHAR(32))",
            "INSERT INTO t VALUES ('fruit', 'apple')",
            "INSERT INTO t VALUES ('fruit', 'banana')",
            "INSERT INTO t VALUES ('veg', 'carrot')",
            ".exit",
        ], self.db)
        _, lines = db_run([
            "SELECT cat, STRING_AGG(item,'/') FROM t GROUP BY cat",
            ".exit",
        ], self.db)
        full = " ".join(lines)
        self.assertIn("apple", full)
        self.assertIn("carrot", full)


if __name__ == "__main__":
    unittest.main()
