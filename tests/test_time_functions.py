"""Tests for TIME column type and time-specific functions:
TIMEDIFF, ADDTIME, SUBTIME, HOUR, MINUTE, SECOND,
TIME_TO_SEC, SEC_TO_TIME, DAY, MONTH, YEAR
"""
import pytest
from hyperion import Database


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "test.hdb"))
    d.execute("CREATE TABLE shifts (id INTEGER, name TEXT, start_time TIME, end_time TIME)")
    d.execute("INSERT INTO shifts VALUES (1, 'morning', '08:00:00', '16:00:00')")
    d.execute("INSERT INTO shifts VALUES (2, 'evening', '16:00:00', '23:30:00')")
    d.execute("INSERT INTO shifts VALUES (3, 'night',   '23:00:00', '07:00:00')")
    d.execute("CREATE TABLE events (id INTEGER, name TEXT, ts TEXT)")
    d.execute("INSERT INTO events VALUES (1, 'alpha', '2024-03-15 14:30:45')")
    d.execute("INSERT INTO events VALUES (2, 'beta',  '2024-12-01 00:00:00')")
    yield d
    d.close()


def _fetch(cursor):
    return list(cursor.fetchall())


# --- TIME column type ---

def test_time_column_stores_value(db):
    rows = _fetch(db.execute("SELECT start_time FROM shifts WHERE id = 1"))
    assert rows[0]["start_time"] == "08:00:00"


def test_time_column_in_where(db):
    rows = _fetch(db.execute("SELECT name FROM shifts WHERE start_time = '16:00:00'"))
    assert rows[0]["name"] == "evening"


def test_time_column_order_by(db):
    rows = _fetch(db.execute("SELECT name FROM shifts WHERE id < 3 ORDER BY start_time"))
    assert [r["name"] for r in rows] == ["morning", "evening"]


def test_time_column_rejects_too_long(db, tmp_path):
    d = Database(str(tmp_path / "t.hdb"))
    d.execute("CREATE TABLE t (v TIME)")
    with pytest.raises(Exception):
        d.execute("INSERT INTO t VALUES ('this-is-too-long')")
    d.close()


def test_time_current_time_default(db, tmp_path):
    d = Database(str(tmp_path / "t2.hdb"))
    d.execute("CREATE TABLE t (id INTEGER, v TIME DEFAULT CURRENT_TIME)")
    d.execute("INSERT INTO t (id) VALUES (1)")
    rows = _fetch(d.execute("SELECT v FROM t"))
    import re
    assert re.match(r'^\d{2}:\d{2}:\d{2}$', rows[0]["v"])
    d.close()


# --- TIMEDIFF ---

def test_timediff_positive(db):
    rows = _fetch(db.execute("SELECT TIMEDIFF('16:00:00', '08:00:00') AS r"))
    assert rows[0]["r"] == "08:00:00"


def test_timediff_negative(db):
    rows = _fetch(db.execute("SELECT TIMEDIFF('08:00:00', '16:00:00') AS r"))
    assert rows[0]["r"] == "-08:00:00"


def test_timediff_zero(db):
    rows = _fetch(db.execute("SELECT TIMEDIFF('12:00:00', '12:00:00') AS r"))
    assert rows[0]["r"] == "00:00:00"


def test_timediff_on_columns(db):
    rows = _fetch(db.execute(
        "SELECT TIMEDIFF(end_time, start_time) AS r FROM shifts WHERE id = 1"
    ))
    assert rows[0]["r"] == "08:00:00"


def test_timediff_with_seconds(db):
    rows = _fetch(db.execute("SELECT TIMEDIFF('14:30:45', '09:15:30') AS r"))
    assert rows[0]["r"] == "05:15:15"


def test_timediff_null(db):
    rows = _fetch(db.execute("SELECT TIMEDIFF(NULL, '08:00:00') AS r"))
    assert rows[0]["r"] is None


def test_timediff_from_datetime(db):
    rows = _fetch(db.execute(
        "SELECT TIMEDIFF(ts, '2024-03-15 08:00:00') AS r FROM events WHERE id = 1"
    ))
    assert rows[0]["r"] == "06:30:45"


# --- ADDTIME / SUBTIME ---

def test_addtime_basic(db):
    rows = _fetch(db.execute("SELECT ADDTIME('10:30:00', '01:30:00') AS r"))
    assert rows[0]["r"] == "12:00:00"


def test_addtime_carry(db):
    rows = _fetch(db.execute("SELECT ADDTIME('23:00:00', '02:00:00') AS r"))
    assert rows[0]["r"] == "25:00:00"


def test_subtime_basic(db):
    rows = _fetch(db.execute("SELECT SUBTIME('16:00:00', '08:00:00') AS r"))
    assert rows[0]["r"] == "08:00:00"


def test_subtime_negative_result(db):
    rows = _fetch(db.execute("SELECT SUBTIME('08:00:00', '10:00:00') AS r"))
    assert rows[0]["r"] == "-02:00:00"


def test_addtime_null(db):
    rows = _fetch(db.execute("SELECT ADDTIME(NULL, '01:00:00') AS r"))
    assert rows[0]["r"] is None


# --- HOUR / MINUTE / SECOND ---

def test_hour_from_time(db):
    rows = _fetch(db.execute("SELECT HOUR('14:30:45') AS r"))
    assert rows[0]["r"] == 14


def test_minute_from_time(db):
    rows = _fetch(db.execute("SELECT MINUTE('14:30:45') AS r"))
    assert rows[0]["r"] == 30


def test_second_from_time(db):
    rows = _fetch(db.execute("SELECT SECOND('14:30:45') AS r"))
    assert rows[0]["r"] == 45


def test_hour_from_datetime(db):
    rows = _fetch(db.execute(
        "SELECT HOUR(ts) AS r FROM events WHERE id = 1"
    ))
    assert rows[0]["r"] == 14


def test_minute_from_column(db):
    rows = _fetch(db.execute(
        "SELECT MINUTE(ts) AS r FROM events WHERE id = 1"
    ))
    assert rows[0]["r"] == 30


def test_second_from_column(db):
    rows = _fetch(db.execute(
        "SELECT SECOND(ts) AS r FROM events WHERE id = 1"
    ))
    assert rows[0]["r"] == 45


def test_hour_null(db):
    rows = _fetch(db.execute("SELECT HOUR(NULL) AS r"))
    assert rows[0]["r"] is None


# --- TIME_TO_SEC / SEC_TO_TIME ---

def test_time_to_sec_basic(db):
    rows = _fetch(db.execute("SELECT TIME_TO_SEC('01:00:00') AS r"))
    assert rows[0]["r"] == 3600


def test_time_to_sec_hours_minutes(db):
    rows = _fetch(db.execute("SELECT TIME_TO_SEC('01:30:00') AS r"))
    assert rows[0]["r"] == 5400


def test_time_to_sec_full(db):
    rows = _fetch(db.execute("SELECT TIME_TO_SEC('08:30:15') AS r"))
    assert rows[0]["r"] == 8 * 3600 + 30 * 60 + 15


def test_sec_to_time_basic(db):
    rows = _fetch(db.execute("SELECT SEC_TO_TIME(3600) AS r"))
    assert rows[0]["r"] == "01:00:00"


def test_sec_to_time_full(db):
    rows = _fetch(db.execute("SELECT SEC_TO_TIME(5400) AS r"))
    assert rows[0]["r"] == "01:30:00"


def test_sec_to_time_negative(db):
    rows = _fetch(db.execute("SELECT SEC_TO_TIME(-3600) AS r"))
    assert rows[0]["r"] == "-01:00:00"


def test_time_to_sec_roundtrip(db):
    rows = _fetch(db.execute("SELECT SEC_TO_TIME(TIME_TO_SEC('14:30:45')) AS r"))
    assert rows[0]["r"] == "14:30:45"


def test_time_to_sec_null(db):
    rows = _fetch(db.execute("SELECT TIME_TO_SEC(NULL) AS r"))
    assert rows[0]["r"] is None


# --- DAY / MONTH / YEAR ---

def test_day_from_date(db):
    rows = _fetch(db.execute("SELECT DAY('2024-03-15') AS r"))
    assert rows[0]["r"] == 15


def test_month_from_date(db):
    rows = _fetch(db.execute("SELECT MONTH('2024-03-15') AS r"))
    assert rows[0]["r"] == 3


def test_year_from_date(db):
    rows = _fetch(db.execute("SELECT YEAR('2024-03-15') AS r"))
    assert rows[0]["r"] == 2024


def test_day_from_datetime_column(db):
    rows = _fetch(db.execute("SELECT DAY(ts) AS r FROM events WHERE id = 1"))
    assert rows[0]["r"] == 15


def test_month_from_datetime_column(db):
    rows = _fetch(db.execute("SELECT MONTH(ts) AS r FROM events WHERE id = 2"))
    assert rows[0]["r"] == 12


def test_year_from_datetime_column(db):
    rows = _fetch(db.execute("SELECT YEAR(ts) AS r FROM events WHERE id = 2"))
    assert rows[0]["r"] == 2024


def test_dayofmonth_alias(db):
    rows = _fetch(db.execute("SELECT DAYOFMONTH('2024-03-15') AS r"))
    assert rows[0]["r"] == 15


def test_day_null(db):
    rows = _fetch(db.execute("SELECT DAY(NULL) AS r"))
    assert rows[0]["r"] is None


def test_year_in_where(db):
    rows = _fetch(db.execute(
        "SELECT name FROM events WHERE YEAR(ts) = 2024 AND MONTH(ts) = 12"
    ))
    assert rows[0]["name"] == "beta"
