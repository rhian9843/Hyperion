"""Tests for _hyperion_schema_meta virtual table and Database.set/get/delete_meta API."""
import pytest
from hyperion import Database


def db():
    d = Database(":memory:")
    d.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)")
    d.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER)")
    return d


# ── Python API ────────────────────────────────────────────────────────────────

class TestPythonAPI:
    def test_set_and_get_meta(self):
        d = db()
        d.set_meta("table", "users", "description", "User account records")
        assert d.get_meta("table", "users", "description") == "User account records"

    def test_get_meta_all_keys(self):
        d = db()
        d.set_meta("table", "users", "description", "User accounts")
        d.set_meta("table", "users", "embedding_model", "text-embedding-3-small")
        tags = d.get_meta("table", "users")
        assert tags == {
            "description": "User accounts",
            "embedding_model": "text-embedding-3-small",
        }

    def test_get_meta_missing_key_returns_none(self):
        d = db()
        assert d.get_meta("table", "users", "nonexistent") is None

    def test_get_meta_missing_object_returns_empty_dict(self):
        d = db()
        assert d.get_meta("table", "ghost") == {}

    def test_set_meta_overwrites_existing_value(self):
        d = db()
        d.set_meta("table", "users", "description", "v1")
        d.set_meta("table", "users", "description", "v2")
        assert d.get_meta("table", "users", "description") == "v2"

    def test_delete_meta_specific_key(self):
        d = db()
        d.set_meta("table", "users", "description", "Users")
        d.set_meta("table", "users", "other", "keep")
        removed = d.delete_meta("table", "users", "description")
        assert removed == 1
        assert d.get_meta("table", "users", "description") is None
        assert d.get_meta("table", "users", "other") == "keep"

    def test_delete_meta_all_keys_for_object(self):
        d = db()
        d.set_meta("table", "users", "a", "1")
        d.set_meta("table", "users", "b", "2")
        removed = d.delete_meta("table", "users")
        assert removed == 2
        assert d.get_meta("table", "users") == {}

    def test_delete_meta_nonexistent_returns_zero(self):
        d = db()
        assert d.delete_meta("table", "ghost", "key") == 0

    def test_column_meta(self):
        d = db()
        d.set_meta("column", "users.email", "description", "Primary email address")
        d.set_meta("column", "users.id", "description", "Auto-increment PK")
        assert d.get_meta("column", "users.email", "description") == "Primary email address"
        assert d.get_meta("column", "users.id", "description") == "Auto-increment PK"

    def test_arbitrary_object_types(self):
        d = db()
        d.set_meta("index", "idx_users_email", "description", "Email lookup index")
        d.set_meta("view", "active_users", "description", "Filter: active = 1")
        assert d.get_meta("index", "idx_users_email", "description") == "Email lookup index"
        assert d.get_meta("view", "active_users", "description") == "Filter: active = 1"


# ── SELECT from _hyperion_schema_meta ─────────────────────────────────────────

class TestSelectVirtualTable:
    def test_empty_table_returns_no_rows(self):
        d = db()
        rows = d.execute("SELECT * FROM _hyperion_schema_meta").fetchall()
        assert rows == []

    def test_rows_present_after_set_meta(self):
        d = db()
        d.set_meta("table", "users", "description", "User accounts")
        rows = d.execute("SELECT * FROM _hyperion_schema_meta").fetchall()
        assert len(rows) == 1
        assert rows[0]["object_type"] == "table"
        assert rows[0]["object_name"] == "users"
        assert rows[0]["key"] == "description"
        assert rows[0]["value"] == "User accounts"

    def test_columns_are_object_type_object_name_key_value(self):
        d = db()
        d.set_meta("table", "users", "x", "y")
        cur = d.execute("SELECT * FROM _hyperion_schema_meta")
        col_names = [c[0] for c in cur.description]
        assert col_names == ["object_type", "object_name", "key", "value"]

    def test_multiple_tags_all_appear(self):
        d = db()
        d.set_meta("table", "users", "description", "Users")
        d.set_meta("column", "users.email", "description", "Email")
        d.set_meta("table", "orders", "tenant", "acme")
        rows = d.execute(
            "SELECT object_type, object_name, key, value "
            "FROM _hyperion_schema_meta ORDER BY object_type, object_name, key"
        ).fetchall()
        assert len(rows) == 3

    def test_where_filter_by_object_type(self):
        d = db()
        d.set_meta("table", "users", "description", "Users")
        d.set_meta("column", "users.email", "description", "Email")
        rows = d.execute(
            "SELECT * FROM _hyperion_schema_meta WHERE object_type = 'column'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["object_name"] == "users.email"

    def test_where_filter_by_object_name(self):
        d = db()
        d.set_meta("table", "users", "description", "Users")
        d.set_meta("table", "orders", "description", "Orders")
        rows = d.execute(
            "SELECT value FROM _hyperion_schema_meta "
            "WHERE object_type = 'table' AND object_name = 'orders'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["value"] == "Orders"

    def test_where_filter_by_key(self):
        d = db()
        d.set_meta("table", "users", "description", "Users")
        d.set_meta("table", "users", "embedding_model", "model-x")
        d.set_meta("table", "orders", "embedding_model", "model-y")
        rows = d.execute(
            "SELECT object_name, value FROM _hyperion_schema_meta "
            "WHERE key = 'embedding_model' ORDER BY object_name"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["object_name"] == "orders"
        assert rows[1]["object_name"] == "users"


# ── INSERT INTO _hyperion_schema_meta ─────────────────────────────────────────

class TestInsertSQL:
    def test_insert_creates_retrievable_tag(self):
        d = db()
        d.execute(
            "INSERT INTO _hyperion_schema_meta VALUES "
            "('table', 'users', 'description', 'User accounts')"
        )
        assert d.get_meta("table", "users", "description") == "User accounts"

    def test_insert_with_column_names(self):
        d = db()
        d.execute(
            "INSERT INTO _hyperion_schema_meta "
            "(object_type, object_name, key, value) "
            "VALUES ('column', 'users.id', 'description', 'Primary key')"
        )
        assert d.get_meta("column", "users.id", "description") == "Primary key"

    def test_insert_visible_in_select(self):
        d = db()
        d.execute(
            "INSERT INTO _hyperion_schema_meta VALUES "
            "('table', 'orders', 'tenant', 'acme')"
        )
        rows = d.execute(
            "SELECT value FROM _hyperion_schema_meta "
            "WHERE object_name = 'orders'"
        ).fetchall()
        assert rows[0]["value"] == "acme"

    def test_insert_multiple_rows(self):
        d = db()
        for tname, desc in [("users", "Users"), ("orders", "Orders")]:
            d.execute(
                f"INSERT INTO _hyperion_schema_meta VALUES "
                f"('table', '{tname}', 'description', '{desc}')"
            )
        rows = d.execute(
            "SELECT * FROM _hyperion_schema_meta ORDER BY object_name"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["value"] == "Orders"
        assert rows[1]["value"] == "Users"


# ── UPDATE _hyperion_schema_meta ──────────────────────────────────────────────

class TestUpdateSQL:
    def test_update_value(self):
        d = db()
        d.set_meta("table", "users", "description", "old")
        d.execute(
            "UPDATE _hyperion_schema_meta SET value = 'new' "
            "WHERE object_type = 'table' AND object_name = 'users' AND key = 'description'"
        )
        assert d.get_meta("table", "users", "description") == "new"

    def test_update_no_matching_rows_is_noop(self):
        d = db()
        d.execute(
            "UPDATE _hyperion_schema_meta SET value = 'x' "
            "WHERE object_name = 'ghost'"
        )
        rows = d.execute("SELECT * FROM _hyperion_schema_meta").fetchall()
        assert rows == []

    def test_update_multiple_matching_rows(self):
        d = db()
        d.set_meta("table", "users", "env", "dev")
        d.set_meta("table", "orders", "env", "dev")
        d.execute(
            "UPDATE _hyperion_schema_meta SET value = 'prod' WHERE key = 'env'"
        )
        assert d.get_meta("table", "users", "env") == "prod"
        assert d.get_meta("table", "orders", "env") == "prod"


# ── DELETE FROM _hyperion_schema_meta ─────────────────────────────────────────

class TestDeleteSQL:
    def test_delete_specific_row(self):
        d = db()
        d.set_meta("table", "users", "description", "Users")
        d.set_meta("table", "users", "other", "keep")
        d.execute(
            "DELETE FROM _hyperion_schema_meta "
            "WHERE object_type = 'table' AND object_name = 'users' AND key = 'description'"
        )
        assert d.get_meta("table", "users", "description") is None
        assert d.get_meta("table", "users", "other") == "keep"

    def test_delete_all_rows_for_object(self):
        d = db()
        d.set_meta("table", "users", "a", "1")
        d.set_meta("table", "users", "b", "2")
        d.execute(
            "DELETE FROM _hyperion_schema_meta "
            "WHERE object_type = 'table' AND object_name = 'users'"
        )
        assert d.get_meta("table", "users") == {}

    def test_delete_without_where_removes_all(self):
        d = db()
        d.set_meta("table", "users", "description", "Users")
        d.set_meta("column", "users.email", "description", "Email")
        d.execute("DELETE FROM _hyperion_schema_meta")
        rows = d.execute("SELECT * FROM _hyperion_schema_meta").fetchall()
        assert rows == []

    def test_delete_no_match_is_noop(self):
        d = db()
        d.set_meta("table", "users", "description", "Users")
        d.execute(
            "DELETE FROM _hyperion_schema_meta WHERE object_name = 'ghost'"
        )
        assert d.get_meta("table", "users", "description") == "Users"


# ── Persistence ───────────────────────────────────────────────────────────────

class TestPersistence:
    def test_meta_survives_file_roundtrip(self, tmp_path):
        path = tmp_path / "meta_test.hdb"
        d = Database(path)
        d.execute("CREATE TABLE t (id INTEGER)")
        # SQL INSERT auto-commits, so meta is flushed to disk
        d.execute(
            "INSERT INTO _hyperion_schema_meta VALUES "
            "('table', 't', 'description', 'Test table')"
        )
        d.close()

        d2 = Database(path)
        assert d2.get_meta("table", "t", "description") == "Test table"
        d2.close()

    def test_meta_rollback_discards_writes(self):
        d = db()
        d.begin()
        d.set_meta("table", "users", "description", "will be rolled back")
        d.rollback()
        assert d.get_meta("table", "users", "description") is None

    def test_meta_commit_persists_in_memory(self):
        d = db()
        d.begin()
        d.execute(
            "INSERT INTO _hyperion_schema_meta VALUES "
            "('table', 'users', 'description', 'committed')"
        )
        d.commit()
        assert d.get_meta("table", "users", "description") == "committed"

    def test_sql_insert_meta_rollback(self):
        d = db()
        d.begin()
        d.execute(
            "INSERT INTO _hyperion_schema_meta VALUES "
            "('table', 'users', 'description', 'will roll back')"
        )
        d.rollback()
        rows = d.execute("SELECT * FROM _hyperion_schema_meta").fetchall()
        assert rows == []


# ── Integration: combined queries ─────────────────────────────────────────────

class TestIntegration:
    def test_select_meta_for_known_tables(self):
        """Verify that metadata covers exactly the tables that exist."""
        d = db()
        d.set_meta("table", "users", "description", "User accounts")
        d.set_meta("table", "orders", "description", "Order records")
        # Get table names from _hyperion_master
        tables = {
            r["name"]
            for r in d.execute(
                "SELECT name FROM _hyperion_master WHERE type = 'table'"
            ).fetchall()
        }
        # Get tagged table names from _hyperion_schema_meta
        tagged = {
            r["object_name"]
            for r in d.execute(
                "SELECT object_name FROM _hyperion_schema_meta "
                "WHERE object_type = 'table' AND key = 'description'"
            ).fetchall()
        }
        assert tables == tagged

    def test_agent_text_to_sql_context_pattern(self):
        """Simulate an LLM agent reading schema context before generating SQL."""
        d = db()
        d.set_meta("table", "users", "description", "Registered user accounts")
        d.set_meta("column", "users.email", "description", "Unique email address")
        d.set_meta("column", "users.id", "description", "Auto-increment primary key")
        d.set_meta("table", "users", "embedding_model", "text-embedding-3-small")

        # Agent reads all metadata for the 'users' table
        rows = d.execute(
            "SELECT key, value FROM _hyperion_schema_meta "
            "WHERE object_type IN ('table', 'column') "
            "AND (object_name = 'users' OR object_name LIKE 'users.%') "
            "ORDER BY object_name, key"
        ).fetchall()
        assert len(rows) == 4
        keys = {r["key"] for r in rows}
        assert "description" in keys
        assert "embedding_model" in keys
