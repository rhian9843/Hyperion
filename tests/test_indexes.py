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


class TestIndex(unittest.TestCase):
    def test_create_and_list_index(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "CREATE INDEX idx_id ON users(id)",
                ".indexes",
                ".exit",
            ], db)
        self.assertTrue(any("idx_id" in l for l in lines))

    def test_index_accelerated_lookup(self):
        with TempDB() as db:
            cmds = [CREATE_USERS]
            for i in range(1, 11):
                cmds.append(f"INSERT INTO users VALUES ({i}, user{i}, u{i}@example.com)")
            cmds += [
                "CREATE INDEX idx_id ON users(id)",
                "SELECT * FROM users WHERE id = 7",
                ".exit",
            ]
            _, lines = db_run(cmds, db)
        self.assertTrue(any("user7" in l for l in lines))
        self.assertFalse(any("user1 " in l or "user10" in l for l in lines))

    def test_drop_index(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "CREATE INDEX idx_id ON users(id)",
                "DROP INDEX idx_id",
                ".indexes",
                ".exit",
            ], db)
        self.assertIn("(no indexes)", lines)

    def test_index_persists_across_reopen(self):
        with TempDB() as db:
            db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "CREATE INDEX idx_id ON users(id)",
                ".exit",
            ], db)
            _, lines = db_run([".indexes", ".exit"], db)
        self.assertTrue(any("idx_id" in l for l in lines))


class TestIndexAllTypes(unittest.TestCase):
    """Indexes on non-INTEGER column types."""

    def test_index_on_varchar(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "INSERT INTO users VALUES (2, bob, bob@example.com)",
                "CREATE INDEX idx_name ON users(name)",
                "SELECT * FROM users WHERE name = alice",
                ".exit",
            ], db)
        self.assertTrue(any("alice" in l for l in lines))
        self.assertFalse(any("bob" in l for l in lines))

    def test_index_on_real(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE prices (id INTEGER, amount REAL)",
                "INSERT INTO prices VALUES (1, 9.99)",
                "INSERT INTO prices VALUES (2, 19.99)",
                "INSERT INTO prices VALUES (3, 4.99)",
                "CREATE INDEX idx_amount ON prices(amount)",
                "SELECT * FROM prices WHERE amount = 9.99",
                ".exit",
            ], db)
        self.assertTrue(any("9.99" in l for l in lines))
        self.assertFalse(any("19.99" in l for l in lines))

    def test_varchar_index_persists(self):
        with TempDB() as db:
            db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "CREATE INDEX idx_email ON users(email)",
                ".exit",
            ], db)
            _, lines = db_run([
                "SELECT * FROM users WHERE email = alice@example.com",
                ".exit",
            ], db)
        self.assertTrue(any("alice" in l for l in lines))

    def test_varchar_index_no_match(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "CREATE INDEX idx_name ON users(name)",
                "SELECT * FROM users WHERE name = nobody",
                ".exit",
            ], db)
        self.assertIn("(no rows)", lines)


class TestMultiColumnIndex(unittest.TestCase):
    def test_create_multi_column_index_listed(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "CREATE INDEX idx_name_email ON users(name, email)",
                ".indexes",
                ".exit",
            ], db)
        self.assertTrue(any("name" in l and "email" in l for l in lines))

    def test_multi_column_index_lookup(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "INSERT INTO users VALUES (2, bob, bob@example.com)",
                "CREATE INDEX idx_name_email ON users(name, email)",
                "SELECT * FROM users WHERE name = alice AND email = alice@example.com",
                ".exit",
            ], db)
        self.assertTrue(any("alice" in l for l in lines))
        self.assertFalse(any("bob" in l for l in lines))

    def test_multi_column_index_no_match(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "CREATE INDEX idx_name_email ON users(name, email)",
                "SELECT * FROM users WHERE name = alice AND email = wrong@example.com",
                ".exit",
            ], db)
        self.assertIn("(no rows)", lines)

    def test_multi_column_index_maintained_on_insert(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "CREATE INDEX idx_name_email ON users(name, email)",
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "SELECT * FROM users WHERE name = alice AND email = alice@example.com",
                ".exit",
            ], db)
        self.assertTrue(any("alice" in l for l in lines))

    def test_multi_column_index_maintained_on_delete(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "CREATE INDEX idx_name_email ON users(name, email)",
                "DELETE FROM users WHERE id = 1",
                "SELECT * FROM users WHERE name = alice AND email = alice@example.com",
                ".exit",
            ], db)
        self.assertIn("(no rows)", lines)

    def test_and_where_full_scan(self):
        """AND WHERE works even without an index (full scan path)."""
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "INSERT INTO users VALUES (2, alice, alice2@example.com)",
                "SELECT * FROM users WHERE name = alice AND email = alice@example.com",
                ".exit",
            ], db)
        self.assertTrue(any("alice@example.com" in l for l in lines))
        self.assertFalse(any("alice2@example.com" in l for l in lines))

    def test_multi_column_index_persists(self):
        with TempDB() as db:
            db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "CREATE INDEX idx_name_email ON users(name, email)",
                ".exit",
            ], db)
            _, lines = db_run([
                "SELECT * FROM users WHERE name = alice AND email = alice@example.com",
                ".exit",
            ], db)
        self.assertTrue(any("alice" in l for l in lines))


class TestNonUniqueIndex(unittest.TestCase):
    """Indexes on non-unique columns must not crash on duplicate keys,
    and SELECT must fall back to a full scan (not the incomplete index)."""

    def test_insert_duplicate_integer_indexed_value(self):
        """Two rows with the same INTEGER indexed value must not crash."""
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, age INTEGER)",
                "CREATE INDEX idx_age ON t(age)",
                "INSERT INTO t VALUES (1, 25)",
                "INSERT INTO t VALUES (2, 25)",
                "SELECT * FROM t",
                ".exit",
            ], db)
        age_lines = [l for l in lines if "25" in l]
        self.assertEqual(len(age_lines), 2)

    def test_insert_duplicate_text_indexed_value(self):
        """Two rows with the same TEXT indexed value must not crash."""
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "CREATE INDEX idx_name ON users(name)",
                "INSERT INTO users VALUES (1, alice, a@x.com)",
                "INSERT INTO users VALUES (2, alice, b@x.com)",
                "SELECT * FROM users",
                ".exit",
            ], db)
        self.assertTrue(any("a@x.com" in l for l in lines))
        self.assertTrue(any("b@x.com" in l for l in lines))

    def test_select_falls_back_to_full_scan(self):
        """SELECT on a non-unique indexed column returns all matching rows."""
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "CREATE INDEX idx_name ON users(name)",
                "INSERT INTO users VALUES (1, alice, a@x.com)",
                "INSERT INTO users VALUES (2, alice, b@x.com)",
                "SELECT * FROM users WHERE name = alice",
                ".exit",
            ], db)
        self.assertTrue(any("a@x.com" in l for l in lines))
        self.assertTrue(any("b@x.com" in l for l in lines))

    def test_delete_with_non_unique_index(self):
        """DELETE on a non-unique indexed column removes correct rows."""
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "CREATE INDEX idx_name ON users(name)",
                "INSERT INTO users VALUES (1, alice, a@x.com)",
                "INSERT INTO users VALUES (2, alice, b@x.com)",
                "INSERT INTO users VALUES (3, bob, c@x.com)",
                "DELETE FROM users WHERE name = alice",
                "SELECT * FROM users",
                ".exit",
            ], db)
        self.assertFalse(any("alice" in l for l in lines))
        self.assertTrue(any("bob" in l for l in lines))

    def test_unique_index_still_accelerated(self):
        """A UNIQUE-constrained column's index IS used for SELECT (fast path)."""
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER UNIQUE, val VARCHAR(32))",
                "CREATE INDEX idx_id ON t(id)",
                "INSERT INTO t VALUES (1, foo)",
                "INSERT INTO t VALUES (2, bar)",
                "SELECT * FROM t WHERE id = 1",
                ".exit",
            ], db)
        self.assertTrue(any("foo" in l for l in lines))
        self.assertFalse(any("bar" in l for l in lines))


class TestIndexRangeScan(unittest.TestCase):
    """Index is used for range operators (>, >=, <, <=) on INTEGER/REAL columns."""

    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE t (id INTEGER, score INTEGER)",
            "INSERT INTO t VALUES (1, 10)",
            "INSERT INTO t VALUES (2, 20)",
            "INSERT INTO t VALUES (3, 30)",
            "INSERT INTO t VALUES (4, 40)",
            "INSERT INTO t VALUES (5, 50)",
            "CREATE INDEX idx_score ON t(score)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_greater_than(self):
        _, lines = db_run(["SELECT * FROM t WHERE score > 30", ".exit"], self.db)
        self.assertTrue(any("40" in l for l in lines))
        self.assertTrue(any("50" in l for l in lines))
        self.assertFalse(any("10" in l for l in lines))
        self.assertFalse(any("30" in l for l in lines))

    def test_greater_than_or_equal(self):
        _, lines = db_run(["SELECT * FROM t WHERE score >= 30", ".exit"], self.db)
        self.assertTrue(any("30" in l for l in lines))
        self.assertTrue(any("40" in l for l in lines))
        self.assertFalse(any("20" in l for l in lines))

    def test_less_than(self):
        _, lines = db_run(["SELECT * FROM t WHERE score < 30", ".exit"], self.db)
        self.assertTrue(any("10" in l for l in lines))
        self.assertTrue(any("20" in l for l in lines))
        self.assertFalse(any("30" in l for l in lines))

    def test_less_than_or_equal(self):
        _, lines = db_run(["SELECT * FROM t WHERE score <= 30", ".exit"], self.db)
        self.assertTrue(any("30" in l for l in lines))
        self.assertTrue(any("10" in l for l in lines))
        self.assertFalse(any("40" in l for l in lines))

    def test_between_via_and(self):
        """WHERE a > lo AND a < hi uses index for the first condition, post-filters second."""
        _, lines = db_run(["SELECT * FROM t WHERE score > 10 AND score < 50", ".exit"], self.db)
        self.assertTrue(any("20" in l for l in lines))
        self.assertTrue(any("40" in l for l in lines))
        self.assertFalse(any(" 10 " in l or l.startswith("10 ") or l.endswith(" 10") for l in lines))
        self.assertFalse(any("50" in l for l in lines))

    def test_range_no_match(self):
        _, lines = db_run(["SELECT * FROM t WHERE score > 999", ".exit"], self.db)
        self.assertIn("(no rows)", lines)

    def test_range_row_count(self):
        _, lines = db_run(["SELECT * FROM t WHERE score >= 20 AND score <= 40", ".exit"], self.db)
        self.assertTrue(any("(3 rows)" in l for l in lines))

    def test_range_on_real_column(self):
        with TempDB() as db:
            db_run([
                "CREATE TABLE prices (id INTEGER, amount REAL)",
                "INSERT INTO prices VALUES (1, 9.99)",
                "INSERT INTO prices VALUES (2, 19.99)",
                "INSERT INTO prices VALUES (3, 4.99)",
                "CREATE INDEX idx_amount ON prices(amount)",
                ".exit",
            ], db)
            _, lines = db_run(["SELECT * FROM prices WHERE amount > 5.0", ".exit"], db)
        self.assertTrue(any("9.99" in l for l in lines))
        self.assertTrue(any("19.99" in l for l in lines))
        self.assertFalse(any("4.99" in l for l in lines))


if __name__ == "__main__":
    unittest.main()
