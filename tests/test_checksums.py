"""Tests for per-page CRC-32 checksums."""
import struct
import tempfile
from pathlib import Path

import pytest
from hyperion import Database
from hyperion.checksum import (
    CorruptPageError, page_checksum, stamp_page, verify_page,
)
from hyperion.constants import PAGE_SIZE, PAGE_CKSUM_OFF, PAGE_CKSUM_SZ
from hyperion.introspect import integrity_check


# ── Unit tests for checksum helpers ──────────────────────────────────────────

def test_stamp_then_verify_passes():
    page = bytearray(PAGE_SIZE)
    page[0] = 0x42
    page[100] = 0xFF
    stamp_page(page)
    verify_page(page, 0)  # should not raise


def test_verify_zero_crc_is_skipped():
    """A page with a stored CRC of 0 is treated as legacy and never raises."""
    page = bytearray(PAGE_SIZE)  # all zeros → stored CRC = 0
    verify_page(page, 0)


def test_verify_wrong_crc_raises():
    page = bytearray(PAGE_SIZE)
    page[0] = 0x01
    stamp_page(page)
    # Corrupt one data byte after stamping
    page[10] ^= 0xFF
    with pytest.raises(CorruptPageError) as exc_info:
        verify_page(page, 5)
    assert exc_info.value.page_num == 5


def test_checksum_covers_only_data_area():
    """Two pages identical in [0:PAGE_CKSUM_OFF] must have the same checksum
    regardless of what is already in the last 4 bytes."""
    a = bytearray(PAGE_SIZE)
    b = bytearray(PAGE_SIZE)
    a[50] = 0xAB
    b[50] = 0xAB
    # Give them different tail bytes before stamping
    a[PAGE_CKSUM_OFF] = 0x11
    b[PAGE_CKSUM_OFF] = 0x22
    assert page_checksum(a) == page_checksum(b)


def test_stamp_is_idempotent():
    """Stamping a page twice produces the same result as stamping once."""
    page = bytearray(PAGE_SIZE)
    page[1] = 0x99
    stamp_page(page)
    crc1 = struct.unpack_from("<I", page, PAGE_CKSUM_OFF)[0]
    stamp_page(page)  # stamp again
    crc2 = struct.unpack_from("<I", page, PAGE_CKSUM_OFF)[0]
    assert crc1 == crc2


def test_corrupt_page_error_message():
    page = bytearray(PAGE_SIZE)
    stamp_page(page)
    page[0] = 0xFF  # corrupt after stamping
    with pytest.raises(CorruptPageError) as exc_info:
        verify_page(page, 99)
    msg = str(exc_info.value)
    assert "page 99" in msg
    assert "0x" in msg


# ── Integration: checksums written and verified on disk ──────────────────────

def test_pages_have_valid_crc_after_write(tmp_path):
    """After writing rows, every page on disk should have a valid CRC."""
    db_path = tmp_path / "db.hdb"
    db = Database(db_path)
    db.execute("CREATE TABLE t (id INTEGER, val TEXT)")
    for i in range(20):
        db.execute(f"INSERT INTO t VALUES ({i}, 'row{i}')")
    db.close()

    db2 = Database(db_path)
    assert integrity_check(db2) == ["ok"]
    db2.close()


def test_data_survives_close_reopen_with_checksums(tmp_path):
    db_path = tmp_path / "db.hdb"
    db = Database(db_path)
    db.execute("CREATE TABLE t (id INTEGER)")
    for i in range(10):
        db.execute(f"INSERT INTO t VALUES ({i})")
    db.close()

    db2 = Database(db_path)
    rows = db2.execute("SELECT id FROM t ORDER BY id").fetchall()
    db2.close()
    assert [r["id"] for r in rows] == list(range(10))


def test_corrupt_page_detected_on_read(tmp_path):
    """Directly corrupt a data byte in the db file; opening should raise."""
    db_path = tmp_path / "db.hdb"
    db = Database(db_path)
    db.execute("CREATE TABLE t (id INTEGER)")
    for i in range(5):
        db.execute(f"INSERT INTO t VALUES ({i})")
    db.close()

    # Flip a bit in the middle of the data file (skip first page = catalog)
    with open(db_path, "r+b") as f:
        f.seek(PAGE_SIZE + 20)  # second page, offset 20
        byte = f.read(1)[0]
        f.seek(PAGE_SIZE + 20)
        f.write(bytes([byte ^ 0xFF]))

    # Opening / reading the corrupted page must raise CorruptPageError
    with pytest.raises(CorruptPageError):
        db2 = Database(db_path)
        db2.execute("SELECT * FROM t").fetchall()


def test_integrity_check_reports_page_corruption(tmp_path):
    """integrity_check must report corrupt pages instead of returning 'ok'."""
    db_path = tmp_path / "db.hdb"
    db = Database(db_path)
    db.execute("CREATE TABLE t (id INTEGER)")
    for i in range(5):
        db.execute(f"INSERT INTO t VALUES ({i})")
    db.close()

    # Corrupt a byte in the second page (first B-tree page after catalog)
    with open(db_path, "r+b") as f:
        f.seek(PAGE_SIZE + 20)
        byte = f.read(1)[0]
        f.seek(PAGE_SIZE + 20)
        f.write(bytes([byte ^ 0xFF]))

    # May raise CorruptPageError on load, or return errors from integrity_check
    try:
        db2 = Database(db_path)
        result = integrity_check(db2)
        assert result != ["ok"], f"Expected corruption to be detected, got: {result}"
    except CorruptPageError:
        pass  # raised during page load — also valid detection


def test_many_rows_all_pages_intact(tmp_path):
    """Write enough rows to create multiple B-tree pages; all CRCs must be valid."""
    db_path = tmp_path / "db.hdb"
    db = Database(db_path)
    db.execute("CREATE TABLE t (id INTEGER, data TEXT)")
    for i in range(500):
        db.execute(f"INSERT INTO t VALUES ({i}, 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx')")
    db.close()

    db2 = Database(db_path)
    assert integrity_check(db2) == ["ok"]
    db2.close()


def test_index_pages_have_valid_crc(tmp_path):
    """Index B-tree pages must also carry valid CRCs."""
    db_path = tmp_path / "db.hdb"
    db = Database(db_path)
    db.execute("CREATE TABLE t (id INTEGER, val INTEGER)")
    db.execute("CREATE INDEX idx_val ON t(val)")
    for i in range(200):
        db.execute(f"INSERT INTO t VALUES ({i}, {i * 2})")
    db.close()

    db2 = Database(db_path)
    assert integrity_check(db2) == ["ok"]
    db2.close()
