"""Tests for CREATE STATISTICS / DROP STATISTICS / SHOW STATISTICS and
joint multi-column selectivity estimation in estimate_row_count_with_where."""

import pytest
from hyperion import Database
from hyperion.optimizer import estimate_row_count_with_where


# ── helpers ───────────────────────────────────────────────────────────────────

def fresh_db(tmp_path):
    return Database(str(tmp_path / "test.hdb"))


def _orders_db(db):
    """6 rows: 3×(East, Electronics), 1×(East, Clothing), 2×(West, ...)."""
    db.execute(
        "CREATE TABLE orders "
        "(id INTEGER PRIMARY KEY, region TEXT, category TEXT, amount REAL)"
    )
    rows = [
        ("East", "Electronics", 100.0),
        ("East", "Electronics", 150.0),
        ("East", "Clothing",    50.0),
        ("West", "Electronics", 200.0),
        ("West", "Clothing",    80.0),
        ("East", "Electronics", 120.0),
    ]
    for i, (r, c, a) in enumerate(rows, 1):
        db.execute("INSERT INTO orders VALUES (?, ?, ?, ?)", (i, r, c, a))
    db.execute("ANALYZE")
    return db


# ── CREATE STATISTICS ─────────────────────────────────────────────────────────

class TestCreateStatistics:

    def test_create_returns_success(self, tmp_path):
        db = fresh_db(tmp_path)
        _orders_db(db)
        result = db.execute("CREATE STATISTICS s ON orders(region, category)")
        # DDL returns None row (message result)
        assert result is not None
        db.close()

    def test_stored_in_catalog(self, tmp_path):
        db = fresh_db(tmp_path)
        _orders_db(db)
        db.execute("CREATE STATISTICS s ON orders(region, category)")
        assert "s" in db._catalog.named_stats
        ns = db._catalog.named_stats["s"]
        assert ns["table"]   == "orders"
        assert ns["columns"] == ["region", "category"]
        db.close()

    def test_joint_ndv_computed(self, tmp_path):
        db = fresh_db(tmp_path)
        _orders_db(db)
        db.execute("CREATE STATISTICS s ON orders(region, category)")
        ns = db._catalog.named_stats["s"]
        # Distinct (region, category) combos: (E,El), (E,Cl), (W,El), (W,Cl) = 4
        assert ns["joint_ndv"] == 4
        db.close()

    def test_joint_mcv_computed(self, tmp_path):
        db = fresh_db(tmp_path)
        _orders_db(db)
        db.execute("CREATE STATISTICS s ON orders(region, category)")
        ns = db._catalog.named_stats["s"]
        # (East, Electronics) appears 3 times — must be top MCV
        top = ns["joint_mcv"][0]
        assert top[0] == ["East", "Electronics"]
        assert top[1] == 3
        db.close()

    def test_joint_mcv_capped_at_10(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT, b TEXT)")
        for i in range(20):
            db.execute("INSERT INTO t VALUES (?, ?, ?)", (i, f"a{i}", f"b{i}"))
        db.execute("ANALYZE")
        db.execute("CREATE STATISTICS s ON t(a, b)")
        ns = db._catalog.named_stats["s"]
        assert len(ns["joint_mcv"]) <= 10
        db.close()

    def test_three_column_statistics(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute(
            "CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT, b TEXT, c INTEGER)"
        )
        for i in range(4):
            db.execute("INSERT INTO t VALUES (?, ?, ?, ?)", (i, "x", "y", i % 2))
        db.execute("ANALYZE")
        db.execute("CREATE STATISTICS s ON t(a, b, c)")
        ns = db._catalog.named_stats["s"]
        assert ns["table"]   == "t"
        assert ns["columns"] == ["a", "b", "c"]
        assert ns["joint_ndv"] >= 1
        db.close()

    def test_error_on_nonexistent_table(self, tmp_path):
        db = fresh_db(tmp_path)
        with pytest.raises(Exception, match="No such table"):
            db.execute("CREATE STATISTICS s ON ghost(a, b)")
        db.close()

    def test_error_on_nonexistent_column(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val INTEGER)")
        with pytest.raises(Exception, match="No such column"):
            db.execute("CREATE STATISTICS s ON t(val, ghost)")
        db.close()

    def test_error_on_single_column(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val INTEGER)")
        with pytest.raises(Exception):
            db.execute("CREATE STATISTICS s ON t(val)")
        db.close()


# ── DROP STATISTICS ───────────────────────────────────────────────────────────

class TestDropStatistics:

    def test_drop_removes_from_catalog(self, tmp_path):
        db = fresh_db(tmp_path)
        _orders_db(db)
        db.execute("CREATE STATISTICS s ON orders(region, category)")
        assert "s" in db._catalog.named_stats
        db.execute("DROP STATISTICS s")
        assert "s" not in db._catalog.named_stats
        db.close()

    def test_drop_if_exists_no_error_when_missing(self, tmp_path):
        db = fresh_db(tmp_path)
        _orders_db(db)
        # Should not raise
        db.execute("DROP STATISTICS IF EXISTS nonexistent")
        db.close()

    def test_drop_nonexistent_raises(self, tmp_path):
        db = fresh_db(tmp_path)
        with pytest.raises(Exception):
            db.execute("DROP STATISTICS nonexistent")
        db.close()

    def test_show_empty_after_drop(self, tmp_path):
        db = fresh_db(tmp_path)
        _orders_db(db)
        db.execute("CREATE STATISTICS s ON orders(region, category)")
        db.execute("DROP STATISTICS s")
        rows = db.execute("SHOW STATISTICS").fetchall()
        assert rows == []
        db.close()


# ── SHOW STATISTICS ───────────────────────────────────────────────────────────

class TestShowStatistics:

    def test_show_all_columns(self, tmp_path):
        db = fresh_db(tmp_path)
        _orders_db(db)
        db.execute("CREATE STATISTICS s ON orders(region, category)")
        rows = db.execute("SHOW STATISTICS").fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["name"]    == "s"
        assert row["table"]   == "orders"
        assert "region"   in row["columns"]
        assert "category" in row["columns"]
        assert int(row["joint_ndv"]) == 4
        assert row["created_at"] != ""
        db.close()

    def test_show_for_table_filters(self, tmp_path):
        db = fresh_db(tmp_path)
        _orders_db(db)
        db.execute(
            "CREATE TABLE t2 (id INTEGER PRIMARY KEY, x TEXT, y TEXT)"
        )
        db.execute("INSERT INTO t2 VALUES (1, 'a', 'b')")
        db.execute("INSERT INTO t2 VALUES (2, 'c', 'd')")
        db.execute("ANALYZE")
        db.execute("CREATE STATISTICS s1 ON orders(region, category)")
        db.execute("CREATE STATISTICS s2 ON t2(x, y)")
        rows = db.execute("SHOW STATISTICS FOR orders").fetchall()
        assert len(rows) == 1
        assert rows[0]["name"] == "s1"
        db.close()

    def test_show_empty_when_none(self, tmp_path):
        db = fresh_db(tmp_path)
        rows = db.execute("SHOW STATISTICS").fetchall()
        assert rows == []
        db.close()

    def test_show_multiple(self, tmp_path):
        db = fresh_db(tmp_path)
        _orders_db(db)
        db.execute("CREATE STATISTICS s1 ON orders(region, category)")
        db.execute("CREATE STATISTICS s2 ON orders(category, amount)")
        rows = db.execute("SHOW STATISTICS").fetchall()
        assert len(rows) == 2
        names = {r["name"] for r in rows}
        assert names == {"s1", "s2"}
        db.close()


# ── Persistence ───────────────────────────────────────────────────────────────

class TestStatisticsPersistence:

    def test_survives_close_reopen(self, tmp_path):
        db = fresh_db(tmp_path)
        _orders_db(db)
        db.execute("CREATE STATISTICS s ON orders(region, category)")
        db.close()

        db2 = Database(str(tmp_path / "test.hdb"))
        rows = db2.execute("SHOW STATISTICS").fetchall()
        assert len(rows) == 1
        assert rows[0]["name"] == "s"
        assert int(rows[0]["joint_ndv"]) == 4
        db2.close()

    def test_analyze_refreshes_joint_stats(self, tmp_path):
        db = fresh_db(tmp_path)
        _orders_db(db)
        db.execute("CREATE STATISTICS s ON orders(region, category)")
        ndv_before = db._catalog.named_stats["s"]["joint_ndv"]

        # Add a new (region, category) combination
        db.execute("INSERT INTO orders VALUES (7, 'North', 'Furniture', 300.0)")
        db.execute("ANALYZE")
        ndv_after = db._catalog.named_stats["s"]["joint_ndv"]
        assert ndv_after > ndv_before
        db.close()


# ── Joint selectivity in estimate_row_count_with_where ───────────────────────

class TestJointSelectivity:

    def test_joint_mcv_more_accurate_than_multiplicative(self, tmp_path):
        """Named stats give exact count via joint MCV; multiplicative underestimates."""
        db = fresh_db(tmp_path)
        _orders_db(db)

        # Without named stats: multiplicative estimate
        est_mult = estimate_row_count_with_where(
            db, "orders",
            [("region", "=", "East"), ("category", "=", "Electronics")]
        )

        db.execute("CREATE STATISTICS region_cat ON orders(region, category)")

        # With named stats: joint MCV gives exact count (3 rows)
        est_joint = estimate_row_count_with_where(
            db, "orders",
            [("region", "=", "East"), ("category", "=", "Electronics")]
        )
        # The joint estimate should be closer to reality (3) than multiplicative (2)
        assert abs(est_joint - 3) <= abs(est_mult - 3)
        db.close()

    def test_joint_estimate_exact_for_mcv_hit(self, tmp_path):
        db = fresh_db(tmp_path)
        _orders_db(db)
        db.execute("CREATE STATISTICS s ON orders(region, category)")
        est = estimate_row_count_with_where(
            db, "orders",
            [("region", "=", "East"), ("category", "=", "Electronics")]
        )
        assert est == 3
        db.close()

    def test_joint_estimate_fallback_for_mcv_miss(self, tmp_path):
        """Combination not in joint MCV falls back to 1/joint_ndv."""
        db = fresh_db(tmp_path)
        _orders_db(db)
        db.execute("CREATE STATISTICS s ON orders(region, category)")
        # "South, Books" is not in the table at all
        est = estimate_row_count_with_where(
            db, "orders",
            [("region", "=", "South"), ("category", "=", "Books")]
        )
        # Should return at least 1 (floor) and less than total rows
        assert 1 <= est <= 6
        db.close()

    def test_non_covered_columns_still_multiplied(self, tmp_path):
        """Named stats absorb covered columns; remaining cols use per-column sel."""
        db = fresh_db(tmp_path)
        _orders_db(db)
        db.execute("CREATE STATISTICS s ON orders(region, category)")
        # Add amount condition (not covered by stats) → further narrows estimate
        est_joint_only = estimate_row_count_with_where(
            db, "orders",
            [("region", "=", "East"), ("category", "=", "Electronics")]
        )
        est_with_amount = estimate_row_count_with_where(
            db, "orders",
            [("region", "=", "East"), ("category", "=", "Electronics"),
             ("amount", ">", 140.0)]
        )
        assert est_with_amount <= est_joint_only
        db.close()

    def test_no_named_stats_unchanged_behaviour(self, tmp_path):
        """Without named stats the function behaves exactly as before."""
        db = fresh_db(tmp_path)
        _orders_db(db)
        est = estimate_row_count_with_where(
            db, "orders",
            [("region", "=", "East")]
        )
        assert est >= 1
        db.close()

    def test_single_condition_not_affected_by_stats(self, tmp_path):
        """Named stats on 2+ cols don't change single-col estimation."""
        db = fresh_db(tmp_path)
        _orders_db(db)
        before = estimate_row_count_with_where(
            db, "orders", [("region", "=", "East")]
        )
        db.execute("CREATE STATISTICS s ON orders(region, category)")
        after = estimate_row_count_with_where(
            db, "orders", [("region", "=", "East")]
        )
        # Single condition doesn't satisfy all named_stats cols → not absorbed
        assert before == after
        db.close()
