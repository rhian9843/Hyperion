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


class TestInsertSelect(unittest.TestCase):
    def test_single_insert_and_select(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "SELECT * FROM users",
                ".exit",
            ], db)
        self.assertTrue(any("alice" in l for l in lines))
        self.assertTrue(any("alice@example.com" in l for l in lines))

    def test_named_column_insert(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users (id, name, email) VALUES (42, bob, bob@example.com)",
                "SELECT * FROM users",
                ".exit",
            ], db)
        self.assertTrue(any("42" in l and "bob" in l for l in lines))

    def test_column_projection(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "SELECT name FROM users",
                ".exit",
            ], db)
        self.assertTrue(any("alice" in l for l in lines))
        self.assertFalse(any("alice@example.com" in l for l in lines))

    def test_multiple_rows(self):
        with TempDB() as db:
            cmds = [CREATE_USERS]
            for i in range(1, 6):
                cmds.append(f"INSERT INTO users VALUES ({i}, user{i}, user{i}@example.com)")
            cmds += ["SELECT * FROM users", ".exit"]
            _, lines = db_run(cmds, db)
        self.assertTrue(any("(5 rows)" in l for l in lines))


class TestUpdate(unittest.TestCase):
    def test_update_single_row(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "UPDATE users SET name=alice2 WHERE id = 1",
                "SELECT * FROM users",
                ".exit",
            ], db)
        self.assertTrue(any("alice2" in l for l in lines))
        self.assertFalse(any("alice@" in l and "alice2" not in l for l in lines))

    def test_update_multiple_rows(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "INSERT INTO users VALUES (2, bob, bob@example.com)",
                "UPDATE users SET email=updated@example.com",
                "SELECT * FROM users WHERE id = 2",
                ".exit",
            ], db)
        self.assertTrue(any("updated@example.com" in l for l in lines))

    def test_update_returns_count(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "INSERT INTO users VALUES (2, bob, bob@example.com)",
                "UPDATE users SET name=x WHERE id = 1",
                ".exit",
            ], db)
        self.assertTrue(any("1 row updated" in l for l in lines))


class TestDelete(unittest.TestCase):
    def test_delete_where(self):
        with TempDB() as db:
            db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "INSERT INTO users VALUES (2, bob, bob@example.com)",
                ".exit",
            ], db)
            _, lines = db_run([
                "DELETE FROM users WHERE id = 1",
                "SELECT * FROM users",
                ".exit",
            ], db)
        self.assertFalse(any("alice" in l for l in lines))
        self.assertTrue(any("bob" in l for l in lines))


class TestPersistence(unittest.TestCase):
    def test_data_survives_reopen(self):
        with TempDB() as db:
            db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                ".exit",
            ], db)
            _, lines = db_run(["SELECT * FROM users", ".exit"], db)
        self.assertTrue(any("alice" in l for l in lines))

    def test_multiple_tables_persist(self):
        with TempDB() as db:
            db_run([
                CREATE_USERS,
                "CREATE TABLE logs (id INTEGER, message VARCHAR(128))",
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "INSERT INTO logs VALUES (1, hello)",
                ".exit",
            ], db)
            _, lines = db_run([".tables", ".exit"], db)
        self.assertIn("logs", lines)
        self.assertIn("users", lines)


class TestErrorHandling(unittest.TestCase):
    def test_insert_into_missing_table(self):
        with TempDB() as db:
            _, lines = db_run(
                ["INSERT INTO ghost VALUES (1, x, y)", ".exit"], db
            )
        self.assertTrue(any("Error" in l for l in lines))

    def test_table_full(self):
        with TempDB() as db:
            cmds = ["CREATE TABLE tiny (id INTEGER, val VARCHAR(1))"]
            cmds += ["DROP TABLE tiny", ".exit"]
            rc, _ = db_run(cmds, db)
        self.assertEqual(rc, 0)

    def test_integer_overflow_raises_error(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, val INTEGER)",
                "INSERT INTO t VALUES (1, 99999999999999999999)",
                ".exit",
            ], db)
        self.assertTrue(any("Error" in l or "error" in l for l in lines))


class TestMultiRowInsert(unittest.TestCase):
    def test_two_rows_in_one_statement(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, a@x.com), (2, bob, b@x.com)",
                "SELECT * FROM users",
                ".exit",
            ], db)
        self.assertTrue(any("alice" in l for l in lines))
        self.assertTrue(any("bob" in l for l in lines))

    def test_three_rows_in_one_statement(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, a@x.com), (2, bob, b@x.com), (3, carol, c@x.com)",
                "SELECT COUNT(*) FROM users",
                ".exit",
            ], db)
        self.assertTrue(any("3" in l for l in lines))

    def test_multi_row_insert_count_message(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, a@x.com), (2, bob, b@x.com)",
                ".exit",
            ], db)
        self.assertTrue(any("2 rows" in l for l in lines))


class TestVarcharTruncation(unittest.TestCase):
    def test_value_too_long_raises_error(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, name VARCHAR(5))",
                "INSERT INTO t VALUES (1, toolongvalue)",
                ".exit",
            ], db)
        self.assertTrue(any("Error" in l or "error" in l for l in lines))

    def test_value_exact_length_allowed(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, name VARCHAR(5))",
                "INSERT INTO t VALUES (1, hello)",
                "SELECT * FROM t",
                ".exit",
            ], db)
        self.assertTrue(any("hello" in l for l in lines))


class TestEscapedQuotes(unittest.TestCase):
    def test_single_quote_in_string(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, val VARCHAR(32))",
                "INSERT INTO t VALUES (1, 'it''s fine')",
                "SELECT * FROM t",
                ".exit",
            ], db)
        self.assertTrue(any("it's fine" in l for l in lines))

    def test_plain_string_still_works(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, val VARCHAR(32))",
                "INSERT INTO t VALUES (1, 'hello')",
                "SELECT * FROM t",
                ".exit",
            ], db)
        self.assertTrue(any("hello" in l for l in lines))


if __name__ == "__main__":
    unittest.main()
