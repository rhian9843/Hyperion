"""Tests for JSON scalar functions and json_each table-valued function."""
import pytest
from hyperion import Database
from hyperion.executor import execute, _rows_for_stmt
from hyperion.parser import parse


def sql(db, s):
    return execute(parse(s), db)


def rows(db, query):
    return _rows_for_stmt(parse(query), db)


# ── json() / json_valid() / json_type() ───────────────────────────────────────

class TestJsonBasic:
    def test_json_minifies(self):
        db = Database(":memory:")
        r = rows(db, "SELECT json('{ \"a\" : 1 }') AS j")
        assert r[0]["j"] == '{"a":1}'

    def test_json_valid_true(self):
        db = Database(":memory:")
        r = rows(db, "SELECT json_valid('[1,2,3]') AS v")
        assert r[0]["v"] == 1

    def test_json_valid_false(self):
        db = Database(":memory:")
        r = rows(db, "SELECT json_valid('not json') AS v")
        assert r[0]["v"] == 0

    def test_json_type_object(self):
        db = Database(":memory:")
        r = rows(db, "SELECT json_type('{\"a\":1}') AS t")
        assert r[0]["t"] == "object"

    def test_json_type_array(self):
        db = Database(":memory:")
        r = rows(db, "SELECT json_type('[1,2]') AS t")
        assert r[0]["t"] == "array"

    def test_json_type_at_path(self):
        db = Database(":memory:")
        r = rows(db, "SELECT json_type('{\"x\":42}', '$.x') AS t")
        assert r[0]["t"] == "integer"

    def test_json_quote_string(self):
        db = Database(":memory:")
        r = rows(db, "SELECT json_quote('hello') AS q")
        assert r[0]["q"] == '"hello"'

    def test_json_quote_integer(self):
        db = Database(":memory:")
        r = rows(db, "SELECT json_quote(42) AS q")
        assert r[0]["q"] == "42"


# ── json_extract() ────────────────────────────────────────────────────────────

class TestJsonExtract:
    def test_extract_top_key(self):
        db = Database(":memory:")
        r = rows(db, "SELECT json_extract('{\"name\":\"Alice\"}', '$.name') AS n")
        assert r[0]["n"] == "Alice"

    def test_extract_nested(self):
        db = Database(":memory:")
        r = rows(db, "SELECT json_extract('{\"a\":{\"b\":99}}', '$.a.b') AS v")
        assert r[0]["v"] == 99

    def test_extract_array_index(self):
        db = Database(":memory:")
        r = rows(db, "SELECT json_extract('[10,20,30]', '$[1]') AS v")
        assert r[0]["v"] == 20

    def test_extract_missing_key_returns_null(self):
        db = Database(":memory:")
        r = rows(db, "SELECT json_extract('{\"a\":1}', '$.z') AS v")
        assert r[0]["v"] is None

    def test_extract_from_column(self):
        db = Database(":memory:")
        sql(db, "CREATE TABLE t (data TEXT)")
        sql(db, "INSERT INTO t VALUES ('{\"score\":88}')")
        r = rows(db, "SELECT json_extract(data, '$.score') AS s FROM t")
        assert r[0]["s"] == 88

    def test_extract_nested_object_returns_json(self):
        db = Database(":memory:")
        r = rows(db, "SELECT json_extract('{\"a\":{\"b\":1}}', '$.a') AS v")
        assert r[0]["v"] == '{"b":1}'


# ── json_object() / json_array() ─────────────────────────────────────────────

class TestJsonBuild:
    def test_json_object_basic(self):
        db = Database(":memory:")
        r = rows(db, "SELECT json_object('x', 1, 'y', 2) AS j")
        import json
        obj = json.loads(r[0]["j"])
        assert obj == {"x": 1, "y": 2}

    def test_json_array_basic(self):
        db = Database(":memory:")
        r = rows(db, "SELECT json_array(1, 2, 3) AS j")
        import json
        assert json.loads(r[0]["j"]) == [1, 2, 3]

    def test_json_array_mixed_types(self):
        db = Database(":memory:")
        r = rows(db, "SELECT json_array('a', 1, NULL) AS j")
        import json
        arr = json.loads(r[0]["j"])
        assert arr[0] == "a"
        assert arr[1] == 1
        assert arr[2] is None

    def test_json_object_from_columns(self):
        db = Database(":memory:")
        sql(db, "CREATE TABLE t (k TEXT, v INTEGER)")
        sql(db, "INSERT INTO t VALUES ('score', 42)")
        r = rows(db, "SELECT json_object(k, v) AS j FROM t")
        import json
        assert json.loads(r[0]["j"]) == {"score": 42}


# ── json_set / json_insert / json_replace / json_remove ──────────────────────

class TestJsonMutation:
    def test_json_set_overwrites(self):
        db = Database(":memory:")
        r = rows(db, "SELECT json_set('{\"a\":1}', '$.a', 99) AS j")
        import json
        assert json.loads(r[0]["j"]) == {"a": 99}

    def test_json_set_inserts_new(self):
        db = Database(":memory:")
        r = rows(db, "SELECT json_set('{\"a\":1}', '$.b', 2) AS j")
        import json
        obj = json.loads(r[0]["j"])
        assert obj["a"] == 1 and obj["b"] == 2

    def test_json_insert_no_overwrite(self):
        db = Database(":memory:")
        r = rows(db, "SELECT json_insert('{\"a\":1}', '$.a', 99) AS j")
        import json
        assert json.loads(r[0]["j"]) == {"a": 1}  # unchanged

    def test_json_replace_no_insert(self):
        db = Database(":memory:")
        r = rows(db, "SELECT json_replace('{\"a\":1}', '$.b', 2) AS j")
        import json
        assert json.loads(r[0]["j"]) == {"a": 1}  # $.b not inserted

    def test_json_remove_key(self):
        db = Database(":memory:")
        r = rows(db, "SELECT json_remove('{\"a\":1,\"b\":2}', '$.a') AS j")
        import json
        assert json.loads(r[0]["j"]) == {"b": 2}

    def test_json_remove_array_element(self):
        db = Database(":memory:")
        r = rows(db, "SELECT json_remove('[10,20,30]', '$[1]') AS j")
        import json
        assert json.loads(r[0]["j"]) == [10, 30]


# ── json_patch() / json_array_length() ───────────────────────────────────────

class TestJsonUtility:
    def test_json_patch_updates(self):
        db = Database(":memory:")
        r = rows(db, "SELECT json_patch('{\"a\":1,\"b\":2}', '{\"b\":99}') AS j")
        import json
        assert json.loads(r[0]["j"]) == {"a": 1, "b": 99}

    def test_json_patch_removes_null(self):
        db = Database(":memory:")
        r = rows(db, "SELECT json_patch('{\"a\":1,\"b\":2}', '{\"b\":null}') AS j")
        import json
        assert json.loads(r[0]["j"]) == {"a": 1}

    def test_json_array_length(self):
        db = Database(":memory:")
        r = rows(db, "SELECT json_array_length('[1,2,3,4]') AS n")
        assert r[0]["n"] == 4

    def test_json_array_length_at_path(self):
        db = Database(":memory:")
        r = rows(db, "SELECT json_array_length('{\"tags\":[\"a\",\"b\"]}', '$.tags') AS n")
        assert r[0]["n"] == 2

    def test_json_array_length_non_array_null(self):
        db = Database(":memory:")
        r = rows(db, "SELECT json_array_length('{\"a\":1}') AS n")
        assert r[0]["n"] is None


# ── json_each() table-valued function ─────────────────────────────────────────

class TestJsonEach:
    def test_json_each_array_keys_and_values(self):
        db = Database(":memory:")
        r = rows(db, "SELECT key, value FROM json_each('[10,20,30]')")
        assert [row["key"] for row in r]   == [0, 1, 2]
        assert [row["value"] for row in r] == [10, 20, 30]

    def test_json_each_object_keys_and_values(self):
        db = Database(":memory:")
        r = rows(db, "SELECT key, value FROM json_each('{\"a\":1,\"b\":2}')")
        keys = [row["key"] for row in r]
        assert "a" in keys and "b" in keys

    def test_json_each_type_column(self):
        db = Database(":memory:")
        r = rows(db, "SELECT type FROM json_each('[1,\"x\",null]')")
        types = [row["type"] for row in r]
        assert types == ["integer", "text", "null"]

    def test_json_each_join_with_table(self):
        db = Database(":memory:")
        sql(db, "CREATE TABLE t (id INTEGER, tags TEXT)")
        sql(db, "INSERT INTO t VALUES (1, '[\"python\",\"sql\"]')")
        sql(db, "INSERT INTO t VALUES (2, '[\"rust\"]')")
        r = rows(db, "SELECT t.id, j.value FROM t JOIN json_each(t.tags) j")
        values = sorted((row["t.id"], row["j.value"]) for row in r)
        assert values == [(1, "python"), (1, "sql"), (2, "rust")]

    def test_json_each_where_filter(self):
        db = Database(":memory:")
        r = rows(db, "SELECT value FROM json_each('[1,2,3,4,5]') WHERE value > 3")
        vals = sorted(row["value"] for row in r)
        assert vals == [4, 5]

    def test_json_each_fullkey_array(self):
        db = Database(":memory:")
        r = rows(db, "SELECT fullkey FROM json_each('[10,20]')")
        assert r[0]["fullkey"] == "$[0]"
        assert r[1]["fullkey"] == "$[1]"

    def test_json_each_empty_array(self):
        db = Database(":memory:")
        r = rows(db, "SELECT key FROM json_each('[]')")
        assert r == []
