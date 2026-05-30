"""Tests for index-accelerated UNIQUE constraint checking.

Before the fix, _check_unique always did a full O(n) table scan.
After the fix it uses an index probe when a matching index exists and
only falls back to a scan for constraints with no backing index.
"""
import pytest
from hyperion import Database
from hyperion.errors import UniqueConstraintError


def _exec(db, sql):
    cur = db.cursor()
    cur.execute(sql)
    return cur


# ── Correctness: constraint still enforced via index path ─────────────────────

class TestUniquenessCorrectness:
    def test_primary_key_duplicate_raises(self):
        db = Database(":memory:")
        _exec(db, "CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
        _exec(db, "INSERT INTO t VALUES (1, 'a')")
        with pytest.raises(UniqueConstraintError):
            _exec(db, "INSERT INTO t VALUES (1, 'b')")

    def test_unique_column_duplicate_raises(self):
        db = Database(":memory:")
        _exec(db, "CREATE TABLE t (id INTEGER, email TEXT UNIQUE)")
        _exec(db, "INSERT INTO t VALUES (1, 'a@x.com')")
        with pytest.raises(UniqueConstraintError):
            _exec(db, "INSERT INTO t VALUES (2, 'a@x.com')")

    def test_unique_column_with_index_raises(self):
        """UNIQUE column + explicit index → conflict caught via index probe."""
        db = Database(":memory:")
        _exec(db, "CREATE TABLE t (id INTEGER, code TEXT UNIQUE)")
        _exec(db, "CREATE INDEX idx_code ON t (code)")
        for i in range(20):
            _exec(db, f"INSERT INTO t VALUES ({i}, 'code{i}')")
        with pytest.raises(UniqueConstraintError):
            _exec(db, "INSERT INTO t VALUES (99, 'code5')")

    def test_unique_null_is_allowed(self):
        """NULL never violates UNIQUE — multiple NULLs are permitted."""
        db = Database(":memory:")
        _exec(db, "CREATE TABLE t (id INTEGER, val TEXT UNIQUE)")
        _exec(db, "INSERT INTO t VALUES (1, NULL)")
        _exec(db, "INSERT INTO t VALUES (2, NULL)")  # must not raise

    def test_multi_col_unique_raises(self):
        db = Database(":memory:")
        _exec(db, "CREATE TABLE t (a INTEGER, b INTEGER, UNIQUE (a, b))")
        _exec(db, "INSERT INTO t VALUES (1, 2)")
        with pytest.raises(UniqueConstraintError):
            _exec(db, "INSERT INTO t VALUES (1, 2)")

    def test_multi_col_unique_partial_overlap_ok(self):
        db = Database(":memory:")
        _exec(db, "CREATE TABLE t (a INTEGER, b INTEGER, UNIQUE (a, b))")
        _exec(db, "INSERT INTO t VALUES (1, 2)")
        _exec(db, "INSERT INTO t VALUES (1, 3)")  # same a, different b — OK
        _exec(db, "INSERT INTO t VALUES (2, 2)")  # different a, same b — OK

    def test_multi_col_unique_with_index_raises(self):
        """Multi-column UNIQUE + matching composite index → probe used."""
        db = Database(":memory:")
        _exec(db, "CREATE TABLE t (tenant INTEGER, key TEXT, UNIQUE (tenant, key))")
        _exec(db, "CREATE INDEX idx_tk ON t (tenant, key)")
        for i in range(20):
            _exec(db, f"INSERT INTO t VALUES ({i % 5}, 'k{i}')")
        with pytest.raises(UniqueConstraintError):
            _exec(db, "INSERT INTO t VALUES (0, 'k0')")

    def test_update_does_not_conflict_with_self(self):
        """UPDATE that keeps the same UNIQUE value must not raise."""
        db = Database(":memory:")
        _exec(db, "CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT UNIQUE)")
        _exec(db, "INSERT INTO t VALUES (1, 'hello')")
        _exec(db, "UPDATE t SET val = 'hello' WHERE id = 1")  # same value, must not raise

    def test_update_to_existing_value_raises(self):
        db = Database(":memory:")
        _exec(db, "CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT UNIQUE)")
        _exec(db, "INSERT INTO t VALUES (1, 'hello')")
        _exec(db, "INSERT INTO t VALUES (2, 'world')")
        with pytest.raises(UniqueConstraintError):
            _exec(db, "UPDATE t SET val = 'hello' WHERE id = 2")


# ── Performance: index probe bypasses scan ────────────────────────────────────

class TestIndexProbeUsed:
    def test_pk_insert_scans_zero_rows(self):
        """With a PRIMARY KEY index, _check_unique should touch 0 table rows."""
        db = Database(":memory:")
        _exec(db, "CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
        for i in range(200):
            _exec(db, f"INSERT INTO t VALUES ({i}, 'v')")

        scan_calls = []
        orig_scan = db._table_btree(db._meta("t")).scan

        def counting_scan():
            scan_calls.append(1)
            return orig_scan()

        import unittest.mock as mock
        meta = db._meta("t")
        btree = db._table_btree(meta)
        with mock.patch.object(btree.__class__, "scan",
                               lambda self: counting_scan()):
            # Duplicate PK — should raise via index probe, not scan
            with pytest.raises(UniqueConstraintError):
                _exec(db, "INSERT INTO t VALUES (50, 'dup')")

        assert scan_calls == [], \
            f"_check_unique triggered a table scan ({len(scan_calls)} calls) even though a PK index exists"

    def test_unique_col_with_index_scans_zero_rows(self):
        """UNIQUE col + explicit index → no table scan during constraint check."""
        db = Database(":memory:")
        _exec(db, "CREATE TABLE t (id INTEGER, code TEXT UNIQUE)")
        _exec(db, "CREATE INDEX idx_code ON t (code)")
        for i in range(200):
            _exec(db, f"INSERT INTO t VALUES ({i}, 'c{i}')")

        scan_calls = []

        import unittest.mock as mock
        meta = db._meta("t")
        btree = db._table_btree(meta)

        def counting_scan():
            scan_calls.append(1)
            return iter([])  # return empty so scan finishes without raising

        with mock.patch.object(btree.__class__, "scan",
                               lambda self: counting_scan()):
            with pytest.raises(UniqueConstraintError):
                _exec(db, "INSERT INTO t VALUES (999, 'c100')")

        assert scan_calls == [], \
            f"Expected index probe but table scan was called {len(scan_calls)} time(s)"

    def test_unindexed_unique_col_uses_scan_fallback(self):
        """UNIQUE col with no backing index must still catch violations via scan."""
        db = Database(":memory:")
        # No explicit index on 'email'; no auto-index for UNIQUE-only columns
        _exec(db, "CREATE TABLE t (id INTEGER, email TEXT UNIQUE)")
        for i in range(10):
            _exec(db, f"INSERT INTO t VALUES ({i}, 'u{i}@x.com')")
        with pytest.raises(UniqueConstraintError):
            _exec(db, "INSERT INTO t VALUES (99, 'u5@x.com')")
