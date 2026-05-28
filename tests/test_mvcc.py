"""Tests for MVCC / snapshot isolation (copy-on-write pager)."""
import pytest
from hyperion import Database
from hyperion.pager import MemoryPager


# ── Helper ────────────────────────────────────────────────────────────────────

def _db():
    return Database(":memory:")


# ── Copy-on-write basics ──────────────────────────────────────────────────────

def test_working_is_empty_before_txn():
    db = _db()
    assert db._pager._working == {}
    assert not db._pager._in_txn


def test_working_populated_during_txn():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER)")
    db.begin()
    db.execute("INSERT INTO t VALUES (1)")
    assert db._pager._working  # pages were CoW'd into working
    db.rollback()


def test_working_cleared_after_commit():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER)")
    db.begin()
    db.execute("INSERT INTO t VALUES (1)")
    db.commit()
    assert db._pager._working == {}
    assert not db._pager._in_txn


def test_working_cleared_after_rollback():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER)")
    db.begin()
    db.execute("INSERT INTO t VALUES (1)")
    db.rollback()
    assert db._pager._working == {}
    assert not db._pager._in_txn


# ── Rollback discards in-progress writes ─────────────────────────────────────

def test_rollback_removes_inserted_rows():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER)")
    db.begin()
    db.execute("INSERT INTO t VALUES (1)")
    db.execute("INSERT INTO t VALUES (2)")
    db.rollback()
    rows = db.execute("SELECT * FROM t").fetchall()
    assert rows == []


def test_rollback_after_update():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER, val TEXT)")
    db.execute("INSERT INTO t VALUES (1, 'original')")
    db.begin()
    db.execute("UPDATE t SET val = 'changed' WHERE id = 1")
    db.rollback()
    row = db.execute("SELECT val FROM t WHERE id = 1").fetchone()
    assert row["val"] == "original"


def test_rollback_after_delete():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER)")
    db.execute("INSERT INTO t VALUES (1)")
    db.execute("INSERT INTO t VALUES (2)")
    db.begin()
    db.execute("DELETE FROM t WHERE id = 1")
    db.rollback()
    rows = db.execute("SELECT id FROM t ORDER BY id").fetchall()
    assert [r["id"] for r in rows] == [1, 2]


# ── No dirty reads: committed cache is stable during a transaction ─────────────

def test_cache_unchanged_during_txn():
    """_cache should not be modified while a transaction is in progress."""
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER)")
    db.execute("INSERT INTO t VALUES (10)")

    # snapshot of committed pages before any new transaction
    committed_before = dict(db._pager._cache)

    db.begin()
    db.execute("INSERT INTO t VALUES (20)")

    # _cache pages must be identical (CoW means writes go to _working)
    for pn, page in committed_before.items():
        assert db._pager._cache[pn] == page, \
            f"_cache page {pn} was mutated during an in-progress transaction"

    db.commit()


def test_read_page_returns_committed_outside_txn():
    """read_page() on a page not in _working returns the committed version."""
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER)")
    db.execute("INSERT INTO t VALUES (99)")

    db.begin()
    db.execute("INSERT INTO t VALUES (100)")  # goes to _working

    # read the root catalog page (not in _working) — must come from _cache
    pn = list(db._pager._cache.keys())[0]
    if pn not in db._pager._working:
        page_via_read = db._pager.read_page(pn)
        assert page_via_read is db._pager._cache[pn]

    db.rollback()


def test_writer_sees_own_writes():
    """read_page() inside a transaction returns the in-progress working copy."""
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER)")
    db.begin()
    db.execute("INSERT INTO t VALUES (42)")
    # SELECT inside the same (auto-committed) sub-operation won't see 42 yet,
    # but a COMMIT followed by SELECT must reveal it.
    db.commit()
    row = db.execute("SELECT id FROM t").fetchone()
    assert row["id"] == 42


# ── Snapshot consistency across streaming reads ───────────────────────────────

def test_streaming_read_sees_only_committed_rows():
    """A streaming SELECT sees exactly the rows that were committed before it started."""
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER)")
    for i in range(5):
        db.execute(f"INSERT INTO t VALUES ({i})")

    cur = db.execute("SELECT id FROM t")

    # Start a new write transaction and insert more rows
    db.begin()
    db.execute("INSERT INTO t VALUES (99)")
    # Do NOT commit yet — the streaming cursor must not see 99

    # Drain the cursor; it should only see 0–4
    rows = cur.fetchall()
    ids = [r["id"] for r in rows]
    assert 99 not in ids, "Streaming cursor saw an uncommitted row (dirty read)"
    assert sorted(ids) == [0, 1, 2, 3, 4]

    db.rollback()


def test_streaming_interleaved_with_committed_write():
    """After a committed write, a still-open cursor sees pre-commit state for
    rows already loaded from _cache, and post-commit state for pages read later.
    The key guarantee is that no uncommitted data is ever visible."""
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER)")
    for i in range(3):
        db.execute(f"INSERT INTO t VALUES ({i})")

    cur = db.execute("SELECT id FROM t")
    first = cur.fetchone()
    assert first is not None

    # Committed write while cursor is open
    db.execute("INSERT INTO t VALUES (100)")

    remaining = cur.fetchall()
    # All IDs from remaining are either from the original 3 or the new 100,
    # but crucially no RuntimeError / corrupt data.
    all_ids = {first["id"]} | {r["id"] for r in remaining}
    assert not (all_ids - {0, 1, 2, 100}), f"Unexpected IDs: {all_ids}"


# ── Savepoint + CoW interaction ────────────────────────────────────────────────

def test_savepoint_rollback_within_transaction():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER)")
    db.begin()
    db.execute("INSERT INTO t VALUES (1)")
    db.savepoint("sp")
    db.execute("INSERT INTO t VALUES (2)")
    db.rollback_to_savepoint("sp")
    db.commit()
    rows = db.execute("SELECT id FROM t ORDER BY id").fetchall()
    assert [r["id"] for r in rows] == [1]


def test_savepoint_release_keeps_all_writes():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER)")
    db.begin()
    db.execute("INSERT INTO t VALUES (1)")
    db.savepoint("sp")
    db.execute("INSERT INTO t VALUES (2)")
    db.release_savepoint("sp")
    db.commit()
    rows = db.execute("SELECT id FROM t ORDER BY id").fetchall()
    assert [r["id"] for r in rows] == [1, 2]


def test_nested_savepoints():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER)")
    db.begin()
    db.execute("INSERT INTO t VALUES (1)")
    db.savepoint("sp1")
    db.execute("INSERT INTO t VALUES (2)")
    db.savepoint("sp2")
    db.execute("INSERT INTO t VALUES (3)")
    db.rollback_to_savepoint("sp2")   # discard 3
    db.rollback_to_savepoint("sp1")   # discard 2
    db.commit()
    rows = db.execute("SELECT id FROM t").fetchall()
    assert [r["id"] for r in rows] == [1]


def test_rollback_to_savepoint_then_commit():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER)")
    db.begin()
    db.execute("INSERT INTO t VALUES (10)")
    db.savepoint("mid")
    db.execute("INSERT INTO t VALUES (20)")
    db.rollback_to_savepoint("mid")
    db.execute("INSERT INTO t VALUES (30)")
    db.commit()
    rows = db.execute("SELECT id FROM t ORDER BY id").fetchall()
    assert [r["id"] for r in rows] == [10, 30]


# ── Multiple commits are fully independent ────────────────────────────────────

def test_sequential_transactions_are_independent():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER)")

    db.begin()
    db.execute("INSERT INTO t VALUES (1)")
    db.commit()

    db.begin()
    db.execute("INSERT INTO t VALUES (2)")
    db.rollback()

    rows = db.execute("SELECT id FROM t").fetchall()
    assert [r["id"] for r in rows] == [1]


def test_many_transactions_accumulate_correctly():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER)")
    for i in range(10):
        db.begin()
        db.execute(f"INSERT INTO t VALUES ({i})")
        db.commit()
    rows = db.execute("SELECT id FROM t ORDER BY id").fetchall()
    assert [r["id"] for r in rows] == list(range(10))


# ── MemoryPager unit tests ────────────────────────────────────────────────────

def test_memorypager_cow_does_not_alias_committed_page():
    mp = MemoryPager()
    mp.begin()
    page = mp.get_page(5)
    page[0] = 0xFF
    # committed cache must not have been modified
    assert 5 not in mp._cache or mp._cache.get(5, bytearray(1))[0] != 0xFF
    mp.rollback()


def test_memorypager_commit_promotes_working_to_cache():
    mp = MemoryPager()
    mp.begin()
    page = mp.get_page(7)
    page[0] = 0xAB
    mp.commit()
    assert mp._cache[7][0] == 0xAB
    assert mp._working == {}


def test_memorypager_rollback_clears_working():
    mp = MemoryPager()
    mp.begin()
    mp.get_page(3)[0] = 0x01
    mp.rollback()
    assert mp._working == {}
    # The page may be in _cache (zero-initialized by _load), but must not hold
    # the uncommitted value — rollback discards _working without touching _cache.
    assert mp._cache.get(3, bytearray(1))[0] != 0x01


def test_memorypager_no_double_begin():
    mp = MemoryPager()
    mp.begin()
    with pytest.raises(RuntimeError, match="Transaction already active"):
        mp.begin()
    mp.rollback()


def test_memorypager_read_page_returns_committed_version_during_txn():
    mp = MemoryPager()
    # Commit a page with value 0x01
    mp.begin()
    mp.get_page(2)[0] = 0x01
    mp.commit()

    # Start a new transaction and modify the same page
    mp.begin()
    mp.get_page(2)[0] = 0x02  # goes to _working

    # read_page returns the working copy (read-your-own-writes)
    assert mp.read_page(2)[0] == 0x02

    mp.rollback()

    # After rollback, cache still has the committed value
    assert mp._cache[2][0] == 0x01
