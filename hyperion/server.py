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

Connection pooling
------------------
The server runs a fixed-size pool of worker threads (``pool_size``, default
10).  Incoming connections are placed on a bounded queue (``max_queue``,
default 100).  When the queue is full the server immediately responds with
a ServerBusyError and closes the socket.

SHOW PROCESSLIST
----------------
``SHOW PROCESSLIST`` is intercepted at the server layer and returns one row
per currently-open connection:

    id | host | command | time | state | info

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

    # Custom pool:
    srv = Server(db, port=5433, pool_size=20, max_queue=200)

CLI (via __main__.py):
    python -m hyperion server mydb.hyp --port 5433
    python -m hyperion server mydb.hyp --socket /tmp/hyperion.sock
    python -m hyperion server mydb.hyp --pool-size 20 --max-queue 200
"""
from __future__ import annotations

import base64
import itertools
import json
import os
import queue
import socket
import struct
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .database import Database

_HDR = struct.Struct("!I")   # 4-byte big-endian unsigned int (message length)

_PL_COLS = ["id", "host", "command", "time", "state", "info"]


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


class _ConnInfo:
    """Mutable snapshot of one active connection (used for SHOW PROCESSLIST)."""
    __slots__ = ("id", "host", "command", "started", "state")

    def __init__(self, conn_id: int, host: str) -> None:
        self.id      = conn_id
        self.host    = host
        self.command = ""
        self.started = time.monotonic()
        self.state   = "idle"


class _PooledServer:
    """Fixed-size worker-thread pool for Hyperion connections.

    Accepts connections on a listening socket and dispatches them to a
    bounded queue.  A fixed number of worker threads drain the queue.
    When the queue is full an immediate error response is returned.
    """

    def __init__(self, db: "Database", *,
                 host: str = "127.0.0.1",
                 port: int = 5433,
                 socket_path: str | None = None,
                 pool_size: int = 10,
                 max_queue: int = 100) -> None:
        self._db           = db
        self._pool_size    = pool_size
        self._shutdown     = threading.Event()
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue)
        self._processlist: dict[int, _ConnInfo] = {}
        self._pl_lock      = threading.Lock()
        self._id_counter   = itertools.count(1)
        self._socket_path  = socket_path

        # Bind the listening socket
        if socket_path:
            if os.path.exists(socket_path):
                os.unlink(socket_path)
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.bind(socket_path)
        else:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((host, port))
        self._sock.listen(pool_size)
        self.server_address = self._sock.getsockname()

        # Start the fixed worker pool
        self._workers: list[threading.Thread] = []
        for _ in range(pool_size):
            t = threading.Thread(target=self._worker, daemon=True)
            t.start()
            self._workers.append(t)

    # ── Accept loop ──────────────────────────────────────────────────────────

    def serve_forever(self) -> None:
        self._sock.settimeout(1.0)
        while not self._shutdown.is_set():
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._queue.put_nowait((conn, addr))
            except queue.Full:
                try:
                    _send(conn, {
                        "status":     "error",
                        "error_type": "ServerBusyError",
                        "message":    (
                            f"Server busy: connection queue full "
                            f"(max_queue={self._queue.maxsize})"
                        ),
                    })
                except OSError:
                    pass
                finally:
                    conn.close()

    # ── Worker threads ────────────────────────────────────────────────────────

    def _worker(self) -> None:
        while not self._shutdown.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:          # poison pill from shutdown()
                self._queue.task_done()
                break
            conn, addr = item
            try:
                self._handle_conn(conn, addr)
            finally:
                self._queue.task_done()

    def _handle_conn(self, conn: socket.socket, addr) -> None:
        conn_id = next(self._id_counter)
        host    = addr[0] if isinstance(addr, tuple) else (str(addr) or "unix")
        info    = _ConnInfo(conn_id, host)
        cur     = self._db.cursor()

        with self._pl_lock:
            self._processlist[conn_id] = info

        try:
            while True:
                req = _recv(conn)
                if req is None:
                    break
                sql = (req.get("sql") or "").strip()
                if sql.upper().startswith("SHOW PROCESSLIST"):
                    resp = self._processlist_resp()
                else:
                    with self._pl_lock:
                        info.command = sql
                        info.started = time.monotonic()
                        info.state   = "active"
                    resp = _handle_request(cur, req)
                    with self._pl_lock:
                        info.command = ""
                        info.started = time.monotonic()
                        info.state   = "idle"
                _send(conn, resp)
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        finally:
            with self._pl_lock:
                self._processlist.pop(conn_id, None)
            try:
                conn.close()
            except OSError:
                pass

    # ── SHOW PROCESSLIST ──────────────────────────────────────────────────────

    def _processlist_resp(self) -> dict:
        now = time.monotonic()
        with self._pl_lock:
            rows = [
                {
                    "id":      info.id,
                    "host":    info.host,
                    "command": info.state.upper(),
                    "time":    round(now - info.started, 3),
                    "state":   info.state,
                    "info":    info.command or None,
                }
                for info in self._processlist.values()
            ]
        return {
            "status":      "ok",
            "rows":        rows,
            "rowcount":    len(rows),
            "lastrowid":   None,
            "description": [{"name": c, "type_code": None} for c in _PL_COLS],
        }

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def shutdown(self) -> None:
        self._shutdown.set()
        # Unblock workers with poison pills
        for _ in self._workers:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
        try:
            self._sock.close()
        except OSError:
            pass
        if self._socket_path:
            try:
                os.unlink(self._socket_path)
            except OSError:
                pass


class Server:
    """Hyperion database server.

    Binds a TCP port or a Unix-domain socket and dispatches incoming
    connections to a fixed-size worker thread pool.  Thread safety is
    provided by the Database's internal RWLock.

    Parameters
    ----------
    db          : Database instance to serve.
    host        : TCP host to bind (ignored when socket_path is set).
    port        : TCP port to bind (ignored when socket_path is set).
    socket_path : Path to a Unix-domain socket file.  Takes priority over
                  host/port when set.
    pool_size   : Number of worker threads (default 10).
    max_queue   : Maximum pending connections before rejecting (default 100).
    """

    def __init__(self, db: "Database", *,
                 host: str = "127.0.0.1",
                 port: int = 5433,
                 socket_path: str | None = None,
                 pool_size: int = 10,
                 max_queue: int = 100) -> None:
        self._db   = db
        self._pool = _PooledServer(
            db,
            host=host,
            port=port,
            socket_path=socket_path,
            pool_size=pool_size,
            max_queue=max_queue,
        )

    @property
    def address(self) -> tuple | str:
        """Bound address — (host, port) for TCP or socket path for Unix."""
        return self._pool.server_address

    def serve_forever(self) -> None:
        """Block and serve until shutdown() is called from another thread."""
        self._pool.serve_forever()

    def start(self) -> threading.Thread:
        """Start serving in a background daemon thread and return it."""
        t = threading.Thread(target=self._pool.serve_forever, daemon=True)
        t.start()
        return t

    def shutdown(self) -> None:
        """Stop accepting new connections and signal workers to exit."""
        self._pool.shutdown()
