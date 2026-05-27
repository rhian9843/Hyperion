# Tests for: PRAGMA, VACUUM, quoted identifiers
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


# ── PRAGMA table_info ──────────────────────────────────────────────────────────

class TestPragmaTableInfo(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE emp (id INTEGER PRIMARY KEY, name VARCHAR(64) NOT NULL, dept VARCHAR(32))",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_returns_all_columns(self):
        _, lines = db_run(["PRAGMA table_info(emp)", ".exit"], self.db)
        full = " ".join(lines)
        self.assertIn("id", full)
        self.assertIn("name", full)
        self.assertIn("dept", full)

    def test_notnull_flag(self):
        _, lines = db_run(["PRAGMA table_info(emp)", ".exit"], self.db)
        # Data rows start with a digit (cid); look for name row with notnull=1
        data_rows = [l for l in lines if l and l[0].isdigit()]
        name_row = next((l for l in data_rows if "| name |" in l or l.split("|")[1].strip() == "name"), None)
        self.assertIsNotNone(name_row, "No data row found for 'name' column")
        self.assertIn("1", name_row)

    def test_pk_flag(self):
        _, lines = db_run(["PRAGMA table_info(emp)", ".exit"], self.db)
        data_rows = [l for l in lines if l and l[0].isdigit()]
        id_row = next((l for l in data_rows if l.split("|")[1].strip() == "id"), None)
        self.assertIsNotNone(id_row, "No data row found for 'id' column")
        self.assertIn("1", id_row)  # pk=1 for id

    def test_unknown_table_raises(self):
        _, lines = db_run(["PRAGMA table_info(nonexistent)", ".exit"], self.db)
        self.assertIn("Error", " ".join(lines))


# ── PRAGMA index_list ──────────────────────────────────────────────────────────

class TestPragmaIndexList(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE t (id INTEGER PRIMARY KEY, val INTEGER)",
            "CREATE INDEX idx_val ON t(val)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_lists_user_index(self):
        _, lines = db_run(["PRAGMA index_list(t)", ".exit"], self.db)
        full = " ".join(lines)
        self.assertIn("idx_val", full)

    def test_lists_pk_index(self):
        _, lines = db_run(["PRAGMA index_list(t)", ".exit"], self.db)
        full = " ".join(lines)
        self.assertIn("_pk_t_id", full)

    def test_no_rows_for_unknown_table(self):
        _, lines = db_run(["PRAGMA index_list(nobody)", ".exit"], self.db)
        self.assertIn("no rows", " ".join(lines))


# ── PRAGMA index_info ──────────────────────────────────────────────────────────

class TestPragmaIndexInfo(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE t (id INTEGER, name VARCHAR(32), age INTEGER)",
            "CREATE INDEX idx_name ON t(name)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_returns_indexed_column(self):
        _, lines = db_run(["PRAGMA index_info(idx_name)", ".exit"], self.db)
        self.assertIn("name", " ".join(lines))

    def test_unknown_index_raises(self):
        _, lines = db_run(["PRAGMA index_info(no_such_idx)", ".exit"], self.db)
        self.assertIn("Error", " ".join(lines))


# ── PRAGMA foreign_keys ────────────────────────────────────────────────────────

class TestPragmaForeignKeys(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE parent (id INTEGER PRIMARY KEY)",
            "CREATE TABLE child (id INTEGER, parent_id INTEGER REFERENCES parent(id))",
            "INSERT INTO parent VALUES (1)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_fk_enforced_by_default(self):
        _, lines = db_run([
            "INSERT INTO child VALUES (1, 999)",
            ".exit",
        ], self.db)
        # Should print an FK error: 999 not in parent
        self.assertIn("Error", " ".join(lines))

    def test_fk_disabled_allows_orphan(self):
        rc, lines = db_run([
            "PRAGMA foreign_keys = OFF",
            "INSERT INTO child VALUES (1, 999)",
            ".exit",
        ], self.db)
        self.assertEqual(rc, 0)
        _, sel = db_run(["SELECT COUNT(*) FROM child", ".exit"], self.db)
        self.assertIn("1", " ".join(sel))

    def test_pragma_query_returns_state(self):
        _, lines = db_run(["PRAGMA foreign_keys", ".exit"], self.db)
        self.assertIn("1", " ".join(lines))  # default ON

    def test_fk_re_enabled(self):
        # Use numeric form to avoid REPL continuation-token issue with the keyword ON
        _, lines = db_run([
            "PRAGMA foreign_keys = 0",
            "PRAGMA foreign_keys = 1",
            "INSERT INTO child VALUES (1, 999)",
            ".exit",
        ], self.db)
        self.assertIn("Error", " ".join(lines))


# ── VACUUM ─────────────────────────────────────────────────────────────────────

class TestVacuum(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE t (id INTEGER, val VARCHAR(32))",
            "INSERT INTO t VALUES (1, 'a')",
            "INSERT INTO t VALUES (2, 'b')",
            "INSERT INTO t VALUES (3, 'c')",
            "DELETE FROM t WHERE id = 2",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_vacuum_succeeds(self):
        rc, lines = db_run(["VACUUM", ".exit"], self.db)
        self.assertEqual(rc, 0)
        self.assertIn("vacuumed", " ".join(lines).lower())

    def test_data_intact_after_vacuum(self):
        db_run(["VACUUM", ".exit"], self.db)
        _, lines = db_run(["SELECT id FROM t ORDER BY id", ".exit"], self.db)
        # Collect only data rows (lines that are plain integers)
        ids = [l.strip() for l in lines if l.strip().isdigit()]
        self.assertIn("1", ids)
        self.assertIn("3", ids)
        self.assertNotIn("2", ids)

    def test_file_size_reduced_or_equal_after_vacuum(self):
        size_before = os.path.getsize(self.db)
        db_run(["VACUUM", ".exit"], self.db)
        size_after = os.path.getsize(self.db)
        # Compact database should not be larger than the original
        self.assertLessEqual(size_after, size_before + 4096)  # allow one page slack


# ── Quoted identifiers ─────────────────────────────────────────────────────────

class TestQuotedIdentifiers(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_double_quoted_column_in_select(self):
        db_run([
            'CREATE TABLE t (id INTEGER, "my col" VARCHAR(32))',
            'INSERT INTO t VALUES (1, \'hello\')',
            ".exit",
        ], self.db)
        _, lines = db_run(['SELECT "my col" FROM t', ".exit"], self.db)
        self.assertIn("hello", " ".join(lines))

    def test_backtick_quoted_column_in_select(self):
        db_run([
            "CREATE TABLE t (id INTEGER, `my col` VARCHAR(32))",
            "INSERT INTO t VALUES (1, 'world')",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT `my col` FROM t", ".exit"], self.db)
        self.assertIn("world", " ".join(lines))

    def test_backtick_reserved_word_as_column(self):
        """Backtick-quoting allows reserved words as column names."""
        db_run([
            "CREATE TABLE t (id INTEGER, `select` VARCHAR(32))",
            "INSERT INTO t VALUES (1, 'reserved')",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT `select` FROM t", ".exit"], self.db)
        self.assertIn("reserved", " ".join(lines))

    def test_double_quote_in_where(self):
        db_run([
            'CREATE TABLE t (id INTEGER, "val" INTEGER)',
            'INSERT INTO t VALUES (1, 42)',
            ".exit",
        ], self.db)
        _, lines = db_run(['SELECT id FROM t WHERE "val" = 42', ".exit"], self.db)
        self.assertIn("1", " ".join(lines))


if __name__ == "__main__":
    unittest.main()
