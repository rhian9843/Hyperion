# Tests for: CREATE/DROP IF [NOT] EXISTS, PRIMARY KEY, AUTOINCREMENT, multi-col UNIQUE
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
        if stripped and stripped != "...":
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


# ── CREATE TABLE IF NOT EXISTS / DROP TABLE IF EXISTS / CREATE INDEX IF NOT EXISTS ──

class TestIfNotExists(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE t (id INTEGER, name VARCHAR(32))",
            "CREATE INDEX idx_t ON t(id)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_create_table_if_not_exists_no_error(self):
        """CREATE TABLE IF NOT EXISTS on existing table does not raise."""
        _, lines = db_run([
            "CREATE TABLE IF NOT EXISTS t (id INTEGER)",
            ".exit",
        ], self.db)
        self.assertFalse(any("Error" in l for l in lines))

    def test_create_table_if_not_exists_new(self):
        """CREATE TABLE IF NOT EXISTS creates the table when it doesn't exist."""
        _, lines = db_run([
            "CREATE TABLE IF NOT EXISTS new_tbl (x INTEGER)",
            "INSERT INTO new_tbl VALUES (42)",
            "SELECT x FROM new_tbl",
            ".exit",
        ], self.db)
        self.assertTrue(any("42" in l for l in lines))

    def test_create_table_without_if_still_errors(self):
        """CREATE TABLE without IF NOT EXISTS still errors on duplicate."""
        _, lines = db_run([
            "CREATE TABLE t (id INTEGER)",
            ".exit",
        ], self.db)
        self.assertTrue(any("Error" in l or "already exists" in l for l in lines))

    def test_drop_table_if_exists_no_error(self):
        """DROP TABLE IF EXISTS on non-existent table does not raise."""
        _, lines = db_run([
            "DROP TABLE IF EXISTS nonexistent",
            ".exit",
        ], self.db)
        self.assertFalse(any("Error" in l for l in lines))

    def test_drop_table_if_exists_drops_real_table(self):
        """DROP TABLE IF EXISTS drops a real table normally."""
        _, lines = db_run([
            "DROP TABLE IF EXISTS t",
            ".exit",
        ], self.db)
        self.assertTrue(any("dropped" in l for l in lines))

    def test_create_index_if_not_exists_no_error(self):
        """CREATE INDEX IF NOT EXISTS on existing index does not raise."""
        _, lines = db_run([
            "CREATE INDEX IF NOT EXISTS idx_t ON t(id)",
            ".exit",
        ], self.db)
        self.assertFalse(any("Error" in l for l in lines))

    def test_create_index_if_not_exists_new(self):
        """CREATE INDEX IF NOT EXISTS creates the index when it doesn't exist."""
        _, lines = db_run([
            "CREATE INDEX IF NOT EXISTS idx_name ON t(name)",
            ".exit",
        ], self.db)
        self.assertTrue(any("created" in l for l in lines))


# ── PRIMARY KEY constraint ─────────────────────────────────────────────────────

class TestPrimaryKey(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR(32))",
            "INSERT INTO users VALUES (1, Alice)",
            "INSERT INTO users VALUES (2, Bob)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_primary_key_enforces_unique(self):
        """Duplicate PRIMARY KEY value is rejected."""
        _, lines = db_run([
            "INSERT INTO users VALUES (1, Carol)",
            ".exit",
        ], self.db)
        self.assertTrue(any("UNIQUE" in l or "Error" in l for l in lines))

    def test_primary_key_null_auto_assigns(self):
        """NULL into INTEGER PRIMARY KEY auto-assigns the next rowid (SQLite rowid-alias semantics)."""
        from hyperion import Database
        db = Database(":memory:")
        db.execute("CREATE TABLE u (id INTEGER PRIMARY KEY, name TEXT)")
        db.execute("INSERT INTO u VALUES (NULL, 'Dave')")
        rows = db.execute("SELECT id, name FROM u").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0]["id"])
        self.assertEqual(rows[0]["name"], "Dave")

    def test_primary_key_allows_select(self):
        """Rows with PRIMARY KEY can be selected normally."""
        _, lines = db_run([
            "SELECT id, name FROM users ORDER BY id",
            ".exit",
        ], self.db)
        full = " ".join(lines)
        self.assertIn("Alice", full)
        self.assertIn("Bob", full)

    def test_primary_key_auto_index(self):
        """PRIMARY KEY auto-creates an index (visible in .indexes)."""
        _, lines = db_run([".indexes", ".exit"], self.db)
        self.assertTrue(any("users" in l for l in lines))


# ── AUTOINCREMENT ─────────────────────────────────────────────────────────────

class TestAutoincrement(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, val VARCHAR(32))",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_autoincrement_assigns_id(self):
        """INSERT with NULL id auto-assigns sequential IDs."""
        db_run([
            "INSERT INTO items VALUES (NULL, apple)",
            "INSERT INTO items VALUES (NULL, banana)",
            "INSERT INTO items VALUES (NULL, cherry)",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT id FROM items ORDER BY id", ".exit"], self.db)
        full = " ".join(lines)
        self.assertIn("1", full)
        self.assertIn("2", full)
        self.assertIn("3", full)

    def test_autoincrement_sequential(self):
        """Auto-assigned IDs are 1, 2, 3, …"""
        db_run([
            "INSERT INTO items VALUES (NULL, x)",
            "INSERT INTO items VALUES (NULL, y)",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT id FROM items ORDER BY id", ".exit"], self.db)
        # Extract numeric lines after the separator
        ids = []
        past_sep = False
        for l in lines:
            if all(c in "-+| " for c in l):
                past_sep = True; continue
            if not past_sep or l.startswith("("):
                continue
            ids.append(int(l.strip()))
        self.assertEqual(ids, [1, 2])

    def test_autoincrement_explicit_value_ok(self):
        """Explicit non-NULL id is still accepted (no forced auto-assign)."""
        db_run([
            "INSERT INTO items VALUES (10, ten)",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT id FROM items", ".exit"], self.db)
        self.assertTrue(any("10" in l for l in lines))

    def test_autoincrement_continues_after_explicit(self):
        """Auto-assign resumes from max(id)+1 after an explicit high id."""
        db_run([
            "INSERT INTO items VALUES (5, five)",
            "INSERT INTO items VALUES (NULL, six)",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT id FROM items ORDER BY id", ".exit"], self.db)
        full = " ".join(lines)
        self.assertIn("5", full)
        self.assertIn("6", full)


# ── Multi-column UNIQUE constraint ─────────────────────────────────────────────

class TestMultiColumnUnique(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE memberships (user_id INTEGER, group_id INTEGER, "
            "UNIQUE (user_id, group_id))",
            "INSERT INTO memberships VALUES (1, 10)",
            "INSERT INTO memberships VALUES (1, 20)",
            "INSERT INTO memberships VALUES (2, 10)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_duplicate_pair_rejected(self):
        """Inserting a duplicate (user_id, group_id) pair raises UNIQUE error."""
        _, lines = db_run([
            "INSERT INTO memberships VALUES (1, 10)",
            ".exit",
        ], self.db)
        self.assertTrue(any("UNIQUE" in l or "Error" in l for l in lines))

    def test_partial_duplicate_allowed(self):
        """Same user_id with different group_id is allowed."""
        _, lines = db_run([
            "INSERT INTO memberships VALUES (1, 30)",
            ".exit",
        ], self.db)
        self.assertFalse(any("Error" in l for l in lines))

    def test_null_exempts_unique_check(self):
        """NULL in either column exempts the row from multi-col UNIQUE check."""
        _, lines = db_run([
            "INSERT INTO memberships VALUES (NULL, 10)",
            "INSERT INTO memberships VALUES (NULL, 10)",
            ".exit",
        ], self.db)
        self.assertFalse(any("Error" in l for l in lines))

    def test_row_count_after_inserts(self):
        """All valid rows are present after constraint checks."""
        _, lines = db_run(["SELECT user_id FROM memberships", ".exit"], self.db)
        self.assertTrue(any("(3 rows)" in l for l in lines))


if __name__ == "__main__":
    unittest.main()
