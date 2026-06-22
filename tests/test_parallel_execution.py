"""Tests for parallel query execution: SET MAX_PARALLEL_WORKERS, SET PARALLEL_THRESHOLD,
SHOW PARALLEL STATUS, /*+ PARALLEL(N) */ hint, parallel aggregate, parallel scan."""

import pytest
from hyperion import Database
from hyperion.parallel_executor import (
    ParallelConfig, parallel_filter, parallel_agg_column,
    parallel_compute_simple_aggs, extract_parallel_hint,
)


# ── unit tests ────────────────────────────────────────────────────────────────

class TestParallelConfig:

    def test_defaults(self):
        pc = ParallelConfig()
        assert pc.max_workers == 4
        assert pc.threshold == 1000

    def test_effective_workers_hint_overrides(self):
        pc = ParallelConfig(max_workers=4)
        assert pc.effective_workers(hint=2) == 2

    def test_effective_workers_clamps_to_cpu_count(self):
        import os
        pc = ParallelConfig(max_workers=999)
        assert pc.effective_workers() <= (os.cpu_count() or 1)

    def test_effective_workers_min_one(self):
        pc = ParallelConfig(max_workers=0)
        assert pc.effective_workers() >= 1


class TestParallelFilter:

    def _rows(self, n):
        return [{"id": i, "val": i} for i in range(n)]

    def test_filters_correctly_single_thread(self):
        from hyperion.where import WhereClause
        rows = self._rows(10)

        class _W:
            and_clause = or_clause = None
            def evaluate(self, r, db):
                return r["val"] > 5
        result = parallel_filter(rows, _W(), None, workers=1)
        assert [r["val"] for r in result] == [6, 7, 8, 9]

    def test_filters_correctly_multi_thread(self):
        rows = self._rows(20)

        class _W:
            and_clause = or_clause = None
            def evaluate(self, r, db):
                return r["val"] % 2 == 0
        result = parallel_filter(rows, _W(), None, workers=4)
        assert all(r["val"] % 2 == 0 for r in result)
        assert len(result) == 10

    def test_empty_rows_returns_empty(self):
        class _W:
            def evaluate(self, r, db): return True
        assert parallel_filter([], _W(), None, workers=2) == []


class TestParallelAggColumn:

    def test_count_and_sum(self):
        rows = [{"v": i} for i in range(1, 11)]  # 1..10
        pa = parallel_agg_column(rows, "v", workers=4)
        assert pa.count == 10
        assert pa.total == 55.0
        assert pa.min_val == 1
        assert pa.max_val == 10

    def test_null_values_skipped(self):
        rows = [{"v": None}, {"v": 5}, {"v": None}, {"v": 3}]
        pa = parallel_agg_column(rows, "v", workers=2)
        assert pa.count == 2
        assert pa.total == 8.0

    def test_single_thread_same_result(self):
        rows = [{"v": i} for i in range(1, 21)]
        pa1 = parallel_agg_column(rows, "v", workers=1)
        pa2 = parallel_agg_column(rows, "v", workers=4)
        assert pa1.count == pa2.count
        assert pa1.total == pa2.total
        assert pa1.min_val == pa2.min_val
        assert pa1.max_val == pa2.max_val


class TestParallelComputeSimpleAggs:

    def test_count_star(self):
        rows = [{"id": i} for i in range(50)]
        result = parallel_compute_simple_aggs(rows, ["COUNT(*)"], workers=4)
        assert result is not None
        assert result["COUNT(*)"] == 50

    def test_sum(self):
        rows = [{"val": i} for i in range(1, 11)]
        result = parallel_compute_simple_aggs(rows, ["SUM(val)"], workers=2)
        assert result is not None
        assert result["SUM(val)"] == pytest.approx(55.0)

    def test_avg(self):
        rows = [{"val": float(i)} for i in range(1, 5)]
        result = parallel_compute_simple_aggs(rows, ["AVG(val)"], workers=2)
        assert result is not None
        assert result["AVG(val)"] == pytest.approx(2.5)

    def test_min_max(self):
        rows = [{"val": i} for i in [3, 1, 4, 1, 5, 9, 2, 6]]
        result = parallel_compute_simple_aggs(rows, ["MIN(val)", "MAX(val)"], workers=4)
        assert result is not None
        assert result["MIN(val)"] == 1
        assert result["MAX(val)"] == 9

    def test_non_aggregate_returns_none(self):
        rows = [{"val": 1}]
        result = parallel_compute_simple_aggs(rows, ["val"], workers=2)
        assert result is None

    def test_distinct_returns_none(self):
        rows = [{"val": 1}]
        result = parallel_compute_simple_aggs(rows, ["COUNT(DISTINCT val)"], workers=2)
        assert result is None


class TestExtractParallelHint:

    def test_extracts_hint(self):
        sql, n = extract_parallel_hint("/*+ PARALLEL(4) */ SELECT 1")
        assert n == 4
        assert "/*+" not in sql

    def test_no_hint_returns_none(self):
        sql, n = extract_parallel_hint("SELECT 1")
        assert n is None
        assert sql == "SELECT 1"

    def test_hint_case_insensitive(self):
        _, n = extract_parallel_hint("/*+ parallel(2) */ SELECT 1")
        assert n == 2

    def test_sql_preserved_without_hint(self):
        original = "SELECT * FROM t WHERE id = 1"
        sql, n = extract_parallel_hint(original)
        assert n is None
        assert sql == original


# ── SQL interface tests ───────────────────────────────────────────────────────

def fresh_db(tmp_path):
    return Database(str(tmp_path / "test.hdb"))


def _load_rows(db, n):
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val INTEGER)")
    for i in range(1, n + 1):
        db.execute("INSERT INTO t VALUES (?, ?)", (i, i))


class TestSetParallelConfig:

    def test_default_workers(self, tmp_path):
        db = fresh_db(tmp_path)
        assert db._parallel.max_workers == 4
        db.close()

    def test_set_max_parallel_workers(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("SET MAX_PARALLEL_WORKERS 2")
        assert db._parallel.max_workers == 2
        db.close()

    def test_set_parallel_threshold(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("SET PARALLEL_THRESHOLD 500")
        assert db._parallel.threshold == 500
        db.close()

    def test_set_workers_clamped_to_cpu_count(self, tmp_path):
        import os
        db = fresh_db(tmp_path)
        db.execute("SET MAX_PARALLEL_WORKERS 9999")
        assert db._parallel.max_workers <= (os.cpu_count() or 1)
        db.close()


class TestShowParallelStatus:

    def test_status_columns(self, tmp_path):
        db = fresh_db(tmp_path)
        rows = db.execute("SHOW PARALLEL STATUS").fetchall()
        settings = {r["setting"] for r in rows}
        assert "max_workers" in settings
        assert "threshold" in settings
        assert "cpu_count" in settings
        db.close()

    def test_status_reflects_set_commands(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("SET MAX_PARALLEL_WORKERS 3")
        db.execute("SET PARALLEL_THRESHOLD 200")
        rows = db.execute("SHOW PARALLEL STATUS").fetchall()
        vals = {r["setting"]: r["value"] for r in rows}
        assert vals["max_workers"] == "3"
        assert vals["threshold"] == "200"
        db.close()


class TestParallelAggregate:

    def test_count_star_correct(self, tmp_path):
        db = fresh_db(tmp_path)
        _load_rows(db, 50)
        db.execute("SET PARALLEL_THRESHOLD 10")
        db.execute("SET MAX_PARALLEL_WORKERS 4")
        rows = db.execute("SELECT COUNT(*) FROM t").fetchall()
        assert rows[0]["COUNT(*)"] == 50
        db.close()

    def test_sum_correct(self, tmp_path):
        db = fresh_db(tmp_path)
        _load_rows(db, 100)
        db.execute("SET PARALLEL_THRESHOLD 10")
        db.execute("SET MAX_PARALLEL_WORKERS 4")
        rows = db.execute("SELECT SUM(val) FROM t").fetchall()
        assert rows[0]["SUM(val)"] == pytest.approx(5050.0)
        db.close()

    def test_min_max_correct(self, tmp_path):
        db = fresh_db(tmp_path)
        _load_rows(db, 50)
        db.execute("SET PARALLEL_THRESHOLD 10")
        db.execute("SET MAX_PARALLEL_WORKERS 4")
        rows = db.execute("SELECT MIN(val), MAX(val) FROM t").fetchall()
        assert rows[0]["MIN(val)"] == 1
        assert rows[0]["MAX(val)"] == 50
        db.close()

    def test_avg_correct(self, tmp_path):
        db = fresh_db(tmp_path)
        _load_rows(db, 10)
        db.execute("SET PARALLEL_THRESHOLD 2")
        db.execute("SET MAX_PARALLEL_WORKERS 4")
        rows = db.execute("SELECT AVG(val) FROM t").fetchall()
        # AVG(1..10) = 5.5
        assert rows[0]["AVG(val)"] == pytest.approx(5.5)
        db.close()

    def test_multi_agg_correct(self, tmp_path):
        db = fresh_db(tmp_path)
        _load_rows(db, 20)
        db.execute("SET PARALLEL_THRESHOLD 5")
        db.execute("SET MAX_PARALLEL_WORKERS 4")
        rows = db.execute("SELECT COUNT(*), SUM(val), MIN(val), MAX(val) FROM t").fetchall()
        r = rows[0]
        assert r["COUNT(*)"] == 20
        assert r["SUM(val)"] == pytest.approx(210.0)
        assert r["MIN(val)"] == 1
        assert r["MAX(val)"] == 20
        db.close()

    def test_result_same_with_and_without_parallel(self, tmp_path):
        db = fresh_db(tmp_path)
        _load_rows(db, 30)
        # High threshold → sequential
        db.execute("SET PARALLEL_THRESHOLD 9999")
        seq = db.execute("SELECT COUNT(*), SUM(val) FROM t").fetchall()[0]
        # Low threshold → parallel
        db.execute("SET PARALLEL_THRESHOLD 5")
        par = db.execute("SELECT COUNT(*), SUM(val) FROM t").fetchall()[0]
        assert seq == par
        db.close()


class TestParallelHint:

    def test_hint_parsed_without_error(self, tmp_path):
        db = fresh_db(tmp_path)
        _load_rows(db, 10)
        rows = db.execute("/*+ PARALLEL(2) */ SELECT COUNT(*) FROM t").fetchall()
        assert rows[0]["COUNT(*)"] == 10
        db.close()

    def test_hint_result_matches_no_hint(self, tmp_path):
        db = fresh_db(tmp_path)
        _load_rows(db, 20)
        db.execute("SET PARALLEL_THRESHOLD 1")
        normal = db.execute("SELECT SUM(val) FROM t").fetchall()[0]["SUM(val)"]
        hinted = db.execute("/*+ PARALLEL(4) */ SELECT SUM(val) FROM t").fetchall()[0]["SUM(val)"]
        assert normal == pytest.approx(hinted)
        db.close()

    def test_hint_workers_zero_handled_gracefully(self, tmp_path):
        db = fresh_db(tmp_path)
        _load_rows(db, 5)
        rows = db.execute("/*+ PARALLEL(0) */ SELECT COUNT(*) FROM t").fetchall()
        assert rows[0]["COUNT(*)"] == 5
        db.close()
