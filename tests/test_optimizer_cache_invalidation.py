"""Tests that the optimizer row-count cache is invalidated after writes.

Before the fix, _opt_row_counts was only populated on first access and
never evicted, so after a bulk INSERT (or DELETE / TRUNCATE / DROP+recreate)
the optimizer would still use the stale row count when computing join order.
"""
from hyperion import Database
from hyperion.optimizer import estimate_rows


def _exec(db, sql):
    cur = db.cursor()
    cur.execute(sql)
    return cur


class TestRowCountCacheInvalidation:
    def test_cache_warm_between_reads(self):
        """Two consecutive reads without a write return the same (cached) value."""
        db = Database(":memory:")
        _exec(db, "CREATE TABLE t (id INTEGER)")
        for i in range(10):
            _exec(db, f"INSERT INTO t VALUES ({i})")
        assert estimate_rows(db, "t") == estimate_rows(db, "t")

    def test_insert_invalidates_cache(self):
        db = Database(":memory:")
        _exec(db, "CREATE TABLE t (id INTEGER)")
        for i in range(5):
            _exec(db, f"INSERT INTO t VALUES ({i})")
        assert estimate_rows(db, "t") == 5
        _exec(db, "INSERT INTO t VALUES (99)")
        assert estimate_rows(db, "t") == 6

    def test_delete_invalidates_cache(self):
        db = Database(":memory:")
        _exec(db, "CREATE TABLE t (id INTEGER)")
        for i in range(10):
            _exec(db, f"INSERT INTO t VALUES ({i})")
        assert estimate_rows(db, "t") == 10
        _exec(db, "DELETE FROM t WHERE id < 5")
        assert estimate_rows(db, "t") == 5

    def test_update_invalidates_cache(self):
        """UPDATE doesn't change row count but must still evict stale entry."""
        db = Database(":memory:")
        _exec(db, "CREATE TABLE t (id INTEGER, val INTEGER)")
        for i in range(8):
            _exec(db, f"INSERT INTO t VALUES ({i}, 0)")
        first = estimate_rows(db, "t")
        _exec(db, "UPDATE t SET val = 1 WHERE id < 4")
        second = estimate_rows(db, "t")
        assert first == second == 8  # count unchanged, but cache re-derived correctly

    def test_truncate_invalidates_cache(self):
        db = Database(":memory:")
        _exec(db, "CREATE TABLE t (id INTEGER)")
        for i in range(20):
            _exec(db, f"INSERT INTO t VALUES ({i})")
        assert estimate_rows(db, "t") == 20
        _exec(db, "TRUNCATE TABLE t")
        assert estimate_rows(db, "t") == 0

    def test_drop_table_invalidates_cache(self):
        db = Database(":memory:")
        _exec(db, "CREATE TABLE t (id INTEGER)")
        for i in range(15):
            _exec(db, f"INSERT INTO t VALUES ({i})")
        assert estimate_rows(db, "t") == 15
        _exec(db, "DROP TABLE t")
        # After drop the entry must be gone from cache; recreate should give fresh count
        _exec(db, "CREATE TABLE t (id INTEGER)")
        for i in range(3):
            _exec(db, f"INSERT INTO t VALUES ({i})")
        assert estimate_rows(db, "t") == 3

    def test_insert_select_invalidates_cache(self):
        db = Database(":memory:")
        _exec(db, "CREATE TABLE src (id INTEGER)")
        _exec(db, "CREATE TABLE dst (id INTEGER)")
        for i in range(7):
            _exec(db, f"INSERT INTO src VALUES ({i})")
        assert estimate_rows(db, "dst") == 0
        _exec(db, "INSERT INTO dst SELECT id FROM src")
        assert estimate_rows(db, "dst") == 7

    def test_bulk_insert_then_join_uses_fresh_count(self):
        """After a bulk load the optimizer must see the updated row count."""
        db = Database(":memory:")
        _exec(db, "CREATE TABLE a (id INTEGER)")
        _exec(db, "CREATE TABLE b (id INTEGER, aid INTEGER)")
        # Warm the cache with empty tables
        assert estimate_rows(db, "a") == 0
        assert estimate_rows(db, "b") == 0
        # Bulk-load
        for i in range(100):
            _exec(db, f"INSERT INTO a VALUES ({i})")
        for i in range(5):
            _exec(db, f"INSERT INTO b VALUES ({i}, {i})")
        # Optimizer must see a=100, b=5 (not stale 0/0)
        assert estimate_rows(db, "a") == 100
        assert estimate_rows(db, "b") == 5
