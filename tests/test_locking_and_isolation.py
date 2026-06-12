"""Tests for SELECT FOR UPDATE and transaction isolation levels."""
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hyperion.database import Database
from hyperion.errors import TransactionError


def _db():
    db = Database(":memory:")
    db.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, balance REAL)")
    db.execute("INSERT INTO accounts VALUES (1, 1000.0)")
    db.execute("INSERT INTO accounts VALUES (2, 500.0)")
    return db


# ── SELECT FOR UPDATE ─────────────────────────────────────────────────────────

class TestSelectForUpdate(unittest.TestCase):

    def test_requires_explicit_transaction(self):
        """FOR UPDATE outside BEGIN raises TransactionError."""
        db = _db()
        with self.assertRaises(TransactionError):
            db.execute("SELECT * FROM accounts WHERE id = 1 FOR UPDATE")

    def test_returns_correct_rows(self):
        """FOR UPDATE returns the same rows as a plain SELECT."""
        db = _db()
        db.execute("BEGIN")
        cur = db.execute("SELECT * FROM accounts WHERE id = 1 FOR UPDATE")
        rows = cur.fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], 1)
        self.assertAlmostEqual(rows[0]["balance"], 1000.0)
        db.execute("COMMIT")

    def test_returns_all_rows_no_where(self):
        """FOR UPDATE on full table returns all rows."""
        db = _db()
        db.execute("BEGIN")
        cur = db.execute("SELECT * FROM accounts FOR UPDATE")
        rows = cur.fetchall()
        self.assertEqual(len(rows), 2)
        db.execute("COMMIT")

    def test_write_lock_held_during_transaction(self):
        """While FOR UPDATE lock is held, _for_update_held=True and depth≥1."""
        db = _db()
        db.execute("BEGIN")
        db.execute("SELECT * FROM accounts WHERE id = 1 FOR UPDATE")
        self.assertTrue(db._for_update_held)
        self.assertGreaterEqual(db._lock._write_depth, 1)
        db.execute("COMMIT")

    def test_lock_released_on_commit(self):
        """After COMMIT, _for_update_held=False and write depth returns to 0."""
        db = _db()
        db.execute("BEGIN")
        db.execute("SELECT * FROM accounts FOR UPDATE")
        db.execute("COMMIT")
        self.assertFalse(db._for_update_held)
        self.assertEqual(db._lock._write_depth, 0)

    def test_lock_released_on_rollback(self):
        """After ROLLBACK, _for_update_held=False and write depth returns to 0."""
        db = _db()
        db.execute("BEGIN")
        db.execute("SELECT * FROM accounts FOR UPDATE")
        db.execute("ROLLBACK")
        self.assertFalse(db._for_update_held)
        self.assertEqual(db._lock._write_depth, 0)

    def test_multiple_for_update_same_transaction_idempotent(self):
        """Two FOR UPDATE calls in the same transaction don't stack write depth."""
        db = _db()
        db.execute("BEGIN")
        db.execute("SELECT * FROM accounts WHERE id = 1 FOR UPDATE")
        db.execute("SELECT * FROM accounts WHERE id = 2 FOR UPDATE")
        # Lock depth should still be 1 (second call is idempotent via _for_update_held)
        self.assertEqual(db._lock._write_depth, 1)
        db.execute("COMMIT")
        self.assertEqual(db._lock._write_depth, 0)

    def test_read_modify_write_pattern(self):
        """Classic read-modify-write: lock row, update, commit, verify new value."""
        db = _db()
        db.execute("BEGIN")
        cur = db.execute("SELECT balance FROM accounts WHERE id = 1 FOR UPDATE")
        balance = cur.fetchone()["balance"]
        new_balance = balance - 200.0
        db.execute(f"UPDATE accounts SET balance = {new_balance} WHERE id = 1")
        db.execute("COMMIT")

        cur = db.execute("SELECT balance FROM accounts WHERE id = 1")
        self.assertAlmostEqual(cur.fetchone()["balance"], 800.0)

    def test_for_update_with_order_by_and_limit(self):
        """FOR UPDATE parses correctly after ORDER BY and LIMIT."""
        db = _db()
        db.execute("BEGIN")
        cur = db.execute(
            "SELECT * FROM accounts ORDER BY balance DESC LIMIT 1 FOR UPDATE"
        )
        rows = cur.fetchall()
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["balance"], 1000.0)
        db.execute("COMMIT")

    def test_concurrent_writer_blocked_until_commit(self):
        """A concurrent UPDATE on the same db is blocked while FOR UPDATE lock is held."""
        db = _db()
        results = []
        barrier = threading.Barrier(2)

        db.execute("BEGIN")
        db.execute("SELECT * FROM accounts FOR UPDATE")

        def writer():
            barrier.wait()
            start = time.monotonic()
            db.execute("UPDATE accounts SET balance = 9999 WHERE id = 2")
            results.append(time.monotonic() - start)

        t = threading.Thread(target=writer)
        t.start()
        barrier.wait()
        time.sleep(0.15)          # hold lock 150 ms
        db.execute("COMMIT")
        t.join(timeout=3.0)

        self.assertTrue(results, "writer thread never completed")
        self.assertGreaterEqual(results[0], 0.10,
            f"writer completed too quickly ({results[0]:.3f}s) — lock was not held")

    def test_database_usable_after_for_update_commit(self):
        """Normal operations work after a FOR UPDATE transaction completes."""
        db = _db()
        db.execute("BEGIN")
        db.execute("SELECT * FROM accounts FOR UPDATE")
        db.execute("COMMIT")

        # Should be able to do a regular SELECT and INSERT
        cur = db.execute("SELECT COUNT(*) AS n FROM accounts")
        self.assertEqual(cur.fetchone()["n"], 2)
        db.execute("INSERT INTO accounts VALUES (3, 250.0)")
        cur = db.execute("SELECT COUNT(*) AS n FROM accounts")
        self.assertEqual(cur.fetchone()["n"], 3)


# ── Transaction isolation levels ──────────────────────────────────────────────

class TestIsolationLevel(unittest.TestCase):

    def test_default_is_read_committed(self):
        """Default isolation level is READ COMMITTED."""
        db = Database(":memory:")
        self.assertEqual(db._isolation_level, "READ COMMITTED")

    def test_set_all_valid_levels(self):
        """All four standard SQL isolation levels are accepted."""
        db = Database(":memory:")
        for level in ("READ UNCOMMITTED", "READ COMMITTED",
                      "REPEATABLE READ", "SERIALIZABLE"):
            db.execute(f"SET TRANSACTION ISOLATION LEVEL {level}")
            self.assertEqual(db._isolation_level, level)

    def test_set_session_transaction_mysql_compat(self):
        """MySQL-style SET SESSION TRANSACTION ISOLATION LEVEL is accepted."""
        db = Database(":memory:")
        db.execute("SET SESSION TRANSACTION ISOLATION LEVEL SERIALIZABLE")
        self.assertEqual(db._isolation_level, "SERIALIZABLE")

    def test_invalid_level_raises(self):
        """An unrecognised level raises a ParseError at parse time."""
        from hyperion.errors import ParseError
        db = Database(":memory:")
        with self.assertRaises(ParseError):
            db.execute("SET TRANSACTION ISOLATION LEVEL SNAPSHOT")

    def test_cannot_set_level_inside_transaction(self):
        """Changing isolation level inside an active transaction raises TransactionError."""
        db = Database(":memory:")
        db.execute("BEGIN")
        with self.assertRaises(TransactionError):
            db.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
        db.execute("ROLLBACK")

    def test_can_set_level_after_commit(self):
        """Can change isolation level after committing a transaction."""
        db = Database(":memory:")
        db.execute("CREATE TABLE t (id INTEGER)")
        db.execute("BEGIN")
        db.execute("INSERT INTO t VALUES (1)")
        db.execute("COMMIT")
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        self.assertEqual(db._isolation_level, "REPEATABLE READ")

    # ── READ COMMITTED (default) behaviour ────────────────────────────────────

    def test_read_committed_no_persistent_lock(self):
        """READ COMMITTED does not hold a write lock across statement boundaries."""
        db = Database(":memory:")
        db.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
        db.execute("CREATE TABLE t (id INTEGER)")
        db.execute("BEGIN")
        self.assertFalse(db._isolation_held)
        self.assertEqual(db._lock._write_depth, 0)
        db.execute("COMMIT")

    # ── REPEATABLE READ behaviour ─────────────────────────────────────────────

    def test_repeatable_read_holds_write_lock_at_begin(self):
        """REPEATABLE READ acquires write lock at BEGIN."""
        db = Database(":memory:")
        db.execute("CREATE TABLE t (id INTEGER)")
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        db.execute("BEGIN")
        self.assertTrue(db._isolation_held)
        self.assertGreaterEqual(db._lock._write_depth, 1)
        db.execute("COMMIT")

    def test_repeatable_read_lock_released_on_commit(self):
        """After COMMIT, REPEATABLE READ write lock is fully released."""
        db = Database(":memory:")
        db.execute("CREATE TABLE t (id INTEGER)")
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        db.execute("BEGIN")
        db.execute("COMMIT")
        self.assertFalse(db._isolation_held)
        self.assertEqual(db._lock._write_depth, 0)

    def test_repeatable_read_lock_released_on_rollback(self):
        """After ROLLBACK, REPEATABLE READ write lock is fully released."""
        db = Database(":memory:")
        db.execute("CREATE TABLE t (id INTEGER)")
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        db.execute("BEGIN")
        db.execute("ROLLBACK")
        self.assertFalse(db._isolation_held)
        self.assertEqual(db._lock._write_depth, 0)

    def test_repeatable_read_blocks_concurrent_writer(self):
        """REPEATABLE READ write lock blocks a concurrent UPDATE from another thread."""
        db = Database(":memory:")
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val INTEGER)")
        db.execute("INSERT INTO t VALUES (1, 0)")
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")

        results = []
        barrier = threading.Barrier(2)

        db.execute("BEGIN")

        def writer():
            barrier.wait()
            start = time.monotonic()
            db.execute("UPDATE t SET val = 99 WHERE id = 1")
            results.append(time.monotonic() - start)

        t = threading.Thread(target=writer)
        t.start()
        barrier.wait()
        time.sleep(0.15)
        db.execute("COMMIT")
        t.join(timeout=3.0)

        self.assertTrue(results, "writer thread never completed")
        self.assertGreaterEqual(results[0], 0.10,
            f"writer was not blocked (elapsed {results[0]:.3f}s)")

    def test_repeatable_read_multiple_transactions(self):
        """Can run multiple REPEATABLE READ transactions sequentially."""
        db = Database(":memory:")
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val INTEGER)")
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")

        for i in range(3):
            db.execute("BEGIN")
            db.execute(f"INSERT INTO t VALUES ({i}, {i * 10})")
            db.execute("COMMIT")
            self.assertFalse(db._isolation_held)
            self.assertEqual(db._lock._write_depth, 0)

        cur = db.execute("SELECT COUNT(*) AS n FROM t")
        self.assertEqual(cur.fetchone()["n"], 3)

    # ── SERIALIZABLE behaviour ────────────────────────────────────────────────

    def test_serializable_holds_write_lock(self):
        """SERIALIZABLE acquires write lock at BEGIN."""
        db = Database(":memory:")
        db.execute("CREATE TABLE t (id INTEGER)")
        db.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
        db.execute("BEGIN")
        self.assertTrue(db._isolation_held)
        db.execute("COMMIT")
        self.assertFalse(db._isolation_held)
        self.assertEqual(db._lock._write_depth, 0)

    def test_serializable_blocks_concurrent_writer(self):
        """SERIALIZABLE write lock blocks concurrent writers."""
        db = Database(":memory:")
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val INTEGER)")
        db.execute("INSERT INTO t VALUES (1, 0)")
        db.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")

        results = []
        barrier = threading.Barrier(2)
        db.execute("BEGIN")

        def writer():
            barrier.wait()
            start = time.monotonic()
            db.execute("UPDATE t SET val = 42 WHERE id = 1")
            results.append(time.monotonic() - start)

        t = threading.Thread(target=writer)
        t.start()
        barrier.wait()
        time.sleep(0.12)
        db.execute("COMMIT")
        t.join(timeout=3.0)

        self.assertTrue(results)
        self.assertGreaterEqual(results[0], 0.08)

    # ── SHOW TRANSACTIONS ─────────────────────────────────────────────────────

    def test_show_transactions_idle(self):
        """SHOW TRANSACTIONS outside a transaction returns status=idle."""
        db = Database(":memory:")
        cur = db.execute("SHOW TRANSACTIONS")
        rows = cur.fetchall()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["status"], "idle")
        self.assertIsNone(row["started_at"])
        self.assertIsNone(row["elapsed_s"])
        self.assertEqual(row["isolation_level"], "READ COMMITTED")

    def test_show_transactions_active(self):
        """SHOW TRANSACTIONS inside a transaction returns status=active with a timestamp."""
        db = Database(":memory:")
        db.execute("CREATE TABLE t (id INTEGER)")
        db.execute("BEGIN")
        time.sleep(0.05)
        cur = db.execute("SHOW TRANSACTIONS")
        rows = cur.fetchall()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["status"], "active")
        self.assertIsNotNone(row["started_at"])
        self.assertGreaterEqual(row["elapsed_s"], 0.0)
        db.execute("COMMIT")

    def test_show_transactions_reflects_isolation_level(self):
        """SHOW TRANSACTIONS reports the current isolation level."""
        db = Database(":memory:")
        db.execute("CREATE TABLE t (id INTEGER)")
        db.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
        db.execute("BEGIN")
        cur = db.execute("SHOW TRANSACTIONS")
        row = cur.fetchone()
        self.assertEqual(row["isolation_level"], "SERIALIZABLE")
        db.execute("COMMIT")

    def test_show_transactions_columns(self):
        """SHOW TRANSACTIONS cursor.description has the expected column names."""
        db = Database(":memory:")
        cur = db.execute("SHOW TRANSACTIONS")
        col_names = [d[0] for d in cur.description]
        self.assertEqual(col_names,
                         ["isolation_level", "started_at", "elapsed_s", "status"])

    def test_txn_start_time_cleared_after_commit(self):
        """_txn_start_time is reset to None after commit."""
        db = Database(":memory:")
        db.execute("CREATE TABLE t (id INTEGER)")
        db.execute("BEGIN")
        self.assertIsNotNone(db._txn_start_time)
        db.execute("COMMIT")
        self.assertIsNone(db._txn_start_time)

    def test_txn_start_time_cleared_after_rollback(self):
        """_txn_start_time is reset to None after rollback."""
        db = Database(":memory:")
        db.execute("CREATE TABLE t (id INTEGER)")
        db.execute("BEGIN")
        self.assertIsNotNone(db._txn_start_time)
        db.execute("ROLLBACK")
        self.assertIsNone(db._txn_start_time)

    # ── Combined: isolation + FOR UPDATE ─────────────────────────────────────

    def test_for_update_inside_repeatable_read_transaction(self):
        """SELECT FOR UPDATE inside REPEATABLE READ doesn't double-acquire the lock."""
        db = Database(":memory:")
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val INTEGER)")
        db.execute("INSERT INTO t VALUES (1, 10)")
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        db.execute("BEGIN")
        # Both _isolation_held and for_update would be True but lock depth stays 1
        db.execute("SELECT * FROM t FOR UPDATE")
        self.assertTrue(db._isolation_held)
        self.assertTrue(db._for_update_held)
        depth = db._lock._write_depth
        self.assertGreaterEqual(depth, 1)
        db.execute("COMMIT")
        self.assertFalse(db._isolation_held)
        self.assertFalse(db._for_update_held)
        self.assertEqual(db._lock._write_depth, 0)


if __name__ == "__main__":
    unittest.main()
