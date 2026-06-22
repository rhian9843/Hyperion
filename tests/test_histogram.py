"""Tests for equi-depth column histograms collected by ANALYZE and used in
selectivity estimation (range predicates and BETWEEN)."""

import pytest
from collections import Counter
from hyperion import Database
from hyperion.executor import _build_equidepth_histogram
from hyperion.optimizer import (
    estimate_selectivity,
    estimate_range_selectivity,
    estimate_row_count_with_where,
)


# ── _build_equidepth_histogram unit tests ─────────────────────────────────────

class TestBuildEquidepthHistogram:

    def test_empty_counter_returns_empty(self):
        assert _build_equidepth_histogram(Counter()) == []

    def test_single_value_returns_one_bucket(self):
        hist = _build_equidepth_histogram(Counter({5: 10}))
        assert len(hist) == 1
        assert hist[0] == [5, 5, 10]

    def test_two_distinct_values(self):
        hist = _build_equidepth_histogram(Counter({1: 5, 9: 5}))
        assert len(hist) <= 2
        total = sum(f for _, _, f in hist)
        assert total == 10

    def test_at_most_ten_buckets(self):
        vc = Counter({i: 1 for i in range(100)})
        hist = _build_equidepth_histogram(vc)
        assert len(hist) <= 10

    def test_total_frequency_equals_row_count(self):
        vc = Counter({i: i + 1 for i in range(50)})
        hist = _build_equidepth_histogram(vc)
        total = sum(f for _, _, f in hist)
        assert total == sum(vc.values())

    def test_bucket_bounds_monotone(self):
        vc = Counter({i: 1 for i in range(30)})
        hist = _build_equidepth_histogram(vc)
        for i in range(len(hist) - 1):
            # Each bucket's hi must be <= next bucket's lo
            assert hist[i][1] <= hist[i + 1][0]

    def test_each_bucket_lo_le_hi(self):
        vc = Counter({i: 3 for i in range(20)})
        hist = _build_equidepth_histogram(vc)
        for lo, hi, _ in hist:
            assert lo <= hi

    def test_string_values_sorted(self):
        vc = Counter({"banana": 5, "apple": 3, "cherry": 2})
        hist = _build_equidepth_histogram(vc)
        assert len(hist) >= 1
        total = sum(f for _, _, f in hist)
        assert total == 10

    def test_mixed_incomparable_types_returns_empty(self):
        vc = Counter({1: 5, "a": 3})
        # Python 3 cannot compare int and str — should return []
        assert _build_equidepth_histogram(vc) == []

    def test_fewer_distinct_than_buckets(self):
        # Only 3 distinct values → at most 3 buckets
        vc = Counter({10: 20, 20: 20, 30: 20})
        hist = _build_equidepth_histogram(vc, n_buckets=10)
        assert len(hist) <= 3

    def test_skewed_distribution_bucket_sizes(self):
        # 90 × value 1, 5 × value 50, 5 × value 100
        vc = Counter({1: 90, 50: 5, 100: 5})
        hist = _build_equidepth_histogram(vc, n_buckets=2)
        # First bucket should cover value 1 (freq ≈ 90)
        assert hist[0][0] == 1
        assert hist[0][2] >= 50


# ── ANALYZE stores histogram in catalog ──────────────────────────────────────

class TestAnalyzeStoresHistogram:

    def fresh_db(self, tmp_path):
        return Database(str(tmp_path / "test.hdb"))

    def test_histogram_key_present_after_analyze(self, tmp_path):
        db = self.fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val INTEGER)")
        for i in range(20):
            db.execute("INSERT INTO t VALUES (?, ?)", (i, i))
        db.execute("ANALYZE")
        col = db._catalog.stats["t"]["columns"]["val"]
        assert "histogram" in col
        db.close()

    def test_histogram_list_of_triples(self, tmp_path):
        db = self.fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val INTEGER)")
        for i in range(30):
            db.execute("INSERT INTO t VALUES (?, ?)", (i, i))
        db.execute("ANALYZE")
        hist = db._catalog.stats["t"]["columns"]["val"]["histogram"]
        assert isinstance(hist, list)
        for entry in hist:
            assert len(entry) == 3
            lo, hi, freq = entry
            assert lo <= hi
            assert freq > 0
        db.close()

    def test_histogram_at_most_ten_buckets_from_sql(self, tmp_path):
        db = self.fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val INTEGER)")
        for i in range(100):
            db.execute("INSERT INTO t VALUES (?, ?)", (i, i))
        db.execute("ANALYZE")
        hist = db._catalog.stats["t"]["columns"]["val"]["histogram"]
        assert len(hist) <= 10
        db.close()

    def test_histogram_total_equals_non_null_count(self, tmp_path):
        db = self.fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val INTEGER)")
        for i in range(50):
            db.execute("INSERT INTO t VALUES (?, ?)", (i, i))
        db.execute("INSERT INTO t VALUES (100, NULL)")
        db.execute("INSERT INTO t VALUES (101, NULL)")
        db.execute("ANALYZE")
        hist = db._catalog.stats["t"]["columns"]["val"]["histogram"]
        total = sum(f for _, _, f in hist)
        assert total == 50  # NULLs excluded
        db.close()

    def test_histogram_persists_after_reopen(self, tmp_path):
        path = str(tmp_path / "test.hdb")
        db = Database(path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val INTEGER)")
        for i in range(20):
            db.execute("INSERT INTO t VALUES (?, ?)", (i, i))
        db.execute("ANALYZE")
        db.close()

        db2 = Database(path)
        hist = db2._catalog.stats["t"]["columns"]["val"]["histogram"]
        assert len(hist) >= 1
        db2.close()

    def test_all_null_column_histogram_is_empty(self, tmp_path):
        db = self.fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val INTEGER)")
        for i in range(5):
            db.execute("INSERT INTO t VALUES (?, NULL)", (i,))
        db.execute("ANALYZE")
        hist = db._catalog.stats["t"]["columns"]["val"]["histogram"]
        assert hist == []
        db.close()


# ── estimate_selectivity with histogram ──────────────────────────────────────

class TestEstimateSelectivityHistogram:

    def _db_skewed(self, tmp_path):
        """80 rows with val in [1, 10], 20 rows with val in [90, 100]."""
        db = Database(str(tmp_path / "test.hdb"))
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val INTEGER)")
        pk = 0
        for v in range(1, 11):      # 8 rows per value × 10 values = 80 rows
            for _ in range(8):
                db.execute("INSERT INTO t VALUES (?, ?)", (pk, v))
                pk += 1
        for v in range(90, 101):    # 2 rows per value × 10 values = 20 rows
            for _ in range(2):
                db.execute("INSERT INTO t VALUES (?, ?)", (pk, v))
                pk += 1
        db.execute("ANALYZE")
        return db

    def test_lt_lower_range_higher_than_linear(self, tmp_path):
        db = self._db_skewed(tmp_path)
        # 80% of data is in [1..10]; val < 11 should be ~0.80
        sel = estimate_selectivity(db, "t", "val", "<", 11)
        # Linear interpolation (min=1, max=100) would give (11-1)/99 ≈ 0.10 — wrong
        # Histogram should give ~0.80
        assert sel > 0.60, f"Expected ~0.80 but got {sel}"
        db.close()

    def test_gt_upper_range(self, tmp_path):
        db = self._db_skewed(tmp_path)
        # 20% of data is in [90..100]; val > 89 should be ~0.20
        sel = estimate_selectivity(db, "t", "val", ">", 89)
        assert sel > 0.10, f"Expected ~0.20 but got {sel}"
        db.close()

    def test_lt_below_all_values_returns_zero(self, tmp_path):
        db = self._db_skewed(tmp_path)
        sel = estimate_selectivity(db, "t", "val", "<", 0)
        assert sel == pytest.approx(0.0)
        db.close()

    def test_gt_above_all_values_returns_zero(self, tmp_path):
        db = self._db_skewed(tmp_path)
        sel = estimate_selectivity(db, "t", "val", ">", 1000)
        assert sel == pytest.approx(0.0)
        db.close()

    def test_selectivity_in_unit_interval(self, tmp_path):
        db = self._db_skewed(tmp_path)
        for op, v in [("<", 5), (">", 5), ("<=", 50), (">=", 50)]:
            sel = estimate_selectivity(db, "t", "val", op, v)
            assert 0.0 <= sel <= 1.0, f"op={op}, val={v}: sel={sel}"
        db.close()

    def test_lt_vs_lte_close(self, tmp_path):
        db = self._db_skewed(tmp_path)
        lt  = estimate_selectivity(db, "t", "val", "<",  10)
        lte = estimate_selectivity(db, "t", "val", "<=", 10)
        assert abs(lt - lte) < 0.15   # approximate — histogram buckets
        db.close()

    def test_gt_vs_gte_close(self, tmp_path):
        db = self._db_skewed(tmp_path)
        gt  = estimate_selectivity(db, "t", "val", ">",  90)
        gte = estimate_selectivity(db, "t", "val", ">=", 90)
        assert abs(gt - gte) < 0.15
        db.close()

    def test_uniform_distribution_lt_midpoint(self, tmp_path):
        db = Database(str(tmp_path / "test.hdb"))
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val INTEGER)")
        for i in range(1, 101):
            db.execute("INSERT INTO t VALUES (?, ?)", (i, i))
        db.execute("ANALYZE")
        # val < 51 → should be ~0.50 for uniform distribution
        sel = estimate_selectivity(db, "t", "val", "<", 51)
        assert 0.35 <= sel <= 0.65, f"Expected ~0.50 but got {sel}"
        db.close()


# ── estimate_range_selectivity (BETWEEN) ─────────────────────────────────────

class TestEstimateRangeSelectivity:

    def _db(self, tmp_path):
        db = Database(str(tmp_path / "test.hdb"))
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val INTEGER)")
        for i in range(1, 101):
            db.execute("INSERT INTO t VALUES (?, ?)", (i, i))
        db.execute("ANALYZE")
        return db

    def test_full_range_returns_one(self, tmp_path):
        db = self._db(tmp_path)
        sel = estimate_range_selectivity(db, "t", "val", 1, 100)
        assert sel == pytest.approx(1.0, abs=0.05)
        db.close()

    def test_empty_range_returns_zero(self, tmp_path):
        db = self._db(tmp_path)
        sel = estimate_range_selectivity(db, "t", "val", 200, 300)
        assert sel == pytest.approx(0.0)
        db.close()

    def test_inverted_range_returns_zero(self, tmp_path):
        db = self._db(tmp_path)
        sel = estimate_range_selectivity(db, "t", "val", 80, 20)
        assert sel == pytest.approx(0.0)
        db.close()

    def test_half_range_approx_half(self, tmp_path):
        db = self._db(tmp_path)
        sel = estimate_range_selectivity(db, "t", "val", 26, 75)
        assert 0.35 <= sel <= 0.65, f"Expected ~0.50 but got {sel}"
        db.close()

    def test_no_stats_returns_default(self, tmp_path):
        db = Database(str(tmp_path / "test.hdb"))
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val INTEGER)")
        sel = estimate_range_selectivity(db, "t", "val", 1, 50)
        assert sel == pytest.approx(0.33)
        db.close()

    def test_result_clamped_to_unit_interval(self, tmp_path):
        db = self._db(tmp_path)
        sel = estimate_range_selectivity(db, "t", "val", -1000, 9999)
        assert 0.0 <= sel <= 1.0
        db.close()


# ── estimate_row_count_with_where with BETWEEN ────────────────────────────────

class TestEstimateRowCountBetween:

    def test_between_condition_syntax(self, tmp_path):
        db = Database(str(tmp_path / "test.hdb"))
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val INTEGER)")
        for i in range(1, 101):
            db.execute("INSERT INTO t VALUES (?, ?)", (i, i))
        db.execute("ANALYZE")
        # BETWEEN 25 AND 75 → ~50 rows
        est = estimate_row_count_with_where(db, "t", [("val", "BETWEEN", (25, 75))])
        assert 30 <= est <= 70
        db.close()

    def test_between_mixed_with_eq(self, tmp_path):
        db = Database(str(tmp_path / "test.hdb"))
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val INTEGER, cat TEXT)")
        for i in range(1, 101):
            cat = "A" if i <= 50 else "B"
            db.execute("INSERT INTO t VALUES (?, ?, ?)", (i, i, cat))
        db.execute("ANALYZE")
        # cat='A' (sel≈0.5) AND val BETWEEN 1 AND 50 (sel≈0.5) → ~25
        est = estimate_row_count_with_where(db, "t", [
            ("cat",  "=",       "A"),
            ("val",  "BETWEEN", (1, 50)),
        ])
        assert 10 <= est <= 40
        db.close()
