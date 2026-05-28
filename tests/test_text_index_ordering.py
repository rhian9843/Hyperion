"""Tests for TEXT index sort-order preservation (range predicates, BETWEEN, ORDER BY)."""
import pytest
from hyperion import Database


def _setup(names):
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    db.execute("CREATE INDEX idx_name ON t(name)")
    for i, name in enumerate(names):
        db.execute("INSERT INTO t VALUES (?, ?)", (i, name))
    return db


NAMES = ["Alice", "Bob", "Charlie", "Dave", "Eve", "Mallory", "Zara"]


# ── range predicates via index ────────────────────────────────────────────────

def test_text_gt():
    db = _setup(NAMES)
    rows = db.execute("SELECT name FROM t WHERE name > 'M' ORDER BY name").fetchall()
    assert [r["name"] for r in rows] == ["Mallory", "Zara"]


def test_text_gte():
    db = _setup(NAMES)
    rows = db.execute("SELECT name FROM t WHERE name >= 'M' ORDER BY name").fetchall()
    assert [r["name"] for r in rows] == ["Mallory", "Zara"]


def test_text_lt():
    db = _setup(NAMES)
    rows = db.execute("SELECT name FROM t WHERE name < 'C' ORDER BY name").fetchall()
    assert [r["name"] for r in rows] == ["Alice", "Bob"]


def test_text_lte():
    db = _setup(NAMES)
    rows = db.execute("SELECT name FROM t WHERE name <= 'C' ORDER BY name").fetchall()
    # 'C' < 'Charlie', so only Alice and Bob
    assert [r["name"] for r in rows] == ["Alice", "Bob"]


def test_text_between_exclusive_boundary():
    db = _setup(NAMES)
    rows = db.execute(
        "SELECT name FROM t WHERE name BETWEEN 'C' AND 'E' ORDER BY name"
    ).fetchall()
    # 'Eve' > 'E', so not included
    assert [r["name"] for r in rows] == ["Charlie", "Dave"]


def test_text_between_inclusive_boundary():
    db = _setup(NAMES)
    rows = db.execute(
        "SELECT name FROM t WHERE name BETWEEN 'C' AND 'F' ORDER BY name"
    ).fetchall()
    assert [r["name"] for r in rows] == ["Charlie", "Dave", "Eve"]


def test_text_and_combination():
    db = _setup(NAMES)
    rows = db.execute(
        "SELECT name FROM t WHERE name >= 'B' AND name < 'D' ORDER BY name"
    ).fetchall()
    assert [r["name"] for r in rows] == ["Bob", "Charlie"]


def test_text_gte_exact_match():
    db = _setup(NAMES)
    rows = db.execute(
        "SELECT name FROM t WHERE name >= 'Alice' ORDER BY name"
    ).fetchall()
    assert rows[0]["name"] == "Alice"


def test_text_lte_exact_match():
    db = _setup(NAMES)
    rows = db.execute(
        "SELECT name FROM t WHERE name <= 'Zara' ORDER BY name"
    ).fetchall()
    assert rows[-1]["name"] == "Zara"


# ── equality still works (post-encoding-change regression) ────────────────────

def test_text_equality_unchanged():
    db = _setup(NAMES)
    rows = db.execute("SELECT id FROM t WHERE name = 'Charlie'").fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == 2


def test_text_equality_no_match():
    db = _setup(NAMES)
    rows = db.execute("SELECT id FROM t WHERE name = 'Nonexistent'").fetchall()
    assert rows == []


# ── prefix-collision correctness (strings > 8 chars sharing a common prefix) ─

def test_prefix_collision_gt():
    """Strings longer than 8 chars with same prefix as the boundary must not be missed."""
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    db.execute("CREATE INDEX idx_name ON t(name)")
    long_names = ["ABCDEFGH", "ABCDEFGHIJK", "ABCDEFGHIJKLMNO", "ABCDEFGHZZ", "ABCDEFGI"]
    for i, n in enumerate(long_names):
        db.execute("INSERT INTO t VALUES (?, ?)", (i, n))
    rows = db.execute("SELECT name FROM t WHERE name > 'ABCDEFGH' ORDER BY name").fetchall()
    expected = sorted(n for n in long_names if n > "ABCDEFGH")
    assert [r["name"] for r in rows] == expected


def test_prefix_collision_gte():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    db.execute("CREATE INDEX idx_name ON t(name)")
    long_names = ["ABCDEFGH", "ABCDEFGHIJK", "ABCDEFGHZZ", "ABCDEFGI"]
    for i, n in enumerate(long_names):
        db.execute("INSERT INTO t VALUES (?, ?)", (i, n))
    rows = db.execute("SELECT name FROM t WHERE name >= 'ABCDEFGH' ORDER BY name").fetchall()
    assert [r["name"] for r in rows] == sorted(long_names)


def test_prefix_collision_lt():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    db.execute("CREATE INDEX idx_name ON t(name)")
    long_names = ["ABCDEFG", "ABCDEFGH", "ABCDEFGHIJK"]
    for i, n in enumerate(long_names):
        db.execute("INSERT INTO t VALUES (?, ?)", (i, n))
    rows = db.execute("SELECT name FROM t WHERE name < 'ABCDEFGH' ORDER BY name").fetchall()
    # Only 'ABCDEFG' (7 chars, shorter prefix) is < 'ABCDEFGH'
    assert [r["name"] for r in rows] == ["ABCDEFG"]


# ── numeric range index still works (regression guard) ────────────────────────

def test_integer_range_still_works():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, val INTEGER)")
    db.execute("CREATE INDEX idx_val ON t(val)")
    for i in range(10):
        db.execute("INSERT INTO t VALUES (?, ?)", (i, i * 10))
    rows = db.execute("SELECT id FROM t WHERE val > 50 ORDER BY id").fetchall()
    assert [r["id"] for r in rows] == [6, 7, 8, 9]
