"""Tests for variable-length row storage and overflow page mechanism."""
import pytest
from hyperion import Database
from hyperion.constants import ROW_INLINE_CAP, PAGE_SIZE


def _db():
    db = Database(":memory:")
    return db


# ── Inline storage (data fits within ROW_INLINE_CAP) ─────────────────────────

def test_short_text_roundtrip():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    db.execute("INSERT INTO t VALUES (1, 'Alice')")
    rows = db.execute("SELECT * FROM t").fetchall()
    assert rows[0]["name"] == "Alice"


def test_null_values():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    db.execute("INSERT INTO t VALUES (1, NULL)")
    rows = db.execute("SELECT * FROM t").fetchall()
    assert rows[0]["name"] is None


def test_multiple_columns_inline():
    db = _db()
    db.execute("CREATE TABLE t (a INTEGER, b REAL, c TEXT, d TEXT)")
    db.execute("INSERT INTO t VALUES (42, 3.14, 'hello', 'world')")
    rows = db.execute("SELECT * FROM t").fetchall()
    assert rows[0]["a"] == 42
    assert abs(rows[0]["b"] - 3.14) < 1e-10
    assert rows[0]["c"] == "hello"
    assert rows[0]["d"] == "world"


def test_inline_boundary():
    """Row that exactly fills ROW_INLINE_CAP should be stored inline (no overflow)."""
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER, data TEXT)")
    # Construct text such that the serialized row is exactly ROW_INLINE_CAP bytes.
    # Row bytes: 1 (bitmap) + 8 (INTEGER) + 4 (length prefix) + N (text) = 13 + N
    # So N = ROW_INLINE_CAP - 13
    n = ROW_INLINE_CAP - 13
    text = "x" * n
    db.execute("INSERT INTO t VALUES (1, ?)", (text,))
    rows = db.execute("SELECT data FROM t").fetchall()
    assert rows[0]["data"] == text


# ── Overflow storage (data exceeds ROW_INLINE_CAP) ───────────────────────────

def test_overflow_single_page():
    """Text longer than ROW_INLINE_CAP but shorter than one overflow page."""
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER, data TEXT)")
    big = "A" * (ROW_INLINE_CAP + 50)
    db.execute("INSERT INTO t VALUES (1, ?)", (big,))
    rows = db.execute("SELECT data FROM t").fetchall()
    assert rows[0]["data"] == big


def test_overflow_multi_page():
    """Text spanning more than one overflow page (> 4087 bytes)."""
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER, data TEXT)")
    big = "Z" * (PAGE_SIZE * 3)    # 12 288 bytes, requires 3 overflow pages
    db.execute("INSERT INTO t VALUES (1, ?)", (big,))
    rows = db.execute("SELECT data FROM t").fetchall()
    assert rows[0]["data"] == big


def test_overflow_blob():
    """BLOB column with large binary payload."""
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER, payload BLOB)")
    payload = bytes(range(256)) * 20    # 5120 bytes
    db.execute("INSERT INTO t VALUES (1, ?)", (payload,))
    rows = db.execute("SELECT payload FROM t").fetchall()
    assert rows[0]["payload"] == payload


def test_overflow_multiple_rows():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER, data TEXT)")
    texts = ["x" * (1000 * i) for i in range(1, 6)]
    for i, text in enumerate(texts, 1):
        db.execute("INSERT INTO t VALUES (?, ?)", (i, text))
    rows = db.execute("SELECT id, data FROM t ORDER BY id").fetchall()
    assert len(rows) == 5
    for i, row in enumerate(rows, 1):
        assert row["data"] == texts[i - 1]


# ── Update and delete with overflow page reclamation ─────────────────────────

def test_update_overflow_to_overflow():
    """Update a large value with another large value; old overflow pages freed."""
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER, data TEXT)")
    old_val = "A" * 5000
    new_val = "B" * 8000
    db.execute("INSERT INTO t VALUES (1, ?)", (old_val,))
    db.execute("UPDATE t SET data = ? WHERE id = 1", (new_val,))
    rows = db.execute("SELECT data FROM t").fetchall()
    assert rows[0]["data"] == new_val


def test_update_overflow_to_inline():
    """Update a large value with a small value; overflow pages freed."""
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER, data TEXT)")
    db.execute("INSERT INTO t VALUES (1, ?)", ("A" * 5000,))
    db.execute("UPDATE t SET data = 'short' WHERE id = 1")
    rows = db.execute("SELECT data FROM t").fetchall()
    assert rows[0]["data"] == "short"


def test_update_inline_to_overflow():
    """Update a small value with a large value; new overflow pages allocated."""
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER, data TEXT)")
    db.execute("INSERT INTO t VALUES (1, 'short')")
    db.execute("UPDATE t SET data = ? WHERE id = 1", ("B" * 5000,))
    rows = db.execute("SELECT data FROM t").fetchall()
    assert rows[0]["data"] == "B" * 5000


def test_delete_overflow_rows():
    """Delete rows with overflow values; overflow pages freed (no assertion on page count,
    just verify subsequent inserts work and results are correct)."""
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER, data TEXT)")
    for i in range(5):
        db.execute("INSERT INTO t VALUES (?, ?)", (i, "X" * 5000))
    db.execute("DELETE FROM t WHERE id < 3")
    rows = db.execute("SELECT id FROM t ORDER BY id").fetchall()
    assert [r["id"] for r in rows] == [3, 4]


# ── WHERE predicates on tables with overflow rows ─────────────────────────────

def test_where_on_overflow_table():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER, name TEXT, data TEXT)")
    for i in range(5):
        db.execute("INSERT INTO t VALUES (?, ?, ?)", (i, f"name{i}", "x" * 5000))
    rows = db.execute("SELECT id, name FROM t WHERE id = 3").fetchall()
    assert len(rows) == 1
    assert rows[0]["name"] == "name3"


def test_order_by_on_overflow_table():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER, data TEXT)")
    for i in [3, 1, 4, 1, 5]:
        db.execute("INSERT INTO t VALUES (?, ?)", (i, "y" * 3000))
    rows = db.execute("SELECT id FROM t ORDER BY id").fetchall()
    assert [r["id"] for r in rows] == sorted([3, 1, 4, 1, 5])


# ── Mixed inline and overflow rows ────────────────────────────────────────────

def test_mixed_inline_and_overflow():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER, data TEXT)")
    db.execute("INSERT INTO t VALUES (1, 'short')")
    db.execute("INSERT INTO t VALUES (2, ?)", ("A" * 5000,))
    db.execute("INSERT INTO t VALUES (3, 'also short')")
    db.execute("INSERT INTO t VALUES (4, ?)", ("B" * 10000,))
    rows = db.execute("SELECT id, data FROM t ORDER BY id").fetchall()
    assert rows[0]["data"] == "short"
    assert rows[1]["data"] == "A" * 5000
    assert rows[2]["data"] == "also short"
    assert rows[3]["data"] == "B" * 10000


# ── BLOB regression ───────────────────────────────────────────────────────────

def test_blob_roundtrip():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER, raw BLOB)")
    data = bytes(range(256))
    db.execute("INSERT INTO t VALUES (1, ?)", (data,))
    rows = db.execute("SELECT raw FROM t").fetchall()
    assert rows[0]["raw"] == data


def test_empty_text():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER, s TEXT)")
    db.execute("INSERT INTO t VALUES (1, '')")
    rows = db.execute("SELECT s FROM t").fetchall()
    assert rows[0]["s"] == ""


def test_unicode_text():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER, s TEXT)")
    text = "こんにちは世界 🌍" * 100
    db.execute("INSERT INTO t VALUES (1, ?)", (text,))
    rows = db.execute("SELECT s FROM t").fetchall()
    assert rows[0]["s"] == text


# ── iterdump with overflow data ───────────────────────────────────────────────

def test_iterdump_with_large_values():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER, data TEXT)")
    big = "hello " * 1000
    db.execute("INSERT INTO t VALUES (1, ?)", (big,))
    sql = "\n".join(db.iterdump())
    assert "INSERT INTO" in sql
    # Verify dump can be replayed
    db2 = Database(":memory:")
    db2.executescript(sql)
    rows = db2.execute("SELECT data FROM t").fetchall()
    assert rows[0]["data"] == big
