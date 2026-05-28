"""Tests for streaming / iterator query results."""
import itertools
import pytest
from hyperion import Database


def _db():
    db = Database(":memory:")
    return db


# ── fetchone ─────────────────────────────────────────────────────────────────

def test_fetchone_basic():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    db.execute("INSERT INTO t VALUES (1, 'Alice')")
    db.execute("INSERT INTO t VALUES (2, 'Bob')")
    cur = db.execute("SELECT * FROM t")
    r1 = cur.fetchone()
    r2 = cur.fetchone()
    r3 = cur.fetchone()
    assert r1["id"] == 1
    assert r2["id"] == 2
    assert r3 is None


def test_fetchone_empty():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER)")
    cur = db.execute("SELECT * FROM t")
    assert cur.fetchone() is None


def test_fetchone_after_exhaustion():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER)")
    db.execute("INSERT INTO t VALUES (1)")
    cur = db.execute("SELECT * FROM t")
    cur.fetchone()
    assert cur.fetchone() is None
    assert cur.fetchone() is None   # idempotent


# ── fetchmany ────────────────────────────────────────────────────────────────

def test_fetchmany_basic():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER)")
    for i in range(5):
        db.execute(f"INSERT INTO t VALUES ({i})")
    cur = db.execute("SELECT * FROM t")
    batch = cur.fetchmany(3)
    assert len(batch) == 3
    assert [r["id"] for r in batch] == [0, 1, 2]
    rest = cur.fetchmany(10)
    assert [r["id"] for r in rest] == [3, 4]
    assert cur.fetchmany(5) == []


def test_fetchmany_size_1():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER)")
    db.execute("INSERT INTO t VALUES (42)")
    cur = db.execute("SELECT id FROM t")
    batch = cur.fetchmany()   # default size=1
    assert batch == [{"id": 42}]
    assert cur.fetchmany() == []


def test_fetchmany_empty():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER)")
    cur = db.execute("SELECT * FROM t")
    assert cur.fetchmany(5) == []


# ── fetchall ─────────────────────────────────────────────────────────────────

def test_fetchall_returns_all():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER)")
    for i in range(10):
        db.execute(f"INSERT INTO t VALUES ({i})")
    cur = db.execute("SELECT id FROM t")
    rows = cur.fetchall()
    assert [r["id"] for r in rows] == list(range(10))


def test_fetchall_after_partial_fetch():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER)")
    for i in range(5):
        db.execute(f"INSERT INTO t VALUES ({i})")
    cur = db.execute("SELECT id FROM t")
    cur.fetchone()
    cur.fetchone()
    remaining = cur.fetchall()
    assert [r["id"] for r in remaining] == [2, 3, 4]


def test_fetchall_empty():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER)")
    assert db.execute("SELECT * FROM t").fetchall() == []


# ── cursor as iterator ────────────────────────────────────────────────────────

def test_for_loop_iteration():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER)")
    for i in range(4):
        db.execute(f"INSERT INTO t VALUES ({i})")
    ids = [row["id"] for row in db.execute("SELECT id FROM t")]
    assert ids == [0, 1, 2, 3]


def test_iter_protocol():
    db = _db()
    db.execute("CREATE TABLE t (x INTEGER)")
    db.execute("INSERT INTO t VALUES (99)")
    cur = db.execute("SELECT x FROM t")
    assert iter(cur) is cur
    row = next(cur)
    assert row["x"] == 99
    with pytest.raises(StopIteration):
        next(cur)


# ── description and rowcount ─────────────────────────────────────────────────

def test_description_set_before_fetch():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    db.execute("INSERT INTO t VALUES (1, 'Alice')")
    cur = db.execute("SELECT id, name FROM t")
    # description must be available before any fetch
    assert cur.description is not None
    assert [d[0] for d in cur.description] == ["id", "name"]


def test_description_empty_result():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    # No rows match; description must still reflect the column names
    cur = db.execute("SELECT id, name FROM t WHERE id = -999")
    assert cur.description is not None
    assert [d[0] for d in cur.description] == ["id", "name"]


def test_rowcount_is_minus1_for_select():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER)")
    db.execute("INSERT INTO t VALUES (1)")
    cur = db.execute("SELECT * FROM t")
    assert cur.rowcount == -1


# ── streaming path gate conditions ───────────────────────────────────────────

def test_streaming_with_where():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER, val TEXT)")
    for i in range(10):
        db.execute(f"INSERT INTO t VALUES ({i}, 'v{i}')")
    rows = db.execute("SELECT id FROM t WHERE id > 6").fetchall()
    assert [r["id"] for r in rows] == [7, 8, 9]


def test_streaming_with_limit():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER)")
    for i in range(20):
        db.execute(f"INSERT INTO t VALUES ({i})")
    rows = db.execute("SELECT id FROM t LIMIT 5").fetchall()
    assert len(rows) == 5
    assert [r["id"] for r in rows] == [0, 1, 2, 3, 4]


def test_streaming_with_offset():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER)")
    for i in range(10):
        db.execute(f"INSERT INTO t VALUES ({i})")
    rows = db.execute("SELECT id FROM t LIMIT 3 OFFSET 4").fetchall()
    assert [r["id"] for r in rows] == [4, 5, 6]


def test_streaming_with_column_projection():
    db = _db()
    db.execute("CREATE TABLE t (a INTEGER, b TEXT, c REAL)")
    db.execute("INSERT INTO t VALUES (1, 'hi', 3.14)")
    row = db.execute("SELECT a, c FROM t").fetchone()
    assert set(row.keys()) == {"a", "c"}


def test_streaming_with_alias():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER)")
    db.execute("INSERT INTO t VALUES (7)")
    row = db.execute("SELECT id AS uid FROM t").fetchone()
    assert row == {"uid": 7}


# ── complex queries still work (fall-through to materialised path) ────────────

def test_order_by_still_works():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER)")
    for i in [3, 1, 4, 1, 5]:
        db.execute(f"INSERT INTO t VALUES ({i})")
    ids = [r["id"] for r in db.execute("SELECT id FROM t ORDER BY id").fetchall()]
    assert ids == sorted([3, 1, 4, 1, 5])


def test_group_by_still_works():
    db = _db()
    db.execute("CREATE TABLE t (cat TEXT, val INTEGER)")
    db.execute("INSERT INTO t VALUES ('a', 1)")
    db.execute("INSERT INTO t VALUES ('a', 2)")
    db.execute("INSERT INTO t VALUES ('b', 10)")
    rows = db.execute("SELECT cat, SUM(val) AS s FROM t GROUP BY cat ORDER BY cat").fetchall()
    assert rows[0] == {"cat": "a", "s": 3}
    assert rows[1] == {"cat": "b", "s": 10}


def test_join_still_works():
    db = _db()
    db.execute("CREATE TABLE a (id INTEGER, x TEXT)")
    db.execute("CREATE TABLE b (id INTEGER, y TEXT)")
    db.execute("INSERT INTO a VALUES (1, 'foo')")
    db.execute("INSERT INTO b VALUES (1, 'bar')")
    row = db.execute("SELECT a.x, b.y FROM a JOIN b ON a.id = b.id").fetchone()
    assert row["a.x"] == "foo"
    assert row["b.y"] == "bar"


# ── close ─────────────────────────────────────────────────────────────────────

def test_close_stops_iteration():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER)")
    for i in range(5):
        db.execute(f"INSERT INTO t VALUES ({i})")
    cur = db.execute("SELECT id FROM t")
    cur.fetchone()
    cur.close()
    assert cur.fetchone() is None
    assert cur.fetchall() == []


# ── row_factory ───────────────────────────────────────────────────────────────

def test_row_factory_applied_lazily():
    db = _db()
    db.row_factory = lambda cur, row: tuple(row.values())
    db.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    db.execute("INSERT INTO t VALUES (1, 'Alice')")
    db.execute("INSERT INTO t VALUES (2, 'Bob')")
    cur = db.execute("SELECT id, name FROM t")
    assert cur.fetchone() == (1, "Alice")
    assert cur.fetchone() == (2, "Bob")


# ── large result set via generator (memory regression check) ─────────────────

def test_large_table_fetchone_chain():
    """fetchone() on a 10 000-row table should not require materialising all rows."""
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER)")
    # Insert in batches to keep the test fast
    for batch_start in range(0, 10_000, 100):
        vals = ", ".join(f"({i})" for i in range(batch_start, batch_start + 100))
        db.execute(f"INSERT INTO t VALUES {vals}")
    cur = db.execute("SELECT id FROM t")
    first = cur.fetchone()
    assert first is not None
    assert first["id"] == 0
    # Drain the rest to confirm the generator works end-to-end
    count = 1 + sum(1 for _ in cur)
    assert count == 10_000
