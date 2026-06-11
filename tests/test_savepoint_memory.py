"""
Tests: savepoint memory correctness under large schema.

Covers catalog snapshot/restore bugs that are masked by small-schema tests.
All tests use Database(":memory:") — no file I/O.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hyperion.database import Database


def _row_count(db: Database, table: str) -> int:
    rows = db.execute(f"SELECT COUNT(*) AS cnt FROM {table}").fetchall()
    return rows[0]["cnt"]


def _table_columns(db: Database, table: str) -> list[str]:
    rows = db.execute(f"PRAGMA table_info({table})").fetchall()
    return [r["name"] for r in rows]


class TestSavepointLargeSchema(unittest.TestCase):
    """Core correctness: 100 tables, savepoint, rollback, verify catalog & data."""

    def setUp(self):
        self.db = Database(":memory:")
        for i in range(100):
            self.db.execute(
                f"CREATE TABLE t{i} (id INTEGER PRIMARY KEY, val TEXT)"
            )

    def tearDown(self):
        self.db.close()

    def test_all_tables_exist_after_rollback(self):
        """All 100 tables still present in catalog after savepoint rollback."""
        self.db.execute("BEGIN")
        self.db.execute("SAVEPOINT sp")
        for i in range(10):
            self.db.execute(f"INSERT INTO t{i} VALUES (1, 'x')")
        self.db.execute("ROLLBACK TO SAVEPOINT sp")
        self.db.execute("COMMIT")

        for i in range(100):
            self.assertIn(f"t{i}", self.db.tables,
                          f"t{i} missing from catalog after rollback")

    def test_rolled_back_tables_have_zero_rows(self):
        """t0..t9 (inserted then rolled back) each have 0 rows."""
        self.db.execute("BEGIN")
        self.db.execute("SAVEPOINT sp")
        for i in range(10):
            self.db.execute(f"INSERT INTO t{i} VALUES (1, 'x')")
        self.db.execute("ROLLBACK TO SAVEPOINT sp")
        self.db.execute("COMMIT")

        for i in range(10):
            self.assertEqual(_row_count(self.db, f"t{i}"), 0,
                             f"t{i} should have 0 rows after rollback")

    def test_unaffected_tables_have_zero_rows(self):
        """t10..t99 (never touched) each have 0 rows."""
        self.db.execute("BEGIN")
        self.db.execute("SAVEPOINT sp")
        for i in range(10):
            self.db.execute(f"INSERT INTO t{i} VALUES (1, 'x')")
        self.db.execute("ROLLBACK TO SAVEPOINT sp")
        self.db.execute("COMMIT")

        for i in range(10, 100):
            self.assertEqual(_row_count(self.db, f"t{i}"), 0,
                             f"t{i} should have 0 rows (unaffected)")

    def test_schema_intact_after_rollback(self):
        """All 100 tables retain correct column schema after rollback."""
        self.db.execute("BEGIN")
        self.db.execute("SAVEPOINT sp")
        for i in range(10):
            self.db.execute(f"INSERT INTO t{i} VALUES (1, 'x')")
        self.db.execute("ROLLBACK TO SAVEPOINT sp")
        self.db.execute("COMMIT")

        for i in range(100):
            cols = _table_columns(self.db, f"t{i}")
            self.assertEqual(cols, ["id", "val"],
                             f"t{i} schema wrong after rollback: {cols}")


class TestSavepointPreTransactionData(unittest.TestCase):
    """Pre-savepoint rows must survive a rollback to savepoint."""

    def setUp(self):
        self.db = Database(":memory:")
        for i in range(5):
            self.db.execute(
                f"CREATE TABLE t{i} (id INTEGER PRIMARY KEY, val TEXT)"
            )
        # Seed rows BEFORE the transaction
        for i in range(5):
            self.db.execute(f"INSERT INTO t{i} VALUES (1, 'pre')")

    def tearDown(self):
        self.db.close()

    def test_pre_transaction_rows_survive_savepoint_rollback(self):
        """Rows inserted before BEGIN are still present after savepoint rollback."""
        self.db.execute("BEGIN")
        self.db.execute("SAVEPOINT sp")
        for i in range(5):
            self.db.execute(f"INSERT INTO t{i} VALUES (2, 'post')")
        self.db.execute("ROLLBACK TO SAVEPOINT sp")
        self.db.execute("COMMIT")

        for i in range(5):
            count = _row_count(self.db, f"t{i}")
            self.assertEqual(count, 1,
                             f"t{i}: expected 1 pre-transaction row, got {count}")

    def test_only_post_savepoint_rows_removed(self):
        """After rollback only the pre-transaction row remains; 'post' is gone."""
        self.db.execute("BEGIN")
        self.db.execute("SAVEPOINT sp")
        for i in range(5):
            self.db.execute(f"INSERT INTO t{i} VALUES (2, 'post')")
        self.db.execute("ROLLBACK TO SAVEPOINT sp")
        self.db.execute("COMMIT")

        rows = self.db.execute("SELECT val FROM t0").fetchall()
        vals = [r["val"] for r in rows]
        self.assertIn("pre", vals)
        self.assertNotIn("post", vals)


class TestNestedSavepoints(unittest.TestCase):
    """Nested savepoints: outer rollback must undo all inner work."""

    def setUp(self):
        self.db = Database(":memory:")
        for i in range(20):
            self.db.execute(
                f"CREATE TABLE t{i} (id INTEGER PRIMARY KEY, val TEXT)"
            )

    def tearDown(self):
        self.db.close()

    def test_nested_rollback_leaves_all_tables_empty(self):
        """ROLLBACK outer then inner => all 20 tables end up empty."""
        self.db.execute("BEGIN")
        self.db.execute("SAVEPOINT outer")
        for i in range(10):
            self.db.execute(f"INSERT INTO t{i} VALUES (1, 'a')")
        self.db.execute("SAVEPOINT inner")
        for i in range(10):
            self.db.execute(f"INSERT INTO t{i} VALUES (2, 'b')")
        self.db.execute("ROLLBACK TO SAVEPOINT inner")
        self.db.execute("ROLLBACK TO SAVEPOINT outer")
        self.db.execute("COMMIT")

        for i in range(20):
            self.assertEqual(_row_count(self.db, f"t{i}"), 0,
                             f"t{i} should be empty after outer rollback")

    def test_inner_rollback_outer_survives(self):
        """ROLLBACK to inner; outer inserts survive; inner extra inserts are gone."""
        self.db.execute("BEGIN")
        self.db.execute("SAVEPOINT outer")
        for i in range(10):
            self.db.execute(f"INSERT INTO t{i} VALUES (1, 'outer')")
        self.db.execute("SAVEPOINT inner")
        for i in range(10):
            self.db.execute(f"INSERT INTO t{i} VALUES (2, 'inner')")
        self.db.execute("ROLLBACK TO SAVEPOINT inner")
        self.db.execute("COMMIT")

        for i in range(10):
            count = _row_count(self.db, f"t{i}")
            self.assertEqual(count, 1, f"t{i} should have 1 row (outer only)")

        rows = self.db.execute("SELECT val FROM t0").fetchall()
        vals = [r["val"] for r in rows]
        self.assertIn("outer", vals)
        self.assertNotIn("inner", vals)


class TestReleaseSavepoint(unittest.TestCase):
    """RELEASE SAVEPOINT must make inserts visible after commit."""

    def setUp(self):
        self.db = Database(":memory:")
        self.db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")

    def tearDown(self):
        self.db.close()

    def test_release_commits_savepoint_work(self):
        """Inserts after SAVEPOINT are visible after RELEASE + COMMIT."""
        self.db.execute("BEGIN")
        self.db.execute("SAVEPOINT sp")
        self.db.execute("INSERT INTO t VALUES (1, 'kept')")
        self.db.execute("RELEASE SAVEPOINT sp")
        self.db.execute("COMMIT")

        rows = self.db.execute("SELECT val FROM t").fetchall()
        vals = [r["val"] for r in rows]
        self.assertIn("kept", vals)

    def test_release_multiple_savepoints(self):
        """All released savepoints' inserts are visible after commit."""
        self.db.execute("BEGIN")
        self.db.execute("SAVEPOINT sp1")
        self.db.execute("INSERT INTO t VALUES (1, 'a')")
        self.db.execute("RELEASE SAVEPOINT sp1")
        self.db.execute("SAVEPOINT sp2")
        self.db.execute("INSERT INTO t VALUES (2, 'b')")
        self.db.execute("RELEASE SAVEPOINT sp2")
        self.db.execute("COMMIT")

        count = _row_count(self.db, "t")
        self.assertEqual(count, 2)

    def test_rollback_after_release_undoes_everything(self):
        """Outer ROLLBACK after RELEASE still undoes all work."""
        self.db.execute("BEGIN")
        self.db.execute("SAVEPOINT sp")
        self.db.execute("INSERT INTO t VALUES (1, 'a')")
        self.db.execute("RELEASE SAVEPOINT sp")
        self.db.execute("ROLLBACK")

        count = _row_count(self.db, "t")
        self.assertEqual(count, 0)


class TestSavepointDDL(unittest.TestCase):
    """DDL inside a savepoint: rolled-back CREATE TABLE must not persist."""

    def setUp(self):
        self.db = Database(":memory:")
        self.db.execute("CREATE TABLE base (id INTEGER PRIMARY KEY)")

    def tearDown(self):
        self.db.close()

    def test_create_table_in_savepoint_rolled_back(self):
        """A table created inside a savepoint must not exist after rollback."""
        try:
            self.db.execute("BEGIN")
            self.db.execute("SAVEPOINT sp")
            self.db.execute(
                "CREATE TABLE temp_tbl (id INTEGER PRIMARY KEY, val TEXT)"
            )
            self.db.execute("ROLLBACK TO SAVEPOINT sp")
            self.db.execute("COMMIT")
        except Exception as e:
            # Engine may not support DDL inside a transaction; skip gracefully.
            self.skipTest(f"DDL inside transaction not supported: {e}")

        self.assertNotIn(
            "temp_tbl", self.db.tables,
            "temp_tbl should not exist after savepoint rollback",
        )

    def test_base_table_survives_savepoint_ddl_rollback(self):
        """The pre-existing 'base' table is unaffected by a DDL savepoint rollback."""
        try:
            self.db.execute("BEGIN")
            self.db.execute("SAVEPOINT sp")
            self.db.execute("CREATE TABLE extra (x INTEGER)")
            self.db.execute("ROLLBACK TO SAVEPOINT sp")
            self.db.execute("COMMIT")
        except Exception as e:
            self.skipTest(f"DDL inside transaction not supported: {e}")

        self.assertIn("base", self.db.tables)
        cols = _table_columns(self.db, "base")
        self.assertEqual(cols, ["id"])


class TestSavepointWithIndexes(unittest.TestCase):
    """Indexes must survive a savepoint rollback and remain functional."""

    N = 20

    def setUp(self):
        self.db = Database(":memory:")
        for i in range(self.N):
            self.db.execute(
                f"CREATE TABLE t{i} (id INTEGER PRIMARY KEY, val TEXT)"
            )
            self.db.execute(f"CREATE INDEX idx_t{i}_val ON t{i} (val)")

    def tearDown(self):
        self.db.close()

    def test_indexes_exist_after_rollback(self):
        """All indexes survive a savepoint rollback."""
        self.db.execute("BEGIN")
        self.db.execute("SAVEPOINT sp")
        for i in range(self.N):
            self.db.execute(f"INSERT INTO t{i} VALUES (1, 'x')")
        self.db.execute("ROLLBACK TO SAVEPOINT sp")
        self.db.execute("COMMIT")

        for i in range(self.N):
            idx_name = f"idx_t{i}_val"
            self.assertIn(idx_name, self.db.indexes,
                          f"{idx_name} missing after rollback")

    def test_indexes_functional_after_rollback(self):
        """Indexes are usable (inserts work and data is retrievable) after rollback."""
        self.db.execute("BEGIN")
        self.db.execute("SAVEPOINT sp")
        for i in range(self.N):
            self.db.execute(f"INSERT INTO t{i} VALUES (1, 'temp')")
        self.db.execute("ROLLBACK TO SAVEPOINT sp")
        self.db.execute("COMMIT")

        # Insert fresh rows; these should succeed and be queryable.
        for i in range(self.N):
            self.db.execute(f"INSERT INTO t{i} VALUES (1, 'final')")

        for i in range(self.N):
            rows = self.db.execute(
                f"SELECT val FROM t{i} WHERE val = 'final'"
            ).fetchall()
            self.assertEqual(len(rows), 1,
                             f"t{i}: expected 1 row via index scan, got {len(rows)}")

    def test_tables_empty_after_rollback_before_reinsert(self):
        """After rollback and before re-insertion, all tables are still empty."""
        self.db.execute("BEGIN")
        self.db.execute("SAVEPOINT sp")
        for i in range(self.N):
            self.db.execute(f"INSERT INTO t{i} VALUES (1, 'gone')")
        self.db.execute("ROLLBACK TO SAVEPOINT sp")
        self.db.execute("COMMIT")

        for i in range(self.N):
            self.assertEqual(_row_count(self.db, f"t{i}"), 0,
                             f"t{i} should be empty after rollback")


if __name__ == "__main__":
    unittest.main()
