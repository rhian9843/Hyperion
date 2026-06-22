"""Tests for query profiler: SET PROFILE ON|OFF, SHOW PROFILES,
SHOW PROFILE FOR QUERY n (ported from milansql query_profiler.hpp)."""

import pytest
from hyperion import Database


def fresh_db(tmp_path):
    return Database(str(tmp_path / "test.hdb"))


# ── SET PROFILE ───────────────────────────────────────────────────────────────

class TestSetProfile:

    def test_profile_off_by_default(self, tmp_path):
        db = fresh_db(tmp_path)
        assert not db._profiler.enabled
        db.close()

    def test_set_profile_on(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("SET PROFILE ON")
        assert db._profiler.enabled
        db.close()

    def test_set_profile_off(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("SET PROFILE ON")
        db.execute("SET PROFILE OFF")
        assert not db._profiler.enabled
        db.close()

    def test_set_profile_on_returns_message(self, tmp_path):
        db = fresh_db(tmp_path)
        cur = db.execute("SET PROFILE ON")
        assert cur.rowcount == -1 or cur.fetchone() is None
        db.close()

    def test_no_profiles_when_disabled(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO t VALUES (1)")
        db.execute("SELECT * FROM t")
        rows = db.execute("SHOW PROFILES").fetchall()
        assert rows == []
        db.close()


# ── SHOW PROFILES ─────────────────────────────────────────────────────────────

class TestShowProfiles:

    def test_empty_when_just_enabled(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("SET PROFILE ON")
        rows = db.execute("SHOW PROFILES").fetchall()
        assert rows == []
        db.close()

    def test_profiles_columns(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("SET PROFILE ON")
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        rows = db.execute("SHOW PROFILES").fetchall()
        assert len(rows) >= 1
        assert set(rows[0].keys()) == {"id", "sql", "total_ms"}
        db.close()

    def test_sql_captured_correctly(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("SET PROFILE ON")
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO t VALUES (1)")
        db.execute("SELECT * FROM t WHERE id = 1")
        rows = db.execute("SHOW PROFILES").fetchall()
        sqls = [r["sql"] for r in rows]
        assert any("CREATE TABLE" in s for s in sqls)
        assert any("INSERT" in s for s in sqls)
        assert any("SELECT" in s for s in sqls)
        db.close()

    def test_ids_are_sequential(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("SET PROFILE ON")
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO t VALUES (1)")
        rows = db.execute("SHOW PROFILES").fetchall()
        ids = [r["id"] for r in rows]
        assert ids == sorted(ids)
        assert ids[0] == 1
        db.close()

    def test_total_ms_is_positive(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("SET PROFILE ON")
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO t VALUES (1)")
        rows = db.execute("SHOW PROFILES").fetchall()
        assert all(r["total_ms"] >= 0 for r in rows)
        db.close()

    def test_long_sql_truncated_to_80_chars(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("SET PROFILE ON")
        db.execute("CREATE TABLE t (a TEXT, b TEXT, c TEXT, d TEXT, e TEXT, f TEXT, g TEXT, h TEXT)")
        rows = db.execute("SHOW PROFILES").fetchall()
        assert all(len(r["sql"]) <= 80 for r in rows)
        db.close()

    def test_each_statement_creates_one_entry(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("SET PROFILE ON")
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO t VALUES (1)")
        db.execute("INSERT INTO t VALUES (2)")
        db.execute("SELECT * FROM t")
        rows = db.execute("SHOW PROFILES").fetchall()
        assert len(rows) == 4  # CREATE + 2 INSERTs + SELECT
        db.close()

    def test_max_100_entries_kept(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        db.execute("SET PROFILE ON")
        for i in range(110):
            db.execute("INSERT INTO t VALUES (?)", (i,))
        rows = db.execute("SHOW PROFILES").fetchall()
        assert len(rows) == 100
        db.close()

    def test_oldest_dropped_when_over_limit(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        db.execute("SET PROFILE ON")
        for i in range(110):
            db.execute("INSERT INTO t VALUES (?)", (i,))
        rows = db.execute("SHOW PROFILES").fetchall()
        # The oldest entries should be gone; newest (id ≥ 11) should remain
        min_id = min(r["id"] for r in rows)
        assert min_id > 1
        db.close()

    def test_profiling_disabled_stops_recording(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        db.execute("SET PROFILE ON")
        db.execute("INSERT INTO t VALUES (1)")
        db.execute("SET PROFILE OFF")
        db.execute("INSERT INTO t VALUES (2)")  # not recorded
        rows = db.execute("SHOW PROFILES").fetchall()
        # Only the first INSERT should be recorded
        assert len(rows) == 1
        db.close()


# ── SHOW PROFILE FOR QUERY n ──────────────────────────────────────────────────

class TestShowProfileForQuery:

    def test_detail_columns(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("SET PROFILE ON")
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        rows = db.execute("SHOW PROFILES").fetchall()
        qid = rows[0]["id"]
        detail = db.execute(f"SHOW PROFILE FOR QUERY {qid}").fetchall()
        assert len(detail) >= 1
        assert set(detail[0].keys()) == {"status", "duration_ms"}
        db.close()

    def test_total_row_present(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("SET PROFILE ON")
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        rows = db.execute("SHOW PROFILES").fetchall()
        qid = rows[0]["id"]
        detail = db.execute(f"SHOW PROFILE FOR QUERY {qid}").fetchall()
        statuses = [r["status"] for r in detail]
        assert "total" in statuses
        db.close()

    def test_execute_step_present(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("SET PROFILE ON")
        db.execute("SELECT 1+1")
        rows = db.execute("SHOW PROFILES").fetchall()
        qid = rows[0]["id"]
        detail = db.execute(f"SHOW PROFILE FOR QUERY {qid}").fetchall()
        statuses = [r["status"] for r in detail]
        assert "execute" in statuses
        db.close()

    def test_total_ms_matches_show_profiles(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("SET PROFILE ON")
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        profiles = db.execute("SHOW PROFILES").fetchall()
        qid   = profiles[0]["id"]
        total = profiles[0]["total_ms"]
        detail = db.execute(f"SHOW PROFILE FOR QUERY {qid}").fetchall()
        total_row = next(r for r in detail if r["status"] == "total")
        assert total_row["duration_ms"] == total
        db.close()

    def test_unknown_id_raises(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("SET PROFILE ON")
        with pytest.raises(Exception):
            db.execute("SHOW PROFILE FOR QUERY 9999").fetchall()
        db.close()

    def test_can_look_up_each_profile_by_id(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("SET PROFILE ON")
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO t VALUES (1)")
        db.execute("SELECT * FROM t")
        profiles = db.execute("SHOW PROFILES").fetchall()
        for p in profiles:
            detail = db.execute(f"SHOW PROFILE FOR QUERY {p['id']}").fetchall()
            assert any(r["status"] == "total" for r in detail)
        db.close()
