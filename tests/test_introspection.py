"""Tests for _hyperion_master, PRAGMA integrity_check, and EXPLAIN QUERY PLAN."""
import pytest
from hyperion import Database
from hyperion.executor import execute, _rows_for_stmt
from hyperion.parser import parse


def sql(db, s):
    return execute(parse(s), db)


def rows(db, query):
    return _rows_for_stmt(parse(query), db)


def _setup_db():
    db = Database(":memory:")
    sql(db, "CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR(50) NOT NULL)")
    sql(db, "CREATE TABLE orders (id INTEGER, user_id INTEGER, amount REAL)")
    sql(db, "CREATE INDEX idx_users_name ON users (name)")
    sql(db, "CREATE INDEX idx_orders_user ON orders (user_id)")
    sql(db, "INSERT INTO users VALUES (1, 'Alice')")
    sql(db, "INSERT INTO users VALUES (2, 'Bob')")
    sql(db, "INSERT INTO orders VALUES (1, 1, 50.0)")
    sql(db, "INSERT INTO orders VALUES (2, 2, 20.0)")
    return db


# ── _hyperion_master ──────────────────────────────────────────────────────────

class TestHyperionMaster:
    def test_tables_appear(self):
        db = _setup_db()
        r = rows(db, "SELECT name FROM _hyperion_master WHERE type = 'table' ORDER BY name")
        names = [row["name"] for row in r]
        assert "users" in names
        assert "orders" in names

    def test_indexes_appear(self):
        db = _setup_db()
        r = rows(db, "SELECT name FROM _hyperion_master WHERE type = 'index' ORDER BY name")
        names = [row["name"] for row in r]
        assert "idx_users_name" in names
        assert "idx_orders_user" in names

    def test_view_appears(self):
        db = Database(":memory:")
        sql(db, "CREATE TABLE t (id INTEGER, v INTEGER)")
        sql(db, "CREATE VIEW vw AS SELECT id, v FROM t WHERE v > 0")
        r = rows(db, "SELECT name, type FROM _hyperion_master WHERE type = 'view'")
        assert len(r) == 1
        assert r[0]["name"] == "vw"
        assert r[0]["type"] == "view"

    def test_sql_column_reconstructed(self):
        db = Database(":memory:")
        sql(db, "CREATE TABLE t (x INTEGER NOT NULL, y REAL)")
        r = rows(db, "SELECT sql FROM _hyperion_master WHERE name = 't'")
        assert len(r) == 1
        reconstructed = r[0]["sql"]
        assert "CREATE TABLE t" in reconstructed
        assert "x INTEGER" in reconstructed
        assert "y REAL" in reconstructed
        assert "NOT NULL" in reconstructed

    def test_tbl_name_for_index(self):
        db = _setup_db()
        r = rows(db, "SELECT tbl_name FROM _hyperion_master WHERE name = 'idx_users_name'")
        assert r[0]["tbl_name"] == "users"

    def test_rootpage_nonzero_for_tables(self):
        db = _setup_db()
        r = rows(db, "SELECT rootpage FROM _hyperion_master WHERE type = 'table' AND name = 'users'")
        assert r[0]["rootpage"] > 0

    def test_select_with_where_filter(self):
        db = _setup_db()
        r = rows(db, "SELECT name FROM _hyperion_master WHERE type = 'table' AND name = 'users'")
        assert len(r) == 1
        assert r[0]["name"] == "users"

    def test_select_star(self):
        db = _setup_db()
        r = rows(db, "SELECT * FROM _hyperion_master")
        assert len(r) > 0
        assert "type" in r[0]
        assert "name" in r[0]
        assert "sql" in r[0]

    def test_all_columns_present(self):
        db = _setup_db()
        r = rows(db, "SELECT * FROM _hyperion_master WHERE type = 'table' LIMIT 1")
        row = r[0]
        for col in ("type", "name", "tbl_name", "rootpage", "sql"):
            assert col in row

    def test_sql_index_statement(self):
        db = _setup_db()
        r = rows(db, "SELECT sql FROM _hyperion_master WHERE name = 'idx_users_name'")
        assert "CREATE INDEX idx_users_name ON users" in r[0]["sql"]
        assert "name" in r[0]["sql"]

    def test_view_sql_contains_original_query(self):
        db = Database(":memory:")
        sql(db, "CREATE TABLE t (id INTEGER)")
        sql(db, "CREATE VIEW vw AS SELECT id FROM t")
        r = rows(db, "SELECT sql FROM _hyperion_master WHERE name = 'vw'")
        assert "SELECT id FROM t" in r[0]["sql"]


# ── PRAGMA integrity_check ────────────────────────────────────────────────────

class TestIntegrityCheck:
    def test_empty_db_ok(self):
        db = Database(":memory:")
        result = sql(db, "PRAGMA integrity_check")
        assert "ok" in result

    def test_populated_db_ok(self):
        db = _setup_db()
        result = sql(db, "PRAGMA integrity_check")
        assert "ok" in result

    def test_returns_ok_string(self):
        db = _setup_db()
        result = sql(db, "PRAGMA integrity_check")
        assert "ok" in result

    def test_db_with_multiple_tables_ok(self):
        db = Database(":memory:")
        for i in range(5):
            sql(db, f"CREATE TABLE t{i} (id INTEGER, val TEXT)")
            for j in range(10):
                sql(db, f"INSERT INTO t{i} VALUES ({j}, 'row{j}')")
        result = sql(db, "PRAGMA integrity_check")
        assert "ok" in result

    def test_db_with_views_ok(self):
        db = Database(":memory:")
        sql(db, "CREATE TABLE t (id INTEGER)")
        sql(db, "CREATE VIEW v AS SELECT id FROM t")
        result = sql(db, "PRAGMA integrity_check")
        assert "ok" in result


# ── EXPLAIN / EXPLAIN QUERY PLAN ──────────────────────────────────────────────

class TestExplain:
    def test_explain_query_plan_scan(self):
        db = _setup_db()
        result = sql(db, "EXPLAIN QUERY PLAN SELECT * FROM orders")
        assert "SCAN TABLE orders" in result

    def test_explain_query_plan_search_with_index(self):
        db = _setup_db()
        result = sql(db, "EXPLAIN QUERY PLAN SELECT * FROM users WHERE id = 1")
        assert "SEARCH TABLE users" in result
        assert "INDEX" in result

    def test_explain_query_plan_join(self):
        db = _setup_db()
        result = sql(db, "EXPLAIN QUERY PLAN SELECT u.name FROM users u JOIN orders o ON u.id = o.user_id")
        assert "SCAN TABLE" in result or "SEARCH TABLE" in result
        # Both tables should appear
        assert "users" in result
        assert "orders" in result

    def test_explain_query_plan_has_id_column(self):
        db = _setup_db()
        result = sql(db, "EXPLAIN QUERY PLAN SELECT * FROM users")
        assert "id" in result
        assert "detail" in result

    def test_explain_without_query_plan(self):
        db = _setup_db()
        result = sql(db, "EXPLAIN SELECT name FROM users")
        assert "SCAN TABLE users" in result or "SEARCH TABLE users" in result

    def test_explain_group_by_noted(self):
        db = _setup_db()
        result = sql(db, "EXPLAIN QUERY PLAN SELECT user_id, SUM(amount) FROM orders GROUP BY user_id")
        assert "GROUP BY" in result

    def test_explain_cte_materialise(self):
        db = _setup_db()
        result = sql(db, """
            EXPLAIN QUERY PLAN
            WITH totals AS (SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id)
            SELECT * FROM totals
        """)
        assert "MATERIALIZE CTE totals" in result

    def test_explain_nofrom(self):
        db = Database(":memory:")
        result = sql(db, "EXPLAIN QUERY PLAN SELECT 1 + 1")
        assert "MATERIALIZE constant row" in result

    def test_explain_rows_have_required_columns(self):
        db = _setup_db()
        result = sql(db, "EXPLAIN QUERY PLAN SELECT * FROM users")
        for col in ("id", "parent", "notused", "detail"):
            assert col in result
