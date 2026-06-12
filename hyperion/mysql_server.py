"""MySQL wire protocol server for Hyperion.

Implements enough of the MySQL client/server protocol so that any
MySQL-compatible client can connect without a Hyperion-specific driver.

Supported commands
------------------
COM_QUERY     — execute any SQL string; returns result sets or OK/ERR
COM_PING      — keepalive; responds with OK
COM_QUIT      — close connection cleanly
COM_INIT_DB   — handle ``USE database``; acknowledged (single-file db)
COM_FIELD_LIST — respond with empty EOF (legacy clients)
COM_STATISTICS — return a plain-text stats string

Auth
----
No real authentication is performed.  Any username/password is accepted.
Use ``mysql --skip-ssl`` or ``--ssl-mode=DISABLED``.

MySQL meta-queries
------------------
A small set of MySQL-specific ``@@variable`` and ``SHOW`` queries are
intercepted and answered without forwarding to the SQL engine, so the
``mysql`` CLI prompt works out of the box.

Usage
-----
CLI::

    python -m hyperion mysql mydb.hdb --port 4406

Programmatic::

    from hyperion.database import Database
    from hyperion.mysql_server import MySQLServer

    db  = Database("mydb.hdb")
    srv = MySQLServer(db, host="127.0.0.1", port=4406)
    srv.serve_forever()        # blocks; call srv.shutdown() from another thread
"""
from __future__ import annotations

import os
import re
import socket
import struct
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .database import Database

# ── Capability flags ──────────────────────────────────────────────────────────

_CLIENT_LONG_PASSWORD     = 0x00000001
_CLIENT_FOUND_ROWS        = 0x00000002
_CLIENT_LONG_FLAG         = 0x00000004
_CLIENT_CONNECT_WITH_DB   = 0x00000008
_CLIENT_PROTOCOL_41       = 0x00000200
_CLIENT_TRANSACTIONS      = 0x00002000
_CLIENT_SECURE_CONNECTION = 0x00008000
_CLIENT_PLUGIN_AUTH       = 0x00080000

_SERVER_CAPS = (
    _CLIENT_LONG_PASSWORD | _CLIENT_FOUND_ROWS | _CLIENT_LONG_FLAG |
    _CLIENT_CONNECT_WITH_DB | _CLIENT_PROTOCOL_41 | _CLIENT_TRANSACTIONS |
    _CLIENT_SECURE_CONNECTION | _CLIENT_PLUGIN_AUTH
)

# ── Command bytes ─────────────────────────────────────────────────────────────

_COM_QUIT       = 0x01
_COM_INIT_DB    = 0x02
_COM_QUERY      = 0x03
_COM_FIELD_LIST = 0x04
_COM_STATISTICS = 0x09
_COM_PING       = 0x0e

# ── Column type codes (text protocol) ────────────────────────────────────────

_TYPE_DOUBLE     = 0x05
_TYPE_LONGLONG   = 0x08
_TYPE_VAR_STRING = 0xfd
_TYPE_BLOB       = 0xfc

# ── Status flags ──────────────────────────────────────────────────────────────

_STATUS_AUTOCOMMIT = 0x0002

# ── MySQL meta-query intercepts ───────────────────────────────────────────────
# Queries the mysql CLI sends automatically that Hyperion can't execute.
# Each entry: (regex, col_names, rows) — if rows is None → send OK (no result).

_INTERCEPTS: list[tuple[re.Pattern, list[str] | None, list[list[str]] | None]] = [
    (re.compile(r"SELECT\s+@@version_comment\b.*", re.I | re.S),
     ["@@version_comment"], [["Hyperion"]]),
    (re.compile(r"SELECT\s+@@version\b.*", re.I | re.S),
     ["@@version"], [["8.0.0-hyperion"]]),
    (re.compile(r"SELECT\s+@@max_allowed_packet\b.*", re.I | re.S),
     ["@@max_allowed_packet"], [["67108864"]]),
    (re.compile(r"SELECT\s+@@global\.\w+.*", re.I | re.S),
     ["@@global"], [[""]]),
    (re.compile(r"SELECT\s+@@session\.\w+.*", re.I | re.S),
     ["@@session"], [[""]]),
    (re.compile(r"SELECT\s+@@\w+.*", re.I | re.S),
     ["@@var"], [[""]]),
    (re.compile(r"SHOW\s+VARIABLES\b.*", re.I | re.S),
     ["Variable_name", "Value"],
     [["version", "8.0.0-hyperion"], ["version_comment", "Hyperion"]]),
    (re.compile(r"SHOW\s+DATABASES\b.*", re.I | re.S),
     ["Database"], [["hyperion"]]),
    (re.compile(r"SHOW\s+WARNINGS\b.*", re.I | re.S),
     ["Level", "Code", "Message"], []),
    (re.compile(r"SELECT\s+DATABASE\(\).*", re.I | re.S),
     ["DATABASE()"], [["hyperion"]]),
    (re.compile(r"SELECT\s+CONNECTION_ID\(\).*", re.I | re.S),
     ["CONNECTION_ID()"], [["1"]]),
    # SET statements → OK (no result set)
    (re.compile(r"SET\s+", re.I), None, None),
    (re.compile(r"ROLLBACK\s+TO\b.*", re.I | re.S), None, None),  # handled by engine too
]

# ── Length-encoded helpers ────────────────────────────────────────────────────

def _lenenc_int(n: int) -> bytes:
    if n < 251:
        return bytes([n])
    if n < 65_536:
        return b'\xfc' + struct.pack('<H', n)
    if n < 16_777_216:
        return b'\xfd' + struct.pack('<I', n)[:3]
    return b'\xfe' + struct.pack('<Q', n)


def _lenenc_str(s: bytes) -> bytes:
    return _lenenc_int(len(s)) + s


# ── Column-def cache (module-level; col names are stable across queries) ──────

_col_def_cache: dict[str, bytes] = {}


def _col_def_cached(name: str) -> bytes:
    cached = _col_def_cache.get(name)
    if cached is None:
        cached = _column_def(name)
        _col_def_cache[name] = cached
    return cached


# ── Per-connection handler ────────────────────────────────────────────────────

class _Connection:
    """Handle one MySQL client connection."""

    # Pre-built constant packets (no sequence byte — added by _write_packet)
    _OK_PAYLOAD  = b'\x00\x00\x00' + struct.pack('<H', _STATUS_AUTOCOMMIT) + b'\x00\x00'
    _EOF_PAYLOAD = b'\xfe\x00\x00' + struct.pack('<H', _STATUS_AUTOCOMMIT)

    __slots__ = ('_sock', '_db', '_conn_id', '_seq', '_wbuf', '_rbuf', '_rpos')

    def __init__(self, sock: socket.socket, db: "Database", conn_id: int) -> None:
        self._sock    = sock
        self._db      = db
        self._conn_id = conn_id
        self._seq     = 0
        self._wbuf    = bytearray()   # outgoing packet accumulator
        self._rbuf    = bytearray()   # incoming read buffer
        self._rpos    = 0             # read cursor into _rbuf

    # ── Public entry point ────────────────────────────────────────────────────

    def handle(self) -> None:
        try:
            self._handshake()
            self._command_loop()
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        finally:
            try:
                self._sock.close()
            except OSError:
                pass

    # ── Handshake ─────────────────────────────────────────────────────────────

    def _handshake(self) -> None:
        self._seq = 0
        auth_data = os.urandom(20)
        payload = (
            b'\x0a'                                          # protocol v10
            + b'8.0.0-hyperion\x00'                         # server version
            + struct.pack('<I', self._conn_id)               # connection id
            + auth_data[:8] + b'\x00'                       # auth part 1 + filler
            + struct.pack('<H', _SERVER_CAPS & 0xFFFF)      # capability flags (lo)
            + b'\x21'                                        # charset: utf8mb4
            + struct.pack('<H', _STATUS_AUTOCOMMIT)          # status flags
            + struct.pack('<H', (_SERVER_CAPS >> 16) & 0xFFFF)  # capability flags (hi)
            + bytes([21])                                    # auth-plugin-data length
            + b'\x00' * 10                                  # reserved
            + auth_data[8:] + b'\x00\x00'                  # auth part 2 (13 bytes)
            + b'mysql_native_password\x00'                  # auth plugin name
        )
        self._write_packet(payload)
        self._flush()
        # Read and discard the client handshake response
        self._recv_packet()
        self._seq = 0
        self._write_packet(self._OK_PAYLOAD)
        self._flush()

    # ── Command loop ──────────────────────────────────────────────────────────

    def _command_loop(self) -> None:
        while True:
            pkt = self._recv_packet()
            if pkt is None or len(pkt) == 0:
                break
            cmd  = pkt[0]
            body = pkt[1:]
            self._seq = 0

            if cmd == _COM_QUIT:
                break
            elif cmd == _COM_PING:
                self._write_packet(self._OK_PAYLOAD)
            elif cmd == _COM_INIT_DB:
                self._write_packet(self._OK_PAYLOAD)
            elif cmd == _COM_QUERY:
                sql = body.decode('utf-8', errors='replace').strip().rstrip(';')
                self._handle_query(sql)
            elif cmd == _COM_FIELD_LIST:
                self._write_packet(self._EOF_PAYLOAD)
            elif cmd == _COM_STATISTICS:
                self._write_packet(b'Hyperion; uptime: 0s; queries: 0')
            else:
                self._write_err(1047, f'Unknown command 0x{cmd:02x}')
            self._flush()

    # ── Query handling ────────────────────────────────────────────────────────

    def _handle_query(self, sql: str) -> None:
        for pattern, cols, rows in _INTERCEPTS:
            if pattern.match(sql):
                if cols is None:
                    self._write_packet(self._OK_PAYLOAD)
                else:
                    self._write_resultset(cols, rows or [])
                return

        try:
            cur = self._db.execute(sql)
        except Exception as exc:
            code, state = _classify_error(exc)
            self._write_err(code, str(exc), state)
            return

        if cur.description is None:
            self._write_ok(
                affected_rows=max(cur.rowcount, 0),
                last_insert_id=cur.lastrowid or 0,
            )
        else:
            col_names = [d[0] for d in cur.description]
            raw_rows  = cur.fetchall()
            self._write_resultset_raw(col_names, raw_rows)

    # ── Result-set framing ────────────────────────────────────────────────────

    def _write_resultset(self, cols: list[str], rows: list[list[Any]]) -> None:
        """Write a pre-built result set (intercept responses) into the buffer."""
        ncols = len(cols)
        self._write_packet(_lenenc_int(ncols))
        for name in cols:
            self._write_packet(_col_def_cached(name))
        self._write_packet(self._EOF_PAYLOAD)
        for row in rows:
            self._write_row_packet(row, ncols)
        self._write_packet(self._EOF_PAYLOAD)

    def _write_resultset_raw(self, col_names: list[str],
                             raw_rows: list[Any]) -> None:
        """Write a live result set from the database engine into the buffer."""
        ncols = len(col_names)
        self._write_packet(_lenenc_int(ncols))
        for name in col_names:
            self._write_packet(_col_def_cached(name))
        self._write_packet(self._EOF_PAYLOAD)
        if raw_rows:
            if isinstance(raw_rows[0], dict):
                for raw in raw_rows:
                    self._write_row_dict(raw, col_names)
            else:
                for raw in raw_rows:
                    self._write_row_packet(list(raw), ncols)
        self._write_packet(self._EOF_PAYLOAD)

    # ── Packet primitives — write into _wbuf ─────────────────────────────────

    def _write_ok(self, affected_rows: int = 0, last_insert_id: int = 0) -> None:
        payload = (
            b'\x00'
            + _lenenc_int(affected_rows)
            + _lenenc_int(last_insert_id)
            + struct.pack('<H', _STATUS_AUTOCOMMIT)
            + b'\x00\x00'
        )
        self._write_packet(payload)

    def _write_err(self, code: int = 1064, message: str = "Error",
                   sql_state: str = "HY000") -> None:
        payload = (
            b'\xff'
            + struct.pack('<H', code)
            + b'#' + sql_state.encode('ascii')
            + message.encode('utf-8', errors='replace')
        )
        self._write_packet(payload)

    def _write_row_packet(self, values: list[Any], ncols: int) -> None:
        """Encode one text-protocol row and append as a framed packet."""
        buf = self._wbuf
        hdr_pos = len(buf)
        buf += b'\x00\x00\x00'
        buf.append(self._seq)
        self._seq = (self._seq + 1) % 256
        for i in range(ncols):
            val = values[i] if i < len(values) else None
            _append_val(buf, val)
        payload_len = len(buf) - hdr_pos - 4
        buf[hdr_pos]     = payload_len & 0xff
        buf[hdr_pos + 1] = (payload_len >> 8) & 0xff
        buf[hdr_pos + 2] = (payload_len >> 16) & 0xff

    def _write_row_dict(self, row: dict, col_names: list[str]) -> None:
        """Encode a dict row directly without an intermediate list."""
        buf = self._wbuf
        hdr_pos = len(buf)
        buf += b'\x00\x00\x00'
        buf.append(self._seq)
        self._seq = (self._seq + 1) % 256
        for name in col_names:
            _append_val(buf, row.get(name))
        payload_len = len(buf) - hdr_pos - 4
        buf[hdr_pos]     = payload_len & 0xff
        buf[hdr_pos + 1] = (payload_len >> 8) & 0xff
        buf[hdr_pos + 2] = (payload_len >> 16) & 0xff

    def _write_packet(self, payload: bytes) -> None:
        """Append a framed MySQL packet to the write buffer."""
        n = len(payload)
        buf = self._wbuf
        buf.append(n & 0xff)
        buf.append((n >> 8) & 0xff)
        buf.append((n >> 16) & 0xff)
        buf.append(self._seq)
        buf += payload
        self._seq = (self._seq + 1) % 256

    def _flush(self) -> None:
        """Send the entire write buffer in one syscall and reset it."""
        if self._wbuf:
            self._sock.sendall(self._wbuf)
            self._wbuf = bytearray()

    # ── Receive path ──────────────────────────────────────────────────────────

    def _recv_packet(self) -> bytes | None:
        hdr = self._read_bytes(4)
        if hdr is None:
            return None
        length = hdr[0] | (hdr[1] << 8) | (hdr[2] << 16)
        self._seq = (hdr[3] + 1) % 256
        if length == 0:
            return b''
        return self._read_bytes(length)

    def _read_bytes(self, n: int) -> bytes | None:
        """Return exactly *n* bytes, pulling from the socket into _rbuf as needed."""
        while self._rpos + n > len(self._rbuf):
            chunk = self._sock.recv(65536)
            if not chunk:
                return None
            self._rbuf += chunk
        result = bytes(self._rbuf[self._rpos:self._rpos + n])
        self._rpos += n
        # Compact the buffer once the read cursor is far ahead
        if self._rpos > 65536:
            self._rbuf = self._rbuf[self._rpos:]
            self._rpos = 0
        return result


# ── Wire-format helpers ───────────────────────────────────────────────────────

def _append_val(buf: bytearray, val: Any) -> None:
    """Append one text-protocol field value (or NULL marker) to *buf* in-place."""
    if val is None:
        buf.append(0xfb)
        return
    s = str(val).encode('utf-8')
    n = len(s)
    if n < 251:
        buf.append(n)
    elif n < 65_536:
        buf.append(0xfc)
        buf += struct.pack('<H', n)
    elif n < 16_777_216:
        buf.append(0xfd)
        buf += struct.pack('<I', n)[:3]
    else:
        buf.append(0xfe)
        buf += struct.pack('<Q', n)
    buf += s


def _column_def(name: str) -> bytes:
    nb = name.encode('utf-8')
    return (
        _lenenc_str(b'def')           # catalog
        + _lenenc_str(b'')            # schema
        + _lenenc_str(b'')            # table
        + _lenenc_str(b'')            # org_table
        + _lenenc_str(nb)             # name
        + _lenenc_str(nb)             # org_name
        + b'\x0c'                     # fixed fields length = 12
        + b'\x21\x00'                 # charset: utf8mb4_general_ci
        + struct.pack('<I', 1024)     # column display length
        + bytes([_TYPE_VAR_STRING])   # type
        + b'\x00\x00'                 # flags
        + b'\x00'                     # decimals
        + b'\x00\x00'                 # filler
    )


def _encode_row(row: list[Any], ncols: int) -> bytes:
    out = bytearray()
    for i in range(ncols):
        val = row[i] if i < len(row) else None
        if val is None:
            out += b'\xfb'          # NULL
        else:
            s = str(val).encode('utf-8')
            out += _lenenc_str(s)
    return bytes(out)


def _classify_error(exc: Exception) -> tuple[int, str]:
    """Map a Hyperion exception to a (MySQL error code, SQL state) pair."""
    try:
        from .errors import (ParseError, NoSuchTableError,
                             UniqueConstraintError, ForeignKeyConstraintError,
                             NotNullConstraintError, TransactionError,
                             CheckConstraintError)
    except ImportError:
        return 1064, 'HY000'

    if isinstance(exc, ParseError):
        return 1064, '42000'
    if isinstance(exc, NoSuchTableError):
        return 1146, '42S02'
    if isinstance(exc, (UniqueConstraintError, ForeignKeyConstraintError,
                        NotNullConstraintError, CheckConstraintError)):
        return 1062, '23000'
    if isinstance(exc, TransactionError):
        return 1213, '25000'
    return 1064, 'HY000'


# ── Server ────────────────────────────────────────────────────────────────────

class MySQLServer:
    """TCP server that speaks the MySQL wire protocol.

    Example::

        db  = Database("mydb.hdb")
        srv = MySQLServer(db, host="127.0.0.1", port=4406)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            srv.shutdown()
            db.close()
    """

    def __init__(self, db: "Database",
                 host: str = "127.0.0.1",
                 port: int = 4406) -> None:
        self._db      = db
        self._sock    = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.listen(64)
        self._running  = False
        self._conn_id  = 0
        self._id_lock  = threading.Lock()

    @property
    def address(self) -> tuple[str, int]:
        return self._sock.getsockname()

    def serve_forever(self) -> None:
        self._running = True
        self._sock.settimeout(1.0)
        while self._running:
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            with self._id_lock:
                self._conn_id += 1
                cid = self._conn_id
            handler = _Connection(conn, self._db, cid)
            t = threading.Thread(target=handler.handle, daemon=True)
            t.start()

    def shutdown(self) -> None:
        self._running = False
        try:
            self._sock.close()
        except OSError:
            pass
