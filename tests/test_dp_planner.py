"""Tests for the bitmask DP join order planner: plan correctness, plan cache,
stats tracking, SHOW DP STATS, and cache invalidation."""

import pytest
from hyperion import Database
from hyperion.optimizer import (
    _dp_plan_inner,
    _dp_plan_cache,
    _dp_stats,
    get_dp_stats,
    invalidate_dp_cache,
    optimize_join,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def fresh_db(tmp_path):
    return Database(str(tmp_path / "test.hdb"))


def _reset_dp():
    """Clear plan cache and reset stats between tests."""
    _dp_plan_cache.clear()
    for k in _dp_stats:
        _dp_stats[k] = 0


def _setup_three_table_db(db):
    """Create users → orders → items with FK indexes and known row counts."""
    db.execute("CREATE TABLE users  (id INTEGER PRIMARY KEY, name TEXT)")
    db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER, total REAL)")
    db.execute("CREATE TABLE items  (id INTEGER PRIMARY KEY, order_id INTEGER, qty INTEGER)")
    db.execute("CREATE INDEX idx_orders_user ON orders(user_id)")
    db.execute("CREATE INDEX idx_items_order ON items(order_id)")
    # 3 users, 10 orders, 30 items — ANALYZE so DP has row count stats
    for i in range(1, 4):
        db.execute("INSERT INTO users VALUES (?, ?)", (i, f"u{i}"))
    for i in range(1, 11):
        db.execute("INSERT INTO orders VALUES (?, ?, ?)", (i, (i % 3) + 1, float(i * 10)))
    for i in range(1, 31):
        db.execute("INSERT INTO items VALUES (?, ?, ?)", (i, (i % 10) + 1, i))
    db.execute("ANALYZE")


# ── _dp_plan_inner unit tests ─────────────────────────────────────────────────

class TestDpPlanInner:

    def test_two_tables_returns_two_element_list(self, tmp_path):
        _reset_dp()
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE a (id INTEGER PRIMARY KEY)")
        db.execute("CREATE TABLE b (id INTEGER PRIMARY KEY, a_id INTEGER)")
        db.execute("CREATE INDEX idx_b_a ON b(a_id)")
        for i in range(1, 4):
            db.execute("INSERT INTO a VALUES (?)", (i,))
        for i in range(1, 21):
            db.execute("INSERT INTO b VALUES (?, ?)", (i, (i % 3) + 1))
        db.execute("ANALYZE")

        tables = [("a", None), ("b", None)]
        adj = {(0, 1): ("a.id", "a_id"), (1, 0): ("b.a_id", "id")}
        order = _dp_plan_inner(tables, adj, db)
        assert order is not None
        assert len(order) == 2
        assert sorted(order) == [0, 1]
        db.close()

    def test_three_tables_returns_three_element_list(self, tmp_path):
        _reset_dp()
        db = fresh_db(tmp_path)
        _setup_three_table_db(db)

        tables = [("users", None), ("orders", None), ("items", None)]
        adj = {
            (0, 1): ("users.id",   "user_id"),
            (1, 0): ("orders.user_id", "id"),
            (1, 2): ("orders.id",  "order_id"),
            (2, 1): ("items.order_id", "id"),
        }
        order = _dp_plan_inner(tables, adj, db)
        assert order is not None
        assert len(order) == 3
        assert sorted(order) == [0, 1, 2]
        db.close()

    def test_small_table_placed_first(self, tmp_path):
        """With row counts available, the small table (users=3) should be first."""
        _reset_dp()
        db = fresh_db(tmp_path)
        _setup_three_table_db(db)

        tables = [("items", None), ("users", None), ("orders", None)]
        adj = {
            (0, 2): ("items.order_id", "id"),
            (2, 0): ("orders.id",      "order_id"),
            (1, 2): ("users.id",       "user_id"),
            (2, 1): ("orders.user_id", "id"),
        }
        order = _dp_plan_inner(tables, adj, db)
        assert order is not None
        # users (idx 1 in this tables list) should come early due to small row count
        assert order[0] == 1  # users first
        db.close()

    def test_empty_adj_still_returns_valid_order(self, tmp_path):
        """No join conditions → cross join is still planned (all tables returned)."""
        _reset_dp()
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE a (id INTEGER PRIMARY KEY)")
        db.execute("CREATE TABLE b (id INTEGER PRIMARY KEY)")
        for i in range(3):
            db.execute("INSERT INTO a VALUES (?)", (i,))
            db.execute("INSERT INTO b VALUES (?)", (i,))
        db.execute("ANALYZE")

        tables = [("a", None), ("b", None)]
        adj: dict = {}
        order = _dp_plan_inner(tables, adj, db)
        assert order is not None
        assert sorted(order) == [0, 1]
        db.close()

    def test_subsets_evaluated_incremented(self, tmp_path):
        _reset_dp()
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE a (id INTEGER PRIMARY KEY)")
        db.execute("CREATE TABLE b (id INTEGER PRIMARY KEY)")
        for i in range(2):
            db.execute("INSERT INTO a VALUES (?)", (i,))
            db.execute("INSERT INTO b VALUES (?)", (i,))
        db.execute("ANALYZE")

        tables = [("a", None), ("b", None)]
        adj = {(0, 1): ("a.id", "id"), (1, 0): ("b.id", "id")}
        _dp_plan_inner(tables, adj, db)
        # 2 tables → 2^2 - 1 = 3 subsets evaluated
        assert _dp_stats["subsets_evaluated"] == 3
        db.close()


# ── Plan cache tests ──────────────────────────────────────────────────────────

class TestPlanCache:

    def _run_join_query(self, db):
        """Run a 3-table join to trigger optimize_join."""
        return db.execute(
            "SELECT u.name, o.total FROM users u "
            "JOIN orders o ON u.id = o.user_id "
            "JOIN items i ON o.id = i.order_id"
        ).fetchall()

    def test_plan_cache_populated_after_query(self, tmp_path):
        _reset_dp()
        db = fresh_db(tmp_path)
        _setup_three_table_db(db)
        self._run_join_query(db)
        assert len(_dp_plan_cache) >= 1
        db.close()

    def test_plan_cache_hit_on_second_query(self, tmp_path):
        _reset_dp()
        db = fresh_db(tmp_path)
        _setup_three_table_db(db)
        self._run_join_query(db)
        hits_before = _dp_stats["plan_cache_hits"]
        self._run_join_query(db)
        assert _dp_stats["plan_cache_hits"] > hits_before
        db.close()

    def test_queries_planned_increments(self, tmp_path):
        _reset_dp()
        db = fresh_db(tmp_path)
        _setup_three_table_db(db)
        self._run_join_query(db)
        assert _dp_stats["queries_planned"] >= 1
        db.close()

    def test_plan_cache_miss_on_first_query(self, tmp_path):
        _reset_dp()
        db = fresh_db(tmp_path)
        _setup_three_table_db(db)
        self._run_join_query(db)
        assert _dp_stats["plan_cache_misses"] >= 1
        db.close()

    def test_invalidate_dp_cache_clears_entry(self, tmp_path):
        _reset_dp()
        db = fresh_db(tmp_path)
        _setup_three_table_db(db)
        self._run_join_query(db)
        assert len(_dp_plan_cache) >= 1
        invalidate_dp_cache("orders")
        # All cache entries containing 'orders' should be gone
        for k in _dp_plan_cache:
            assert "orders" not in k
        db.close()

    def test_invalidate_unknown_table_no_error(self):
        _reset_dp()
        invalidate_dp_cache("nonexistent_table")  # must not raise

    def test_invalidate_called_on_insert(self, tmp_path):
        _reset_dp()
        db = fresh_db(tmp_path)
        _setup_three_table_db(db)
        self._run_join_query(db)
        cache_before = len(_dp_plan_cache)
        # Insert into orders should invalidate any plan involving orders
        db.execute("INSERT INTO orders VALUES (99, 1, 500.0)")
        for k in _dp_plan_cache:
            assert "orders" not in k
        db.close()

    def test_invalidate_called_on_delete(self, tmp_path):
        _reset_dp()
        db = fresh_db(tmp_path)
        _setup_three_table_db(db)
        self._run_join_query(db)
        db.execute("DELETE FROM items WHERE id = 1")
        for k in _dp_plan_cache:
            assert "items" not in k
        db.close()


# ── get_dp_stats ──────────────────────────────────────────────────────────────

class TestGetDpStats:

    def test_get_dp_stats_returns_all_keys(self):
        s = get_dp_stats()
        assert "queries_planned"   in s
        assert "plan_cache_hits"   in s
        assert "plan_cache_misses" in s
        assert "subsets_evaluated" in s

    def test_get_dp_stats_returns_copy(self):
        s = get_dp_stats()
        s["queries_planned"] = 9999
        assert _dp_stats["queries_planned"] != 9999


# ── SHOW DP STATS SQL command ─────────────────────────────────────────────────

class TestShowDpStats:

    def test_show_dp_stats_returns_rows(self, tmp_path):
        db = fresh_db(tmp_path)
        rows = db.execute("SHOW DP STATS").fetchall()
        assert len(rows) == 4
        stats_keys = {r["stat"] for r in rows}
        assert "queries_planned"   in stats_keys
        assert "plan_cache_hits"   in stats_keys
        assert "plan_cache_misses" in stats_keys
        assert "subsets_evaluated" in stats_keys
        db.close()

    def test_show_dp_stats_values_are_strings(self, tmp_path):
        db = fresh_db(tmp_path)
        rows = db.execute("SHOW DP STATS").fetchall()
        for r in rows:
            assert isinstance(r["value"], str)
            int(r["value"])  # must be parseable as int
        db.close()

    def test_show_dp_stats_queries_planned_after_join(self, tmp_path):
        _reset_dp()
        db = fresh_db(tmp_path)
        _setup_three_table_db(db)
        db.execute(
            "SELECT u.name FROM users u "
            "JOIN orders o ON u.id = o.user_id"
        ).fetchall()
        rows = db.execute("SHOW DP STATS").fetchall()
        vals = {r["stat"]: int(r["value"]) for r in rows}
        assert vals["queries_planned"] >= 1
        db.close()


# ── Query correctness with DP planner ────────────────────────────────────────

class TestDpPlannerQueryCorrectness:

    def test_two_table_join_correct_result(self, tmp_path):
        _reset_dp()
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE a (id INTEGER PRIMARY KEY, val INTEGER)")
        db.execute("CREATE TABLE b (id INTEGER PRIMARY KEY, a_id INTEGER, name TEXT)")
        db.execute("INSERT INTO a VALUES (1, 10)")
        db.execute("INSERT INTO a VALUES (2, 20)")
        db.execute("INSERT INTO b VALUES (1, 1, 'foo')")
        db.execute("INSERT INTO b VALUES (2, 2, 'bar')")
        rows = db.execute(
            "SELECT a.val, b.name FROM a JOIN b ON a.id = b.a_id ORDER BY a.val"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["val"] == 10 and rows[0]["name"] == "foo"
        assert rows[1]["val"] == 20 and rows[1]["name"] == "bar"
        db.close()

    def test_three_table_join_correct_result(self, tmp_path):
        _reset_dp()
        db = fresh_db(tmp_path)
        _setup_three_table_db(db)
        rows = db.execute(
            "SELECT u.name, COUNT(*) AS cnt "
            "FROM users u "
            "JOIN orders o ON u.id = o.user_id "
            "JOIN items i ON o.id = i.order_id "
            "GROUP BY u.name ORDER BY u.name"
        ).fetchall()
        # Each user should have some items
        assert len(rows) == 3
        total = sum(r["cnt"] for r in rows)
        assert total == 30  # 30 items total
        db.close()

    def test_join_result_same_regardless_of_table_order_in_sql(self, tmp_path):
        _reset_dp()
        db = fresh_db(tmp_path)
        _setup_three_table_db(db)
        # Same join, tables listed in different order in SQL
        r1 = db.execute(
            "SELECT COUNT(*) AS n FROM users u "
            "JOIN orders o ON u.id = o.user_id "
            "JOIN items i ON o.id = i.order_id"
        ).fetchall()[0]["n"]
        r2 = db.execute(
            "SELECT COUNT(*) AS n FROM items i "
            "JOIN orders o ON i.order_id = o.id "
            "JOIN users u ON o.user_id = u.id"
        ).fetchall()[0]["n"]
        assert r1 == r2 == 30
        db.close()

    def test_dp_planner_does_not_break_outer_join(self, tmp_path):
        _reset_dp()
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE a (id INTEGER PRIMARY KEY, val INTEGER)")
        db.execute("CREATE TABLE b (id INTEGER PRIMARY KEY, a_id INTEGER)")
        db.execute("INSERT INTO a VALUES (1, 10)")
        db.execute("INSERT INTO a VALUES (2, 20)")
        db.execute("INSERT INTO b VALUES (1, 1)")
        # a LEFT JOIN b — a row with id=2 has no match in b
        rows = db.execute(
            "SELECT a.val FROM a LEFT JOIN b ON a.id = b.a_id ORDER BY a.val"
        ).fetchall()
        assert len(rows) == 2
        db.close()
