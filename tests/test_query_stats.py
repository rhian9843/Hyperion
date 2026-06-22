"""Tests for adaptive query statistics: access tracking, SHOW QUERY STATS,
and SHOW INDEX SUGGESTIONS (ported from milansql adaptive_stats.hpp)."""

import pytest
from hyperion import Database


# ── helpers ──────────────────────────────────────────────────────────────────

def fresh_db(tmp_path):
    return Database(str(tmp_path / "test.hdb"))


# ── stat recording ────────────────────────────────────────────────────────────

class TestStatRecording:

    def test_select_increments_table_count(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO t VALUES (1)")
        db.execute("SELECT * FROM t")
        stats = db._catalog.access_stats
        assert stats["tables"]["t"] >= 1
        db.close()

    def test_insert_increments_table_count(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        before = db._catalog.access_stats.get("tables", {}).get("t", 0)
        db.execute("INSERT INTO t VALUES (1)")
        after = db._catalog.access_stats["tables"]["t"]
        assert after == before + 1
        db.close()

    def test_update_increments_table_count(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        db.execute("INSERT INTO t VALUES (1, 'a')")
        before = db._catalog.access_stats["tables"]["t"]
        db.execute("UPDATE t SET v = 'b' WHERE id = 1")
        after = db._catalog.access_stats["tables"]["t"]
        assert after == before + 1
        db.close()

    def test_delete_increments_table_count(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO t VALUES (1)")
        before = db._catalog.access_stats["tables"]["t"]
        db.execute("DELETE FROM t WHERE id = 1")
        after = db._catalog.access_stats["tables"]["t"]
        assert after == before + 1
        db.close()

    def test_where_column_recorded_from_select(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
        db.execute("INSERT INTO t VALUES (1, 'Alice')")
        db.execute("SELECT * FROM t WHERE name = Alice")
        cols = db._catalog.access_stats["columns"].get("t", {})
        assert cols.get("name", 0) >= 1
        db.close()

    def test_where_column_recorded_from_update(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        db.execute("INSERT INTO t VALUES (1, 'x')")
        db.execute("UPDATE t SET v = 'y' WHERE id = 1")
        cols = db._catalog.access_stats["columns"].get("t", {})
        assert cols.get("id", 0) >= 1
        db.close()

    def test_where_column_recorded_from_delete(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO t VALUES (1)")
        db.execute("DELETE FROM t WHERE id = 1")
        cols = db._catalog.access_stats["columns"].get("t", {})
        assert cols.get("id", 0) >= 1
        db.close()

    def test_no_stat_for_virtual_table(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE real_t (id INTEGER PRIMARY KEY)")
        db.execute("SELECT name FROM _hyperion_master")
        stats = db._catalog.access_stats.get("tables", {})
        assert "_hyperion_master" not in stats
        db.close()

    def test_multiple_where_cols_all_recorded(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (a INTEGER PRIMARY KEY, b TEXT, c INTEGER)")
        db.execute("INSERT INTO t VALUES (1, 'x', 10)")
        db.execute("SELECT * FROM t WHERE b = x AND c = 10")
        cols = db._catalog.access_stats["columns"].get("t", {})
        assert "b" in cols
        assert "c" in cols
        db.close()

    def test_repeated_queries_accumulate(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        for i in range(5):
            db.execute("INSERT INTO t VALUES (?)", (i,))
        for i in range(5):
            db.execute("SELECT * FROM t WHERE id = ?", (i,))
        stats = db._catalog.access_stats
        assert stats["tables"]["t"] >= 10
        assert stats["columns"]["t"]["id"] >= 5
        db.close()


# ── SHOW QUERY STATS ──────────────────────────────────────────────────────────

class TestShowQueryStats:

    def test_empty_stats(self, tmp_path):
        db = fresh_db(tmp_path)
        rows = db.execute("SHOW QUERY STATS").fetchall()
        assert rows == []
        db.close()

    def test_columns_present(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO t VALUES (1)")
        db.execute("SELECT * FROM t WHERE id = 1")
        rows = db.execute("SHOW QUERY STATS").fetchall()
        assert len(rows) == 1
        r = rows[0]
        assert set(r.keys()) == {"table", "query_count", "top_filter_column",
                                  "top_filter_count"}
        db.close()

    def test_query_count_correct(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT)")
        db.execute("INSERT INTO orders VALUES (1, 'open')")
        db.execute("INSERT INTO orders VALUES (2, 'closed')")
        db.execute("SELECT * FROM orders")
        db.execute("SELECT * FROM orders WHERE status = open")
        rows = db.execute("SHOW QUERY STATS").fetchall()
        assert len(rows) == 1
        # 2 INSERTs + 2 SELECTs = 4
        assert rows[0]["query_count"] == 4
        db.close()

    def test_top_filter_column_is_most_frequent(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (a INTEGER PRIMARY KEY, b TEXT)")
        db.execute("INSERT INTO t VALUES (1, 'x')")
        # filter on a 3x, on b 1x
        db.execute("SELECT * FROM t WHERE a = 1")
        db.execute("SELECT * FROM t WHERE a = 2")
        db.execute("SELECT * FROM t WHERE a = 3")
        db.execute("SELECT * FROM t WHERE b = x")
        rows = db.execute("SHOW QUERY STATS").fetchall()
        assert rows[0]["top_filter_column"] == "a"
        assert rows[0]["top_filter_count"] == 3
        db.close()

    def test_multiple_tables_all_shown(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE a (id INTEGER PRIMARY KEY)")
        db.execute("CREATE TABLE b (id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO a VALUES (1)")
        db.execute("INSERT INTO b VALUES (1)")
        rows = db.execute("SHOW QUERY STATS").fetchall()
        tables = {r["table"] for r in rows}
        assert "a" in tables
        assert "b" in tables
        db.close()

    def test_no_filter_column_shows_none(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO t VALUES (1)")
        db.execute("SELECT * FROM t")
        rows = db.execute("SHOW QUERY STATS").fetchall()
        assert rows[0]["top_filter_column"] is None
        assert rows[0]["top_filter_count"] is None
        db.close()


# ── SHOW INDEX SUGGESTIONS ────────────────────────────────────────────────────

class TestShowIndexSuggestions:

    def test_empty_when_no_stats(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        rows = db.execute("SHOW INDEX SUGGESTIONS").fetchall()
        assert rows == []
        db.close()

    def test_no_suggestion_below_threshold(self, tmp_path):
        """Column filtered in <20% of queries should not be suggested."""
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, rare TEXT)")
        # 10 INSERTs, 1 SELECT with WHERE on rare → 1/11 ≈ 9%
        for i in range(10):
            db.execute("INSERT INTO t VALUES (?, ?)", (i, "x"))
        db.execute("SELECT * FROM t WHERE rare = x")
        rows = db.execute("SHOW INDEX SUGGESTIONS").fetchall()
        assert rows == []
        db.close()

    def test_suggestion_above_threshold(self, tmp_path):
        """Column filtered in ≥20% of queries should be suggested."""
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, cid INTEGER)")
        db.execute("INSERT INTO orders VALUES (1, 10)")
        # 4 SELECTs on cid → 4/5 = 80%
        for i in range(4):
            db.execute("SELECT * FROM orders WHERE cid = ?", (i,))
        rows = db.execute("SHOW INDEX SUGGESTIONS").fetchall()
        assert len(rows) == 1
        r = rows[0]
        assert r["table"] == "orders"
        assert r["column"] == "cid"
        assert r["filter_pct"] >= 20
        assert "CREATE INDEX" in r["suggestion"]
        assert "orders" in r["suggestion"]
        assert "cid" in r["suggestion"]
        db.close()

    def test_existing_index_not_suggested(self, tmp_path):
        """Column with an existing index should never appear in suggestions."""
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, col INTEGER)")
        db.execute("CREATE INDEX idx_t_col ON t(col)")
        db.execute("INSERT INTO t VALUES (1, 99)")
        for _ in range(10):
            db.execute("SELECT * FROM t WHERE col = 99")
        rows = db.execute("SHOW INDEX SUGGESTIONS").fetchall()
        assert all(r["column"] != "col" for r in rows)
        db.close()

    def test_columns_correct(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, x INTEGER)")
        db.execute("INSERT INTO t VALUES (1, 5)")
        for _ in range(5):
            db.execute("SELECT * FROM t WHERE x = 5")
        rows = db.execute("SHOW INDEX SUGGESTIONS").fetchall()
        assert len(rows) >= 1
        r = rows[0]
        assert set(r.keys()) == {"table", "column", "filter_count",
                                  "query_count", "filter_pct", "suggestion"}
        db.close()

    def test_suggestion_text_format(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE sales (id INTEGER PRIMARY KEY, region TEXT)")
        db.execute("INSERT INTO sales VALUES (1, 'north')")
        for _ in range(5):
            db.execute("SELECT * FROM sales WHERE region = north")
        rows = db.execute("SHOW INDEX SUGGESTIONS").fetchall()
        assert any(
            r["suggestion"] == "CREATE INDEX idx_sales_region ON sales(region)"
            for r in rows
        )
        db.close()


# ── persistence ───────────────────────────────────────────────────────────────

class TestStatPersistence:

    def test_stats_survive_reopen(self, tmp_path):
        db_path = str(tmp_path / "test.hdb")
        db = Database(db_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO t VALUES (1)")
        for _ in range(5):
            db.execute("SELECT * FROM t WHERE id = 1")
        # Force a write so access_stats snippet is committed
        db.execute("INSERT INTO t VALUES (2)")
        db.close()

        db2 = Database(db_path)
        stats = db2.execute("SHOW QUERY STATS").fetchall()
        assert any(r["table"] == "t" and r["query_count"] >= 5 for r in stats)
        db2.close()
