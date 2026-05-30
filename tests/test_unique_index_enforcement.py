"""Tests for CREATE UNIQUE INDEX enforcement on INSERT and UPDATE."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hyperion.database import Database
from hyperion.executor import execute
from hyperion.parser import parse
from hyperion.errors import UniqueConstraintError


def sql(db: Database, stmt: str):
    return execute(parse(stmt), db)


class TestUniqueIndexBasic(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        sql(self.db, "CREATE TABLE t (id INTEGER, v TEXT)")
        sql(self.db, "CREATE UNIQUE INDEX uidx ON t(v)")

    def tearDown(self):
        self.db.close()

    def test_first_insert_succeeds(self):
        sql(self.db, "INSERT INTO t VALUES (1, 'hello')")

    def test_duplicate_raises(self):
        sql(self.db, "INSERT INTO t VALUES (1, 'hello')")
        with self.assertRaises(UniqueConstraintError):
            sql(self.db, "INSERT INTO t VALUES (2, 'hello')")

    def test_distinct_values_allowed(self):
        sql(self.db, "INSERT INTO t VALUES (1, 'hello')")
        sql(self.db, "INSERT INTO t VALUES (2, 'world')")

    def test_null_not_enforced(self):
        sql(self.db, "INSERT INTO t VALUES (1, NULL)")
        sql(self.db, "INSERT INTO t VALUES (2, NULL)")

    def test_update_duplicate_raises(self):
        sql(self.db, "INSERT INTO t VALUES (1, 'hello')")
        sql(self.db, "INSERT INTO t VALUES (2, 'world')")
        with self.assertRaises(UniqueConstraintError):
            sql(self.db, "UPDATE t SET v = 'hello' WHERE id = 2")

    def test_update_same_value_no_raise(self):
        sql(self.db, "INSERT INTO t VALUES (1, 'hello')")
        sql(self.db, "UPDATE t SET v = 'hello' WHERE id = 1")

    def test_drop_index_removes_enforcement(self):
        sql(self.db, "INSERT INTO t VALUES (1, 'hello')")
        sql(self.db, "DROP INDEX uidx")
        sql(self.db, "INSERT INTO t VALUES (2, 'hello')")


class TestUniqueIndexMultiColumn(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        sql(self.db, "CREATE TABLE t (a INTEGER, b INTEGER, c TEXT)")
        sql(self.db, "CREATE UNIQUE INDEX uidx ON t(a, b)")

    def tearDown(self):
        self.db.close()

    def test_same_a_different_b_allowed(self):
        sql(self.db, "INSERT INTO t VALUES (1, 1, 'x')")
        sql(self.db, "INSERT INTO t VALUES (1, 2, 'y')")

    def test_duplicate_pair_raises(self):
        sql(self.db, "INSERT INTO t VALUES (1, 1, 'x')")
        with self.assertRaises(UniqueConstraintError):
            sql(self.db, "INSERT INTO t VALUES (1, 1, 'z')")

    def test_null_in_pair_exempted(self):
        sql(self.db, "INSERT INTO t VALUES (1, NULL, 'x')")
        sql(self.db, "INSERT INTO t VALUES (1, NULL, 'y')")


class TestUniqueIndexPreexistingRows(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        sql(self.db, "CREATE TABLE t (id INTEGER, v TEXT)")
        sql(self.db, "INSERT INTO t VALUES (1, 'hello')")
        sql(self.db, "INSERT INTO t VALUES (2, 'world')")
        sql(self.db, "CREATE UNIQUE INDEX uidx ON t(v)")

    def tearDown(self):
        self.db.close()

    def test_insert_duplicate_after_index_creation_raises(self):
        with self.assertRaises(UniqueConstraintError):
            sql(self.db, "INSERT INTO t VALUES (3, 'hello')")

    def test_insert_new_value_succeeds(self):
        sql(self.db, "INSERT INTO t VALUES (3, 'new')")


class TestUniqueIndexPragma(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        sql(self.db, "CREATE TABLE t (id INTEGER, v TEXT)")
        sql(self.db, "CREATE UNIQUE INDEX uidx ON t(v)")

    def tearDown(self):
        self.db.close()

    def test_pragma_index_list_shows_unique(self):
        result = sql(self.db, "PRAGMA index_list(t)")
        self.assertIn("1", result)  # unique = 1

    def test_index_meta_unique_flag(self):
        idx = self.db.indexes["uidx"]
        self.assertTrue(idx.unique)

    def test_non_unique_index_flag_false(self):
        sql(self.db, "CREATE INDEX nidx ON t(id)")
        idx = self.db.indexes["nidx"]
        self.assertFalse(idx.unique)


class TestUniqueIndexConflictActions(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        sql(self.db, "CREATE TABLE t (id INTEGER, v TEXT)")
        sql(self.db, "CREATE UNIQUE INDEX uidx ON t(v)")
        sql(self.db, "INSERT INTO t VALUES (1, 'hello')")

    def tearDown(self):
        self.db.close()

    def test_insert_or_ignore_skips_duplicate(self):
        sql(self.db, "INSERT OR IGNORE INTO t VALUES (2, 'hello')")
        result = sql(self.db, "SELECT COUNT(*) FROM t")
        self.assertIn("1", result)

    def test_insert_or_replace_replaces_row(self):
        sql(self.db, "INSERT OR REPLACE INTO t VALUES (2, 'hello')")
        result = sql(self.db, "SELECT COUNT(*) FROM t")
        self.assertIn("1", result)
        result2 = sql(self.db, "SELECT id FROM t WHERE v = 'hello'")
        self.assertIn("2", result2)

    def test_on_conflict_do_nothing_skips(self):
        sql(self.db, "INSERT INTO t VALUES (2, 'hello') ON CONFLICT DO NOTHING")
        result = sql(self.db, "SELECT COUNT(*) FROM t")
        self.assertIn("1", result)


class TestUniqueIndexAfterAlter(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        sql(self.db, "CREATE TABLE t (id INTEGER, v TEXT)")
        sql(self.db, "CREATE UNIQUE INDEX uidx ON t(v)")
        sql(self.db, "INSERT INTO t VALUES (1, 'hello')")

    def tearDown(self):
        self.db.close()

    def test_enforcement_survives_alter_add_column(self):
        sql(self.db, "ALTER TABLE t ADD COLUMN extra INTEGER")
        with self.assertRaises(UniqueConstraintError):
            sql(self.db, "INSERT INTO t VALUES (2, 'hello', 99)")

    def test_enforcement_survives_alter_rename_column(self):
        sql(self.db, "ALTER TABLE t RENAME COLUMN v TO value")
        with self.assertRaises(UniqueConstraintError):
            sql(self.db, "INSERT INTO t VALUES (2, 'hello')")

    def test_enforcement_survives_alter_column_type(self):
        sql(self.db, "ALTER TABLE t ALTER COLUMN id TYPE REAL")
        with self.assertRaises(UniqueConstraintError):
            sql(self.db, "INSERT INTO t VALUES (2, 'hello')")


if __name__ == "__main__":
    unittest.main()
