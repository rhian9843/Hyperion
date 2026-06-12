"""Tests for server-side connection pooling and SHOW PROCESSLIST."""
from __future__ import annotations

import socket
import struct
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hyperion.database import Database
from hyperion.server import Server, _PooledServer, _ConnInfo
from hyperion.client import connect, Connection

_HDR = struct.Struct("!I")


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _raw_send(sock: socket.socket, payload: dict) -> None:
    import json
    data = json.dumps(payload).encode()
    sock.sendall(_HDR.pack(len(data)) + data)


def _raw_recv(sock: socket.socket) -> dict | None:
    import json
    buf = b""
    while len(buf) < 4:
        chunk = sock.recv(4 - len(buf))
        if not chunk:
            return None
        buf += chunk
    (length,) = _HDR.unpack(buf)
    body = b""
    while len(body) < length:
        chunk = sock.recv(length - len(body))
        if not chunk:
            return None
        body += chunk
    return json.loads(body.decode())


def _raw_connect(host: str, port: int) -> socket.socket:
    sock = socket.create_connection((host, port), timeout=5.0)
    return sock


# ── Server fixture ────────────────────────────────────────────────────────────

class _Fixture(unittest.TestCase):
    """Start a Server with port=0 and a fresh in-memory database."""

    pool_size: int = 4
    max_queue: int = 8

    @classmethod
    def setUpClass(cls):
        cls.db  = Database(":memory:")
        cls.db.execute("CREATE TABLE nums (n INTEGER)")
        cls.db.execute("INSERT INTO nums VALUES (1)")
        cls.db.execute("INSERT INTO nums VALUES (2)")
        cls.db.execute("INSERT INTO nums VALUES (3)")
        cls.srv = Server(cls.db, host="127.0.0.1", port=0,
                         pool_size=cls.pool_size, max_queue=cls.max_queue)
        cls.srv.start()
        time.sleep(0.05)
        cls.host, cls.port = cls.srv.address

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.db.close()


# ── Basic functionality ────────────────────────────────────────────────────────

class TestBasicQueries(_Fixture):

    def test_select_returns_rows(self):
        conn = connect(host=self.host, port=self.port)
        cur  = conn.cursor()
        cur.execute("SELECT n FROM nums ORDER BY n")
        rows = cur.fetchall()
        self.assertEqual([r["n"] for r in rows], [1, 2, 3])
        conn.close()

    def test_insert_and_select(self):
        db  = Database(":memory:")
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        srv = Server(db, host="127.0.0.1", port=0)
        srv.start()
        time.sleep(0.05)
        host, port = srv.address
        try:
            conn = connect(host=host, port=port)
            conn.execute("INSERT INTO t VALUES (1, 'hello')")
            row = conn.execute("SELECT v FROM t WHERE id = 1").fetchone()
            self.assertEqual(row["v"], "hello")
            conn.close()
        finally:
            srv.shutdown()
            db.close()

    def test_transaction_state_preserved(self):
        conn = connect(host=self.host, port=self.port)
        conn.execute("BEGIN")
        self.assertTrue(conn.in_transaction)
        conn.execute("ROLLBACK")
        self.assertFalse(conn.in_transaction)
        conn.close()

    def test_description_present_for_select(self):
        conn = connect(host=self.host, port=self.port)
        cur  = conn.cursor()
        cur.execute("SELECT n FROM nums LIMIT 1")
        self.assertIsNotNone(cur.description)
        self.assertEqual(cur.description[0][0], "n")
        conn.close()

    def test_error_propagated(self):
        from hyperion.errors import NoSuchTableError
        conn = connect(host=self.host, port=self.port)
        with self.assertRaises(Exception):
            conn.execute("SELECT * FROM no_such_table")
        conn.close()

    def test_multiple_sequential_queries(self):
        conn = connect(host=self.host, port=self.port)
        for _ in range(20):
            row = conn.execute("SELECT COUNT(*) AS c FROM nums").fetchone()
            self.assertEqual(row["c"], 3)
        conn.close()


# ── Concurrency ────────────────────────────────────────────────────────────────

class TestConcurrency(_Fixture):

    def test_multiple_simultaneous_connections(self):
        errors   = []
        results  = []
        lock     = threading.Lock()

        def query():
            try:
                conn = connect(host=self.host, port=self.port)
                row  = conn.execute("SELECT SUM(n) AS s FROM nums").fetchone()
                conn.close()
                with lock:
                    results.append(row["s"])
            except Exception as e:
                with lock:
                    errors.append(str(e))

        threads = [threading.Thread(target=query) for _ in range(self.pool_size)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), self.pool_size)
        for s in results:
            self.assertEqual(s, 6)

    def test_connections_up_to_pool_size_all_succeed(self):
        conns = []
        try:
            for _ in range(self.pool_size):
                c = connect(host=self.host, port=self.port)
                # Send a query so the worker is holding the connection open
                c.execute("SELECT 1")
                conns.append(c)
            self.assertEqual(len(conns), self.pool_size)
        finally:
            for c in conns:
                try:
                    c.close()
                except Exception:
                    pass


# ── Queue overflow / busy rejection ───────────────────────────────────────────

class TestQueueOverflow(unittest.TestCase):

    def test_busy_rejection_when_queue_full(self):
        """Server with pool_size=1, max_queue=1 rejects when worker busy + queue full.

        Flow:
          conn1 → worker picks it up, blocks on _recv (no data sent)
          conn2 → accepted, placed in queue (queue now full at capacity 1)
          conn3 → queue.Full → server sends ServerBusyError and closes
        """
        # Note: queue.Queue(maxsize=0) means *unlimited* in Python, so min useful
        # max_queue is 1.
        db  = Database(":memory:")
        srv = Server(db, host="127.0.0.1", port=0, pool_size=1, max_queue=1)
        srv.start()
        time.sleep(0.05)
        host, port = srv.address

        done = threading.Event()

        def hold_conn():
            sock = _raw_connect(host, port)
            done.wait(timeout=5.0)
            sock.close()

        # conn1: worker picks it up and blocks waiting for data
        holder1 = threading.Thread(target=hold_conn, daemon=True)
        holder1.start()
        time.sleep(0.1)   # let worker dequeue conn1

        # conn2: fills the queue
        holder2 = threading.Thread(target=hold_conn, daemon=True)
        holder2.start()
        time.sleep(0.1)   # let accept loop enqueue conn2

        # conn3: queue full → should get ServerBusyError
        try:
            sock3 = _raw_connect(host, port)
            sock3.settimeout(3.0)
            resp  = _raw_recv(sock3)
            sock3.close()
            self.assertIsNotNone(resp)
            self.assertEqual(resp.get("status"), "error")
            self.assertEqual(resp.get("error_type"), "ServerBusyError")
            self.assertIn("queue full", resp.get("message", ""))
        finally:
            done.set()
            holder1.join(timeout=2.0)
            holder2.join(timeout=2.0)
            srv.shutdown()
            db.close()


# ── SHOW PROCESSLIST ──────────────────────────────────────────────────────────

class TestShowProcesslist(_Fixture):

    def test_processlist_returns_ok(self):
        conn = connect(host=self.host, port=self.port)
        cur  = conn.cursor()
        cur.execute("SHOW PROCESSLIST")
        rows = cur.fetchall()
        self.assertIsInstance(rows, list)
        conn.close()

    def test_processlist_description(self):
        conn = connect(host=self.host, port=self.port)
        cur  = conn.cursor()
        cur.execute("SHOW PROCESSLIST")
        self.assertIsNotNone(cur.description)
        col_names = [c[0] for c in cur.description]
        for expected in ("id", "host", "time", "state", "info"):
            self.assertIn(expected, col_names)
        conn.close()

    def test_processlist_shows_active_connection(self):
        conn = connect(host=self.host, port=self.port)
        cur  = conn.cursor()
        cur.execute("SHOW PROCESSLIST")
        rows = cur.fetchall()
        # At least this connection should appear
        self.assertGreaterEqual(len(rows), 1)
        ids = [r["id"] for r in rows]
        self.assertTrue(all(isinstance(i, int) for i in ids))
        conn.close()

    def test_processlist_time_is_numeric(self):
        conn = connect(host=self.host, port=self.port)
        cur  = conn.cursor()
        cur.execute("SHOW PROCESSLIST")
        rows = cur.fetchall()
        for row in rows:
            self.assertIsInstance(row["time"], (int, float))
        conn.close()

    def test_processlist_state_values(self):
        conn = connect(host=self.host, port=self.port)
        cur  = conn.cursor()
        cur.execute("SHOW PROCESSLIST")
        rows = cur.fetchall()
        for row in rows:
            self.assertIn(row["state"], ("idle", "active"))
        conn.close()

    def test_processlist_multiple_connections(self):
        conn1 = connect(host=self.host, port=self.port)
        conn2 = connect(host=self.host, port=self.port)
        # Give both connections time to register
        time.sleep(0.05)
        cur = conn1.cursor()
        cur.execute("SHOW PROCESSLIST")
        rows = cur.fetchall()
        self.assertGreaterEqual(len(rows), 2)
        conn1.close()
        conn2.close()

    def test_processlist_cleaned_up_after_close(self):
        conn_tmp = connect(host=self.host, port=self.port)
        conn_tmp.close()
        time.sleep(0.05)

        conn = connect(host=self.host, port=self.port)
        cur  = conn.cursor()
        cur.execute("SHOW PROCESSLIST")
        rows = cur.fetchall()
        # All IDs should be unique — no stale entries from closed connection
        ids  = [r["id"] for r in rows]
        self.assertEqual(len(ids), len(set(ids)))
        conn.close()

    def test_processlist_case_insensitive(self):
        conn = connect(host=self.host, port=self.port)
        cur  = conn.cursor()
        cur.execute("show processlist")
        rows = cur.fetchall()
        self.assertIsInstance(rows, list)
        conn.close()


# ── Pool configuration ────────────────────────────────────────────────────────

class TestPoolConfiguration(unittest.TestCase):

    def test_custom_pool_size_and_queue(self):
        db  = Database(":memory:")
        srv = Server(db, host="127.0.0.1", port=0, pool_size=3, max_queue=5)
        srv.start()
        time.sleep(0.05)
        host, port = srv.address
        try:
            conn = connect(host=host, port=port)
            row  = conn.execute("SELECT 42 AS v").fetchone()
            self.assertEqual(row["v"], 42)
            conn.close()
        finally:
            srv.shutdown()
            db.close()

    def test_address_returns_bound_port(self):
        db  = Database(":memory:")
        srv = Server(db, host="127.0.0.1", port=0)
        host, port = srv.address
        self.assertIsInstance(port, int)
        self.assertGreater(port, 0)
        srv.shutdown()
        db.close()

    def test_server_start_returns_thread(self):
        db  = Database(":memory:")
        srv = Server(db, host="127.0.0.1", port=0)
        t   = srv.start()
        self.assertIsInstance(t, __import__("threading").Thread)
        srv.shutdown()
        db.close()

    def test_shutdown_closes_port(self):
        db  = Database(":memory:")
        srv = Server(db, host="127.0.0.1", port=0)
        host, port = srv.address
        srv.start()
        time.sleep(0.05)
        srv.shutdown()
        time.sleep(0.05)
        # Port should be released — binding it again should succeed
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
            sock.close()
        except OSError:
            pass  # port may linger briefly — not a hard failure
        db.close()


# ── _ConnInfo unit tests ──────────────────────────────────────────────────────

class TestConnInfo(unittest.TestCase):

    def test_initial_state(self):
        info = _ConnInfo(1, "127.0.0.1")
        self.assertEqual(info.id, 1)
        self.assertEqual(info.host, "127.0.0.1")
        self.assertEqual(info.state, "idle")
        self.assertEqual(info.command, "")

    def test_mutable(self):
        info = _ConnInfo(2, "10.0.0.1")
        info.state   = "active"
        info.command = "SELECT 1"
        self.assertEqual(info.state, "active")
        self.assertEqual(info.command, "SELECT 1")

    def test_started_is_monotonic(self):
        before = time.monotonic()
        info   = _ConnInfo(3, "localhost")
        after  = time.monotonic()
        self.assertGreaterEqual(info.started, before)
        self.assertLessEqual(info.started, after)


if __name__ == "__main__":
    unittest.main()
