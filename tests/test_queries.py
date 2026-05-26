# Tests for: multiple JOINs, implicit FROM (multi-table), INSERT SELECT, subquery in FROM
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


# ── Multiple JOINs ────────────────────────────────────────────────────────────

class TestMultipleJoins(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE authors (id INTEGER, name VARCHAR(64))",
            "CREATE TABLE books (id INTEGER, title VARCHAR(64), author_id INTEGER)",
            "CREATE TABLE reviews (id INTEGER, book_id INTEGER, score INTEGER)",
            "INSERT INTO authors VALUES (1, 'Tolkien')",
            "INSERT INTO authors VALUES (2, 'Orwell')",
            "INSERT INTO books VALUES (10, 'LOTR', 1)",
            "INSERT INTO books VALUES (20, '1984', 2)",
            "INSERT INTO reviews VALUES (100, 10, 5)",
            "INSERT INTO reviews VALUES (200, 20, 4)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_three_table_join(self):
        """Three-table join: authors JOIN books JOIN reviews."""
        _, lines = db_run([
            "SELECT a.name, b.title, r.score "
            "FROM authors a JOIN books b ON a.id = b.author_id "
            "JOIN reviews r ON b.id = r.book_id "
            "ORDER BY a.id",
            ".exit",
        ], self.db)
        self.assertTrue(any("Tolkien" in l for l in lines))
        self.assertTrue(any("LOTR" in l for l in lines))
        self.assertTrue(any("1984" in l for l in lines))
        self.assertTrue(any("Orwell" in l for l in lines))

    def test_three_table_join_filter(self):
        """Three-table join with WHERE filter."""
        _, lines = db_run([
            "SELECT a.name, r.score "
            "FROM authors a JOIN books b ON a.id = b.author_id "
            "JOIN reviews r ON b.id = r.book_id "
            "WHERE r.score = 5",
            ".exit",
        ], self.db)
        self.assertTrue(any("Tolkien" in l for l in lines))
        self.assertFalse(any("Orwell" in l for l in lines))

    def test_two_join_result_count(self):
        """Three-table join returns exactly 2 rows (one per book)."""
        _, lines = db_run([
            "SELECT a.name FROM authors a "
            "JOIN books b ON a.id = b.author_id "
            "JOIN reviews r ON b.id = r.book_id",
            ".exit",
        ], self.db)
        data = [l for l in lines if "---" not in l and not l.startswith("(")
                and l not in ("name", "a.name")]
        self.assertEqual(len(data), 2)


# ── Multi-table implicit FROM ─────────────────────────────────────────────────

class TestImplicitFrom(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE dept (id INTEGER, name VARCHAR(32))",
            "CREATE TABLE emp (id INTEGER, name VARCHAR(32), dept_id INTEGER)",
            "INSERT INTO dept VALUES (1, 'Engineering')",
            "INSERT INTO dept VALUES (2, 'Marketing')",
            "INSERT INTO emp VALUES (1, 'Alice', 1)",
            "INSERT INTO emp VALUES (2, 'Bob', 2)",
            "INSERT INTO emp VALUES (3, 'Carol', 1)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_two_table_implicit_join(self):
        """FROM a, b WHERE a.id = b.id acts as INNER JOIN."""
        _, lines = db_run([
            "SELECT emp.name, dept.name "
            "FROM emp, dept "
            "WHERE emp.dept_id = dept.id "
            "ORDER BY emp.id",
            ".exit",
        ], self.db)
        self.assertTrue(any("Alice" in l for l in lines))
        self.assertTrue(any("Engineering" in l for l in lines))
        self.assertTrue(any("Bob" in l for l in lines))
        self.assertTrue(any("Marketing" in l for l in lines))

    def test_implicit_join_filters_correctly(self):
        """Cross-table WHERE excludes unmatched pairs."""
        _, lines = db_run([
            "SELECT emp.name FROM emp, dept WHERE emp.dept_id = dept.id AND dept.name = Engineering",
            ".exit",
        ], self.db)
        self.assertTrue(any("Alice" in l for l in lines))
        self.assertTrue(any("Carol" in l for l in lines))
        self.assertFalse(any("Bob" in l for l in lines))

    def test_implicit_join_row_count(self):
        """3 employees × 2 departments = 6 rows in pure cross product; WHERE reduces to 3."""
        _, lines = db_run([
            "SELECT emp.name FROM emp, dept WHERE emp.dept_id = dept.id",
            ".exit",
        ], self.db)
        data = [l for l in lines if "---" not in l and not l.startswith("(")
                and l not in ("name", "emp.name")]
        self.assertEqual(len(data), 3)


# ── INSERT INTO … SELECT ──────────────────────────────────────────────────────

class TestInsertSelect(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE src (id INTEGER, name VARCHAR(32))",
            "CREATE TABLE dst (id INTEGER, name VARCHAR(32))",
            "INSERT INTO src VALUES (1, 'Alice')",
            "INSERT INTO src VALUES (2, 'Bob')",
            "INSERT INTO src VALUES (3, 'Carol')",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_insert_select_all(self):
        """INSERT INTO dst SELECT * FROM src copies all rows."""
        _, lines = db_run([
            "INSERT INTO dst SELECT * FROM src",
            "SELECT name FROM dst ORDER BY id",
            ".exit",
        ], self.db)
        self.assertTrue(any("Alice" in l for l in lines))
        self.assertTrue(any("Bob" in l for l in lines))
        self.assertTrue(any("Carol" in l for l in lines))

    def test_insert_select_with_where(self):
        """INSERT INTO dst SELECT * FROM src WHERE filters rows."""
        _, lines = db_run([
            "INSERT INTO dst SELECT * FROM src WHERE id < 3",
            "SELECT name FROM dst",
            ".exit",
        ], self.db)
        self.assertTrue(any("Alice" in l for l in lines))
        self.assertTrue(any("Bob" in l for l in lines))
        self.assertFalse(any("Carol" in l for l in lines))

    def test_insert_select_row_count_message(self):
        """INSERT INTO … SELECT reports inserted row count."""
        _, lines = db_run([
            "INSERT INTO dst SELECT * FROM src",
            ".exit",
        ], self.db)
        self.assertTrue(any("inserted" in l.lower() for l in lines))

    def test_insert_select_with_col_names(self):
        """INSERT INTO dst (id, name) SELECT id, name FROM src."""
        _, lines = db_run([
            "INSERT INTO dst (id, name) SELECT id, name FROM src WHERE id = 1",
            "SELECT name FROM dst",
            ".exit",
        ], self.db)
        self.assertTrue(any("Alice" in l for l in lines))


# ── Subquery in FROM (derived tables) ─────────────────────────────────────────

class TestSubqueryFrom(unittest.TestCase):
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

    def test_derived_table_basic(self):
        """SELECT * FROM (SELECT ...) AS sub returns inner rows."""
        _, lines = db_run([
            "SELECT sub.id FROM (SELECT id FROM orders WHERE amount > 75) AS sub",
            ".exit",
        ], self.db)
        # id=1 (100>75), id=3 (200>75) should appear; id=2 (50) and id=4 (75) should not
        self.assertTrue(any("1" in l for l in lines))
        self.assertTrue(any("3" in l for l in lines))

    def test_derived_table_with_outer_where(self):
        """Outer WHERE applied on derived table result."""
        _, lines = db_run([
            "SELECT sub.customer FROM (SELECT customer, amount FROM orders) AS sub "
            "WHERE sub.amount > 100",
            ".exit",
        ], self.db)
        self.assertTrue(any("Alice" in l for l in lines))  # amount=200
        self.assertFalse(any("Bob" in l for l in lines))

    def test_derived_table_star(self):
        """SELECT * FROM (SELECT ...) AS sub returns all projected columns."""
        _, lines = db_run([
            "SELECT * FROM (SELECT id, customer FROM orders WHERE id = 2) AS sub",
            ".exit",
        ], self.db)
        self.assertTrue(any("Bob" in l for l in lines))


if __name__ == "__main__":
    unittest.main()
