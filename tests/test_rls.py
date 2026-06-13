"""Tests for Row-Level Security: ENABLE/DISABLE RLS, CREATE/DROP POLICY, CURRENT_USER_ID()."""
from __future__ import annotations

import pytest
from hyperion.database import Database
from hyperion.errors import SchemaError


def _db():
    db = Database(":memory:")
    db.begin()
    db.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, owner_id INTEGER, body TEXT)")
    db.execute("INSERT INTO documents VALUES (1, 10, 'doc A')")
    db.execute("INSERT INTO documents VALUES (2, 20, 'doc B')")
    db.execute("INSERT INTO documents VALUES (3, 10, 'doc C')")
    db.commit()
    return db


# ── ENABLE / DISABLE RLS ──────────────────────────────────────────────────────

def test_enable_rls_sql():
    db = _db()
    db.begin()
    db.execute("ALTER TABLE documents ENABLE ROW LEVEL SECURITY")
    db.commit()
    assert db._catalog.tables["documents"].rls_enabled
    db.close()


def test_disable_rls_sql():
    db = _db()
    db.begin()
    db.execute("ALTER TABLE documents ENABLE ROW LEVEL SECURITY")
    db.commit()
    db.begin()
    db.execute("ALTER TABLE documents DISABLE ROW LEVEL SECURITY")
    db.commit()
    assert not db._catalog.tables["documents"].rls_enabled
    db.close()


def test_rls_persists_across_reopen(tmp_path):
    db = Database(tmp_path / "rls.hdb")
    db.begin()
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, uid INTEGER, v TEXT)")
    db.execute("INSERT INTO t VALUES (1, 1, 'a')")
    db.commit()
    db.begin()
    db.execute("ALTER TABLE t ENABLE ROW LEVEL SECURITY")
    db.commit()
    db.close()

    db2 = Database(tmp_path / "rls.hdb")
    assert db2._catalog.tables["t"].rls_enabled
    db2.close()


# ── CREATE / DROP POLICY ──────────────────────────────────────────────────────

def test_create_policy_sql():
    db = _db()
    db.begin()
    db.execute("CREATE POLICY owner_only ON documents USING (owner_id = CURRENT_USER_ID())")
    db.commit()
    assert "owner_only" in db._catalog.policies
    assert db._catalog.policies["owner_only"].table == "documents"
    db.close()


def test_drop_policy_sql():
    db = _db()
    db.begin()
    db.execute("CREATE POLICY p1 ON documents USING (owner_id = 10)")
    db.commit()
    db.begin()
    db.execute("DROP POLICY p1 ON documents")
    db.commit()
    assert "p1" not in db._catalog.policies
    db.close()


def test_drop_policy_if_exists_no_error():
    db = _db()
    db.begin()
    db.execute("DROP POLICY IF EXISTS nonexistent ON documents")
    db.commit()
    db.close()


def test_create_policy_duplicate_raises():
    db = _db()
    db.begin()
    db.execute("CREATE POLICY p1 ON documents USING (owner_id = 10)")
    db.commit()
    with pytest.raises((SchemaError, RuntimeError)):
        db.begin()
        db.execute("CREATE POLICY p1 ON documents USING (owner_id = 20)")
        db.commit()
    db.close()


def test_policy_persists_across_reopen(tmp_path):
    db = Database(tmp_path / "rls.hdb")
    db.begin()
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, uid INTEGER)")
    db.execute("INSERT INTO t VALUES (1, 1)")
    db.commit()
    db.begin()
    db.execute("CREATE POLICY pol1 ON t USING (uid = 1)")
    db.commit()
    db.close()

    db2 = Database(tmp_path / "rls.hdb")
    assert "pol1" in db2._catalog.policies
    assert db2._catalog.policies["pol1"].using_expr == "uid = 1"
    db2.close()


# ── CURRENT_USER_ID() ─────────────────────────────────────────────────────────

def test_current_user_id_scalar():
    db = Database(":memory:")
    db.set_user(42)
    rows = list(db.execute("SELECT CURRENT_USER_ID()").fetchall())
    assert len(rows) == 1
    assert list(rows[0].values())[0] == 42
    db.close()


def test_current_user_id_null_when_not_set():
    db = Database(":memory:")
    rows = list(db.execute("SELECT CURRENT_USER_ID()").fetchall())
    assert list(rows[0].values())[0] is None
    db.close()


# ── RLS SELECT enforcement ────────────────────────────────────────────────────

def test_rls_filters_select():
    db = _db()
    db.begin()
    db.execute("ALTER TABLE documents ENABLE ROW LEVEL SECURITY")
    db.execute("CREATE POLICY owner_filter ON documents USING (owner_id = CURRENT_USER_ID())")
    db.commit()

    db.set_user(10)
    rows = list(db.execute("SELECT * FROM documents").fetchall())
    assert len(rows) == 2
    assert all(r["owner_id"] == 10 for r in rows)

    db.set_user(20)
    rows = list(db.execute("SELECT * FROM documents").fetchall())
    assert len(rows) == 1
    assert rows[0]["owner_id"] == 20
    db.close()


def test_rls_no_policies_denies_all():
    db = _db()
    db.begin()
    db.execute("ALTER TABLE documents ENABLE ROW LEVEL SECURITY")
    db.commit()
    db.set_user(10)
    rows = list(db.execute("SELECT * FROM documents").fetchall())
    assert rows == []
    db.close()


def test_superuser_bypasses_rls():
    db = _db()
    db.begin()
    db.execute("ALTER TABLE documents ENABLE ROW LEVEL SECURITY")
    db.execute("CREATE POLICY owner_filter ON documents USING (owner_id = CURRENT_USER_ID())")
    db.commit()

    db.set_user(10)
    db.set_superuser(True)
    rows = list(db.execute("SELECT * FROM documents").fetchall())
    assert len(rows) == 3
    db.close()


def test_rls_disabled_shows_all():
    db = _db()
    db.set_user(10)
    rows = list(db.execute("SELECT * FROM documents").fetchall())
    assert len(rows) == 3
    db.close()


def test_multiple_policies_or_combined():
    db = _db()
    db.begin()
    db.execute("ALTER TABLE documents ENABLE ROW LEVEL SECURITY")
    db.execute("CREATE POLICY pol_10 ON documents USING (owner_id = 10)")
    db.execute("CREATE POLICY pol_20 ON documents USING (owner_id = 20)")
    db.commit()

    rows = list(db.execute("SELECT * FROM documents").fetchall())
    assert len(rows) == 3  # all pass because OR-combined
    db.close()


# ── RLS UPDATE/DELETE enforcement ─────────────────────────────────────────────

def test_rls_filters_update():
    db = _db()
    db.begin()
    db.execute("ALTER TABLE documents ENABLE ROW LEVEL SECURITY")
    db.execute("CREATE POLICY owner_filter ON documents USING (owner_id = CURRENT_USER_ID())")
    db.commit()

    db.set_user(10)
    db.begin()
    db.execute("UPDATE documents SET body = 'updated'")
    db.commit()

    # Owner 10's docs updated; owner 20's doc untouched
    db.set_superuser(True)
    rows = {r["id"]: r for r in db.execute("SELECT * FROM documents").fetchall()}
    assert rows[1]["body"] == "updated"
    assert rows[2]["body"] == "doc B"   # owner 20, not touched
    assert rows[3]["body"] == "updated"
    db.close()


def test_rls_filters_delete():
    db = _db()
    db.begin()
    db.execute("ALTER TABLE documents ENABLE ROW LEVEL SECURITY")
    db.execute("CREATE POLICY owner_filter ON documents USING (owner_id = CURRENT_USER_ID())")
    db.commit()

    db.set_user(10)
    db.begin()
    db.execute("DELETE FROM documents")
    db.commit()

    # Only owner 10's rows deleted
    db.set_superuser(True)
    rows = list(db.execute("SELECT * FROM documents").fetchall())
    assert len(rows) == 1
    assert rows[0]["owner_id"] == 20
    db.close()


# ── Savepoint correctness ─────────────────────────────────────────────────────

def test_rls_rolls_back_with_savepoint():
    db = _db()
    db.begin()
    db.execute("SAVEPOINT sp1")
    db.execute("ALTER TABLE documents ENABLE ROW LEVEL SECURITY")
    db.execute("CREATE POLICY p ON documents USING (owner_id = 10)")
    db.execute("ROLLBACK TO SAVEPOINT sp1")
    db.commit()

    assert not db._catalog.tables["documents"].rls_enabled
    assert "p" not in db._catalog.policies
    db.close()
