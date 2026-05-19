# test suite for Hyperion
import os
import tempfile
import unittest
from subprocess import PIPE, run

DATABASE_COMMAND = ["python3", "-m", "hyperion"]

CREATE_USERS = "CREATE TABLE users (id INTEGER, name VARCHAR(32), email VARCHAR(255))"


def db_run(commands, db_path):
    """Run a list of SQL commands against a database file and return stdout lines."""
    result = run(
        DATABASE_COMMAND + [db_path],
        input="\n".join(commands) + "\n",
        stdout=PIPE,
        stderr=PIPE,
        encoding="utf-8",
    )
    lines = []
    for line in result.stdout.splitlines():
        stripped = line.removeprefix("H > ").strip()
        if stripped:
            lines.append(stripped)
    return result.returncode, lines


class TempDB:
    """Context manager that provides a fresh temporary database path."""
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


class TestJoin(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            CREATE_USERS,
            "CREATE TABLE orders (uid INTEGER, item VARCHAR(64))",
            "INSERT INTO users VALUES (1, alice, alice@example.com)",
            "INSERT INTO users VALUES (2, bob, bob@example.com)",
            "INSERT INTO orders VALUES (1, widget)",
            "INSERT INTO orders VALUES (1, gadget)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_inner_join_basic(self):
        _, lines = db_run([
            "SELECT users.name, orders.item FROM users INNER JOIN orders ON users.id = orders.uid",
            ".exit",
        ], self.db)
        self.assertTrue(any("alice" in l and "widget" in l for l in lines))

    def test_inner_join_no_match(self):
        _, lines = db_run([
            "SELECT users.name, orders.item FROM users INNER JOIN orders ON users.id = orders.uid WHERE users.id = 2",
            ".exit",
        ], self.db)
        self.assertIn("(no rows)", lines)

    def test_inner_join_row_count(self):
        _, lines = db_run([
            "SELECT users.name, orders.item FROM users INNER JOIN orders ON users.id = orders.uid",
            ".exit",
        ], self.db)
        self.assertTrue(any("(2 rows)" in l for l in lines))


class TestSelfJoin(unittest.TestCase):
    """Self join — a table joined with itself using aliases."""

    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        # employees: id, name, manager_id (NULL for the top-level manager)
        db_run([
            "CREATE TABLE employees (id INTEGER, name VARCHAR(32), manager_id INTEGER)",
            "INSERT INTO employees VALUES (1, alice, NULL)",
            "INSERT INTO employees VALUES (2, bob, 1)",
            "INSERT INTO employees VALUES (3, carol, 1)",
            "INSERT INTO employees VALUES (4, dave, 2)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_self_join_finds_manager_names(self):
        """Each employee paired with their manager's name."""
        _, lines = db_run([
            "SELECT e.name, m.name FROM employees AS e INNER JOIN employees AS m ON e.manager_id = m.id",
            ".exit",
        ], self.db)
        self.assertTrue(any("bob" in l and "alice" in l for l in lines))
        self.assertTrue(any("carol" in l and "alice" in l for l in lines))
        self.assertTrue(any("dave" in l and "bob" in l for l in lines))

    def test_self_join_excludes_top_level(self):
        """alice has no manager (NULL manager_id) so must not appear as an employee here."""
        _, lines = db_run([
            "SELECT e.name, m.name FROM employees AS e INNER JOIN employees AS m ON e.manager_id = m.id",
            ".exit",
        ], self.db)
        emp_lines = [l for l in lines if "|" in l and "name" not in l]
        self.assertFalse(any(l.split("|")[0].strip() == "alice" for l in emp_lines))

    def test_self_join_row_count(self):
        """3 employees have a manager (bob, carol, dave)."""
        _, lines = db_run([
            "SELECT e.name, m.name FROM employees AS e INNER JOIN employees AS m ON e.manager_id = m.id",
            ".exit",
        ], self.db)
        self.assertTrue(any("(3 rows)" in l for l in lines))

    def test_self_left_join_includes_top_level(self):
        """LEFT JOIN keeps alice (no manager) with NULL for the manager name."""
        _, lines = db_run([
            "SELECT e.name, m.name FROM employees AS e LEFT JOIN employees AS m ON e.manager_id = m.id",
            ".exit",
        ], self.db)
        self.assertTrue(any("alice" in l and "NULL" in l for l in lines))
        self.assertTrue(any("(4 rows)" in l for l in lines))

    def test_alias_without_as_keyword(self):
        """Bare alias (no AS keyword) should also work."""
        _, lines = db_run([
            "SELECT e.name, m.name FROM employees e INNER JOIN employees m ON e.manager_id = m.id",
            ".exit",
        ], self.db)
        self.assertTrue(any("(3 rows)" in l for l in lines))


class TestLeftJoin(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            CREATE_USERS,
            "CREATE TABLE orders (uid INTEGER, item VARCHAR(64))",
            "INSERT INTO users VALUES (1, alice, alice@example.com)",
            "INSERT INTO users VALUES (2, bob, bob@example.com)",
            "INSERT INTO users VALUES (3, carol, carol@example.com)",
            "INSERT INTO orders VALUES (1, widget)",
            "INSERT INTO orders VALUES (1, gadget)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_left_join_includes_unmatched_left_rows(self):
        """Users with no orders must appear with NULL item."""
        _, lines = db_run([
            "SELECT users.name, orders.item FROM users LEFT JOIN orders ON users.id = orders.uid",
            ".exit",
        ], self.db)
        self.assertTrue(any("bob" in l for l in lines))
        self.assertTrue(any("carol" in l for l in lines))
        self.assertTrue(any("NULL" in l for l in lines))

    def test_left_join_matched_rows_present(self):
        """Matched rows from the right side still appear."""
        _, lines = db_run([
            "SELECT users.name, orders.item FROM users LEFT JOIN orders ON users.id = orders.uid",
            ".exit",
        ], self.db)
        self.assertTrue(any("alice" in l and "widget" in l for l in lines))
        self.assertTrue(any("alice" in l and "gadget" in l for l in lines))

    def test_left_join_row_count(self):
        """2 matched (alice×2) + 1 unmatched (bob) + 1 unmatched (carol) = 4 rows."""
        _, lines = db_run([
            "SELECT users.name, orders.item FROM users LEFT JOIN orders ON users.id = orders.uid",
            ".exit",
        ], self.db)
        self.assertTrue(any("(4 rows)" in l for l in lines))

    def test_left_outer_join_synonym(self):
        """LEFT OUTER JOIN is identical to LEFT JOIN."""
        _, lines = db_run([
            "SELECT users.name, orders.item FROM users LEFT OUTER JOIN orders ON users.id = orders.uid",
            ".exit",
        ], self.db)
        self.assertTrue(any("(4 rows)" in l for l in lines))

    def test_inner_join_unchanged(self):
        """Existing INNER JOIN behaviour must not regress."""
        _, lines = db_run([
            "SELECT users.name, orders.item FROM users INNER JOIN orders ON users.id = orders.uid",
            ".exit",
        ], self.db)
        self.assertTrue(any("(2 rows)" in l for l in lines))
        self.assertFalse(any("bob" in l for l in lines))


class TestRightFullCrossNaturalJoin(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            CREATE_USERS,
            "CREATE TABLE orders (uid INTEGER, item VARCHAR(64))",
            "INSERT INTO users VALUES (1, alice, alice@example.com)",
            "INSERT INTO users VALUES (2, bob, bob@example.com)",
            "INSERT INTO orders VALUES (1, widget)",
            "INSERT INTO orders VALUES (3, gadget)",   # uid=3 has no matching user
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    # ── RIGHT JOIN ────────────────────────────────────────────────────────────

    def test_right_join_includes_unmatched_right_rows(self):
        """orders row with uid=3 (no matching user) must appear with NULL user columns."""
        _, lines = db_run([
            "SELECT users.name, orders.item FROM users RIGHT JOIN orders ON users.id = orders.uid",
            ".exit",
        ], self.db)
        self.assertTrue(any("gadget" in l for l in lines))
        self.assertTrue(any("NULL" in l for l in lines))

    def test_right_join_excludes_unmatched_left_rows(self):
        """bob (uid=2, no order) must NOT appear in RIGHT JOIN."""
        _, lines = db_run([
            "SELECT users.name, orders.item FROM users RIGHT JOIN orders ON users.id = orders.uid",
            ".exit",
        ], self.db)
        self.assertFalse(any("bob" in l for l in lines))

    def test_right_join_matched_rows_present(self):
        _, lines = db_run([
            "SELECT users.name, orders.item FROM users RIGHT JOIN orders ON users.id = orders.uid",
            ".exit",
        ], self.db)
        self.assertTrue(any("alice" in l and "widget" in l for l in lines))

    def test_right_outer_join_synonym(self):
        _, lines = db_run([
            "SELECT users.name, orders.item FROM users RIGHT OUTER JOIN orders ON users.id = orders.uid",
            ".exit",
        ], self.db)
        self.assertTrue(any("gadget" in l for l in lines))

    # ── FULL OUTER JOIN ───────────────────────────────────────────────────────

    def test_full_outer_join_includes_both_unmatched_sides(self):
        """bob (no order) and gadget (no user) must both appear."""
        _, lines = db_run([
            "SELECT users.name, orders.item FROM users FULL OUTER JOIN orders ON users.id = orders.uid",
            ".exit",
        ], self.db)
        self.assertTrue(any("bob" in l for l in lines))
        self.assertTrue(any("gadget" in l for l in lines))

    def test_full_outer_join_matched_rows_present(self):
        _, lines = db_run([
            "SELECT users.name, orders.item FROM users FULL OUTER JOIN orders ON users.id = orders.uid",
            ".exit",
        ], self.db)
        self.assertTrue(any("alice" in l and "widget" in l for l in lines))

    def test_full_join_row_count(self):
        """1 matched + 1 unmatched-left (bob) + 1 unmatched-right (gadget) = 3 rows."""
        _, lines = db_run([
            "SELECT users.name, orders.item FROM users FULL OUTER JOIN orders ON users.id = orders.uid",
            ".exit",
        ], self.db)
        self.assertTrue(any("(3 rows)" in l for l in lines))

    # ── CROSS JOIN ────────────────────────────────────────────────────────────

    def test_cross_join_cartesian_product(self):
        """2 users × 2 orders = 4 rows."""
        _, lines = db_run([
            "SELECT users.name, orders.item FROM users CROSS JOIN orders",
            ".exit",
        ], self.db)
        self.assertTrue(any("(4 rows)" in l for l in lines))

    def test_cross_join_all_combinations_present(self):
        _, lines = db_run([
            "SELECT users.name, orders.item FROM users CROSS JOIN orders",
            ".exit",
        ], self.db)
        self.assertTrue(any("alice" in l and "widget" in l for l in lines))
        self.assertTrue(any("alice" in l and "gadget" in l for l in lines))
        self.assertTrue(any("bob" in l and "widget" in l for l in lines))

    # ── NATURAL JOIN ──────────────────────────────────────────────────────────

    def test_natural_join_matches_on_shared_columns(self):
        """Tables sharing 'id' column: NATURAL JOIN should join on it."""
        with TempDB() as db:
            db_run([
                "CREATE TABLE a (id INTEGER, val VARCHAR(32))",
                "CREATE TABLE b (id INTEGER, label VARCHAR(32))",
                "INSERT INTO a VALUES (1, foo)",
                "INSERT INTO a VALUES (2, bar)",
                "INSERT INTO b VALUES (1, alpha)",
                "INSERT INTO b VALUES (3, gamma)",
                ".exit",
            ], db)
            _, lines = db_run([
                "SELECT a.val, b.label FROM a NATURAL JOIN b",
                ".exit",
            ], db)
        self.assertTrue(any("foo" in l and "alpha" in l for l in lines))
        self.assertFalse(any("bar" in l for l in lines))   # id=2 has no match
        self.assertFalse(any("gamma" in l for l in lines)) # id=3 has no match

    def test_natural_join_row_count(self):
        with TempDB() as db:
            db_run([
                "CREATE TABLE a (id INTEGER, val VARCHAR(32))",
                "CREATE TABLE b (id INTEGER, label VARCHAR(32))",
                "INSERT INTO a VALUES (1, foo)",
                "INSERT INTO a VALUES (2, bar)",
                "INSERT INTO b VALUES (1, alpha)",
                "INSERT INTO b VALUES (1, beta)",
                ".exit",
            ], db)
            _, lines = db_run([
                "SELECT a.val, b.label FROM a NATURAL JOIN b",
                ".exit",
            ], db)
        self.assertTrue(any("(2 rows)" in l for l in lines))


if __name__ == "__main__":
    unittest.main()
