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


class TestSubquery(unittest.TestCase):

    def _setup(self, db):
        db_run([
            "CREATE TABLE dept (id INTEGER, name VARCHAR(32))",
            "CREATE TABLE emp (id INTEGER, name VARCHAR(32), dept_id INTEGER, salary INTEGER)",
            "INSERT INTO dept VALUES (1, Engineering)",
            "INSERT INTO dept VALUES (2, Marketing)",
            "INSERT INTO dept VALUES (3, HR)",
            "INSERT INTO emp VALUES (1, Alice, 1, 90000)",
            "INSERT INTO emp VALUES (2, Bob, 2, 70000)",
            "INSERT INTO emp VALUES (3, Carol, 1, 80000)",
            "INSERT INTO emp VALUES (4, Dave, 3, 60000)",
            ".exit",
        ], db)

    def test_in_subquery(self):
        """WHERE col IN (SELECT ...) returns rows whose col matches any subquery result."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "SELECT name FROM emp WHERE dept_id IN (SELECT id FROM dept WHERE name = Engineering)",
                ".exit",
            ], db)
        self.assertTrue(any("Alice" in l for l in lines))
        self.assertTrue(any("Carol" in l for l in lines))
        self.assertFalse(any("Bob" in l for l in lines))
        self.assertFalse(any("Dave" in l for l in lines))

    def test_not_in_subquery(self):
        """WHERE col NOT IN (SELECT ...) excludes rows matching the subquery."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "SELECT name FROM emp WHERE dept_id NOT IN (SELECT id FROM dept WHERE name = Engineering)",
                ".exit",
            ], db)
        self.assertFalse(any("Alice" in l for l in lines))
        self.assertFalse(any("Carol" in l for l in lines))
        self.assertTrue(any("Bob" in l for l in lines))
        self.assertTrue(any("Dave" in l for l in lines))

    def test_scalar_subquery_equality(self):
        """WHERE col = (SELECT ...) compares against a single subquery value."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "SELECT name FROM emp WHERE dept_id = (SELECT id FROM dept WHERE name = Marketing)",
                ".exit",
            ], db)
        self.assertTrue(any("Bob" in l for l in lines))
        self.assertFalse(any("Alice" in l for l in lines))

    def test_exists_subquery(self):
        """EXISTS (SELECT ...) is True when the subquery returns any row."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "SELECT name FROM emp WHERE EXISTS (SELECT id FROM dept WHERE id = 1)",
                ".exit",
            ], db)
        self.assertTrue(any("Alice" in l for l in lines))
        self.assertTrue(any("Bob" in l for l in lines))

    def test_not_exists_subquery(self):
        """NOT EXISTS (SELECT ...) is True when the subquery returns no rows."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "SELECT name FROM emp WHERE NOT EXISTS (SELECT id FROM dept WHERE id = 999)",
                ".exit",
            ], db)
        self.assertTrue(any("Alice" in l for l in lines))
        self.assertTrue(any("Bob" in l for l in lines))

    def test_in_subquery_no_match(self):
        """IN subquery that returns no rows → no output rows."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "SELECT name FROM emp WHERE dept_id IN (SELECT id FROM dept WHERE id = 999)",
                ".exit",
            ], db)
        self.assertIn("(no rows)", lines)

    def test_subquery_in_delete(self):
        """DELETE WHERE col IN (SELECT ...) removes matching rows."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "DELETE FROM emp WHERE dept_id IN (SELECT id FROM dept WHERE name = HR)",
                "SELECT name FROM emp",
                ".exit",
            ], db)
        self.assertFalse(any("Dave" in l for l in lines))
        self.assertTrue(any("Alice" in l for l in lines))

    def test_subquery_in_update(self):
        """UPDATE WHERE col IN (SELECT ...) modifies only matching rows."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "UPDATE emp SET salary = 99999 WHERE dept_id IN (SELECT id FROM dept WHERE name = Marketing)",
                "SELECT salary FROM emp WHERE name = Bob",
                ".exit",
            ], db)
        self.assertTrue(any("99999" in l for l in lines))

    def test_scalar_subquery_comparison(self):
        """WHERE col > (SELECT ...) works with comparison operators."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "SELECT name FROM emp WHERE salary > (SELECT salary FROM emp WHERE name = Bob)",
                ".exit",
            ], db)
        self.assertTrue(any("Alice" in l for l in lines))
        self.assertFalse(any("Dave" in l for l in lines))

    def test_not_in_literal_list_still_works(self):
        """NOT IN with a literal value list still works after the refactor."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "SELECT name FROM emp WHERE id NOT IN (1, 3)",
                ".exit",
            ], db)
        self.assertFalse(any("Alice" in l for l in lines))
        self.assertFalse(any("Carol" in l for l in lines))
        self.assertTrue(any("Bob" in l for l in lines))
        self.assertTrue(any("Dave" in l for l in lines))


class TestCorrelatedSubquery(unittest.TestCase):

    def _setup(self, db):
        db_run([
            "CREATE TABLE emp (id INTEGER, name VARCHAR(32), dept_id INTEGER, salary INTEGER)",
            "CREATE TABLE orders (id INTEGER, emp_id INTEGER, amount INTEGER)",
            "INSERT INTO emp VALUES (1, Alice, 10, 90000)",
            "INSERT INTO emp VALUES (2, Bob,   20, 70000)",
            "INSERT INTO emp VALUES (3, Carol, 10, 80000)",
            "INSERT INTO emp VALUES (4, Dave,  30, 60000)",
            "INSERT INTO orders VALUES (1, 1, 500)",
            "INSERT INTO orders VALUES (2, 1, 300)",
            "INSERT INTO orders VALUES (3, 3, 200)",
            ".exit",
        ], db)

    def test_correlated_exists(self):
        """EXISTS subquery references the outer row's column."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "SELECT name FROM emp WHERE EXISTS (SELECT id FROM orders WHERE orders.emp_id = emp.id)",
                ".exit",
            ], db)
        self.assertTrue(any("Alice" in l for l in lines))
        self.assertTrue(any("Carol" in l for l in lines))
        self.assertFalse(any("Bob" in l for l in lines))
        self.assertFalse(any("Dave" in l for l in lines))

    def test_correlated_not_exists(self):
        """NOT EXISTS subquery correctly excludes rows that have a match."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "SELECT name FROM emp WHERE NOT EXISTS (SELECT id FROM orders WHERE orders.emp_id = emp.id)",
                ".exit",
            ], db)
        self.assertTrue(any("Bob" in l for l in lines))
        self.assertTrue(any("Dave" in l for l in lines))
        self.assertFalse(any("Alice" in l for l in lines))
        self.assertFalse(any("Carol" in l for l in lines))

    def test_correlated_in(self):
        """IN subquery with correlated reference to outer row."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "SELECT name FROM emp WHERE id IN (SELECT emp_id FROM orders WHERE orders.emp_id = emp.id)",
                ".exit",
            ], db)
        self.assertTrue(any("Alice" in l for l in lines))
        self.assertTrue(any("Carol" in l for l in lines))
        self.assertFalse(any("Bob" in l for l in lines))

    def test_correlated_scalar_equality(self):
        """Scalar correlated subquery used with = operator."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "SELECT name FROM emp WHERE dept_id = (SELECT dept_id FROM emp WHERE name = Alice)",
                ".exit",
            ], db)
        self.assertTrue(any("Alice" in l for l in lines))
        self.assertTrue(any("Carol" in l for l in lines))
        self.assertFalse(any("Bob" in l for l in lines))

    def test_qualified_col_in_inner_where(self):
        """Table-qualified column names in inner WHERE resolve against inner rows."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "SELECT name FROM emp WHERE EXISTS (SELECT id FROM orders WHERE orders.emp_id = emp.id AND orders.amount > 250)",
                ".exit",
            ], db)
        self.assertTrue(any("Alice" in l for l in lines))
        self.assertFalse(any("Carol" in l for l in lines))

    def test_correlated_row_count(self):
        """Correct number of rows returned from correlated EXISTS."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "SELECT name FROM emp WHERE EXISTS (SELECT id FROM orders WHERE orders.emp_id = emp.id)",
                ".exit",
            ], db)
        self.assertTrue(any("(2 rows)" in l for l in lines))


class TestSetOperations(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE a (id INTEGER, val VARCHAR(32))",
            "CREATE TABLE b (id INTEGER, val VARCHAR(32))",
            "INSERT INTO a VALUES (1, foo)",
            "INSERT INTO a VALUES (2, bar)",
            "INSERT INTO a VALUES (3, baz)",
            "INSERT INTO b VALUES (2, bar)",
            "INSERT INTO b VALUES (3, baz)",
            "INSERT INTO b VALUES (4, qux)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    # ── UNION ─────────────────────────────────────────────────────────────────

    def test_union_removes_duplicates(self):
        _, lines = db_run([
            "SELECT id, val FROM a UNION SELECT id, val FROM b",
            ".exit",
        ], self.db)
        # a∪b = {1,2,3,4} — 4 distinct rows
        self.assertTrue(any("(4 rows)" in l for l in lines))

    def test_union_all_keeps_duplicates(self):
        _, lines = db_run([
            "SELECT id, val FROM a UNION ALL SELECT id, val FROM b",
            ".exit",
        ], self.db)
        # 3 from a + 3 from b = 6 rows (including duplicates)
        self.assertTrue(any("(6 rows)" in l for l in lines))

    def test_union_contains_all_values(self):
        _, lines = db_run([
            "SELECT id, val FROM a UNION SELECT id, val FROM b",
            ".exit",
        ], self.db)
        for v in ("foo", "bar", "baz", "qux"):
            self.assertTrue(any(v in l for l in lines))

    # ── INTERSECT ─────────────────────────────────────────────────────────────

    def test_intersect_returns_common_rows(self):
        _, lines = db_run([
            "SELECT id, val FROM a INTERSECT SELECT id, val FROM b",
            ".exit",
        ], self.db)
        # a∩b = {(2,bar),(3,baz)}
        self.assertTrue(any("(2 rows)" in l for l in lines))
        self.assertTrue(any("bar" in l for l in lines))
        self.assertTrue(any("baz" in l for l in lines))

    def test_intersect_excludes_non_common(self):
        _, lines = db_run([
            "SELECT id, val FROM a INTERSECT SELECT id, val FROM b",
            ".exit",
        ], self.db)
        self.assertFalse(any("foo" in l for l in lines))
        self.assertFalse(any("qux" in l for l in lines))

    def test_intersect_empty_when_no_common(self):
        with TempDB() as db:
            db_run([
                "CREATE TABLE x (id INTEGER)",
                "CREATE TABLE y (id INTEGER)",
                "INSERT INTO x VALUES (1)",
                "INSERT INTO y VALUES (2)",
                ".exit",
            ], db)
            _, lines = db_run([
                "SELECT id FROM x INTERSECT SELECT id FROM y",
                ".exit",
            ], db)
        self.assertIn("(no rows)", lines)

    # ── EXCEPT ────────────────────────────────────────────────────────────────

    def test_except_removes_right_rows(self):
        _, lines = db_run([
            "SELECT id, val FROM a EXCEPT SELECT id, val FROM b",
            ".exit",
        ], self.db)
        # a - b = {(1,foo)}
        self.assertTrue(any("(1 row)" in l for l in lines))
        self.assertTrue(any("foo" in l for l in lines))

    def test_except_excludes_shared_rows(self):
        _, lines = db_run([
            "SELECT id, val FROM a EXCEPT SELECT id, val FROM b",
            ".exit",
        ], self.db)
        self.assertFalse(any("bar" in l for l in lines))
        self.assertFalse(any("baz" in l for l in lines))

    def test_except_all_multiset(self):
        """EXCEPT ALL removes one copy per right-side occurrence."""
        with TempDB() as db:
            db_run([
                "CREATE TABLE p (v INTEGER)",
                "CREATE TABLE q (v INTEGER)",
                "INSERT INTO p VALUES (1)",
                "INSERT INTO p VALUES (1)",
                "INSERT INTO p VALUES (2)",
                "INSERT INTO q VALUES (1)",
                ".exit",
            ], db)
            _, lines = db_run([
                "SELECT v FROM p EXCEPT ALL SELECT v FROM q",
                ".exit",
            ], db)
        # p has two 1s, q removes one → one 1 remains plus the 2 → 2 rows
        self.assertTrue(any("(2 rows)" in l for l in lines))

    # ── Chained set operations ─────────────────────────────────────────────────

    def test_union_then_except(self):
        """(a UNION b) EXCEPT b  should equal a (minus shared)."""
        _, lines = db_run([
            "SELECT id, val FROM a UNION SELECT id, val FROM b EXCEPT SELECT id, val FROM b",
            ".exit",
        ], self.db)
        # UNION gives {1,2,3,4}; EXCEPT b removes {2,3,4} → {1} = (1,foo)
        self.assertTrue(any("foo" in l for l in lines))
        self.assertFalse(any("qux" in l for l in lines))


class TestCorrelatedOuterRefLeftSide(unittest.TestCase):
    def test_exists_outer_ref_on_left(self):
        """EXISTS subquery where outer ref is on the left side of the condition."""
        with TempDB() as db:
            db_run([
                "CREATE TABLE dept (id INTEGER, name VARCHAR(32))",
                "CREATE TABLE emp (id INTEGER, name VARCHAR(32), dept_id INTEGER)",
                "INSERT INTO dept VALUES (1, Engineering)",
                "INSERT INTO emp VALUES (1, Alice, 1)",
                "INSERT INTO emp VALUES (2, Bob, 2)",
                ".exit",
            ], db)
            _, lines = db_run([
                "SELECT emp.name FROM emp INNER JOIN dept ON emp.dept_id = dept.id",
                ".exit",
            ], db)
        self.assertTrue(any("Alice" in l for l in lines))
        self.assertFalse(any("Bob" in l for l in lines))


class TestOffsetClause(unittest.TestCase):
    def test_limit_offset_skips_rows(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, a@x.com)",
                "INSERT INTO users VALUES (2, bob, b@x.com)",
                "INSERT INTO users VALUES (3, carol, c@x.com)",
                "SELECT * FROM users ORDER BY id ASC LIMIT 2 OFFSET 1",
                ".exit",
            ], db)
        self.assertFalse(any("alice" in l for l in lines))
        self.assertTrue(any("bob" in l for l in lines))
        self.assertTrue(any("carol" in l for l in lines))

    def test_offset_past_end_returns_no_rows(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, a@x.com)",
                "SELECT * FROM users ORDER BY id ASC LIMIT 10 OFFSET 5",
                ".exit",
            ], db)
        self.assertIn("(no rows)", lines)

    def test_offset_without_order_by(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, a@x.com)",
                "INSERT INTO users VALUES (2, bob, b@x.com)",
                "SELECT * FROM users LIMIT 1 OFFSET 1",
                ".exit",
            ], db)
        self.assertTrue(any("(1 row)" in l for l in lines))


class TestTableQualifiedProjection(unittest.TestCase):
    def test_select_table_dot_column(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, a@x.com)",
                "INSERT INTO users VALUES (2, bob, b@x.com)",
                "SELECT users.id, users.name FROM users",
                ".exit",
            ], db)
        self.assertTrue(any("alice" in l for l in lines))
        self.assertTrue(any("bob" in l for l in lines))

    def test_select_table_dot_column_with_where(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, a@x.com)",
                "INSERT INTO users VALUES (2, bob, b@x.com)",
                "SELECT users.name FROM users WHERE id = 1",
                ".exit",
            ], db)
        self.assertTrue(any("alice" in l for l in lines))
        self.assertFalse(any("bob" in l for l in lines))


if __name__ == "__main__":
    unittest.main()
