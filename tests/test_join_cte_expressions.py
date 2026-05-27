"""Regression tests for the 4 bugs fixed in assessment: expressions in VALUES,
JOIN+GROUP BY, CTE+JOIN key conflict, and CTE in top-level JOIN."""
import pytest
from hyperion import Database
from hyperion.parser import parse
from hyperion.executor import execute, _rows_for_stmt


def sql(db, s):
    return execute(parse(s), db)


def rows(db, query):
    return _rows_for_stmt(parse(query), db)


def _orders_db():
    db = Database(":memory:")
    sql(db, "CREATE TABLE users (id INTEGER, name VARCHAR(50))")
    sql(db, "CREATE TABLE orders (id INTEGER, user_id INTEGER, amount REAL)")
    sql(db, "INSERT INTO users VALUES (1, 'Alice')")
    sql(db, "INSERT INTO users VALUES (2, 'Bob')")
    sql(db, "INSERT INTO orders VALUES (1, 1, 50.0)")
    sql(db, "INSERT INTO orders VALUES (2, 1, 30.0)")
    sql(db, "INSERT INTO orders VALUES (3, 2, 20.0)")
    return db


# ── Bug 1: Expressions in INSERT VALUES ──────────────────────────────────────

class TestExpressionsInValues:
    def test_arithmetic_expression(self):
        db = Database(":memory:")
        sql(db, "CREATE TABLE t (x INTEGER, y INTEGER)")
        sql(db, "INSERT INTO t VALUES (1 + 2, 3 * 4)")
        r = rows(db, "SELECT x, y FROM t")
        assert r[0]["x"] == 3
        assert r[0]["y"] == 12

    def test_string_concat_expression(self):
        db = Database(":memory:")
        sql(db, "CREATE TABLE t (label VARCHAR(100))")
        sql(db, "INSERT INTO t VALUES ('hello' || ' world')")
        r = rows(db, "SELECT label FROM t")
        assert r[0]["label"] == "hello world"

    def test_nested_arithmetic(self):
        db = Database(":memory:")
        sql(db, "CREATE TABLE t (v REAL)")
        sql(db, "INSERT INTO t VALUES (10.0 / 4.0)")
        r = rows(db, "SELECT v FROM t")
        assert r[0]["v"] == pytest.approx(2.5)

    def test_plain_literal_unaffected(self):
        db = Database(":memory:")
        sql(db, "CREATE TABLE t (x INTEGER, s VARCHAR(50))")
        sql(db, "INSERT INTO t VALUES (42, 'hello')")
        r = rows(db, "SELECT x, s FROM t")
        assert r[0]["x"] == 42
        assert r[0]["s"] == "hello"

    def test_string_with_percent_not_treated_as_expr(self):
        db = Database(":memory:")
        sql(db, "CREATE TABLE t (code VARCHAR(50))")
        sql(db, "INSERT INTO t VALUES ('50%OFF')")
        r = rows(db, "SELECT code FROM t")
        assert r[0]["code"] == "50%OFF"

    def test_date_string_not_evaluated(self):
        db = Database(":memory:")
        sql(db, "CREATE TABLE t (d VARCHAR(20))")
        sql(db, "INSERT INTO t VALUES ('2024-01-15')")
        r = rows(db, "SELECT d FROM t")
        assert r[0]["d"] == "2024-01-15"

    def test_string_with_spaces_not_evaluated(self):
        db = Database(":memory:")
        sql(db, "CREATE TABLE t (v VARCHAR(100))")
        sql(db, "INSERT INTO t VALUES ('foo bar baz')")
        r = rows(db, "SELECT v FROM t")
        assert r[0]["v"] == "foo bar baz"

    def test_escaped_quote_in_string(self):
        db = Database(":memory:")
        sql(db, "CREATE TABLE t (v VARCHAR(100))")
        sql(db, "INSERT INTO t VALUES ('it''s fine')")
        r = rows(db, "SELECT v FROM t")
        assert r[0]["v"] == "it's fine"

    def test_true_false_constants(self):
        db = Database(":memory:")
        sql(db, "CREATE TABLE t (a INTEGER, b INTEGER)")
        sql(db, "INSERT INTO t VALUES (TRUE, FALSE)")
        r = rows(db, "SELECT a, b FROM t")
        assert r[0]["a"] == 1
        assert r[0]["b"] == 0

    def test_multi_row_with_expressions(self):
        db = Database(":memory:")
        sql(db, "CREATE TABLE t (x INTEGER)")
        sql(db, "INSERT INTO t VALUES (1 + 0), (1 + 1), (1 + 2)")
        r = rows(db, "SELECT x FROM t ORDER BY x ASC")
        assert [row["x"] for row in r] == [1, 2, 3]


# ── Bug 2: JOIN + GROUP BY ────────────────────────────────────────────────────

class TestJoinGroupBy:
    def setup_method(self):
        self.db = _orders_db()

    def test_join_group_by_count(self):
        # Use sql() and check string output — rows() returns raw names without aliases
        result = sql(self.db,
            "SELECT name, COUNT(*) FROM users JOIN orders ON users.id = orders.user_id GROUP BY name ORDER BY name ASC")
        assert "Alice" in result
        assert "Bob" in result
        assert "2" in result  # Alice has 2 orders
        assert "1" in result  # Bob has 1 order

    def test_join_group_by_sum(self):
        result = sql(self.db,
            "SELECT name, SUM(amount) FROM users JOIN orders ON users.id = orders.user_id GROUP BY name ORDER BY name ASC")
        assert "Alice" in result
        assert "80.0" in result
        assert "Bob" in result
        assert "20.0" in result

    def test_join_group_by_correct_row_count(self):
        result = sql(self.db,
            "SELECT name, COUNT(*) FROM users JOIN orders ON users.id = orders.user_id GROUP BY name")
        assert "(2 rows)" in result  # Alice and Bob — not 3 or 1

    def test_join_group_by_sum_values_correct(self):
        """Verify Alice=80 (not 100), Bob=20."""
        result = sql(self.db,
            "SELECT name, SUM(amount) FROM users JOIN orders ON users.id = orders.user_id GROUP BY name ORDER BY name ASC")
        # 100.0 would indicate all rows in one bucket (bug)
        assert "100.0" not in result
        assert "80.0" in result

    def test_join_group_by_having(self):
        result = sql(self.db,
            "SELECT name, SUM(amount) FROM users JOIN orders ON users.id = orders.user_id GROUP BY name HAVING SUM(amount) > 50")
        assert "Alice" in result
        assert "Bob" not in result
        assert "(1 row)" in result


# ── Bug 3: CTE + JOIN column key conflict ─────────────────────────────────────

class TestCteJoinKeyConflict:
    def setup_method(self):
        self.db = _orders_db()

    def test_cte_with_join_projects_bare_names(self):
        result = sql(self.db, """
            WITH j AS (SELECT users.name, orders.amount
                       FROM users JOIN orders ON users.id = orders.user_id)
            SELECT name, amount FROM j ORDER BY amount DESC
        """)
        assert "Alice" in result
        assert "50.0" in result
        assert "(3 rows)" in result

    def test_cte_join_then_group_by(self):
        result = sql(self.db, """
            WITH j AS (SELECT users.name, orders.amount
                       FROM users JOIN orders ON users.id = orders.user_id)
            SELECT name, SUM(amount) FROM j GROUP BY name ORDER BY name ASC
        """)
        assert "Alice" in result
        assert "80.0" in result
        assert "Bob" in result
        assert "20.0" in result
        assert "(2 rows)" in result

    def test_cte_join_where_on_bare_name(self):
        result = sql(self.db, """
            WITH j AS (SELECT users.name, orders.amount
                       FROM users JOIN orders ON users.id = orders.user_id)
            SELECT name, amount FROM j WHERE amount > 25 ORDER BY amount ASC
        """)
        assert "30.0" in result
        assert "50.0" in result
        assert "20.0" not in result
        assert "(2 rows)" in result


# ── Bug 4: CTE in top-level JOIN ─────────────────────────────────────────────

class TestCteInTopLevelJoin:
    def setup_method(self):
        self.db = _orders_db()

    def test_join_with_cte_on_right(self):
        result = sql(self.db, """
            WITH totals AS (SELECT user_id, SUM(amount) as total FROM orders GROUP BY user_id)
            SELECT u.name, t.total FROM users u JOIN totals t ON u.id = t.user_id ORDER BY t.total DESC
        """)
        assert "Alice" in result
        assert "80.0" in result
        assert "Bob" in result
        assert "20.0" in result
        assert "(2 rows)" in result

    def test_cte_alias_resolved_in_join(self):
        """CTE column alias must be accessible via table.alias in the outer JOIN."""
        result = sql(self.db, """
            WITH top AS (SELECT user_id, SUM(amount) as total FROM orders GROUP BY user_id)
            SELECT u.name, t.total FROM users u JOIN top t ON u.id = t.user_id ORDER BY t.total DESC
        """)
        # Correct values — Alice 80, Bob 20
        assert "80.0" in result
        assert "20.0" in result

    def test_join_non_cte_tables_unaffected(self):
        """Regular two-table JOINs must still work after the CTE fix."""
        result = sql(self.db,
            "SELECT users.name, orders.amount FROM users JOIN orders ON users.id = orders.user_id ORDER BY orders.amount DESC")
        assert "Alice" in result
        assert "50.0" in result
        assert "(3 rows)" in result
