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


class TestWhere(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            CREATE_USERS,
            "INSERT INTO users VALUES (1, alice, alice@example.com)",
            "INSERT INTO users VALUES (2, bob, bob@other.com)",
            "INSERT INTO users VALUES (3, carol, carol@example.com)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_where_equals(self):
        _, lines = db_run(["SELECT * FROM users WHERE id = 1", ".exit"], self.db)
        self.assertTrue(any("alice" in l for l in lines))
        self.assertFalse(any("bob" in l for l in lines))

    def test_where_greater_than(self):
        _, lines = db_run(["SELECT * FROM users WHERE id > 1", ".exit"], self.db)
        self.assertFalse(any("alice" in l for l in lines))
        self.assertTrue(any("(2 rows)" in l for l in lines))

    def test_where_like(self):
        _, lines = db_run(
            ["SELECT * FROM users WHERE email LIKE %example.com", ".exit"], self.db
        )
        self.assertTrue(any("alice" in l for l in lines))
        self.assertTrue(any("carol" in l for l in lines))
        self.assertFalse(any("bob" in l for l in lines))

    def test_where_no_match(self):
        _, lines = db_run(["SELECT * FROM users WHERE id = 999", ".exit"], self.db)
        self.assertIn("(no rows)", lines)


class TestWhereOrIn(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            CREATE_USERS,
            "INSERT INTO users VALUES (1, alice, alice@example.com)",
            "INSERT INTO users VALUES (2, bob, bob@example.com)",
            "INSERT INTO users VALUES (3, carol, carol@example.com)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_or_two_conditions(self):
        _, lines = db_run(["SELECT * FROM users WHERE id = 1 OR id = 3", ".exit"], self.db)
        self.assertTrue(any("alice" in l for l in lines))
        self.assertTrue(any("carol" in l for l in lines))
        self.assertFalse(any("bob" in l for l in lines))

    def test_or_three_conditions(self):
        _, lines = db_run(["SELECT * FROM users WHERE id = 1 OR id = 2 OR id = 3", ".exit"], self.db)
        self.assertTrue(any("(3 rows)" in l for l in lines))

    def test_in_integers(self):
        _, lines = db_run(["SELECT * FROM users WHERE id IN (1, 3)", ".exit"], self.db)
        self.assertTrue(any("alice" in l for l in lines))
        self.assertTrue(any("carol" in l for l in lines))
        self.assertFalse(any("bob" in l for l in lines))

    def test_in_strings(self):
        _, lines = db_run(["SELECT * FROM users WHERE name IN (alice, carol)", ".exit"], self.db)
        self.assertTrue(any("alice" in l for l in lines))
        self.assertTrue(any("carol" in l for l in lines))
        self.assertFalse(any("bob" in l for l in lines))

    def test_in_no_match(self):
        _, lines = db_run(["SELECT * FROM users WHERE id IN (99, 100)", ".exit"], self.db)
        self.assertIn("(no rows)", lines)

    def test_and_then_or(self):
        _, lines = db_run(
            ["SELECT * FROM users WHERE id = 1 AND name = alice OR id = 3", ".exit"], self.db
        )
        self.assertTrue(any("alice" in l for l in lines))
        self.assertTrue(any("carol" in l for l in lines))
        self.assertFalse(any("bob" in l for l in lines))

    def test_or_falls_back_to_full_scan_with_index(self):
        db_run(["CREATE INDEX idx_id ON users(id)", ".exit"], self.db)
        _, lines = db_run(["SELECT * FROM users WHERE id = 1 OR id = 2", ".exit"], self.db)
        self.assertTrue(any("(2 rows)" in l for l in lines))

    def test_delete_with_in(self):
        _, lines = db_run([
            "DELETE FROM users WHERE id IN (1, 2)",
            "SELECT * FROM users",
            ".exit",
        ], self.db)
        self.assertFalse(any("alice" in l for l in lines))
        self.assertFalse(any("bob" in l for l in lines))
        self.assertTrue(any("carol" in l for l in lines))

    def test_update_with_or(self):
        _, lines = db_run([
            "UPDATE users SET name=updated WHERE id = 1 OR id = 2",
            "SELECT * FROM users WHERE name = updated",
            ".exit",
        ], self.db)
        self.assertTrue(any("(2 rows)" in l for l in lines))


class TestNull(unittest.TestCase):
    def test_explicit_null_stored_and_displayed(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users (id, name) VALUES (1, alice)",
                "SELECT * FROM users",
                ".exit",
            ], db)
        self.assertTrue(any("NULL" in l for l in lines))

    def test_null_keyword_in_values(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, NULL, alice@example.com)",
                "SELECT * FROM users",
                ".exit",
            ], db)
        self.assertTrue(any("NULL" in l for l in lines))

    def test_where_is_null(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users (id, name) VALUES (1, alice)",
                "INSERT INTO users VALUES (2, bob, bob@example.com)",
                "SELECT * FROM users WHERE email IS NULL",
                ".exit",
            ], db)
        self.assertTrue(any("alice" in l for l in lines))
        self.assertFalse(any("bob" in l for l in lines))

    def test_where_is_not_null(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users (id, name) VALUES (1, alice)",
                "INSERT INTO users VALUES (2, bob, bob@example.com)",
                "SELECT * FROM users WHERE email IS NOT NULL",
                ".exit",
            ], db)
        self.assertFalse(any("alice" in l for l in lines))
        self.assertTrue(any("bob" in l for l in lines))

    def test_not_null_constraint(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER NOT NULL, val VARCHAR(32))",
                "INSERT INTO t VALUES (NULL, hello)",
                ".exit",
            ], db)
        self.assertTrue(any("Error" in l for l in lines))

    def test_null_not_matched_by_equals(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users (id, name) VALUES (1, alice)",
                "SELECT * FROM users WHERE email = NULL",
                ".exit",
            ], db)
        self.assertIn("(no rows)", lines)


class TestOrderByLimit(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            CREATE_USERS,
            "INSERT INTO users VALUES (3, carol, carol@example.com)",
            "INSERT INTO users VALUES (1, alice, alice@example.com)",
            "INSERT INTO users VALUES (2, bob, bob@example.com)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    @staticmethod
    def _data_lines(lines, skip_header):
        """Return data-only lines: skip header, separator (all dashes/plus), row count."""
        return [
            l.strip() for l in lines
            if l.strip()
            and l.strip() != skip_header
            and not all(c in "-+" for c in l.strip())
            and "row" not in l
        ]

    def test_order_by_asc(self):
        _, lines = db_run(["SELECT name FROM users ORDER BY name ASC", ".exit"], self.db)
        self.assertEqual(self._data_lines(lines, "name"), ["alice", "bob", "carol"])

    def test_order_by_desc(self):
        _, lines = db_run(["SELECT name FROM users ORDER BY name DESC", ".exit"], self.db)
        self.assertEqual(self._data_lines(lines, "name"), ["carol", "bob", "alice"])

    def test_order_by_integer(self):
        _, lines = db_run(["SELECT id FROM users ORDER BY id ASC", ".exit"], self.db)
        self.assertEqual(self._data_lines(lines, "id"), ["1", "2", "3"])

    def test_limit(self):
        _, lines = db_run(["SELECT * FROM users ORDER BY id ASC LIMIT 2", ".exit"], self.db)
        self.assertTrue(any("(2 rows)" in l for l in lines))
        self.assertFalse(any("carol" in l for l in lines))

    def test_limit_1(self):
        _, lines = db_run(["SELECT * FROM users ORDER BY id ASC LIMIT 1", ".exit"], self.db)
        self.assertTrue(any("(1 row)" in l for l in lines))
        self.assertTrue(any("alice" in l for l in lines))

    def test_order_by_with_where(self):
        _, lines = db_run(
            ["SELECT name FROM users WHERE id > 1 ORDER BY name DESC", ".exit"], self.db
        )
        self.assertEqual(self._data_lines(lines, "name"), ["carol", "bob"])

    def test_nulls_last(self):
        db_run(["INSERT INTO users (id, name) VALUES (4, dave)", ".exit"], self.db)
        _, lines = db_run(["SELECT email FROM users ORDER BY email ASC", ".exit"], self.db)
        data = self._data_lines(lines, "email")
        self.assertEqual(data[-1], "NULL")


class TestDistinct(unittest.TestCase):

    def test_distinct_removes_duplicates(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, dept VARCHAR(32))",
                "INSERT INTO t VALUES (1, eng)",
                "INSERT INTO t VALUES (2, eng)",
                "INSERT INTO t VALUES (3, hr)",
                "SELECT DISTINCT dept FROM t",
                ".exit",
            ], db)
        dept_lines = [l for l in lines if "eng" in l or "hr" in l]
        self.assertEqual(len(dept_lines), 2)

    def test_distinct_all_unique_unchanged(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, a@x.com)",
                "INSERT INTO users VALUES (2, bob, b@x.com)",
                "SELECT DISTINCT name FROM users",
                ".exit",
            ], db)
        self.assertTrue(any("alice" in l for l in lines))
        self.assertTrue(any("bob" in l for l in lines))

    def test_distinct_with_where(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, dept VARCHAR(32), active INTEGER)",
                "INSERT INTO t VALUES (1, eng, 1)",
                "INSERT INTO t VALUES (2, eng, 1)",
                "INSERT INTO t VALUES (3, hr,  0)",
                "SELECT DISTINCT dept FROM t WHERE active = 1",
                ".exit",
            ], db)
        dept_lines = [l for l in lines if "eng" in l or "hr" in l]
        self.assertEqual(len(dept_lines), 1)
        self.assertTrue(any("eng" in l for l in lines))

    def test_distinct_multi_column(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (a INTEGER, b INTEGER)",
                "INSERT INTO t VALUES (1, 1)",
                "INSERT INTO t VALUES (1, 2)",
                "INSERT INTO t VALUES (1, 1)",
                "SELECT DISTINCT a, b FROM t",
                ".exit",
            ], db)
        row_lines = [l for l in lines if "|" in l and "a" not in l]
        self.assertEqual(len(row_lines), 2)


class TestUnknownColumn(unittest.TestCase):
    def test_where_unknown_column_raises_error(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "SELECT * FROM users WHERE nonexistent = 1",
                ".exit",
            ], db)
        self.assertTrue(any("Error" in l or "error" in l for l in lines))

    def test_where_valid_column_does_not_raise(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "SELECT * FROM users WHERE id = 1",
                ".exit",
            ], db)
        self.assertTrue(any("alice" in l for l in lines))


class TestColumnAlias(unittest.TestCase):
    def test_select_as_renames_header(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, a@x.com)",
                "SELECT id AS user_id, name AS full_name FROM users",
                ".exit",
            ], db)
        self.assertTrue(any("user_id" in l for l in lines))
        self.assertTrue(any("full_name" in l for l in lines))
        self.assertTrue(any("alice" in l for l in lines))

    def test_select_as_does_not_include_as_keyword_in_output(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, a@x.com)",
                "SELECT name AS n FROM users",
                ".exit",
            ], db)
        self.assertFalse(any(l.strip() == "AS" for l in lines))
        self.assertTrue(any("n" in l for l in lines))


class TestParenthesizedWhere(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            CREATE_USERS,
            "INSERT INTO users VALUES (1, alice, a@x.com)",
            "INSERT INTO users VALUES (2, bob, b@x.com)",
            "INSERT INTO users VALUES (3, carol, c@x.com)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_paren_or_then_and(self):
        """(a=1 OR a=2) AND name=alice  → only alice."""
        _, lines = db_run([
            "SELECT name FROM users WHERE (id = 1 OR id = 2) AND name = alice",
            ".exit",
        ], self.db)
        self.assertTrue(any("alice" in l for l in lines))
        self.assertFalse(any("bob" in l for l in lines))

    def test_without_parens_different_result(self):
        """id=1 OR (id=2 AND name=alice) — without parens OR has lowest precedence."""
        _, lines = db_run([
            "SELECT name FROM users WHERE id = 1 OR id = 2 AND name = alice",
            ".exit",
        ], self.db)
        # id=1 matches alice; id=2 AND name=alice matches nothing → just alice
        self.assertTrue(any("alice" in l for l in lines))
        self.assertFalse(any("bob" in l for l in lines))

    def test_paren_changes_precedence(self):
        """(id=1 OR id=2) AND name=bob → only bob (id 2)."""
        _, lines = db_run([
            "SELECT name FROM users WHERE (id = 1 OR id = 2) AND name = bob",
            ".exit",
        ], self.db)
        self.assertFalse(any("alice" in l for l in lines))
        self.assertTrue(any("bob" in l for l in lines))

    def test_nested_parens(self):
        """((id=1 OR id=2) OR id=3) → all three rows."""
        _, lines = db_run([
            "SELECT name FROM users WHERE ((id = 1 OR id = 2) OR id = 3)",
            ".exit",
        ], self.db)
        self.assertTrue(any("alice" in l for l in lines))
        self.assertTrue(any("bob" in l for l in lines))
        self.assertTrue(any("carol" in l for l in lines))

    def test_paren_group_with_and_outside(self):
        """(id=2 OR id=3) AND id != 3 → only bob."""
        _, lines = db_run([
            "SELECT name FROM users WHERE (id = 2 OR id = 3) AND id != 3",
            ".exit",
        ], self.db)
        self.assertTrue(any("bob" in l for l in lines))
        self.assertFalse(any("carol" in l for l in lines))


if __name__ == "__main__":
    unittest.main()
