"""Tests for the Hyperion REST HTTP server."""
import json
import threading
import time
import urllib.request
import urllib.error
import pytest

from hyperion import Database
from hyperion.http_server import HTTPServerMode


# ── Fixture ───────────────────────────────────────────────────────────────────

@pytest.fixture
def server():
    db  = Database(":memory:")
    srv = HTTPServerMode(db, host="127.0.0.1", port=0)   # port=0 → OS picks free port
    srv.start()
    _, port = srv.address
    base = f"http://127.0.0.1:{port}"
    yield base, db
    srv.shutdown()
    db.close()


def _get(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _post(url: str, body: dict) -> tuple[int, dict]:
    data = json.dumps(body).encode()
    req  = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# ── GET /health ───────────────────────────────────────────────────────────────

def test_health(server):
    base, _ = server
    code, body = _get(f"{base}/health")
    assert code == 200
    assert body["status"] == "ok"
    assert "tables" in body
    assert "indexes" in body


# ── POST /query — basic ───────────────────────────────────────────────────────

def test_query_select_no_from(server):
    base, _ = server
    code, body = _post(f"{base}/query", {"sql": "SELECT 1+1 AS result"})
    assert code == 200
    assert body["status"] == "ok"
    assert body["rows"][0]["result"] == 2


def test_query_ddl_and_select(server):
    base, db = server
    _post(f"{base}/query", {"sql": "CREATE TABLE t (id INTEGER, name TEXT)"})
    _post(f"{base}/query", {"sql": "INSERT INTO t VALUES (1, 'Alice')"})
    code, body = _post(f"{base}/query", {"sql": "SELECT * FROM t"})
    assert code == 200
    assert body["rows"][0]["name"] == "Alice"


def test_query_returns_description(server):
    base, _ = server
    _post(f"{base}/query", {"sql": "CREATE TABLE t (id INTEGER, val TEXT)"})
    code, body = _post(f"{base}/query", {"sql": "SELECT id, val FROM t WHERE id = -1"})
    assert code == 200
    assert body["description"] is not None
    names = [col["name"] for col in body["description"]]
    assert names == ["id", "val"]


def test_query_returns_rowcount(server):
    base, _ = server
    _post(f"{base}/query", {"sql": "CREATE TABLE t (id INTEGER)"})
    _post(f"{base}/query", {"sql": "INSERT INTO t VALUES (1)"})
    _post(f"{base}/query", {"sql": "INSERT INTO t VALUES (2)"})
    code, body = _post(f"{base}/query", {"sql": "DELETE FROM t"})
    assert code == 200
    assert body["rowcount"] == 2


def test_query_returns_lastrowid(server):
    base, _ = server
    _post(f"{base}/query", {"sql": "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, v TEXT)"})
    code, body = _post(f"{base}/query", {"sql": "INSERT INTO t (v) VALUES ('x')"})
    assert code == 200
    assert body["lastrowid"] == 1


# ── POST /query — parameter binding ──────────────────────────────────────────

def test_query_positional_params(server):
    base, _ = server
    _post(f"{base}/query", {"sql": "CREATE TABLE t (id INTEGER, name TEXT)"})
    _post(f"{base}/query", {"sql": "INSERT INTO t VALUES (?, ?)", "params": [1, "Alice"]})
    code, body = _post(f"{base}/query", {"sql": "SELECT name FROM t WHERE id = ?", "params": [1]})
    assert code == 200
    assert body["rows"][0]["name"] == "Alice"


def test_query_named_params(server):
    base, _ = server
    _post(f"{base}/query", {"sql": "CREATE TABLE t (id INTEGER, val TEXT)"})
    _post(f"{base}/query", {"sql": "INSERT INTO t VALUES (:id, :val)", "params": {"id": 7, "val": "hi"}})
    code, body = _post(f"{base}/query", {"sql": "SELECT val FROM t WHERE id = :id", "params": {"id": 7}})
    assert code == 200
    assert body["rows"][0]["val"] == "hi"


# ── POST /query — multi-statement ─────────────────────────────────────────────

def test_query_multi_statement(server):
    base, _ = server
    sql = "CREATE TABLE t (id INTEGER); INSERT INTO t VALUES (1); INSERT INTO t VALUES (2);"
    code, body = _post(f"{base}/query", {"sql": sql})
    assert code == 200
    code, body = _post(f"{base}/query", {"sql": "SELECT COUNT(*) AS n FROM t"})
    assert body["rows"][0]["n"] == 2


# ── POST /query — error handling ──────────────────────────────────────────────

def test_query_syntax_error(server):
    base, _ = server
    code, body = _post(f"{base}/query", {"sql": "SELECT FROM"})
    assert body["status"] == "error"
    assert "error_type" in body
    assert "message" in body


def test_query_missing_table(server):
    base, _ = server
    code, body = _post(f"{base}/query", {"sql": "SELECT * FROM nonexistent"})
    assert body["status"] == "error"


def test_query_empty_sql(server):
    base, _ = server
    code, body = _post(f"{base}/query", {"sql": ""})
    assert body["status"] == "error"


def test_query_invalid_json(server):
    base, _ = server
    req = urllib.request.Request(
        f"{base}/query", data=b"not json", method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as r:
            body = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = json.loads(e.read())
    assert body["status"] == "error"


# ── GET /tables ───────────────────────────────────────────────────────────────

def test_get_tables_empty(server):
    base, _ = server
    code, body = _get(f"{base}/tables")
    assert code == 200
    assert body["tables"] == []


def test_get_tables_after_create(server):
    base, _ = server
    _post(f"{base}/query", {"sql": "CREATE TABLE users (id INTEGER)"})
    _post(f"{base}/query", {"sql": "CREATE TABLE orders (id INTEGER)"})
    code, body = _get(f"{base}/tables")
    assert code == 200
    assert "users" in body["tables"]
    assert "orders" in body["tables"]


# ── GET /tables/{name} ────────────────────────────────────────────────────────

def test_get_table_schema(server):
    base, _ = server
    _post(f"{base}/query", {"sql": "CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"})
    code, body = _get(f"{base}/tables/t")
    assert code == 200
    assert body["table"] == "t"
    cols = {c["name"]: c for c in body["columns"]}
    assert "id" in cols
    assert "name" in cols
    assert cols["name"]["nullable"] is False


def test_get_table_schema_not_found(server):
    base, _ = server
    code, body = _get(f"{base}/tables/nonexistent")
    assert code == 404
    assert body["status"] == "error"


def test_get_table_includes_indexes(server):
    base, _ = server
    _post(f"{base}/query", {"sql": "CREATE TABLE t (id INTEGER, val TEXT)"})
    _post(f"{base}/query", {"sql": "CREATE INDEX idx_val ON t(val)"})
    code, body = _get(f"{base}/tables/t")
    assert code == 200
    idx_names = [i["name"] for i in body["indexes"]]
    assert "idx_val" in idx_names


# ── GET /indexes ──────────────────────────────────────────────────────────────

def test_get_indexes(server):
    base, _ = server
    _post(f"{base}/query", {"sql": "CREATE TABLE t (id INTEGER, val TEXT)"})
    _post(f"{base}/query", {"sql": "CREATE UNIQUE INDEX idx_val ON t(val)"})
    code, body = _get(f"{base}/indexes")
    assert code == 200
    idx = next((i for i in body["indexes"] if i["name"] == "idx_val"), None)
    assert idx is not None
    assert idx["unique"] is True
    assert idx["table"] == "t"
    assert "val" in idx["columns"]


# ── POST /vacuum ──────────────────────────────────────────────────────────────

def test_vacuum(server):
    base, _ = server
    _post(f"{base}/query", {"sql": "CREATE TABLE t (id INTEGER)"})
    code, body = _post(f"{base}/vacuum", {})
    assert code == 200
    assert body["status"] == "ok"


# ── POST /analyze ─────────────────────────────────────────────────────────────

def test_analyze(server):
    base, _ = server
    _post(f"{base}/query", {"sql": "CREATE TABLE t (id INTEGER)"})
    code, body = _post(f"{base}/analyze", {})
    assert code == 200
    assert body["status"] == "ok"


# ── CORS headers ──────────────────────────────────────────────────────────────

def test_cors_headers_on_get(server):
    base, _ = server
    with urllib.request.urlopen(f"{base}/health") as r:
        assert r.headers.get("Access-Control-Allow-Origin") == "*"


def test_cors_headers_on_post(server):
    base, _ = server
    req = urllib.request.Request(
        f"{base}/query",
        data=json.dumps({"sql": "SELECT 1"}).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as r:
        assert r.headers.get("Access-Control-Allow-Origin") == "*"


# ── 404 routing ───────────────────────────────────────────────────────────────

def test_unknown_route(server):
    base, _ = server
    code, body = _get(f"{base}/unknown")
    assert code == 404
    assert body["status"] == "error"


# ── Transactions ──────────────────────────────────────────────────────────────

def test_transaction_commit(server):
    base, _ = server
    _post(f"{base}/query", {"sql": "CREATE TABLE t (id INTEGER)"})
    _post(f"{base}/query", {"sql": "BEGIN"})
    _post(f"{base}/query", {"sql": "INSERT INTO t VALUES (1)"})
    _post(f"{base}/query", {"sql": "COMMIT"})
    code, body = _post(f"{base}/query", {"sql": "SELECT COUNT(*) AS n FROM t"})
    assert body["rows"][0]["n"] == 1
