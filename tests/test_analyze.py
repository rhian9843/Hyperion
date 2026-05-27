# Tests for: ANALYZE — per-table/index statistics and NDV-aware optimizer
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hyperion.database import Database
from hyperion.executor import execute
from hyperion.optimizer import estimate_rows, get_ndv, optimize_join
from hyperion.parser import parse


def sql(db: Database, stmt: str) -> str:
    return execute(parse(stmt), db)


# ── ANALYZE correctness ───────────────────────────────────────────────────────

class TestAnalyzeBasic(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        sql(self.db, "CREATE TABLE t (id INTEGER, name VARCHAR(32), age INTEGER)")
        for i in range(10):
            sql(self.db, f"INSERT INTO t VALUES ({i}, 'name{i % 3}', {20 + i})")

    def tearDown(self):
        self.db.close()

    def test_analyze_all_tables_returns_message(self):
        result = sql(self.db, "ANALYZE")
        self.assertIn("Statistics collected", result)
        self.assertIn("t", result)

    def test_analyze_specific_table(self):
        result = sql(self.db, "ANALYZE t")
        self.assertIn("Statistics collected", result)
        self.assertIn("t", result)

    def test_analyze_unknown_table_raises(self):
        with self.assertRaises(RuntimeError):
            sql(self.db, "ANALYZE nonexistent")

    def test_stats_row_count_correct(self):
        sql(self.db, "ANALYZE t")
        stats = self.db._catalog.stats
        self.assertIn("t", stats)
        self.assertEqual(stats["t"]["row_count"], 10)

    def test_stats_ndv_id_column(self):
        # id has 10 distinct values (0..9)
        sql(self.db, "ANALYZE t")
        ndv = get_ndv(self.db, "t", "id")
        self.assertEqual(ndv, 10)

    def test_stats_ndv_low_cardinality_column(self):
        # name cycles through 3 values
        sql(self.db, "ANALYZE t")
        ndv = get_ndv(self.db, "t", "name")
        self.assertEqual(ndv, 3)

    def test_stats_ndv_age_column(self):
        # age has 10 distinct values (20..29)
        sql(self.db, "ANALYZE t")
        ndv = get_ndv(self.db, "t", "age")
        self.assertEqual(ndv, 10)

    def test_get_ndv_before_analyze_returns_none(self):
        # No stats yet — should return None, not raise
        ndv = get_ndv(self.db, "t", "id")
        self.assertIsNone(ndv)

    def test_get_ndv_unknown_column_returns_none(self):
        sql(self.db, "ANALYZE t")
        ndv = get_ndv(self.db, "t", "nonexistent")
        self.assertIsNone(ndv)

    def test_analyze_null_values_count_as_distinct(self):
        sql(self.db, "CREATE TABLE n (id INTEGER, val INTEGER)")
        for i in range(5):
            sql(self.db, f"INSERT INTO n VALUES ({i}, NULL)")
        sql(self.db, "INSERT INTO n VALUES (5, 1)")
        sql(self.db, "ANALYZE n")
        # val has 2 distinct values: NULL and 1
        ndv = get_ndv(self.db, "n", "val")
        self.assertEqual(ndv, 2)


# ── estimate_rows uses stats ──────────────────────────────────────────────────

class TestEstimateRowsWithStats(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        sql(self.db, "CREATE TABLE t (id INTEGER)")
        for i in range(20):
            sql(self.db, f"INSERT INTO t VALUES ({i})")

    def tearDown(self):
        self.db.close()

    def test_estimate_rows_uses_persisted_stats(self):
        sql(self.db, "ANALYZE t")
        # Clear/reset session cache to force re-read from catalog stats
        self.db._opt_row_counts = {}
        count = estimate_rows(self.db, "t")
        self.assertEqual(count, 20)

    def test_analyze_refreshes_session_cache(self):
        # Seed stale value in cache
        self.db._opt_row_counts = {"t": 999}
        sql(self.db, "ANALYZE t")
        # Cache should now be refreshed
        self.assertEqual(self.db._opt_row_counts.get("t"), 20)

    def test_estimate_rows_fallback_without_stats(self):
        # Without ANALYZE, estimate_rows falls back to btree scan
        count = estimate_rows(self.db, "t")
        self.assertEqual(count, 20)

    def test_analyze_multiple_tables(self):
        sql(self.db, "CREATE TABLE u (id INTEGER)")
        for i in range(5):
            sql(self.db, f"INSERT INTO u VALUES ({i})")
        result = sql(self.db, "ANALYZE")
        self.assertEqual(self.db._catalog.stats["t"]["row_count"], 20)
        self.assertEqual(self.db._catalog.stats["u"]["row_count"], 5)


# ── NDV-aware output estimation improves join ordering ────────────────────────

class TestNDVAwareJoinOrdering(unittest.TestCase):
    """After ANALYZE, the optimizer uses NDV to estimate intermediate row sizes,
    which should produce better join ordering decisions for multi-table queries."""

    def setUp(self):
        self.db = Database(":memory:")
        # orders: 100 rows, user_id has 10 distinct values (FK to users)
        # users: 10 rows, id is PK (NDV=10)
        # tags: 3 rows, user_id has 3 distinct values
        sql(self.db, "CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR(32))")
        sql(self.db, "CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER, amount INTEGER)")
        sql(self.db, "CREATE TABLE tags (id INTEGER PRIMARY KEY, user_id INTEGER, tag VARCHAR(16))")
        for i in range(1, 11):
            sql(self.db, f"INSERT INTO users VALUES ({i}, 'user{i}')")
        for i in range(1, 101):
            sql(self.db, f"INSERT INTO orders VALUES ({i}, {(i % 10) + 1}, {i * 5})")
        for i in range(1, 4):
            sql(self.db, f"INSERT INTO tags VALUES ({i}, {i}, 'tag{i}')")

    def tearDown(self):
        self.db.close()

    def test_three_way_join_correct_after_analyze(self):
        # users 1-3 each have 10 matching orders and 1 matching tag → 30 rows total
        sql(self.db, "ANALYZE")
        result = sql(self.db, """
            SELECT users.name, orders.amount, tags.tag
            FROM users
            JOIN orders ON users.id = orders.user_id
            JOIN tags ON users.id = tags.user_id
            ORDER BY orders.id
        """)
        self.assertIn("(30 rows)", result)

    def test_two_way_join_correct_after_analyze(self):
        sql(self.db, "ANALYZE")
        result = sql(self.db, """
            SELECT users.name, orders.amount
            FROM users
            JOIN orders ON users.id = orders.user_id
            ORDER BY orders.id
        """)
        self.assertIn("(100 rows)", result)

    def test_get_ndv_qualified_col(self):
        sql(self.db, "ANALYZE")
        # get_ndv should strip the table prefix
        ndv = get_ndv(self.db, "orders", "orders.user_id")
        self.assertEqual(ndv, 10)


# ── Stats persistence (file-backed) ──────────────────────────────────────────

class TestStatsPersistence(unittest.TestCase):
    def setUp(self):
        import tempfile, os
        self._tmpdir = tempfile.mkdtemp()
        self._dbpath = Path(self._tmpdir) / "test.hdb"

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_stats_survive_reopen(self):
        db = Database(self._dbpath)
        try:
            execute(parse("CREATE TABLE t (id INTEGER, val INTEGER)"), db)
            for i in range(7):
                execute(parse(f"INSERT INTO t VALUES ({i}, {i % 3})"), db)
            execute(parse("ANALYZE t"), db)
        finally:
            db.close()

        db2 = Database(self._dbpath)
        try:
            # Stats should be loaded from disk
            stats = db2._catalog.stats
            self.assertEqual(stats["t"]["row_count"], 7)
            self.assertEqual(stats["t"]["columns"]["val"]["ndv"], 3)
            # estimate_rows should use the persisted stats without scanning
            count = estimate_rows(db2, "t")
            self.assertEqual(count, 7)
        finally:
            db2.close()


if __name__ == "__main__":
    unittest.main()
