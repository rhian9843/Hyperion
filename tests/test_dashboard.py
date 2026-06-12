"""Tests for the web dashboard served at GET / by the HTTP server."""
import json
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hyperion.database import Database
from hyperion.http_server import HTTPServerMode, _DASHBOARD_HTML


def _get(url: str) -> tuple[int, str]:
    try:
        r = urllib.request.urlopen(url, timeout=5)
        return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


class _ServerFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = Database(":memory:")
        cls.db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, email TEXT UNIQUE)")
        cls.db.execute("INSERT INTO users VALUES (1, 'Alice', 'alice@example.com')")
        cls.db.execute("INSERT INTO users VALUES (2, 'Bob', 'bob@example.com')")
        cls.db.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, title TEXT, price REAL)")
        cls.db.execute("CREATE INDEX idx_price ON products(price)")

        cls.srv = HTTPServerMode(cls.db, host="127.0.0.1", port=0)
        cls._thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls._thread.start()
        time.sleep(0.05)
        cls.host, cls.port = cls.srv.address
        cls.base = f"http://{cls.host}:{cls.port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.shutdown()
        cls._thread.join(timeout=2.0)
        cls.db.close()


class TestDashboardConstant(unittest.TestCase):
    """Sanity checks on the HTML string itself."""

    def test_is_valid_html(self):
        self.assertTrue(_DASHBOARD_HTML.strip().startswith("<!DOCTYPE html>"))

    def test_contains_sql_editor(self):
        self.assertIn("sql-editor", _DASHBOARD_HTML)

    def test_calls_health_endpoint(self):
        self.assertIn("/health", _DASHBOARD_HTML)

    def test_calls_tables_endpoint(self):
        self.assertIn("/tables", _DASHBOARD_HTML)

    def test_calls_query_endpoint(self):
        self.assertIn("/query", _DASHBOARD_HTML)

    def test_no_external_js(self):
        # Must not load from CDNs
        for cdn in ("cdn.jsdelivr.net", "unpkg.com", "cdnjs.cloudflare.com",
                    "code.jquery.com", "googleapis.com"):
            self.assertNotIn(cdn, _DASHBOARD_HTML,
                             f"Dashboard loads from external CDN: {cdn}")

    def test_self_contained_single_page(self):
        # No <link rel="stylesheet" href="..."> or <script src="..."> referencing external files
        import re
        external_link = re.search(r'<link[^>]+href=["\']https?://', _DASHBOARD_HTML)
        external_script = re.search(r'<script[^>]+src=["\']https?://', _DASHBOARD_HTML)
        self.assertIsNone(external_link, "Dashboard has external stylesheet link")
        self.assertIsNone(external_script, "Dashboard has external script src")

    def test_has_run_button(self):
        self.assertIn("runSql", _DASHBOARD_HTML)

    def test_has_vacuum_button(self):
        self.assertIn("vacuum", _DASHBOARD_HTML.lower())

    def test_has_analyze_button(self):
        self.assertIn("analyze", _DASHBOARD_HTML.lower())

    def test_ctrl_enter_shortcut(self):
        self.assertIn("ctrlKey", _DASHBOARD_HTML)

    def test_has_xss_escape_function(self):
        # Must HTML-escape output to prevent XSS
        self.assertIn("&amp;", _DASHBOARD_HTML)   # esc() escapes ampersands
        self.assertIn("&lt;",  _DASHBOARD_HTML)   # and angle brackets


class TestDashboardServed(_ServerFixture):
    """Integration tests: the server actually serves the dashboard."""

    def test_get_root_returns_200(self):
        code, body = _get(self.base + "/")
        self.assertEqual(code, 200)

    def test_get_root_content_type_is_html(self):
        r = urllib.request.urlopen(self.base + "/", timeout=5)
        ct = r.headers.get("Content-Type", "")
        self.assertIn("text/html", ct)

    def test_get_root_body_is_html(self):
        _, body = _get(self.base + "/")
        self.assertTrue(body.strip().startswith("<!DOCTYPE html>"))

    def test_get_root_without_slash(self):
        # http://host:port  (no trailing slash)
        code, body = _get(self.base)
        self.assertEqual(code, 200)
        self.assertIn("<!DOCTYPE html>", body)

    def test_dashboard_contains_hyperion_title(self):
        _, body = _get(self.base + "/")
        self.assertIn("Hyperion", body)

    def test_dashboard_references_query_endpoint(self):
        _, body = _get(self.base + "/")
        self.assertIn("/query", body)

    def test_dashboard_references_health_endpoint(self):
        _, body = _get(self.base + "/")
        self.assertIn("/health", body)

    def test_dashboard_references_tables_endpoint(self):
        _, body = _get(self.base + "/")
        self.assertIn("/tables", body)

    def test_dashboard_is_non_empty(self):
        _, body = _get(self.base + "/")
        self.assertGreater(len(body), 5000)

    def test_existing_api_endpoints_unaffected(self):
        """Adding GET / must not break any existing REST endpoints."""
        code, body = _get(self.base + "/health")
        d = json.loads(body)
        self.assertEqual(d["status"], "ok")
        self.assertEqual(d["tables"], 2)

    def test_tables_endpoint_still_works(self):
        code, body = _get(self.base + "/tables")
        d = json.loads(body)
        self.assertIn("users", d["tables"])
        self.assertIn("products", d["tables"])

    def test_table_schema_endpoint_still_works(self):
        code, body = _get(self.base + "/tables/users")
        self.assertEqual(code, 200)
        d = json.loads(body)
        self.assertEqual(d["table"], "users")
        col_names = [c["name"] for c in d["columns"]]
        self.assertIn("id", col_names)
        self.assertIn("email", col_names)

    def test_unknown_route_still_404(self):
        code, _ = _get(self.base + "/nonexistent-route-xyz")
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
