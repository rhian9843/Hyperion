"""Tests for ALTER TABLE … ALTER COLUMN col TYPE new_type."""
import pytest
from hyperion import Database
from hyperion.errors import (
    NoSuchColumnError, NoSuchTableError, DataError,
)


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


# ── Basic type conversions ────────────────────────────────────────────────────

class TestBasicConversions:
    def test_text_to_integer(self, db):
        db.execute("CREATE TABLE t (id INTEGER, v TEXT)")
        db.execute("INSERT INTO t VALUES (1, '10')")
        db.execute("INSERT INTO t VALUES (2, '20')")
        db.execute("ALTER TABLE t ALTER COLUMN v TYPE INTEGER")
        rows = db.execute("SELECT v FROM t ORDER BY id").fetchall()
        assert [r["v"] for r in rows] == [10, 20]
        assert all(isinstance(r["v"], int) for r in rows)

    def test_text_to_real(self, db):
        db.execute("CREATE TABLE t (id INTEGER, v TEXT)")
        db.execute("INSERT INTO t VALUES (1, '1.5')")
        db.execute("INSERT INTO t VALUES (2, '2.5')")
        db.execute("ALTER TABLE t ALTER COLUMN v TYPE REAL")
        rows = db.execute("SELECT v FROM t ORDER BY id").fetchall()
        assert [r["v"] for r in rows] == [1.5, 2.5]

    def test_integer_to_text(self, db):
        db.execute("CREATE TABLE t (id INTEGER, n INTEGER)")
        db.execute("INSERT INTO t VALUES (1, 42)")
        db.execute("ALTER TABLE t ALTER COLUMN n TYPE TEXT")
        row = db.execute("SELECT n FROM t").fetchone()
        assert row["n"] == "42"
        assert isinstance(row["n"], str)

    def test_real_to_integer_truncates(self, db):
        db.execute("CREATE TABLE t (id INTEGER, v REAL)")
        db.execute("INSERT INTO t VALUES (1, 7.0)")
        db.execute("ALTER TABLE t ALTER COLUMN v TYPE INTEGER")
        row = db.execute("SELECT v FROM t").fetchone()
        assert row["v"] == 7
        assert isinstance(row["v"], int)

    def test_integer_to_real(self, db):
        db.execute("CREATE TABLE t (id INTEGER, v INTEGER)")
        db.execute("INSERT INTO t VALUES (1, 5)")
        db.execute("ALTER TABLE t ALTER COLUMN v TYPE REAL")
        row = db.execute("SELECT v FROM t").fetchone()
        assert row["v"] == 5.0
        assert isinstance(row["v"], float)

    def test_integer_to_blob(self, db):
        db.execute("CREATE TABLE t (id INTEGER, v TEXT)")
        db.execute("INSERT INTO t VALUES (1, 'hello')")
        db.execute("ALTER TABLE t ALTER COLUMN v TYPE BLOB")
        row = db.execute("SELECT v FROM t").fetchone()
        assert row["v"] == b"hello"

    def test_null_values_preserved(self, db):
        db.execute("CREATE TABLE t (id INTEGER, v TEXT)")
        db.execute("INSERT INTO t VALUES (1, NULL)")
        db.execute("ALTER TABLE t ALTER COLUMN v TYPE INTEGER")
        row = db.execute("SELECT v FROM t").fetchone()
        assert row["v"] is None


# ── Schema is updated ─────────────────────────────────────────────────────────

class TestSchemaUpdate:
    def test_new_type_reflected_in_schema(self, db):
        db.execute("CREATE TABLE t (id INTEGER, v TEXT)")
        db.execute("INSERT INTO t VALUES (1, '99')")
        db.execute("ALTER TABLE t ALTER COLUMN v TYPE INTEGER")
        # Schema update is visible: retrieved value has the new Python type
        row = db.execute("SELECT v FROM t").fetchone()
        assert isinstance(row["v"], int)
        # And the catalog column type is updated
        col = next(c for c in db._meta("t").schema.columns if c.name == "v")
        assert col.type == "INTEGER"

    def test_subsequent_inserts_use_new_type(self, db):
        db.execute("CREATE TABLE t (id INTEGER, v TEXT)")
        db.execute("ALTER TABLE t ALTER COLUMN v TYPE INTEGER")
        db.execute("INSERT INTO t VALUES (2, 99)")
        row = db.execute("SELECT v FROM t WHERE id = 2").fetchone()
        assert row["v"] == 99
        assert isinstance(row["v"], int)

    def test_other_columns_unchanged(self, db):
        db.execute("CREATE TABLE t (id INTEGER, a TEXT, b TEXT)")
        db.execute("INSERT INTO t VALUES (1, '42', 'world')")
        db.execute("ALTER TABLE t ALTER COLUMN a TYPE INTEGER")
        row = db.execute("SELECT id, a, b FROM t").fetchone()
        assert row["id"] == 1
        assert row["a"] == 42
        assert row["b"] == "world"


# ── Index rebuild ─────────────────────────────────────────────────────────────

class TestIndexRebuild:
    def test_index_on_altered_column_still_works(self, db):
        db.execute("CREATE TABLE t (id INTEGER, v TEXT)")
        db.execute("CREATE INDEX idx ON t(v)")
        for i in range(5):
            db.execute(f"INSERT INTO t VALUES ({i}, '{i * 10}')")
        db.execute("ALTER TABLE t ALTER COLUMN v TYPE INTEGER")
        # Index scan should still find the right rows
        rows = db.execute("SELECT id FROM t WHERE v = 30").fetchall()
        assert len(rows) == 1
        assert rows[0]["id"] == 3

    def test_unique_column_constraint_still_enforced(self, db):
        from hyperion.errors import UniqueConstraintError
        db.execute("CREATE TABLE t (id INTEGER, v TEXT UNIQUE)")
        db.execute("INSERT INTO t VALUES (1, '10')")
        db.execute("ALTER TABLE t ALTER COLUMN v TYPE INTEGER")
        with pytest.raises(UniqueConstraintError):
            db.execute("INSERT INTO t VALUES (2, 10)")


# ── Error cases ───────────────────────────────────────────────────────────────

class TestErrors:
    def test_unconvertible_value_raises_data_error(self, db):
        db.execute("CREATE TABLE t (id INTEGER, v TEXT)")
        db.execute("INSERT INTO t VALUES (1, 'not_a_number')")
        with pytest.raises(DataError):
            db.execute("ALTER TABLE t ALTER COLUMN v TYPE INTEGER")

    def test_no_such_column_raises(self, db):
        db.execute("CREATE TABLE t (id INTEGER)")
        with pytest.raises(NoSuchColumnError):
            db.execute("ALTER TABLE t ALTER COLUMN nonexistent TYPE TEXT")

    def test_no_such_table_raises(self, db):
        with pytest.raises(NoSuchTableError):
            db.execute("ALTER TABLE ghost ALTER COLUMN v TYPE INTEGER")

    def test_same_type_is_noop(self, db):
        db.execute("CREATE TABLE t (id INTEGER, v INTEGER)")
        db.execute("INSERT INTO t VALUES (1, 42)")
        db.execute("ALTER TABLE t ALTER COLUMN v TYPE INTEGER")
        row = db.execute("SELECT v FROM t").fetchone()
        assert row["v"] == 42
