"""Tests for EXPLAIN ANALYZE."""
import pytest
from hyperion import Database


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "test.hdb"))
    d.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER)")
    d.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER, amount REAL)")
    for i in range(1, 6):
        d.execute(f"INSERT INTO users VALUES ({i}, 'user{i}', {20 + i})")
    for i in range(1, 11):
        d.execute(f"INSERT INTO orders VALUES ({i}, {(i % 5) + 1}, {i * 10.0})")
    d.execute("CREATE INDEX idx_orders_user ON orders(user_id)")
    yield d
    d.close()


def _fetch(cursor):
    return list(cursor.fetchall())


# --- Column structure ---

def test_explain_analyze_has_expected_columns(db):
    rows = _fetch(db.execute("EXPLAIN ANALYZE SELECT * FROM users"))
    assert len(rows) > 0
    assert "detail" in rows[0]
    assert "actual_rows" in rows[0]
    assert "actual_time_ms" in rows[0]
    assert "id" in rows[0]
    assert "parent" in rows[0]


def test_explain_analyze_detail_has_annotation(db):
    rows = _fetch(db.execute("EXPLAIN ANALYZE SELECT * FROM users"))
    detail = rows[0]["detail"]
    assert "actual rows=" in detail
    assert "time=" in detail
    assert "ms)" in detail


# --- Actual row counts ---

def test_explain_analyze_full_table_scan_row_count(db):
    rows = _fetch(db.execute("EXPLAIN ANALYZE SELECT * FROM users"))
    assert rows[0]["actual_rows"] == 5


def test_explain_analyze_filtered_row_count(db):
    rows = _fetch(db.execute("EXPLAIN ANALYZE SELECT * FROM users WHERE age > 23"))
    assert rows[0]["actual_rows"] == 2


def test_explain_analyze_no_rows(db):
    rows = _fetch(db.execute("EXPLAIN ANALYZE SELECT * FROM users WHERE id = 999"))
    assert rows[0]["actual_rows"] == 0


def test_explain_analyze_all_orders(db):
    rows = _fetch(db.execute("EXPLAIN ANALYZE SELECT * FROM orders"))
    assert rows[0]["actual_rows"] == 10


# --- Timing ---

def test_explain_analyze_time_is_non_negative(db):
    rows = _fetch(db.execute("EXPLAIN ANALYZE SELECT * FROM users"))
    assert rows[0]["actual_time_ms"] >= 0


def test_explain_analyze_time_is_numeric(db):
    rows = _fetch(db.execute("EXPLAIN ANALYZE SELECT * FROM users"))
    t = rows[0]["actual_time_ms"]
    assert isinstance(t, (int, float))


# --- Plan content still present ---

def test_explain_analyze_scan_detail(db):
    rows = _fetch(db.execute("EXPLAIN ANALYZE SELECT * FROM users"))
    assert "SCAN TABLE users" in rows[0]["detail"]


def test_explain_analyze_index_detail(db):
    rows = _fetch(db.execute(
        "EXPLAIN ANALYZE SELECT * FROM orders WHERE user_id = 1"
    ))
    root = rows[0]
    assert "idx_orders_user" in root["detail"] or "SCAN TABLE orders" in root["detail"]


# --- vs EXPLAIN (no ANALYZE) ---

def test_explain_without_analyze_lacks_actual_columns(db):
    rows = _fetch(db.execute("EXPLAIN SELECT * FROM users"))
    assert "actual_rows" not in rows[0]
    assert "actual_time_ms" not in rows[0]


def test_explain_query_plan_unaffected(db):
    rows = _fetch(db.execute("EXPLAIN QUERY PLAN SELECT * FROM users"))
    assert "actual_rows" not in rows[0]


# --- DML ---

def test_explain_analyze_insert(db, tmp_path):
    d = Database(str(tmp_path / "ins.hdb"))
    d.execute("CREATE TABLE t (id INTEGER, v TEXT)")
    rows = _fetch(d.execute("EXPLAIN ANALYZE INSERT INTO t VALUES (1, 'a')"))
    assert rows[0]["actual_rows"] >= 0
    assert rows[0]["actual_time_ms"] >= 0
    # Verify the INSERT actually ran
    result = _fetch(d.execute("SELECT * FROM t"))
    assert len(result) == 1
    d.close()


def test_explain_analyze_update(db):
    rows = _fetch(db.execute(
        "EXPLAIN ANALYZE UPDATE users SET age = 99 WHERE id = 1"
    ))
    assert rows[0]["actual_time_ms"] >= 0
    # Verify UPDATE actually ran
    result = _fetch(db.execute("SELECT age FROM users WHERE id = 1"))
    assert result[0]["age"] == 99


# --- Multi-node plans ---

def test_explain_analyze_join_has_multiple_nodes(db):
    rows = _fetch(db.execute(
        "EXPLAIN ANALYZE SELECT u.name, o.amount "
        "FROM users u JOIN orders o ON u.id = o.user_id"
    ))
    assert len(rows) >= 2


def test_explain_analyze_join_root_has_timing(db):
    rows = _fetch(db.execute(
        "EXPLAIN ANALYZE SELECT u.name, o.amount "
        "FROM users u JOIN orders o ON u.id = o.user_id"
    ))
    assert rows[0]["actual_time_ms"] >= 0


def test_explain_analyze_cte(db):
    rows = _fetch(db.execute(
        "EXPLAIN ANALYZE WITH top AS (SELECT * FROM users WHERE age > 22) "
        "SELECT * FROM top"
    ))
    assert len(rows) > 0
    assert rows[0]["actual_rows"] >= 0
