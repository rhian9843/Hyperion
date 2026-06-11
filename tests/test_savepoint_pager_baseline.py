"""
Regression tests for: ROLLBACK TO SAVEPOINT + COMMIT corrupts pager undo snapshot.

After a transaction that uses ROLLBACK TO SAVEPOINT followed by COMMIT, a
subsequent BEGIN … ROLLBACK must restore to the committed state (not to the
savepoint's pre-state).
"""
import pytest
from hyperion import Database


@pytest.fixture
def db():
    d = Database(":memory:")
    d.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
    yield d
    d.close()


def _rows(db, table="t"):
    return db.execute(f"SELECT id, val FROM {table} ORDER BY id").fetchall()


# ── Core regression: the exact scenario from the bug report ──────────────────

def test_rollback_after_savepoint_txn_sees_committed_state(db):
    """ROLLBACK in tx3 must not revert rows committed by tx2."""
    db.execute("INSERT INTO t VALUES (1, 'a')")
    db.execute("INSERT INTO t VALUES (2, 'b')")
    db.execute("INSERT INTO t VALUES (3, 'c')")

    # tx2: UPDATE, savepoint, another UPDATE, rollback to savepoint, commit
    db.execute("BEGIN")
    db.execute("UPDATE t SET val = 'updated' WHERE id = 1")
    db.execute("SAVEPOINT sp")
    db.execute("UPDATE t SET val = 'second' WHERE id = 2")
    db.execute("ROLLBACK TO SAVEPOINT sp")
    db.execute("COMMIT")

    # tx3: insert a row then roll back
    db.execute("BEGIN")
    db.execute("INSERT INTO t VALUES (4, 'd')")
    db.execute("ROLLBACK")

    rows = _rows(db)
    assert len(rows) == 3, f"Expected 3 rows, got {len(rows)}: {rows}"
    assert rows[0] == {"id": 1, "val": "updated"}
    assert rows[1] == {"id": 2, "val": "b"}
    assert rows[2] == {"id": 3, "val": "c"}


def test_table_not_empty_after_savepoint_rollback_sequence(db):
    """Table must not appear empty after the savepoint+rollback+commit pattern."""
    db.execute("INSERT INTO t VALUES (1, 'x')")

    db.execute("BEGIN")
    db.execute("SAVEPOINT sp")
    db.execute("INSERT INTO t VALUES (2, 'y')")
    db.execute("ROLLBACK TO SAVEPOINT sp")
    db.execute("COMMIT")

    db.execute("BEGIN")
    db.execute("ROLLBACK")

    rows = _rows(db)
    assert rows == [{"id": 1, "val": "x"}], f"Expected 1 row, got: {rows}"


# ── Ops page integrity after multiple savepoint transactions ──────────────────

def test_catalog_root_page_intact_after_savepoint_commit(db):
    """The catalog's root_page for the table must be non-zero after the pattern."""
    db.execute("INSERT INTO t VALUES (1, 'a')")

    db.execute("BEGIN")
    db.execute("SAVEPOINT sp")
    db.execute("INSERT INTO t VALUES (2, 'b')")
    db.execute("ROLLBACK TO SAVEPOINT sp")
    db.execute("COMMIT")

    assert db._catalog.tables["t"].root_page != 0, \
        "root_page became 0 after ROLLBACK TO SAVEPOINT + COMMIT"


def test_subsequent_commit_works_after_savepoint_txn(db):
    """A normal commit after the savepoint pattern must persist its rows."""
    db.execute("INSERT INTO t VALUES (1, 'a')")

    db.execute("BEGIN")
    db.execute("SAVEPOINT sp")
    db.execute("INSERT INTO t VALUES (2, 'b')")
    db.execute("ROLLBACK TO SAVEPOINT sp")
    db.execute("COMMIT")

    db.execute("INSERT INTO t VALUES (3, 'c')")

    rows = _rows(db)
    assert len(rows) == 2
    assert rows[0]["id"] == 1
    assert rows[1]["id"] == 3


def test_multiple_savepoint_rollback_commit_cycles(db):
    """Repeated savepoint+rollback+commit cycles must not corrupt the ops page."""
    for i in range(1, 6):
        db.execute(f"INSERT INTO t VALUES ({i}, 'base{i}')")

    for cycle in range(3):
        db.execute("BEGIN")
        db.execute("SAVEPOINT sp")
        for j in range(10, 15):
            db.execute(f"INSERT INTO t VALUES ({cycle * 100 + j}, 'tmp')")
        db.execute("ROLLBACK TO SAVEPOINT sp")
        db.execute("COMMIT")

        # After each cycle, rolling back a new txn must still see 5 base rows
        db.execute("BEGIN")
        db.execute("INSERT INTO t VALUES (999, 'gone')")
        db.execute("ROLLBACK")

        rows = _rows(db)
        assert len(rows) == 5, \
            f"Cycle {cycle}: expected 5 rows, got {len(rows)}: {rows}"


# ── Savepoint with no modifications before the savepoint ─────────────────────

def test_savepoint_at_txn_start_no_prior_dirty(db):
    """Savepoint taken before any modification in the transaction."""
    db.execute("INSERT INTO t VALUES (1, 'a')")

    db.execute("BEGIN")
    db.execute("SAVEPOINT sp")   # no dirty pages at savepoint time
    db.execute("INSERT INTO t VALUES (2, 'b')")
    db.execute("ROLLBACK TO SAVEPOINT sp")
    db.execute("COMMIT")

    db.execute("BEGIN")
    db.execute("ROLLBACK")

    rows = _rows(db)
    assert rows == [{"id": 1, "val": "a"}]


# ── Index ops survive the same pattern ───────────────────────────────────────

def test_index_ops_intact_after_savepoint_commit():
    """Index root_page must not be zeroed out after the savepoint pattern."""
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
    db.execute("CREATE INDEX idx_val ON t(val)")
    db.execute("INSERT INTO t VALUES (1, 'hello')")

    db.execute("BEGIN")
    db.execute("SAVEPOINT sp")
    db.execute("INSERT INTO t VALUES (2, 'world')")
    db.execute("ROLLBACK TO SAVEPOINT sp")
    db.execute("COMMIT")

    db.execute("BEGIN")
    db.execute("ROLLBACK")

    # Index must still be queryable
    rows = db.execute("SELECT id FROM t WHERE val = 'hello'").fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == 1
    db.close()
