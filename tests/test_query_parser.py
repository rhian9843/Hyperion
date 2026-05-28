"""Tests for Query/Parser TODO items: multi-condition JOIN ON, window frame bounds, named WINDOW."""
import pytest
from hyperion import Database


# ── Multi-condition JOIN ON ───────────────────────────────────────────────────

def test_join_multi_condition_on_basic():
    db = Database(":memory:")
    db.execute("CREATE TABLE a (x INTEGER, y INTEGER)")
    db.execute("CREATE TABLE b (x INTEGER, y INTEGER, val TEXT)")
    db.executemany("INSERT INTO a VALUES (?, ?)", [(1, 10), (2, 20), (3, 30)])
    db.executemany("INSERT INTO b VALUES (?, ?, ?)", [(1, 10, "hit"), (1, 99, "miss"), (3, 30, "hit2")])
    rows = db.execute("SELECT a.x, b.val FROM a JOIN b ON a.x = b.x AND a.y = b.y").fetchall()
    assert len(rows) == 2
    vals = {r["b.val"] for r in rows}
    assert vals == {"hit", "hit2"}


def test_join_multi_condition_three_predicates():
    db = Database(":memory:")
    db.execute("CREATE TABLE t1 (a INTEGER, b INTEGER, c INTEGER)")
    db.execute("CREATE TABLE t2 (a INTEGER, b INTEGER, c INTEGER, label TEXT)")
    db.executemany("INSERT INTO t1 VALUES (?, ?, ?)", [(1, 2, 3), (1, 2, 9)])
    db.executemany("INSERT INTO t2 VALUES (?, ?, ?, ?)", [(1, 2, 3, "match"), (1, 2, 9, "other")])
    rows = db.execute(
        "SELECT t2.label FROM t1 JOIN t2 ON t1.a = t2.a AND t1.b = t2.b AND t1.c = t2.c"
    ).fetchall()
    assert len(rows) == 2
    labels = {r["t2.label"] for r in rows}
    assert labels == {"match", "other"}


def test_join_single_condition_still_works():
    db = Database(":memory:")
    db.execute("CREATE TABLE p (id INTEGER, name TEXT)")
    db.execute("CREATE TABLE c (pid INTEGER, val TEXT)")
    db.executemany("INSERT INTO p VALUES (?, ?)", [(1, "Alice"), (2, "Bob")])
    db.executemany("INSERT INTO c VALUES (?, ?)", [(1, "a"), (1, "b"), (2, "c")])
    rows = db.execute("SELECT p.name, c.val FROM p JOIN c ON p.id = c.pid ORDER BY p.name, c.val").fetchall()
    assert len(rows) == 3


def test_join_multi_condition_left_outer():
    db = Database(":memory:")
    db.execute("CREATE TABLE a (x INTEGER, y INTEGER)")
    db.execute("CREATE TABLE b (x INTEGER, y INTEGER, v TEXT)")
    db.executemany("INSERT INTO a VALUES (?, ?)", [(1, 10), (2, 20)])
    db.executemany("INSERT INTO b VALUES (?, ?, ?)", [(1, 10, "yes")])
    rows = db.execute(
        "SELECT a.x, b.v FROM a LEFT JOIN b ON a.x = b.x AND a.y = b.y"
    ).fetchall()
    assert len(rows) == 2
    v_vals = {r.get("b.v") for r in rows}
    assert "yes" in v_vals
    assert None in v_vals  # unmatched left row


# ── Window function frame bounds ──────────────────────────────────────────────

def test_window_rows_between_unbounded_preceding_current_row():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, val INTEGER)")
    db.executemany("INSERT INTO t VALUES (?, ?)", [(1, 10), (2, 20), (3, 30), (4, 40)])
    rows = db.execute(
        "SELECT id, SUM(val) OVER (ORDER BY id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running_sum "
        "FROM t ORDER BY id"
    ).fetchall()
    running = [r["running_sum"] for r in rows]
    assert running == [10, 30, 60, 100]


def test_window_rows_between_n_preceding_current_row():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, val INTEGER)")
    db.executemany("INSERT INTO t VALUES (?, ?)", [(1, 10), (2, 20), (3, 30), (4, 40)])
    rows = db.execute(
        "SELECT id, SUM(val) OVER (ORDER BY id ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) AS roll "
        "FROM t ORDER BY id"
    ).fetchall()
    roll = [r["roll"] for r in rows]
    # row1: only row1=10; row2: row1+row2=30; row3: row2+row3=50; row4: row3+row4=70
    assert roll == [10, 30, 50, 70]


def test_window_rows_unbounded_preceding_unbounded_following():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, val INTEGER)")
    db.executemany("INSERT INTO t VALUES (?, ?)", [(1, 10), (2, 20), (3, 30)])
    rows = db.execute(
        "SELECT id, SUM(val) OVER (ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS total "
        "FROM t ORDER BY id"
    ).fetchall()
    totals = [r["total"] for r in rows]
    assert totals == [60, 60, 60]


def test_window_first_value_with_frame():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, val INTEGER)")
    db.executemany("INSERT INTO t VALUES (?, ?)", [(1, 10), (2, 20), (3, 30), (4, 40)])
    rows = db.execute(
        "SELECT id, FIRST_VALUE(val) OVER (ORDER BY id ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) AS fv "
        "FROM t ORDER BY id"
    ).fetchall()
    fv = [r["fv"] for r in rows]
    # row1: frame=[row1] → 10; row2: frame=[row1,row2] → 10; row3: frame=[row2,row3] → 20; row4: [row3,row4]→30
    assert fv == [10, 10, 20, 30]


def test_window_last_value_with_frame():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, val INTEGER)")
    db.executemany("INSERT INTO t VALUES (?, ?)", [(1, 10), (2, 20), (3, 30)])
    rows = db.execute(
        "SELECT id, LAST_VALUE(val) OVER (ORDER BY id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS lv "
        "FROM t ORDER BY id"
    ).fetchall()
    lv = [r["lv"] for r in rows]
    assert lv == [10, 20, 30]


# ── Named WINDOW clause ───────────────────────────────────────────────────────

def test_named_window_basic():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (dept TEXT, sal INTEGER)")
    db.executemany("INSERT INTO t VALUES (?, ?)", [
        ("eng", 100), ("eng", 200), ("mkt", 50), ("mkt", 150)
    ])
    rows = db.execute(
        "SELECT dept, sal, ROW_NUMBER() OVER w AS rn "
        "FROM t "
        "WINDOW w AS (PARTITION BY dept ORDER BY sal) "
        "ORDER BY dept, sal"
    ).fetchall()
    assert len(rows) == 4
    rns = {(r["dept"], r["sal"]): r["rn"] for r in rows}
    assert rns[("eng", 100)] == 1
    assert rns[("eng", 200)] == 2
    assert rns[("mkt", 50)]  == 1
    assert rns[("mkt", 150)] == 2


def test_named_window_reused_across_multiple_cols():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (g INTEGER, v INTEGER)")
    db.executemany("INSERT INTO t VALUES (?, ?)", [(1, 10), (1, 20), (1, 30)])
    rows = db.execute(
        "SELECT v, "
        "ROW_NUMBER() OVER w AS rn, "
        "SUM(v) OVER w AS total "
        "FROM t "
        "WINDOW w AS (PARTITION BY g ORDER BY v ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) "
        "ORDER BY v"
    ).fetchall()
    assert [r["rn"] for r in rows] == [1, 2, 3]
    assert all(r["total"] == 60 for r in rows)


def test_named_window_no_partition():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, v INTEGER)")
    db.executemany("INSERT INTO t VALUES (?, ?)", [(1, 5), (2, 3), (3, 8)])
    rows = db.execute(
        "SELECT id, RANK() OVER w AS rnk "
        "FROM t "
        "WINDOW w AS (ORDER BY v) "
        "ORDER BY v"
    ).fetchall()
    rnks = [r["rnk"] for r in rows]
    assert rnks == [1, 2, 3]
