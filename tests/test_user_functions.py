"""Tests for application-defined scalar and aggregate functions (create_function / create_aggregate)."""
import pytest
from hyperion import Database
from hyperion.executor import execute, _rows_for_stmt
from hyperion.parser import parse


def sql(db, s):
    return execute(parse(s), db)


def rows(db, query):
    return _rows_for_stmt(parse(query), db)


# ── Scalar functions ──────────────────────────────────────────────────────────

class TestScalarFunctions:
    def test_simple_scalar(self):
        db = Database(":memory:")
        db.create_function("double", 1, lambda x: x * 2)
        r = rows(db, "SELECT double(21) AS v")
        assert r[0]["v"] == 42

    def test_scalar_on_column(self):
        db = Database(":memory:")
        db.create_function("square", 1, lambda x: x * x)
        sql(db, "CREATE TABLE t (n INTEGER)")
        sql(db, "INSERT INTO t VALUES (3)")
        sql(db, "INSERT INTO t VALUES (4)")
        r = rows(db, "SELECT square(n) AS sq FROM t ORDER BY sq ASC")
        assert [row["sq"] for row in r] == [9, 16]

    def test_scalar_multi_arg(self):
        db = Database(":memory:")
        db.create_function("add3", 3, lambda a, b, c: a + b + c)
        r = rows(db, "SELECT add3(1, 2, 3) AS v")
        assert r[0]["v"] == 6

    def test_scalar_variadic(self):
        db = Database(":memory:")
        db.create_function("myconcat", -1, lambda *args: "".join(str(a) for a in args))
        r = rows(db, "SELECT myconcat('a', 'b', 'c') AS v")
        assert r[0]["v"] == "abc"

    def test_scalar_returns_none(self):
        db = Database(":memory:")
        db.create_function("always_null", 1, lambda x: None)
        r = rows(db, "SELECT always_null(1) AS v")
        assert r[0]["v"] is None

    def test_scalar_in_where(self):
        db = Database(":memory:")
        db.create_function("is_even", 1, lambda x: 1 if x % 2 == 0 else 0)
        sql(db, "CREATE TABLE t (n INTEGER)")
        for i in range(1, 6):
            sql(db, f"INSERT INTO t VALUES ({i})")
        r = rows(db, "SELECT n FROM t WHERE is_even(n) = 1 ORDER BY n ASC")
        assert [row["n"] for row in r] == [2, 4]

    def test_scalar_wrong_n_args_raises(self):
        db = Database(":memory:")
        db.create_function("inc", 1, lambda x: x + 1)
        with pytest.raises(RuntimeError, match="wrong number of arguments"):
            rows(db, "SELECT inc(1, 2) AS v")

    def test_scalar_string_function(self):
        db = Database(":memory:")
        db.create_function("shout", 1, lambda s: (s or "").upper() + "!")
        r = rows(db, "SELECT shout('hello') AS v")
        assert r[0]["v"] == "HELLO!"

    def test_overwrite_existing_registration(self):
        db = Database(":memory:")
        db.create_function("myfunc", 1, lambda x: x + 1)
        db.create_function("myfunc", 1, lambda x: x * 10)
        r = rows(db, "SELECT myfunc(5) AS v")
        assert r[0]["v"] == 50

    def test_scalar_case_insensitive_name(self):
        db = Database(":memory:")
        db.create_function("MyFunc", 1, lambda x: x + 100)
        r = rows(db, "SELECT myfunc(1) AS v")
        assert r[0]["v"] == 101


# ── Aggregate functions ───────────────────────────────────────────────────────

class SumSquares:
    """Sum of squares aggregate: sum(x^2) over a group."""
    def __init__(self):
        self._total = 0

    def step(self, val):
        if val is not None:
            self._total += val * val

    def finalize(self):
        return self._total


class StringJoin:
    """Concatenate strings with a fixed '|' separator."""
    def __init__(self):
        self._parts = []

    def step(self, val):
        if val is not None:
            self._parts.append(str(val))

    def finalize(self):
        return "|".join(self._parts) if self._parts else None


class TestAggregateFunctions:
    def test_basic_aggregate(self):
        db = Database(":memory:")
        db.create_aggregate("sum_sq", 1, SumSquares)
        sql(db, "CREATE TABLE t (n INTEGER)")
        for v in [1, 2, 3]:
            sql(db, f"INSERT INTO t VALUES ({v})")
        r = rows(db, "SELECT sum_sq(n) AS v FROM t")
        assert r[0]["v"] == 1 + 4 + 9  # 14

    def test_aggregate_with_group_by(self):
        db = Database(":memory:")
        db.create_aggregate("strjoin", 1, StringJoin)
        sql(db, "CREATE TABLE t (grp TEXT, val TEXT)")
        sql(db, "INSERT INTO t VALUES ('a', 'x')")
        sql(db, "INSERT INTO t VALUES ('a', 'y')")
        sql(db, "INSERT INTO t VALUES ('b', 'z')")
        r = rows(db, "SELECT grp, strjoin(val) AS joined FROM t GROUP BY grp ORDER BY grp ASC")
        assert r[0]["grp"] == "a"
        assert r[0]["joined"] == "x|y"
        assert r[1]["grp"] == "b"
        assert r[1]["joined"] == "z"

    def test_aggregate_null_values_skipped(self):
        db = Database(":memory:")
        db.create_aggregate("sum_sq", 1, SumSquares)
        sql(db, "CREATE TABLE t (n INTEGER)")
        sql(db, "INSERT INTO t VALUES (2)")
        sql(db, "INSERT INTO t VALUES (NULL)")
        sql(db, "INSERT INTO t VALUES (3)")
        r = rows(db, "SELECT sum_sq(n) AS v FROM t")
        assert r[0]["v"] == 4 + 9  # 13

    def test_aggregate_empty_group_finalize(self):
        db = Database(":memory:")
        db.create_aggregate("strjoin", 1, StringJoin)
        sql(db, "CREATE TABLE t (n INTEGER)")
        r = rows(db, "SELECT strjoin(n) AS v FROM t")
        assert r[0]["v"] is None  # finalize() on empty group

    def test_aggregate_case_insensitive(self):
        db = Database(":memory:")
        db.create_aggregate("MyAgg", 1, SumSquares)
        sql(db, "CREATE TABLE t (n INTEGER)")
        sql(db, "INSERT INTO t VALUES (5)")
        r = rows(db, "SELECT myagg(n) AS v FROM t")
        assert r[0]["v"] == 25
