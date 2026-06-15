"""Tests for REGEXP_REPLACE, REGEXP_EXTRACT/REGEXP_SUBSTR, REGEXP, RLIKE operators."""
import pytest
from hyperion import Database


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "test.hdb"))
    d.execute("CREATE TABLE t (id INTEGER, name TEXT, val TEXT)")
    d.execute("INSERT INTO t VALUES (1, 'hello world', 'abc123')")
    d.execute("INSERT INTO t VALUES (2, 'foo bar', 'xyz456')")
    d.execute("INSERT INTO t VALUES (3, 'HELLO WORLD', 'ABC789')")
    d.execute("INSERT INTO t VALUES (4, 'test123', NULL)")
    yield d
    d.close()


def _fetch(cursor):
    return list(cursor.fetchall())


# --- REGEXP_REPLACE ---

def test_regexp_replace_basic(db):
    rows = _fetch(db.execute("SELECT REGEXP_REPLACE(name, 'o+', '0') AS r FROM t WHERE id = 1"))
    assert rows[0]["r"] == "hell0 w0rld"


def test_regexp_replace_digits(db):
    rows = _fetch(db.execute("SELECT REGEXP_REPLACE(val, '[0-9]+', 'NUM') AS r FROM t WHERE id = 1"))
    assert rows[0]["r"] == "abcNUM"


def test_regexp_replace_no_match(db):
    rows = _fetch(db.execute("SELECT REGEXP_REPLACE(name, 'zzz', 'X') AS r FROM t WHERE id = 1"))
    assert rows[0]["r"] == "hello world"


def test_regexp_replace_null_input(db):
    rows = _fetch(db.execute("SELECT REGEXP_REPLACE(val, '[0-9]', 'X') AS r FROM t WHERE id = 4"))
    assert rows[0]["r"] is None


def test_regexp_replace_invalid_pattern(db):
    rows = _fetch(db.execute("SELECT REGEXP_REPLACE(name, '[invalid', 'X') AS r FROM t WHERE id = 1"))
    assert rows[0]["r"] is None


# --- REGEXP_EXTRACT / REGEXP_SUBSTR ---

def test_regexp_extract_basic(db):
    rows = _fetch(db.execute("SELECT REGEXP_EXTRACT(val, '[0-9]+') AS r FROM t WHERE id = 1"))
    assert rows[0]["r"] == "123"


def test_regexp_extract_group(db):
    rows = _fetch(db.execute("SELECT REGEXP_EXTRACT(val, '([a-z]+)') AS r FROM t WHERE id = 1"))
    assert rows[0]["r"] == "abc"


def test_regexp_extract_no_match(db):
    rows = _fetch(db.execute("SELECT REGEXP_EXTRACT(name, '[0-9]+') AS r FROM t WHERE id = 1"))
    assert rows[0]["r"] is None


def test_regexp_extract_null(db):
    rows = _fetch(db.execute("SELECT REGEXP_EXTRACT(val, '[0-9]+') AS r FROM t WHERE id = 4"))
    assert rows[0]["r"] is None


def test_regexp_substr_alias(db):
    rows = _fetch(db.execute("SELECT REGEXP_SUBSTR(val, '[a-z]+') AS r FROM t WHERE id = 2"))
    assert rows[0]["r"] == "xyz"


def test_regexp_extract_invalid_pattern(db):
    rows = _fetch(db.execute("SELECT REGEXP_EXTRACT(name, '(bad[') AS r FROM t WHERE id = 1"))
    assert rows[0]["r"] is None


# --- REGEXP / RLIKE infix operators ---

def test_regexp_operator_filter(db):
    rows = _fetch(db.execute("SELECT id FROM t WHERE name REGEXP 'hello'"))
    assert [r["id"] for r in rows] == [1]


def test_rlike_operator_filter(db):
    rows = _fetch(db.execute("SELECT id FROM t WHERE name RLIKE '^foo'"))
    assert [r["id"] for r in rows] == [2]


def test_regexp_case_sensitive(db):
    rows = _fetch(db.execute("SELECT id FROM t WHERE name REGEXP 'HELLO'"))
    assert [r["id"] for r in rows] == [3]


def test_regexp_digit_pattern(db):
    rows = _fetch(db.execute("SELECT id FROM t WHERE name REGEXP '[0-9]'"))
    assert [r["id"] for r in rows] == [4]


def test_regexp_anchored(db):
    rows = _fetch(db.execute("SELECT id FROM t WHERE val REGEXP '^[A-Z]'"))
    assert [r["id"] for r in rows] == [3]


def test_regexp_no_match(db):
    rows = _fetch(db.execute("SELECT id FROM t WHERE name REGEXP 'zzz'"))
    assert rows == []


def test_regexp_all_rows(db):
    rows = _fetch(db.execute("SELECT id FROM t WHERE name REGEXP '.+'"))
    assert len(rows) == 4


# --- NOT REGEXP / NOT RLIKE ---

def test_not_regexp_basic(db):
    rows = _fetch(db.execute("SELECT id FROM t WHERE val NOT REGEXP '^[a-z]'"))
    ids = [r["id"] for r in rows]
    assert 3 in ids
    assert 1 not in ids
    assert 2 not in ids


def test_not_rlike(db):
    rows = _fetch(db.execute("SELECT id FROM t WHERE name NOT RLIKE '^hello' AND id < 3"))
    assert [r["id"] for r in rows] == [2]


def test_not_regexp_anchored(db):
    rows = _fetch(db.execute("SELECT id FROM t WHERE name NOT REGEXP '^[A-Z]'"))
    ids = [r["id"] for r in rows]
    assert 1 in ids
    assert 2 in ids
    assert 4 in ids
    assert 3 not in ids
