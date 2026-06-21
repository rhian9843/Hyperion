"""Tests for SHOW LOGICAL LOG."""
import json
import pytest
from hyperion import Database


def _fetch(cursor):
    return list(cursor.fetchall())


@pytest.fixture
def db(tmp_path):
    """Database with a publication so changelog entries are recorded."""
    d = Database(str(tmp_path / "test.hdb"))
    d.execute("CREATE TABLE users (id INTEGER, name TEXT, age INTEGER)")
    d.execute("CREATE TABLE orders (id INTEGER, user_id INTEGER, amount REAL)")
    d.execute("CREATE PUBLICATION pub FOR TABLE users, orders")
    yield d
    d.close()


@pytest.fixture
def db_with_data(db):
    db.execute("INSERT INTO users VALUES (1, 'Alice', 30)")
    db.execute("INSERT INTO users VALUES (2, 'Bob',   25)")
    db.execute("UPDATE users SET age = 31 WHERE id = 1")
    db.execute("DELETE FROM users WHERE id = 2")
    db.execute("INSERT INTO orders VALUES (10, 1, 99.99)")
    return db


# --- Column structure ---

def test_show_logical_log_columns(db_with_data):
    rows = _fetch(db_with_data.execute("SHOW LOGICAL LOG"))
    assert len(rows) > 0
    row = rows[0]
    assert "lsn" in row
    assert "timestamp" in row
    assert "table" in row
    assert "op" in row
    assert "row" in row
    assert "row_before" in row


# --- Row count and ordering ---

def test_show_logical_log_all_entries(db_with_data):
    rows = _fetch(db_with_data.execute("SHOW LOGICAL LOG"))
    assert len(rows) == 5


def test_show_logical_log_lsn_ascending(db_with_data):
    rows = _fetch(db_with_data.execute("SHOW LOGICAL LOG"))
    lsns = [r["lsn"] for r in rows]
    assert lsns == sorted(lsns)
    assert lsns == list(range(1, 6))


# --- Operations ---

def test_show_logical_log_insert_ops(db_with_data):
    rows = _fetch(db_with_data.execute("SHOW LOGICAL LOG"))
    inserts = [r for r in rows if r["op"] == "INSERT"]
    assert len(inserts) == 3  # 2 users + 1 order


def test_show_logical_log_update_op(db_with_data):
    rows = _fetch(db_with_data.execute("SHOW LOGICAL LOG"))
    updates = [r for r in rows if r["op"] == "UPDATE"]
    assert len(updates) == 1


def test_show_logical_log_delete_op(db_with_data):
    rows = _fetch(db_with_data.execute("SHOW LOGICAL LOG"))
    deletes = [r for r in rows if r["op"] == "DELETE"]
    assert len(deletes) == 1


# --- Row data ---

def test_show_logical_log_insert_row_data(db_with_data):
    rows = _fetch(db_with_data.execute("SHOW LOGICAL LOG"))
    first_insert = next(r for r in rows if r["op"] == "INSERT" and r["table"] == "users")
    row_data = json.loads(first_insert["row"])
    assert "id" in row_data
    assert "name" in row_data


def test_show_logical_log_insert_row_before_is_null(db_with_data):
    rows = _fetch(db_with_data.execute("SHOW LOGICAL LOG"))
    insert = next(r for r in rows if r["op"] == "INSERT")
    assert insert["row_before"] is None


def test_show_logical_log_update_has_row_before(db_with_data):
    rows = _fetch(db_with_data.execute("SHOW LOGICAL LOG"))
    update = next(r for r in rows if r["op"] == "UPDATE")
    assert update["row_before"] is not None
    before = json.loads(update["row_before"])
    after = json.loads(update["row"])
    assert before["age"] == 30
    assert after["age"] == 31


def test_show_logical_log_delete_has_row(db_with_data):
    rows = _fetch(db_with_data.execute("SHOW LOGICAL LOG"))
    delete = next(r for r in rows if r["op"] == "DELETE")
    row_data = json.loads(delete["row"])
    assert row_data["name"] == "Bob"
    assert delete["row_before"] is None


# --- Table column ---

def test_show_logical_log_table_names(db_with_data):
    rows = _fetch(db_with_data.execute("SHOW LOGICAL LOG"))
    tables = {r["table"] for r in rows}
    assert "users" in tables
    assert "orders" in tables


# --- LIMIT ---

def test_show_logical_log_limit(db_with_data):
    rows = _fetch(db_with_data.execute("SHOW LOGICAL LOG LIMIT 2"))
    assert len(rows) == 2


def test_show_logical_log_limit_returns_last_n(db_with_data):
    rows_all = _fetch(db_with_data.execute("SHOW LOGICAL LOG"))
    rows_limited = _fetch(db_with_data.execute("SHOW LOGICAL LOG LIMIT 2"))
    assert rows_limited == rows_all[-2:]


def test_show_logical_log_limit_bare_number(db_with_data):
    rows = _fetch(db_with_data.execute("SHOW LOGICAL LOG 3"))
    assert len(rows) == 3


def test_show_logical_log_limit_larger_than_entries(db_with_data):
    rows = _fetch(db_with_data.execute("SHOW LOGICAL LOG LIMIT 1000"))
    assert len(rows) == 5


# --- Empty log ---

def test_show_logical_log_empty_when_no_dml(db):
    rows = _fetch(db.execute("SHOW LOGICAL LOG"))
    assert rows == []


def test_show_logical_log_no_publication(tmp_path):
    d = Database(str(tmp_path / "nopub.hdb"))
    d.execute("CREATE TABLE t (id INTEGER)")
    d.execute("INSERT INTO t VALUES (1)")
    rows = _fetch(d.execute("SHOW LOGICAL LOG"))
    assert rows == []
    d.close()


# --- Timestamp format ---

def test_show_logical_log_timestamp_format(db_with_data):
    import re
    rows = _fetch(db_with_data.execute("SHOW LOGICAL LOG LIMIT 1"))
    ts = rows[0]["timestamp"]
    assert re.match(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$', ts)
