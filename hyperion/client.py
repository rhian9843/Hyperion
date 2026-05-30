"""PEP 249-compatible client for a Hyperion server.

Connects to a running hyperion.server.Server over TCP or a Unix-domain
socket and exposes the same Connection / Cursor interface as the embedded
``Database`` / ``Cursor`` pair, so application code can swap between
in-process and remote access without changing SQL logic.

Usage
-----
TCP:
    from hyperion.client import connect
    conn = connect(host="127.0.0.1", port=5433)
    cur  = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id = ?", [42])
    row  = cur.fetchone()
    conn.close()

Unix socket:
    conn = connect(socket_path="/tmp/hyperion.sock")
"""
from __future__ import annotations

import base64
import json
import socket
import struct
from typing import Any

_HDR = struct.Struct("!I")


def _encode_blobs(obj):
    """Recursively convert bytes values to {"__blob__": "<b64>"} sentinels."""
    if isinstance(obj, (bytes, bytearray)):
        return {"__blob__": base64.b64encode(obj).decode("ascii")}
    if isinstance(obj, dict):
        return {k: _encode_blobs(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_encode_blobs(v) for v in obj]
    return obj


def _send(sock: socket.socket, payload: dict) -> None:
    data = json.dumps(_encode_blobs(payload)).encode()
    sock.sendall(_HDR.pack(len(data)) + data)


def _recv_exactly(sock: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _decode_blobs(obj):
    """Recursively convert {"__blob__": "<b64>"} sentinels back to bytes."""
    if isinstance(obj, dict):
        if "__blob__" in obj and len(obj) == 1:
            return base64.b64decode(obj["__blob__"])
        return {k: _decode_blobs(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decode_blobs(v) for v in obj]
    return obj


def _recv(sock: socket.socket) -> dict:
    hdr = _recv_exactly(sock, _HDR.size)
    if hdr is None:
        raise ConnectionError("Server closed the connection")
    (length,) = _HDR.unpack(hdr)
    body = _recv_exactly(sock, length)
    if body is None:
        raise ConnectionError("Server closed the connection mid-message")
    return _decode_blobs(json.loads(body.decode()))


# ── Error mapping ─────────────────────────────────────────────────────────────

_ERROR_MAP: dict[str, type] = {}

def _build_error_map() -> None:
    from . import errors
    for name in dir(errors):
        obj = getattr(errors, name)
        if isinstance(obj, type) and issubclass(obj, Exception):
            _ERROR_MAP[name] = obj

_build_error_map()


def _remote_error(resp: dict) -> Exception:
    """Convert a server error response into a local exception."""
    error_type = resp.get("error_type", "HyperionError")
    message    = resp.get("message", "")
    exc_class  = _ERROR_MAP.get(error_type, Exception)
    return exc_class(message)


# ── Cursor ────────────────────────────────────────────────────────────────────

class Cursor:
    """Remote cursor matching the Hyperion Cursor interface."""

    def __init__(self, conn: "Connection") -> None:
        self._conn       = conn
        self.description: list | None = None
        self.rowcount:    int         = -1
        self.lastrowid:   int | None  = None
        self.arraysize:   int         = 1
        self.row_factory              = None  # set by Connection.cursor()
        self._rows:       list        = []
        self._pos:        int         = 0

    def execute(self, sql: str, params: Any = None) -> "Cursor":
        resp = self._conn._send_recv({"sql": sql, "params": params})
        if resp["status"] == "error":
            raise _remote_error(resp)
        raw_desc = resp.get("description")
        self.description = (
            [tuple(col.values()) + (None,) * (7 - len(col)) for col in raw_desc]
            if raw_desc else None
        )
        self.rowcount  = resp.get("rowcount", -1)
        self.lastrowid = resp.get("lastrowid")
        # Rows arrive as lists of values; convert to dicts using description names
        raw_rows = resp.get("rows") or []
        if self.description and raw_rows and isinstance(raw_rows[0], (list, tuple)):
            names = [col[0] for col in self.description]
            self._rows = [dict(zip(names, r)) for r in raw_rows]
        else:
            self._rows = raw_rows
        self._pos = 0
        return self

    def _apply_factory(self, row: dict) -> Any:
        if self.row_factory is not None:
            return self.row_factory(self, row)
        return row

    def fetchone(self) -> Any:
        if self._pos >= len(self._rows):
            return None
        row = self._apply_factory(self._rows[self._pos])
        self._pos += 1
        return row

    def executemany(self, sql: str, seq_of_params) -> "Cursor":
        total_rowcount = 0
        for params in seq_of_params:
            self.execute(sql, params)
            if self.rowcount >= 0:
                total_rowcount += self.rowcount
        self.rowcount = total_rowcount
        return self

    def fetchmany(self, size: int | None = None) -> list:
        n = size if size is not None else self.arraysize
        rows = [self._apply_factory(r) for r in self._rows[self._pos: self._pos + n]]
        self._pos += len(rows)
        return rows

    def fetchall(self) -> list:
        rows = [self._apply_factory(r) for r in self._rows[self._pos:]]
        self._pos = len(self._rows)
        return rows

    def __iter__(self):
        return self

    def __next__(self) -> dict:
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row

    def close(self) -> None:
        pass  # stateless on the server side


# ── Connection ────────────────────────────────────────────────────────────────

class Connection:
    """Remote connection to a Hyperion server.

    Each ``execute`` call is a stateless round-trip — the server uses the
    same underlying Database cursor per connection, so transaction state
    (BEGIN/COMMIT/ROLLBACK) is preserved across calls on one Connection.
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock           = sock
        self._lock           = __import__("threading").Lock()
        self._in_transaction = False
        self.row_factory     = None  # callable(cursor, row_dict) -> Any

    def cursor(self) -> Cursor:
        cur = Cursor(self)
        cur.row_factory = self.row_factory
        return cur

    def execute(self, sql: str, params: Any = None) -> Cursor:
        cur = self.cursor()
        cur.execute(sql, params)
        first = sql.split()[0].upper() if sql.strip() else ""
        if first == "BEGIN":
            self._in_transaction = True
        elif first in ("COMMIT", "ROLLBACK"):
            self._in_transaction = False
        return cur

    def executemany(self, sql: str, seq_of_params) -> Cursor:
        cur = self.cursor()
        cur.executemany(sql, seq_of_params)
        return cur

    def executescript(self, script: str) -> Cursor:
        import re
        cur = self.cursor()
        for stmt in re.split(r";\s*", script):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
        return cur

    def commit(self) -> None:
        self.execute("COMMIT")
        self._in_transaction = False

    def rollback(self) -> None:
        self.execute("ROLLBACK")
        self._in_transaction = False

    @property
    def in_transaction(self) -> bool:
        return self._in_transaction

    def savepoint(self, name: str) -> None:
        self.execute(f"SAVEPOINT {name}")
        self._in_transaction = True

    def release_savepoint(self, name: str) -> None:
        self.execute(f"RELEASE SAVEPOINT {name}")

    def rollback_to_savepoint(self, name: str) -> None:
        self.execute(f"ROLLBACK TO SAVEPOINT {name}")

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def _send_recv(self, payload: dict) -> dict:
        with self._lock:
            _send(self._sock, payload)
            return _recv(self._sock)

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, *_) -> None:
        self.close()


# ── Factory ───────────────────────────────────────────────────────────────────

def connect(*,
            host: str = "127.0.0.1",
            port: int = 5433,
            socket_path: str | None = None,
            timeout: float | None = None) -> Connection:
    """Connect to a running Hyperion server.

    Parameters
    ----------
    host        : TCP host (default 127.0.0.1).
    port        : TCP port (default 5433).
    socket_path : Path to a Unix-domain socket; takes priority over host/port.
    timeout     : Socket timeout in seconds (None = blocking).
    """
    if socket_path:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(socket_path)
    else:
        sock = socket.create_connection((host, port))
    if timeout is not None:
        sock.settimeout(timeout)
    return Connection(sock)
