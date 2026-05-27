"""Tests for PEP 249 cursor interface, parameter binding, and context manager."""
import pytest
from hyperion import Database
from hyperion.cursor import Cursor, _bind_params, _sql_literal


# ── _sql_literal ──────────────────────────────────────────────────────────────

class TestSqlLiteral:
    def test_none(self):
        assert _sql_literal(None) == "NULL"

    def test_int(self):
        assert _sql_literal(42) == "42"

    def test_negative_int(self):
        assert _sql_literal(-7) == "-7"

    def test_float(self):
        assert _sql_literal(3.14) == repr(3.14)

    def test_bool_true(self):
        assert _sql_literal(True) == "1"

    def test_bool_false(self):
        assert _sql_literal(False) == "0"

    def test_plain_string(self):
        assert _sql_literal("hello") == "'hello'"

    def test_string_with_single_quote(self):
        assert _sql_literal("it's") == "'it''s'"

    def test_bytes(self):
        assert _sql_literal(b"\xde\xad") == "X'dead'"


# ── _bind_params ──────────────────────────────────────────────────────────────

class TestBindParams:
    def test_positional_int(self):
        result = _bind_params("SELECT * FROM t WHERE id = ?", (5,))
        assert result == "SELECT * FROM t WHERE id = 5"

    def test_positional_string(self):
        result = _bind_params("SELECT * FROM t WHERE name = ?", ("Alice",))
        assert result == "SELECT * FROM t WHERE name = 'Alice'"

    def test_positional_none(self):
        result = _bind_params("WHERE x = ?", (None,))
        assert result == "WHERE x = NULL"

    def test_positional_multiple(self):
        result = _bind_params("INSERT INTO t VALUES (?, ?)", (1, "Bob"))
        assert result == "INSERT INTO t VALUES (1, 'Bob')"

    def test_positional_too_few_raises(self):
        with pytest.raises(ValueError, match="Not enough"):
            _bind_params("WHERE a = ? AND b = ?", (1,))

    def test_positional_too_many_raises(self):
        with pytest.raises(ValueError, match="Too many"):
            _bind_params("WHERE a = ?", (1, 2))

    def test_question_mark_inside_string_not_replaced(self):
        result = _bind_params("WHERE x = '?'", ())
        assert result == "WHERE x = '?'"

    def test_named_colon(self):
        result = _bind_params("WHERE id = :id", {"id": 7})
        assert result == "WHERE id = 7"

    def test_named_dollar(self):
        result = _bind_params("WHERE name = $name", {"name": "Eve"})
        assert result == "WHERE name = 'Eve'"

    def test_named_missing_key_raises(self):
        with pytest.raises(ValueError, match="No value for named parameter"):
            _bind_params("WHERE id = :missing", {})

    def test_named_inside_string_not_replaced(self):
        result = _bind_params("WHERE x = ':name'", {"name": "Eve"})
        assert result == "WHERE x = ':name'"

    def test_no_params(self):
        assert _bind_params("SELECT 1", None) == "SELECT 1"

    def test_escaped_quote_in_string_not_confused(self):
        # The literal 'it''s' should not confuse the scanner
        result = _bind_params("WHERE x = 'it''s' AND y = ?", (99,))
        assert result == "WHERE x = 'it''s' AND y = 99"


# ── Cursor ────────────────────────────────────────────────────────────────────

def _db():
    db = Database(":memory:")
    db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, score REAL)")
    db.execute("INSERT INTO users VALUES (1, 'Alice', 9.5)")
    db.execute("INSERT INTO users VALUES (2, 'Bob', 7.0)")
    db.execute("INSERT INTO users VALUES (3, 'Carol', 8.0)")
    return db


class TestCursorExecute:
    def test_returns_cursor(self):
        db = _db()
        cur = db.cursor()
        result = cur.execute("SELECT * FROM users")
        assert result is cur

    def test_select_fetchall(self):
        db = _db()
        cur = db.execute("SELECT id, name FROM users ORDER BY id")
        rows = cur.fetchall()
        assert len(rows) == 3
        assert rows[0]["name"] == "Alice"
        assert rows[2]["name"] == "Carol"

    def test_select_fetchone(self):
        db = _db()
        cur = db.execute("SELECT id FROM users ORDER BY id")
        row = cur.fetchone()
        assert row["id"] == 1
        row = cur.fetchone()
        assert row["id"] == 2

    def test_fetchone_exhausted_returns_none(self):
        db = _db()
        cur = db.execute("SELECT id FROM users ORDER BY id")
        cur.fetchall()
        assert cur.fetchone() is None

    def test_fetchmany(self):
        db = _db()
        cur = db.execute("SELECT id FROM users ORDER BY id")
        batch = cur.fetchmany(2)
        assert len(batch) == 2
        assert batch[0]["id"] == 1
        assert batch[1]["id"] == 2
        rest = cur.fetchmany(10)
        assert len(rest) == 1

    def test_cursor_iter(self):
        db = _db()
        ids = [row["id"] for row in db.execute("SELECT id FROM users ORDER BY id")]
        assert ids == [1, 2, 3]

    def test_description_set_for_select(self):
        db = _db()
        cur = db.execute("SELECT id, name FROM users LIMIT 1")
        assert cur.description is not None
        col_names = [d[0] for d in cur.description]
        assert "id" in col_names
        assert "name" in col_names

    def test_description_seven_items(self):
        db = _db()
        cur = db.execute("SELECT id FROM users LIMIT 1")
        assert len(cur.description[0]) == 7

    def test_rowcount_minus_one_for_select(self):
        db = _db()
        cur = db.execute("SELECT * FROM users")
        assert cur.rowcount == -1

    def test_rowcount_for_insert(self):
        db = _db()
        cur = db.execute("INSERT INTO users VALUES (4, 'Dave', 6.0)")
        assert cur.rowcount == 1

    def test_rowcount_for_update(self):
        db = _db()
        cur = db.execute("UPDATE users SET score = 10.0 WHERE score > 7.0")
        assert cur.rowcount == 2

    def test_rowcount_for_delete(self):
        db = _db()
        cur = db.execute("DELETE FROM users WHERE id = 1")
        assert cur.rowcount == 1

    def test_description_none_for_dml(self):
        db = _db()
        cur = db.execute("INSERT INTO users VALUES (5, 'Eve', 5.0)")
        assert cur.description is None

    def test_cursor_close_clears_results(self):
        db = _db()
        cur = db.execute("SELECT * FROM users")
        cur.close()
        assert cur.fetchone() is None


class TestCursorWithParams:
    def test_positional_select(self):
        db = _db()
        cur = db.execute("SELECT name FROM users WHERE id = ?", (2,))
        rows = cur.fetchall()
        assert rows[0]["name"] == "Bob"

    def test_positional_insert(self):
        db = _db()
        db.execute("INSERT INTO users VALUES (?, ?, ?)", (10, "Zara", 5.5))
        cur = db.execute("SELECT name FROM users WHERE id = 10")
        assert cur.fetchone()["name"] == "Zara"

    def test_named_colon_select(self):
        db = _db()
        cur = db.execute("SELECT name FROM users WHERE id = :uid", {"uid": 3})
        assert cur.fetchone()["name"] == "Carol"

    def test_named_dollar_select(self):
        db = _db()
        cur = db.execute("SELECT name FROM users WHERE id = $uid", {"uid": 1})
        assert cur.fetchone()["name"] == "Alice"

    def test_string_param_escaped(self):
        db = _db()
        db.execute("INSERT INTO users VALUES (?, ?, ?)", (20, "O'Brien", 7.0))
        cur = db.execute("SELECT name FROM users WHERE id = 20")
        assert cur.fetchone()["name"] == "O'Brien"

    def test_none_param(self):
        db = _db()
        db.execute("INSERT INTO users VALUES (?, ?, ?)", (30, None, 0.0))
        cur = db.execute("SELECT name FROM users WHERE id = 30")
        assert cur.fetchone()["name"] is None


class TestExecuteMany:
    def test_insert_many(self):
        db = _db()
        rows = [(10, "X", 1.0), (11, "Y", 2.0), (12, "Z", 3.0)]
        cur = db.executemany("INSERT INTO users VALUES (?, ?, ?)", rows)
        assert cur.rowcount == 3
        count_cur = db.execute("SELECT COUNT(*) FROM users")
        assert count_cur.fetchone()["COUNT(*)"] == 6

    def test_executemany_empty(self):
        db = _db()
        cur = db.executemany("INSERT INTO users VALUES (?, ?, ?)", [])
        assert cur.rowcount == 0


class TestExecuteScript:
    def test_creates_and_inserts(self):
        db = Database(":memory:")
        db.executescript("""
            CREATE TABLE t (id INTEGER, val TEXT);
            INSERT INTO t VALUES (1, 'a');
            INSERT INTO t VALUES (2, 'b');
        """)
        cur = db.execute("SELECT COUNT(*) FROM t")
        assert cur.fetchone()["COUNT(*)"] == 2

    def test_commits_open_transaction(self):
        db = Database(":memory:")
        db.execute("CREATE TABLE t (id INTEGER)")
        db.begin()
        db.execute("INSERT INTO t VALUES (1)")
        # executescript should commit the pending txn first
        db.executescript("INSERT INTO t VALUES (2);")
        cur = db.execute("SELECT COUNT(*) FROM t")
        assert cur.fetchone()["COUNT(*)"] == 2


# ── Context manager ───────────────────────────────────────────────────────────

class TestContextManager:
    def test_clean_exit_commits(self):
        db = Database(":memory:")
        db.execute("CREATE TABLE t (id INTEGER)")
        with db:
            db.execute("INSERT INTO t VALUES (1)")
            db.execute("INSERT INTO t VALUES (2)")
        # After clean exit, changes should be committed
        cur = db.execute("SELECT COUNT(*) FROM t")
        assert cur.fetchone()["COUNT(*)"] == 2

    def test_exception_rolls_back(self):
        db = Database(":memory:")
        db.execute("CREATE TABLE t (id INTEGER)")
        try:
            with db:
                db.execute("INSERT INTO t VALUES (1)")
                raise RuntimeError("oops")
        except RuntimeError:
            pass
        cur = db.execute("SELECT COUNT(*) FROM t")
        assert cur.fetchone()["COUNT(*)"] == 0

    def test_returns_db(self):
        db = Database(":memory:")
        with db as conn:
            assert conn is db

    def test_nested_usage(self):
        db = Database(":memory:")
        db.execute("CREATE TABLE t (id INTEGER)")
        with db:
            db.execute("INSERT INTO t VALUES (10)")
        with db:
            db.execute("INSERT INTO t VALUES (20)")
        cur = db.execute("SELECT COUNT(*) FROM t")
        assert cur.fetchone()["COUNT(*)"] == 2

    def test_exception_does_not_suppress(self):
        db = Database(":memory:")
        db.execute("CREATE TABLE t (id INTEGER)")
        with pytest.raises(RuntimeError, match="boom"):
            with db:
                raise RuntimeError("boom")
