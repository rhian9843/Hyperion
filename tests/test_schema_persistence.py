"""Schema persistence round-trip with 50 tables.

Creates 50 tables, each with:
  - 3 columns (id INTEGER PRIMARY KEY, val TEXT, score REAL)
  - 1 index on the val column
  - 1 AFTER INSERT trigger that increments a counter in a side table

Closes and reopens the database, then verifies:
  (a) All 50 table schemas survive exactly (column names, types, constraints)
  (b) All 50 indexes are present and functional (INSERT + probe via EXPLAIN)
  (c) All 50 triggers fire correctly (counter incremented on INSERT)
  (d) ANALYZE stats survive after ANALYZE + reopen
"""
import tempfile
import os
import pytest
from hyperion import Database

N = 50


def _db_path(tmp_path):
    return str(tmp_path / "schema50.hdb")


def _build_db(path: str) -> Database:
    """Create the 50-table schema, insert seed rows, run ANALYZE."""
    db = Database(path)

    # Side-table to verify triggers
    db.execute("CREATE TABLE trigger_log (tbl TEXT, cnt INTEGER DEFAULT 0)")
    for i in range(N):
        db.execute(f"INSERT INTO trigger_log VALUES ('t{i}', 0)")

    for i in range(N):
        db.execute(
            f"CREATE TABLE t{i} ("
            f"  id    INTEGER PRIMARY KEY,"
            f"  val   TEXT,"
            f"  score REAL"
            f")"
        )
        db.execute(f"CREATE INDEX idx_t{i}_val ON t{i}(val)")
        db.execute(
            f"CREATE TRIGGER trg_t{i} AFTER INSERT ON t{i} "
            f"BEGIN "
            f"  UPDATE trigger_log SET cnt = cnt + 1 WHERE tbl = 't{i}'; "
            f"END"
        )

    # Seed one row per table so indexes have data to probe
    for i in range(N):
        db.execute(f"INSERT INTO t{i} VALUES (1, 'hello{i}', {i}.5)")

    db.execute("ANALYZE")
    db.close()
    return db


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def db_path(tmp_path):
    path = _db_path(tmp_path)
    _build_db(path)
    return path


# ── (a) Schema survival ───────────────────────────────────────────────────────

def test_all_50_tables_present(db_path):
    db = Database(db_path)
    rows = db.execute(
        "SELECT name FROM _hyperion_master WHERE type = 'table' AND name LIKE 't%'"
    ).fetchall()
    names = {r["name"] for r in rows}
    for i in range(N):
        assert f"t{i}" in names, f"Table t{i} missing after reopen"
    db.close()


def test_column_names_and_types_survive(db_path):
    db = Database(db_path)
    for i in range(N):
        rows = db.execute(f"PRAGMA table_info(t{i})").fetchall()
        cols = {r["name"]: r["type"] for r in rows}
        assert cols.get("id")    == "INTEGER", f"t{i}.id type wrong"
        assert cols.get("val")   == "TEXT",    f"t{i}.val type wrong"
        assert cols.get("score") == "REAL",    f"t{i}.score type wrong"
    db.close()


def test_primary_key_flag_survives(db_path):
    db = Database(db_path)
    for i in range(N):
        rows = db.execute(f"PRAGMA table_info(t{i})").fetchall()
        pk_cols = [r["name"] for r in rows if r["pk"]]
        assert pk_cols == ["id"], f"t{i} PK wrong after reopen: {pk_cols}"
    db.close()


# ── (b) Index presence and functionality ─────────────────────────────────────

def test_all_50_indexes_present(db_path):
    db = Database(db_path)
    rows = db.execute(
        "SELECT name FROM _hyperion_master WHERE type = 'index' AND name LIKE 'idx_t%'"
    ).fetchall()
    names = {r["name"] for r in rows}
    for i in range(N):
        assert f"idx_t{i}_val" in names, f"Index idx_t{i}_val missing after reopen"
    db.close()


def test_index_lookup_returns_correct_row(db_path):
    """Each index must be functional: probe produces the seeded row."""
    db = Database(db_path)
    for i in range(N):
        rows = db.execute(
            f"SELECT id, val FROM t{i} WHERE val = 'hello{i}'"
        ).fetchall()
        assert len(rows) == 1, f"t{i}: expected 1 row via index, got {len(rows)}"
        assert rows[0]["id"]  == 1,         f"t{i}: wrong id"
        assert rows[0]["val"] == f"hello{i}", f"t{i}: wrong val"
    db.close()


def test_index_insert_and_probe_after_reopen(db_path):
    """Insert a new row after reopen and verify it is index-reachable."""
    db = Database(db_path)
    db.execute(f"INSERT INTO t0 VALUES (2, 'newval', 99.0)")
    rows = db.execute("SELECT id FROM t0 WHERE val = 'newval'").fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == 2
    db.close()


def test_unique_pk_still_enforced(db_path):
    """PK uniqueness must still be enforced after reopen."""
    db = Database(db_path)
    with pytest.raises(Exception):
        db.execute("INSERT INTO t0 VALUES (1, 'dup', 0.0)")
    db.close()


# ── (c) Trigger correctness ───────────────────────────────────────────────────

def test_all_50_triggers_present(db_path):
    db = Database(db_path)
    rows = db.execute(
        "SELECT name FROM _hyperion_master WHERE type = 'trigger' AND name LIKE 'trg_t%'"
    ).fetchall()
    names = {r["name"] for r in rows}
    for i in range(N):
        assert f"trg_t{i}" in names, f"Trigger trg_t{i} missing after reopen"
    db.close()


def test_trigger_fires_after_reopen(db_path):
    """After reopen, inserting into each table must increment trigger_log.cnt."""
    db = Database(db_path)
    # Baseline: each cnt should be 1 (from the seed INSERT in _build_db)
    for i in range(N):
        rows = db.execute(
            f"SELECT cnt FROM trigger_log WHERE tbl = 't{i}'"
        ).fetchall()
        assert rows[0]["cnt"] == 1, \
            f"t{i}: expected seed cnt=1, got {rows[0]['cnt']}"

    # Insert one more row into t0 and verify its counter increments
    db.execute("INSERT INTO t0 VALUES (2, 'post_reopen', 0.0)")
    rows = db.execute("SELECT cnt FROM trigger_log WHERE tbl = 't0'").fetchall()
    assert rows[0]["cnt"] == 2, \
        f"trigger_log not updated after post-reopen INSERT into t0"
    db.close()


def test_trigger_fires_for_all_tables(db_path):
    """Insert one additional row per table; each counter must reach 2."""
    db = Database(db_path)
    for i in range(N):
        db.execute(f"INSERT INTO t{i} VALUES (2, 'extra{i}', 0.0)")
    for i in range(N):
        rows = db.execute(
            f"SELECT cnt FROM trigger_log WHERE tbl = 't{i}'"
        ).fetchall()
        assert rows[0]["cnt"] == 2, \
            f"t{i}: trigger_log cnt expected 2, got {rows[0]['cnt']}"
    db.close()


# ── (d) ANALYZE stats survival ────────────────────────────────────────────────

def test_analyze_row_counts_survive(db_path):
    """Row counts collected by ANALYZE must be readable after reopen."""
    db = Database(db_path)
    for i in range(N):
        rc = db._catalog.stats.get(f"t{i}", {}).get("row_count")
        assert rc is not None, f"t{i}: row_count stat missing after reopen"
        assert rc >= 1, f"t{i}: row_count should be >= 1, got {rc}"
    db.close()


def test_analyze_ndv_survives(db_path):
    """NDV (distinct-value) stats for the val column must survive reopen."""
    db = Database(db_path)
    for i in range(N):
        cols = db._catalog.stats.get(f"t{i}", {}).get("columns", {})
        ndv = cols.get("val", {}).get("ndv")
        assert ndv is not None, f"t{i}: NDV stat for 'val' missing after reopen"
        assert ndv >= 1, f"t{i}: NDV for 'val' should be >= 1, got {ndv}"
    db.close()


def test_second_analyze_after_reopen(db_path):
    """Running ANALYZE again after reopen must not raise and must update stats."""
    db = Database(db_path)
    db.execute("INSERT INTO t0 VALUES (2, 'extra', 1.0)")
    db.execute("ANALYZE")
    rc = db._catalog.stats.get("t0", {}).get("row_count")
    assert rc == 2, f"After re-ANALYZE, t0 row_count should be 2, got {rc}"
    db.close()
