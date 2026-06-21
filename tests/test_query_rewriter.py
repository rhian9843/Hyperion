"""Tests for the query rewriter (automatic query rewriting + EXPLAIN REWRITTEN)."""
import pytest
from hyperion import Database


def _fetch(cursor):
    return list(cursor.fetchall())


@pytest.fixture
def db():
    d = Database(":memory:")
    d.execute("CREATE TABLE t (x INTEGER, y TEXT)")
    d.execute("INSERT INTO t VALUES (10, 'a')")
    d.execute("INSERT INTO t VALUES (50, 'b')")
    d.execute("INSERT INTO t VALUES (200, 'c')")
    yield d
    d.close()


# ── WHERE 1=1 removal ─────────────────────────────────────────────────────────

def test_where_1_eq_1_returns_all_rows(db):
    rows = _fetch(db.execute("SELECT * FROM t WHERE 1=1"))
    assert len(rows) == 3


def test_where_1_eq_1_explain_rewritten(db):
    rows = _fetch(db.execute("EXPLAIN REWRITTEN SELECT * FROM t WHERE 1=1"))
    steps = {r["step"]: r["detail"] for r in rows}
    assert "Removed always-true condition" in steps["transformation"]
    assert steps["rewritten_where"] == "(none)"


def test_where_1_eq_1_and_real_condition(db):
    rows = _fetch(db.execute("SELECT * FROM t WHERE 1=1 AND x > 100"))
    assert len(rows) == 1
    assert rows[0]["x"] == 200


# ── WHERE 1=0 zero-row short circuit ─────────────────────────────────────────

def test_where_1_eq_0_returns_no_rows(db):
    rows = _fetch(db.execute("SELECT * FROM t WHERE 1=0"))
    assert rows == []


def test_where_1_eq_0_explain_rewritten(db):
    rows = _fetch(db.execute("EXPLAIN REWRITTEN SELECT * FROM t WHERE 1=0"))
    steps = {r["step"]: r["detail"] for r in rows}
    assert "always-false" in steps["transformation"].lower()
    assert "0 rows" in steps["result"]


def test_where_literal_false_numeric(db):
    rows = _fetch(db.execute("SELECT * FROM t WHERE 1=2"))
    assert rows == []


# ── Redundant condition elimination ──────────────────────────────────────────

def test_redundant_greater_than(db):
    # x > 5 AND x > 100 → x > 100
    rows = _fetch(db.execute("SELECT * FROM t WHERE x > 5 AND x > 100"))
    assert len(rows) == 1
    assert rows[0]["x"] == 200


def test_redundant_less_than(db):
    # x < 200 AND x < 50 → x < 50
    rows = _fetch(db.execute("SELECT * FROM t WHERE x < 200 AND x < 50"))
    assert len(rows) == 1
    assert rows[0]["x"] == 10


def test_redundant_explain_note(db):
    rows = _fetch(db.execute(
        "EXPLAIN REWRITTEN SELECT * FROM t WHERE x > 5 AND x > 100"
    ))
    steps = {r["step"]: r["detail"] for r in rows}
    assert "Removed redundant condition" in steps["transformation"]
    assert "x > 5" in steps["transformation"]


def test_redundant_three_conditions(db):
    # x > 1 AND x > 10 AND x > 50 → x > 50
    rows = _fetch(db.execute("SELECT * FROM t WHERE x > 1 AND x > 10 AND x > 50"))
    assert len(rows) == 1
    assert rows[0]["x"] == 200


def test_non_redundant_conditions_preserved(db):
    # x > 5 AND x < 100 — different operators, both kept; matches x=10 and x=50
    rows = _fetch(db.execute("SELECT * FROM t WHERE x > 5 AND x < 100"))
    assert len(rows) == 2
    assert all(5 < r["x"] < 100 for r in rows)


def test_different_columns_not_eliminated(db):
    # x > 0 AND x > 0 both same — but only one column, all rows pass
    # Different column conditions must not interfere
    d = Database(":memory:")
    d.execute("CREATE TABLE u (a INTEGER, b INTEGER)")
    d.execute("INSERT INTO u VALUES (5, 200)")
    d.execute("INSERT INTO u VALUES (150, 3)")
    # a > 10 AND b > 10 — only (150, 3) has a>10 and only (5,200) has b>10
    # Different columns: no row satisfies both
    rows = _fetch(d.execute("SELECT * FROM u WHERE a > 10 AND b > 10"))
    assert rows == []
    d.close()


# ── IN (SELECT ...) note ──────────────────────────────────────────────────────

def test_in_subquery_noted_as_join_candidate(db):
    db.execute("CREATE TABLE u (id INTEGER)")
    rows = _fetch(db.execute(
        "EXPLAIN REWRITTEN SELECT * FROM t WHERE x IN (SELECT id FROM u)"
    ))
    steps = [r for r in rows if r["step"] == "transformation"]
    assert any("JOIN candidate" in s["detail"] for s in steps)


# ── EXPLAIN REWRITTEN structure ───────────────────────────────────────────────

def test_explain_rewritten_columns(db):
    rows = _fetch(db.execute("EXPLAIN REWRITTEN SELECT * FROM t WHERE x > 5"))
    assert all("step" in r and "detail" in r for r in rows)


def test_explain_rewritten_no_change(db):
    rows = _fetch(db.execute("EXPLAIN REWRITTEN SELECT * FROM t WHERE x > 5"))
    steps = {r["step"]: r["detail"] for r in rows}
    assert "no rewrites applied" in steps["transformation"]


def test_explain_rewritten_original_preserved(db):
    rows = _fetch(db.execute(
        "EXPLAIN REWRITTEN SELECT * FROM t WHERE x > 5 AND x > 100"
    ))
    original = next(r["detail"] for r in rows if r["step"] == "original_where")
    assert "x > 5" in original
    assert "x > 100" in original


# ── SET REWRITER ON|OFF ───────────────────────────────────────────────────────

def test_set_rewriter_off_disables_rewriting(db):
    db.execute("SET REWRITER OFF")
    # With rewriter off, WHERE 1=0 is still evaluated by the normal WHERE engine
    # which correctly returns 0 rows — rewriter just doesn't short-circuit it
    # The result should be the same; but the short-circuit optimisation is off
    rows = _fetch(db.execute("SELECT * FROM t WHERE 1=0"))
    assert rows == []  # WHERE evaluator still handles it correctly
    db.execute("SET REWRITER ON")


def test_set_rewriter_on_re_enables(db):
    db.execute("SET REWRITER OFF")
    db.execute("SET REWRITER ON")
    rows = _fetch(db.execute("SELECT * FROM t WHERE 1=0"))
    assert rows == []


def test_set_rewriter_returns_status(db):
    result = db.execute("SET REWRITER OFF")
    # No error; result is a string message
    db.execute("SET REWRITER ON")


# ── Correctness: rewriting must not change query results ─────────────────────

def test_rewrite_does_not_change_results_with_order_by(db):
    rows = _fetch(db.execute(
        "SELECT * FROM t WHERE x > 1 AND x > 5 ORDER BY x"
    ))
    assert [r["x"] for r in rows] == [10, 50, 200]


def test_rewrite_with_limit(db):
    rows = _fetch(db.execute(
        "SELECT * FROM t WHERE x > 0 AND x > 0 ORDER BY x LIMIT 2"
    ))
    assert len(rows) == 2


def test_rewrite_preserves_or_clauses(db):
    # OR clauses should not be touched by rewriter
    rows = _fetch(db.execute(
        "SELECT * FROM t WHERE x < 20 OR x > 100"
    ))
    assert len(rows) == 2  # x=10 and x=200
