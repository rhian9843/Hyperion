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
    # strip the "H > " prompts and blank lines, return meaningful output lines
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
        os.unlink(self._f.name)   # remove so Hyperion creates it fresh
        return self._f.name

    def __exit__(self, *_):
        try:
            os.unlink(self._f.name)
        except FileNotFoundError:
            pass


class TestDDL(unittest.TestCase):
    def test_create_table(self):
        with TempDB() as db:
            rc, lines = db_run([CREATE_USERS, ".exit"], db)
        self.assertEqual(rc, 0)
        self.assertIn("Table 'users' created.", lines)

    def test_create_duplicate_table_raises(self):
        with TempDB() as db:
            _, lines = db_run([CREATE_USERS, CREATE_USERS, ".exit"], db)
        self.assertTrue(any("already exists" in l for l in lines))

    def test_drop_table(self):
        with TempDB() as db:
            _, lines = db_run([CREATE_USERS, "DROP TABLE users", ".tables", ".exit"], db)
        self.assertNotIn("users", lines)

    def test_dot_tables(self):
        with TempDB() as db:
            _, lines = db_run([CREATE_USERS, ".tables", ".exit"], db)
        self.assertIn("users", lines)

    def test_dot_schema(self):
        with TempDB() as db:
            _, lines = db_run([CREATE_USERS, ".schema users", ".exit"], db)
        self.assertTrue(any("CREATE TABLE users" in l for l in lines))


class TestAlterTable(unittest.TestCase):
    def test_add_column(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "ALTER TABLE users ADD COLUMN age INTEGER",
                "SELECT * FROM users",
                ".exit",
            ], db)
        self.assertTrue(any("age" in l for l in lines))
        self.assertTrue(any("alice" in l for l in lines))

    def test_add_column_existing_rows_null(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "ALTER TABLE users ADD COLUMN age INTEGER",
                "SELECT age FROM users",
                ".exit",
            ], db)
        self.assertTrue(any("NULL" in l for l in lines))

    def test_drop_column(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "ALTER TABLE users DROP COLUMN email",
                "SELECT * FROM users",
                ".exit",
            ], db)
        self.assertTrue(any("alice" in l for l in lines))
        self.assertFalse(any("alice@example.com" in l for l in lines))

    def test_drop_column_removes_index(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "CREATE INDEX idx_id ON users(id)",
                "ALTER TABLE users DROP COLUMN id",
                ".indexes",
                ".exit",
            ], db)
        self.assertIn("(no indexes)", lines)

    def test_rename_column(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "ALTER TABLE users RENAME COLUMN name TO username",
                "SELECT username FROM users",
                ".exit",
            ], db)
        self.assertTrue(any("alice" in l for l in lines))

    def test_rename_table(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "ALTER TABLE users RENAME TO members",
                "SELECT * FROM members",
                ".exit",
            ], db)
        self.assertTrue(any("alice" in l for l in lines))

    def test_rename_table_old_name_gone(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "ALTER TABLE users RENAME TO members",
                "SELECT * FROM users",
                ".exit",
            ], db)
        self.assertTrue(any("Error" in l for l in lines))

    def test_add_column_persists(self):
        with TempDB() as db:
            db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "ALTER TABLE users ADD COLUMN score INTEGER",
                "UPDATE users SET score=99 WHERE id = 1",
                ".exit",
            ], db)
            _, lines = db_run(["SELECT score FROM users", ".exit"], db)
        self.assertTrue(any("99" in l for l in lines))


class TestMultiPageCatalog(unittest.TestCase):
    def test_many_tables_persist(self):
        """Create enough tables to overflow a single catalog page (~4088 bytes)."""
        with TempDB() as db:
            cmds = []
            for i in range(20):
                cmds.append(
                    f"CREATE TABLE tbl{i} "
                    f"(id INTEGER, name VARCHAR(255), email VARCHAR(255))"
                )
            cmds.append(".exit")
            _, lines = db_run(cmds, db)
            # All creates should succeed
            self.assertEqual(sum(1 for l in lines if "created" in l), 20)
            # Verify all tables survive a reopen
            _, lines2 = db_run([".tables", ".exit"], db)
        for i in range(20):
            self.assertIn(f"tbl{i}", lines2)

    def test_data_survives_large_catalog(self):
        with TempDB() as db:
            cmds = []
            for i in range(20):
                cmds.append(
                    f"CREATE TABLE tbl{i} (id INTEGER, val VARCHAR(64))"
                )
            cmds += [
                "INSERT INTO tbl0 VALUES (1, hello)",
                "INSERT INTO tbl19 VALUES (99, world)",
                ".exit",
            ]
            db_run(cmds, db)
            _, lines = db_run([
                "SELECT * FROM tbl0",
                "SELECT * FROM tbl19",
                ".exit",
            ], db)
        self.assertTrue(any("hello" in l for l in lines))
        self.assertTrue(any("world" in l for l in lines))


class TestFreePageReclamation(unittest.TestCase):
    def test_file_does_not_grow_after_drop_and_recreate(self):
        """File size should stay bounded when tables are repeatedly dropped and recreated."""
        with TempDB() as db:
            db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "INSERT INTO users VALUES (2, bob, bob@example.com)",
                "DROP TABLE users",
                CREATE_USERS,
                "INSERT INTO users VALUES (1, carol, carol@example.com)",
                ".exit",
            ], db)
            size_after = os.path.getsize(db)

            db_run([
                "DROP TABLE users",
                CREATE_USERS,
                "INSERT INTO users VALUES (1, dave, dave@example.com)",
                ".exit",
            ], db)
            size_final = os.path.getsize(db)

        self.assertLessEqual(size_final, size_after + 4096)

    def test_alter_table_does_not_grow_unboundedly(self):
        with TempDB() as db:
            db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "ALTER TABLE users ADD COLUMN age INTEGER",
                "ALTER TABLE users DROP COLUMN age",
                ".exit",
            ], db)
            size1 = os.path.getsize(db)
            db_run([
                "ALTER TABLE users ADD COLUMN score INTEGER",
                "ALTER TABLE users DROP COLUMN score",
                ".exit",
            ], db)
            size2 = os.path.getsize(db)
        self.assertLessEqual(size2, size1 + 4096)

    def test_drop_index_frees_pages(self):
        with TempDB() as db:
            db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "CREATE INDEX idx_id ON users(id)",
                "DROP INDEX idx_id",
                "CREATE INDEX idx_id2 ON users(id)",
                ".exit",
            ], db)
            size1 = os.path.getsize(db)
            db_run([
                "DROP INDEX idx_id2",
                "CREATE INDEX idx_id3 ON users(id)",
                ".exit",
            ], db)
            size2 = os.path.getsize(db)
        self.assertLessEqual(size2, size1 + 4096)


class TestDropIndexIfExists(unittest.TestCase):
    def test_drop_index_if_exists_no_error_when_missing(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "DROP INDEX IF EXISTS idx_ghost",
                ".exit",
            ], db)
        self.assertFalse(any("Error" in l for l in lines))

    def test_drop_index_if_exists_drops_when_present(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "CREATE INDEX idx_name ON users(name)",
                "DROP INDEX IF EXISTS idx_name",
                ".exit",
            ], db)
        self.assertFalse(any("Error" in l for l in lines))

    def test_drop_index_without_if_exists_errors_when_missing(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "DROP INDEX idx_ghost",
                ".exit",
            ], db)
        self.assertTrue(any("Error" in l for l in lines))


if __name__ == "__main__":
    unittest.main()
