"""Tests for CREATE/REFRESH/DROP MATERIALIZED VIEW and SHOW MATERIALIZED VIEWS."""
import pytest
from hyperion import Database
from hyperion.errors import SchemaError, NoSuchTableError


def _fetch(cursor):
    return list(cursor.fetchall())


@pytest.fixture
def db():
    d = Database(":memory:")
    d.execute("CREATE TABLE sales (region TEXT, amount REAL)")
    d.execute("INSERT INTO sales VALUES ('East', 100)")
    d.execute("INSERT INTO sales VALUES ('West', 200)")
    d.execute("INSERT INTO sales VALUES ('East', 150)")
    yield d
    d.close()


# ── CREATE MATERIALIZED VIEW ──────────────────────────────────────────────────

def test_create_mat_view_appears_in_show(db):
    db.execute("CREATE MATERIALIZED VIEW mv AS SELECT region FROM sales")
    rows = _fetch(db.execute("SHOW MATERIALIZED VIEWS"))
    assert any(r["name"] == "mv" for r in rows)


def test_create_mat_view_stores_definition(db):
    db.execute("CREATE MATERIALIZED VIEW mv AS SELECT region, amount FROM sales")
    rows = _fetch(db.execute("SHOW MATERIALIZED VIEWS"))
    mv = next(r for r in rows if r["name"] == "mv")
    assert "region" in mv["definition"]
    assert "amount" in mv["definition"]


def test_create_mat_view_not_yet_refreshed(db):
    db.execute("CREATE MATERIALIZED VIEW mv AS SELECT region FROM sales")
    rows = _fetch(db.execute("SHOW MATERIALIZED VIEWS"))
    mv = next(r for r in rows if r["name"] == "mv")
    assert mv["last_refresh"] is None
    assert mv["row_count"] is None


def test_create_mat_view_if_not_exists(db):
    db.execute("CREATE MATERIALIZED VIEW mv AS SELECT 1 AS n")
    db.execute("CREATE MATERIALIZED VIEW IF NOT EXISTS mv AS SELECT 2 AS n")
    # Second create is a no-op, not an error
    rows = _fetch(db.execute("SHOW MATERIALIZED VIEWS"))
    assert len([r for r in rows if r["name"] == "mv"]) == 1


def test_create_mat_view_duplicate_raises(db):
    db.execute("CREATE MATERIALIZED VIEW mv AS SELECT 1 AS n")
    with pytest.raises(SchemaError):
        db.execute("CREATE MATERIALIZED VIEW mv AS SELECT 2 AS n")


def test_show_mat_views_columns(db):
    db.execute("CREATE MATERIALIZED VIEW mv AS SELECT region FROM sales")
    rows = _fetch(db.execute("SHOW MATERIALIZED VIEWS"))
    assert len(rows) > 0
    row = rows[0]
    assert "name" in row
    assert "definition" in row
    assert "last_refresh" in row
    assert "row_count" in row


def test_show_mat_views_empty(db):
    rows = _fetch(db.execute("SHOW MATERIALIZED VIEWS"))
    assert rows == []


def test_show_mat_views_sorted(db):
    db.execute("CREATE MATERIALIZED VIEW zz AS SELECT 1 AS n")
    db.execute("CREATE MATERIALIZED VIEW aa AS SELECT 2 AS n")
    rows = _fetch(db.execute("SHOW MATERIALIZED VIEWS"))
    names = [r["name"] for r in rows]
    assert names == sorted(names)


# ── REFRESH MATERIALIZED VIEW ─────────────────────────────────────────────────

def test_refresh_populates_data(db):
    db.execute("CREATE MATERIALIZED VIEW mv AS SELECT region, amount FROM sales")
    db.execute("REFRESH MATERIALIZED VIEW mv")
    rows = _fetch(db.execute("SELECT * FROM mv"))
    assert len(rows) == 3


def test_refresh_updates_row_count(db):
    db.execute("CREATE MATERIALIZED VIEW mv AS SELECT region FROM sales")
    db.execute("REFRESH MATERIALIZED VIEW mv")
    meta = _fetch(db.execute("SHOW MATERIALIZED VIEWS"))
    mv = next(r for r in meta if r["name"] == "mv")
    assert mv["row_count"] == 3


def test_refresh_sets_last_refresh_timestamp(db):
    import re
    db.execute("CREATE MATERIALIZED VIEW mv AS SELECT region FROM sales")
    db.execute("REFRESH MATERIALIZED VIEW mv")
    meta = _fetch(db.execute("SHOW MATERIALIZED VIEWS"))
    mv = next(r for r in meta if r["name"] == "mv")
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", mv["last_refresh"])


def test_refresh_reflects_new_data(db):
    db.execute("CREATE MATERIALIZED VIEW mv AS SELECT region, amount FROM sales")
    db.execute("REFRESH MATERIALIZED VIEW mv")
    rows1 = _fetch(db.execute("SELECT * FROM mv"))
    assert len(rows1) == 3

    db.execute("INSERT INTO sales VALUES ('North', 300)")
    db.execute("REFRESH MATERIALIZED VIEW mv")
    rows2 = _fetch(db.execute("SELECT * FROM mv"))
    assert len(rows2) == 4


def test_refresh_stale_data_not_auto_updated(db):
    """Before refresh, the view is not queryable as a table (no backing data)."""
    db.execute("CREATE MATERIALIZED VIEW mv AS SELECT region FROM sales")
    # No REFRESH yet — backing table doesn't exist
    with pytest.raises(Exception):
        db.execute("SELECT * FROM mv").fetchall()


def test_refresh_missing_view_raises(db):
    with pytest.raises((NoSuchTableError, Exception)):
        db.execute("REFRESH MATERIALIZED VIEW nonexistent")


def test_refresh_with_where_clause(db):
    db.execute(
        "CREATE MATERIALIZED VIEW mv_east AS "
        "SELECT region, amount FROM sales WHERE region = 'East'"
    )
    db.execute("REFRESH MATERIALIZED VIEW mv_east")
    rows = _fetch(db.execute("SELECT * FROM mv_east"))
    assert len(rows) == 2
    assert all(r["region"] == "East" for r in rows)


def test_refresh_with_aggregate(db):
    db.execute("CREATE TABLE scores (name TEXT, val INTEGER)")
    db.execute("INSERT INTO scores VALUES ('a', 10)")
    db.execute("INSERT INTO scores VALUES ('a', 20)")
    db.execute("INSERT INTO scores VALUES ('b', 5)")
    d = Database(":memory:")
    d.execute("CREATE TABLE scores (name TEXT, val INTEGER)")
    d.execute("INSERT INTO scores VALUES ('a', 10)")
    d.execute("INSERT INTO scores VALUES ('a', 20)")
    d.execute("INSERT INTO scores VALUES ('b', 5)")
    d.execute(
        "CREATE MATERIALIZED VIEW mv_totals AS "
        "SELECT name, SUM(val) AS total FROM scores GROUP BY name"
    )
    d.execute("REFRESH MATERIALIZED VIEW mv_totals")
    rows = _fetch(d.execute("SELECT * FROM mv_totals ORDER BY name"))
    assert len(rows) == 2
    totals = {r["name"]: int(r["total"]) for r in rows}
    assert totals["a"] == 30
    assert totals["b"] == 5
    d.close()


# ── DROP MATERIALIZED VIEW ────────────────────────────────────────────────────

def test_drop_mat_view_removes_from_show(db):
    db.execute("CREATE MATERIALIZED VIEW mv AS SELECT 1 AS n")
    db.execute("DROP MATERIALIZED VIEW mv")
    rows = _fetch(db.execute("SHOW MATERIALIZED VIEWS"))
    assert not any(r["name"] == "mv" for r in rows)


def test_drop_mat_view_removes_backing_table(db):
    db.execute("CREATE MATERIALIZED VIEW mv AS SELECT region FROM sales")
    db.execute("REFRESH MATERIALIZED VIEW mv")
    db.execute("DROP MATERIALIZED VIEW mv")
    with pytest.raises(Exception):
        db.execute("SELECT * FROM mv").fetchall()


def test_drop_mat_view_if_exists(db):
    db.execute("DROP MATERIALIZED VIEW IF EXISTS nonexistent")  # no error


def test_drop_mat_view_missing_raises(db):
    with pytest.raises((NoSuchTableError, Exception)):
        db.execute("DROP MATERIALIZED VIEW nonexistent")


# ── Multiple views ────────────────────────────────────────────────────────────

def test_multiple_mat_views(db):
    db.execute("CREATE MATERIALIZED VIEW mv1 AS SELECT region FROM sales")
    db.execute("CREATE MATERIALIZED VIEW mv2 AS SELECT amount FROM sales")
    db.execute("REFRESH MATERIALIZED VIEW mv1")
    db.execute("REFRESH MATERIALIZED VIEW mv2")

    rows = _fetch(db.execute("SHOW MATERIALIZED VIEWS"))
    names = {r["name"] for r in rows}
    assert "mv1" in names
    assert "mv2" in names

    r1 = _fetch(db.execute("SELECT * FROM mv1"))
    r2 = _fetch(db.execute("SELECT * FROM mv2"))
    assert len(r1) == 3
    assert len(r2) == 3


# ── Persistence ───────────────────────────────────────────────────────────────

def test_mat_view_persists_across_reopen(tmp_path):
    path = str(tmp_path / "test.hdb")
    db = Database(path)
    db.execute("CREATE TABLE t (x INTEGER)")
    db.execute("INSERT INTO t VALUES (1)")
    db.execute("INSERT INTO t VALUES (2)")
    db.execute("CREATE MATERIALIZED VIEW mv AS SELECT x FROM t")
    db.execute("REFRESH MATERIALIZED VIEW mv")
    db.close()

    db2 = Database(path)
    meta = _fetch(db2.execute("SHOW MATERIALIZED VIEWS"))
    assert len(meta) == 1
    assert meta[0]["name"] == "mv"
    assert meta[0]["row_count"] == 2
    assert meta[0]["last_refresh"] is not None
    data = _fetch(db2.execute("SELECT * FROM mv"))
    assert len(data) == 2
    db2.close()


def test_unrefreshed_mat_view_persists_metadata(tmp_path):
    path = str(tmp_path / "test2.hdb")
    db = Database(path)
    db.execute("CREATE TABLE t (x INTEGER)")
    db.execute("CREATE MATERIALIZED VIEW mv AS SELECT x FROM t")
    db.close()

    db2 = Database(path)
    meta = _fetch(db2.execute("SHOW MATERIALIZED VIEWS"))
    assert len(meta) == 1
    assert meta[0]["last_refresh"] is None
    assert meta[0]["row_count"] is None
    db2.close()
