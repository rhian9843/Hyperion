"""Tests for agent-layer bug fixes: cursor.description, lastrowid, query timeout."""
import time
import pytest
from hyperion import Database, QueryTimeoutError


# ── cursor.description on empty result sets ───────────────────────────────────

def test_description_nonempty():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    db.execute("INSERT INTO t VALUES (1, 'a')")
    cur = db.execute("SELECT id, name FROM t")
    assert cur.description is not None
    names = [d[0] for d in cur.description]
    assert names == ["id", "name"]


def test_description_empty_explicit_cols():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    cur = db.execute("SELECT id, name FROM t WHERE id = -999")
    assert cur.description is not None
    names = [d[0] for d in cur.description]
    assert names == ["id", "name"]


def test_description_empty_star():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, val REAL)")
    cur = db.execute("SELECT * FROM t WHERE id = -999")
    assert cur.description is not None
    names = [d[0] for d in cur.description]
    assert names == ["id", "val"]


def test_description_select_nofrom():
    db = Database(":memory:")
    cur = db.execute("SELECT 1+1 AS result")
    assert cur.description is not None
    assert cur.description[0][0] == "result"


def test_description_non_select_is_none():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER)")
    cur = db.execute("INSERT INTO t VALUES (1)")
    assert cur.description is None


# ── cursor.lastrowid ──────────────────────────────────────────────────────────

def test_lastrowid_after_insert():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
    cur = db.execute("INSERT INTO t VALUES (42, 'hello')")
    # INTEGER PRIMARY KEY aliases the B-tree rowid, so lastrowid == supplied PK value
    assert cur.lastrowid == 42


def test_lastrowid_sequential():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, val TEXT)")
    cur1 = db.execute("INSERT INTO t (val) VALUES ('a')")
    cur2 = db.execute("INSERT INTO t (val) VALUES ('b')")
    assert cur1.lastrowid == 1
    assert cur2.lastrowid == 2


def test_lastrowid_non_insert_is_none():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER)")
    db.execute("INSERT INTO t VALUES (1)")
    cur = db.execute("UPDATE t SET id = 2")
    assert cur.lastrowid is None


def test_lastrowid_select_is_none():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER)")
    db.execute("INSERT INTO t VALUES (1)")
    cur = db.execute("SELECT * FROM t")
    assert cur.lastrowid is None


# ── last_insert_rowid() SQL function ─────────────────────────────────────────

def test_last_insert_rowid_sql_function():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
    db.execute("INSERT INTO t VALUES (7, 'x')")
    cur = db.execute("SELECT last_insert_rowid()")
    row = cur.fetchone()
    assert list(row.values())[0] == 7


def test_last_insert_rowid_updates_per_insert():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    db.execute("INSERT INTO t VALUES (10)")
    db.execute("INSERT INTO t VALUES (20)")
    r = db.execute("SELECT last_insert_rowid()").fetchone()
    assert list(r.values())[0] == 20


# ── Query timeout ─────────────────────────────────────────────────────────────

def test_timeout_raises_on_slow_query():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, val TEXT)")
    db.executemany("INSERT INTO t VALUES (?, 'x')", [(i,) for i in range(100)])
    # timeout_ms=0 sets the deadline to "right now"; the first timeout check fires
    with pytest.raises(QueryTimeoutError):
        db.execute("SELECT * FROM t", timeout_ms=0)


def test_timeout_clears_after_execution():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER)")
    db.execute("INSERT INTO t VALUES (1)")
    # Fast query with very short timeout should still clear deadline
    try:
        db.execute("SELECT * FROM t", timeout_ms=10000)
    except QueryTimeoutError:
        pass
    assert getattr(db, "_query_deadline", None) is None


def test_no_timeout_by_default():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER)")
    for i in range(100):
        db.execute(f"INSERT INTO t VALUES ({i})")
    cur = db.execute("SELECT * FROM t")
    assert len(cur.fetchall()) == 100


def test_timeout_error_is_runtime_error():
    assert issubclass(QueryTimeoutError, RuntimeError)
