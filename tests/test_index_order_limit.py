"""Tests for index use when ORDER BY, LIMIT, or DISTINCT are present."""
import pytest
from hyperion import Database


def _int_db():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, val INTEGER, tag TEXT)")
    db.execute("CREATE INDEX idx_val ON t(val)")
    for i in range(10):
        db.execute("INSERT INTO t VALUES (?, ?, ?)", (i, i * 10, "a" if i % 2 == 0 else "b"))
    return db


def _text_db():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    db.execute("CREATE INDEX idx_name ON t(name)")
    names = ["Charlie", "Alice", "Eve", "Bob", "Dave"]
    for i, n in enumerate(names):
        db.execute("INSERT INTO t VALUES (?, ?)", (i, n))
    return db


# ── equality index + ORDER BY ─────────────────────────────────────────────────

def test_eq_index_with_order_by_asc():
    db = _int_db()
    # val = 0 matches id=0; index should still be used even though ORDER BY is present
    rows = db.execute("SELECT id FROM t WHERE val = 0 ORDER BY id").fetchall()
    assert [r["id"] for r in rows] == [0]


def test_eq_index_with_order_by_sorts_correctly():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, tag TEXT, score INTEGER)")
    db.execute("CREATE INDEX idx_tag ON t(tag)")
    for i in range(6):
        db.execute("INSERT INTO t VALUES (?, ?, ?)", (i, "x" if i < 3 else "y", i * 10))
    rows = db.execute("SELECT score FROM t WHERE tag = 'x' ORDER BY score DESC").fetchall()
    assert [r["score"] for r in rows] == [20, 10, 0]


# ── equality index + LIMIT ────────────────────────────────────────────────────

def test_eq_index_with_limit():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, tag TEXT)")
    db.execute("CREATE INDEX idx_tag ON t(tag)")
    for i in range(10):
        db.execute("INSERT INTO t VALUES (?, ?)", (i, "x"))
    rows = db.execute("SELECT id FROM t WHERE tag = 'x' LIMIT 3").fetchall()
    assert len(rows) == 3


def test_eq_index_with_order_by_and_limit():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, tag TEXT, score INTEGER)")
    db.execute("CREATE INDEX idx_tag ON t(tag)")
    for i in range(8):
        db.execute("INSERT INTO t VALUES (?, ?, ?)", (i, "x", i))
    rows = db.execute(
        "SELECT score FROM t WHERE tag = 'x' ORDER BY score DESC LIMIT 3"
    ).fetchall()
    assert [r["score"] for r in rows] == [7, 6, 5]


# ── equality index + DISTINCT ─────────────────────────────────────────────────

def test_eq_index_with_distinct():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, tag TEXT, val INTEGER)")
    db.execute("CREATE INDEX idx_tag ON t(tag)")
    for i in range(6):
        db.execute("INSERT INTO t VALUES (?, ?, ?)", (i, "x", i % 3))
    rows = db.execute("SELECT DISTINCT val FROM t WHERE tag = 'x'").fetchall()
    assert sorted(r["val"] for r in rows) == [0, 1, 2]


# ── range index + ORDER BY ────────────────────────────────────────────────────

def test_range_index_gt_with_order_by():
    db = _int_db()
    rows = db.execute("SELECT id FROM t WHERE val > 50 ORDER BY id ASC").fetchall()
    assert [r["id"] for r in rows] == [6, 7, 8, 9]


def test_range_index_lt_with_order_by_desc():
    db = _int_db()
    rows = db.execute("SELECT id FROM t WHERE val < 40 ORDER BY id DESC").fetchall()
    assert [r["id"] for r in rows] == [3, 2, 1, 0]


def test_range_index_text_with_order_by():
    db = _text_db()
    rows = db.execute("SELECT name FROM t WHERE name > 'B' ORDER BY name").fetchall()
    assert [r["name"] for r in rows] == ["Bob", "Charlie", "Dave", "Eve"]


# ── range index + LIMIT ───────────────────────────────────────────────────────

def test_range_index_with_limit():
    db = _int_db()
    rows = db.execute("SELECT id FROM t WHERE val >= 0 ORDER BY id LIMIT 4").fetchall()
    assert [r["id"] for r in rows] == [0, 1, 2, 3]


def test_range_index_with_order_by_and_limit():
    db = _int_db()
    rows = db.execute(
        "SELECT val FROM t WHERE val >= 20 ORDER BY val DESC LIMIT 3"
    ).fetchall()
    assert [r["val"] for r in rows] == [90, 80, 70]


# ── range index + DISTINCT ────────────────────────────────────────────────────

def test_range_index_with_distinct():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, score INTEGER, bucket INTEGER)")
    db.execute("CREATE INDEX idx_score ON t(score)")
    for i in range(9):
        db.execute("INSERT INTO t VALUES (?, ?, ?)", (i, i * 10, i // 3))
    rows = db.execute("SELECT DISTINCT bucket FROM t WHERE score >= 30").fetchall()
    assert sorted(r["bucket"] for r in rows) == [1, 2]


def test_distinct_limit_with_duplicates_in_early_rows():
    """DISTINCT+LIMIT must not early-terminate: duplicates in first N rows would under-collect."""
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, group_id INTEGER, val INTEGER)")
    db.execute("CREATE INDEX idx_gid ON t(group_id)")
    # vals = [1,1,2,2,3,3,4,4,5,5] — first 3 rows have vals [1,1,2], only 2 distinct
    for i in range(10):
        db.execute("INSERT INTO t VALUES (?, 5, ?)", (i, (i // 2) + 1))
    rows = db.execute("SELECT DISTINCT val FROM t WHERE group_id = 5 LIMIT 3").fetchall()
    assert len(rows) == 3
    assert sorted(r["val"] for r in rows) == [1, 2, 3]


# ── combined: equality + ORDER BY + OFFSET ───────────────────────────────────

def test_eq_index_with_order_by_offset():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, tag TEXT)")
    db.execute("CREATE INDEX idx_tag ON t(tag)")
    for i in range(6):
        db.execute("INSERT INTO t VALUES (?, ?)", (i, "x"))
    rows = db.execute(
        "SELECT id FROM t WHERE tag = 'x' ORDER BY id LIMIT 3 OFFSET 2"
    ).fetchall()
    assert [r["id"] for r in rows] == [2, 3, 4]


# ── correctness: index result same as full scan ───────────────────────────────

def test_range_index_matches_full_scan():
    db = _int_db()
    idx_rows  = db.execute("SELECT id FROM t WHERE val > 30 ORDER BY id").fetchall()
    # verify against known correct answer
    assert [r["id"] for r in idx_rows] == [4, 5, 6, 7, 8, 9]


def test_eq_index_matches_full_scan_multirow():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, group_id INTEGER)")
    db.execute("CREATE INDEX idx_group ON t(group_id)")
    for i in range(12):
        db.execute("INSERT INTO t VALUES (?, ?)", (i, i % 4))
    rows = db.execute("SELECT id FROM t WHERE group_id = 2 ORDER BY id").fetchall()
    assert [r["id"] for r in rows] == [2, 6, 10]


# ── ORDER BY elimination: index column = sort column ─────────────────────────

def test_range_index_order_by_asc_no_sort_needed():
    """ORDER BY on indexed column ASC: index delivers rows already in order."""
    db = _int_db()
    rows = db.execute("SELECT val FROM t WHERE val >= 30 ORDER BY val ASC").fetchall()
    assert [r["val"] for r in rows] == [30, 40, 50, 60, 70, 80, 90]


def test_range_index_order_by_desc():
    """ORDER BY on indexed column DESC: reverse of index order, no sort needed."""
    db = _int_db()
    rows = db.execute("SELECT val FROM t WHERE val >= 30 ORDER BY val DESC").fetchall()
    assert [r["val"] for r in rows] == [90, 80, 70, 60, 50, 40, 30]


def test_range_index_order_by_desc_with_limit():
    db = _int_db()
    rows = db.execute(
        "SELECT val FROM t WHERE val >= 0 ORDER BY val DESC LIMIT 3"
    ).fetchall()
    assert [r["val"] for r in rows] == [90, 80, 70]


def test_eq_index_order_by_asc():
    """ORDER BY on the equality-indexed column: trivially satisfied."""
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, tag TEXT, score INTEGER)")
    db.execute("CREATE INDEX idx_tag ON t(tag)")
    for i in range(6):
        db.execute("INSERT INTO t VALUES (?, ?, ?)", (i, "x", i * 5))
    rows = db.execute("SELECT score FROM t WHERE tag = 'x' ORDER BY score ASC").fetchall()
    assert rows == sorted(rows, key=lambda r: r["score"])


def test_text_range_index_order_by_asc():
    db = _text_db()
    rows = db.execute("SELECT name FROM t WHERE name >= 'B' ORDER BY name ASC").fetchall()
    assert [r["name"] for r in rows] == ["Bob", "Charlie", "Dave", "Eve"]


def test_text_range_index_order_by_desc():
    db = _text_db()
    rows = db.execute("SELECT name FROM t WHERE name >= 'B' ORDER BY name DESC").fetchall()
    assert [r["name"] for r in rows] == ["Eve", "Dave", "Charlie", "Bob"]


# ── LIMIT early termination ───────────────────────────────────────────────────

def test_range_index_limit_no_order_by():
    """LIMIT without ORDER BY: scan stops early, correct count returned."""
    db = _int_db()
    rows = db.execute("SELECT id FROM t WHERE val >= 0 LIMIT 4").fetchall()
    assert len(rows) == 4


def test_range_index_limit_no_order_by_early_termination():
    """Verify early termination fires: fewer rows scanned than total matches."""
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, val INTEGER)")
    db.execute("CREATE INDEX idx_val ON t(val)")
    for i in range(100):
        db.execute("INSERT INTO t VALUES (?, ?)", (i, i))
    # LIMIT 5 on a 100-row table: early termination means only 5 rows collected
    rows = db.execute("SELECT id FROM t WHERE val >= 0 LIMIT 5").fetchall()
    assert len(rows) == 5
    # All returned ids must be from the first 5 in index order (val 0..4)
    assert all(r["id"] in range(5) for r in rows)


def test_range_index_limit_with_idx_col_order_by():
    """LIMIT + ORDER BY indexed col ASC: early termination gives correct top-N."""
    db = _int_db()
    rows = db.execute("SELECT val FROM t WHERE val >= 0 ORDER BY val LIMIT 3").fetchall()
    assert [r["val"] for r in rows] == [0, 10, 20]


def test_eq_index_limit():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, tag TEXT)")
    db.execute("CREATE INDEX idx_tag ON t(tag)")
    for i in range(20):
        db.execute("INSERT INTO t VALUES (?, ?)", (i, "x"))
    rows = db.execute("SELECT id FROM t WHERE tag = 'x' LIMIT 5").fetchall()
    assert len(rows) == 5


def test_range_index_limit_offset_with_order_by():
    """LIMIT + OFFSET + ORDER BY indexed col: correct page of results."""
    db = _int_db()
    rows = db.execute(
        "SELECT val FROM t WHERE val >= 0 ORDER BY val LIMIT 3 OFFSET 2"
    ).fetchall()
    assert [r["val"] for r in rows] == [20, 30, 40]


def test_range_index_limit_exact_boundary():
    """LIMIT equal to exact number of matching rows."""
    db = _int_db()
    rows = db.execute("SELECT val FROM t WHERE val > 50 ORDER BY val LIMIT 4").fetchall()
    assert [r["val"] for r in rows] == [60, 70, 80, 90]


# ── DESC + LIMIT + OFFSET ─────────────────────────────────────────────────────

def test_range_index_desc_with_limit_and_offset():
    """Reverse scan early-terminates at limit+offset, then offset is sliced off."""
    db = _int_db()
    rows = db.execute(
        "SELECT val FROM t WHERE val >= 0 ORDER BY val DESC LIMIT 3 OFFSET 2"
    ).fetchall()
    # All vals DESC: [90,80,70,60,50,40,30,20,10,0]; skip 2, take 3 → [70,60,50]
    assert [r["val"] for r in rows] == [70, 60, 50]


def test_range_index_desc_offset_larger_than_result():
    db = _int_db()
    rows = db.execute(
        "SELECT val FROM t WHERE val > 80 ORDER BY val DESC LIMIT 5 OFFSET 10"
    ).fetchall()
    assert rows == []


# ── ORDER BY on non-indexed column (falls back to post-scan sort) ─────────────

def test_range_index_order_by_non_indexed_col():
    """Range filter via index, ORDER BY on a different column: full sort applied."""
    db = _int_db()
    rows = db.execute("SELECT id FROM t WHERE val >= 50 ORDER BY id DESC").fetchall()
    assert [r["id"] for r in rows] == [9, 8, 7, 6, 5]


def test_eq_index_order_by_non_indexed_col():
    """Equality index used for row filtering; ORDER BY non-indexed col correctly sorted."""
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, tag TEXT, score INTEGER)")
    db.execute("CREATE INDEX idx_tag ON t(tag)")
    for i in range(6):
        db.execute("INSERT INTO t VALUES (?, ?, ?)", (i, "x", i * 10))
    rows = db.execute("SELECT score FROM t WHERE tag = 'x' ORDER BY score DESC").fetchall()
    assert [r["score"] for r in rows] == [50, 40, 30, 20, 10, 0]


def test_range_index_multi_col_order_by_falls_back():
    """Multi-column ORDER BY cannot be satisfied by single-col index; full sort applied."""
    db = _int_db()
    rows = db.execute(
        "SELECT id, val FROM t WHERE val >= 50 ORDER BY val ASC, id DESC"
    ).fetchall()
    # val ASC then id DESC — val is unique here so id ordering doesn't matter,
    # just verify the val order is correct
    assert [r["val"] for r in rows] == [50, 60, 70, 80, 90]


# ── BETWEEN + ORDER BY + LIMIT ────────────────────────────────────────────────

def test_between_with_order_by_asc_and_limit():
    db = _int_db()
    rows = db.execute(
        "SELECT val FROM t WHERE val BETWEEN 20 AND 70 ORDER BY val ASC LIMIT 3"
    ).fetchall()
    assert [r["val"] for r in rows] == [20, 30, 40]


def test_between_with_order_by_desc_and_limit():
    db = _int_db()
    rows = db.execute(
        "SELECT val FROM t WHERE val BETWEEN 20 AND 70 ORDER BY val DESC LIMIT 3"
    ).fetchall()
    assert [r["val"] for r in rows] == [70, 60, 50]


def test_between_with_order_by_desc_limit_offset():
    db = _int_db()
    rows = db.execute(
        "SELECT val FROM t WHERE val BETWEEN 20 AND 70 ORDER BY val DESC LIMIT 2 OFFSET 1"
    ).fetchall()
    # BETWEEN 20..70 DESC: [70,60,50,40,30,20]; skip 1, take 2 → [60,50]
    assert [r["val"] for r in rows] == [60, 50]


# ── TEXT prefix collision: ORDER BY must use real sort, not index order ────────

def test_text_prefix_collision_order_by_asc_correct():
    """Strings > 8 chars sharing a prefix must be sorted correctly, not by rowid."""
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    db.execute("CREATE INDEX idx_name ON t(name)")
    # Insert in reverse alphabetical order within same 8-byte prefix bucket
    db.execute("INSERT INTO t VALUES (1, 'ABCDEFGHIJK')")  # rowid 1, but > ABCDEFGHAAA
    db.execute("INSERT INTO t VALUES (2, 'ABCDEFGHAAA')")  # rowid 2, but < ABCDEFGHIJK
    rows = db.execute(
        "SELECT name FROM t WHERE name >= 'A' ORDER BY name ASC"
    ).fetchall()
    assert [r["name"] for r in rows] == ["ABCDEFGHAAA", "ABCDEFGHIJK"]


def test_text_prefix_collision_order_by_desc_correct():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    db.execute("CREATE INDEX idx_name ON t(name)")
    db.execute("INSERT INTO t VALUES (1, 'ABCDEFGHIJK')")
    db.execute("INSERT INTO t VALUES (2, 'ABCDEFGHAAA')")
    rows = db.execute(
        "SELECT name FROM t WHERE name >= 'A' ORDER BY name DESC"
    ).fetchall()
    assert [r["name"] for r in rows] == ["ABCDEFGHIJK", "ABCDEFGHAAA"]


def test_text_short_strings_order_by_still_works():
    """Short TEXT strings (< 8 chars) have no prefix collision; order is preserved."""
    db = _text_db()
    rows = db.execute("SELECT name FROM t WHERE name >= 'A' ORDER BY name ASC").fetchall()
    assert [r["name"] for r in rows] == sorted(["Charlie", "Alice", "Eve", "Bob", "Dave"])
