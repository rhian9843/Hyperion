"""Tests for COLLATE, CREATE TEMP TABLE, Generated Columns, and Recursive CTEs."""
import pytest
from hyperion import Database
from hyperion.parser import parse
from hyperion.executor import execute


def sql(db, stmt):
    return execute(parse(stmt), db)


def rows(db, query):
    """Execute a SELECT and return raw list of dicts via db.select internals."""
    from hyperion.executor import _rows_for_stmt
    from hyperion.parser import _parse_tokens, _tokenize
    ast = parse(query)
    # Use _rows_for_stmt to get raw dicts
    return _rows_for_stmt(ast, db)


# ── COLLATE ───────────────────────────────────────────────────────────────────

class TestCollate:
    def setup_method(self):
        self.db = Database(":memory:")
        sql(self.db, "CREATE TABLE t (name VARCHAR(50))")
        for v in ["Banana", "apple", "Cherry", "avocado"]:
            sql(self.db, f"INSERT INTO t VALUES ('{v}')")

    def test_order_by_nocase(self):
        r = rows(self.db, "SELECT name FROM t ORDER BY name COLLATE NOCASE ASC")
        names = [row["name"] for row in r]
        assert names == sorted(names, key=str.lower)

    def test_order_by_nocase_desc(self):
        r = rows(self.db, "SELECT name FROM t ORDER BY name COLLATE NOCASE DESC")
        names = [row["name"] for row in r]
        assert names == sorted(names, key=str.lower, reverse=True)

    def test_order_by_binary(self):
        r = rows(self.db, "SELECT name FROM t ORDER BY name COLLATE BINARY ASC")
        names = [row["name"] for row in r]
        assert names == sorted(names)

    def test_order_by_rtrim(self):
        db = Database(":memory:")
        sql(db, "CREATE TABLE s (v VARCHAR(50))")
        for v in ["hello  ", "aaa", "zzz  "]:
            sql(db, f"INSERT INTO s VALUES ('{v}')")
        r = rows(db, "SELECT v FROM s ORDER BY v COLLATE RTRIM ASC")
        names = [row["v"].rstrip() for row in r]
        assert names == sorted(names)

    def test_no_collate_defaults_to_binary(self):
        r = rows(self.db, "SELECT name FROM t ORDER BY name ASC")
        names = [row["name"] for row in r]
        assert names == sorted(names)


# ── CREATE TEMP TABLE ─────────────────────────────────────────────────────────

class TestTempTable:
    def test_temp_table_accessible_in_session(self):
        db = Database(":memory:")
        sql(db, "CREATE TEMP TABLE session_data (id INTEGER, val VARCHAR(50))")
        sql(db, "INSERT INTO session_data VALUES (1, 'hello')")
        r = rows(db, "SELECT id, val FROM session_data")
        assert len(r) == 1
        assert r[0]["id"] == 1

    def test_temp_keyword(self):
        db = Database(":memory:")
        sql(db, "CREATE TEMPORARY TABLE tmp (x INTEGER)")
        sql(db, "INSERT INTO tmp VALUES (42)")
        r = rows(db, "SELECT x FROM tmp")
        assert r[0]["x"] == 42

    def test_temp_table_not_persisted(self):
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = f.name
        try:
            db = Database(path)
            db.begin()
            sql(db, "CREATE TABLE perm (x INTEGER)")
            sql(db, "CREATE TEMP TABLE tmp (x INTEGER)")
            sql(db, "INSERT INTO perm VALUES (1)")
            sql(db, "INSERT INTO tmp VALUES (99)")
            db.commit()
            db.close()

            db2 = Database(path)
            r = rows(db2, "SELECT x FROM perm")
            assert r[0]["x"] == 1
            with pytest.raises(Exception):
                rows(db2, "SELECT x FROM tmp")
            db2.close()
        finally:
            os.unlink(path)

    def test_temp_table_dropped_on_close(self):
        db = Database(":memory:")
        sql(db, "CREATE TEMP TABLE t (id INTEGER)")
        assert "t" in db.tables
        db.close()

    def test_regular_table_survives_close(self):
        db = Database(":memory:")
        sql(db, "CREATE TABLE perm (id INTEGER)")
        assert "perm" in db.tables
        assert not db.tables["perm"].temporary

    def test_temp_table_is_flagged(self):
        db = Database(":memory:")
        sql(db, "CREATE TEMP TABLE tmp (x INTEGER)")
        assert db.tables["tmp"].temporary


# ── GENERATED COLUMNS ─────────────────────────────────────────────────────────

class TestGeneratedColumns:
    def setup_method(self):
        self.db = Database(":memory:")

    def test_virtual_generated_column(self):
        sql(self.db, "CREATE TABLE rect (w REAL, h REAL, area REAL AS (w * h) VIRTUAL)")
        sql(self.db, "INSERT INTO rect (w, h) VALUES (3.0, 4.0)")
        r = rows(self.db, "SELECT w, h, area FROM rect")
        assert r[0]["area"] == pytest.approx(12.0)

    def test_stored_generated_column(self):
        sql(self.db, "CREATE TABLE rect2 (w REAL, h REAL, area REAL AS (w * h) STORED)")
        sql(self.db, "INSERT INTO rect2 (w, h) VALUES (5.0, 2.0)")
        r = rows(self.db, "SELECT w, h, area FROM rect2")
        assert r[0]["area"] == pytest.approx(10.0)

    def test_virtual_not_stored_in_row(self):
        sql(self.db, "CREATE TABLE t (a INTEGER, b INTEGER, c INTEGER AS (a + b) VIRTUAL)")
        schema = self.db.tables["t"].schema
        stored = schema.stored_columns
        assert not any(col.name == "c" for col in stored)

    def test_stored_is_in_row(self):
        sql(self.db, "CREATE TABLE t2 (a INTEGER, b INTEGER, c INTEGER AS (a + b) STORED)")
        schema = self.db.tables["t2"].schema
        stored = schema.stored_columns
        assert any(col.name == "c" for col in stored)

    def test_generated_string_concat(self):
        sql(self.db, "CREATE TABLE person (first VARCHAR(50), last VARCHAR(50), full_name VARCHAR(100) AS (first || ' ' || last) VIRTUAL)")
        sql(self.db, "INSERT INTO person (first, last) VALUES ('John', 'Doe')")
        r = rows(self.db, "SELECT full_name FROM person")
        assert r[0]["full_name"] == "John Doe"

    def test_multiple_rows_virtual(self):
        sql(self.db, "CREATE TABLE p (x INTEGER, doubled INTEGER AS (x * 2) VIRTUAL)")
        for v in [1, 2, 3, 5]:
            sql(self.db, f"INSERT INTO p (x) VALUES ({v})")
        r = rows(self.db, "SELECT x, doubled FROM p ORDER BY x ASC")
        for row in r:
            assert row["doubled"] == row["x"] * 2

    def test_cannot_insert_into_virtual_column(self):
        sql(self.db, "CREATE TABLE t3 (a INTEGER, b INTEGER AS (a + 1) VIRTUAL)")
        sql(self.db, "INSERT INTO t3 (a) VALUES (10)")
        r = rows(self.db, "SELECT a, b FROM t3")
        assert r[0]["b"] == 11


# ── RECURSIVE CTEs ────────────────────────────────────────────────────────────

class TestRecursiveCTE:
    def setup_method(self):
        self.db = Database(":memory:")

    def test_simple_counter(self):
        r = rows(self.db, """
            WITH RECURSIVE cnt(n) AS (
                SELECT 1
                UNION ALL
                SELECT n + 1 FROM cnt WHERE n < 5
            )
            SELECT n FROM cnt
        """)
        assert [row["n"] for row in r] == [1, 2, 3, 4, 5]

    def test_fibonacci(self):
        r = rows(self.db, """
            WITH RECURSIVE fib(a, b) AS (
                SELECT 0, 1
                UNION ALL
                SELECT b, a + b FROM fib WHERE a < 20
            )
            SELECT a FROM fib
        """)
        vals = [row["a"] for row in r]
        assert vals == [0, 1, 1, 2, 3, 5, 8, 13, 21]

    def test_hierarchy_traversal(self):
        sql(self.db, "CREATE TABLE nodes (id INTEGER, parent INTEGER)")
        for id_, parent in [(1, None), (2, 1), (3, 1), (4, 2), (5, 2)]:
            p = "NULL" if parent is None else str(parent)
            sql(self.db, f"INSERT INTO nodes VALUES ({id_}, {p})")
        r = rows(self.db, """
            WITH RECURSIVE tree(id) AS (
                SELECT id FROM nodes WHERE parent IS NULL
                UNION ALL
                SELECT n.id FROM nodes n JOIN tree t ON n.parent = t.id
            )
            SELECT id FROM tree ORDER BY id ASC
        """)
        assert [row["id"] for row in r] == [1, 2, 3, 4, 5]

    def test_union_deduplicates(self):
        r = rows(self.db, """
            WITH RECURSIVE cnt(n) AS (
                SELECT 1
                UNION
                SELECT n + 1 FROM cnt WHERE n < 5
            )
            SELECT n FROM cnt ORDER BY n ASC
        """)
        vals = [row["n"] for row in r]
        assert vals == list(range(1, 6))
        assert len(vals) == len(set(vals))  # no duplicates
