"""Tests for NULL propagation through nested function calls and expressions.

Verifies that eval_expr returns None for all NULL-producing expressions and
never raises an unhandled exception (TypeError, ZeroDivisionError, etc.).
"""
import pytest
from hyperion import Database


def _q(expr: str):
    """Evaluate a scalar expression via SELECT and return its value."""
    db = Database(":memory:")
    rows = db.execute(f"SELECT {expr} AS v").fetchall()
    return rows[0]["v"]


# ── Scalar function NULL propagation ─────────────────────────────────────────

def test_upper_null():
    assert _q("UPPER(NULL)") is None

def test_lower_null():
    assert _q("LOWER(NULL)") is None

def test_length_null():
    assert _q("LENGTH(NULL)") is None

def test_abs_null():
    assert _q("ABS(NULL)") is None

def test_round_null():
    assert _q("ROUND(NULL, 2)") is None

def test_round_null_no_places():
    assert _q("ROUND(NULL)") is None

def test_substr_null_string():
    assert _q("SUBSTR(NULL, 1, 3)") is None

def test_substr_null_start():
    assert _q("SUBSTR('hello', NULL, 3)") is None

def test_trim_null():
    assert _q("TRIM(NULL)") is None

def test_ltrim_null():
    assert _q("LTRIM(NULL)") is None

def test_rtrim_null():
    assert _q("RTRIM(NULL)") is None

def test_replace_null_string():
    assert _q("REPLACE(NULL, 'a', 'b')") is None

def test_replace_null_pattern():
    assert _q("REPLACE('hello', NULL, 'b')") is None

def test_instr_null():
    assert _q("INSTR(NULL, 'x')") is None

def test_typeof_null():
    # TYPEOF(NULL) returns 'null', not Python None
    assert _q("TYPEOF(NULL)") == "null"


# ── Arithmetic NULL propagation ───────────────────────────────────────────────

def test_null_plus_int():
    assert _q("NULL + 1") is None

def test_null_minus_int():
    assert _q("NULL - 1") is None

def test_null_times_int():
    assert _q("NULL * 5") is None

def test_null_divide_int():
    assert _q("NULL / 2") is None

def test_int_divide_null():
    assert _q("10 / NULL") is None

def test_null_modulo():
    assert _q("NULL % 3") is None

def test_null_concat():
    # NULL || 'x' → NULL (standard SQL NULL propagation in concatenation)
    assert _q("NULL || 'x'") is None

def test_null_concat_both():
    assert _q("NULL || NULL") is None


# ── Nested function calls ─────────────────────────────────────────────────────

def test_coalesce_nullif_abs_null():
    # NULLIF(NULL, NULL) → NULL (first arg NULL), ABS(NULL) → NULL,
    # COALESCE(NULL, NULL) → NULL
    assert _q("COALESCE(NULLIF(NULL, NULL), ABS(NULL))") is None

def test_round_sum_null():
    assert _q("ROUND(NULL, 2)") is None

def test_upper_null_concat():
    # UPPER(NULL) || 'x' → NULL || 'x' → NULL
    assert _q("UPPER(NULL) || 'x'") is None

def test_abs_of_expression_with_null():
    assert _q("ABS(NULL + 5)") is None

def test_length_of_null_concat():
    assert _q("LENGTH(NULL || 'abc')") is None

def test_coalesce_all_null():
    assert _q("COALESCE(NULL, NULL, NULL)") is None

def test_coalesce_first_non_null():
    assert _q("COALESCE(NULL, NULL, 42)") == 42

def test_coalesce_first_value():
    assert _q("COALESCE(7, NULL, 99)") == 7


# ── NULLIF ────────────────────────────────────────────────────────────────────

def test_nullif_null_any():
    # NULLIF(NULL, x) → NULL (first arg is NULL — condition is UNKNOWN → ELSE = NULL)
    assert _q("NULLIF(NULL, 5)") is None

def test_nullif_equal():
    assert _q("NULLIF(5, 5)") is None

def test_nullif_not_equal():
    assert _q("NULLIF(5, 3)") == 5

def test_nullif_null_null():
    # NULLIF(NULL, NULL): CASE WHEN NULL=NULL (UNKNOWN) THEN NULL ELSE NULL → NULL
    assert _q("NULLIF(NULL, NULL)") is None


# ── IFNULL ────────────────────────────────────────────────────────────────────

def test_ifnull_null_fallback():
    assert _q("IFNULL(NULL, 'fallback')") == "fallback"

def test_ifnull_non_null():
    assert _q("IFNULL('value', 'fallback')") == "value"

def test_ifnull_null_null():
    assert _q("IFNULL(NULL, NULL)") is None


# ── CASE WHEN with NULL condition ─────────────────────────────────────────────

def test_case_when_null_condition_with_else():
    # NULL condition is UNKNOWN → does not match WHEN → falls to ELSE
    assert _q("CASE WHEN NULL THEN 1 ELSE 2 END") == 2

def test_case_when_null_condition_no_else():
    # NULL condition, no ELSE → NULL
    assert _q("CASE WHEN NULL THEN 1 END") is None

def test_case_when_true_returns_value():
    assert _q("CASE WHEN 1 THEN 'yes' ELSE 'no' END") == "yes"

def test_case_nested_null():
    # Inner CASE produces NULL, outer COALESCE catches it
    result = _q("COALESCE(CASE WHEN NULL THEN 1 END, 99)")
    assert result == 99


# ── NULL in WHERE / comparison ────────────────────────────────────────────────

def test_null_eq_null_returns_no_rows():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (x INTEGER)")
    db.execute("INSERT INTO t VALUES (NULL)")
    rows = db.execute("SELECT x FROM t WHERE x = NULL").fetchall()
    assert rows == []

def test_is_null_matches():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (x INTEGER)")
    db.execute("INSERT INTO t VALUES (NULL)")
    rows = db.execute("SELECT x FROM t WHERE x IS NULL").fetchall()
    assert len(rows) == 1
    assert rows[0]["x"] is None

def test_null_comparison_returns_null():
    # NULL = 1 should produce NULL (no rows match)
    db = Database(":memory:")
    db.execute("CREATE TABLE t (x INTEGER)")
    db.execute("INSERT INTO t VALUES (1)")
    db.execute("INSERT INTO t VALUES (NULL)")
    rows = db.execute("SELECT x FROM t WHERE x = 1").fetchall()
    assert len(rows) == 1
    assert rows[0]["x"] == 1


# ── No unhandled exceptions on any NULL expression ───────────────────────────

@pytest.mark.parametrize("expr", [
    "COALESCE(NULLIF(NULL, NULL), ABS(NULL))",
    "ROUND(NULL, 2)",
    "UPPER(NULL) || 'x'",
    "CASE WHEN NULL THEN 1 ELSE 2 END",
    "CASE WHEN NULL THEN 1 END",
    "NULL + 1",
    "NULL - NULL",
    "NULL * NULL",
    "NULL / NULL",
    "NULL % NULL",
    "NULL || NULL",
    "ABS(NULL)",
    "LENGTH(NULL)",
    "SUBSTR(NULL, 1)",
    "TRIM(NULL)",
    "UPPER(NULL)",
    "LOWER(NULL)",
    "ROUND(NULL)",
    "IFNULL(NULL, NULL)",
    "NULLIF(NULL, NULL)",
    "COALESCE(NULL)",
    "COALESCE(NULL, NULL)",
    "TYPEOF(NULL)",
])
def test_no_exception_on_null_expr(expr):
    """None of these expressions should raise an unhandled exception."""
    try:
        result = _q(expr)
        # result is either None or a valid value — both acceptable
    except Exception as e:
        pytest.fail(f"Expression {expr!r} raised {type(e).__name__}: {e}")
