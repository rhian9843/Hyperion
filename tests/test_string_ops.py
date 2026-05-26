# Tests for: string concat (||), GLOB, LIKE ESCAPE, NOT IN NULL semantics,
# expression evaluation in SELECT (function calls returning NULL gracefully)
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
        if stripped:
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


# ── String concatenation ──────────────────────────────────────────────────────

class TestStringConcat(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE people (id INTEGER, first VARCHAR(32), last VARCHAR(32))",
            "INSERT INTO people VALUES (1, 'Alice', 'Smith')",
            "INSERT INTO people VALUES (2, 'Bob', 'Jones')",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_concat_two_columns(self):
        """first || last → AliceSmith."""
        _, lines = db_run([
            "SELECT first || last FROM people WHERE id = 1",
            ".exit",
        ], self.db)
        self.assertTrue(any("AliceSmith" in l for l in lines))

    def test_concat_with_string_literal(self):
        """first || ' ' || last → Alice Smith."""
        _, lines = db_run([
            "SELECT first || ' ' || last FROM people WHERE id = 1",
            ".exit",
        ], self.db)
        self.assertTrue(any("Alice Smith" in l for l in lines))

    def test_concat_alias(self):
        """first || ' ' || last AS fullname → header shows fullname."""
        _, lines = db_run([
            "SELECT first || ' ' || last AS fullname FROM people WHERE id = 2",
            ".exit",
        ], self.db)
        header = lines[0] if lines else ""
        self.assertIn("fullname", header)
        self.assertTrue(any("Bob Jones" in l for l in lines))

    def test_concat_in_where(self):
        """WHERE first || last = 'BobJones' → only Bob, not Alice."""
        _, lines = db_run([
            "SELECT first FROM people WHERE first || last = 'BobJones'",
            ".exit",
        ], self.db)
        self.assertTrue(any("Bob" in l for l in lines))
        self.assertFalse(any("Alice" in l for l in lines))

    def test_concat_with_separator_all_rows(self):
        """SELECT first || ', ' || last returns both rows."""
        _, lines = db_run([
            "SELECT first || ', ' || last AS name FROM people ORDER BY id",
            ".exit",
        ], self.db)
        self.assertTrue(any("Alice, Smith" in l for l in lines))
        self.assertTrue(any("Bob, Jones" in l for l in lines))


# ── GLOB operator ─────────────────────────────────────────────────────────────

class TestGlob(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE files (id INTEGER, name VARCHAR(64))",
            "INSERT INTO files VALUES (1, 'report.txt')",
            "INSERT INTO files VALUES (2, 'summary.csv')",
            "INSERT INTO files VALUES (3, 'notes.txt')",
            "INSERT INTO files VALUES (4, 'README')",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_glob_star_suffix(self):
        """GLOB '*.txt' matches report.txt and notes.txt."""
        _, lines = db_run([
            "SELECT name FROM files WHERE name GLOB '*.txt'",
            ".exit",
        ], self.db)
        self.assertTrue(any("report.txt" in l for l in lines))
        self.assertTrue(any("notes.txt" in l for l in lines))
        self.assertFalse(any("summary.csv" in l for l in lines))
        self.assertFalse(any("README" in l for l in lines))

    def test_glob_question_mark(self):
        """GLOB 'READM?' matches README (5 chars after prefix)."""
        _, lines = db_run([
            "SELECT name FROM files WHERE name GLOB 'READM?'",
            ".exit",
        ], self.db)
        self.assertTrue(any("README" in l for l in lines))

    def test_glob_case_sensitive(self):
        """GLOB is case-sensitive: 'readme' does NOT match 'README'."""
        _, lines = db_run([
            "SELECT name FROM files WHERE name GLOB 'readme'",
            ".exit",
        ], self.db)
        self.assertFalse(any("README" in l for l in lines))

    def test_glob_prefix_star(self):
        """GLOB '*.csv' matches only summary.csv."""
        _, lines = db_run([
            "SELECT name FROM files WHERE name GLOB '*.csv'",
            ".exit",
        ], self.db)
        self.assertTrue(any("summary.csv" in l for l in lines))
        self.assertFalse(any(".txt" in l for l in lines))


# ── LIKE … ESCAPE ─────────────────────────────────────────────────────────────

class TestLikeEscape(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE discounts (id INTEGER, code VARCHAR(64))",
            "INSERT INTO discounts VALUES (1, '50%OFF')",
            "INSERT INTO discounts VALUES (2, '20%SALE')",
            "INSERT INTO discounts VALUES (3, 'FREESHIP')",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_like_escape_literal_percent(self):
        """LIKE '50!%OFF' ESCAPE '!' matches only '50%OFF'."""
        _, lines = db_run([
            "SELECT code FROM discounts WHERE code LIKE '50!%OFF' ESCAPE '!'",
            ".exit",
        ], self.db)
        self.assertTrue(any("50%OFF" in l for l in lines))
        self.assertFalse(any("20%SALE" in l for l in lines))
        self.assertFalse(any("FREESHIP" in l for l in lines))

    def test_like_escape_wildcard_still_works(self):
        """LIKE '!%%' ESCAPE '!' matches codes ending in anything after literal %."""
        _, lines = db_run([
            "SELECT code FROM discounts WHERE code LIKE '!%%' ESCAPE '!'",
            ".exit",
        ], self.db)
        # Matches anything starting with literal '%'
        # None of our codes start with '%', so should be 0 rows
        self.assertFalse(any("OFF" in l for l in lines))

    def test_like_no_escape_normal(self):
        """Regular LIKE without ESCAPE still works."""
        _, lines = db_run([
            "SELECT code FROM discounts WHERE code LIKE '%OFF'",
            ".exit",
        ], self.db)
        self.assertTrue(any("50%OFF" in l for l in lines))
        self.assertFalse(any("FREESHIP" in l for l in lines))


# ── NOT IN NULL semantics ─────────────────────────────────────────────────────

class TestNotInNullSemantics(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE t (id INTEGER, val INTEGER)",
            "INSERT INTO t VALUES (1, 10)",
            "INSERT INTO t VALUES (2, 20)",
            "INSERT INTO t VALUES (3, 30)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_not_in_without_null(self):
        """x NOT IN (10, 20) → only row with val=30."""
        _, lines = db_run([
            "SELECT val FROM t WHERE val NOT IN (10, 20)",
            ".exit",
        ], self.db)
        self.assertTrue(any("30" in l for l in lines))
        self.assertFalse(any("10" in l for l in lines))
        self.assertFalse(any("20" in l for l in lines))

    def test_not_in_with_null_returns_unknown(self):
        """x NOT IN (10, NULL) → UNKNOWN for x=20,30 → no data rows returned."""
        _, lines = db_run([
            "SELECT val FROM t WHERE val NOT IN (10, NULL)",
            ".exit",
        ], self.db)
        # x=10: matched → NOT IN = False (not returned)
        # x=20: UNKNOWN → False (not returned)
        # x=30: UNKNOWN → False (not returned)
        self.assertFalse(any("20" in l for l in lines))
        self.assertFalse(any("30" in l for l in lines))

    def test_in_with_null_match_still_works(self):
        """x IN (10, NULL) → row with val=10 is returned."""
        _, lines = db_run([
            "SELECT val FROM t WHERE val IN (10, NULL)",
            ".exit",
        ], self.db)
        self.assertTrue(any("10" in l for l in lines))


# ── Expression evaluation: any function call returns NULL (not crash) ─────────

class TestExpressionEvalFunctions(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE t (id INTEGER, name VARCHAR(32))",
            "INSERT INTO t VALUES (1, 'alice')",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_unknown_function_returns_null_not_crash(self):
        """SELECT UPPER(name) FROM t should not crash (returns NULL for unknown funcs)."""
        rc, lines = db_run([
            "SELECT UPPER(name) FROM t",
            ".exit",
        ], self.db)
        # Should not crash (return code 0 from the REPL itself)
        # The result may be NULL since UPPER is not implemented
        self.assertFalse(any("Traceback" in l for l in lines))
        self.assertFalse(any("RuntimeError" in l for l in lines))


if __name__ == "__main__":
    unittest.main()
