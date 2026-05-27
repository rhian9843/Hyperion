"""Tests for row_factory, set_authorizer, and iterdump."""
import pytest
from hyperion import (
    Database,
    Row, dict_factory, tuple_factory,
    SQLITE_OK, SQLITE_DENY, SQLITE_IGNORE,
    SQLITE_SELECT, SQLITE_INSERT, SQLITE_UPDATE, SQLITE_DELETE,
    SQLITE_CREATE_TABLE,
)


def _db():
    db = Database(":memory:")
    db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, score REAL)")
    db.execute("INSERT INTO users VALUES (1, 'Alice', 9.5)")
    db.execute("INSERT INTO users VALUES (2, 'Bob', 7.0)")
    db.execute("INSERT INTO users VALUES (3, 'Carol', 8.0)")
    return db


# ── Row ───────────────────────────────────────────────────────────────────────

class TestRow:
    def test_key_access(self):
        db = _db()
        db.row_factory = Row
        row = db.execute("SELECT id, name FROM users WHERE id = 1").fetchone()
        assert row["id"] == 1
        assert row["name"] == "Alice"

    def test_index_access(self):
        db = _db()
        db.row_factory = Row
        row = db.execute("SELECT id, name FROM users WHERE id = 2").fetchone()
        assert row[0] == 2
        assert row[1] == "Bob"

    def test_iter(self):
        db = _db()
        db.row_factory = Row
        row = db.execute("SELECT id, name FROM users WHERE id = 1").fetchone()
        vals = list(row)
        assert vals == [1, "Alice"]

    def test_len(self):
        db = _db()
        db.row_factory = Row
        row = db.execute("SELECT id, name FROM users WHERE id = 1").fetchone()
        assert len(row) == 2

    def test_keys(self):
        db = _db()
        db.row_factory = Row
        row = db.execute("SELECT id, name FROM users WHERE id = 1").fetchone()
        assert "id" in row.keys()
        assert "name" in row.keys()

    def test_repr(self):
        db = _db()
        db.row_factory = Row
        row = db.execute("SELECT id FROM users WHERE id = 1").fetchone()
        assert "Row" in repr(row)

    def test_eq_tuple(self):
        db = _db()
        db.row_factory = Row
        row = db.execute("SELECT id, name FROM users WHERE id = 1").fetchone()
        assert row == (1, "Alice")

    def test_missing_key_raises(self):
        db = _db()
        db.row_factory = Row
        row = db.execute("SELECT id FROM users LIMIT 1").fetchone()
        with pytest.raises(KeyError):
            _ = row["nonexistent"]

    def test_fetchall_returns_rows(self):
        db = _db()
        db.row_factory = Row
        rows = db.execute("SELECT id FROM users ORDER BY id").fetchall()
        assert all(isinstance(r, Row) for r in rows)
        assert [r[0] for r in rows] == [1, 2, 3]

    def test_fetchmany_returns_rows(self):
        db = _db()
        db.row_factory = Row
        rows = db.execute("SELECT id FROM users ORDER BY id").fetchmany(2)
        assert len(rows) == 2
        assert isinstance(rows[0], Row)


# ── tuple_factory ─────────────────────────────────────────────────────────────

class TestTupleFactory:
    def test_returns_tuple(self):
        db = _db()
        db.row_factory = tuple_factory
        row = db.execute("SELECT id, name FROM users WHERE id = 1").fetchone()
        assert isinstance(row, tuple)
        assert row == (1, "Alice")

    def test_fetchall_tuples(self):
        db = _db()
        db.row_factory = tuple_factory
        rows = db.execute("SELECT id FROM users ORDER BY id").fetchall()
        assert rows == [(1,), (2,), (3,)]

    def test_iter_tuples(self):
        db = _db()
        db.row_factory = tuple_factory
        ids = [r[0] for r in db.execute("SELECT id FROM users ORDER BY id")]
        assert ids == [1, 2, 3]


# ── dict_factory ──────────────────────────────────────────────────────────────

class TestDictFactory:
    def test_explicit_dict_factory(self):
        db = _db()
        db.row_factory = dict_factory
        row = db.execute("SELECT id, name FROM users WHERE id = 1").fetchone()
        assert isinstance(row, dict)
        assert row["id"] == 1

    def test_none_factory_gives_dict(self):
        db = _db()
        # default is None → dict
        row = db.execute("SELECT id FROM users LIMIT 1").fetchone()
        assert isinstance(row, dict)


# ── row_factory isolation ─────────────────────────────────────────────────────

class TestRowFactoryIsolation:
    def test_cursor_snapshots_factory_at_creation(self):
        db = _db()
        cur = db.cursor()
        # Change factory after cursor created — cursor keeps old factory
        db.row_factory = tuple_factory
        cur.execute("SELECT id FROM users LIMIT 1")
        row = cur.fetchone()
        assert isinstance(row, dict)  # cursor was created before factory was set

    def test_new_cursor_picks_up_changed_factory(self):
        db = _db()
        db.row_factory = tuple_factory
        row = db.execute("SELECT id FROM users LIMIT 1").fetchone()
        assert isinstance(row, tuple)


# ── set_authorizer ────────────────────────────────────────────────────────────

class TestSetAuthorizer:
    def test_allow_all(self):
        db = _db()
        db.set_authorizer(lambda *a: SQLITE_OK)
        rows = db.execute("SELECT * FROM users").fetchall()
        assert len(rows) == 3

    def test_deny_select_raises(self):
        db = _db()
        db.set_authorizer(lambda action, *a: SQLITE_DENY if action == SQLITE_SELECT else SQLITE_OK)
        with pytest.raises(RuntimeError, match="Access denied"):
            db.execute("SELECT * FROM users").fetchall()

    def test_ignore_select_returns_empty(self):
        db = _db()
        db.set_authorizer(lambda action, *a: SQLITE_IGNORE if action == SQLITE_SELECT else SQLITE_OK)
        cur = db.execute("SELECT * FROM users")
        assert cur.fetchall() == []
        assert cur.rowcount == -1

    def test_deny_insert_raises(self):
        db = _db()
        db.set_authorizer(lambda action, *a: SQLITE_DENY if action == SQLITE_INSERT else SQLITE_OK)
        with pytest.raises(RuntimeError, match="Access denied"):
            db.execute("INSERT INTO users VALUES (10, 'X', 1.0)")

    def test_ignore_insert_is_noop(self):
        db = _db()
        db.set_authorizer(lambda action, *a: SQLITE_IGNORE if action == SQLITE_INSERT else SQLITE_OK)
        db.execute("INSERT INTO users VALUES (10, 'X', 1.0)")
        count = db.execute("SELECT COUNT(*) FROM users").fetchone()["COUNT(*)"]
        assert count == 3  # unchanged

    def test_deny_update_raises(self):
        db = _db()
        db.set_authorizer(lambda action, *a: SQLITE_DENY if action == SQLITE_UPDATE else SQLITE_OK)
        with pytest.raises(RuntimeError, match="Access denied"):
            db.execute("UPDATE users SET score = 0 WHERE id = 1")

    def test_deny_delete_raises(self):
        db = _db()
        db.set_authorizer(lambda action, *a: SQLITE_DENY if action == SQLITE_DELETE else SQLITE_OK)
        with pytest.raises(RuntimeError, match="Access denied"):
            db.execute("DELETE FROM users WHERE id = 1")

    def test_deny_create_table_raises(self):
        db = _db()
        db.set_authorizer(lambda action, *a: SQLITE_DENY if action == SQLITE_CREATE_TABLE else SQLITE_OK)
        with pytest.raises(RuntimeError, match="Access denied"):
            db.execute("CREATE TABLE new_t (id INTEGER)")

    def test_table_name_passed_to_callback(self):
        seen = []
        db = _db()
        def auth(action, table, col, dbname, trigger):
            seen.append((action, table))
            return SQLITE_OK
        db.set_authorizer(auth)
        db.execute("INSERT INTO users VALUES (10, 'X', 1.0)")
        assert any(t == "users" for _, t in seen)

    def test_remove_authorizer(self):
        db = _db()
        db.set_authorizer(lambda *a: SQLITE_DENY)
        db.set_authorizer(None)  # remove
        rows = db.execute("SELECT * FROM users").fetchall()
        assert len(rows) == 3

    def test_selective_table_deny(self):
        db = _db()
        db.execute("CREATE TABLE secrets (token TEXT)")
        db.execute("INSERT INTO secrets VALUES ('abc123')")
        db.set_authorizer(
            lambda action, table, *a: SQLITE_DENY if table == "secrets" else SQLITE_OK
        )
        # Can still read users
        rows = db.execute("SELECT * FROM users").fetchall()
        assert len(rows) == 3
        # Cannot read secrets
        with pytest.raises(RuntimeError, match="Access denied"):
            db.execute("SELECT * FROM secrets").fetchall()


# ── iterdump ──────────────────────────────────────────────────────────────────

class TestIterDump:
    def test_yields_begin_commit(self):
        db = _db()
        stmts = list(db.iterdump())
        assert stmts[0] == "BEGIN TRANSACTION;"
        assert stmts[-1] == "COMMIT;"

    def test_contains_create_table(self):
        db = _db()
        dump = "\n".join(db.iterdump())
        assert "CREATE TABLE" in dump
        assert "users" in dump

    def test_contains_insert_rows(self):
        db = _db()
        dump = "\n".join(db.iterdump())
        assert "INSERT INTO" in dump
        assert "Alice" in dump
        assert "Bob" in dump

    def test_roundtrip(self):
        src = _db()
        dump_sql = "\n".join(src.iterdump())

        dst = Database(":memory:")
        dst.executescript(dump_sql)

        rows = dst.execute("SELECT name FROM users ORDER BY id").fetchall()
        assert [r["name"] for r in rows] == ["Alice", "Bob", "Carol"]

    def test_dump_includes_index(self):
        db = _db()
        db.execute("CREATE INDEX idx_name ON users (name)")
        dump = "\n".join(db.iterdump())
        assert "CREATE INDEX" in dump
        assert "idx_name" in dump

    def test_dump_includes_view(self):
        db = _db()
        db.execute("CREATE VIEW top AS SELECT * FROM users WHERE score > 8")
        dump = "\n".join(db.iterdump())
        assert "CREATE VIEW" in dump
        assert "top" in dump

    def test_roundtrip_null_value(self):
        db = Database(":memory:")
        db.execute("CREATE TABLE t (id INTEGER, val TEXT)")
        db.execute("INSERT INTO t VALUES (1, NULL)")
        dst = Database(":memory:")
        dst.executescript("\n".join(db.iterdump()))
        row = dst.execute("SELECT val FROM t WHERE id = 1").fetchone()
        assert row["val"] is None

    def test_roundtrip_special_chars(self):
        db = Database(":memory:")
        db.execute("CREATE TABLE t (id INTEGER, name TEXT)")
        db.execute("INSERT INTO t VALUES (1, 'O''Brien')")
        dst = Database(":memory:")
        dst.executescript("\n".join(db.iterdump()))
        row = dst.execute("SELECT name FROM t WHERE id = 1").fetchone()
        assert row["name"] == "O'Brien"

    def test_empty_table_roundtrip(self):
        db = Database(":memory:")
        db.execute("CREATE TABLE empty (id INTEGER)")
        dst = Database(":memory:")
        dst.executescript("\n".join(db.iterdump()))
        rows = dst.execute("SELECT * FROM empty").fetchall()
        assert rows == []
