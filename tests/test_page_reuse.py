"""Tests verifying that B-tree split allocations reuse freed pages.

Before the fix, _make_alloc and _make_idx_alloc incremented next_free_page
directly, bypassing _alloc_page's free-list check.  Pages freed by
drop_table were added to free_pages but never consumed by subsequent
B-tree splits, so the file grew indefinitely under create/load/drop cycles.
"""
import os
import tempfile
import pytest
from hyperion import Database


def _exec(db, sql):
    cur = db.cursor()
    cur.execute(sql)
    return cur


# ── Free-list reuse ────────────────────────────────────────────────────────────

class TestPageReuse:
    def test_freed_pages_enter_free_list(self):
        db = Database(":memory:")
        _exec(db, "CREATE TABLE t (id INTEGER, val TEXT)")
        for i in range(50):
            _exec(db, f"INSERT INTO t VALUES ({i}, 'x')")
        before_hwm = db._catalog.next_free_page
        _exec(db, "DROP TABLE t")
        assert len(db._catalog.free_pages) > 0, "drop_table must populate free_pages"
        assert db._catalog.next_free_page == before_hwm, \
            "drop should not grow next_free_page, only add to free_pages"

    def test_btree_splits_consume_free_list(self):
        db = Database(":memory:")
        _exec(db, "CREATE TABLE a (id INTEGER, val TEXT)")
        for i in range(50):
            _exec(db, f"INSERT INTO a VALUES ({i}, 'x')")

        _exec(db, "DROP TABLE a")
        freed_count = len(db._catalog.free_pages)
        hwm_after_drop = db._catalog.next_free_page

        _exec(db, "CREATE TABLE b (id INTEGER, val TEXT)")
        for i in range(50):
            _exec(db, f"INSERT INTO b VALUES ({i}, 'x')")

        remaining_free = len(db._catalog.free_pages)
        assert remaining_free < freed_count, \
            f"B-tree splits must consume free pages: had {freed_count}, still have {remaining_free}"
        extra_growth = db._catalog.next_free_page - hwm_after_drop
        assert extra_growth <= freed_count, \
            "new allocations should prefer free list; HWM grew more than freed pages"

    def test_index_splits_consume_free_list(self):
        db = Database(":memory:")
        _exec(db, "CREATE TABLE t (id INTEGER)")
        _exec(db, "CREATE INDEX idx_t ON t (id)")
        for i in range(50):
            _exec(db, f"INSERT INTO t VALUES ({i})")

        _exec(db, "DROP INDEX idx_t")
        _exec(db, "DROP TABLE t")
        freed_count = len(db._catalog.free_pages)

        _exec(db, "CREATE TABLE t2 (id INTEGER)")
        _exec(db, "CREATE INDEX idx_t2 ON t2 (id)")
        for i in range(50):
            _exec(db, f"INSERT INTO t2 VALUES ({i})")

        assert len(db._catalog.free_pages) < freed_count, \
            "index B-tree splits must also consume free pages"

    def test_file_size_stable_under_churn(self):
        """Repeated create/load/drop cycles must not grow the file indefinitely."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = f.name
        try:
            db = Database(path)
            for cycle in range(5):
                _exec(db, "CREATE TABLE churn (id INTEGER, val TEXT)")
                for i in range(40):
                    _exec(db, f"INSERT INTO churn VALUES ({i}, 'data')")
                _exec(db, "DROP TABLE churn")

            db.close()
            size_after_churn = os.path.getsize(path)

            db2 = Database(path)
            _exec(db2, "CREATE TABLE final (id INTEGER, val TEXT)")
            for i in range(40):
                _exec(db2, f"INSERT INTO final VALUES ({i}, 'data')")
            db2.close()
            size_after_final = os.path.getsize(path)

            assert size_after_final <= size_after_churn * 1.5, \
                f"File grew from {size_after_churn} to {size_after_final} bytes on final insert — free pages not reused"
        finally:
            os.unlink(path)

    def test_high_water_mark_bounded_by_free_list(self):
        """HWM after refill must not exceed peak HWM when free list covers demand."""
        db = Database(":memory:")
        _exec(db, "CREATE TABLE t (id INTEGER, val TEXT)")
        for i in range(100):
            _exec(db, f"INSERT INTO t VALUES ({i}, 'row')")
        hwm_peak = db._catalog.next_free_page

        _exec(db, "DROP TABLE t")
        freed_after_drop = len(db._catalog.free_pages)

        _exec(db, "CREATE TABLE t2 (id INTEGER, val TEXT)")
        for i in range(100):
            _exec(db, f"INSERT INTO t2 VALUES ({i}, 'row')")

        hwm_new = db._catalog.next_free_page
        assert hwm_new <= hwm_peak + 5, \
            f"HWM grew to {hwm_new} but original peak was {hwm_peak} — free list not consumed by B-tree splits"
