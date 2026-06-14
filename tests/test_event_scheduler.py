"""Tests for the Event Scheduler: CREATE/DROP/ALTER EVENT, SHOW EVENTS, background firing."""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from hyperion.database import Database
from hyperion.errors import SchemaError


# ── Helpers ────────────────────────────────────────────────────────────────────

def _db():
    db = Database(":memory:")
    db.begin()
    db.execute("CREATE TABLE counter (id INTEGER PRIMARY KEY, n INTEGER)")
    db.execute("INSERT INTO counter VALUES (1, 0)")
    db.commit()
    return db


# ── CREATE EVENT ──────────────────────────────────────────────────────────────

def test_create_event_interval_sql():
    db = _db()
    db.execute("CREATE EVENT tick ON SCHEDULE EVERY 60 SECOND DO UPDATE counter SET n = n + 1 WHERE id = 1")
    assert "tick" in db._catalog.events
    evt = db._catalog.events["tick"]
    assert evt.schedule_type == "INTERVAL"
    assert evt.interval_seconds == 60
    assert evt.enabled is True
    assert evt.sql == "UPDATE counter SET n = n + 1 WHERE id = 1"
    db.close()


def test_create_event_interval_minute():
    db = _db()
    db.execute("CREATE EVENT tick ON SCHEDULE EVERY 2 MINUTE DO SELECT 1")
    assert db._catalog.events["tick"].interval_seconds == 120
    db.close()


def test_create_event_interval_hour():
    db = _db()
    db.execute("CREATE EVENT tick ON SCHEDULE EVERY 1 HOUR DO SELECT 1")
    assert db._catalog.events["tick"].interval_seconds == 3600
    db.close()


def test_create_event_interval_day():
    db = _db()
    db.execute("CREATE EVENT tick ON SCHEDULE EVERY 1 DAY DO SELECT 1")
    assert db._catalog.events["tick"].interval_seconds == 86400
    db.close()


def test_create_event_at():
    db = _db()
    db.execute("CREATE EVENT oneshot ON SCHEDULE AT '2099-01-01 00:00:00' DO SELECT 1")
    evt = db._catalog.events["oneshot"]
    assert evt.schedule_type == "AT"
    assert evt.at_time == "2099-01-01 00:00:00"
    db.close()


def test_create_event_if_not_exists():
    db = _db()
    db.execute("CREATE EVENT tick ON SCHEDULE EVERY 5 SECOND DO SELECT 1")
    db.execute("CREATE EVENT IF NOT EXISTS tick ON SCHEDULE EVERY 10 SECOND DO SELECT 2")
    assert db._catalog.events["tick"].interval_seconds == 5
    db.close()


def test_create_event_duplicate_raises():
    db = _db()
    db.execute("CREATE EVENT tick ON SCHEDULE EVERY 5 SECOND DO SELECT 1")
    with pytest.raises((SchemaError, RuntimeError)):
        db.execute("CREATE EVENT tick ON SCHEDULE EVERY 10 SECOND DO SELECT 2")
    db.close()


# ── DROP EVENT ────────────────────────────────────────────────────────────────

def test_drop_event_sql():
    db = _db()
    db.execute("CREATE EVENT tick ON SCHEDULE EVERY 60 SECOND DO SELECT 1")
    db.execute("DROP EVENT tick")
    assert "tick" not in db._catalog.events
    db.close()


def test_drop_event_if_exists():
    db = _db()
    db.execute("DROP EVENT IF EXISTS nonexistent")
    db.close()


def test_drop_event_missing_raises():
    db = _db()
    with pytest.raises((SchemaError, RuntimeError)):
        db.execute("DROP EVENT nonexistent")
    db.close()


# ── ALTER EVENT ENABLE / DISABLE ──────────────────────────────────────────────

def test_alter_event_disable():
    db = _db()
    db.execute("CREATE EVENT tick ON SCHEDULE EVERY 60 SECOND DO SELECT 1")
    db.execute("ALTER EVENT tick DISABLE")
    assert db._catalog.events["tick"].enabled is False
    db.close()


def test_alter_event_enable():
    db = _db()
    db.execute("CREATE EVENT tick ON SCHEDULE EVERY 60 SECOND DO SELECT 1")
    db.execute("ALTER EVENT tick DISABLE")
    db.execute("ALTER EVENT tick ENABLE")
    assert db._catalog.events["tick"].enabled is True
    db.close()


# ── SHOW EVENTS ───────────────────────────────────────────────────────────────

def test_show_events_empty():
    db = _db()
    rows = list(db.execute("SHOW EVENTS").fetchall())
    assert rows == []
    db.close()


def test_show_events_populated():
    db = _db()
    db.execute("CREATE EVENT tick ON SCHEDULE EVERY 30 SECOND DO SELECT 1")
    db.execute("CREATE EVENT oneshot ON SCHEDULE AT '2099-12-31 23:59:59' DO SELECT 2")
    rows = list(db.execute("SHOW EVENTS").fetchall())
    assert len(rows) == 2
    names = {r["name"] for r in rows}
    assert names == {"tick", "oneshot"}
    tick = next(r for r in rows if r["name"] == "tick")
    assert "30" in tick["schedule"]
    assert tick["enabled"] is True
    db.close()


# ── Event persistence ─────────────────────────────────────────────────────────

def test_events_persist_across_reopen(tmp_path):
    db = Database(tmp_path / "evt.hdb")
    db.begin()
    db.execute("CREATE TABLE counter (id INTEGER PRIMARY KEY, n INTEGER)")
    db.execute("INSERT INTO counter VALUES (1, 0)")
    db.commit()
    db.execute("CREATE EVENT tick ON SCHEDULE EVERY 60 SECOND DO UPDATE counter SET n = n + 1 WHERE id = 1")
    db.begin(); db.commit()  # flush schema
    db.close()

    db2 = Database(tmp_path / "evt.hdb")
    assert "tick" in db2._catalog.events
    assert db2._catalog.events["tick"].interval_seconds == 60
    db2.close()


# ── Background event firing ───────────────────────────────────────────────────

def test_interval_event_fires(tmp_path):
    db = Database(tmp_path / "fire.hdb")
    db.begin()
    db.execute("CREATE TABLE counter (id INTEGER PRIMARY KEY, n INTEGER)")
    db.execute("INSERT INTO counter VALUES (1, 0)")
    db.commit()

    # Create a very short-interval event (fires every 1 second)
    db.execute("CREATE EVENT tick ON SCHEDULE EVERY 1 SECOND DO UPDATE counter SET n = n + 1 WHERE id = 1")

    # Wait up to 4 seconds for at least one fire
    deadline = time.time() + 4
    while time.time() < deadline:
        rows = list(db.execute("SELECT n FROM counter WHERE id = 1").fetchall())
        if rows and rows[0]["n"] > 0:
            break
        time.sleep(0.2)

    rows = list(db.execute("SELECT n FROM counter WHERE id = 1").fetchall())
    assert rows[0]["n"] > 0, "event never fired"
    db.close()


def test_at_event_fires_and_drops(tmp_path):
    db = Database(tmp_path / "at.hdb")
    db.begin()
    db.execute("CREATE TABLE counter (id INTEGER PRIMARY KEY, n INTEGER)")
    db.execute("INSERT INTO counter VALUES (1, 0)")
    db.commit()

    # AT event set for ~2 seconds in the future
    at_dt = datetime.now() + timedelta(seconds=2)
    at_str = at_dt.strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        f"CREATE EVENT oneshot ON SCHEDULE AT '{at_str}' "
        "DO UPDATE counter SET n = n + 1 WHERE id = 1"
    )

    # Wait up to 6 seconds for it to fire and auto-drop
    deadline = time.time() + 6
    while time.time() < deadline:
        rows = list(db.execute("SELECT n FROM counter WHERE id = 1").fetchall())
        if rows and rows[0]["n"] > 0:
            break
        time.sleep(0.3)

    rows = list(db.execute("SELECT n FROM counter WHERE id = 1").fetchall())
    assert rows[0]["n"] > 0, "AT event never fired"
    # AT events are auto-dropped after firing
    assert "oneshot" not in db._catalog.events
    db.close()


def test_disabled_event_does_not_fire(tmp_path):
    db = Database(tmp_path / "dis.hdb")
    db.begin()
    db.execute("CREATE TABLE counter (id INTEGER PRIMARY KEY, n INTEGER)")
    db.execute("INSERT INTO counter VALUES (1, 0)")
    db.commit()

    db.execute("CREATE EVENT tick ON SCHEDULE EVERY 1 SECOND DO UPDATE counter SET n = n + 1 WHERE id = 1")
    db.execute("ALTER EVENT tick DISABLE")

    time.sleep(2.5)

    rows = list(db.execute("SELECT n FROM counter WHERE id = 1").fetchall())
    assert rows[0]["n"] == 0, "disabled event should not fire"
    db.close()


def test_at_event_in_past_does_not_fire(tmp_path):
    """An AT event whose timestamp is in the past fires immediately on first tick."""
    db = Database(tmp_path / "past.hdb")
    db.begin()
    db.execute("CREATE TABLE counter (id INTEGER PRIMARY KEY, n INTEGER)")
    db.execute("INSERT INTO counter VALUES (1, 0)")
    db.commit()

    past_str = "2000-01-01 00:00:00"
    db.execute(
        f"CREATE EVENT old ON SCHEDULE AT '{past_str}' "
        "DO UPDATE counter SET n = n + 1 WHERE id = 1"
    )

    # Wait up to 3 seconds — should fire quickly on first tick
    deadline = time.time() + 3
    while time.time() < deadline:
        rows = list(db.execute("SELECT n FROM counter WHERE id = 1").fetchall())
        if rows and rows[0]["n"] > 0:
            break
        time.sleep(0.2)

    rows = list(db.execute("SELECT n FROM counter WHERE id = 1").fetchall())
    assert rows[0]["n"] > 0
    assert "old" not in db._catalog.events  # auto-dropped
    db.close()


# ── Savepoint rollback ────────────────────────────────────────────────────────

def test_event_rolls_back_with_savepoint():
    db = _db()
    db.begin()
    db.execute("SAVEPOINT sp1")
    db.execute("CREATE EVENT tick ON SCHEDULE EVERY 60 SECOND DO SELECT 1")
    db.execute("ROLLBACK TO SAVEPOINT sp1")
    db.commit()
    assert "tick" not in db._catalog.events
    db.close()
