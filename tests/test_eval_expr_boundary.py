"""Adversarial eval_expr boundary input tests.

Verifies the expression evaluator handles edge-case and malformed inputs
without raising unhandled exceptions (TypeError, ZeroDivisionError,
AttributeError, KeyError, etc.).  All cases must either return None/NULL
or raise a well-typed HyperionError subclass — never an internal exception.
"""
import pytest
from hyperion import Database
from hyperion.errors import HyperionError


def _q(expr: str):
    """Evaluate a scalar SQL expression and return its Python value."""
    db = Database(":memory:")
    rows = db.execute(f"SELECT {expr} AS v").fetchall()
    return rows[0]["v"]


def _q_raises(expr: str):
    """Return True if expr raises HyperionError, False if it returns a value."""
    db = Database(":memory:")
    try:
        db.execute(f"SELECT {expr} AS v").fetchall()
        return False
    except HyperionError:
        return True


# ── Integer / float division and modulo by zero ───────────────────────────────

def test_integer_divide_by_zero_returns_null():
    # SQLite returns NULL for integer division by zero
    assert _q("10 / 0") is None


def test_float_divide_by_zero_returns_null():
    assert _q("10.0 / 0") is None


def test_modulo_by_zero_returns_null():
    assert _q("10 % 0") is None


def test_null_divide_by_zero_returns_null():
    assert _q("NULL / 0") is None


def test_zero_divide_by_zero_returns_null():
    assert _q("0 / 0") is None


# ── Type mismatch in arithmetic ───────────────────────────────────────────────

def test_string_plus_int_returns_null_or_coerces():
    # 'abc' + 1 — SQLite coerces 'abc' to 0, so result is 1; or returns NULL
    result = _q("'abc' + 1")
    assert result is None or isinstance(result, (int, float))


def test_string_times_string():
    result = _q("'hello' * 'world'")
    assert result is None or isinstance(result, (int, float))


def test_string_minus_null():
    assert _q("'hello' - NULL") is None


def test_bool_arithmetic():
    # TRUE = 1, FALSE = 0 in SQL
    result = _q("TRUE + 1")
    assert result in (None, 2, 1)


# ── Deeply nested CASE WHEN (5 levels) ───────────────────────────────────────

def test_deeply_nested_case_5_levels():
    expr = (
        "CASE WHEN 1 THEN "
        "  CASE WHEN 1 THEN "
        "    CASE WHEN 1 THEN "
        "      CASE WHEN 1 THEN "
        "        CASE WHEN 1 THEN 'deep' ELSE 'e4' END "
        "      ELSE 'e3' END "
        "    ELSE 'e2' END "
        "  ELSE 'e1' END "
        "ELSE 'e0' END"
    )
    assert _q(expr) == "deep"


def test_deeply_nested_case_all_false():
    expr = (
        "CASE WHEN 0 THEN "
        "  CASE WHEN 0 THEN "
        "    CASE WHEN 0 THEN "
        "      CASE WHEN 0 THEN 'never' ELSE NULL END "
        "    ELSE NULL END "
        "  ELSE NULL END "
        "ELSE 'fallback' END"
    )
    assert _q(expr) == "fallback"


def test_deeply_nested_case_null_condition():
    expr = (
        "CASE WHEN NULL THEN "
        "  CASE WHEN NULL THEN 'a' ELSE 'b' END "
        "ELSE "
        "  CASE WHEN 1 THEN 'c' ELSE 'd' END "
        "END"
    )
    assert _q(expr) == "c"


# ── Column not in row dict (evaluated against empty context) ─────────────────

def test_unknown_column_in_expression_on_table():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
    db.execute("INSERT INTO t VALUES (1, 'hello')")
    # ghost_col doesn't exist — engine silently returns NULL or raises a
    # HyperionError; either is acceptable; must not raise an internal exception
    try:
        db.execute("SELECT ghost_col FROM t").fetchall()
    except HyperionError:
        pass


def test_empty_string_in_expression():
    # Empty string literal — valid SQL value
    assert _q("''") == ""


def test_empty_string_in_length():
    assert _q("LENGTH('')") == 0


def test_empty_string_in_upper():
    assert _q("UPPER('')") == ""


def test_empty_string_concat():
    assert _q("'' || 'x'") == "x"


# ── Numeric boundary values ───────────────────────────────────────────────────

def test_int64_max_literal():
    v = _q("9223372036854775807")
    assert v == 9223372036854775807


def test_int64_min_literal():
    # -9223372036854775808 as a literal
    v = _q("-9223372036854775808")
    assert v == -9223372036854775808


def test_very_large_float():
    result = _q("1.7976931348623157e+308")
    assert result is not None


def test_very_small_float():
    result = _q("5e-324")
    assert result is not None


def test_nan_handling():
    # NaN via 0.0/0.0 — should return NULL or raise HyperionError, not crash
    result = _q("CAST('nan' AS REAL)")
    # Any return (None, float, string) is acceptable as long as no internal crash


def test_inf_handling():
    result = _q("CAST('inf' AS REAL)")
    # Any return is acceptable as long as no internal crash


# ── ABS, ROUND, CEIL, FLOOR on edge values ───────────────────────────────────

def test_abs_int64_min():
    # ABS(INT64_MIN) overflows in two's complement — should return NULL or large positive
    result = _q("ABS(-9223372036854775808)")
    # As long as it doesn't raise an internal exception, any result is acceptable

def test_round_huge_integer():
    result = _q("ROUND(9999999999999999, 0)")
    assert result is not None


def test_round_negative_places():
    # ROUND(1234, -2) — SQLite returns 1200
    result = _q("ROUND(1234, -2)")
    # Accept any numeric result or NULL — must not raise
    assert result is None or isinstance(result, (int, float))


def test_ceil_null():
    assert _q("CEIL(NULL)") is None


def test_floor_null():
    assert _q("FLOOR(NULL)") is None


# ── String functions on NULL and edge inputs ──────────────────────────────────

def test_substr_zero_length():
    result = _q("SUBSTR('hello', 1, 0)")
    assert result == "" or result is None


def test_substr_negative_start():
    # SQLite: SUBSTR('hello', -2) counts from end → 'lo'
    result = _q("SUBSTR('hello', -2)")
    assert result is not None


def test_substr_start_beyond_end():
    result = _q("SUBSTR('hello', 100)")
    assert result == "" or result is None


def test_substr_negative_length():
    result = _q("SUBSTR('hello', 2, -1)")
    assert result is None or isinstance(result, str)


def test_instr_empty_needle():
    # INSTR('hello', '') — SQLite returns 0
    result = _q("INSTR('hello', '')")
    assert result is not None


def test_instr_empty_haystack():
    result = _q("INSTR('', 'x')")
    assert result == 0 or result is None


def test_replace_empty_pattern():
    # REPLACE('hello', '', 'X') — SQLite returns 'hello'
    result = _q("REPLACE('hello', '', 'X')")
    assert result is not None


def test_trim_empty_string():
    assert _q("TRIM('')") == ""


def test_length_of_number():
    # LENGTH(42) — SQLite returns 2 (length of the string representation)
    result = _q("LENGTH(42)")
    assert result is not None


# ── COALESCE / IFNULL / NULLIF edge cases ────────────────────────────────────

def test_coalesce_single_non_null():
    assert _q("COALESCE(42)") == 42


def test_coalesce_zero_is_not_null():
    assert _q("COALESCE(0, 99)") == 0


def test_coalesce_empty_string_is_not_null():
    assert _q("COALESCE('', 'fallback')") == ""


def test_nullif_both_null():
    assert _q("NULLIF(NULL, NULL)") is None


def test_ifnull_zero_is_not_null():
    assert _q("IFNULL(0, 99)") == 0


# ── Modulo and arithmetic with floats ────────────────────────────────────────

def test_float_modulo():
    result = _q("5.5 % 2")
    assert result is not None


def test_negative_modulo():
    result = _q("-7 % 3")
    assert result is not None


# ── No unhandled internal exceptions on any of the above ─────────────────────

@pytest.mark.parametrize("expr", [
    "10 / 0",
    "10.0 / 0",
    "10 % 0",
    "NULL / 0",
    "0 / 0",
    "'abc' + 1",
    "'hello' * 'world'",
    "ABS(NULL)",
    "ROUND(NULL)",
    "CEIL(NULL)",
    "FLOOR(NULL)",
    "SUBSTR(NULL, 1)",
    "SUBSTR('hello', NULL)",
    "SUBSTR('hello', 1, 0)",
    "SUBSTR('hello', 100)",
    "INSTR(NULL, 'x')",
    "INSTR('hello', '')",
    "REPLACE(NULL, 'a', 'b')",
    "REPLACE('hello', '', 'X')",
    "TRIM(NULL)",
    "LENGTH(NULL)",
    "UPPER(NULL)",
    "LOWER(NULL)",
    "COALESCE(NULL)",
    "NULLIF(NULL, NULL)",
    "IFNULL(NULL, NULL)",
    "CAST(NULL AS INTEGER)",
    "CAST(NULL AS TEXT)",
    "CAST(NULL AS REAL)",
    "NULL + NULL",
    "NULL * NULL",
    "NULL || NULL",
    "TYPEOF(NULL)",
    "TYPEOF(1)",
    "TYPEOF(1.5)",
    "TYPEOF('x')",
    "CASE WHEN NULL THEN 1 END",
    "CASE WHEN NULL THEN 1 ELSE 2 END",
])
def test_no_internal_exception(expr):
    """None of these must raise an unhandled non-Hyperion exception."""
    db = Database(":memory:")
    try:
        db.execute(f"SELECT {expr} AS v").fetchall()
    except HyperionError:
        pass  # well-typed error is acceptable
    except Exception as e:
        pytest.fail(
            f"Expression {expr!r} raised unexpected {type(e).__name__}: {e}"
        )
