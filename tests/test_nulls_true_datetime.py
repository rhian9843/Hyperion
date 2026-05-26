# Tests for: NULLS FIRST/LAST, TRUE/FALSE literals, CURRENT_TIMESTAMP/DATE/TIME
import os
import re
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


# ── NULLS FIRST / NULLS LAST ──────────────────────────────────────────────────

class TestNullsOrdering(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE t (name VARCHAR(32), score INTEGER)",
            "INSERT INTO t VALUES (Alice, 10)",
            "INSERT INTO t VALUES (Bob, NULL)",
            "INSERT INTO t VALUES (Carol, 20)",
            "INSERT INTO t VALUES (Dave, NULL)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def _data_rows(self, lines):
        """Return data rows only (after the separator line, before the row count)."""
        data = []
        past_sep = False
        for l in lines:
            if all(c in "-+| " for c in l):
                past_sep = True
                continue
            if l.startswith("("):
                continue
            if not past_sep:
                continue
            data.append(l)
        return data

    def test_nulls_last_default(self):
        """Default ORDER BY puts NULLs last."""
        _, lines = db_run([
            "SELECT name, score FROM t ORDER BY score",
            ".exit",
        ], self.db)
        data = self._data_rows(lines)
        # non-null first: Alice(10), Carol(20), then NULLs
        scores = [l.split("|")[1].strip() for l in data]
        self.assertNotEqual(scores[0], "NULL")
        self.assertNotEqual(scores[1], "NULL")
        self.assertEqual(scores[-1], "NULL")
        self.assertEqual(scores[-2], "NULL")

    def test_nulls_last_explicit(self):
        """ORDER BY score NULLS LAST puts NULLs last."""
        _, lines = db_run([
            "SELECT name, score FROM t ORDER BY score NULLS LAST",
            ".exit",
        ], self.db)
        data = self._data_rows(lines)
        scores = [l.split("|")[1].strip() for l in data]
        self.assertEqual(scores[-1], "NULL")
        self.assertEqual(scores[-2], "NULL")

    def test_nulls_first(self):
        """ORDER BY score NULLS FIRST puts NULLs at the top."""
        _, lines = db_run([
            "SELECT name, score FROM t ORDER BY score NULLS FIRST",
            ".exit",
        ], self.db)
        data = self._data_rows(lines)
        scores = [l.split("|")[1].strip() for l in data]
        self.assertEqual(scores[0], "NULL")
        self.assertEqual(scores[1], "NULL")

    def test_nulls_first_desc(self):
        """ORDER BY score DESC NULLS FIRST keeps NULLs first."""
        _, lines = db_run([
            "SELECT name, score FROM t ORDER BY score DESC NULLS FIRST",
            ".exit",
        ], self.db)
        data = self._data_rows(lines)
        scores = [l.split("|")[1].strip() for l in data]
        self.assertEqual(scores[0], "NULL")
        self.assertEqual(scores[1], "NULL")
        # non-null should be descending after NULLs
        self.assertEqual(scores[2], "20")
        self.assertEqual(scores[3], "10")

    def test_nulls_last_desc(self):
        """ORDER BY score DESC NULLS LAST: non-null descending, NULLs at end."""
        _, lines = db_run([
            "SELECT name, score FROM t ORDER BY score DESC NULLS LAST",
            ".exit",
        ], self.db)
        data = self._data_rows(lines)
        scores = [l.split("|")[1].strip() for l in data]
        self.assertEqual(scores[0], "20")
        self.assertEqual(scores[1], "10")
        self.assertEqual(scores[-1], "NULL")


# ── TRUE / FALSE literals ─────────────────────────────────────────────────────

class TestTrueFalseLiterals(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE flags (name VARCHAR(32), active INTEGER)",
            "INSERT INTO flags VALUES (Alice, 1)",
            "INSERT INTO flags VALUES (Bob, 0)",
            "INSERT INTO flags VALUES (Carol, 1)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_where_equals_true(self):
        """WHERE active = TRUE filters rows where active = 1."""
        _, lines = db_run([
            "SELECT name FROM flags WHERE active = TRUE",
            ".exit",
        ], self.db)
        full = " ".join(lines)
        self.assertIn("Alice", full)
        self.assertIn("Carol", full)
        self.assertNotIn("Bob", full)

    def test_where_equals_false(self):
        """WHERE active = FALSE filters rows where active = 0."""
        _, lines = db_run([
            "SELECT name FROM flags WHERE active = FALSE",
            ".exit",
        ], self.db)
        full = " ".join(lines)
        self.assertIn("Bob", full)
        self.assertNotIn("Alice", full)
        self.assertNotIn("Carol", full)

    def test_select_true_expression(self):
        """SELECT TRUE returns 1."""
        _, lines = db_run(["SELECT TRUE", ".exit"], self.db)
        self.assertTrue(any("1" in l for l in lines))

    def test_select_false_expression(self):
        """SELECT FALSE returns 0."""
        _, lines = db_run(["SELECT FALSE", ".exit"], self.db)
        self.assertTrue(any("0" in l for l in lines))

    def test_true_false_arithmetic(self):
        """SELECT TRUE + FALSE returns 1."""
        _, lines = db_run(["SELECT TRUE + FALSE", ".exit"], self.db)
        self.assertTrue(any("1" in l for l in lines))


# ── CURRENT_TIMESTAMP / CURRENT_DATE / CURRENT_TIME ──────────────────────────

class TestCurrentDateTime(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_current_timestamp_format(self):
        """SELECT CURRENT_TIMESTAMP returns ISO-format datetime string."""
        _, lines = db_run(["SELECT CURRENT_TIMESTAMP", ".exit"], self.db)
        full = " ".join(lines)
        # Should match YYYY-MM-DD HH:MM:SS
        self.assertRegex(full, r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}')

    def test_current_date_format(self):
        """SELECT CURRENT_DATE returns YYYY-MM-DD."""
        _, lines = db_run(["SELECT CURRENT_DATE", ".exit"], self.db)
        full = " ".join(lines)
        self.assertRegex(full, r'\d{4}-\d{2}-\d{2}')

    def test_current_time_format(self):
        """SELECT CURRENT_TIME returns HH:MM:SS."""
        _, lines = db_run(["SELECT CURRENT_TIME", ".exit"], self.db)
        full = " ".join(lines)
        self.assertRegex(full, r'\d{2}:\d{2}:\d{2}')

    def test_current_timestamp_in_insert(self):
        """CURRENT_TIMESTAMP can be used in INSERT DEFAULT-like expressions via SELECT."""
        db_run([
            "CREATE TABLE events (name VARCHAR(32), ts VARCHAR(32))",
            ".exit",
        ], self.db)
        # Insert using INSERT INTO ... SELECT
        db_run([
            "INSERT INTO events SELECT 'login', CURRENT_TIMESTAMP",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT ts FROM events", ".exit"], self.db)
        full = " ".join(lines)
        self.assertRegex(full, r'\d{4}-\d{2}-\d{2}')

    def test_current_date_no_from(self):
        """SELECT CURRENT_DATE works without a FROM clause."""
        _, lines = db_run(["SELECT CURRENT_DATE", ".exit"], self.db)
        self.assertTrue(any(re.search(r'\d{4}-\d{2}-\d{2}', l) for l in lines))


if __name__ == "__main__":
    unittest.main()
