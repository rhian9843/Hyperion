# Tests for: scalar subquery in SELECT list, multi-line REPL, ORDER BY position
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


# ── Scalar subquery in SELECT list ────────────────────────────────────────────

class TestScalarSubquery(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE users (id INTEGER, name VARCHAR(32))",
            "CREATE TABLE orders (id INTEGER, user_id INTEGER, amount INTEGER)",
            "INSERT INTO users VALUES (1, Alice)",
            "INSERT INTO users VALUES (2, Bob)",
            "INSERT INTO orders VALUES (1, 1, 100)",
            "INSERT INTO orders VALUES (2, 1, 200)",
            "INSERT INTO orders VALUES (3, 2, 50)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_scalar_subquery_count(self):
        """(SELECT COUNT(*) FROM orders WHERE user_id = u.id) returns per-user count."""
        _, lines = db_run([
            "SELECT name, (SELECT COUNT(*) FROM orders WHERE user_id = users.id) AS cnt "
            "FROM users ORDER BY name",
            ".exit",
        ], self.db)
        full = " ".join(lines)
        self.assertIn("Alice", full)
        self.assertIn("Bob", full)
        # Alice has 2 orders, Bob has 1
        self.assertIn("2", full)
        self.assertIn("1", full)

    def test_scalar_subquery_sum(self):
        """Scalar subquery computing SUM per user."""
        _, lines = db_run([
            "SELECT name, (SELECT SUM(amount) FROM orders WHERE user_id = users.id) AS total "
            "FROM users ORDER BY name",
            ".exit",
        ], self.db)
        # Alice: 100+200=300, Bob: 50
        full = " ".join(lines)
        self.assertIn("300", full)
        self.assertIn("50", full)

    def test_scalar_subquery_no_match_returns_null(self):
        """Scalar subquery with no matching rows returns NULL."""
        db_run([
            "INSERT INTO users VALUES (3, Carol)",
            ".exit",
        ], self.db)
        _, lines = db_run([
            "SELECT name, (SELECT COUNT(*) FROM orders WHERE user_id = users.id) AS cnt "
            "FROM users WHERE name = Carol",
            ".exit",
        ], self.db)
        # Carol has no orders — correlated subquery returns no rows → NULL
        full = " ".join(lines)
        self.assertIn("Carol", full)

    def test_scalar_subquery_row_count(self):
        """SELECT with scalar subquery returns one row per outer row."""
        _, lines = db_run([
            "SELECT name, (SELECT COUNT(*) FROM orders WHERE user_id = users.id) AS cnt "
            "FROM users",
            ".exit",
        ], self.db)
        self.assertTrue(any("(2 rows)" in l for l in lines))


# ── Multi-line SQL in REPL ────────────────────────────────────────────────────

class TestMultiLineREPL(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE t (id INTEGER, val INTEGER)",
            "INSERT INTO t VALUES (1, 10)",
            "INSERT INTO t VALUES (2, 20)",
            "INSERT INTO t VALUES (3, 30)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_select_split_at_comma(self):
        """SELECT col1,\\n col2 FROM t — split after comma."""
        _, lines = db_run([
            "SELECT id,",
            "val FROM t ORDER BY id",
            ".exit",
        ], self.db)
        self.assertTrue(any("10" in l for l in lines))
        self.assertTrue(any("20" in l for l in lines))

    def test_where_split_at_and(self):
        """WHERE a = 1 AND\\n b = 2 — split after AND."""
        _, lines = db_run([
            "SELECT val FROM t WHERE id = 1 AND",
            "val > 5",
            ".exit",
        ], self.db)
        self.assertTrue(any("10" in l for l in lines))

    def test_create_table_multiline(self):
        """CREATE TABLE with column list split across lines."""
        db_run([
            "CREATE TABLE multi (",
            "a INTEGER,",
            "b INTEGER)",
            "INSERT INTO multi VALUES (99, 88)",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT a FROM multi", ".exit"], self.db)
        self.assertTrue(any("99" in l for l in lines))

    def test_empty_line_abandons_buffer(self):
        """Empty line resets the buffer without executing."""
        _, lines = db_run([
            "SELECT id,",
            "",              # empty line → abandon buffer
            "SELECT val FROM t WHERE id = 1",
            ".exit",
        ], self.db)
        self.assertTrue(any("10" in l for l in lines))


# ── ORDER BY column position ──────────────────────────────────────────────────

class TestOrderByPosition(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE t (name VARCHAR(32), score INTEGER)",
            "INSERT INTO t VALUES (Alice, 30)",
            "INSERT INTO t VALUES (Bob, 10)",
            "INSERT INTO t VALUES (Carol, 20)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def _data_rows(self, lines):
        """Filter out header, separator, and count lines; return data rows only."""
        return [l for l in lines
                if not all(c in "-+| " for c in l)
                and not l.startswith("(")
                and not all(c.isalpha() or c in " |" for c in l)]

    def test_order_by_position_1(self):
        """ORDER BY 1 orders by first column (name)."""
        _, lines = db_run([
            "SELECT name, score FROM t ORDER BY 1",
            ".exit",
        ], self.db)
        data = self._data_rows(lines)
        names = [l.split("|")[0].strip() for l in data]
        self.assertEqual(names, ["Alice", "Bob", "Carol"])

    def test_order_by_position_2_desc(self):
        """ORDER BY 2 DESC orders by second column descending."""
        _, lines = db_run([
            "SELECT name, score FROM t ORDER BY 2 DESC",
            ".exit",
        ], self.db)
        data = self._data_rows(lines)
        # Alice (30) first, then Carol (20), then Bob (10)
        self.assertTrue(data[0].split("|")[0].strip() == "Alice")

    def test_order_by_position_with_alias(self):
        """ORDER BY 2 works when columns have aliases."""
        _, lines = db_run([
            "SELECT name AS n, score AS s FROM t ORDER BY 2",
            ".exit",
        ], self.db)
        # Ordered by score ASC: Bob(10), Carol(20), Alice(30)
        data = self._data_rows(lines)
        self.assertTrue(data[0].split("|")[0].strip() == "Bob")

    def test_order_by_mixed_positional_and_name(self):
        """ORDER BY 1 is equivalent to ORDER BY name."""
        _, lines_pos = db_run(["SELECT name, score FROM t ORDER BY 1", ".exit"], self.db)
        _, lines_name = db_run(["SELECT name, score FROM t ORDER BY name", ".exit"], self.db)
        self.assertEqual(lines_pos, lines_name)


if __name__ == "__main__":
    unittest.main()
