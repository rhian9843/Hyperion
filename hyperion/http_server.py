"""Lightweight HTTP REST server for Hyperion — no external dependencies.

Endpoints
---------
GET  /
    Web dashboard — table browser, schema viewer, SQL editor.

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

# ── Web dashboard (served at GET /) ───────────────────────────────────────────

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hyperion Dashboard</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0f1117;--surface:#1a1d27;--border:#2d3148;--accent:#6c8ef5;
      --text:#dde2f0;--muted:#7a82a0;--ok:#4caf7d;--err:#e05c5c;--warn:#e0a84a}
body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--text);
     height:100vh;display:flex;flex-direction:column;overflow:hidden}
/* header */
#hdr{display:flex;align-items:center;gap:12px;padding:10px 18px;
     background:var(--surface);border-bottom:1px solid var(--border);flex-shrink:0}
#hdr h1{font-size:1rem;font-weight:700;letter-spacing:.5px;color:var(--accent)}
#hdr .sep{color:var(--border)}
.stat{font-size:.8rem;color:var(--muted)}
.stat b{color:var(--text)}
#hdr-right{margin-left:auto;display:flex;gap:8px}
/* layout */
#main{display:flex;flex:1;overflow:hidden}
/* sidebar */
#sidebar{width:190px;flex-shrink:0;background:var(--surface);
         border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden}
#sidebar h2{font-size:.7rem;font-weight:600;letter-spacing:1px;text-transform:uppercase;
            color:var(--muted);padding:12px 14px 6px}
#tbl-list{overflow-y:auto;flex:1}
.tbl-item{padding:7px 14px;cursor:pointer;font-size:.85rem;border-left:3px solid transparent;
          white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tbl-item:hover{background:#ffffff0d}
.tbl-item.active{border-left-color:var(--accent);background:#6c8ef514;color:var(--accent)}
/* content */
#content{flex:1;display:flex;flex-direction:column;overflow:hidden}
/* schema pane */
#schema{padding:16px 20px;border-bottom:1px solid var(--border);overflow-y:auto;max-height:45%}
#schema h2{font-size:.85rem;font-weight:600;color:var(--muted);margin-bottom:10px}
#schema h2 span{color:var(--text);font-size:1rem}
.col-table{width:100%;border-collapse:collapse;font-size:.82rem}
.col-table th{text-align:left;padding:5px 10px;color:var(--muted);border-bottom:1px solid var(--border);
              font-weight:500;font-size:.75rem;text-transform:uppercase;letter-spacing:.5px}
.col-table td{padding:5px 10px;border-bottom:1px solid #ffffff08}
.col-table tr:last-child td{border-bottom:none}
.badge{display:inline-block;padding:1px 6px;border-radius:3px;font-size:.7rem;
       font-weight:600;margin-left:4px}
.pk{background:#6c8ef530;color:var(--accent)}
.uq{background:#e0a84a20;color:var(--warn)}
.nn{background:#e05c5c20;color:var(--err)}
.idx-list{margin-top:10px;font-size:.8rem;color:var(--muted)}
.idx-list span{color:var(--text)}
/* sql pane */
#sql-pane{flex:1;display:flex;flex-direction:column;overflow:hidden;padding:14px 20px 16px}
#sql-pane h2{font-size:.8rem;font-weight:600;color:var(--muted);margin-bottom:8px}
#sql-editor{width:100%;font-family:monospace;font-size:.85rem;background:#0d0f19;
             color:var(--text);border:1px solid var(--border);border-radius:5px;
             padding:10px;resize:none;height:90px;outline:none}
#sql-editor:focus{border-color:var(--accent)}
#sql-bar{display:flex;align-items:center;gap:10px;margin-top:8px}
.btn{padding:5px 14px;border-radius:4px;border:none;cursor:pointer;font-size:.82rem;font-weight:600}
.btn-primary{background:var(--accent);color:#fff}
.btn-primary:hover{background:#7fa0ff}
.btn-sm{background:#ffffff12;color:var(--text)}
.btn-sm:hover{background:#ffffff1e}
#sql-status{font-size:.78rem;color:var(--muted)}
/* results */
#results{overflow:auto;flex:1;margin-top:10px}
.res-table{width:100%;border-collapse:collapse;font-size:.82rem;white-space:nowrap}
.res-table th{background:#ffffff0a;padding:5px 12px;text-align:left;
              border-bottom:1px solid var(--border);position:sticky;top:0;
              font-size:.75rem;text-transform:uppercase;letter-spacing:.5px;color:var(--muted)}
.res-table td{padding:5px 12px;border-bottom:1px solid #ffffff06}
.res-table tr:hover td{background:#ffffff06}
.null{color:var(--muted);font-style:italic}
.err-msg{color:var(--err);font-size:.83rem;padding:6px 0}
.ok-msg{color:var(--ok);font-size:.83rem;padding:6px 0}
/* scrollbars */
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:#ffffff20;border-radius:3px}
</style>
</head>
<body>
<div id="hdr">
  <h1>&#x29E6; Hyperion</h1>
  <span class="sep">|</span>
  <span class="stat">tables: <b id="s-tables">&#x2026;</b></span>
  <span class="stat">indexes: <b id="s-indexes">&#x2026;</b></span>
  <div id="hdr-right">
    <button class="btn btn-sm" onclick="vacuum()">VACUUM</button>
    <button class="btn btn-sm" onclick="analyze()">ANALYZE</button>
  </div>
</div>
<div id="main">
  <div id="sidebar">
    <h2>Tables</h2>
    <div id="tbl-list"></div>
  </div>
  <div id="content">
    <div id="schema"><h2>Select a table from the sidebar</h2></div>
    <div id="sql-pane">
      <h2>SQL Editor <span style="color:#ffffff30;font-weight:400">(Ctrl+Enter to run)</span></h2>
      <textarea id="sql-editor" spellcheck="false" placeholder="SELECT * FROM ..."></textarea>
      <div id="sql-bar">
        <button class="btn btn-primary" onclick="runSql()">&#x25B6; Run</button>
        <button class="btn btn-sm" onclick="clearResults()">Clear</button>
        <span id="sql-status"></span>
      </div>
      <div id="results"></div>
    </div>
  </div>
</div>
<script>
const $ = id => document.getElementById(id);
let activeTable = null;

async function api(method, path, body) {
  const opts = {method, headers:{'Content-Type':'application/json'}};
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  return r.json();
}

async function loadHealth() {
  try {
    const d = await api('GET', '/health');
    $('s-tables').textContent  = d.tables  ?? '?';
    $('s-indexes').textContent = d.indexes ?? '?';
  } catch(e) {}
}

async function loadTables() {
  const d = await api('GET', '/tables');
  const list = $('tbl-list');
  list.innerHTML = '';
  (d.tables || []).forEach(t => {
    const el = document.createElement('div');
    el.className = 'tbl-item';
    el.textContent = t;
    el.onclick = () => selectTable(t, el);
    list.appendChild(el);
  });
}

async function selectTable(name, el) {
  document.querySelectorAll('.tbl-item').forEach(e => e.classList.remove('active'));
  el.classList.add('active');
  activeTable = name;
  $('sql-editor').value = 'SELECT * FROM ' + name + ' LIMIT 100';
  const d = await api('GET', '/tables/' + encodeURIComponent(name));
  renderSchema(d);
}

function renderSchema(d) {
  const cols = d.columns || [];
  const idxs = d.indexes || [];
  let html = '<h2>Schema: <span>' + esc(d.table) + '</span></h2>';
  html += '<table class="col-table"><thead><tr>'
        + '<th>Column</th><th>Type</th><th>Nullable</th><th>Flags</th><th>Default</th>'
        + '</tr></thead><tbody>';
  cols.forEach(c => {
    const flags = (c.pk ? '<span class="badge pk">PK</span>' : '')
                + (c.unique && !c.pk ? '<span class="badge uq">UNIQUE</span>' : '')
                + (!c.nullable ? '<span class="badge nn">NOT NULL</span>' : '');
    html += '<tr><td><b>' + esc(c.name) + '</b></td>'
          + '<td>' + esc(c.type) + '</td>'
          + '<td>' + (c.nullable ? '' : '<span style="color:var(--muted)">no</span>') + '</td>'
          + '<td>' + (flags || '') + '</td>'
          + '<td>' + (c.default != null ? esc(String(c.default)) : '') + '</td></tr>';
  });
  html += '</tbody></table>';
  if (idxs.length) {
    html += '<div class="idx-list"><b>Indexes:</b> ';
    html += idxs.map(i =>
      '<span>' + esc(i.name) + '</span> on (' + i.columns.map(esc).join(', ') + ')'
      + (i.unique ? ' <span class="badge uq">UNIQUE</span>' : '')
    ).join(' &nbsp; ');
    html += '</div>';
  }
  $('schema').innerHTML = html;
}

async function runSql() {
  const sql = $('sql-editor').value.trim();
  if (!sql) return;
  const t0 = Date.now();
  $('sql-status').textContent = 'Running…';
  $('results').innerHTML = '';
  try {
    const d = await api('POST', '/query', {sql});
    const ms = Date.now() - t0;
    if (d.status === 'error') {
      $('results').innerHTML = '<div class="err-msg">&#x2715; ' + esc(d.message) + '</div>';
      $('sql-status').textContent = 'Error';
      return;
    }
    const rows = d.rows || [];
    const desc = d.description || [];
    if (!desc.length) {
      const rc = d.rowcount ?? 0;
      $('results').innerHTML = '<div class="ok-msg">&#x2713; OK &mdash; ' + rc + ' row' + (rc===1?'':'s') + ' affected (' + ms + ' ms)</div>';
      $('sql-status').textContent = 'OK · ' + ms + 'ms';
      await loadHealth();
      await loadTables();
      return;
    }
    const cols = desc.map(c => c.name);
    let tbl = '<table class="res-table"><thead><tr>'
            + cols.map(c => '<th>' + esc(c) + '</th>').join('') + '</tr></thead><tbody>';
    rows.forEach(row => {
      const vals = Array.isArray(row) ? row : cols.map(c => row[c]);
      tbl += '<tr>' + vals.map(v =>
        v == null ? '<td class="null">NULL</td>'
                  : '<td>' + esc(String(v)) + '</td>'
      ).join('') + '</tr>';
    });
    tbl += '</tbody></table>';
    $('results').innerHTML = tbl;
    $('sql-status').textContent = rows.length + ' row' + (rows.length===1?'':'s') + ' · ' + ms + 'ms';
  } catch(e) {
    $('results').innerHTML = '<div class="err-msg">&#x2715; ' + esc(e.message) + '</div>';
    $('sql-status').textContent = 'Error';
  }
}

function clearResults() {
  $('results').innerHTML = '';
  $('sql-status').textContent = '';
}

async function vacuum() {
  await api('POST', '/vacuum');
  $('sql-status').textContent = 'VACUUM done';
}

async function analyze() {
  await api('POST', '/analyze');
  $('sql-status').textContent = 'ANALYZE done';
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

$('sql-editor').addEventListener('keydown', e => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); runSql(); }
});

loadHealth();
loadTables();
</script>
</body>
</html>"""


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
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in ("", "/"):
            self._send_html(_DASHBOARD_HTML)
        elif path == "/tables":
            self._send(*self._get_tables())
        elif path.startswith("/tables/"):
            name = path[len("/tables/"):]
            self._send(*self._get_table(name))
        elif path == "/indexes":
            self._send(*self._get_indexes())
        elif path == "/health":
            self._send(*self._get_health())
        elif path == "/replication/changes":
            self._send(*self._get_replication_changes())
        elif path == "/replication/physical/snapshot":
            self._send(*self._get_physical_snapshot())
        elif path == "/replication/physical/changes":
            self._send(*self._get_physical_changes())
        else:
            self._send(*_not_found(f"No route for GET {self.path}"))

    def _send_html(self, html: str) -> None:
        data = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

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

    def _get_replication_changes(self) -> tuple[int, dict]:
        try:
            qs     = self.path.split("?", 1)[1] if "?" in self.path else ""
            params = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
            pub_name  = params.get("publication", "")
            since_lsn = int(params.get("since", "0"))
            if pub_name not in self._db._catalog.publications:
                return _not_found(f"No such publication: '{pub_name}'")
            pub     = self._db._catalog.publications[pub_name]
            changes = self._db.changelog.read_for_publication(pub.tables, since_lsn)
            return _ok({"changes": [e.to_dict() for e in changes],
                        "lsn": self._db._catalog.lsn})
        except Exception as exc:
            return _err(exc, 500)

    def _get_physical_snapshot(self) -> tuple[int, dict]:
        try:
            from .pager import MemoryPager
            db = self._db
            if isinstance(db._pager, MemoryPager):
                return _err(ValueError("Cannot snapshot an in-memory database"))
            with db._lock.read():
                db._pager._file.seek(0)
                db_bytes = db._pager._file.read()
                wal_path = db._pager._path.with_suffix(".wal")
                wal_bytes = wal_path.read_bytes() if wal_path.exists() else b""
                lsn = db._pager._phys_current_lsn
            return _ok({
                "lsn":      lsn,
                "db_data":  base64.b64encode(db_bytes).decode(),
                "wal_data": base64.b64encode(wal_bytes).decode() if wal_bytes else "",
            })
        except Exception as exc:
            return _err(exc, 500)

    def _get_physical_changes(self) -> tuple[int, dict]:
        try:
            qs        = self.path.split("?", 1)[1] if "?" in self.path else ""
            params    = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
            since_lsn = int(params.get("since", "0"))
            db = self._db
            from .pager import MemoryPager
            if isinstance(db._pager, MemoryPager):
                return _ok({"lsn": 0, "pages": [], "snapshot_required": False})
            with db._lock.read():
                lsn = db._pager._phys_current_lsn
                if since_lsn == 0 or since_lsn < db._pager._phys_checkpoint_lsn:
                    return _ok({"lsn": lsn, "snapshot_required": True})
                pages = [
                    {"page_num": pn, "data": base64.b64encode(data).decode()}
                    for pn, (page_lsn, data) in db._pager._phys_dirty.items()
                    if page_lsn > since_lsn
                ]
            return _ok({"lsn": lsn, "pages": pages, "snapshot_required": False})
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
