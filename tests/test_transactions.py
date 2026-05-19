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


class TestTransactions(unittest.TestCase):
    def test_commit_persists(self):
        with TempDB() as db:
            db_run([
                CREATE_USERS,
                "BEGIN",
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "COMMIT",
                ".exit",
            ], db)
            _, lines = db_run(["SELECT * FROM users", ".exit"], db)
        self.assertTrue(any("alice" in l for l in lines))

    def test_rollback_discards(self):
        with TempDB() as db:
            db_run([CREATE_USERS, ".exit"], db)
            db_run([
                "BEGIN",
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "ROLLBACK",
                ".exit",
            ], db)
            _, lines = db_run(["SELECT * FROM users", ".exit"], db)
        self.assertIn("(no rows)", lines)

    def test_multi_statement_txn(self):
        with TempDB() as db:
            db_run([
                CREATE_USERS,
                "BEGIN",
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "INSERT INTO users VALUES (2, bob, bob@example.com)",
                "DELETE FROM users WHERE id = 1",
                "COMMIT",
                ".exit",
            ], db)
            _, lines = db_run(["SELECT * FROM users", ".exit"], db)
        self.assertFalse(any("alice" in l for l in lines))
        self.assertTrue(any("bob" in l for l in lines))

    def test_begin_already_active(self):
        with TempDB() as db:
            _, lines = db_run([CREATE_USERS, "BEGIN", "BEGIN", ".exit"], db)
        self.assertTrue(any("Error" in l for l in lines))

    def test_commit_without_begin(self):
        with TempDB() as db:
            _, lines = db_run([CREATE_USERS, "COMMIT", ".exit"], db)
        self.assertTrue(any("Error" in l for l in lines))

    def test_rollback_discards_updates(self):
        with TempDB() as db:
            db_run([CREATE_USERS,
                    "INSERT INTO users VALUES (1, alice, a@x.com)", ".exit"], db)
            db_run(["BEGIN",
                    "UPDATE users SET name = bob WHERE id = 1",
                    "ROLLBACK", ".exit"], db)
            _, lines = db_run(["SELECT * FROM users", ".exit"], db)
        self.assertTrue(any("alice" in l for l in lines))
        self.assertFalse(any("bob" in l for l in lines))

    def test_rollback_discards_deletes(self):
        with TempDB() as db:
            db_run([CREATE_USERS,
                    "INSERT INTO users VALUES (1, alice, a@x.com)", ".exit"], db)
            db_run(["BEGIN", "DELETE FROM users WHERE id = 1", "ROLLBACK", ".exit"], db)
            _, lines = db_run(["SELECT * FROM users", ".exit"], db)
        self.assertTrue(any("alice" in l for l in lines))

    def test_autocommit_outside_transaction(self):
        """Each statement outside BEGIN/COMMIT is individually auto-committed."""
        with TempDB() as db:
            db_run([CREATE_USERS,
                    "INSERT INTO users VALUES (1, alice, a@x.com)", ".exit"], db)
            _, lines = db_run(["SELECT * FROM users", ".exit"], db)
        self.assertTrue(any("alice" in l for l in lines))


if __name__ == "__main__":
    unittest.main()
