# Tests for expression evaluation: arithmetic, CAST, COALESCE, NULLIF, IFNULL, CASE WHEN
import os
import sys
import tempfile
import unittest
from pathlib import Path
from subprocess import PIPE, run

sys.path.insert(0, str(Path(__file__).parent.parent))

DATABASE_COMMAND = ["python3", "-m", "hyperion"]

CREATE_PRODUCTS = (
    "CREATE TABLE products "
    "(id INTEGER, name VARCHAR(64), price REAL, qty INTEGER, discount REAL)"
)


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


# ── Arithmetic in SELECT ───────────────────────────────────────────────────────

class TestArithmeticSelect(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            CREATE_PRODUCTS,
            "INSERT INTO products VALUES (1, widget, 10, 5, 0.1)",
            "INSERT INTO products VALUES (2, gadget, 25, 2, 0.2)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_multiply_columns(self):
        """SELECT price * qty computes the product."""
        _, lines = db_run([
            "SELECT price * qty FROM products WHERE id = 1",
            ".exit",
        ], self.db)
        self.assertTrue(any("50" in l for l in lines))

    def test_arithmetic_with_alias(self):
        """SELECT price * qty AS total → header shows 'total'."""
        _, lines = db_run([
            "SELECT price * qty AS total FROM products WHERE id = 2",
            ".exit",
        ], self.db)
        header = lines[0] if lines else ""
        self.assertIn("total", header)
        # 25 * 2 = 50
        self.assertTrue(any("50" in l for l in lines))

    def test_add_constant(self):
        """SELECT price + 5 adds a constant."""
        _, lines = db_run([
            "SELECT price + 5 FROM products WHERE id = 1",
            ".exit",
        ], self.db)
        self.assertTrue(any("15" in l for l in lines))

    def test_parenthesized_arithmetic(self):
        """SELECT (price - 5) * qty uses parens for precedence."""
        _, lines = db_run([
            "SELECT (price - 5) * qty FROM products WHERE id = 1",
            ".exit",
        ], self.db)
        # (10 - 5) * 5 = 25
        self.assertTrue(any("25" in l for l in lines))


# ── Arithmetic in WHERE ────────────────────────────────────────────────────────

class TestArithmeticWhere(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            CREATE_PRODUCTS,
            "INSERT INTO products VALUES (1, widget, 10, 5, 0.1)",
            "INSERT INTO products VALUES (2, gadget, 25, 2, 0.2)",
            "INSERT INTO products VALUES (3, doohickey, 5, 10, 0.05)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_arithmetic_left_side(self):
        """WHERE price * qty > 40 → widget (50) and gadget (50), not doohickey (50)."""
        _, lines = db_run([
            "SELECT name FROM products WHERE price * qty > 40",
            ".exit",
        ], self.db)
        self.assertTrue(any("widget" in l for l in lines))
        self.assertTrue(any("gadget" in l for l in lines))
        # doohickey: 5 * 10 = 50 > 40 → also matches
        self.assertTrue(any("doohickey" in l for l in lines))

    def test_arithmetic_strict_filter(self):
        """WHERE price * 3 > 20 → widget (30) and gadget (75), not doohickey (15)."""
        _, lines = db_run([
            "SELECT name FROM products WHERE price * 3 > 20",
            ".exit",
        ], self.db)
        self.assertTrue(any("widget" in l for l in lines))
        self.assertTrue(any("gadget" in l for l in lines))
        self.assertFalse(any("doohickey" in l for l in lines))

    def test_arithmetic_with_float(self):
        """WHERE price * 2 > 15 → gadget (50) and widget (20)."""
        _, lines = db_run([
            "SELECT name FROM products WHERE price * 2 > 15",
            ".exit",
        ], self.db)
        self.assertTrue(any("gadget" in l for l in lines))
        self.assertTrue(any("widget" in l for l in lines))
        self.assertFalse(any("doohickey" in l for l in lines))


# ── CAST ──────────────────────────────────────────────────────────────────────

class TestCast(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            CREATE_PRODUCTS,
            "INSERT INTO products VALUES (1, widget, 10, 5, 0.1)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_cast_real_to_integer_in_select(self):
        """SELECT CAST(price AS INTEGER) → 10."""
        _, lines = db_run([
            "SELECT CAST(price AS INTEGER) FROM products WHERE id = 1",
            ".exit",
        ], self.db)
        self.assertTrue(any("10" in l for l in lines))

    def test_cast_integer_to_text_in_select(self):
        """SELECT CAST(id AS TEXT) returns a text representation."""
        _, lines = db_run([
            "SELECT CAST(id AS TEXT) FROM products WHERE id = 1",
            ".exit",
        ], self.db)
        self.assertTrue(any("1" in l for l in lines))

    def test_cast_in_where(self):
        """WHERE CAST(price AS INTEGER) = 10 → widget."""
        _, lines = db_run([
            "SELECT name FROM products WHERE CAST(price AS INTEGER) = 10",
            ".exit",
        ], self.db)
        self.assertTrue(any("widget" in l for l in lines))


# ── COALESCE / NULLIF / IFNULL ────────────────────────────────────────────────

class TestCoalesceNullifIfnull(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE t (id INTEGER, val INTEGER, fallback INTEGER)",
            "INSERT INTO t VALUES (1, NULL, 99)",
            "INSERT INTO t VALUES (2, 42, 0)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_coalesce_returns_first_non_null(self):
        """COALESCE(val, fallback) → 99 for row 1, 42 for row 2."""
        _, lines = db_run([
            "SELECT COALESCE(val, fallback) FROM t ORDER BY id",
            ".exit",
        ], self.db)
        data = [l for l in lines if l.strip().lstrip("-").replace("|", "").strip().isdigit()
                or any(c.isdigit() for c in l)]
        self.assertTrue(any("99" in l for l in lines))
        self.assertTrue(any("42" in l for l in lines))

    def test_nullif_returns_null_when_equal(self):
        """NULLIF(val, 42) → NULL for row 2 (val=42), 99 for... wait row 1 val is NULL."""
        _, lines = db_run([
            "SELECT NULLIF(fallback, 99) FROM t WHERE id = 1",
            ".exit",
        ], self.db)
        # fallback=99, NULLIF(99,99) → NULL
        self.assertTrue(any("NULL" in l for l in lines))

    def test_nullif_returns_value_when_different(self):
        """NULLIF(fallback, 0) → fallback value when fallback != 0."""
        _, lines = db_run([
            "SELECT NULLIF(fallback, 0) FROM t WHERE id = 1",
            ".exit",
        ], self.db)
        # fallback=99, NULLIF(99,0) → 99
        self.assertTrue(any("99" in l for l in lines))

    def test_ifnull_replaces_null(self):
        """IFNULL(val, 0) → 0 when val is NULL."""
        _, lines = db_run([
            "SELECT IFNULL(val, 0) FROM t WHERE id = 1",
            ".exit",
        ], self.db)
        self.assertTrue(any("0" in l for l in lines))

    def test_coalesce_in_where(self):
        """WHERE COALESCE(val, fallback) > 50 → row 1 (coalesce=99)."""
        _, lines = db_run([
            "SELECT id FROM t WHERE COALESCE(val, fallback) > 50",
            ".exit",
        ], self.db)
        self.assertTrue(any("1" in l for l in lines))
        self.assertFalse(any("2" in l for l in lines))


# ── CASE WHEN ─────────────────────────────────────────────────────────────────

class TestCaseWhen(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            CREATE_PRODUCTS,
            "INSERT INTO products VALUES (1, widget, 10, 5, 0.1)",
            "INSERT INTO products VALUES (2, gadget, 25, 2, 0.2)",
            "INSERT INTO products VALUES (3, doohickey, 5, 10, 0.05)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_case_when_basic(self):
        """CASE WHEN price > 20 THEN expensive ELSE cheap END."""
        _, lines = db_run([
            "SELECT name, CASE WHEN price > 20 THEN expensive ELSE cheap END AS tier "
            "FROM products ORDER BY id",
            ".exit",
        ], self.db)
        # gadget (price=25) → expensive; widget and doohickey → cheap
        self.assertTrue(any("expensive" in l and "gadget" in l for l in lines))
        self.assertTrue(any("cheap" in l and "widget" in l for l in lines))
        self.assertTrue(any("cheap" in l and "doohickey" in l for l in lines))

    def test_case_when_multiple_branches(self):
        """CASE WHEN price < 8 THEN low WHEN price < 15 THEN mid ELSE high END."""
        _, lines = db_run([
            "SELECT name, CASE WHEN price < 8 THEN low "
            "WHEN price < 15 THEN mid ELSE high END AS band "
            "FROM products ORDER BY id",
            ".exit",
        ], self.db)
        self.assertTrue(any("mid" in l and "widget" in l for l in lines))
        self.assertTrue(any("high" in l and "gadget" in l for l in lines))
        self.assertTrue(any("low" in l and "doohickey" in l for l in lines))

    def test_case_when_no_else_returns_null(self):
        """CASE WHEN price > 100 THEN overpriced END → NULL for all rows here."""
        _, lines = db_run([
            "SELECT CASE WHEN price > 100 THEN overpriced END FROM products WHERE id = 1",
            ".exit",
        ], self.db)
        self.assertTrue(any("NULL" in l for l in lines))


if __name__ == "__main__":
    unittest.main()
