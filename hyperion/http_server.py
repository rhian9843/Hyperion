"""Lightweight HTTP REST server for Hyperion — no external dependencies.

Endpoints
---------
POST /query
    Body: {"sql": "...", "params": [...]}
    Returns: {"status": "ok", "rows": [...], "rowcount": N,
              "lastrowid": N|null, "description": [...]}

GET  /tables
    Returns: {"tables": ["t1", "t2", ...]}

GET  /tables/{name}
    Returns: {"table": "name", "columns": [...], "indexes": [...]}

GET  /indexes
    Returns: {"indexes": [{"name": ..., "table": ..., "columns": [...],
                            "unique": bool}, ...]}

POST /vacuum
    Returns: {"status": "ok"}

POST /analyze
    Returns: {"status": "ok"}

GET  /health
    Returns: {"status": "ok", "tables": N, "indexes": N}

All error responses: {"status": "error", "error_type": "...", "message": "..."}

Usage
-----
    python -m hyperion http mydb.hdb --port 8080
"""
from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .database import Database

_CORS_HEADERS = {
    "Access-Control-Allow-Origin":  "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


def _json_default(obj):
    if isinstance(obj, (bytes, bytearray)):
        return {"__blob__": base64.b64encode(obj).decode("ascii")}
    return str(obj)


def _ok(data: dict) -> tuple[int, dict]:
    return 200, {"status": "ok", **data}


def _err(exc: Exception, code: int = 400) -> tuple[int, dict]:
    return code, {
        "status":     "error",
        "error_type": type(exc).__name__,
        "message":    str(exc),
    }


def _not_found(msg: str) -> tuple[int, dict]:
    return 404, {"status": "error", "error_type": "NotFound", "message": msg}


def _method_not_allowed() -> tuple[int, dict]:
    return 405, {"status": "error", "error_type": "MethodNotAllowed",
                 "message": "Method not allowed"}


class _Handler(BaseHTTPRequestHandler):

    # ── helpers ───────────────────────────────────────────────────────────────

    @property
    def _db(self) -> "Database":
        return self.server.hyperion_db  # type: ignore[attr-defined]

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body, default=_json_default).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in _CORS_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # silence default access log
        pass

    # ── routing ───────────────────────────────────────────────────────────────

    def do_OPTIONS(self):
        self.send_response(204)
        for k, v in _CORS_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()

    def do_GET(self):
        path = self.path.rstrip("/")
        if path == "/tables":
            self._send(*self._get_tables())
        elif path.startswith("/tables/"):
            name = path[len("/tables/"):]
            self._send(*self._get_table(name))
        elif path == "/indexes":
            self._send(*self._get_indexes())
        elif path == "/health":
            self._send(*self._get_health())
        else:
            self._send(*_not_found(f"No route for GET {self.path}"))

    def do_POST(self):
        path = self.path.rstrip("/")
        if path == "/query":
            self._send(*self._post_query())
        elif path == "/vacuum":
            self._send(*self._post_vacuum())
        elif path == "/analyze":
            self._send(*self._post_analyze())
        else:
            self._send(*_not_found(f"No route for POST {self.path}"))

    # ── handlers ─────────────────────────────────────────────────────────────

    def _post_query(self) -> tuple[int, dict]:
        try:
            req = self._read_json()
        except Exception as e:
            return _err(ValueError(f"Invalid JSON: {e}"))

        sql    = req.get("sql", "")
        params = req.get("params") or None
        if not sql.strip():
            return _err(ValueError("'sql' field is required and must not be empty"))

        try:
            cur = self._db.cursor()
            # Use executescript for multi-statement SQL (params not supported in that mode)
            is_multi = sql.count(";") > 1 or (sql.count(";") == 1 and not sql.rstrip().endswith(";"))
            if is_multi and params is None:
                cur.executescript(sql)
            else:
                cur.execute(sql, params)
            rows = cur.fetchall()
            description = (
                [{"name": col[0], "type_code": None} for col in cur.description]
                if cur.description else None
            )
            return _ok({
                "rows":        rows,
                "rowcount":    cur.rowcount,
                "lastrowid":   getattr(cur, "lastrowid", None),
                "description": description,
            })
        except Exception as exc:
            return _err(exc)

    def _get_tables(self) -> tuple[int, dict]:
        try:
            return _ok({"tables": sorted(self._db.tables)})
        except Exception as exc:
            return _err(exc, 500)

    def _get_table(self, name: str) -> tuple[int, dict]:
        try:
            if name not in self._db.tables:
                return _not_found(f"No such table: '{name}'")
            meta   = self._db.tables[name]
            schema = meta.schema
            columns = [
                {
                    "name":     c.name,
                    "type":     c.type + (f"({c.size})" if c.type == "TEXT" and c.size != 255 else ""),
                    "nullable": c.nullable,
                    "unique":   c.unique,
                    "pk":       c.name in (schema.primary_key_columns or []),
                    "default":  c.default,
                }
                for c in schema.columns
            ]
            indexes = [
                {"name": idx_name, "columns": idx_meta.columns, "unique": idx_meta.unique}
                for idx_name, idx_meta in self._db.indexes.items()
                if idx_meta.table_name == name
            ]
            return _ok({"table": name, "columns": columns, "indexes": indexes})
        except Exception as exc:
            return _err(exc, 500)

    def _get_indexes(self) -> tuple[int, dict]:
        try:
            indexes = [
                {
                    "name":    idx_name,
                    "table":   m.table_name,
                    "columns": m.columns,
                    "unique":  m.unique,
                }
                for idx_name, m in sorted(self._db.indexes.items())
            ]
            return _ok({"indexes": indexes})
        except Exception as exc:
            return _err(exc, 500)

    def _post_vacuum(self) -> tuple[int, dict]:
        try:
            self._db.execute("VACUUM")
            return _ok({})
        except Exception as exc:
            return _err(exc, 500)

    def _post_analyze(self) -> tuple[int, dict]:
        try:
            self._db.execute("ANALYZE")
            return _ok({})
        except Exception as exc:
            return _err(exc, 500)

    def _get_health(self) -> tuple[int, dict]:
        try:
            return _ok({
                "tables":  len(self._db.tables),
                "indexes": len(self._db.indexes),
            })
        except Exception as exc:
            return _err(exc, 500)


class HTTPServerMode:
    """Hyperion REST HTTP server.

    Parameters
    ----------
    db   : Database instance to serve.
    host : Bind host (default 127.0.0.1).
    port : Bind port (default 8080).
    """

    def __init__(self, db: "Database", *, host: str = "127.0.0.1", port: int = 8080) -> None:
        self._db = db
        self._server = HTTPServer((host, port), _Handler)
        self._server.hyperion_db = db  # type: ignore[attr-defined]

    @property
    def address(self) -> tuple[str, int]:
        return self._server.server_address  # type: ignore[return-value]

    def serve_forever(self) -> None:
        self._server.serve_forever()

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self._server.serve_forever, daemon=True)
        t.start()
        return t

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()
