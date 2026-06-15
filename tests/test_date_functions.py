"""Tests for MySQL-compatible date manipulation functions:
NOW(), DATEDIFF(), DATE_ADD(), DATE_SUB(), DATE_FORMAT()
"""
import re
import pytest
from datetime import datetime
from hyperion import Database


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "test.hdb"))
    d.execute("CREATE TABLE events (id INTEGER, name TEXT, ts TEXT, dt TEXT)")
    d.execute("INSERT INTO events VALUES (1, 'alpha', '2024-01-15 10:30:00', '2024-01-15')")
    d.execute("INSERT INTO events VALUES (2, 'beta',  '2024-03-20 08:00:00', '2024-03-20')")
    d.execute("INSERT INTO events VALUES (3, 'gamma', '2024-12-31 23:59:59', '2024-12-31')")
    yield d
    d.close()


def _fetch(cursor):
    return list(cursor.fetchall())


# --- NOW() ---

def test_now_returns_string(db):
    rows = _fetch(db.execute("SELECT NOW() AS n"))
    val = rows[0]["n"]
    assert isinstance(val, str)
    assert re.match(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$', val)


def test_now_close_to_current_time(db):
    rows = _fetch(db.execute("SELECT NOW() AS n"))
    val = rows[0]["n"]
    diff = abs((datetime.now() - datetime.strptime(val, "%Y-%m-%d %H:%M:%S")).total_seconds())
    assert diff < 5


def test_now_in_where(db):
    rows = _fetch(db.execute("SELECT id FROM events WHERE ts < NOW()"))
    assert len(rows) == 3


def test_now_alias_for_current_timestamp(db):
    rows_now = _fetch(db.execute("SELECT NOW() AS n"))
    rows_ct  = _fetch(db.execute("SELECT CURRENT_TIMESTAMP AS n"))
    n = datetime.strptime(rows_now[0]["n"], "%Y-%m-%d %H:%M:%S")
    c = datetime.strptime(rows_ct[0]["n"],  "%Y-%m-%d %H:%M:%S")
    assert abs((n - c).total_seconds()) < 2


# --- DATEDIFF() ---

def test_datediff_positive(db):
    rows = _fetch(db.execute("SELECT DATEDIFF('2024-03-20', '2024-01-15') AS d"))
    assert rows[0]["d"] == 65


def test_datediff_negative(db):
    rows = _fetch(db.execute("SELECT DATEDIFF('2024-01-15', '2024-03-20') AS d"))
    assert rows[0]["d"] == -65


def test_datediff_same_date(db):
    rows = _fetch(db.execute("SELECT DATEDIFF('2024-06-01', '2024-06-01') AS d"))
    assert rows[0]["d"] == 0


def test_datediff_on_columns(db):
    rows = _fetch(db.execute("SELECT DATEDIFF('2024-03-20', dt) AS d FROM events WHERE id = 1"))
    assert rows[0]["d"] == 65


def test_datediff_null_propagation(db):
    rows = _fetch(db.execute("SELECT DATEDIFF(NULL, '2024-01-01') AS d"))
    assert rows[0]["d"] is None


def test_datediff_year_boundary(db):
    rows = _fetch(db.execute("SELECT DATEDIFF('2025-01-01', '2024-01-01') AS d"))
    assert rows[0]["d"] == 366  # 2024 is a leap year


# --- DATE_ADD() ---

def test_date_add_days(db):
    rows = _fetch(db.execute("SELECT DATE_ADD('2024-01-15', INTERVAL 30 DAY) AS r"))
    assert rows[0]["r"] == "2024-02-14 00:00:00"


def test_date_add_months(db):
    rows = _fetch(db.execute("SELECT DATE_ADD('2024-01-31', INTERVAL 1 MONTH) AS r"))
    assert rows[0]["r"] == "2024-02-29 00:00:00"  # leap year clamping


def test_date_add_years(db):
    rows = _fetch(db.execute("SELECT DATE_ADD('2024-03-20', INTERVAL 2 YEAR) AS r"))
    assert rows[0]["r"] == "2026-03-20 00:00:00"


def test_date_add_hours(db):
    rows = _fetch(db.execute("SELECT DATE_ADD('2024-01-15 10:30:00', INTERVAL 3 HOUR) AS r"))
    assert rows[0]["r"] == "2024-01-15 13:30:00"


def test_date_add_minutes(db):
    rows = _fetch(db.execute("SELECT DATE_ADD('2024-01-15 10:30:00', INTERVAL 45 MINUTE) AS r"))
    assert rows[0]["r"] == "2024-01-15 11:15:00"


def test_date_add_seconds(db):
    rows = _fetch(db.execute("SELECT DATE_ADD('2024-01-15 10:30:00', INTERVAL 90 SECOND) AS r"))
    assert rows[0]["r"] == "2024-01-15 10:31:30"


def test_date_add_on_column(db):
    rows = _fetch(db.execute(
        "SELECT DATE_ADD(ts, INTERVAL 7 DAY) AS r FROM events WHERE id = 1"
    ))
    assert rows[0]["r"] == "2024-01-22 10:30:00"


def test_date_add_null_propagation(db):
    rows = _fetch(db.execute("SELECT DATE_ADD(NULL, INTERVAL 1 DAY) AS r"))
    assert rows[0]["r"] is None


# --- DATE_SUB() ---

def test_date_sub_days(db):
    rows = _fetch(db.execute("SELECT DATE_SUB('2024-03-20', INTERVAL 65 DAY) AS r"))
    assert rows[0]["r"] == "2024-01-15 00:00:00"


def test_date_sub_months(db):
    rows = _fetch(db.execute("SELECT DATE_SUB('2024-03-31', INTERVAL 1 MONTH) AS r"))
    assert rows[0]["r"] == "2024-02-29 00:00:00"  # leap year clamping


def test_date_sub_years(db):
    rows = _fetch(db.execute("SELECT DATE_SUB('2024-03-20', INTERVAL 2 YEAR) AS r"))
    assert rows[0]["r"] == "2022-03-20 00:00:00"


def test_date_sub_hours(db):
    rows = _fetch(db.execute("SELECT DATE_SUB('2024-01-15 10:30:00', INTERVAL 2 HOUR) AS r"))
    assert rows[0]["r"] == "2024-01-15 08:30:00"


def test_date_sub_null_propagation(db):
    rows = _fetch(db.execute("SELECT DATE_SUB(NULL, INTERVAL 1 DAY) AS r"))
    assert rows[0]["r"] is None


def test_date_add_sub_inverse(db):
    rows = _fetch(db.execute(
        "SELECT DATE_SUB(DATE_ADD('2024-06-15', INTERVAL 30 DAY), INTERVAL 30 DAY) AS r"
    ))
    assert rows[0]["r"] == "2024-06-15 00:00:00"


# --- DATE_FORMAT() ---

def test_date_format_year_month_day(db):
    rows = _fetch(db.execute("SELECT DATE_FORMAT('2024-03-20', '%Y-%m-%d') AS r"))
    assert rows[0]["r"] == "2024-03-20"


def test_date_format_custom(db):
    rows = _fetch(db.execute("SELECT DATE_FORMAT('2024-03-20 08:00:00', '%d/%m/%Y') AS r"))
    assert rows[0]["r"] == "20/03/2024"


def test_date_format_time_components(db):
    rows = _fetch(db.execute("SELECT DATE_FORMAT('2024-01-15 10:30:45', '%H:%i:%s') AS r"))
    assert rows[0]["r"] == "10:30:45"


def test_date_format_month_name(db):
    rows = _fetch(db.execute("SELECT DATE_FORMAT('2024-03-20', '%M %Y') AS r"))
    assert rows[0]["r"] == "March 2024"


def test_date_format_24h_clock(db):
    rows = _fetch(db.execute("SELECT DATE_FORMAT('2024-01-15 14:30:00', '%T') AS r"))
    assert rows[0]["r"] == "14:30:00"


def test_date_format_null_propagation(db):
    rows = _fetch(db.execute("SELECT DATE_FORMAT(NULL, '%Y-%m-%d') AS r"))
    assert rows[0]["r"] is None


def test_date_format_on_column(db):
    rows = _fetch(db.execute(
        "SELECT DATE_FORMAT(ts, '%Y/%m/%d') AS r FROM events WHERE id = 2"
    ))
    assert rows[0]["r"] == "2024/03/20"


# --- Combined usage ---

def test_date_add_in_where(db):
    rows = _fetch(db.execute(
        "SELECT id FROM events WHERE ts > DATE_SUB('2024-03-20', INTERVAL 10 DAY)"
    ))
    ids = [r["id"] for r in rows]
    assert 2 in ids
    assert 3 in ids
    assert 1 not in ids


def test_datediff_filter(db):
    rows = _fetch(db.execute(
        "SELECT name FROM events WHERE DATEDIFF('2024-12-31', dt) > 100"
    ))
    names = [r["name"] for r in rows]
    assert "alpha" in names
    assert "beta" in names
