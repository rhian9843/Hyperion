# Tests for: SELECT without FROM, batch statements, CTE, window functions
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
        if stripped:
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


# ── SELECT without FROM ───────────────────────────────────────────────────────

class TestSelectNoFrom(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_select_integer_literal(self):
        _, lines = db_run(["SELECT 1", ".exit"], self.db)
        self.assertTrue(any("1" in l for l in lines))

    def test_select_arithmetic(self):
        _, lines = db_run(["SELECT 3 + 4", ".exit"], self.db)
        self.assertTrue(any("7" in l for l in lines))

    def test_select_string_literal(self):
        _, lines = db_run(["SELECT 'hello'", ".exit"], self.db)
        self.assertTrue(any("hello" in l for l in lines))

    def test_select_with_alias(self):
        _, lines = db_run(["SELECT 42 AS answer", ".exit"], self.db)
        self.assertTrue(any("answer" in l for l in lines))
        self.assertTrue(any("42" in l for l in lines))

    def test_select_multiple_literals(self):
        _, lines = db_run(["SELECT 1, 2, 3", ".exit"], self.db)
        full = " ".join(lines)
        self.assertIn("1", full)
        self.assertIn("2", full)
        self.assertIn("3", full)

    def test_select_returns_one_row(self):
        _, lines = db_run(["SELECT 99", ".exit"], self.db)
        self.assertTrue(any("(1 row)" in l for l in lines))


# ── Batch statements ──────────────────────────────────────────────────────────

class TestBatchStatements(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE t (x INTEGER)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_two_inserts_in_one_line(self):
        db_run([
            "INSERT INTO t VALUES (10); INSERT INTO t VALUES (20)",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT x FROM t ORDER BY x", ".exit"], self.db)
        self.assertTrue(any("10" in l for l in lines))
        self.assertTrue(any("20" in l for l in lines))

    def test_insert_then_select_in_one_line(self):
        _, lines = db_run([
            "INSERT INTO t VALUES (7); SELECT x FROM t",
            ".exit",
        ], self.db)
        self.assertTrue(any("7" in l for l in lines))

    def test_multiple_selects_in_one_line(self):
        _, lines = db_run(["SELECT 1; SELECT 2", ".exit"], self.db)
        full = " ".join(lines)
        self.assertIn("1", full)
        self.assertIn("2", full)

    def test_batch_empty_parts_ignored(self):
        _, lines = db_run(["SELECT 5;; SELECT 6", ".exit"], self.db)
        full = " ".join(lines)
        self.assertIn("5", full)
        self.assertIn("6", full)


# ── CTE (WITH … AS) ───────────────────────────────────────────────────────────

class TestCTE(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE orders (id INTEGER, customer VARCHAR(32), amount INTEGER)",
            "INSERT INTO orders VALUES (1, Alice, 100)",
            "INSERT INTO orders VALUES (2, Bob, 50)",
            "INSERT INTO orders VALUES (3, Alice, 200)",
            "INSERT INTO orders VALUES (4, Carol, 75)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_cte_basic_filter(self):
        _, lines = db_run([
            "WITH big AS (SELECT id FROM orders WHERE amount > 75) SELECT id FROM big",
            ".exit",
        ], self.db)
        # ids 1 (100) and 3 (200) qualify; 2 (50) and 4 (75) do not
        self.assertTrue(any("1" in l for l in lines))
        self.assertTrue(any("3" in l for l in lines))

    def test_cte_row_count(self):
        _, lines = db_run([
            "WITH cte AS (SELECT id FROM orders) SELECT id FROM cte",
            ".exit",
        ], self.db)
        self.assertTrue(any("(4 rows)" in l for l in lines))

    def test_cte_outer_where(self):
        _, lines = db_run([
            "WITH all_o AS (SELECT id, amount FROM orders) "
            "SELECT id FROM all_o WHERE amount > 100",
            ".exit",
        ], self.db)
        # Only id=3 has amount 200 > 100
        self.assertTrue(any("(1 row)" in l for l in lines))
        self.assertTrue(any("3" in l for l in lines))

    def test_cte_with_order_by(self):
        _, lines = db_run([
            "WITH cte AS (SELECT id, amount FROM orders) "
            "SELECT id FROM cte ORDER BY amount DESC",
            ".exit",
        ], self.db)
        data = [l for l in lines if not l.startswith("(") and l != "id"
                and not all(c in "-+| " for c in l)]
        # id=3 (amount=200) should be first
        self.assertTrue(data[0].strip() == "3")


# ── Window functions ──────────────────────────────────────────────────────────

class TestWindowFunctions(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE emp (id INTEGER, name VARCHAR(32), dept_id INTEGER, salary INTEGER)",
            "INSERT INTO emp VALUES (1, Alice, 1, 70000)",
            "INSERT INTO emp VALUES (2, Bob, 1, 80000)",
            "INSERT INTO emp VALUES (3, Carol, 2, 60000)",
            "INSERT INTO emp VALUES (4, Dave, 2, 90000)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_row_number_basic(self):
        _, lines = db_run([
            "SELECT name, ROW_NUMBER() OVER (ORDER BY salary) AS rn FROM emp",
            ".exit",
        ], self.db)
        self.assertTrue(any("(4 rows)" in l for l in lines))
        self.assertTrue(any("Carol" in l for l in lines))
        self.assertTrue(any("Dave" in l for l in lines))

    def test_row_number_ordered_result(self):
        _, lines = db_run([
            "SELECT name, ROW_NUMBER() OVER (ORDER BY salary) AS rn FROM emp ORDER BY rn",
            ".exit",
        ], self.db)
        # Carol has lowest salary → rn=1, should appear before Dave (highest → rn=4)
        carol_pos = next((i for i, l in enumerate(lines) if "Carol" in l), None)
        dave_pos  = next((i for i, l in enumerate(lines) if "Dave" in l), None)
        self.assertIsNotNone(carol_pos)
        self.assertIsNotNone(dave_pos)
        self.assertLess(carol_pos, dave_pos)

    def test_rank_with_partition(self):
        _, lines = db_run([
            "SELECT name, RANK() OVER (PARTITION BY dept_id ORDER BY salary DESC) AS rnk "
            "FROM emp ORDER BY dept_id, rnk",
            ".exit",
        ], self.db)
        self.assertTrue(any("(4 rows)" in l for l in lines))
        self.assertTrue(any("Bob" in l for l in lines))
        self.assertTrue(any("Dave" in l for l in lines))

    def test_dense_rank(self):
        _, lines = db_run([
            "SELECT name, DENSE_RANK() OVER (ORDER BY dept_id) AS dr FROM emp ORDER BY id",
            ".exit",
        ], self.db)
        # dept_id 1 → dr=1 (Alice, Bob); dept_id 2 → dr=2 (Carol, Dave)
        self.assertTrue(any("(4 rows)" in l for l in lines))

    def test_lag(self):
        _, lines = db_run([
            "SELECT name, LAG(salary, 1) OVER (ORDER BY id) AS prev_sal FROM emp ORDER BY id",
            ".exit",
        ], self.db)
        # Alice (id=1) has no previous → NULL; Bob (id=2) prev_sal = 70000
        full = " ".join(lines)
        self.assertIn("NULL", full)
        self.assertIn("70000", full)

    def test_row_number_no_partition(self):
        _, lines = db_run([
            "SELECT name, ROW_NUMBER() OVER () AS rn FROM emp",
            ".exit",
        ], self.db)
        self.assertTrue(any("(4 rows)" in l for l in lines))


if __name__ == "__main__":
    unittest.main()
