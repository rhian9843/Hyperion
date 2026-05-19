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


class TestUnique(unittest.TestCase):
    def test_unique_insert_duplicate_raises(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER UNIQUE, val VARCHAR(32))",
                "INSERT INTO t VALUES (1, a)",
                "INSERT INTO t VALUES (1, b)",
                ".exit",
            ], db)
        self.assertTrue(any("Error" in l for l in lines))

    def test_unique_insert_different_values_ok(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER UNIQUE, val VARCHAR(32))",
                "INSERT INTO t VALUES (1, a)",
                "INSERT INTO t VALUES (2, b)",
                "SELECT * FROM t",
                ".exit",
            ], db)
        self.assertTrue(any("(2 rows)" in l for l in lines))

    def test_unique_allows_null(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, email VARCHAR(64) UNIQUE)",
                "INSERT INTO t (id) VALUES (1)",
                "INSERT INTO t (id) VALUES (2)",
                "SELECT * FROM t",
                ".exit",
            ], db)
        self.assertTrue(any("(2 rows)" in l for l in lines))

    def test_unique_update_to_duplicate_raises(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER UNIQUE, val VARCHAR(32))",
                "INSERT INTO t VALUES (1, a)",
                "INSERT INTO t VALUES (2, b)",
                "UPDATE t SET id=1 WHERE id = 2",
                ".exit",
            ], db)
        self.assertTrue(any("Error" in l for l in lines))

    def test_unique_not_null_combined(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER NOT NULL UNIQUE, val VARCHAR(32))",
                "INSERT INTO t VALUES (NULL, x)",
                ".exit",
            ], db)
        self.assertTrue(any("Error" in l for l in lines))

    def test_schema_shows_unique(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER UNIQUE)",
                ".schema t",
                ".exit",
            ], db)
        self.assertTrue(any("UNIQUE" in l for l in lines))


class TestCheck(unittest.TestCase):

    def test_check_violated_on_insert(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, age INTEGER CHECK (age > 0))",
                "INSERT INTO t VALUES (1, -1)",
                ".exit",
            ], db)
        self.assertTrue(any("Error" in l for l in lines))

    def test_check_satisfied_on_insert(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, age INTEGER CHECK (age > 0))",
                "INSERT INTO t VALUES (1, 25)",
                "SELECT * FROM t",
                ".exit",
            ], db)
        self.assertTrue(any("25" in l for l in lines))

    def test_check_violated_on_update(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, age INTEGER CHECK (age > 0))",
                "INSERT INTO t VALUES (1, 25)",
                "UPDATE t SET age = -5 WHERE id = 1",
                ".exit",
            ], db)
        self.assertTrue(any("Error" in l for l in lines))

    def test_check_boundary_value(self):
        """Boundary: age > 0 means 0 is rejected but 1 is accepted."""
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, age INTEGER CHECK (age > 0))",
                "INSERT INTO t VALUES (1, 0)",
                ".exit",
            ], db)
        self.assertTrue(any("Error" in l for l in lines))

    def test_check_persists_in_schema(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, age INTEGER CHECK (age > 0))",
                ".schema t",
                ".exit",
            ], db)
        self.assertTrue(any("CHECK" in l for l in lines))

    def test_check_survives_reopen(self):
        with TempDB() as db:
            db_run([
                "CREATE TABLE t (id INTEGER, age INTEGER CHECK (age > 0))",
                ".exit",
            ], db)
            _, lines = db_run([
                "INSERT INTO t VALUES (1, -1)",
                ".exit",
            ], db)
        self.assertTrue(any("Error" in l for l in lines))

    def test_check_varchar_constraint(self):
        """CHECK can constrain TEXT columns too (via string comparison)."""
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, status VARCHAR(16) CHECK (status = active))",
                "INSERT INTO t VALUES (1, inactive)",
                ".exit",
            ], db)
        self.assertTrue(any("Error" in l for l in lines))


class TestForeignKey(unittest.TestCase):

    def _setup(self, db):
        db_run([
            "CREATE TABLE dept (id INTEGER, name VARCHAR(32))",
            "CREATE TABLE emp (id INTEGER, dept_id INTEGER REFERENCES dept (id))",
            "INSERT INTO dept VALUES (1, Engineering)",
            "INSERT INTO dept VALUES (2, Marketing)",
            ".exit",
        ], db)

    def test_valid_child_insert(self):
        """Insert child row whose FK value exists in parent → succeeds."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "INSERT INTO emp VALUES (10, 1)",
                "SELECT * FROM emp",
                ".exit",
            ], db)
        self.assertTrue(any("10" in l for l in lines))

    def test_invalid_child_insert_rejected(self):
        """Insert child row whose FK value does not exist in parent → error."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "INSERT INTO emp VALUES (10, 99)",
                ".exit",
            ], db)
        self.assertTrue(any("Error" in l for l in lines))

    def test_null_fk_allowed(self):
        """NULL in FK column is permitted (no parent lookup needed)."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "INSERT INTO emp (id) VALUES (20)",
                "SELECT * FROM emp",
                ".exit",
            ], db)
        self.assertTrue(any("20" in l for l in lines))

    def test_delete_parent_with_child_rejected(self):
        """Deleting a parent row that is referenced by a child → RESTRICT error."""
        with TempDB() as db:
            self._setup(db)
            db_run(["INSERT INTO emp VALUES (10, 1)", ".exit"], db)
            _, lines = db_run([
                "DELETE FROM dept WHERE id = 1",
                ".exit",
            ], db)
        self.assertTrue(any("Error" in l for l in lines))

    def test_delete_unreferenced_parent_ok(self):
        """Deleting a parent row with no child references → succeeds."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "DELETE FROM dept WHERE id = 2",
                "SELECT * FROM dept",
                ".exit",
            ], db)
        self.assertFalse(any("Marketing" in l for l in lines))

    def test_update_child_to_invalid_fk_rejected(self):
        """Updating a child FK column to a value not in parent → error."""
        with TempDB() as db:
            self._setup(db)
            db_run(["INSERT INTO emp VALUES (10, 1)", ".exit"], db)
            _, lines = db_run([
                "UPDATE emp SET dept_id = 99 WHERE id = 10",
                ".exit",
            ], db)
        self.assertTrue(any("Error" in l for l in lines))

    def test_table_level_foreign_key_syntax(self):
        """Table-level FOREIGN KEY (...) REFERENCES ... syntax is parsed and enforced."""
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE parent (id INTEGER)",
                "CREATE TABLE child (id INTEGER, pid INTEGER, "
                "FOREIGN KEY (pid) REFERENCES parent (id))",
                "INSERT INTO child VALUES (1, 99)",
                ".exit",
            ], db)
        self.assertTrue(any("Error" in l for l in lines))

    def test_schema_shows_foreign_key(self):
        """`.schema` output includes FOREIGN KEY clause."""
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE dept (id INTEGER)",
                "CREATE TABLE emp (id INTEGER, dept_id INTEGER REFERENCES dept (id))",
                ".schema emp",
                ".exit",
            ], db)
        self.assertTrue(any("FOREIGN KEY" in l for l in lines))

    def test_fk_survives_reopen(self):
        """FK constraints are persisted and enforced after database is re-opened."""
        with TempDB() as db:
            db_run([
                "CREATE TABLE dept (id INTEGER)",
                "INSERT INTO dept VALUES (1)",
                "CREATE TABLE emp (id INTEGER, dept_id INTEGER REFERENCES dept (id))",
                ".exit",
            ], db)
            _, lines = db_run([
                "INSERT INTO emp VALUES (1, 999)",
                ".exit",
            ], db)
        self.assertTrue(any("Error" in l for l in lines))


class TestDefault(unittest.TestCase):

    def test_default_used_when_column_omitted(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, status VARCHAR(16) DEFAULT active)",
                "INSERT INTO t (id) VALUES (1)",
                "SELECT * FROM t",
                ".exit",
            ], db)
        self.assertTrue(any("active" in l for l in lines))

    def test_default_integer(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, score INTEGER DEFAULT 0)",
                "INSERT INTO t (id) VALUES (1)",
                "SELECT * FROM t",
                ".exit",
            ], db)
        self.assertTrue(any("0" in l for l in lines))

    def test_explicit_value_overrides_default(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, status VARCHAR(16) DEFAULT pending)",
                "INSERT INTO t VALUES (1, done)",
                "SELECT * FROM t",
                ".exit",
            ], db)
        self.assertTrue(any("done" in l for l in lines))
        self.assertFalse(any("pending" in l for l in lines))

    def test_null_used_when_no_default(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, note VARCHAR(32))",
                "INSERT INTO t (id) VALUES (1)",
                "SELECT * FROM t",
                ".exit",
            ], db)
        self.assertTrue(any("NULL" in l for l in lines))

    def test_default_persists_in_schema(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, status VARCHAR(16) DEFAULT active)",
                ".schema t",
                ".exit",
            ], db)
        self.assertTrue(any("DEFAULT active" in l for l in lines))

    def test_default_survives_reopen(self):
        with TempDB() as db:
            db_run([
                "CREATE TABLE t (id INTEGER, status VARCHAR(16) DEFAULT active)",
                ".exit",
            ], db)
            _, lines = db_run([
                "INSERT INTO t (id) VALUES (1)",
                "SELECT * FROM t",
                ".exit",
            ], db)
        self.assertTrue(any("active" in l for l in lines))


if __name__ == "__main__":
    unittest.main()
