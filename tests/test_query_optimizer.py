# Tests for: cost-based join optimizer (INLJ + join reordering)
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hyperion.database import Database
from hyperion.executor import execute
from hyperion.optimizer import estimate_rows, find_eq_index, probe_index, optimize_join
from hyperion.parser import parse


def sql(db: Database, stmt: str) -> str:
    return execute(parse(stmt), db)


# ── estimate_rows ─────────────────────────────────────────────────────────────

class TestEstimateRows(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")

    def tearDown(self):
        self.db.close()

    def test_empty_table_returns_zero(self):
        sql(self.db, "CREATE TABLE t (id INTEGER)")
        self.assertEqual(estimate_rows(self.db, "t"), 0)

    def test_count_matches_inserts(self):
        sql(self.db, "CREATE TABLE t (id INTEGER)")
        for i in range(10):
            sql(self.db, f"INSERT INTO t VALUES ({i})")
        self.assertEqual(estimate_rows(self.db, "t"), 10)

    def test_result_is_cached(self):
        sql(self.db, "CREATE TABLE t (id INTEGER)")
        for i in range(5):
            sql(self.db, f"INSERT INTO t VALUES ({i})")
        first  = estimate_rows(self.db, "t")
        # Insert another row — cached value should NOT change within session
        sql(self.db, "INSERT INTO t VALUES (99)")
        second = estimate_rows(self.db, "t")
        self.assertEqual(first, second)


# ── find_eq_index ─────────────────────────────────────────────────────────────

class TestFindEqIndex(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        sql(self.db, "CREATE TABLE t (id INTEGER PRIMARY KEY, val INTEGER)")
        sql(self.db, "CREATE INDEX idx_val ON t(val)")

    def tearDown(self):
        self.db.close()

    def test_finds_explicit_index(self):
        idx = find_eq_index(self.db, "t", "val")
        self.assertIsNotNone(idx)
        self.assertEqual(idx.columns[0], "val")

    def test_finds_pk_index(self):
        idx = find_eq_index(self.db, "t", "id")
        self.assertIsNotNone(idx)

    def test_returns_none_for_unindexed_column(self):
        sql(self.db, "CREATE TABLE t2 (x INTEGER, y INTEGER)")
        idx = find_eq_index(self.db, "t2", "x")
        self.assertIsNone(idx)

    def test_qualified_col_name_works(self):
        idx = find_eq_index(self.db, "t", "t.val")
        self.assertIsNotNone(idx)


# ── probe_index ───────────────────────────────────────────────────────────────

class TestProbeIndex(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        sql(self.db, "CREATE TABLE t (id INTEGER PRIMARY KEY, val INTEGER, name VARCHAR(32))")
        for i in range(10):
            sql(self.db, f"INSERT INTO t VALUES ({i}, {i * 10}, 'n{i}')")

    def tearDown(self):
        self.db.close()

    def test_returns_matching_rows(self):
        rows = probe_index(self.db, "t", "id", 5)
        self.assertIsNotNone(rows)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], 5)
        self.assertEqual(rows[0]["name"], "n5")

    def test_returns_empty_for_no_match(self):
        rows = probe_index(self.db, "t", "id", 999)
        self.assertIsNotNone(rows)
        self.assertEqual(rows, [])

    def test_returns_none_for_no_index(self):
        rows = probe_index(self.db, "t", "val", 50)
        self.assertIsNone(rows)

    def test_multiple_matches_via_non_unique_index(self):
        sql(self.db, "CREATE TABLE m (id INTEGER, category INTEGER)")
        sql(self.db, "CREATE INDEX idx_cat ON m(category)")
        for i in range(5):
            for cat in (1, 2):
                sql(self.db, f"INSERT INTO m VALUES ({i * 2 + cat}, {cat})")
        rows = probe_index(self.db, "m", "category", 1)
        self.assertIsNotNone(rows)
        self.assertEqual(len(rows), 5)


# ── INLJ correctness in 2-table join ─────────────────────────────────────────

class TestINLJJoin(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        sql(self.db, "CREATE TABLE orders (id INTEGER PRIMARY KEY, cust_id INTEGER, amount INTEGER)")
        sql(self.db, "CREATE TABLE customers (id INTEGER PRIMARY KEY, name VARCHAR(32))")
        for i in range(1, 6):
            sql(self.db, f"INSERT INTO customers VALUES ({i}, 'cust{i}')")
        for i in range(1, 11):
            sql(self.db, f"INSERT INTO orders VALUES ({i}, {(i % 5) + 1}, {i * 100})")

    def tearDown(self):
        self.db.close()

    def test_inlj_inner_join_result_correct(self):
        result = sql(self.db, """
            SELECT o.id, c.name
            FROM orders AS o JOIN customers AS c ON o.cust_id = c.id
            ORDER BY o.id
        """)
        self.assertIn("cust1", result)
        self.assertIn("cust5", result)
        # 10 orders, all have matching customers
        self.assertIn("(10 rows)", result)

    def test_join_without_index_still_correct(self):
        # customers.name has no index — should use nested loop but still be correct
        sql(self.db, "CREATE TABLE t (id INTEGER, tag VARCHAR(8))")
        sql(self.db, "CREATE TABLE u (id INTEGER, tag VARCHAR(8))")
        for i in range(3):
            sql(self.db, f"INSERT INTO t VALUES ({i}, 'x')")
            sql(self.db, f"INSERT INTO u VALUES ({i}, 'x')")
        result = sql(self.db, "SELECT t.id, u.id FROM t JOIN u ON t.id = u.id ORDER BY t.id")
        self.assertIn("(3 rows)", result)

    def test_inlj_null_join_key_skipped(self):
        # NULL on left side of join should not match anything
        sql(self.db, "CREATE TABLE left_t (id INTEGER, ref INTEGER)")
        sql(self.db, "CREATE TABLE right_t (id INTEGER PRIMARY KEY)")
        sql(self.db, "INSERT INTO left_t VALUES (1, NULL)")
        sql(self.db, "INSERT INTO left_t VALUES (2, 1)")
        sql(self.db, "INSERT INTO right_t VALUES (1)")
        result = sql(self.db, """
            SELECT left_t.id, right_t.id
            FROM left_t JOIN right_t ON left_t.ref = right_t.id
        """)
        self.assertIn("(1 row)", result)

    def test_left_outer_join_not_inlj(self):
        """LEFT JOIN should still work correctly (falls back to nested loop)."""
        sql(self.db, "CREATE TABLE a (id INTEGER)")
        sql(self.db, "CREATE TABLE b (a_id INTEGER PRIMARY KEY, val INTEGER)")
        for i in range(3):
            sql(self.db, f"INSERT INTO a VALUES ({i})")
        sql(self.db, "INSERT INTO b VALUES (1, 100)")
        result = sql(self.db, "SELECT a.id, b.val FROM a LEFT JOIN b ON a.id = b.a_id ORDER BY a.id")
        # Rows 0 and 2 have no matching b — should appear with NULL val
        self.assertIn("0", result)
        self.assertIn("2", result)
        self.assertIn("(3 rows)", result)


# ── 3-way join correctness ────────────────────────────────────────────────────

class TestMultiJoinCorrectness(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        sql(self.db, "CREATE TABLE dept (id INTEGER PRIMARY KEY, name VARCHAR(32))")
        sql(self.db, "CREATE TABLE emp (id INTEGER PRIMARY KEY, dept_id INTEGER, name VARCHAR(32))")
        sql(self.db, "CREATE TABLE proj (id INTEGER PRIMARY KEY, emp_id INTEGER, title VARCHAR(32))")
        sql(self.db, "INSERT INTO dept VALUES (1, 'Eng'), (2, 'Sales')")
        sql(self.db, "INSERT INTO emp VALUES (1, 1, 'Alice'), (2, 1, 'Bob'), (3, 2, 'Carol')")
        sql(self.db, "INSERT INTO proj VALUES (1, 1, 'Alpha'), (2, 2, 'Beta'), (3, 1, 'Gamma')")

    def tearDown(self):
        self.db.close()

    def test_three_way_inner_join_correct(self):
        result = sql(self.db, """
            SELECT dept.name, emp.name, proj.title
            FROM dept
            JOIN emp ON dept.id = emp.dept_id
            JOIN proj ON emp.id = proj.emp_id
            ORDER BY proj.title
        """)
        self.assertIn("Alpha", result)
        self.assertIn("Beta", result)
        self.assertIn("Gamma", result)
        self.assertIn("(3 rows)", result)

    def test_three_way_no_cross_contamination(self):
        result = sql(self.db, """
            SELECT dept.name, emp.name, proj.title
            FROM dept
            JOIN emp ON dept.id = emp.dept_id
            JOIN proj ON emp.id = proj.emp_id
            WHERE dept.id = 1
        """)
        # Sales employees (Carol) should not appear
        self.assertNotIn("Carol", result)
        self.assertIn("Alice", result)
        self.assertIn("Bob", result)


# ── optimize_join function ────────────────────────────────────────────────────

class TestOptimizeJoin(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        # tiny (3 rows), medium (20 rows, B has PK index on id + index on a_id), big (100 rows)
        sql(self.db, "CREATE TABLE big (id INTEGER, val INTEGER)")
        sql(self.db, "CREATE TABLE medium (id INTEGER PRIMARY KEY, a_id INTEGER)")
        sql(self.db, "CREATE INDEX idx_med_aid ON medium(a_id)")
        sql(self.db, "CREATE TABLE tiny (id INTEGER, m_id INTEGER)")
        sql(self.db, "CREATE INDEX idx_tiny_mid ON tiny(m_id)")
        for i in range(100):
            sql(self.db, f"INSERT INTO big VALUES ({i}, {i})")
        for i in range(20):
            sql(self.db, f"INSERT INTO medium VALUES ({i}, {i})")
        for i in range(3):
            sql(self.db, f"INSERT INTO tiny VALUES ({i}, {i})")
        # Seed the row count cache
        estimate_rows(self.db, "big")
        estimate_rows(self.db, "medium")
        estimate_rows(self.db, "tiny")

    def tearDown(self):
        self.db.close()

    def test_reorders_when_reverse_probe_is_cheaper(self):
        """
        big_pk (50 rows, PK on id) JOIN small_ni (5 rows, no index on big_id):
          Cost forward  big_pk → small_ni: 50 * 5 = 250  (nested loop, no index on small_ni.big_id)
          Cost reversed small_ni → big_pk: 5 * log2(51) ≈ 28 (INLJ via big_pk's PK index)
        Optimizer must pick small_ni as the driving table.
        """
        sql(self.db, "CREATE TABLE big_pk (id INTEGER PRIMARY KEY, val INTEGER)")
        sql(self.db, "CREATE TABLE small_ni (id INTEGER, big_id INTEGER)")
        for i in range(50):
            sql(self.db, f"INSERT INTO big_pk VALUES ({i}, {i})")
        for i in range(5):
            sql(self.db, f"INSERT INTO small_ni VALUES ({i}, {i})")
        stmt = parse(
            "SELECT big_pk.id, small_ni.id "
            "FROM big_pk JOIN small_ni ON big_pk.id = small_ni.big_id"
        )
        opt = optimize_join(stmt, self.db)
        self.assertEqual(opt["left_table"], "small_ni",
            f"Expected 'small_ni' as driving table, got '{opt['left_table']}'")

    def test_optimized_plan_produces_correct_results(self):
        result = sql(self.db, """
            SELECT big.id, medium.id, tiny.id
            FROM big
            JOIN medium ON big.id = medium.a_id
            JOIN tiny ON medium.id = tiny.m_id
            ORDER BY tiny.id
        """)
        self.assertIn("(3 rows)", result)

    def test_non_inner_join_not_reordered(self):
        stmt = parse("""
            SELECT big.id, medium.id
            FROM big LEFT JOIN medium ON big.id = medium.a_id
        """)
        opt = optimize_join(stmt, self.db)
        # LEFT JOIN: must keep original order
        self.assertEqual(opt["left_table"], "big")
        self.assertEqual(opt["right_table"], "medium")

    def test_two_table_inner_join_reorder_considered(self):
        # For a 2-table join, optimizer considers swapping; result must still be correct
        result = sql(self.db, """
            SELECT big.id, medium.id
            FROM big JOIN medium ON big.id = medium.a_id
            ORDER BY big.id
        """)
        # All 20 medium rows have matching big rows (big has 0..99, medium.a_id = 0..19)
        self.assertIn("(20 rows)", result)


if __name__ == "__main__":
    unittest.main()
