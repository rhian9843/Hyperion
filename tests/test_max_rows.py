"""Tests for max_rows guard on Database and Cursor."""
import pytest
from hyperion import Database
from hyperion.executor import TooManyRowsError


def _db_with_rows(n: int) -> Database:
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER)")
    for i in range(n):
        db.execute(f"INSERT INTO t VALUES ({i})")
    return db


# ── Connection-level max_rows ─────────────────────────────────────────────────

def test_max_rows_default_is_none():
    db = Database(":memory:")
    assert db.max_rows is None
    db.close()


def test_connection_max_rows_allows_within_limit():
    db = _db_with_rows(10)
    db.max_rows = 10
    rows = db.execute("SELECT * FROM t").fetchall()
    assert len(rows) == 10
    db.close()


def test_connection_max_rows_raises_when_exceeded():
    db = _db_with_rows(10)
    db.max_rows = 5
    with pytest.raises(TooManyRowsError):
        db.execute("SELECT * FROM t").fetchall()
    db.close()


def test_connection_max_rows_raises_on_fetchone_loop():
    db = _db_with_rows(10)
    db.max_rows = 3
    cur = db.execute("SELECT * FROM t ORDER BY id")
    cur.fetchone()
    cur.fetchone()
    cur.fetchone()
    with pytest.raises(TooManyRowsError):
        cur.fetchone()
    db.close()


def test_connection_max_rows_none_means_no_limit():
    db = _db_with_rows(1000)
    db.max_rows = None
    rows = db.execute("SELECT * FROM t").fetchall()
    assert len(rows) == 1000
    db.close()


# ── Per-query max_rows override ───────────────────────────────────────────────

def test_per_query_max_rows_overrides_connection_default():
    db = _db_with_rows(20)
    db.max_rows = 100       # lenient connection default
    with pytest.raises(TooManyRowsError):
        db.execute("SELECT * FROM t", max_rows=5).fetchall()
    db.close()


def test_per_query_max_rows_tighter_than_connection():
    db = _db_with_rows(20)
    db.max_rows = 10
    with pytest.raises(TooManyRowsError):
        db.execute("SELECT * FROM t", max_rows=3).fetchall()
    db.close()


def test_per_query_max_rows_overrides_connection_with_none():
    """max_rows=None on execute() disables the limit for that query."""
    db = _db_with_rows(10)
    db.max_rows = 5
    # per-query None should NOT bypass connection default — None means "use db default"
    with pytest.raises(TooManyRowsError):
        db.execute("SELECT * FROM t", max_rows=None).fetchall()
    db.close()


def test_per_query_max_rows_allows_within_limit():
    db = _db_with_rows(5)
    rows = db.execute("SELECT * FROM t", max_rows=10).fetchall()
    assert len(rows) == 5
    db.close()


# ── Error message ─────────────────────────────────────────────────────────────

def test_error_message_contains_limit():
    db = _db_with_rows(10)
    with pytest.raises(TooManyRowsError) as exc_info:
        db.execute("SELECT * FROM t", max_rows=3).fetchall()
    assert "3" in str(exc_info.value)
    assert "max_rows" in str(exc_info.value)
    db.close()


# ── DML unaffected ────────────────────────────────────────────────────────────

def test_max_rows_does_not_affect_insert():
    db = Database(":memory:")
    db.max_rows = 1
    db.execute("CREATE TABLE t (id INTEGER)")
    db.execute("INSERT INTO t VALUES (1)")
    db.execute("INSERT INTO t VALUES (2)")
    db.close()


def test_max_rows_of_1_allows_single_row_select():
    db = _db_with_rows(5)
    db.max_rows = 1
    row = db.execute("SELECT * FROM t LIMIT 1").fetchone()
    assert row is not None
    db.close()


# ── fetchmany respects guard ──────────────────────────────────────────────────

def test_fetchmany_raises_when_limit_exceeded():
    db = _db_with_rows(10)
    db.max_rows = 3
    cur = db.execute("SELECT * FROM t")
    with pytest.raises(TooManyRowsError):
        cur.fetchmany(10)
    db.close()
