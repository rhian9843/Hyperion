"""Tests for query result cache: SET CACHE ON|OFF, SHOW CACHE STATUS,
LRU eviction, TTL expiry, invalidation on write."""

import time
import pytest
from hyperion import Database
from hyperion.query_cache import QueryCache, make_cache_key, extract_tables


# ── unit tests for QueryCache class ──────────────────────────────────────────

class TestQueryCacheUnit:

    def test_disabled_by_default(self):
        qc = QueryCache()
        assert not qc.enabled

    def test_enable_disable(self):
        qc = QueryCache()
        qc.enable()
        assert qc.enabled
        qc.disable()
        assert not qc.enabled

    def test_get_returns_none_when_disabled(self):
        qc = QueryCache()
        qc.put("SELECT 1", [{"a": 1}], set())
        assert qc.get("SELECT 1") is None

    def test_put_and_get(self):
        qc = QueryCache()
        qc.enable()
        qc.put("SELECT 1", [{"a": 1}], {"t"})
        result = qc.get("SELECT 1")
        assert result == [{"a": 1}]

    def test_get_increments_hits(self):
        qc = QueryCache()
        qc.enable()
        qc.put("q", [{}], set())
        qc.get("q")
        qc.get("q")
        assert qc._total_hits == 2

    def test_miss_increments_misses(self):
        qc = QueryCache()
        qc.enable()
        qc.get("nonexistent")
        assert qc._total_misses == 1

    def test_invalidate_removes_entry(self):
        qc = QueryCache()
        qc.enable()
        qc.put("SELECT * FROM t", [{"id": 1}], {"t"})
        qc.invalidate("t")
        assert qc.get("SELECT * FROM t") is None

    def test_invalidate_only_matching_table(self):
        qc = QueryCache()
        qc.enable()
        qc.put("SELECT * FROM a", [{"id": 1}], {"a"})
        qc.put("SELECT * FROM b", [{"id": 2}], {"b"})
        qc.invalidate("a")
        assert qc.get("SELECT * FROM a") is None
        assert qc.get("SELECT * FROM b") is not None

    def test_lru_eviction_at_max_size(self):
        qc = QueryCache()
        qc.enable()
        qc._max_size = 3
        qc.put("q1", [{"a": 1}], set())
        time.sleep(0.01)
        qc.put("q2", [{"a": 2}], set())
        time.sleep(0.01)
        qc.put("q3", [{"a": 3}], set())
        # Access q1 to make it recently used
        qc.get("q1")
        time.sleep(0.01)
        # q2 is now oldest; inserting q4 should evict q2
        qc.put("q4", [{"a": 4}], set())
        assert qc.get("q2") is None
        assert qc.get("q1") is not None
        assert qc.get("q3") is not None
        assert qc.get("q4") is not None

    def test_ttl_expiry(self):
        qc = QueryCache()
        qc.enable()
        qc._ttl = 0  # instant expiry
        qc.put("q", [{"x": 1}], set())
        time.sleep(0.01)
        assert qc.get("q") is None

    def test_clear(self):
        qc = QueryCache()
        qc.enable()
        qc.put("q1", [{"a": 1}], set())
        qc.put("q2", [{"b": 2}], set())
        qc.clear()
        assert qc.get("q1") is None
        assert qc.get("q2") is None

    def test_status_keys(self):
        qc = QueryCache()
        s = qc.status()
        assert set(s.keys()) == {"status", "entries", "max_entries",
                                  "ttl_seconds", "hits", "misses", "hit_rate"}

    def test_status_hit_rate(self):
        qc = QueryCache()
        qc.enable()
        qc.put("q", [{}], set())
        qc.get("q")           # hit
        qc.get("missing")     # miss
        s = qc.status()
        assert s["hit_rate"] == "50%"

    def test_get_returns_defensive_copy(self):
        qc = QueryCache()
        qc.enable()
        original = [{"a": 1}]
        qc.put("q", original, set())
        result = qc.get("q")
        result.append({"b": 2})
        assert len(qc.get("q")) == 1  # original cache unchanged


# ── SQL interface tests ───────────────────────────────────────────────────────

def fresh_db(tmp_path):
    return Database(str(tmp_path / "test.hdb"))


class TestSetCache:

    def test_off_by_default(self, tmp_path):
        db = fresh_db(tmp_path)
        assert not db._query_cache.enabled
        db.close()

    def test_set_cache_on(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("SET CACHE ON")
        assert db._query_cache.enabled
        db.close()

    def test_set_cache_off(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("SET CACHE ON")
        db.execute("SET CACHE OFF")
        assert not db._query_cache.enabled
        db.close()

    def test_no_caching_when_disabled(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO t VALUES (1)")
        db.execute("SELECT * FROM t")
        assert db._query_cache._total_hits + db._query_cache._total_misses == 0
        db.close()


class TestShowCacheStatus:

    def test_status_columns(self, tmp_path):
        db = fresh_db(tmp_path)
        rows = db.execute("SHOW CACHE STATUS").fetchall()
        settings = {r["setting"] for r in rows}
        assert "status" in settings
        assert "entries" in settings
        assert "hits" in settings
        assert "misses" in settings
        assert "hit_rate" in settings
        db.close()

    def test_status_off_by_default(self, tmp_path):
        db = fresh_db(tmp_path)
        rows = db.execute("SHOW CACHE STATUS").fetchall()
        status_row = next(r for r in rows if r["setting"] == "status")
        assert status_row["value"] == "OFF"
        db.close()

    def test_status_on_after_enable(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("SET CACHE ON")
        rows = db.execute("SHOW CACHE STATUS").fetchall()
        status_row = next(r for r in rows if r["setting"] == "status")
        assert status_row["value"] == "ON"
        db.close()


class TestCacheHitMiss:

    def test_second_select_is_cache_hit(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
        db.execute("INSERT INTO t VALUES (1, 'a')")
        db.execute("SET CACHE ON")
        db.execute("SELECT * FROM t").fetchall()   # miss
        db.execute("SELECT * FROM t").fetchall()   # hit
        assert db._query_cache._total_hits == 1
        assert db._query_cache._total_misses == 1
        db.close()

    def test_cached_result_matches_original(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
        db.execute("INSERT INTO t VALUES (1, 'hello')")
        db.execute("SET CACHE ON")
        rows1 = db.execute("SELECT * FROM t").fetchall()
        rows2 = db.execute("SELECT * FROM t").fetchall()
        assert rows1 == rows2
        db.close()

    def test_different_sqls_are_separate_entries(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
        db.execute("INSERT INTO t VALUES (1, 'a')")
        db.execute("INSERT INTO t VALUES (2, 'b')")
        db.execute("SET CACHE ON")
        db.execute("SELECT * FROM t WHERE id = 1").fetchall()
        db.execute("SELECT * FROM t WHERE id = 2").fetchall()
        assert db._query_cache._total_misses == 2
        db.execute("SELECT * FROM t WHERE id = 1").fetchall()
        db.execute("SELECT * FROM t WHERE id = 2").fetchall()
        assert db._query_cache._total_hits == 2
        db.close()


class TestCacheInvalidation:

    def test_insert_invalidates_cache(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
        db.execute("INSERT INTO t VALUES (1, 'a')")
        db.execute("SET CACHE ON")
        db.execute("SELECT * FROM t").fetchall()   # populate cache
        db.execute("INSERT INTO t VALUES (2, 'b')")  # invalidate
        rows = db.execute("SELECT * FROM t").fetchall()
        assert len(rows) == 2
        db.close()

    def test_update_invalidates_cache(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
        db.execute("INSERT INTO t VALUES (1, 'original')")
        db.execute("SET CACHE ON")
        db.execute("SELECT * FROM t").fetchall()
        db.execute("UPDATE t SET val = 'changed' WHERE id = 1")
        rows = db.execute("SELECT * FROM t").fetchall()
        assert rows[0]["val"] == "changed"
        db.close()

    def test_delete_invalidates_cache(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
        db.execute("INSERT INTO t VALUES (1, 'a')")
        db.execute("INSERT INTO t VALUES (2, 'b')")
        db.execute("SET CACHE ON")
        db.execute("SELECT * FROM t").fetchall()
        db.execute("DELETE FROM t WHERE id = 1")
        rows = db.execute("SELECT * FROM t").fetchall()
        assert len(rows) == 1
        db.close()

    def test_write_to_other_table_does_not_invalidate(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE a (id INTEGER PRIMARY KEY)")
        db.execute("CREATE TABLE b (id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO a VALUES (1)")
        db.execute("SET CACHE ON")
        db.execute("SELECT * FROM a").fetchall()   # cache a query
        hits_before = db._query_cache._total_hits
        db.execute("INSERT INTO b VALUES (1)")      # write to b, not a
        db.execute("SELECT * FROM a").fetchall()   # should still hit cache
        assert db._query_cache._total_hits == hits_before + 1
        db.close()

    def test_cache_repopulated_after_invalidation(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO t VALUES (1)")
        db.execute("SET CACHE ON")
        db.execute("SELECT * FROM t").fetchall()   # miss → cache
        db.execute("INSERT INTO t VALUES (2)")      # invalidate
        db.execute("SELECT * FROM t").fetchall()   # miss → re-cache
        db.execute("SELECT * FROM t").fetchall()   # hit
        assert db._query_cache._total_hits >= 1
        db.close()


class TestCacheWithParams:

    def test_different_params_are_separate_cache_entries(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
        db.execute("INSERT INTO t VALUES (1, 'one')")
        db.execute("INSERT INTO t VALUES (2, 'two')")
        db.execute("SET CACHE ON")
        rows1 = db.execute("SELECT * FROM t WHERE id = ?", (1,)).fetchall()
        rows2 = db.execute("SELECT * FROM t WHERE id = ?", (2,)).fetchall()
        assert rows1[0]["id"] == 1
        assert rows2[0]["id"] == 2
        assert db._query_cache._total_misses == 2
        db.close()

    def test_same_params_is_cache_hit(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
        db.execute("INSERT INTO t VALUES (1, 'one')")
        db.execute("SET CACHE ON")
        db.execute("SELECT * FROM t WHERE id = ?", (1,)).fetchall()
        db.execute("SELECT * FROM t WHERE id = ?", (1,)).fetchall()
        assert db._query_cache._total_hits == 1
        db.close()
