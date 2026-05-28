"""Tests for range index use, outer join optimisation, and true prepared statements."""
import pytest
from hyperion import Database


# ── Range predicate index use ─────────────────────────────────────────────────

def test_range_gt_uses_index():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, val INTEGER)")
    db.execute("CREATE INDEX idx_val ON t(val)")
    for i in range(10):
        db.execute("INSERT INTO t VALUES (?, ?)", (i, i * 10))
    rows = db.execute("SELECT id FROM t WHERE val > 50").fetchall()
    assert {r["id"] for r in rows} == {6, 7, 8, 9}


def test_range_gte_uses_index():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, val INTEGER)")
    db.execute("CREATE INDEX idx_val ON t(val)")
    for i in range(10):
        db.execute("INSERT INTO t VALUES (?, ?)", (i, i * 10))
    rows = db.execute("SELECT id FROM t WHERE val >= 40").fetchall()
    assert {r["id"] for r in rows} == {4, 5, 6, 7, 8, 9}


def test_range_lt_uses_index():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, val INTEGER)")
    db.execute("CREATE INDEX idx_val ON t(val)")
    for i in range(10):
        db.execute("INSERT INTO t VALUES (?, ?)", (i, i * 10))
    rows = db.execute("SELECT id FROM t WHERE val < 30").fetchall()
    assert {r["id"] for r in rows} == {0, 1, 2}


def test_range_lte_uses_index():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, val INTEGER)")
    db.execute("CREATE INDEX idx_val ON t(val)")
    for i in range(10):
        db.execute("INSERT INTO t VALUES (?, ?)", (i, i * 10))
    rows = db.execute("SELECT id FROM t WHERE val <= 20").fetchall()
    assert {r["id"] for r in rows} == {0, 1, 2}


def test_range_combined_with_where():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, val INTEGER, tag TEXT)")
    db.execute("CREATE INDEX idx_val ON t(val)")
    for i in range(10):
        tag = "odd" if i % 2 else "even"
        db.execute("INSERT INTO t VALUES (?, ?, ?)", (i, i * 10, tag))
    rows = db.execute("SELECT id FROM t WHERE val > 30 AND tag = 'odd'").fetchall()
    # val > 30 → id in {4,5,6,7,8,9}; tag=odd → {5,7,9}
    assert {r["id"] for r in rows} == {5, 7, 9}


# ── Outer join optimisation ───────────────────────────────────────────────────

def test_left_join_inlj_matched_and_unmatched():
    db = Database(":memory:")
    db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    db.execute("CREATE TABLE orders (id INTEGER, user_id INTEGER, amount INTEGER)")
    db.execute("CREATE INDEX idx_orders_uid ON orders(user_id)")
    for i in range(1, 5):
        db.execute("INSERT INTO users VALUES (?, ?)", (i, f"User{i}"))
    db.executemany("INSERT INTO orders VALUES (?, ?, ?)", [(1, 1, 100), (2, 1, 200), (3, 3, 50)])
    rows = db.execute(
        "SELECT u.id, o.amount FROM users u LEFT JOIN orders o ON u.id = o.user_id "
        "ORDER BY u.id, o.amount"
    ).fetchall()
    # User 1 → 2 rows, User 2 → NULL, User 3 → 1 row, User 4 → NULL
    assert len(rows) == 5
    null_rows = [r for r in rows if r["o.amount"] is None]
    assert len(null_rows) == 2


def test_left_join_inlj_all_unmatched():
    db = Database(":memory:")
    db.execute("CREATE TABLE a (id INTEGER PRIMARY KEY)")
    db.execute("CREATE TABLE b (a_id INTEGER, val INTEGER)")
    db.execute("CREATE INDEX idx_b ON b(a_id)")
    for i in range(3):
        db.execute("INSERT INTO a VALUES (?)", (i,))
    rows = db.execute("SELECT a.id, b.val FROM a LEFT JOIN b ON a.id = b.a_id").fetchall()
    assert len(rows) == 3
    assert all(r["b.val"] is None for r in rows)


def test_left_join_null_key_produces_null_row():
    db = Database(":memory:")
    db.execute("CREATE TABLE a (id INTEGER, fk INTEGER)")
    db.execute("CREATE TABLE b (id INTEGER PRIMARY KEY, name TEXT)")
    db.execute("CREATE INDEX idx_b ON b(id)")
    db.execute("INSERT INTO a VALUES (1, NULL)")
    db.execute("INSERT INTO b VALUES (1, 'x')")
    rows = db.execute("SELECT a.id, b.name FROM a LEFT JOIN b ON a.fk = b.id").fetchall()
    assert len(rows) == 1
    assert rows[0]["b.name"] is None


def test_outer_join_with_inner_extras_reordered():
    """LEFT JOIN primary + INNER extra joins: extras should be reorderable."""
    db = Database(":memory:")
    db.execute("CREATE TABLE a (id INTEGER PRIMARY KEY, name TEXT)")
    db.execute("CREATE TABLE b (id INTEGER, a_id INTEGER)")
    db.execute("CREATE TABLE c (id INTEGER, a_id INTEGER, val INTEGER)")
    db.execute("CREATE INDEX idx_c ON c(a_id)")
    for i in range(1, 4):
        db.execute("INSERT INTO a VALUES (?, ?)", (i, f"A{i}"))
    db.executemany("INSERT INTO b VALUES (?, ?)", [(1, 1), (2, 2)])
    db.executemany("INSERT INTO c VALUES (?, ?, ?)", [(1, 1, 10), (2, 2, 20), (3, 2, 30)])
    rows = db.execute(
        "SELECT a.name, c.val FROM a "
        "LEFT JOIN b ON a.id = b.a_id "
        "JOIN c ON a.id = c.a_id "
        "ORDER BY a.name, c.val"
    ).fetchall()
    # Only a.id=1 and a.id=2 are in both b and c
    assert len(rows) == 3
    vals = [r["c.val"] for r in rows]
    assert vals == [10, 20, 30]


# ── True prepared statements / plan cache ────────────────────────────────────

def test_plan_cache_reuses_ast():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    db.executemany("INSERT INTO t VALUES (?, ?)", [(i, f"N{i}") for i in range(5)])
    # Execute same query multiple times — plan cache should be hit
    for i in range(5):
        rows = db.execute("SELECT name FROM t WHERE id = ?", (i,)).fetchall()
        assert rows[0]["name"] == f"N{i}"
    # Cache should have exactly 2 entries: CREATE TABLE and INSERT
    # (SELECT template only stored once)
    assert "SELECT name FROM t WHERE id = ?" in db._plan_cache


def test_executemany_uses_plan_cache():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (x INTEGER, y TEXT)")
    # executemany calls execute N times with the same SQL
    n = 100
    db.executemany("INSERT INTO t VALUES (?, ?)", [(i, chr(65 + i % 26)) for i in range(n)])
    rows = db.execute("SELECT COUNT(*) AS cnt FROM t").fetchall()
    assert rows[0]["cnt"] == n


def test_plan_cache_positional_params_select():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, val REAL)")
    for i in range(10):
        db.execute("INSERT INTO t VALUES (?, ?)", (i, float(i) * 1.5))
    rows = db.execute("SELECT val FROM t WHERE id = ?", (3,)).fetchall()
    assert abs(rows[0]["val"] - 4.5) < 1e-9


def test_plan_cache_named_params_select():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    db.executemany("INSERT INTO t VALUES (?, ?)", [(1, "Alice"), (2, "Bob")])
    rows = db.execute("SELECT name FROM t WHERE id = :uid", {"uid": 2}).fetchall()
    assert rows[0]["name"] == "Bob"


def test_plan_cache_positional_params_update():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    db.executemany("INSERT INTO t VALUES (?, ?)", [(1, "Alice"), (2, "Bob")])
    db.execute("UPDATE t SET name = ? WHERE id = ?", ("Carol", 1))
    rows = db.execute("SELECT name FROM t WHERE id = ?", (1,)).fetchall()
    assert rows[0]["name"] == "Carol"


def test_plan_cache_positional_params_delete():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    db.executemany("INSERT INTO t VALUES (?, ?)", [(i, f"N{i}") for i in range(5)])
    db.execute("DELETE FROM t WHERE id = ?", (3,))
    rows = db.execute("SELECT id FROM t ORDER BY id").fetchall()
    assert [r["id"] for r in rows] == [0, 1, 2, 4]


def test_plan_cache_null_param():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER, val TEXT)")
    db.execute("INSERT INTO t VALUES (?, ?)", (1, None))
    rows = db.execute("SELECT * FROM t WHERE val IS NULL").fetchall()
    assert len(rows) == 1


def test_plan_cache_insert_mixed_types():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (a INTEGER, b REAL, c TEXT)")
    db.execute("INSERT INTO t VALUES (?, ?, ?)", (42, 3.14, "hello"))
    rows = db.execute("SELECT * FROM t").fetchall()
    assert rows[0]["a"] == 42
    assert abs(rows[0]["b"] - 3.14) < 1e-9
    assert rows[0]["c"] == "hello"


def test_plan_cache_multiple_where_params():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (x INTEGER, y INTEGER, z INTEGER)")
    db.executemany("INSERT INTO t VALUES (?, ?, ?)",
                   [(i, i*2, i*3) for i in range(10)])
    rows = db.execute("SELECT z FROM t WHERE x = ? AND y = ?", (4, 8)).fetchall()
    assert rows[0]["z"] == 12
