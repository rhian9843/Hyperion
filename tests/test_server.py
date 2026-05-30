"""Tests for the Hyperion server/client network mode.

Covers TCP and Unix-socket transports, basic CRUD, error propagation,
multi-client concurrency, and transaction isolation.
"""
import os
import sys
import tempfile
import threading
import time
import pytest

from hyperion import Database
from hyperion.server import Server
from hyperion.client import connect
from hyperion.errors import UniqueConstraintError, NoSuchTableError


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _start_tcp_server(db: Database) -> tuple[Server, int]:
    """Start a server on a random free port; return (server, port)."""
    srv = Server(db, host="127.0.0.1", port=0)
    _, port = srv.address
    srv.start()
    return srv, port


# ── TCP transport ─────────────────────────────────────────────────────────────

class TestTCPBasic:
    def setup_method(self):
        self.db  = Database(":memory:")
        self.srv, self.port = _start_tcp_server(self.db)

    def teardown_method(self):
        self.srv.shutdown()
        self.db.close()

    def _conn(self):
        return connect(host="127.0.0.1", port=self.port)

    def test_select_no_from(self):
        with self._conn() as c:
            cur = c.execute("SELECT 1 + 1 AS v")
            row = cur.fetchone()
            assert row["v"] == 2

    def test_create_insert_select(self):
        with self._conn() as c:
            c.execute("CREATE TABLE t (id INTEGER, name TEXT)")
            c.execute("INSERT INTO t VALUES (1, 'alice')")
            c.execute("INSERT INTO t VALUES (2, 'bob')")
            rows = c.execute("SELECT id, name FROM t ORDER BY id").fetchall()
        assert len(rows) == 2
        assert rows[0]["name"] == "alice"
        assert rows[1]["name"] == "bob"

    def test_rowcount_insert(self):
        with self._conn() as c:
            c.execute("CREATE TABLE t (id INTEGER)")
            cur = c.execute("INSERT INTO t VALUES (1)")
            assert cur.rowcount == 1

    def test_rowcount_update(self):
        with self._conn() as c:
            c.execute("CREATE TABLE t (id INTEGER, v INTEGER)")
            c.execute("INSERT INTO t VALUES (1, 0)")
            c.execute("INSERT INTO t VALUES (2, 0)")
            cur = c.execute("UPDATE t SET v = 99")
            assert cur.rowcount == 2

    def test_description_populated(self):
        with self._conn() as c:
            c.execute("CREATE TABLE t (id INTEGER, name TEXT)")
            cur = c.execute("SELECT id, name FROM t")
            assert cur.description is not None
            names = [col[0] for col in cur.description]
            assert names == ["id", "name"]

    def test_fetchmany(self):
        with self._conn() as c:
            c.execute("CREATE TABLE t (n INTEGER)")
            for i in range(10):
                c.execute(f"INSERT INTO t VALUES ({i})")
            cur = c.execute("SELECT n FROM t ORDER BY n")
            first  = cur.fetchmany(3)
            second = cur.fetchmany(3)
            rest   = cur.fetchall()
        assert [r["n"] for r in first]  == [0, 1, 2]
        assert [r["n"] for r in second] == [3, 4, 5]
        assert [r["n"] for r in rest]   == [6, 7, 8, 9]

    def test_error_propagated_as_correct_type(self):
        with self._conn() as c:
            c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
            c.execute("INSERT INTO t VALUES (1)")
            with pytest.raises(UniqueConstraintError):
                c.execute("INSERT INTO t VALUES (1)")

    def test_no_such_table_error(self):
        with self._conn() as c:
            with pytest.raises(NoSuchTableError):
                c.execute("SELECT * FROM nonexistent")

    def test_cursor_iteration(self):
        with self._conn() as c:
            c.execute("CREATE TABLE t (v INTEGER)")
            for i in range(5):
                c.execute(f"INSERT INTO t VALUES ({i})")
            rows = list(c.execute("SELECT v FROM t ORDER BY v"))
        assert [r["v"] for r in rows] == [0, 1, 2, 3, 4]

    def test_shared_state_across_connections(self):
        """Data written by one connection is visible to another."""
        with self._conn() as c1:
            c1.execute("CREATE TABLE shared (x INTEGER)")
            c1.execute("INSERT INTO shared VALUES (42)")
        with self._conn() as c2:
            row = c2.execute("SELECT x FROM shared").fetchone()
        assert row["x"] == 42

    def test_transaction_commit(self):
        """BEGIN / INSERT / COMMIT across separate requests must persist."""
        with self._conn() as c:
            c.execute("CREATE TABLE txn_t (v INTEGER)")
            c.execute("BEGIN")
            c.execute("INSERT INTO txn_t VALUES (1)")
            c.execute("INSERT INTO txn_t VALUES (2)")
            c.execute("COMMIT")
        with self._conn() as c2:
            rows = c2.execute("SELECT v FROM txn_t ORDER BY v").fetchall()
        assert [r["v"] for r in rows] == [1, 2]

    def test_transaction_rollback(self):
        """BEGIN / INSERT / ROLLBACK must discard the inserted rows."""
        with self._conn() as c:
            c.execute("CREATE TABLE rb_t (v INTEGER)")
            c.execute("BEGIN")
            c.execute("INSERT INTO rb_t VALUES (99)")
            c.execute("ROLLBACK")
        with self._conn() as c2:
            rows = c2.execute("SELECT v FROM rb_t").fetchall()
        assert rows == []

    def test_blob_round_trip(self):
        """BLOB values must survive the JSON transport without corruption."""
        payload = bytes(range(256))
        with self._conn() as c:
            c.execute("CREATE TABLE blobs (id INTEGER, data BLOB)")
            c.execute("INSERT INTO blobs VALUES (1, ?)", [payload])
            row = c.execute("SELECT data FROM blobs WHERE id = 1").fetchone()
        assert row["data"] == payload

    def test_lastrowid(self):
        with self._conn() as c:
            c.execute("CREATE TABLE lr (id INTEGER PRIMARY KEY AUTOINCREMENT, v TEXT)")
            cur = c.execute("INSERT INTO lr VALUES (NULL, 'x')")
            assert cur.lastrowid == 1
            cur = c.execute("INSERT INTO lr VALUES (NULL, 'y')")
            assert cur.lastrowid == 2

    def test_commit_rollback_convenience(self):
        with self._conn() as c:
            c.execute("CREATE TABLE cr (v INTEGER)")
            c.execute("BEGIN")
            c.execute("INSERT INTO cr VALUES (1)")
            c.rollback()
        with self._conn() as c2:
            assert c2.execute("SELECT COUNT(*) AS n FROM cr").fetchone()["n"] == 0

        with self._conn() as c:
            c.execute("BEGIN")
            c.execute("INSERT INTO cr VALUES (42)")
            c.commit()
        with self._conn() as c2:
            assert c2.execute("SELECT COUNT(*) AS n FROM cr").fetchone()["n"] == 1

    def test_executemany(self):
        with self._conn() as c:
            c.execute("CREATE TABLE em (n INTEGER)")
            c.executemany("INSERT INTO em VALUES (?)", [[i] for i in range(5)])
            rows = c.execute("SELECT n FROM em ORDER BY n").fetchall()
        assert [r["n"] for r in rows] == [0, 1, 2, 3, 4]

    def test_executescript(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE es (id INTEGER, v TEXT);
                INSERT INTO es VALUES (1, 'a');
                INSERT INTO es VALUES (2, 'b')
            """)
            rows = c.execute("SELECT id, v FROM es ORDER BY id").fetchall()
        assert [(r["id"], r["v"]) for r in rows] == [(1, "a"), (2, "b")]

    def test_top_level_import(self):
        from hyperion import Server, connect  # noqa: F401  — just verify importable
        assert Server is not None
        assert connect is not None

    def test_cursor_executemany_rowcount(self):
        with self._conn() as c:
            c.execute("CREATE TABLE cem (n INTEGER)")
            cur = c.cursor()
            cur.executemany("INSERT INTO cem VALUES (?)", [[i] for i in range(7)])
            assert cur.rowcount == 7

    def test_arraysize_respected_by_fetchmany(self):
        with self._conn() as c:
            c.execute("CREATE TABLE az (n INTEGER)")
            for i in range(6):
                c.execute(f"INSERT INTO az VALUES ({i})")
            cur = c.execute("SELECT n FROM az ORDER BY n")
            cur.arraysize = 2
            first  = cur.fetchmany()   # no size arg — uses arraysize
            second = cur.fetchmany()
            rest   = cur.fetchall()
        assert [r["n"] for r in first]  == [0, 1]
        assert [r["n"] for r in second] == [2, 3]
        assert [r["n"] for r in rest]   == [4, 5]

    def test_row_factory(self):
        with self._conn() as c:
            c.row_factory = lambda cur, row: tuple(row.values())
            c.execute("CREATE TABLE rf (id INTEGER, v TEXT)")
            c.execute("INSERT INTO rf VALUES (1, 'hi')")
            row = c.execute("SELECT id, v FROM rf").fetchone()
        assert row == (1, "hi")

    def test_savepoint(self):
        with self._conn() as c:
            c.execute("CREATE TABLE sp (v INTEGER)")
            c.execute("BEGIN")
            c.execute("INSERT INTO sp VALUES (1)")
            c.savepoint("s1")
            c.execute("INSERT INTO sp VALUES (2)")
            c.rollback_to_savepoint("s1")
            c.release_savepoint("s1")
            c.commit()
        with self._conn() as c2:
            rows = c2.execute("SELECT v FROM sp ORDER BY v").fetchall()
        assert [r["v"] for r in rows] == [1]

    def test_in_transaction_tracking(self):
        with self._conn() as c:
            assert c.in_transaction is False
            c.execute("BEGIN")
            assert c.in_transaction is True
            c.execute("ROLLBACK")
            assert c.in_transaction is False

    def test_executescript_returns_cursor(self):
        with self._conn() as c:
            result = c.executescript("CREATE TABLE sr (x INTEGER); INSERT INTO sr VALUES (9)")
            assert result is not None   # returns a Cursor, not None


# ── Unix socket transport ─────────────────────────────────────────────────────

@pytest.mark.skipif(sys.platform == "win32", reason="Unix sockets not available on Windows")
class TestUnixSocket:
    def setup_method(self):
        self.db = Database(":memory:")
        self.tmpdir = tempfile.mkdtemp()
        self.sock_path = os.path.join(self.tmpdir, "hyperion.sock")
        self.srv = Server(self.db, socket_path=self.sock_path)
        self.srv.start()
        # Give the server a moment to bind
        time.sleep(0.05)

    def teardown_method(self):
        self.srv.shutdown()
        self.db.close()
        if os.path.exists(self.sock_path):
            os.unlink(self.sock_path)
        os.rmdir(self.tmpdir)

    def _conn(self):
        return connect(socket_path=self.sock_path)

    def test_basic_query(self):
        with self._conn() as c:
            c.execute("CREATE TABLE t (id INTEGER)")
            c.execute("INSERT INTO t VALUES (7)")
            row = c.execute("SELECT id FROM t").fetchone()
        assert row["id"] == 7

    def test_socket_file_created(self):
        assert os.path.exists(self.sock_path)


# ── Concurrency ───────────────────────────────────────────────────────────────

class TestConcurrency:
    def setup_method(self):
        self.db = Database(":memory:")
        self.srv, self.port = _start_tcp_server(self.db)
        # Create and populate the table once
        with connect(host="127.0.0.1", port=self.port) as c:
            c.execute("CREATE TABLE counts (id INTEGER PRIMARY KEY, val INTEGER)")
            for i in range(20):
                c.execute(f"INSERT INTO counts VALUES ({i}, 0)")

    def teardown_method(self):
        self.srv.shutdown()
        self.db.close()

    def test_concurrent_reads(self):
        """Multiple clients reading simultaneously must all get correct results."""
        errors = []
        results = {}

        def reader(client_id):
            try:
                with connect(host="127.0.0.1", port=self.port) as c:
                    rows = c.execute(
                        "SELECT COUNT(*) AS n FROM counts"
                    ).fetchone()
                    results[client_id] = rows["n"]
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader, args=(i,)) for i in range(10)]
        for t in threads: t.start()
        for t in threads: t.join()

        assert not errors, f"Errors during concurrent reads: {errors}"
        assert all(v == 20 for v in results.values())

    def test_concurrent_inserts_unique_conflict_isolated(self):
        """UniqueConstraintError on one connection must not affect another connection."""
        errors = []

        def worker(client_id):
            try:
                with connect(host="127.0.0.1", port=self.port) as c:
                    try:
                        c.execute(f"INSERT INTO counts VALUES ({client_id}, 999)")
                    except UniqueConstraintError:
                        pass  # expected for some clients; must not crash others
                    row = c.execute(
                        f"SELECT val FROM counts WHERE id = {client_id}"
                    ).fetchone()
                    errors.append(None if row is not None else f"client {client_id}: no row")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads: t.start()
        for t in threads: t.join()
        real_errors = [e for e in errors if e is not None]
        assert not real_errors, f"Unexpected errors: {real_errors}"

    def test_concurrent_inserts_no_corruption(self):
        """Many concurrent inserts must not corrupt the table."""
        errors = []
        barrier = threading.Barrier(5)

        def inserter(start):
            barrier.wait()
            try:
                with connect(host="127.0.0.1", port=self.port) as c:
                    for i in range(start, start + 10):
                        c.execute(f"INSERT INTO counts VALUES ({100 + i}, {i})")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=inserter, args=(i * 10,)) for i in range(5)]
        for t in threads: t.start()
        for t in threads: t.join()

        assert not errors, f"Errors during concurrent inserts: {errors}"
        with connect(host="127.0.0.1", port=self.port) as c:
            row = c.execute("SELECT COUNT(*) AS n FROM counts").fetchone()
        assert row["n"] == 70  # 20 original + 50 inserted
