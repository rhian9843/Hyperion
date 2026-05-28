"""Tests for read-only connection mode."""
import pytest
from hyperion import Database
from hyperion.executor import ReadOnlyError


# ── Toggle / context manager ──────────────────────────────────────────────────

def test_readonly_property_readable():
    db = Database(":memory:")
    assert db.readonly is False
    db.close()


def test_readonly_property_settable():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER)")
    db.readonly = True
    with pytest.raises(ReadOnlyError):
        db.execute("INSERT INTO t VALUES (1)")
    db.readonly = False
    db.execute("INSERT INTO t VALUES (1)")  # should succeed
    db.close()


def test_as_readonly_context_manager_blocks_writes():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER)")
    with db.as_readonly():
        with pytest.raises(ReadOnlyError):
            db.execute("INSERT INTO t VALUES (1)")
    db.close()


def test_as_readonly_restores_writable_on_exit():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER)")
    with db.as_readonly():
        pass
    db.execute("INSERT INTO t VALUES (1)")  # writable again
    rows = db.execute("SELECT COUNT(*) AS n FROM t").fetchone()
    assert rows["n"] == 1
    db.close()


def test_as_readonly_restores_on_exception():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER)")
    try:
        with db.as_readonly():
            raise ValueError("boom")
    except ValueError:
        pass
    assert db.readonly is False
    db.execute("INSERT INTO t VALUES (1)")  # writable again
    db.close()


def test_as_readonly_select_passes_through():
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER)")
    db.execute("INSERT INTO t VALUES (42)")
    with db.as_readonly() as rdb:
        row = rdb.execute("SELECT id FROM t").fetchone()
    assert row["id"] == 42
    db.close()


def test_as_readonly_on_already_readonly_connection():
    """as_readonly() on a permanently-readonly connection stays readonly on exit."""
    db = Database(":memory:", readonly=True)
    with db.as_readonly():
        pass
    assert db.readonly is True  # original state restored
    db.close()


def _populated_db(tmp_path):
    db = Database(tmp_path / "db.hdb")
    db.execute("CREATE TABLE t (id INTEGER, val TEXT)")
    db.execute("INSERT INTO t VALUES (1, 'a')")
    db.execute("INSERT INTO t VALUES (2, 'b')")
    db.close()
    return tmp_path / "db.hdb"


# ── Constructor / flag ────────────────────────────────────────────────────────

def test_readonly_flag_stored():
    db = Database(":memory:", readonly=True)
    assert db._readonly is True
    db.close()


def test_writable_by_default():
    db = Database(":memory:")
    assert db._readonly is False
    db.close()


# ── SELECT passes through ─────────────────────────────────────────────────────

def test_select_allowed_in_readonly(tmp_path):
    path = _populated_db(tmp_path)
    db = Database(path, readonly=True)
    rows = db.execute("SELECT * FROM t ORDER BY id").fetchall()
    assert len(rows) == 2
    db.close()


def test_select_nofrom_allowed_in_readonly():
    db = Database(":memory:", readonly=True)
    row = db.execute("SELECT 1 + 1").fetchone()
    assert row["1 + 1"] == 2
    db.close()


# ── DML blocked ───────────────────────────────────────────────────────────────

def test_insert_blocked_in_readonly(tmp_path):
    path = _populated_db(tmp_path)
    db = Database(path, readonly=True)
    with pytest.raises(ReadOnlyError):
        db.execute("INSERT INTO t VALUES (3, 'c')")
    db.close()


def test_update_blocked_in_readonly(tmp_path):
    path = _populated_db(tmp_path)
    db = Database(path, readonly=True)
    with pytest.raises(ReadOnlyError):
        db.execute("UPDATE t SET val = 'x' WHERE id = 1")
    db.close()


def test_delete_blocked_in_readonly(tmp_path):
    path = _populated_db(tmp_path)
    db = Database(path, readonly=True)
    with pytest.raises(ReadOnlyError):
        db.execute("DELETE FROM t WHERE id = 1")
    db.close()


def test_truncate_blocked_in_readonly(tmp_path):
    path = _populated_db(tmp_path)
    db = Database(path, readonly=True)
    with pytest.raises(ReadOnlyError):
        db.execute("TRUNCATE TABLE t")
    db.close()


# ── DDL blocked ───────────────────────────────────────────────────────────────

def test_create_table_blocked_in_readonly():
    db = Database(":memory:", readonly=True)
    with pytest.raises(ReadOnlyError):
        db.execute("CREATE TABLE new_t (id INTEGER)")
    db.close()


def test_drop_table_blocked_in_readonly(tmp_path):
    path = _populated_db(tmp_path)
    db = Database(path, readonly=True)
    with pytest.raises(ReadOnlyError):
        db.execute("DROP TABLE t")
    db.close()


def test_create_index_blocked_in_readonly(tmp_path):
    path = _populated_db(tmp_path)
    db = Database(path, readonly=True)
    with pytest.raises(ReadOnlyError):
        db.execute("CREATE INDEX idx ON t(val)")
    db.close()


def test_create_view_blocked_in_readonly(tmp_path):
    path = _populated_db(tmp_path)
    db = Database(path, readonly=True)
    with pytest.raises(ReadOnlyError):
        db.execute("CREATE VIEW v AS SELECT * FROM t")
    db.close()


# ── Data unchanged after blocked write ────────────────────────────────────────

def test_data_unchanged_after_blocked_insert(tmp_path):
    path = _populated_db(tmp_path)
    db = Database(path, readonly=True)
    with pytest.raises(ReadOnlyError):
        db.execute("INSERT INTO t VALUES (99, 'z')")
    rows = db.execute("SELECT COUNT(*) AS n FROM t").fetchone()
    assert rows["n"] == 2
    db.close()


# ── Writable db unaffected ────────────────────────────────────────────────────

def test_writable_db_still_works(tmp_path):
    path = _populated_db(tmp_path)
    db = Database(path)
    db.execute("INSERT INTO t VALUES (3, 'c')")
    rows = db.execute("SELECT COUNT(*) AS n FROM t").fetchone()
    assert rows["n"] == 3
    db.close()


# ── Error message ─────────────────────────────────────────────────────────────

def test_readonly_error_message_contains_op(tmp_path):
    path = _populated_db(tmp_path)
    db = Database(path, readonly=True)
    with pytest.raises(ReadOnlyError) as exc_info:
        db.execute("INSERT INTO t VALUES (3, 'c')")
    assert "INSERT" in str(exc_info.value)
    assert "read-only" in str(exc_info.value)
    db.close()
