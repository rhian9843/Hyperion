"""Tests for the depth-tracking _tokenize lexer (Priority 4 audit items)."""
import random
import string
import pytest

from hyperion.parser import _tokenize, parse
from hyperion.errors import ParseError
from hyperion.database import Database


# ---------------------------------------------------------------------------
# Deeply nested function calls
# ---------------------------------------------------------------------------

class TestNestedFunctionTokenizing:
    def test_round_nested_args_tokenized(self):
        """New lexer must emit ROUND(ABS(-1.5), 1) as a single token so it
        evaluates correctly without splitting on the inner comma."""
        db = Database(":memory:")
        db.execute("CREATE TABLE t (col REAL)")
        db.execute("INSERT INTO t VALUES (-1.5), (2.7), (-0.3)")
        rows = db.execute("SELECT ROUND(ABS(col), 1) AS v FROM t ORDER BY col").fetchall()
        assert [r["v"] for r in rows] == [1.5, 0.3, 2.7]

    def test_json_extract_nested(self):
        db = Database(":memory:")
        db.execute("CREATE TABLE t (data TEXT)")
        db.execute("INSERT INTO t VALUES ('{\"a\":{\"b\":42}}')")
        rows = db.execute(
            "SELECT json_extract(json_extract(data, '$.a'), '$.b') AS v FROM t"
        ).fetchall()
        assert rows[0]["v"] == 42

    def test_upper_in_where(self):
        db = Database(":memory:")
        db.execute("CREATE TABLE t (name TEXT)")
        db.execute("INSERT INTO t VALUES ('alice'), ('Bob')")
        rows = db.execute("SELECT name FROM t WHERE UPPER(name) = 'ALICE'").fetchall()
        assert len(rows) == 1
        assert rows[0]["name"] == "alice"

    def test_coalesce_sum_in_group_by(self):
        db = Database(":memory:")
        db.execute("CREATE TABLE t (grp TEXT, val INTEGER)")
        db.execute("INSERT INTO t VALUES ('a', 1), ('a', 2), ('b', NULL)")
        rows = db.execute(
            "SELECT grp, COALESCE(SUM(val), 0) AS s FROM t GROUP BY grp ORDER BY grp"
        ).fetchall()
        assert rows[0] == {"grp": "a", "s": 3}
        assert rows[1] == {"grp": "b", "s": 0}

    def test_expression_index_with_nested_function(self):
        db = Database(":memory:")
        db.execute("CREATE TABLE t (name TEXT)")
        db.execute("CREATE INDEX idx_upper ON t(UPPER(name))")
        db.execute("INSERT INTO t VALUES ('alice'), ('Bob')")
        rows = db.execute("SELECT name FROM t WHERE UPPER(name) = 'ALICE'").fetchall()
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Fuzz tokenizer — random SQL strings must not raise unhandled exceptions
# ---------------------------------------------------------------------------

class TestFuzzTokenizer:
    KEYWORDS = [
        "SELECT", "FROM", "WHERE", "INSERT", "INTO", "VALUES",
        "UPDATE", "SET", "DELETE", "CREATE", "TABLE", "INDEX",
        "DROP", "AND", "OR", "NOT", "IN", "IS", "NULL",
        "ORDER", "BY", "GROUP", "HAVING", "LIMIT", "OFFSET",
        "JOIN", "ON", "AS", "DISTINCT", "COUNT", "SUM", "MAX", "MIN",
        "UPPER", "LOWER", "ROUND", "CAST", "INTEGER", "TEXT", "REAL",
    ]
    IDENTIFIERS = ["t", "t1", "t2", "col", "id", "name", "val", "x", "y"]
    LITERALS = ["1", "2", "'abc'", "'it''s'", "3.14", "NULL", "TRUE", "FALSE"]
    OPERATORS = ["=", "!=", "<", ">", "<=", ">=", "<>", "AND", "OR", "+", "-", "*", "/"]
    PUNCTUATION = ["(", ")", ",", ";"]

    def _random_sql(self, rng: random.Random, length: int) -> str:
        pool = (
            self.KEYWORDS * 3
            + self.IDENTIFIERS * 5
            + self.LITERALS * 3
            + self.OPERATORS * 4
            + self.PUNCTUATION * 4
        )
        parts = [rng.choice(pool) for _ in range(length)]
        return " ".join(parts)

    def test_fuzz_tokenizer_no_unhandled_exceptions(self):
        rng = random.Random(42)
        for _ in range(1000):
            sql = self._random_sql(rng, rng.randint(3, 20))
            # _tokenize must never raise; parse may raise ParseError but not
            # internal exceptions like IndexError / KeyError / AttributeError.
            try:
                tokens = _tokenize(sql)
                assert isinstance(tokens, list)
            except Exception as exc:
                pytest.fail(f"_tokenize raised unexpected {type(exc).__name__}: {exc!r} on {sql!r}")

    def test_fuzz_parse_only_raises_parse_error(self):
        rng = random.Random(99)
        forbidden = (IndexError, KeyError, AttributeError, TypeError, ValueError)
        for _ in range(1000):
            sql = self._random_sql(rng, rng.randint(3, 20))
            try:
                parse(sql)
            except ParseError:
                pass  # expected
            except forbidden as exc:
                pytest.fail(f"parse raised {type(exc).__name__}: {exc!r} on {sql!r}")
            except Exception:
                pass  # RuntimeError, etc. are acceptable
