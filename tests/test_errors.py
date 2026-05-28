"""Tests for structured error types — agents can distinguish error kinds."""
import pytest
from hyperion import Database
from hyperion.errors import (
    HyperionError,
    ParseError,
    SchemaError, NoSuchTableError, NoSuchColumnError, NoSuchIndexError,
    TableExistsError, ColumnExistsError, IndexExistsError,
    ConstraintError, UniqueConstraintError, NotNullConstraintError,
    CheckConstraintError, ForeignKeyConstraintError,
    DataError, TransactionError, AuthorizationError,
)


def db():
    d = Database(":memory:")
    d.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    d.execute("INSERT INTO t VALUES (1, 'alice')")
    return d


# ── Hierarchy ─────────────────────────────────────────────────────────────────

def test_all_errors_inherit_hyperion_error():
    for cls in (ParseError, SchemaError, NoSuchTableError, NoSuchColumnError,
                NoSuchIndexError, TableExistsError, ColumnExistsError,
                IndexExistsError, ConstraintError, UniqueConstraintError,
                NotNullConstraintError, CheckConstraintError,
                ForeignKeyConstraintError, DataError, TransactionError,
                AuthorizationError):
        assert issubclass(cls, HyperionError), f"{cls} not subclass of HyperionError"


def test_parse_error_also_inherits_value_error():
    assert issubclass(ParseError, ValueError)


def test_constraint_subclasses_inherit_constraint_error():
    for cls in (UniqueConstraintError, NotNullConstraintError,
                CheckConstraintError, ForeignKeyConstraintError):
        assert issubclass(cls, ConstraintError)


def test_schema_subclasses_inherit_schema_error():
    for cls in (NoSuchTableError, NoSuchColumnError, NoSuchIndexError,
                TableExistsError, ColumnExistsError, IndexExistsError):
        assert issubclass(cls, SchemaError)


# ── ParseError ────────────────────────────────────────────────────────────────

def test_parse_error_on_bad_sql():
    d = db()
    with pytest.raises(ParseError):
        d.execute("CREATE TABLE x (id NOTATYPE)")


# ── NoSuchTableError ──────────────────────────────────────────────────────────

def test_no_such_table_on_select():
    d = db()
    with pytest.raises(NoSuchTableError):
        d.execute("SELECT * FROM nonexistent").fetchall()


def test_no_such_table_on_insert():
    d = db()
    with pytest.raises(NoSuchTableError):
        d.execute("INSERT INTO ghost VALUES (1)")


def test_no_such_table_on_drop():
    d = db()
    with pytest.raises(NoSuchTableError):
        d.execute("DROP TABLE ghost")


def test_no_such_table_is_schema_error():
    d = db()
    with pytest.raises(SchemaError):
        d.execute("SELECT * FROM nonexistent").fetchall()


# ── TableExistsError ──────────────────────────────────────────────────────────

def test_table_exists_error_on_create():
    d = db()
    with pytest.raises(TableExistsError):
        d.execute("CREATE TABLE t (x INTEGER)")


# ── NoSuchColumnError ─────────────────────────────────────────────────────────

def test_no_such_column_in_where():
    d = db()
    with pytest.raises(NoSuchColumnError):
        d.execute("SELECT * FROM t WHERE ghost = 1").fetchall()


def test_no_such_column_is_schema_error():
    d = db()
    with pytest.raises(SchemaError):
        d.execute("SELECT * FROM t WHERE ghost = 1").fetchall()


# ── IndexExistsError / NoSuchIndexError ───────────────────────────────────────

def test_index_exists_error():
    d = db()
    d.execute("CREATE INDEX idx ON t(name)")
    with pytest.raises(IndexExistsError):
        d.execute("CREATE INDEX idx ON t(name)")


def test_no_such_index_error():
    d = db()
    with pytest.raises(NoSuchIndexError):
        d.execute("DROP INDEX ghost_idx")


# ── UniqueConstraintError ─────────────────────────────────────────────────────

def test_unique_constraint_on_primary_key():
    d = db()
    with pytest.raises(UniqueConstraintError):
        d.execute("INSERT INTO t VALUES (1, 'bob')")


def test_unique_constraint_is_constraint_error():
    d = db()
    with pytest.raises(ConstraintError):
        d.execute("INSERT INTO t VALUES (1, 'bob')")


# ── NotNullConstraintError ────────────────────────────────────────────────────

def test_not_null_constraint_error():
    d = db()
    with pytest.raises(NotNullConstraintError):
        d.execute("INSERT INTO t VALUES (2, NULL)")


# ── CheckConstraintError ──────────────────────────────────────────────────────

def test_check_constraint_error():
    d = Database(":memory:")
    d.execute("CREATE TABLE pos (n INTEGER CHECK (n > 0))")
    with pytest.raises(CheckConstraintError):
        d.execute("INSERT INTO pos VALUES (-1)")


# ── ForeignKeyConstraintError ─────────────────────────────────────────────────

def test_foreign_key_constraint_error():
    d = Database(":memory:")
    d.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
    d.execute("CREATE TABLE child (id INTEGER, pid INTEGER REFERENCES parent(id))")
    with pytest.raises(ForeignKeyConstraintError):
        d.execute("INSERT INTO child VALUES (1, 99)")  # 99 not in parent


# ── DataError ─────────────────────────────────────────────────────────────────

def test_data_error_on_integer_overflow():
    d = Database(":memory:")
    d.execute("CREATE TABLE t (n INTEGER)")
    with pytest.raises(DataError):
        d.execute("INSERT INTO t VALUES (99999999999999999999999)")


def test_data_error_on_varchar_overflow():
    d = Database(":memory:")
    d.execute("CREATE TABLE t (s VARCHAR(5))")
    with pytest.raises(DataError):
        d.execute("INSERT INTO t VALUES ('toolongstring')")


# ── TransactionError ──────────────────────────────────────────────────────────

def test_transaction_error_on_double_begin():
    d = db()
    d.begin()
    with pytest.raises(TransactionError):
        d.begin()
    d.rollback()


def test_transaction_error_on_commit_without_begin():
    d = db()
    with pytest.raises(TransactionError):
        d.commit()


# ── AuthorizationError ────────────────────────────────────────────────────────

def test_authorization_error_on_deny():
    from hyperion import SQLITE_DENY
    d = db()
    d.set_authorizer(lambda *a: SQLITE_DENY)
    with pytest.raises(AuthorizationError):
        d.execute("INSERT INTO t VALUES (99, 'x')")


# ── Catch-all with HyperionError ─────────────────────────────────────────────

def test_hyperion_error_catches_all():
    d = db()
    caught = []
    for sql in [
        "SELECT * FROM ghost",
        "INSERT INTO t VALUES (1, 'dup')",
        "INSERT INTO t VALUES (2, NULL)",
    ]:
        try:
            d.execute(sql).fetchall()
        except HyperionError as e:
            caught.append(type(e).__name__)
    assert len(caught) == 3
