"""Server mode: expose a Hyperion Database over a TCP or Unix-domain socket.

Protocol
--------
Every message is length-prefixed: a 4-byte big-endian uint32 gives the
byte-length of the UTF-8 JSON payload that follows.

Client → server  (request):
    {"sql": "SELECT ...", "params": null | [value, ...]}

Server → client  (response — success):
    {"status": "ok", "rows": [[v, ...], ...], "rowcount": N,
     "description": [{"name": col, "type_code": null}, ...] | null}

Server → client  (response — error):
    {"status": "error", "error_type": "UniqueConstraintError",
     "message": "UNIQUE constraint failed: ..."}

Each TCP/Unix connection gets its own Cursor.  The underlying Database and
its WAL lock are shared, so multi-reader / single-writer semantics are
preserved across connections.

Usage
-----
Programmatic:
    from hyperion import Database
    from hyperion.server import Server

    db  = Database("mydb.hyp")
    srv = Server(db, host="127.0.0.1", port=5433)
    srv.serve_forever()          # blocks; call srv.shutdown() from another thread

    # Unix socket:
    srv = Server(db, socket_path="/tmp/hyperion.sock")
    srv.serve_forever()

CLI (via __main__.py):
    python -m hyperion server mydb.hyp --port 5433
    python -m hyperion server mydb.hyp --socket /tmp/hyperion.sock
"""
from __future__ import annotations

import base64
import json
import os
import socket
import socketserver
import struct
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .database import Database

_HDR = struct.Struct("!I")   # 4-byte big-endian unsigned int (message length)


def _json_default(obj):
    """JSON serialiser for types not handled by the default encoder."""
    if isinstance(obj, (bytes, bytearray)):
        return {"__blob__": base64.b64encode(obj).decode("ascii")}
    return str(obj)


def _send(sock: socket.socket, payload: dict) -> None:
    data = json.dumps(payload, default=_json_default).encode()
    sock.sendall(_HDR.pack(len(data)) + data)


def _decode_blobs(obj):
    """Recursively convert {"__blob__": "<b64>"} sentinels back to bytes."""
    if isinstance(obj, dict):
        if "__blob__" in obj and len(obj) == 1:
            return base64.b64decode(obj["__blob__"])
        return {k: _decode_blobs(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decode_blobs(v) for v in obj]
    return obj


def _recv(sock: socket.socket) -> dict | None:
    """Read one length-prefixed JSON message.  Returns None on EOF."""
    hdr = _recv_exactly(sock, _HDR.size)
    if hdr is None:
        return None
    (length,) = _HDR.unpack(hdr)
    body = _recv_exactly(sock, length)
    if body is None:
        return None
    return _decode_blobs(json.loads(body.decode()))


def _recv_exactly(sock: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _handle_request(cur, req: dict) -> dict:
    """Execute one SQL request on an existing cursor and return a response dict.

    The cursor is created once per connection and reused, so transaction
    state (BEGIN / COMMIT / ROLLBACK / SAVEPOINTs) is preserved across
    multiple requests on the same connection.
    """
    sql    = req.get("sql", "")
    params = req.get("params") or None
    try:
        cur.execute(sql, params)
        rows = cur.fetchall()
        description = (
            [{"name": col[0], "type_code": None} for col in cur.description]
            if cur.description else None
        )
        return {
            "status":      "ok",
            "rows":        rows,
            "rowcount":    cur.rowcount,
            "lastrowid":   getattr(cur, "lastrowid", None),
            "description": description,
        }
    except Exception as exc:
        return {
            "status":     "error",
            "error_type": type(exc).__name__,
            "message":    str(exc),
        }


class _ConnectionHandler(socketserver.BaseRequestHandler):
    """Handle one client connection for the duration of its lifetime.

    One Cursor is created per connection and reused for every request,
    preserving transaction state across round-trips.
    """

    def handle(self) -> None:
        db: "Database" = self.server.hyperion_db  # type: ignore[attr-defined]
        cur  = db.cursor()   # one cursor per connection — preserves txn state
        sock: socket.socket = self.request
        try:
            while True:
                req = _recv(sock)
                if req is None:
                    break
                resp = _handle_request(cur, req)
                _send(sock, resp)
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass


class _ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


class _ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


class Server:
    """Hyperion database server.

    Binds a TCP port or a Unix-domain socket and dispatches each incoming
    connection to a dedicated thread.  Thread safety is provided by the
    Database's internal RWLock — no extra locking is needed here.

    Parameters
    ----------
    db          : Database instance to serve.
    host        : TCP host to bind (ignored when socket_path is set).
    port        : TCP port to bind (ignored when socket_path is set).
    socket_path : Path to a Unix-domain socket file.  Takes priority over
                  host/port when set.
    """

    def __init__(self, db: "Database", *,
                 host: str = "127.0.0.1",
                 port: int = 5433,
                 socket_path: str | None = None) -> None:
        self._db = db
        if socket_path:
            if os.path.exists(socket_path):
                os.unlink(socket_path)
            self._server: socketserver.BaseServer = _ThreadingUnixServer(
                socket_path, _ConnectionHandler)
        else:
            self._server = _ThreadingTCPServer((host, port), _ConnectionHandler)
        self._server.hyperion_db = db  # type: ignore[attr-defined]

    @property
    def address(self) -> tuple | str:
        """Bound address — (host, port) for TCP or socket path for Unix."""
        return self._server.server_address

    def serve_forever(self) -> None:
        """Block and serve until shutdown() is called from another thread."""
        self._server.serve_forever()

    def start(self) -> threading.Thread:
        """Start serving in a background daemon thread and return it."""
        t = threading.Thread(target=self._server.serve_forever, daemon=True)
        t.start()
        return t

    def shutdown(self) -> None:
        """Stop accepting new connections and close the server socket."""
        self._server.shutdown()
        self._server.server_close()
