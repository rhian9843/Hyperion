# Tests for: file locking (Pager) and in-memory databases (Database(":memory:"))
import os
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hyperion.database import Database
from hyperion.executor import execute
from hyperion.parser import parse
from hyperion.pager import MemoryPager


def sql(db: Database, stmt: str) -> str:
    return execute(parse(stmt), db)


class TempDB:
    def __enter__(self):
        import tempfile as _tmp
        self._f = _tmp.NamedTemporaryFile(suffix=".db", delete=False)
        self._f.close()
        os.unlink(self._f.name)
        return Path(self._f.name)

    def __exit__(self, *_):
        for ext in (".db", ".wal"):
            p = Path(str(self._f.name).replace(".db", ext))
            try:
                p.unlink()
            except FileNotFoundError:
                pass


# ── In-memory database ────────────────────────────────────────────────────────

class TestInMemoryDatabase(unittest.TestCase):
    def test_uses_memory_pager(self):
        db = Database(":memory:")
        self.assertIsInstance(db._pager, MemoryPager)
        db.close()

    def test_basic_crud(self):
        db = Database(":memory:")
        sql(db, "CREATE TABLE t (id INTEGER, val VARCHAR(32))")
        sql(db, "INSERT INTO t VALUES (1, 'hello')")
        sql(db, "INSERT INTO t VALUES (2, 'world')")
        result = sql(db, "SELECT * FROM t")
        self.assertIn("hello", result)
        self.assertIn("world", result)
        db.close()

    def test_no_file_created(self):
        db = Database(":memory:")
        sql(db, "CREATE TABLE t (id INTEGER)")
        sql(db, "INSERT INTO t VALUES (1)")
        db.close()
        self.assertFalse(Path(":memory:").exists())

    def test_rollback_works(self):
        db = Database(":memory:")
        sql(db, "CREATE TABLE t (id INTEGER, val VARCHAR(32))")
        sql(db, "INSERT INTO t VALUES (1, 'keep')")
        sql(db, "BEGIN")
        sql(db, "INSERT INTO t VALUES (2, 'discard')")
        sql(db, "ROLLBACK")
        result = sql(db, "SELECT val FROM t")
        self.assertIn("keep", result)
        self.assertNotIn("discard", result)
        db.close()

    def test_two_independent_memory_dbs(self):
        db1 = Database(":memory:")
        db2 = Database(":memory:")
        sql(db1, "CREATE TABLE t (id INTEGER)")
        sql(db1, "INSERT INTO t VALUES (42)")
        # db2 is completely independent — has no table 't'
        self.assertNotIn("t", db2.tables)
        db1.close()
        db2.close()

    def test_delete_and_update(self):
        db = Database(":memory:")
        sql(db, "CREATE TABLE t (id INTEGER, val INTEGER)")
        for i in range(5):
            sql(db, f"INSERT INTO t VALUES ({i}, {i * 10})")
        sql(db, "DELETE FROM t WHERE id = 2")
        sql(db, "UPDATE t SET val = 99 WHERE id = 4")
        result = sql(db, "SELECT id, val FROM t ORDER BY id")
        self.assertNotIn("| 2 |", result)
        self.assertIn("99", result)
        db.close()

    def test_vacuum_noop_for_memory(self):
        db = Database(":memory:")
        sql(db, "CREATE TABLE t (id INTEGER)")
        result = sql(db, "VACUUM")
        self.assertIn("vacuumed", result.lower())
        db.close()

    def test_index_works(self):
        db = Database(":memory:")
        sql(db, "CREATE TABLE t (id INTEGER, val INTEGER)")
        sql(db, "CREATE INDEX idx_val ON t(val)")
        for i in range(5):
            sql(db, f"INSERT INTO t VALUES ({i}, {i})")
        result = sql(db, "SELECT COUNT(*) FROM t")
        self.assertIn("5", result)
        db.close()

    def test_create_and_drop_table(self):
        db = Database(":memory:")
        sql(db, "CREATE TABLE t (id INTEGER)")
        self.assertIn("t", db.tables)
        sql(db, "DROP TABLE t")
        self.assertNotIn("t", db.tables)
        db.close()

    def test_transaction_commit_persists(self):
        db = Database(":memory:")
        sql(db, "CREATE TABLE t (id INTEGER)")
        sql(db, "BEGIN")
        sql(db, "INSERT INTO t VALUES (7)")
        sql(db, "COMMIT")
        result = sql(db, "SELECT id FROM t")
        self.assertIn("7", result)
        db.close()


# ── File locking ──────────────────────────────────────────────────────────────

class TestFileLocking(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db_path = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_exclusive_lock_held_during_transaction(self):
        """While a transaction is open, another fd cannot get LOCK_EX (non-blocking)."""
        try:
            import fcntl
        except ImportError:
            self.skipTest("fcntl not available on this platform")

        db = Database(self.db_path)
        db.begin()

        with open(self.db_path, "r+b") as f2:
            try:
                fcntl.flock(f2.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                got_lock = True
                fcntl.flock(f2.fileno(), fcntl.LOCK_UN)
            except BlockingIOError:
                got_lock = False

        db.rollback()
        db.close()
        self.assertFalse(got_lock, "Exclusive lock should not be acquirable during open transaction")

    def test_lock_released_after_close(self):
        """After close, another fd can grab an exclusive lock."""
        try:
            import fcntl
        except ImportError:
            self.skipTest("fcntl not available on this platform")

        db = Database(self.db_path)
        db.begin()
        db.commit()
        db.close()

        with open(self.db_path, "r+b") as f2:
            try:
                fcntl.flock(f2.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                got_lock = True
                fcntl.flock(f2.fileno(), fcntl.LOCK_UN)
            except BlockingIOError:
                got_lock = False
        self.assertTrue(got_lock, "Lock should be released after close")

    def test_write_serialization_across_threads(self):
        """Two threads writing to the same DB file serialize without corruption."""
        db_setup = Database(self.db_path)
        sql(db_setup, "CREATE TABLE t (id INTEGER, val INTEGER)")
        db_setup.close()

        errors: list[Exception] = []

        def writer(val: int, delay: float) -> None:
            try:
                db = Database(self.db_path)
                db.begin()
                time.sleep(delay)
                db.insert("t", {"id": val, "val": val})
                db.commit()
                db.close()
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=writer, args=(1, 0.05))
        t2 = threading.Thread(target=writer, args=(2, 0.0))
        t1.start()
        time.sleep(0.01)
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertEqual(errors, [], f"Thread errors: {errors}")

        db = Database(self.db_path)
        rows = list(db.select("t", None, None))
        db.close()
        self.assertEqual(len(rows), 2)

    def test_sequential_connections(self):
        """Two Database objects opened sequentially on the same file both work."""
        db1 = Database(self.db_path)
        sql(db1, "CREATE TABLE t (id INTEGER, val VARCHAR(32))")
        sql(db1, "INSERT INTO t VALUES (1, 'first')")
        db1.close()

        db2 = Database(self.db_path)
        rows = list(db2.select("t", None, None))
        db2.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["val"], "first")

    def test_concurrent_open_does_not_block(self):
        """Opening a second connection while the first is open must not deadlock.

        Previously Pager.__init__ always acquired LOCK_EX (blocking), so connection B
        would block indefinitely while connection A held LOCK_SH.  The fix uses
        LOCK_EX | LOCK_NB and skips WAL replay if another connection is already live.
        """
        db1 = Database(self.db_path)
        sql(db1, "CREATE TABLE t (id INTEGER)")
        db1.close()

        db_a = Database(self.db_path)
        # db_b must open without hanging even though db_a holds LOCK_SH
        db_b = Database(self.db_path)
        rows_a = list(db_a.select("t", None, None))
        rows_b = list(db_b.select("t", None, None))
        db_a.close()
        db_b.close()
        self.assertEqual(rows_a, [])
        self.assertEqual(rows_b, [])

    def test_readonly_opens_read_only_file(self):
        """Database(path, readonly=True) must open a chmod 444 file without error."""
        db1 = Database(self.db_path)
        sql(db1, "CREATE TABLE t (id INTEGER, v TEXT)")
        sql(db1, "INSERT INTO t VALUES (1, 'hello')")
        db1.close()

        # Make file read-only at the OS level
        self.db_path.chmod(0o444)
        try:
            db_ro = Database(self.db_path, readonly=True)
            rows = list(db_ro.select("t", None, None))
            db_ro.close()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["v"], "hello")
        finally:
            self.db_path.chmod(0o644)  # restore so TearDown can delete it


if __name__ == "__main__":
    unittest.main()
