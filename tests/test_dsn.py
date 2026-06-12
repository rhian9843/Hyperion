"""Tests for connect_dsn() — DSN connection string parsing and dispatch."""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hyperion.client import connect_dsn, Connection
from hyperion.database import Database
from hyperion.server import Server


def _start_server(db: Database, port: int) -> Server:
    srv = Server(db, host="127.0.0.1", port=port)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.05)
    return srv


class TestDsnMemory(unittest.TestCase):
    """hyperion://:memory: — returns an in-memory Database."""

    def test_returns_database_instance(self):
        db = connect_dsn("hyperion://:memory:")
        self.assertIsInstance(db, Database)
        db.close()

    def test_database_is_functional(self):
        db = connect_dsn("hyperion://:memory:")
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
        db.execute("INSERT INTO t VALUES (1, 'hello')")
        row = db.execute("SELECT * FROM t").fetchone()
        self.assertEqual(row["id"], 1)
        self.assertEqual(row["val"], "hello")
        db.close()

    def test_memory_dsn_variants(self):
        for dsn in ("hyperion://:memory:", "hyperion://:memory:/ignored"):
            db = connect_dsn(dsn)
            self.assertIsInstance(db, Database)
            db.close()


class TestDsnLocalFile(unittest.TestCase):
    """hyperion:///path/to/file — opens a file-backed Database."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".hdb")
        os.close(fd)
        os.unlink(self.path)   # let Database create it fresh

    def tearDown(self):
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_returns_database_instance(self):
        db = connect_dsn(f"hyperion://{self.path}")
        self.assertIsInstance(db, Database)
        db.close()

    def test_file_is_created(self):
        db = connect_dsn(f"hyperion://{self.path}")
        db.execute("CREATE TABLE x (n INTEGER)")
        db.close()
        self.assertTrue(os.path.exists(self.path))

    def test_data_persists_across_connections(self):
        db = connect_dsn(f"hyperion://{self.path}")
        db.execute("CREATE TABLE nums (n INTEGER)")
        db.execute("INSERT INTO nums VALUES (7)")
        db.close()

        db2 = connect_dsn(f"hyperion://{self.path}")
        row = db2.execute("SELECT n FROM nums").fetchone()
        self.assertEqual(row["n"], 7)
        db2.close()

    def test_absolute_path_with_triple_slash(self):
        db = connect_dsn(f"hyperion://{self.path}")
        self.assertIsInstance(db, Database)
        db.close()


class TestDsnRemote(unittest.TestCase):
    """hyperion://host:port — connects to a running TCP server."""

    @classmethod
    def setUpClass(cls):
        cls.srv_db = Database(":memory:")
        cls.srv_db.execute("CREATE TABLE data (id INTEGER PRIMARY KEY, msg TEXT)")
        cls.srv_db.execute("INSERT INTO data VALUES (1, 'from server')")
        cls.srv = _start_server(cls.srv_db, port=15500)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv_db.close()

    def test_returns_connection_instance(self):
        conn = connect_dsn("hyperion://127.0.0.1:15500")
        self.assertIsInstance(conn, Connection)
        conn.close()

    def test_can_query_server(self):
        conn = connect_dsn("hyperion://127.0.0.1:15500/mydb")
        cur = conn.cursor()
        cur.execute("SELECT msg FROM data WHERE id = 1")
        row = cur.fetchone()
        self.assertEqual(row["msg"], "from server")
        conn.close()

    def test_dbname_in_path_is_ignored(self):
        # The server is single-file; the /dbname segment is accepted but ignored
        conn = connect_dsn("hyperion://127.0.0.1:15500/any_name_here")
        self.assertIsInstance(conn, Connection)
        conn.close()

    def test_default_port_5433(self):
        srv_db = Database(":memory:")
        srv = _start_server(srv_db, port=5433)
        try:
            conn = connect_dsn("hyperion://127.0.0.1/mydb")
            self.assertIsInstance(conn, Connection)
            conn.close()
        finally:
            srv.shutdown()
            srv_db.close()

    def test_connection_is_usable_after_dsn(self):
        conn = connect_dsn("hyperion://127.0.0.1:15500")
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM data")
        self.assertEqual(cur.fetchone()["n"], 1)
        conn.close()


class TestDsnErrors(unittest.TestCase):

    def test_wrong_scheme_raises_value_error(self):
        for bad in ("postgres://localhost/db", "sqlite:///db.sqlite",
                    "mysql://root@localhost/db", "http://localhost"):
            with self.subTest(dsn=bad):
                with self.assertRaises(ValueError):
                    connect_dsn(bad)

    def test_no_scheme_raises_value_error(self):
        with self.assertRaises(ValueError):
            connect_dsn("localhost:5433/mydb")

    def test_error_message_mentions_scheme(self):
        try:
            connect_dsn("postgres://localhost/db")
        except ValueError as e:
            self.assertIn("hyperion://", str(e))


class TestDsnImport(unittest.TestCase):

    def test_importable_from_client(self):
        from hyperion.client import connect_dsn as f
        self.assertTrue(callable(f))

    def test_importable_from_package(self):
        from hyperion import connect_dsn as f
        self.assertTrue(callable(f))


if __name__ == "__main__":
    unittest.main()
