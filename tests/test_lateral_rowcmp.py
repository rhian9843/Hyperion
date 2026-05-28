"""Tests for LATERAL join and multi-column row comparison."""
import pytest
from hyperion import Database


# ── Multi-column row comparison ───────────────────────────────────────────────

def test_row_in_subquery_basic():
    db = Database(":memory:")
    db.execute("CREATE TABLE t1 (a INTEGER, b INTEGER)")
    db.execute("CREATE TABLE t2 (x INTEGER, y INTEGER)")
    db.executemany("INSERT INTO t1 VALUES (?, ?)", [(1, 2), (3, 4), (5, 6)])
    db.executemany("INSERT INTO t2 VALUES (?, ?)", [(1, 2), (5, 6)])
    rows = db.execute(
        "SELECT a, b FROM t1 WHERE (a, b) IN (SELECT x, y FROM t2)"
    ).fetchall()
    assert len(rows) == 2
    pairs = {(r["a"], r["b"]) for r in rows}
    assert pairs == {(1, 2), (5, 6)}


def test_row_not_in_subquery():
    db = Database(":memory:")
    db.execute("CREATE TABLE t1 (a INTEGER, b INTEGER)")
    db.execute("CREATE TABLE t2 (x INTEGER, y INTEGER)")
    db.executemany("INSERT INTO t1 VALUES (?, ?)", [(1, 2), (3, 4), (5, 6)])
    db.executemany("INSERT INTO t2 VALUES (?, ?)", [(1, 2), (5, 6)])
    rows = db.execute(
        "SELECT a, b FROM t1 WHERE (a, b) NOT IN (SELECT x, y FROM t2)"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["a"] == 3 and rows[0]["b"] == 4


def test_row_in_returns_no_rows_when_empty():
    db = Database(":memory:")
    db.execute("CREATE TABLE t1 (a INTEGER, b INTEGER)")
    db.execute("CREATE TABLE t2 (x INTEGER, y INTEGER)")
    db.executemany("INSERT INTO t1 VALUES (?, ?)", [(1, 2), (3, 4)])
    rows = db.execute(
        "SELECT a FROM t1 WHERE (a, b) IN (SELECT x, y FROM t2)"
    ).fetchall()
    assert rows == []


def test_row_eq_literal_tuple():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (a INTEGER, b TEXT)")
    db.executemany("INSERT INTO t VALUES (?, ?)", [(1, "hello"), (2, "world"), (1, "bye")])
    rows = db.execute("SELECT a, b FROM t WHERE (a, b) = (1, 'hello')").fetchall()
    assert len(rows) == 1
    assert rows[0]["a"] == 1 and rows[0]["b"] == "hello"


def test_row_neq_literal_tuple():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (a INTEGER, b TEXT)")
    db.executemany("INSERT INTO t VALUES (?, ?)", [(1, "hello"), (2, "world")])
    rows = db.execute("SELECT a, b FROM t WHERE (a, b) != (1, 'hello')").fetchall()
    assert len(rows) == 1
    assert rows[0]["a"] == 2


def test_row_in_combined_with_and():
    db = Database(":memory:")
    db.execute("CREATE TABLE t1 (a INTEGER, b INTEGER, c INTEGER)")
    db.execute("CREATE TABLE t2 (x INTEGER, y INTEGER)")
    db.executemany("INSERT INTO t1 VALUES (?, ?, ?)", [(1, 2, 10), (3, 4, 20), (1, 2, 30)])
    db.executemany("INSERT INTO t2 VALUES (?, ?)", [(1, 2)])
    rows = db.execute(
        "SELECT c FROM t1 WHERE (a, b) IN (SELECT x, y FROM t2) AND c > 15"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["c"] == 30


# ── LATERAL join ──────────────────────────────────────────────────────────────

def test_lateral_basic_comma_form():
    db = Database(":memory:")
    db.execute("CREATE TABLE users (id INTEGER, name TEXT)")
    db.execute("CREATE TABLE orders (user_id INTEGER, amount INTEGER)")
    db.executemany("INSERT INTO users VALUES (?, ?)", [(1, "Alice"), (2, "Bob")])
    db.executemany("INSERT INTO orders VALUES (?, ?)", [
        (1, 100), (1, 200), (2, 50)
    ])
    rows = db.execute(
        "SELECT u.name, o.amount "
        "FROM users u, LATERAL (SELECT amount FROM orders WHERE user_id = u.id) AS o "
        "ORDER BY u.name, o.amount"
    ).fetchall()
    assert len(rows) == 3
    names = [r["u.name"] for r in rows]
    assert names == ["Alice", "Alice", "Bob"]


def test_lateral_no_match_rows_excluded():
    db = Database(":memory:")
    db.execute("CREATE TABLE users (id INTEGER, name TEXT)")
    db.execute("CREATE TABLE orders (user_id INTEGER, amount INTEGER)")
    db.executemany("INSERT INTO users VALUES (?, ?)", [(1, "Alice"), (2, "Bob")])
    db.executemany("INSERT INTO orders VALUES (?, ?)", [(1, 100)])
    rows = db.execute(
        "SELECT u.name FROM users u, "
        "LATERAL (SELECT amount FROM orders WHERE user_id = u.id) AS o "
        "ORDER BY u.name"
    ).fetchall()
    # Bob has no orders → no row for Bob (INNER semantics)
    assert len(rows) == 1
    assert rows[0]["u.name"] == "Alice"


def test_lateral_with_limit():
    db = Database(":memory:")
    db.execute("CREATE TABLE users (id INTEGER, name TEXT)")
    db.execute("CREATE TABLE orders (user_id INTEGER, amount INTEGER)")
    db.executemany("INSERT INTO users VALUES (?, ?)", [(1, "Alice"), (2, "Bob")])
    db.executemany("INSERT INTO orders VALUES (?, ?)", [
        (1, 300), (1, 100), (1, 200), (2, 50), (2, 80)
    ])
    rows = db.execute(
        "SELECT u.name, o.amount "
        "FROM users u, LATERAL (SELECT amount FROM orders WHERE user_id = u.id "
        "                        ORDER BY amount DESC LIMIT 1) AS o "
        "ORDER BY u.name"
    ).fetchall()
    assert len(rows) == 2
    by_user = {r["u.name"]: r["o.amount"] for r in rows}
    assert by_user["Alice"] == 300
    assert by_user["Bob"] == 80


def test_lateral_aggregate():
    db = Database(":memory:")
    db.execute("CREATE TABLE users (id INTEGER, name TEXT)")
    db.execute("CREATE TABLE orders (user_id INTEGER, amount INTEGER)")
    db.executemany("INSERT INTO users VALUES (?, ?)", [(1, "Alice"), (2, "Bob")])
    db.executemany("INSERT INTO orders VALUES (?, ?)", [
        (1, 100), (1, 200), (2, 50)
    ])
    rows = db.execute(
        "SELECT u.name, s.total "
        "FROM users u, "
        "LATERAL (SELECT SUM(amount) AS total FROM orders WHERE user_id = u.id) AS s "
        "ORDER BY u.name"
    ).fetchall()
    assert len(rows) == 2
    by_user = {r["u.name"]: r["s.total"] for r in rows}
    assert by_user["Alice"] == 300
    assert by_user["Bob"] == 50


def test_lateral_join_on_true():
    db = Database(":memory:")
    db.execute("CREATE TABLE users (id INTEGER, name TEXT)")
    db.execute("CREATE TABLE orders (user_id INTEGER, amount INTEGER)")
    db.executemany("INSERT INTO users VALUES (?, ?)", [(1, "Alice"), (2, "Bob")])
    db.executemany("INSERT INTO orders VALUES (?, ?)", [(1, 10), (1, 20), (2, 5)])
    rows = db.execute(
        "SELECT u.name, o.amount "
        "FROM users u "
        "JOIN LATERAL (SELECT amount FROM orders WHERE user_id = u.id) AS o ON true "
        "ORDER BY u.name, o.amount"
    ).fetchall()
    assert len(rows) == 3
    assert [r["o.amount"] for r in rows if r["u.name"] == "Alice"] == [10, 20]
