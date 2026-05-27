# Tests for: UPPER, LOWER, LENGTH, SUBSTR, TRIM, LTRIM, RTRIM
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


class TestUpperLower(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE t (id INTEGER, name VARCHAR(32))",
            "INSERT INTO t VALUES (1, 'Alice')",
            "INSERT INTO t VALUES (2, 'bob')",
            "INSERT INTO t VALUES (3, 'CAROL')",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_upper_select(self):
        """UPPER returns uppercase version of a string column."""
        _, lines = db_run(["SELECT UPPER(name) FROM t WHERE id = 2", ".exit"], self.db)
        self.assertIn("BOB", " ".join(lines))

    def test_lower_select(self):
        """LOWER returns lowercase version of a string column."""
        _, lines = db_run(["SELECT LOWER(name) FROM t WHERE id = 3", ".exit"], self.db)
        self.assertIn("carol", " ".join(lines))

    def test_upper_literal(self):
        """UPPER on a string literal."""
        _, lines = db_run(["SELECT UPPER('hello')", ".exit"], self.db)
        self.assertIn("HELLO", " ".join(lines))

    def test_lower_literal(self):
        """LOWER on a string literal."""
        _, lines = db_run(["SELECT LOWER('WORLD')", ".exit"], self.db)
        self.assertIn("world", " ".join(lines))

    def test_upper_in_where(self):
        """UPPER in WHERE clause filters case-insensitively."""
        _, lines = db_run([
            "SELECT id FROM t WHERE UPPER(name) = 'BOB'",
            ".exit",
        ], self.db)
        self.assertIn("2", " ".join(lines))

    def test_upper_already_upper(self):
        """UPPER on an already-uppercase string returns same value."""
        _, lines = db_run(["SELECT UPPER(name) FROM t WHERE id = 3", ".exit"], self.db)
        self.assertIn("CAROL", " ".join(lines))

    def test_null_upper(self):
        """UPPER(NULL) returns NULL."""
        _, lines = db_run([
            "CREATE TABLE n (id INTEGER, val VARCHAR(32))",
            "INSERT INTO n (id) VALUES (1)",
            "SELECT UPPER(val) FROM n",
            ".exit",
        ], self.db)
        full = " ".join(lines)
        self.assertIn("NULL", full)


class TestLength(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE t (id INTEGER, word VARCHAR(32))",
            "INSERT INTO t VALUES (1, 'hello')",
            "INSERT INTO t VALUES (2, '')",
            "INSERT INTO t VALUES (3, 'abc')",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_length_basic(self):
        """LENGTH returns character count of a string."""
        _, lines = db_run(["SELECT LENGTH(word) FROM t WHERE id = 1", ".exit"], self.db)
        self.assertIn("5", " ".join(lines))

    def test_length_empty(self):
        """LENGTH of empty string is 0."""
        _, lines = db_run(["SELECT LENGTH(word) FROM t WHERE id = 2", ".exit"], self.db)
        self.assertIn("0", " ".join(lines))

    def test_length_literal(self):
        """LENGTH on a string literal."""
        _, lines = db_run(["SELECT LENGTH('hyperion')", ".exit"], self.db)
        self.assertIn("8", " ".join(lines))

    def test_length_in_where(self):
        """LENGTH in WHERE filters rows by string length."""
        _, lines = db_run([
            "SELECT id FROM t WHERE LENGTH(word) = 3",
            ".exit",
        ], self.db)
        self.assertIn("3", " ".join(lines))


class TestSubstr(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_substr_start_length(self):
        """SUBSTR(str, start, length) extracts a substring."""
        _, lines = db_run(["SELECT SUBSTR('hello', 2, 3)", ".exit"], self.db)
        self.assertIn("ell", " ".join(lines))

    def test_substr_start_only(self):
        """SUBSTR(str, start) extracts from start to end."""
        _, lines = db_run(["SELECT SUBSTR('hello', 3)", ".exit"], self.db)
        self.assertIn("llo", " ".join(lines))

    def test_substr_from_column(self):
        """SUBSTR on a column value."""
        db_run([
            "CREATE TABLE t (id INTEGER, code VARCHAR(16))",
            "INSERT INTO t VALUES (1, 'ABC123')",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT SUBSTR(code, 4) FROM t", ".exit"], self.db)
        self.assertIn("123", " ".join(lines))

    def test_substr_pos_1(self):
        """SUBSTR starting at position 1 returns the full string."""
        _, lines = db_run(["SELECT SUBSTR('abc', 1)", ".exit"], self.db)
        self.assertIn("abc", " ".join(lines))

    def test_substr_length_zero(self):
        """SUBSTR with length 0 returns empty string."""
        db_run([
            "CREATE TABLE t (id INTEGER, s VARCHAR(32))",
            "INSERT INTO t VALUES (1, 'hello')",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT id, SUBSTR(s, 2, 0) FROM t", ".exit"], self.db)
        # Row should show id=1 and empty second column — no substring chars
        data_lines = [l for l in lines if l and l.split()[0].isdigit()]
        self.assertTrue(data_lines)
        self.assertNotIn("ell", " ".join(data_lines))


class TestTrim(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_trim_whitespace(self):
        """TRIM removes leading and trailing whitespace."""
        db_run([
            "CREATE TABLE t (id INTEGER, val VARCHAR(32))",
            "INSERT INTO t VALUES (1, '  hello  ')",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT id, TRIM(val) FROM t", ".exit"], self.db)
        data_lines = [l for l in lines if l and l.split()[0].isdigit()]
        self.assertTrue(data_lines)
        row = data_lines[0]
        self.assertIn("hello", row)
        self.assertNotIn("  hello  ", row)

    def test_ltrim_whitespace(self):
        """LTRIM removes only leading whitespace."""
        _, lines = db_run(["SELECT LTRIM('  hi')", ".exit"], self.db)
        self.assertIn("hi", " ".join(lines))

    def test_rtrim_whitespace(self):
        """RTRIM removes only trailing whitespace."""
        _, lines = db_run(["SELECT RTRIM('hi  ')", ".exit"], self.db)
        full = " ".join(lines)
        self.assertIn("hi", full)

    def test_trim_custom_chars(self):
        """TRIM(str, chars) removes specified characters from both ends."""
        _, lines = db_run(["SELECT TRIM('xxhelloxx', 'x')", ".exit"], self.db)
        self.assertIn("hello", " ".join(lines))

    def test_ltrim_custom_chars(self):
        """LTRIM(str, chars) removes specified characters from the left."""
        _, lines = db_run(["SELECT LTRIM('---hello', '-')", ".exit"], self.db)
        self.assertIn("hello", " ".join(lines))

    def test_rtrim_custom_chars(self):
        """RTRIM(str, chars) removes specified characters from the right."""
        _, lines = db_run(["SELECT RTRIM('hello...', '.')", ".exit"], self.db)
        self.assertIn("hello", " ".join(lines))

    def test_trim_from_column(self):
        """TRIM works on a column value."""
        db_run([
            "CREATE TABLE t (id INTEGER, val VARCHAR(32))",
            "INSERT INTO t VALUES (1, '  padded  ')",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT TRIM(val) FROM t", ".exit"], self.db)
        self.assertIn("padded", " ".join(lines))

    def test_trim_null(self):
        """TRIM(NULL) returns NULL."""
        db_run([
            "CREATE TABLE t (id INTEGER, val VARCHAR(32))",
            "INSERT INTO t (id) VALUES (1)",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT TRIM(val) FROM t", ".exit"], self.db)
        self.assertIn("NULL", " ".join(lines))


class TestStringFunctionsComposed(unittest.TestCase):
    """Tests combining multiple string functions."""

    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_upper_of_substr(self):
        """UPPER(SUBSTR(str, start, len)) composes correctly."""
        _, lines = db_run(["SELECT UPPER(SUBSTR('hello world', 7))", ".exit"], self.db)
        self.assertIn("WORLD", " ".join(lines))

    def test_length_of_trim(self):
        """LENGTH(TRIM(str)) gives trimmed string length."""
        _, lines = db_run(["SELECT LENGTH(TRIM('  hi  '))", ".exit"], self.db)
        self.assertIn("2", " ".join(lines))

    def test_functions_in_order_by(self):
        """String functions in ORDER BY work correctly."""
        db_run([
            "CREATE TABLE t (id INTEGER, name VARCHAR(32))",
            "INSERT INTO t VALUES (1, 'charlie')",
            "INSERT INTO t VALUES (2, 'alice')",
            "INSERT INTO t VALUES (3, 'bob')",
            ".exit",
        ], self.db)
        _, lines = db_run([
            "SELECT id, name FROM t ORDER BY UPPER(name)",
            ".exit",
        ], self.db)
        # alice < bob < charlie alphabetically
        id_lines = [l.split()[0] for l in lines if l and l.split()[0].isdigit()]
        self.assertEqual(id_lines, ["2", "3", "1"])


if __name__ == "__main__":
    unittest.main()
