"""Tests for full column statistics (MCVs, min/max, null_count) and
selectivity estimation functions added to ANALYZE and optimizer.py."""

import pytest
from hyperion import Database
from hyperion.optimizer import (
    estimate_selectivity,
    estimate_row_count_with_where,
    _col_stats,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def fresh_db(tmp_path):
    return Database(str(tmp_path / "test.hdb"))


# ── ANALYZE output structure ──────────────────────────────────────────────────

class TestAnalyzeCollectsStats:

    def test_row_count_stored(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val INTEGER)")
        for i in range(1, 6):
            db.execute("INSERT INTO t VALUES (?, ?)", (i, i * 10))
        db.execute("ANALYZE")
        stats = db._catalog.stats["t"]
        assert stats["row_count"] == 5
        db.close()

    def test_ndv_stored(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, cat TEXT)")
        for i in range(10):
            db.execute("INSERT INTO t VALUES (?, ?)", (i, "A" if i % 2 == 0 else "B"))
        db.execute("ANALYZE")
        col = db._catalog.stats["t"]["columns"]["cat"]
        assert col["ndv"] == 2
        db.close()

    def test_min_max_stored(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val INTEGER)")
        for v in [5, 1, 9, 3, 7]:
            db.execute("INSERT INTO t (val) VALUES (?)", (v,))
        db.execute("ANALYZE")
        col = db._catalog.stats["t"]["columns"]["val"]
        assert col["min_val"] == 1
        assert col["max_val"] == 9
        db.close()

    def test_null_count_stored(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val INTEGER)")
        db.execute("INSERT INTO t VALUES (1, 10)")
        db.execute("INSERT INTO t VALUES (2, NULL)")
        db.execute("INSERT INTO t VALUES (3, NULL)")
        db.execute("ANALYZE")
        col = db._catalog.stats["t"]["columns"]["val"]
        assert col["null_count"] == 2
        db.close()

    def test_mcv_stored_and_ordered(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, cat TEXT)")
        for i in range(10):
            db.execute("INSERT INTO t (cat) VALUES ('A')")   # 10× A
        for i in range(5):
            db.execute("INSERT INTO t (cat) VALUES ('B')")   # 5× B
        for i in range(2):
            db.execute("INSERT INTO t (cat) VALUES ('C')")   # 2× C
        db.execute("ANALYZE")
        mcv = db._catalog.stats["t"]["columns"]["cat"]["mcv"]
        assert mcv[0][0] == "A" and mcv[0][1] == 10
        assert mcv[1][0] == "B" and mcv[1][1] == 5
        assert mcv[2][0] == "C" and mcv[2][1] == 2
        db.close()

    def test_mcv_capped_at_10(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val INTEGER)")
        for i in range(20):
            db.execute("INSERT INTO t VALUES (?, ?)", (i, i))
        db.execute("ANALYZE")
        mcv = db._catalog.stats["t"]["columns"]["val"]["mcv"]
        assert len(mcv) <= 10
        db.close()

    def test_all_null_column(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val INTEGER)")
        for i in range(3):
            db.execute("INSERT INTO t VALUES (?, NULL)", (i,))
        db.execute("ANALYZE")
        col = db._catalog.stats["t"]["columns"]["val"]
        assert col["null_count"] == 3
        assert col["min_val"] is None
        assert col["max_val"] is None
        assert col["mcv"] == []
        db.close()

    def test_analyze_specific_table(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE a (id INTEGER PRIMARY KEY)")
        db.execute("CREATE TABLE b (id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO a VALUES (1)")
        db.execute("INSERT INTO b VALUES (1)")
        db.execute("INSERT INTO b VALUES (2)")
        db.execute("ANALYZE a")
        assert db._catalog.stats["a"]["row_count"] == 1
        assert "b" not in db._catalog.stats
        db.close()

    def test_stats_persist_after_reopen(self, tmp_path):
        path = str(tmp_path / "test.hdb")
        db = Database(path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val INTEGER)")
        for i in range(5):
            db.execute("INSERT INTO t VALUES (?, ?)", (i, i))
        db.execute("ANALYZE")
        db.close()

        db2 = Database(path)
        col = db2._catalog.stats["t"]["columns"]["val"]
        assert col["min_val"] == 0
        assert col["max_val"] == 4
        db2.close()


# ── estimate_selectivity ──────────────────────────────────────────────────────

class TestEstimateSelectivity:

    def _db_with_data(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val INTEGER, cat TEXT)")
        # val: 1..10 uniform; cat: 6×A, 3×B, 1×C
        for i in range(1, 11):
            cat = "A" if i <= 6 else ("B" if i <= 9 else "C")
            db.execute("INSERT INTO t VALUES (?, ?, ?)", (i, i, cat))
        db.execute("ANALYZE")
        return db

    def test_eq_mcv_hit(self, tmp_path):
        db = self._db_with_data(tmp_path)
        sel = estimate_selectivity(db, "t", "cat", "=", "A")
        assert abs(sel - 0.6) < 0.01   # 6/10
        db.close()

    def test_eq_mcv_miss_falls_back_to_ndv(self, tmp_path):
        db = self._db_with_data(tmp_path)
        # "val" has 10 distinct values, no MCV value equals 999
        sel = estimate_selectivity(db, "t", "val", "=", 999)
        assert abs(sel - 1.0 / 10) < 0.01
        db.close()

    def test_lt_interpolation(self, tmp_path):
        db = self._db_with_data(tmp_path)
        # val: min=1, max=10; val < 5.5 → (5.5 - 1) / (10 - 1) ≈ 0.5
        sel = estimate_selectivity(db, "t", "val", "<", 5.5)
        assert 0.4 < sel < 0.6
        db.close()

    def test_gt_interpolation(self, tmp_path):
        db = self._db_with_data(tmp_path)
        # val > 8 → 2 out of 10 rows (9, 10); boundary bucket [8,8] excluded by strict >
        sel = estimate_selectivity(db, "t", "val", ">", 8)
        assert 0.15 < sel < 0.30
        db.close()

    def test_lte_ge_than_lt(self, tmp_path):
        db = self._db_with_data(tmp_path)
        sel_lt  = estimate_selectivity(db, "t", "val", "<",  5.0)
        sel_lte = estimate_selectivity(db, "t", "val", "<=", 5.0)
        # <= must be at least as large as < (includes the boundary value)
        assert sel_lte >= sel_lt
        db.close()

    def test_gte_ge_than_gt(self, tmp_path):
        db = self._db_with_data(tmp_path)
        sel_gt  = estimate_selectivity(db, "t", "val", ">",  5.0)
        sel_gte = estimate_selectivity(db, "t", "val", ">=", 5.0)
        # >= must be at least as large as > (includes the boundary value)
        assert sel_gte >= sel_gt
        db.close()

    def test_no_stats_returns_default(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val INTEGER)")
        # no ANALYZE — no stats
        sel = estimate_selectivity(db, "t", "val", "=", 1)
        assert sel == pytest.approx(0.33)
        db.close()

    def test_selectivity_clipped_to_zero_one(self, tmp_path):
        db = self._db_with_data(tmp_path)
        sel_low = estimate_selectivity(db, "t", "val", "<", -999)
        sel_high = estimate_selectivity(db, "t", "val", ">", 9999)
        assert sel_low == pytest.approx(0.0)
        assert sel_high == pytest.approx(0.0)
        db.close()

    def test_uniform_column_eq_selectivity(self, tmp_path):
        db = self._db_with_data(tmp_path)
        # val has ndv=10, so 1/10 per value
        sel = estimate_selectivity(db, "t", "val", "=", 1)
        assert abs(sel - 0.1) < 0.01
        db.close()


# ── estimate_row_count_with_where ─────────────────────────────────────────────

class TestEstimateRowCountWithWhere:

    def _db(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val INTEGER, cat TEXT)")
        for i in range(1, 101):
            cat = "A" if i <= 50 else "B"
            db.execute("INSERT INTO t VALUES (?, ?, ?)", (i, i, cat))
        db.execute("ANALYZE")
        return db

    def test_single_eq_condition(self, tmp_path):
        db = self._db(tmp_path)
        # cat='A' → 50/100 = 0.5 selectivity → ~50 rows
        est = estimate_row_count_with_where(db, "t", [("cat", "=", "A")])
        assert 40 <= est <= 60
        db.close()

    def test_single_range_condition(self, tmp_path):
        db = self._db(tmp_path)
        # val < 50 → about half the rows
        est = estimate_row_count_with_where(db, "t", [("val", "<", 50)])
        assert 30 <= est <= 70
        db.close()

    def test_multiple_conditions_multiplicative(self, tmp_path):
        db = self._db(tmp_path)
        # cat='A' (sel=0.5) AND val < 30 (sel≈0.29) → ~0.145 * 100 ≈ 14
        est = estimate_row_count_with_where(db, "t", [
            ("cat", "=", "A"),
            ("val", "<", 30),
        ])
        assert 5 <= est <= 30
        db.close()

    def test_empty_conditions_returns_full_row_count(self, tmp_path):
        db = self._db(tmp_path)
        est = estimate_row_count_with_where(db, "t", [])
        assert est == 100
        db.close()

    def test_result_at_least_one(self, tmp_path):
        db = self._db(tmp_path)
        # Impossible condition — result clamped to 1
        est = estimate_row_count_with_where(db, "t", [
            ("val", "<", -9999),
            ("cat", "=", "Z"),
        ])
        assert est >= 1
        db.close()


# ── _col_stats helper ─────────────────────────────────────────────────────────

class TestColStats:

    def test_returns_empty_dict_when_no_analyze(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        assert _col_stats(db, "t", "id") == {}
        db.close()

    def test_strips_table_prefix(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val INTEGER)")
        db.execute("INSERT INTO t VALUES (1, 42)")
        db.execute("ANALYZE")
        cs = _col_stats(db, "t", "t.val")
        assert cs.get("ndv") == 1
        db.close()
