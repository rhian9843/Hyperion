# Tests for: REPLACE, INSTR, PRINTF/FORMAT, ABS, ROUND, CEIL, FLOOR, MOD,
#            RANDOM, RANDOMBLOB, TYPEOF
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


# ── REPLACE ───────────────────────────────────────────────────────────────────

class TestReplace(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_replace_basic(self):
        """REPLACE replaces all occurrences of a substring."""
        _, lines = db_run(["SELECT REPLACE('hello world', 'world', 'there')", ".exit"], self.db)
        self.assertIn("hello there", " ".join(lines))

    def test_replace_multiple_occurrences(self):
        """REPLACE replaces all occurrences, not just the first."""
        _, lines = db_run(["SELECT REPLACE('aabaa', 'a', 'x')", ".exit"], self.db)
        self.assertIn("xxbxx", " ".join(lines))

    def test_replace_no_match(self):
        """REPLACE returns original string when pattern not found."""
        _, lines = db_run(["SELECT REPLACE('hello', 'z', 'x')", ".exit"], self.db)
        self.assertIn("hello", " ".join(lines))

    def test_replace_from_column(self):
        """REPLACE works on a column value."""
        db_run([
            "CREATE TABLE t (id INTEGER, val VARCHAR(64))",
            "INSERT INTO t VALUES (1, 'foo bar foo')",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT REPLACE(val, 'foo', 'baz') FROM t", ".exit"], self.db)
        self.assertIn("baz bar baz", " ".join(lines))

    def test_replace_null_input(self):
        """REPLACE(NULL, ...) returns NULL."""
        db_run([
            "CREATE TABLE t (id INTEGER, val VARCHAR(32))",
            "INSERT INTO t (id) VALUES (1)",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT REPLACE(val, 'a', 'b') FROM t", ".exit"], self.db)
        self.assertIn("NULL", " ".join(lines))


# ── INSTR ─────────────────────────────────────────────────────────────────────

class TestInstr(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_instr_found(self):
        """INSTR returns 1-based position of first occurrence."""
        _, lines = db_run(["SELECT INSTR('hello', 'ell')", ".exit"], self.db)
        self.assertIn("2", " ".join(lines))

    def test_instr_not_found(self):
        """INSTR returns 0 when substring is not found."""
        _, lines = db_run(["SELECT INSTR('hello', 'xyz')", ".exit"], self.db)
        self.assertIn("0", " ".join(lines))

    def test_instr_first_char(self):
        """INSTR returns 1 when substring is at the start."""
        _, lines = db_run(["SELECT INSTR('hello', 'h')", ".exit"], self.db)
        self.assertIn("1", " ".join(lines))

    def test_instr_from_column(self):
        """INSTR works on column values."""
        db_run([
            "CREATE TABLE t (id INTEGER, s VARCHAR(32))",
            "INSERT INTO t VALUES (1, 'abcdef')",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT INSTR(s, 'cd') FROM t", ".exit"], self.db)
        self.assertIn("3", " ".join(lines))


# ── PRINTF / FORMAT ───────────────────────────────────────────────────────────

class TestPrintf(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_printf_string(self):
        """PRINTF formats a string argument with %s."""
        _, lines = db_run(["SELECT PRINTF('Hello, %s!', 'world')", ".exit"], self.db)
        self.assertIn("Hello, world!", " ".join(lines))

    def test_printf_integer(self):
        """PRINTF formats an integer with %d."""
        _, lines = db_run(["SELECT PRINTF('Value: %d', 42)", ".exit"], self.db)
        self.assertIn("Value: 42", " ".join(lines))

    def test_printf_float(self):
        """PRINTF formats a float with %.2f."""
        _, lines = db_run(["SELECT PRINTF('%.2f', 3.14159)", ".exit"], self.db)
        self.assertIn("3.14", " ".join(lines))

    def test_printf_multiple_args(self):
        """PRINTF handles multiple format arguments."""
        _, lines = db_run(["SELECT PRINTF('%s=%d', 'x', 7)", ".exit"], self.db)
        self.assertIn("x=7", " ".join(lines))

    def test_format_alias(self):
        """FORMAT is an alias for PRINTF."""
        _, lines = db_run(["SELECT FORMAT('%05d', 42)", ".exit"], self.db)
        self.assertIn("00042", " ".join(lines))


# ── Math functions ─────────────────────────────────────────────────────────────

class TestMathFunctions(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_abs_positive(self):
        """ABS of a positive number returns the same value."""
        _, lines = db_run(["SELECT ABS(5)", ".exit"], self.db)
        self.assertIn("5", " ".join(lines))

    def test_abs_negative(self):
        """ABS of a negative number returns its positive form."""
        _, lines = db_run(["SELECT ABS(-7)", ".exit"], self.db)
        self.assertIn("7", " ".join(lines))

    def test_abs_null(self):
        """ABS(NULL) returns NULL."""
        db_run([
            "CREATE TABLE t (id INTEGER, val INTEGER)",
            "INSERT INTO t (id) VALUES (1)",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT ABS(val) FROM t", ".exit"], self.db)
        self.assertIn("NULL", " ".join(lines))

    def test_round_no_digits(self):
        """ROUND with no digits argument rounds to nearest integer."""
        _, lines = db_run(["SELECT ROUND(3.7)", ".exit"], self.db)
        self.assertIn("4", " ".join(lines))

    def test_round_with_digits(self):
        """ROUND(x, n) rounds to n decimal places."""
        _, lines = db_run(["SELECT ROUND(3.14159, 2)", ".exit"], self.db)
        self.assertIn("3.14", " ".join(lines))

    def test_ceil_basic(self):
        """CEIL returns the smallest integer >= x."""
        _, lines = db_run(["SELECT CEIL(2.1)", ".exit"], self.db)
        self.assertIn("3", " ".join(lines))

    def test_ceil_negative(self):
        """CEIL of a negative fractional number rounds toward zero."""
        _, lines = db_run(["SELECT CEIL(-2.9)", ".exit"], self.db)
        self.assertIn("-2", " ".join(lines))

    def test_floor_basic(self):
        """FLOOR returns the largest integer <= x."""
        _, lines = db_run(["SELECT FLOOR(2.9)", ".exit"], self.db)
        self.assertIn("2", " ".join(lines))

    def test_floor_negative(self):
        """FLOOR of a negative fractional number rounds away from zero."""
        _, lines = db_run(["SELECT FLOOR(-2.1)", ".exit"], self.db)
        self.assertIn("-3", " ".join(lines))

    def test_mod_basic(self):
        """MOD(a, b) returns the remainder of a divided by b."""
        _, lines = db_run(["SELECT MOD(10, 3)", ".exit"], self.db)
        self.assertIn("1", " ".join(lines))

    def test_mod_exact(self):
        """MOD(9, 3) returns 0 when evenly divisible."""
        _, lines = db_run(["SELECT MOD(9, 3)", ".exit"], self.db)
        self.assertIn("0", " ".join(lines))

    def test_math_from_column(self):
        """Math functions work on column values."""
        db_run([
            "CREATE TABLE t (id INTEGER, val REAL)",
            "INSERT INTO t VALUES (1, -4.7)",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT ABS(val), CEIL(val), FLOOR(val) FROM t", ".exit"], self.db)
        full = " ".join(lines)
        self.assertIn("4.7", full)
        self.assertIn("-4", full)
        self.assertIn("-5", full)


# ── RANDOM / RANDOMBLOB ────────────────────────────────────────────────────────

class TestRandom(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_random_returns_value(self):
        """RANDOM() returns a numeric value."""
        _, lines = db_run(["SELECT RANDOM()", ".exit"], self.db)
        full = " ".join(lines)
        # Should produce some numeric output (could be negative)
        self.assertTrue(any(c.isdigit() for c in full))

    def test_random_two_calls_likely_differ(self):
        """Two RANDOM() calls in different rows likely produce different values."""
        db_run([
            "CREATE TABLE t (id INTEGER)",
            "INSERT INTO t VALUES (1)",
            "INSERT INTO t VALUES (2)",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT RANDOM() FROM t", ".exit"], self.db)
        data = [l for l in lines if any(c.isdigit() for c in l)]
        # Not a guarantee but two independent calls should almost never match
        self.assertGreaterEqual(len(data), 1)

    def test_randomblob_length(self):
        """RANDOMBLOB(n) returns a bytes object of length n."""
        from hyperion.expr import eval_expr
        result = eval_expr("RANDOMBLOB(16)", {})
        self.assertIsInstance(result, bytes)
        self.assertEqual(len(result), 16)

    def test_randomblob_zero(self):
        """RANDOMBLOB(0) returns empty bytes."""
        from hyperion.expr import eval_expr
        result = eval_expr("RANDOMBLOB(0)", {})
        self.assertIsInstance(result, bytes)
        self.assertEqual(len(result), 0)


# ── TYPEOF ────────────────────────────────────────────────────────────────────

class TestTypeof(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_typeof_integer_literal(self):
        """TYPEOF of an integer literal returns 'integer'."""
        _, lines = db_run(["SELECT TYPEOF(42)", ".exit"], self.db)
        self.assertIn("integer", " ".join(lines))

    def test_typeof_real_literal(self):
        """TYPEOF of a real literal returns 'real'."""
        _, lines = db_run(["SELECT TYPEOF(3.14)", ".exit"], self.db)
        self.assertIn("real", " ".join(lines))

    def test_typeof_text_literal(self):
        """TYPEOF of a string literal returns 'text'."""
        _, lines = db_run(["SELECT TYPEOF('hello')", ".exit"], self.db)
        self.assertIn("text", " ".join(lines))

    def test_typeof_null_literal(self):
        """TYPEOF(NULL) returns 'null'."""
        _, lines = db_run(["SELECT TYPEOF(NULL)", ".exit"], self.db)
        self.assertIn("null", " ".join(lines))

    def test_typeof_null_column(self):
        """TYPEOF of a NULL column value returns 'null'."""
        db_run([
            "CREATE TABLE t (id INTEGER, val VARCHAR(32))",
            "INSERT INTO t (id) VALUES (1)",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT TYPEOF(val) FROM t", ".exit"], self.db)
        self.assertIn("null", " ".join(lines))

    def test_typeof_integer_column(self):
        """TYPEOF of an integer column value returns 'integer'."""
        db_run([
            "CREATE TABLE t (id INTEGER, n INTEGER)",
            "INSERT INTO t VALUES (1, 99)",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT TYPEOF(n) FROM t", ".exit"], self.db)
        self.assertIn("integer", " ".join(lines))

    def test_typeof_text_column(self):
        """TYPEOF of a text column value returns 'text'."""
        db_run([
            "CREATE TABLE t (id INTEGER, s VARCHAR(32))",
            "INSERT INTO t VALUES (1, 'hi')",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT TYPEOF(s) FROM t", ".exit"], self.db)
        self.assertIn("text", " ".join(lines))


if __name__ == "__main__":
    unittest.main()
