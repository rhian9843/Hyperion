# Tests for: COUNT/SUM DISTINCT aggregates and expression indexes
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hyperion.database import Database
from hyperion.executor import execute
from hyperion.parser import parse


def sql(db: Database, stmt: str) -> str:
    return execute(parse(stmt), db)


# ── COUNT(DISTINCT) / SUM(DISTINCT) ──────────────────────────────────────────

class TestDistinctAggregates(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        sql(self.db, "CREATE TABLE t (id INTEGER, cat VARCHAR(8), val INTEGER)")
        # 6 rows: cat repeats (a,a,b,b,c,c), val repeats (1,1,2,2,3,3)
        for i, (c, v) in enumerate([("a", 1), ("a", 1), ("b", 2), ("b", 2), ("c", 3), ("c", 3)]):
            sql(self.db, f"INSERT INTO t VALUES ({i}, '{c}', {v})")

    def tearDown(self):
        self.db.close()

    def test_count_distinct_col(self):
        result = sql(self.db, "SELECT COUNT(DISTINCT cat) FROM t")
        self.assertIn("3", result)

    def test_count_distinct_vs_plain_count(self):
        plain    = sql(self.db, "SELECT COUNT(cat) FROM t")
        distinct = sql(self.db, "SELECT COUNT(DISTINCT cat) FROM t")
        self.assertIn("6", plain)
        self.assertIn("3", distinct)

    def test_sum_distinct(self):
        # val: 1,1,2,2,3,3 → SUM(DISTINCT val) = 1+2+3 = 6
        result = sql(self.db, "SELECT SUM(DISTINCT val) FROM t")
        self.assertIn("6", result)

    def test_sum_distinct_vs_plain_sum(self):
        plain    = sql(self.db, "SELECT SUM(val) FROM t")
        distinct = sql(self.db, "SELECT SUM(DISTINCT val) FROM t")
        self.assertIn("12", plain)
        self.assertIn("6", distinct)

    def test_avg_distinct(self):
        # val: 1,1,2,2,3,3 → AVG(DISTINCT val) = (1+2+3)/3 = 2.0
        result = sql(self.db, "SELECT AVG(DISTINCT val) FROM t")
        self.assertIn("2", result)

    def test_min_distinct_same_as_plain(self):
        # Column header differs; value must be the same
        self.assertIn("1", sql(self.db, "SELECT MIN(val) FROM t"))
        self.assertIn("1", sql(self.db, "SELECT MIN(DISTINCT val) FROM t"))

    def test_max_distinct_same_as_plain(self):
        self.assertIn("3", sql(self.db, "SELECT MAX(val) FROM t"))
        self.assertIn("3", sql(self.db, "SELECT MAX(DISTINCT val) FROM t"))

    def test_count_distinct_excludes_nulls(self):
        sql(self.db, "CREATE TABLE n (val INTEGER)")
        for v in [1, 1, None, None]:
            if v is None:
                sql(self.db, "INSERT INTO n VALUES (NULL)")
            else:
                sql(self.db, f"INSERT INTO n VALUES ({v})")
        # Only 1 distinct non-null value
        result = sql(self.db, "SELECT COUNT(DISTINCT val) FROM n")
        self.assertIn("1", result)

    def test_count_distinct_in_group_by(self):
        # GROUP BY cat: each group has 1 distinct val
        result = sql(self.db, "SELECT cat, COUNT(DISTINCT val) FROM t GROUP BY cat ORDER BY cat")
        self.assertIn("(3 rows)", result)
        data_lines = [l for l in result.splitlines() if "|" in l and "cat" not in l]
        for line in data_lines:
            self.assertIn("1", line)

    def test_group_concat_distinct(self):
        result = sql(self.db, "SELECT GROUP_CONCAT(DISTINCT cat) FROM t")
        # Each of a, b, c must appear exactly once
        self.assertIn("a", result)
        self.assertIn("b", result)
        self.assertIn("c", result)
        # Find the data value — it's the non-header, non-separator, non-count line
        data = [l.strip() for l in result.splitlines()
                if l.strip() and not l.startswith("-") and "GROUP" not in l
                and "row" not in l]
        self.assertTrue(data)
        csv = data[0].strip("|").strip()
        self.assertEqual(len(csv.split(",")), 3)


# ── Expression indexes ────────────────────────────────────────────────────────

class TestExpressionIndexCreate(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        sql(self.db, "CREATE TABLE t (id INTEGER PRIMARY KEY, name VARCHAR(64))")

    def tearDown(self):
        self.db.close()

    def test_create_expression_index(self):
        result = sql(self.db, "CREATE INDEX idx_upper ON t(UPPER(name))")
        self.assertIn("idx_upper", result)
        self.assertIn("idx_upper", self.db.indexes)

    def test_expression_stored_in_catalog(self):
        sql(self.db, "CREATE INDEX idx_upper ON t(UPPER(name))")
        idx = self.db.indexes["idx_upper"]
        self.assertEqual(idx.columns, ["UPPER(name)"])

    def test_create_if_not_exists(self):
        sql(self.db, "CREATE INDEX idx_upper ON t(UPPER(name))")
        result = sql(self.db, "CREATE INDEX IF NOT EXISTS idx_upper ON t(UPPER(name))")
        self.assertIn("already exists", result)

    def test_drop_expression_index(self):
        sql(self.db, "CREATE INDEX idx_upper ON t(UPPER(name))")
        sql(self.db, "DROP INDEX idx_upper")
        self.assertNotIn("idx_upper", self.db.indexes)

    def test_pragma_index_info_shows_expression(self):
        sql(self.db, "CREATE INDEX idx_upper ON t(UPPER(name))")
        result = sql(self.db, "PRAGMA index_info(idx_upper)")
        self.assertIn("UPPER(name)", result)


class TestExpressionIndexMaintenance(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        sql(self.db, "CREATE TABLE t (id INTEGER PRIMARY KEY, name VARCHAR(64))")
        sql(self.db, "CREATE INDEX idx_upper ON t(UPPER(name))")

    def tearDown(self):
        self.db.close()

    def _idx_count(self) -> int:
        idx_meta = self.db.indexes["idx_upper"]
        return sum(1 for _ in self.db._index_btree(idx_meta).scan())

    def test_insert_populates_index(self):
        sql(self.db, "INSERT INTO t VALUES (1, 'alice')")
        sql(self.db, "INSERT INTO t VALUES (2, 'Bob')")
        self.assertEqual(self._idx_count(), 2)

    def test_delete_removes_from_index(self):
        sql(self.db, "INSERT INTO t VALUES (1, 'alice')")
        sql(self.db, "INSERT INTO t VALUES (2, 'bob')")
        sql(self.db, "DELETE FROM t WHERE id = 1")
        self.assertEqual(self._idx_count(), 1)

    def test_update_refreshes_index(self):
        sql(self.db, "INSERT INTO t VALUES (1, 'alice')")
        sql(self.db, "UPDATE t SET name = 'ALICE' WHERE id = 1")
        self.assertEqual(self._idx_count(), 1)

    def test_index_built_on_existing_rows(self):
        sql(self.db, "DROP INDEX idx_upper")
        sql(self.db, "INSERT INTO t VALUES (1, 'alice')")
        sql(self.db, "INSERT INTO t VALUES (2, 'bob')")
        sql(self.db, "CREATE INDEX idx_upper ON t(UPPER(name))")
        self.assertEqual(self._idx_count(), 2)

    def test_null_value_not_indexed(self):
        sql(self.db, "CREATE TABLE nullable (id INTEGER, name VARCHAR(64))")
        sql(self.db, "CREATE INDEX idx_n ON nullable(UPPER(name))")
        sql(self.db, "INSERT INTO nullable VALUES (1, NULL)")
        idx_meta = self.db.indexes["idx_n"]
        entries = list(self.db._index_btree(idx_meta).scan())
        self.assertEqual(len(entries), 0)


class TestExpressionIndexQueryCorrectness(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        sql(self.db, "CREATE TABLE t (id INTEGER PRIMARY KEY, name VARCHAR(64))")
        sql(self.db, "CREATE INDEX idx_upper ON t(UPPER(name))")
        for i, name in enumerate(["alice", "Bob", "CAROL", "dave"]):
            sql(self.db, f"INSERT INTO t VALUES ({i+1}, '{name}')")

    def tearDown(self):
        self.db.close()

    def test_select_all_still_correct(self):
        result = sql(self.db, "SELECT COUNT(*) FROM t")
        self.assertIn("4", result)

    def test_where_on_expression(self):
        result = sql(self.db, "SELECT id, name FROM t WHERE UPPER(name) = 'ALICE'")
        self.assertIn("alice", result)
        self.assertIn("(1 row)", result)

    def test_lower_expression_index(self):
        sql(self.db, "CREATE INDEX idx_lower ON t(LOWER(name))")
        sql(self.db, "INSERT INTO t VALUES (10, 'Frank')")
        result = sql(self.db, "SELECT name FROM t WHERE LOWER(name) = 'frank'")
        self.assertIn("Frank", result)

    def test_multiple_rows_match(self):
        sql(self.db, "INSERT INTO t VALUES (10, 'Alice')")
        result = sql(self.db, "SELECT id FROM t WHERE UPPER(name) = 'ALICE'")
        self.assertIn("(2 rows)", result)

    def test_insert_after_index_creation_is_correct(self):
        sql(self.db, "INSERT INTO t VALUES (20, 'zara')")
        result = sql(self.db, "SELECT name FROM t WHERE UPPER(name) = 'ZARA'")
        self.assertIn("zara", result)


if __name__ == "__main__":
    unittest.main()
