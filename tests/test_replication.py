"""Tests for logical replication: publications, subscriptions, changelog, apply."""
import threading
import time
import unittest

from hyperion import Database
from hyperion.catalog import PublicationMeta, SubscriptionMeta
from hyperion.changelog import ChangelogEntry, InMemoryChangelog
from hyperion.replication import apply_change, _sql_literal, _pk_cols_for


# ── Changelog unit tests ──────────────────────────────────────────────────────

class TestChangelog(unittest.TestCase):

    def _make_entry(self, lsn, table="t", op="INSERT", row=None):
        return ChangelogEntry(lsn=lsn, table=table, op=op,
                              row=row or {"id": lsn}, row_before=None, ts=0.0)

    def test_append_and_read(self):
        cl = InMemoryChangelog()
        cl.append(self._make_entry(1))
        cl.append(self._make_entry(2))
        cl.append(self._make_entry(3))
        self.assertEqual(len(cl.read_since(0)), 3)
        self.assertEqual(len(cl.read_since(1)), 2)
        self.assertEqual(len(cl.read_since(2)), 1)
        self.assertEqual(len(cl.read_since(3)), 0)

    def test_latest_lsn(self):
        cl = InMemoryChangelog()
        self.assertEqual(cl.latest_lsn(), 0)
        cl.append(self._make_entry(5))
        cl.append(self._make_entry(3))
        self.assertEqual(cl.latest_lsn(), 3)   # last appended

    def test_filter_by_publication_tables(self):
        cl = InMemoryChangelog()
        cl.append(self._make_entry(1, table="orders"))
        cl.append(self._make_entry(2, table="users"))
        cl.append(self._make_entry(3, table="orders"))
        result = cl.read_for_publication(["orders"], 0)
        self.assertEqual(len(result), 2)
        self.assertTrue(all(e.table == "orders" for e in result))

    def test_empty_tables_means_all_tables(self):
        cl = InMemoryChangelog()
        cl.append(self._make_entry(1, table="orders"))
        cl.append(self._make_entry(2, table="users"))
        result = cl.read_for_publication([], 0)  # FOR ALL TABLES
        self.assertEqual(len(result), 2)

    def test_entry_roundtrip(self):
        entry = ChangelogEntry(lsn=7, table="orders", op="UPDATE",
                               row={"id": 1, "status": "shipped"},
                               row_before={"id": 1, "status": "pending"},
                               ts=999.9)
        d = entry.to_dict()
        self.assertEqual(d["lsn"], 7)
        self.assertEqual(d["row_before"]["status"], "pending")
        restored = ChangelogEntry.from_dict(d)
        self.assertEqual(restored.lsn, 7)
        self.assertEqual(restored.row_before["status"], "pending")


# ── Publication DDL tests ─────────────────────────────────────────────────────

class TestPublications(unittest.TestCase):

    def setUp(self):
        self.db = Database(":memory:")
        self.db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT)")
        self.db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")

    def tearDown(self):
        self.db.close()

    def test_create_publication_for_table(self):
        self.db.execute("CREATE PUBLICATION pub1 FOR TABLE orders")
        pubs = self.db._catalog.publications
        self.assertIn("pub1", pubs)
        self.assertEqual(pubs["pub1"].tables, ["orders"])

    def test_create_publication_for_all_tables(self):
        self.db.execute("CREATE PUBLICATION pub_all FOR ALL TABLES")
        self.assertEqual(self.db._catalog.publications["pub_all"].tables, [])

    def test_create_publication_multiple_tables(self):
        self.db.execute("CREATE PUBLICATION pub2 FOR TABLE orders, users")
        self.assertIn("orders", self.db._catalog.publications["pub2"].tables)
        self.assertIn("users", self.db._catalog.publications["pub2"].tables)

    def test_drop_publication(self):
        self.db.execute("CREATE PUBLICATION pub1 FOR TABLE orders")
        self.db.execute("DROP PUBLICATION pub1")
        self.assertNotIn("pub1", self.db._catalog.publications)

    def test_drop_publication_if_exists(self):
        # no-op when doesn't exist
        self.db.execute("DROP PUBLICATION IF EXISTS no_such_pub")

    def test_show_publications(self):
        self.db.execute("CREATE PUBLICATION pub1 FOR TABLE orders")
        self.db.execute("CREATE PUBLICATION pub_all FOR ALL TABLES")
        result = self.db.execute("SHOW PUBLICATIONS")
        rows = result.fetchall()
        names = {r["name"] for r in rows}
        self.assertIn("pub1", names)
        self.assertIn("pub_all", names)

    def test_duplicate_publication_raises(self):
        self.db.execute("CREATE PUBLICATION pub1 FOR TABLE orders")
        with self.assertRaises(Exception):
            self.db.execute("CREATE PUBLICATION pub1 FOR TABLE users")

    def test_drop_nonexistent_raises(self):
        with self.assertRaises(Exception):
            self.db.execute("DROP PUBLICATION no_such")


# ── Subscription DDL tests ────────────────────────────────────────────────────

class TestSubscriptions(unittest.TestCase):

    def setUp(self):
        self.db = Database(":memory:")

    def tearDown(self):
        self.db.close()

    def test_create_subscription(self):
        self.db.execute(
            "CREATE SUBSCRIPTION sub1 CONNECTION 'http://localhost:9000' PUBLICATION pub1"
        )
        subs = self.db._catalog.subscriptions
        self.assertIn("sub1", subs)
        self.assertEqual(subs["sub1"].connection, "http://localhost:9000")
        self.assertEqual(subs["sub1"].publication, "pub1")
        self.assertEqual(subs["sub1"].last_lsn, 0)

    def test_drop_subscription(self):
        self.db.execute(
            "CREATE SUBSCRIPTION sub1 CONNECTION 'http://localhost:9000' PUBLICATION pub1"
        )
        self.db.execute("DROP SUBSCRIPTION sub1")
        self.assertNotIn("sub1", self.db._catalog.subscriptions)

    def test_drop_subscription_if_exists(self):
        self.db.execute("DROP SUBSCRIPTION IF EXISTS no_such_sub")

    def test_show_subscriptions(self):
        self.db.execute(
            "CREATE SUBSCRIPTION sub1 CONNECTION 'http://localhost:9001' PUBLICATION pub1"
        )
        result = self.db.execute("SHOW SUBSCRIPTIONS")
        rows = result.fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "sub1")
        self.assertEqual(rows[0]["connection"], "http://localhost:9001")

    def test_duplicate_subscription_raises(self):
        self.db.execute(
            "CREATE SUBSCRIPTION sub1 CONNECTION 'http://localhost:9000' PUBLICATION pub1"
        )
        with self.assertRaises(Exception):
            self.db.execute(
                "CREATE SUBSCRIPTION sub1 CONNECTION 'http://localhost:9000' PUBLICATION pub1"
            )

    def test_drop_nonexistent_subscription_raises(self):
        with self.assertRaises(Exception):
            self.db.execute("DROP SUBSCRIPTION no_such")


# ── Changelog instrumentation tests ──────────────────────────────────────────

class TestChangelogInstrumentation(unittest.TestCase):
    """DML on a published table should produce changelog entries."""

    def setUp(self):
        self.db = Database(":memory:")
        self.db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT, amount REAL)")
        self.db.execute("CREATE PUBLICATION pub1 FOR TABLE orders")

    def tearDown(self):
        self.db.close()

    def test_insert_writes_changelog(self):
        self.db.execute("INSERT INTO orders VALUES (1, 'pending', 99.9)")
        entries = self.db.changelog.read_since(0)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].op, "INSERT")
        self.assertEqual(entries[0].table, "orders")
        self.assertEqual(entries[0].row["id"], 1)
        self.assertEqual(entries[0].row["status"], "pending")

    def test_update_writes_changelog(self):
        self.db.execute("INSERT INTO orders VALUES (1, 'pending', 99.9)")
        self.db.execute("UPDATE orders SET status = 'shipped' WHERE id = 1")
        entries = self.db.changelog.read_since(0)
        upd = [e for e in entries if e.op == "UPDATE"]
        self.assertEqual(len(upd), 1)
        self.assertEqual(upd[0].row["status"], "shipped")
        self.assertEqual(upd[0].row_before["status"], "pending")

    def test_delete_writes_changelog(self):
        self.db.execute("INSERT INTO orders VALUES (1, 'pending', 99.9)")
        self.db.execute("DELETE FROM orders WHERE id = 1")
        entries = self.db.changelog.read_since(0)
        dels = [e for e in entries if e.op == "DELETE"]
        self.assertEqual(len(dels), 1)
        self.assertEqual(dels[0].row["id"], 1)

    def test_lsn_increments_monotonically(self):
        self.db.execute("INSERT INTO orders VALUES (1, 'a', 1.0)")
        self.db.execute("INSERT INTO orders VALUES (2, 'b', 2.0)")
        self.db.execute("INSERT INTO orders VALUES (3, 'c', 3.0)")
        entries = self.db.changelog.read_since(0)
        lsns = [e.lsn for e in entries]
        self.assertEqual(lsns, sorted(lsns))
        self.assertEqual(len(set(lsns)), 3)

    def test_unpublished_table_not_in_changelog(self):
        self.db.execute("CREATE TABLE internal (id INTEGER PRIMARY KEY, val TEXT)")
        self.db.execute("INSERT INTO internal VALUES (1, 'x')")
        entries = self.db.changelog.read_since(0)
        self.assertTrue(all(e.table != "internal" for e in entries))

    def test_no_publication_no_changelog(self):
        db2 = Database(":memory:")
        db2.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        db2.execute("INSERT INTO t VALUES (1, 'x')")
        # no publication → changelog is never created / empty
        entries = db2.changelog.read_since(0)
        self.assertEqual(entries, [])
        db2.close()

    def test_for_all_tables_publication(self):
        db2 = Database(":memory:")
        db2.execute("CREATE TABLE t1 (id INTEGER PRIMARY KEY, v TEXT)")
        db2.execute("CREATE TABLE t2 (id INTEGER PRIMARY KEY, v TEXT)")
        db2.execute("CREATE PUBLICATION pub_all FOR ALL TABLES")
        db2.execute("INSERT INTO t1 VALUES (1, 'a')")
        db2.execute("INSERT INTO t2 VALUES (1, 'b')")
        entries = db2.changelog.read_since(0)
        tables = {e.table for e in entries}
        self.assertIn("t1", tables)
        self.assertIn("t2", tables)
        db2.close()


# ── Apply function tests ──────────────────────────────────────────────────────

class TestApplyChange(unittest.TestCase):
    """apply_change() should replicate changes onto a target database."""

    def setUp(self):
        self.replica = Database(":memory:")
        self.replica.execute(
            "CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT, amount REAL)"
        )

    def tearDown(self):
        self.replica.close()

    def test_apply_insert(self):
        entry = {"lsn": 1, "table": "orders", "op": "INSERT",
                 "row": {"id": 1, "status": "pending", "amount": 50.0}, "ts": 0.0}
        apply_change(self.replica, entry)
        rows = self.replica.execute("SELECT * FROM orders").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "pending")

    def test_apply_update(self):
        self.replica.execute("INSERT INTO orders VALUES (1, 'pending', 50.0)")
        entry = {"lsn": 2, "table": "orders", "op": "UPDATE",
                 "row": {"id": 1, "status": "shipped", "amount": 50.0}, "ts": 0.0}
        apply_change(self.replica, entry)
        rows = self.replica.execute("SELECT status FROM orders WHERE id = 1").fetchall()
        self.assertEqual(rows[0]["status"], "shipped")

    def test_apply_delete(self):
        self.replica.execute("INSERT INTO orders VALUES (1, 'pending', 50.0)")
        entry = {"lsn": 3, "table": "orders", "op": "DELETE",
                 "row": {"id": 1, "status": "pending", "amount": 50.0}, "ts": 0.0}
        apply_change(self.replica, entry)
        rows = self.replica.execute("SELECT * FROM orders").fetchall()
        self.assertEqual(len(rows), 0)

    def test_apply_insert_idempotent(self):
        entry = {"lsn": 1, "table": "orders", "op": "INSERT",
                 "row": {"id": 1, "status": "pending", "amount": 50.0}, "ts": 0.0}
        apply_change(self.replica, entry)
        apply_change(self.replica, entry)   # duplicate → INSERT OR REPLACE
        rows = self.replica.execute("SELECT COUNT(*) AS n FROM orders").fetchall()
        self.assertEqual(rows[0]["n"], 1)

    def test_apply_skips_missing_table(self):
        entry = {"lsn": 1, "table": "no_such_table", "op": "INSERT",
                 "row": {"id": 1}, "ts": 0.0}
        # should not raise
        apply_change(self.replica, entry)

    def test_apply_delete_no_pk_is_skipped(self):
        self.replica.execute("CREATE TABLE nopk (val TEXT)")
        self.replica.execute("INSERT INTO nopk VALUES ('x')")
        entry = {"lsn": 1, "table": "nopk", "op": "DELETE",
                 "row": {"val": "x"}, "ts": 0.0}
        apply_change(self.replica, entry)   # no PK → silently skipped
        rows = self.replica.execute("SELECT * FROM nopk").fetchall()
        self.assertEqual(len(rows), 1)


# ── Full primary → replica cycle (using shared InMemoryChangelog) ─────────────

class TestFullReplicationCycle(unittest.TestCase):
    """Simulate primary inserting rows and replica applying them."""

    def setUp(self):
        self.primary = Database(":memory:")
        self.replica = Database(":memory:")
        for db in (self.primary, self.replica):
            db.execute(
                "CREATE TABLE products ("
                "  id INTEGER PRIMARY KEY, name TEXT, price REAL"
                ")"
            )
        self.primary.execute("CREATE PUBLICATION pub FOR TABLE products")
        # Give primary a shared in-memory changelog that we can read directly
        from hyperion.changelog import InMemoryChangelog
        self._shared_log = InMemoryChangelog()
        self.primary._changelog = self._shared_log

    def tearDown(self):
        self.primary.close()
        self.replica.close()

    def _apply_all(self, since_lsn: int = 0) -> int:
        """Apply all changes from shared log to replica. Returns new last_lsn."""
        entries = self._shared_log.read_since(since_lsn)
        for e in entries:
            apply_change(self.replica, e.to_dict())
        return self._shared_log.latest_lsn()

    def test_insert_replication(self):
        self.primary.execute("INSERT INTO products VALUES (1, 'Widget', 9.99)")
        self.primary.execute("INSERT INTO products VALUES (2, 'Gadget', 24.99)")
        self._apply_all()
        rows = self.replica.execute("SELECT * FROM products ORDER BY id").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["name"], "Widget")
        self.assertEqual(rows[1]["name"], "Gadget")

    def test_update_replication(self):
        self.primary.execute("INSERT INTO products VALUES (1, 'Widget', 9.99)")
        self._apply_all()
        self.primary.execute("UPDATE products SET price = 12.99 WHERE id = 1")
        lsn = self._apply_all()
        rows = self.replica.execute("SELECT price FROM products WHERE id = 1").fetchall()
        self.assertAlmostEqual(rows[0]["price"], 12.99, places=2)
        self.assertGreater(lsn, 0)

    def test_delete_replication(self):
        self.primary.execute("INSERT INTO products VALUES (1, 'Widget', 9.99)")
        self.primary.execute("INSERT INTO products VALUES (2, 'Gadget', 24.99)")
        self._apply_all()
        self.primary.execute("DELETE FROM products WHERE id = 1")
        self._apply_all()
        ids = {r["id"] for r in
               self.replica.execute("SELECT id FROM products").fetchall()}
        self.assertNotIn(1, ids)
        self.assertIn(2, ids)

    def test_incremental_replication(self):
        self.primary.execute("INSERT INTO products VALUES (1, 'A', 1.0)")
        last = self._apply_all(0)
        self.primary.execute("INSERT INTO products VALUES (2, 'B', 2.0)")
        self._apply_all(last)
        rows = self.replica.execute("SELECT COUNT(*) AS n FROM products").fetchall()
        self.assertEqual(rows[0]["n"], 2)

    def test_only_published_tables_replicate(self):
        self.primary.execute("CREATE TABLE internal_log (id INTEGER PRIMARY KEY, msg TEXT)")
        self.primary.execute("INSERT INTO products VALUES (1, 'Widget', 9.99)")
        self.primary.execute("INSERT INTO internal_log VALUES (1, 'debug')")
        self._apply_all()
        # products replicated; internal_log not in replica schema anyway but
        # more importantly it was never written to changelog
        entries = self._shared_log.read_since(0)
        tables  = {e.table for e in entries}
        self.assertIn("products", tables)
        self.assertNotIn("internal_log", tables)

    def test_multiple_operations_correct_lsns(self):
        self.primary.execute("INSERT INTO products VALUES (1, 'A', 1.0)")
        self.primary.execute("INSERT INTO products VALUES (2, 'B', 2.0)")
        self.primary.execute("UPDATE products SET price = 99.0 WHERE id = 1")
        self.primary.execute("DELETE FROM products WHERE id = 2")
        entries = self._shared_log.read_since(0)
        self.assertEqual(len(entries), 4)
        lsns = [e.lsn for e in entries]
        self.assertEqual(lsns, sorted(set(lsns)))


# ── HTTP endpoint tests ───────────────────────────────────────────────────────

class TestHTTPReplicationEndpoint(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from hyperion.http_server import HTTPServerMode
        cls.db = Database(":memory:")
        cls.db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT)")
        cls.db.execute("CREATE PUBLICATION pub FOR TABLE orders")
        cls.db.execute("INSERT INTO orders VALUES (1, 'pending')")
        cls.db.execute("INSERT INTO orders VALUES (2, 'shipped')")
        cls.server = HTTPServerMode(cls.db, host="127.0.0.1", port=0)
        cls.thread = cls.server.start()
        cls.base = "http://127.0.0.1:{}".format(cls.server.address[1])

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.db.close()

    def _get(self, path: str) -> dict:
        import urllib.request, json
        with urllib.request.urlopen(self.base + path, timeout=5) as resp:
            return json.loads(resp.read().decode())

    def test_replication_changes_empty_since_latest(self):
        current_lsn = self.db._catalog.lsn
        data = self._get(f"/replication/changes?publication=pub&since={current_lsn}")
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["changes"], [])

    def test_replication_changes_since_zero(self):
        data = self._get("/replication/changes?publication=pub&since=0")
        self.assertEqual(data["status"], "ok")
        self.assertEqual(len(data["changes"]), 2)
        tables = {c["table"] for c in data["changes"]}
        self.assertEqual(tables, {"orders"})

    def test_replication_changes_unknown_publication(self):
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/replication/changes?publication=no_such_pub&since=0")
        self.assertEqual(ctx.exception.code, 404)

    def test_replication_changes_has_lsn_field(self):
        data = self._get("/replication/changes?publication=pub&since=0")
        self.assertIn("lsn", data)
        self.assertGreater(data["lsn"], 0)


# ── Catalog persistence tests ─────────────────────────────────────────────────

class TestCatalogPersistence(unittest.TestCase):
    """Publications and subscriptions survive db.close() / re-open."""

    def test_publication_persists(self):
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".hdb", delete=False) as f:
            path = f.name
        try:
            db = Database(path)
            db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY)")
            db.execute("CREATE PUBLICATION pub1 FOR TABLE orders")
            db.close()

            db2 = Database(path)
            self.assertIn("pub1", db2._catalog.publications)
            self.assertEqual(db2._catalog.publications["pub1"].tables, ["orders"])
            db2.close()
        finally:
            for ext in ("", "-wal"):
                try:
                    os.unlink(path + (ext if ext != "" else ""))
                except FileNotFoundError:
                    pass

    def test_lsn_persists(self):
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".hdb", delete=False) as f:
            path = f.name
        try:
            db = Database(path)
            db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
            db.execute("CREATE PUBLICATION pub FOR TABLE t")
            db.execute("INSERT INTO t VALUES (1)")
            db.execute("INSERT INTO t VALUES (2)")
            lsn_before = db._catalog.lsn
            self.assertEqual(lsn_before, 2)
            db.close()

            db2 = Database(path)
            self.assertEqual(db2._catalog.lsn, 2)
            db2.close()
        finally:
            for suffix in (".hdb", ".hdb-wal"):
                try:
                    os.unlink(path.replace(".hdb", "") + suffix)
                except FileNotFoundError:
                    pass


if __name__ == "__main__":
    unittest.main()
