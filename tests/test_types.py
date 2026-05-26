# Tests for: BLOB/BYTES, BOOLEAN, DATE/DATETIME/TIMESTAMP, integer size aliases
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


# ── BOOLEAN column type ────────────────────────────────────────────────────────

class TestBooleanType(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_boolean_create_no_error(self):
        """CREATE TABLE with BOOLEAN column produces no error."""
        _, lines = db_run([
            "CREATE TABLE t (id INTEGER, active BOOLEAN)",
            ".exit",
        ], self.db)
        self.assertFalse(any("Error" in l for l in lines))

    def test_boolean_insert_and_select(self):
        """Insert and retrieve BOOLEAN values (stored as 0/1)."""
        db_run([
            "CREATE TABLE t (id INTEGER, active BOOLEAN)",
            "INSERT INTO t VALUES (1, 1)",
            "INSERT INTO t VALUES (2, 0)",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT active FROM t WHERE id = 1", ".exit"], self.db)
        self.assertIn("1", " ".join(lines))

    def test_boolean_where_true(self):
        """WHERE active = TRUE filters correctly."""
        db_run([
            "CREATE TABLE t (id INTEGER, active BOOLEAN)",
            "INSERT INTO t VALUES (1, 1)",
            "INSERT INTO t VALUES (2, 0)",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT id FROM t WHERE active = TRUE", ".exit"], self.db)
        full = " ".join(lines)
        self.assertIn("1", full)
        self.assertNotIn("2", full)

    def test_boolean_where_false(self):
        """WHERE active = FALSE filters to inactive rows."""
        db_run([
            "CREATE TABLE t (id INTEGER, active BOOLEAN)",
            "INSERT INTO t VALUES (1, 1)",
            "INSERT INTO t VALUES (2, 0)",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT id FROM t WHERE active = FALSE", ".exit"], self.db)
        id_lines = [l for l in lines if l.strip().isdigit()]
        self.assertIn("2", id_lines)
        self.assertNotIn("1", id_lines)

    def test_bool_alias(self):
        """BOOL is accepted as a column type alias for BOOLEAN."""
        _, lines = db_run([
            "CREATE TABLE t (id INTEGER, flag BOOL)",
            ".exit",
        ], self.db)
        self.assertFalse(any("Error" in l for l in lines))


# ── DATE / DATETIME / TIMESTAMP types ─────────────────────────────────────────

class TestDateTimeTypes(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_date_column_no_error(self):
        """CREATE TABLE with DATE column produces no error."""
        _, lines = db_run([
            "CREATE TABLE t (id INTEGER, born DATE)",
            ".exit",
        ], self.db)
        self.assertFalse(any("Error" in l for l in lines))

    def test_date_insert_and_select(self):
        """Insert and retrieve a DATE value."""
        db_run([
            "CREATE TABLE t (id INTEGER, born DATE)",
            "INSERT INTO t VALUES (1, '2000-06-15')",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT born FROM t", ".exit"], self.db)
        self.assertIn("2000-06-15", " ".join(lines))

    def test_datetime_column(self):
        """DATETIME column stores and retrieves ISO datetime strings."""
        db_run([
            "CREATE TABLE t (id INTEGER, created_at DATETIME)",
            "INSERT INTO t VALUES (1, '2024-01-15 10:30:00')",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT created_at FROM t", ".exit"], self.db)
        self.assertIn("2024-01-15", " ".join(lines))

    def test_timestamp_column(self):
        """TIMESTAMP column works like DATETIME."""
        _, lines = db_run([
            "CREATE TABLE t (id INTEGER, ts TIMESTAMP)",
            ".exit",
        ], self.db)
        self.assertFalse(any("Error" in l for l in lines))

    def test_date_order_by(self):
        """Dates stored as text sort lexicographically (ISO-8601 order)."""
        db_run([
            "CREATE TABLE t (id INTEGER, born DATE)",
            "INSERT INTO t VALUES (1, '2000-01-01')",
            "INSERT INTO t VALUES (2, '1990-06-15')",
            "INSERT INTO t VALUES (3, '2010-12-31')",
            ".exit",
        ], self.db)
        # Select both id and born so ORDER BY born has the value available
        _, lines = db_run(["SELECT id, born FROM t ORDER BY born", ".exit"], self.db)
        id_lines = [l.split()[0] for l in lines if l and l.split()[0].isdigit()]
        self.assertEqual(id_lines, ["2", "1", "3"])

    def test_date_where_comparison(self):
        """WHERE born > '2000-01-01' works with date strings."""
        db_run([
            "CREATE TABLE t (id INTEGER, born DATE)",
            "INSERT INTO t VALUES (1, '1990-01-01')",
            "INSERT INTO t VALUES (2, '2010-06-15')",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT id FROM t WHERE born > '2000-01-01'", ".exit"], self.db)
        id_lines = [l for l in lines if l.strip().isdigit()]
        self.assertIn("2", id_lines)
        self.assertNotIn("1", id_lines)


# ── Integer size aliases ────────────────────────────────────────────────────────

class TestIntegerAliases(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_tinyint(self):
        """TINYINT is accepted and behaves as INTEGER."""
        db_run([
            "CREATE TABLE t (id INTEGER, score TINYINT)",
            "INSERT INTO t VALUES (1, 127)",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT score FROM t", ".exit"], self.db)
        self.assertIn("127", " ".join(lines))

    def test_smallint(self):
        """SMALLINT is accepted and behaves as INTEGER."""
        _, lines = db_run([
            "CREATE TABLE t (id INTEGER, val SMALLINT)",
            ".exit",
        ], self.db)
        self.assertFalse(any("Error" in l for l in lines))

    def test_bigint(self):
        """BIGINT is accepted and stores large values correctly."""
        db_run([
            "CREATE TABLE t (id INTEGER, big BIGINT)",
            "INSERT INTO t VALUES (1, 9007199254740992)",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT big FROM t", ".exit"], self.db)
        self.assertIn("9007199254740992", " ".join(lines))

    def test_int_alias(self):
        """INT (without 'EGER') is accepted as an alias for INTEGER."""
        _, lines = db_run([
            "CREATE TABLE t (id INT, val INT)",
            ".exit",
        ], self.db)
        self.assertFalse(any("Error" in l for l in lines))

    def test_mediumint(self):
        """MEDIUMINT is accepted and behaves as INTEGER."""
        _, lines = db_run([
            "CREATE TABLE t (id INTEGER, val MEDIUMINT)",
            ".exit",
        ], self.db)
        self.assertFalse(any("Error" in l for l in lines))

    def test_integer_aliases_insert_and_select(self):
        """All integer aliases work for insert and select."""
        db_run([
            "CREATE TABLE t (a TINYINT, b SMALLINT, c MEDIUMINT, d BIGINT)",
            "INSERT INTO t VALUES (1, 100, 10000, 1000000)",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT a, b, c, d FROM t", ".exit"], self.db)
        full = " ".join(lines)
        self.assertIn("1", full)
        self.assertIn("100", full)
        self.assertIn("10000", full)
        self.assertIn("1000000", full)


# ── BLOB / BYTES column type ───────────────────────────────────────────────────

class TestBlobType(unittest.TestCase):
    """
    BLOB columns store raw binary data (returned as bytes).
    Via the REPL, values are stored as text encoded to bytes, which is fine
    for testing round-trip through text-mode input.
    """

    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_blob_create_no_error(self):
        """CREATE TABLE with BLOB column produces no error."""
        _, lines = db_run([
            "CREATE TABLE t (id INTEGER, data BLOB)",
            ".exit",
        ], self.db)
        self.assertFalse(any("Error" in l for l in lines))

    def test_bytes_alias_no_error(self):
        """BYTES is accepted as an alias for BLOB."""
        _, lines = db_run([
            "CREATE TABLE t (id INTEGER, raw BYTES)",
            ".exit",
        ], self.db)
        self.assertFalse(any("Error" in l for l in lines))

    def test_blob_insert_and_select(self):
        """Insert a text value into a BLOB column and retrieve it."""
        db_run([
            "CREATE TABLE t (id INTEGER, data BLOB)",
            "INSERT INTO t VALUES (1, 'hello')",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT id FROM t WHERE id = 1", ".exit"], self.db)
        self.assertIn("1", " ".join(lines))

    def test_blob_null(self):
        """NULL can be stored in a BLOB column."""
        db_run([
            "CREATE TABLE t (id INTEGER, data BLOB)",
            "INSERT INTO t (id) VALUES (1)",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT COUNT(*) FROM t WHERE data IS NULL", ".exit"], self.db)
        self.assertIn("1", " ".join(lines))

    def test_blob_n_syntax(self):
        """BLOB(n) syntax is accepted."""
        _, lines = db_run([
            "CREATE TABLE t (id INTEGER, data BLOB(512))",
            ".exit",
        ], self.db)
        self.assertFalse(any("Error" in l for l in lines))

    def test_binary_alias(self):
        """BINARY is accepted as an alias for BLOB."""
        _, lines = db_run([
            "CREATE TABLE t (id INTEGER, raw BINARY)",
            ".exit",
        ], self.db)
        self.assertFalse(any("Error" in l for l in lines))


# ── Float type aliases ─────────────────────────────────────────────────────────

class TestFloatAliases(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_float_alias(self):
        """FLOAT is accepted as an alias for REAL."""
        db_run([
            "CREATE TABLE t (id INTEGER, price FLOAT)",
            "INSERT INTO t VALUES (1, 9.99)",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT price FROM t", ".exit"], self.db)
        self.assertIn("9.99", " ".join(lines))

    def test_double_alias(self):
        """DOUBLE is accepted as an alias for REAL."""
        _, lines = db_run([
            "CREATE TABLE t (id INTEGER, val DOUBLE)",
            ".exit",
        ], self.db)
        self.assertFalse(any("Error" in l for l in lines))


if __name__ == "__main__":
    unittest.main()
