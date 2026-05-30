"""Tests for sort-order-preserving composite index keys and prefix range scans.

Before the fix, _encode_composite_key used FNV-1a hashing for multi-column
indexes, destroying sort order.  Range predicates and prefix equality + range
on composite indexes silently fell back to full table scans with no warning.

After the fix:
- _encode_composite_key returns per-column keys (full int64 precision, order preserved).
- _make_index_key is polymorphic and packs N columns into (N+1)*8 byte keys.
- Query engine detects prefix equality + range patterns and uses the index.
"""
import pytest
from hyperion import Database
from hyperion.errors import UniqueConstraintError


def _exec(db, sql):
    cur = db.cursor()
    cur.execute(sql)
    return cur


def _rows(db, sql):
    cur = _exec(db, sql)
    return list(cur)


# ── Correctness: range queries on composite indexes ────────────────────────────

class TestCompositeIndexRange:
    def test_prefix_eq_range_gt(self):
        db = Database(":memory:")
        _exec(db, "CREATE TABLE t (tenant INTEGER, val INTEGER)")
        _exec(db, "CREATE INDEX idx ON t (tenant, val)")
        for t in range(3):
            for v in range(10):
                _exec(db, f"INSERT INTO t VALUES ({t}, {v})")
        rows = _rows(db, "SELECT val FROM t WHERE tenant = 1 AND val > 5 ORDER BY val ASC")
        assert [r["val"] for r in rows] == [6, 7, 8, 9]

    def test_prefix_eq_range_gte(self):
        db = Database(":memory:")
        _exec(db, "CREATE TABLE t (tenant INTEGER, val INTEGER)")
        _exec(db, "CREATE INDEX idx ON t (tenant, val)")
        for t in range(3):
            for v in range(10):
                _exec(db, f"INSERT INTO t VALUES ({t}, {v})")
        rows = _rows(db, "SELECT val FROM t WHERE tenant = 0 AND val >= 7 ORDER BY val ASC")
        assert [r["val"] for r in rows] == [7, 8, 9]

    def test_prefix_eq_range_lt(self):
        db = Database(":memory:")
        _exec(db, "CREATE TABLE t (tenant INTEGER, val INTEGER)")
        _exec(db, "CREATE INDEX idx ON t (tenant, val)")
        for t in range(3):
            for v in range(10):
                _exec(db, f"INSERT INTO t VALUES ({t}, {v})")
        rows = _rows(db, "SELECT val FROM t WHERE tenant = 2 AND val < 3 ORDER BY val ASC")
        assert [r["val"] for r in rows] == [0, 1, 2]

    def test_prefix_eq_range_lte(self):
        db = Database(":memory:")
        _exec(db, "CREATE TABLE t (tenant INTEGER, val INTEGER)")
        _exec(db, "CREATE INDEX idx ON t (tenant, val)")
        for t in range(3):
            for v in range(10):
                _exec(db, f"INSERT INTO t VALUES ({t}, {v})")
        rows = _rows(db, "SELECT val FROM t WHERE tenant = 1 AND val <= 4 ORDER BY val ASC")
        assert [r["val"] for r in rows] == [0, 1, 2, 3, 4]

    def test_prefix_isolates_tenant(self):
        """Results must only contain rows for the specified tenant."""
        db = Database(":memory:")
        _exec(db, "CREATE TABLE t (tenant INTEGER, val INTEGER)")
        _exec(db, "CREATE INDEX idx ON t (tenant, val)")
        for t in range(5):
            for v in range(20):
                _exec(db, f"INSERT INTO t VALUES ({t}, {v})")
        rows = _rows(db, "SELECT tenant, val FROM t WHERE tenant = 3 AND val > 15")
        assert all(r["tenant"] == 3 for r in rows)
        assert sorted(r["val"] for r in rows) == [16, 17, 18, 19]

    def test_text_range_on_composite_index(self):
        """Prefix equality + TEXT range on composite index."""
        db = Database(":memory:")
        _exec(db, "CREATE TABLE t (ns TEXT, name TEXT)")
        _exec(db, "CREATE INDEX idx ON t (ns, name)")
        for ns in ("a", "b", "c"):
            for name in ("alpha", "beta", "gamma", "delta"):
                _exec(db, f"INSERT INTO t VALUES ('{ns}', '{name}')")
        rows = _rows(db, "SELECT name FROM t WHERE ns = 'b' AND name >= 'delta' ORDER BY name ASC")
        assert [r["name"] for r in rows] == ["delta", "gamma"]

    def test_composite_equality_still_works(self):
        """Pure equality on all composite columns must still work after encoding change."""
        db = Database(":memory:")
        _exec(db, "CREATE TABLE t (a INTEGER, b INTEGER, v TEXT)")
        _exec(db, "CREATE INDEX idx ON t (a, b)")
        for a in range(5):
            for b in range(5):
                _exec(db, f"INSERT INTO t VALUES ({a}, {b}, 'v{a}{b}')")
        rows = _rows(db, "SELECT v FROM t WHERE a = 2 AND b = 3")
        assert len(rows) == 1
        assert rows[0]["v"] == "v23"

    def test_pk_composite_unique_enforced(self):
        """Composite PRIMARY KEY must still reject duplicates after encoding change."""
        db = Database(":memory:")
        _exec(db, "CREATE TABLE orders (order_id INTEGER, item_id INTEGER, qty INTEGER, PRIMARY KEY (order_id, item_id))")
        _exec(db, "INSERT INTO orders VALUES (1, 10, 5)")
        _exec(db, "INSERT INTO orders VALUES (1, 20, 3)")  # different item_id → OK
        _exec(db, "INSERT INTO orders VALUES (2, 10, 7)")  # different order_id → OK
        with pytest.raises(UniqueConstraintError):
            _exec(db, "INSERT INTO orders VALUES (1, 10, 9)")  # duplicate (1, 10)

    def test_no_cross_tenant_leak(self):
        """Tenant A's data must not appear in tenant B's query results."""
        db = Database(":memory:")
        _exec(db, "CREATE TABLE t (tenant INTEGER, score INTEGER)")
        _exec(db, "CREATE INDEX idx ON t (tenant, score)")
        for t in range(10):
            for s in range(100):
                _exec(db, f"INSERT INTO t VALUES ({t}, {s})")
        rows = _rows(db, "SELECT score FROM t WHERE tenant = 5 AND score > 90")
        assert len(rows) == 9  # 91-99
        assert all(r["score"] > 90 for r in rows)

    def test_range_returns_empty_when_no_match(self):
        db = Database(":memory:")
        _exec(db, "CREATE TABLE t (tenant INTEGER, val INTEGER)")
        _exec(db, "CREATE INDEX idx ON t (tenant, val)")
        for v in range(5):
            _exec(db, f"INSERT INTO t VALUES (1, {v})")
        rows = _rows(db, "SELECT val FROM t WHERE tenant = 1 AND val > 100")
        assert rows == []


# ── Key encoding: sort order preserved ────────────────────────────────────────

class TestEncodingSortOrder:
    def test_composite_keys_sorted_by_first_column(self):
        """Index entries with larger first-column values must appear after smaller ones."""
        from hyperion.encoding import _encode_composite_key, _make_index_key
        from hyperion.constants import INTEGER
        # (1, 0) < (2, 0) < (2, 999)
        k1 = _make_index_key(_encode_composite_key([1, 0],   [INTEGER, INTEGER]), 0)
        k2 = _make_index_key(_encode_composite_key([2, 0],   [INTEGER, INTEGER]), 0)
        k3 = _make_index_key(_encode_composite_key([2, 999], [INTEGER, INTEGER]), 0)
        assert k1 < k2 < k3

    def test_composite_keys_sorted_by_second_column(self):
        """With equal first columns, sort must be determined by the second column."""
        from hyperion.encoding import _encode_composite_key, _make_index_key
        from hyperion.constants import INTEGER, TEXT
        k_lo = _make_index_key(_encode_composite_key([5, "alpha"], [INTEGER, TEXT]), 0)
        k_hi = _make_index_key(_encode_composite_key([5, "zeta"],  [INTEGER, TEXT]), 0)
        assert k_lo < k_hi

    def test_small_integer_ids_are_distinct(self):
        """Consecutive small integer first-column values must produce different keys."""
        from hyperion.encoding import _encode_composite_key, _make_index_key
        from hyperion.constants import INTEGER
        keys = [
            _make_index_key(_encode_composite_key([i, 0], [INTEGER, INTEGER]), 0)
            for i in range(10)
        ]
        # All keys must be strictly increasing
        assert keys == sorted(set(keys)), "composite keys for consecutive ints are not distinct/ordered"
