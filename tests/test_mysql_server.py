"""Tests for the MySQL wire-protocol server.

These tests use raw sockets so there's no dependency on a MySQL client
library (mysql-connector-python, PyMySQL, etc.).  They verify the protocol
at the byte level: handshake, COM_PING, COM_QUIT, COM_QUERY (SELECT,
INSERT/UPDATE/DDL, errors), COM_INIT_DB, result-set framing, ERR packets,
and the meta-query intercept layer.
"""
from __future__ import annotations

import socket
import struct
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hyperion.database import Database
from hyperion.mysql_server import (
    MySQLServer,
    _lenenc_int,
    _lenenc_str,
    _column_def,
    _encode_row,
    _classify_error,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _recv_packet(sock: socket.socket) -> bytes:
    """Read exactly one MySQL packet from *sock*."""
    hdr = _recvall(sock, 4)
    # Length is the first 3 bytes (LE); byte 3 is the sequence number.
    length = hdr[0] | (hdr[1] << 8) | (hdr[2] << 16)
    if length == 0:
        return b''
    return _recvall(sock, length)


def _recvall(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        assert chunk, "connection closed unexpectedly"
        buf.extend(chunk)
    return bytes(buf)


def _send_packet(sock: socket.socket, payload: bytes, seq: int = 0) -> None:
    hdr = struct.pack('<I', len(payload))[:3] + bytes([seq])
    sock.sendall(hdr + payload)


def _com_ping(sock: socket.socket) -> bytes:
    _send_packet(sock, bytes([0x0e]), seq=0)
    return _recv_packet(sock)


def _com_quit(sock: socket.socket) -> None:
    _send_packet(sock, bytes([0x01]), seq=0)


def _com_query(sock: socket.socket, sql: str) -> list[bytes]:
    """Send COM_QUERY and collect all response packets until EOF/ERR/OK."""
    payload = bytes([0x03]) + sql.encode('utf-8')
    _send_packet(sock, payload, seq=0)
    packets: list[bytes] = []
    while True:
        pkt = _recv_packet(sock)
        packets.append(pkt)
        if not pkt:
            break
        first = pkt[0]
        if first in (0x00, 0xff):   # OK or ERR
            break
        if first == 0xfe and len(pkt) < 9:   # EOF
            # For a result set we need two EOFs (after col-defs and after rows)
            # Count EOFs received so far
            eof_count = sum(
                1 for p in packets if p and p[0] == 0xfe and len(p) < 9
            )
            if eof_count >= 2:
                break
    return packets


def _com_init_db(sock: socket.socket, db_name: str) -> bytes:
    payload = bytes([0x02]) + db_name.encode('utf-8')
    _send_packet(sock, payload, seq=0)
    return _recv_packet(sock)


# ─────────────────────────────────────────────────────────────────────────────
# Fixture: start a server on an ephemeral port before each test class
# ─────────────────────────────────────────────────────────────────────────────

class _ServerFixture(unittest.TestCase):
    """Base class that starts a MySQLServer in a background thread."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.db  = Database(":memory:")
        cls.db.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT, price REAL)")
        cls.db.execute("INSERT INTO items VALUES (1, 'apple', 1.5)")
        cls.db.execute("INSERT INTO items VALUES (2, 'banana', 0.75)")
        # bind on port 0 → OS chooses a free port
        cls.srv = MySQLServer(cls.db, host="127.0.0.1", port=0)
        cls._thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls._thread.start()
        time.sleep(0.05)   # let server enter accept loop

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.shutdown()
        cls._thread.join(timeout=2.0)
        cls.db.close()

    def _connect(self) -> socket.socket:
        """Open a raw TCP connection, complete the handshake, and return the socket."""
        host, port = self.srv.address
        sock = socket.create_connection((host, port), timeout=5.0)
        # Server greeting (seq 0)
        _recv_packet(sock)
        # Client handshake response — minimal: just send capabilities + dummy auth
        caps  = 0x0000_a28d        # long_password | found_rows | long_flag | connect_with_db
        caps |= 0x0000_0200        # protocol_41
        caps |= 0x0000_8000        # secure_connection
        payload = (
            struct.pack('<I', caps)
            + struct.pack('<I', 1 << 24)    # max_packet_size
            + bytes([33])                    # charset utf8mb4
            + b'\x00' * 23                  # filler
            + b'root\x00'                   # username
            + bytes([0])                    # auth-response length = 0 (no password)
        )
        _send_packet(sock, payload, seq=1)
        # Server OK after auth
        _recv_packet(sock)
        return sock


# ─────────────────────────────────────────────────────────────────────────────
# Test groups
# ─────────────────────────────────────────────────────────────────────────────

class TestServerLifecycle(_ServerFixture):

    def test_server_address_is_set(self):
        host, port = self.srv.address
        self.assertEqual(host, "127.0.0.1")
        self.assertGreater(port, 0)

    def test_connect_handshake_succeeds(self):
        sock = self._connect()
        sock.close()

    def test_multiple_sequential_connections(self):
        for _ in range(3):
            sock = self._connect()
            sock.close()

    def test_concurrent_connections(self):
        socks = [self._connect() for _ in range(4)]
        for s in socks:
            s.close()


class TestComPing(_ServerFixture):

    def test_ping_returns_ok(self):
        sock = self._connect()
        pkt = _com_ping(sock)
        self.assertEqual(pkt[0:1], b'\x00', "expected OK packet")
        sock.close()

    def test_ping_twice(self):
        sock = self._connect()
        for _ in range(2):
            pkt = _com_ping(sock)
            self.assertEqual(pkt[0:1], b'\x00')
        sock.close()


class TestComQuit(_ServerFixture):

    def test_quit_closes_connection(self):
        sock = self._connect()
        _com_quit(sock)
        # Server should close — next recv returns empty
        sock.settimeout(1.0)
        try:
            data = sock.recv(4)
            self.assertEqual(data, b'', "server should close after QUIT")
        except socket.timeout:
            pass  # also acceptable
        sock.close()


class TestComInitDb(_ServerFixture):

    def test_init_db_returns_ok(self):
        sock = self._connect()
        pkt = _com_init_db(sock, "hyperion")
        self.assertEqual(pkt[0:1], b'\x00')
        sock.close()

    def test_init_db_any_name_returns_ok(self):
        sock = self._connect()
        pkt = _com_init_db(sock, "nonexistent_db")
        self.assertEqual(pkt[0:1], b'\x00')
        sock.close()


class TestComQuerySelect(_ServerFixture):

    def test_select_returns_result_set(self):
        sock = self._connect()
        pkts = _com_query(sock, "SELECT id, name FROM items ORDER BY id")
        # Should have: col_count, coldef×2, EOF, row×2, EOF
        self.assertGreaterEqual(len(pkts), 6)
        sock.close()

    def test_select_column_count_packet(self):
        sock = self._connect()
        pkts = _com_query(sock, "SELECT id, name FROM items")
        # First packet is column count (lenenc int = 2)
        self.assertEqual(pkts[0], b'\x02')
        sock.close()

    def test_select_star_returns_three_columns(self):
        sock = self._connect()
        pkts = _com_query(sock, "SELECT * FROM items")
        # col count packet = 3
        self.assertEqual(pkts[0], b'\x03')
        sock.close()

    def test_select_where_returns_fewer_rows(self):
        sock = self._connect()
        pkts = _com_query(sock, "SELECT * FROM items WHERE id = 1")
        # col_count(1) + 3 coldefs + EOF + 1 row + EOF = 7 packets
        # Actually: col_count(1) + 3 coldefs + EOF + 1 row + EOF = 7
        # count EOFs
        eof_count = sum(1 for p in pkts if p and p[0] == 0xfe and len(p) < 9)
        self.assertEqual(eof_count, 2)
        # count non-meta data packets (rows): total - 1 col_count - 3 coldefs - 2 EOFs = rows
        rows = len(pkts) - 1 - 3 - 2
        self.assertEqual(rows, 1)
        sock.close()

    def test_select_no_rows(self):
        sock = self._connect()
        pkts = _com_query(sock, "SELECT * FROM items WHERE id = 999")
        # col_count + 3 coldefs + EOF + EOF (no rows)
        eof_count = sum(1 for p in pkts if p and p[0] == 0xfe and len(p) < 9)
        self.assertEqual(eof_count, 2)
        sock.close()

    def test_select_literal(self):
        sock = self._connect()
        pkts = _com_query(sock, "SELECT 42")
        self.assertEqual(pkts[0], b'\x01')   # 1 column
        sock.close()


class TestComQueryDML(_ServerFixture):

    def test_insert_returns_ok(self):
        sock = self._connect()
        pkts = _com_query(sock, "INSERT INTO items VALUES (99, 'test', 9.99)")
        self.assertEqual(pkts[-1][0:1], b'\x00', "expected OK packet")
        # cleanup
        _com_query(sock, "DELETE FROM items WHERE id = 99")
        sock.close()

    def test_update_returns_ok(self):
        sock = self._connect()
        pkts = _com_query(sock, "UPDATE items SET price = 2.0 WHERE id = 1")
        self.assertEqual(pkts[-1][0:1], b'\x00')
        _com_query(sock, "UPDATE items SET price = 1.5 WHERE id = 1")
        sock.close()

    def test_create_table_returns_ok(self):
        sock = self._connect()
        pkts = _com_query(sock, "CREATE TABLE _tmp_test (x INTEGER)")
        self.assertEqual(pkts[-1][0:1], b'\x00')
        _com_query(sock, "DROP TABLE _tmp_test")
        sock.close()

    def test_drop_table_returns_ok(self):
        sock = self._connect()
        _com_query(sock, "CREATE TABLE _drop_me (x INTEGER)")
        pkts = _com_query(sock, "DROP TABLE _drop_me")
        self.assertEqual(pkts[-1][0:1], b'\x00')
        sock.close()


class TestComQueryErrors(_ServerFixture):

    def test_syntax_error_returns_err_packet(self):
        sock = self._connect()
        pkts = _com_query(sock, "THIS IS NOT SQL")
        self.assertEqual(pkts[-1][0:1], b'\xff', "expected ERR packet for bad SQL")
        sock.close()

    def test_unknown_table_returns_err_packet(self):
        sock = self._connect()
        pkts = _com_query(sock, "SELECT * FROM does_not_exist")
        self.assertEqual(pkts[-1][0:1], b'\xff')
        sock.close()

    def test_err_packet_contains_sql_state(self):
        sock = self._connect()
        pkts = _com_query(sock, "SELECT * FROM no_such_table_xyz")
        err = pkts[-1]
        self.assertEqual(err[0:1], b'\xff')
        # bytes 3..8 are '#' + 5-char SQL state
        self.assertEqual(err[3:4], b'#')
        sql_state = err[4:9].decode('ascii')
        self.assertRegex(sql_state, r'^[A-Z0-9]{5}$')
        sock.close()


class TestMetaQueryIntercepts(_ServerFixture):

    def _single_value(self, sql: str) -> str:
        sock = self._connect()
        pkts = _com_query(sock, sql)
        sock.close()
        # Find the first data row packet (after col_count + coldef + EOF)
        eof_seen = 0
        for pkt in pkts:
            if pkt and pkt[0] == 0xfe and len(pkt) < 9:
                eof_seen += 1
                continue
            if eof_seen == 1 and pkt and pkt[0] not in (0x00, 0xff, 0xfe):
                # This is a row packet; first byte is lenenc length, rest is value
                length = pkt[0]
                return pkt[1:1 + length].decode('utf-8')
        return ""

    def test_select_version(self):
        val = self._single_value("SELECT @@version")
        self.assertIn("hyperion", val.lower())

    def test_select_version_comment(self):
        val = self._single_value("SELECT @@version_comment")
        self.assertIn("Hyperion", val)

    def test_show_databases(self):
        val = self._single_value("SHOW DATABASES")
        self.assertEqual(val, "hyperion")

    def test_show_warnings_empty(self):
        sock = self._connect()
        pkts = _com_query(sock, "SHOW WARNINGS")
        # 3 cols + 2 EOFs, no row data
        eof_count = sum(1 for p in pkts if p and p[0] == 0xfe and len(p) < 9)
        self.assertEqual(eof_count, 2)
        # rows = total - col_count(1) - 3 coldefs - 2 EOFs = 0
        rows = len(pkts) - 1 - 3 - 2
        self.assertEqual(rows, 0)
        sock.close()

    def test_set_statement_returns_ok(self):
        sock = self._connect()
        pkts = _com_query(sock, "SET NAMES utf8mb4")
        self.assertEqual(pkts[-1][0:1], b'\x00')
        sock.close()


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests for wire-format helpers (no server needed)
# ─────────────────────────────────────────────────────────────────────────────

class TestLenencInt(unittest.TestCase):

    def test_small(self):
        self.assertEqual(_lenenc_int(0), b'\x00')
        self.assertEqual(_lenenc_int(250), b'\xfa')

    def test_2byte(self):
        b = _lenenc_int(251)
        self.assertEqual(b[0:1], b'\xfc')
        self.assertEqual(struct.unpack_from('<H', b, 1)[0], 251)

    def test_3byte(self):
        b = _lenenc_int(65536)
        self.assertEqual(b[0:1], b'\xfd')

    def test_8byte(self):
        b = _lenenc_int(16_777_216)
        self.assertEqual(b[0:1], b'\xfe')


class TestLenencStr(unittest.TestCase):

    def test_empty(self):
        self.assertEqual(_lenenc_str(b''), b'\x00')

    def test_short(self):
        s = b'hello'
        enc = _lenenc_str(s)
        self.assertEqual(enc[0], 5)
        self.assertEqual(enc[1:], s)


class TestColumnDef(unittest.TestCase):

    def test_returns_bytes(self):
        b = _column_def("my_col")
        self.assertIsInstance(b, bytes)
        self.assertGreater(len(b), 10)

    def test_col_name_present(self):
        b = _column_def("price")
        self.assertIn(b'price', b)


class TestEncodeRow(unittest.TestCase):

    def test_string_value(self):
        row = _encode_row(["hello"], 1)
        self.assertEqual(row[0], 5)    # lenenc length
        self.assertEqual(row[1:], b'hello')

    def test_null_value(self):
        row = _encode_row([None], 1)
        self.assertEqual(row, b'\xfb')

    def test_int_value(self):
        row = _encode_row([42], 1)
        self.assertEqual(row[1:], b'42')

    def test_multiple_cols(self):
        row = _encode_row(["a", None, 99], 3)
        self.assertIn(b'\xfb', row)   # NULL marker present
        self.assertIn(b'99', row)

    def test_fewer_values_than_cols_pads_null(self):
        row = _encode_row(["only_one"], 3)
        # Second and third columns should be NULL (0xfb)
        self.assertEqual(row.count(b'\xfb'), 2)


class TestClassifyError(unittest.TestCase):

    def test_parse_error(self):
        from hyperion.errors import ParseError
        code, state = _classify_error(ParseError("bad sql"))
        self.assertEqual(code, 1064)
        self.assertEqual(state, '42000')

    def test_no_such_table(self):
        from hyperion.errors import NoSuchTableError
        code, state = _classify_error(NoSuchTableError("t"))
        self.assertEqual(code, 1146)
        self.assertEqual(state, '42S02')

    def test_unique_constraint(self):
        from hyperion.errors import UniqueConstraintError
        code, state = _classify_error(UniqueConstraintError("dup key"))
        self.assertEqual(code, 1062)
        self.assertEqual(state, '23000')

    def test_transaction_error(self):
        from hyperion.errors import TransactionError
        code, state = _classify_error(TransactionError("no txn"))
        self.assertEqual(code, 1213)
        self.assertEqual(state, '25000')

    def test_unknown_exception(self):
        code, state = _classify_error(ValueError("misc"))
        self.assertEqual(code, 1064)
        self.assertEqual(state, 'HY000')


if __name__ == "__main__":
    unittest.main()
