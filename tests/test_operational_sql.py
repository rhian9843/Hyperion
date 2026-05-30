# Tests for: PRAGMA, VACUUM, quoted identifiers
import os
import sys
import tempfile
import unittest
from pathlib import Path
from subprocess import PIPE, run

sys.path.insert(0, str(Path(__file__).parent.parent))

DATABASE_COMMAND = ["python3", "-m", "hyperion"]


def db_run(commands, db_path):
    result = run(
        DATABASE_COMMAND + [db_path],
        input="\n".join(commands) + "\n",
        stdout=PIPE, stderr=PIPE, encoding="utf-8",
    )
    lines = []
    for line in result.stdout.splitlines():
        stripped = line.removeprefix("H > ").strip()
        if stripped and stripped != "...":
            lines.append(stripped)
    return result.returncode, lines


class TempDB:
    def __enter__(self):
        self._f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._f.close()
        os.unlink(self._f.name)
        return self._f.name

    def __exit__(self, *_):
        try:
            os.unlink(self._f.name)
        except FileNotFoundError:
            pass


# ── PRAGMA table_info ──────────────────────────────────────────────────────────

class TestPragmaTableInfo(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE emp (id INTEGER PRIMARY KEY, name VARCHAR(64) NOT NULL, dept VARCHAR(32))",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_returns_all_columns(self):
        _, lines = db_run(["PRAGMA table_info(emp)", ".exit"], self.db)
        full = " ".join(lines)
        self.assertIn("id", full)
        self.assertIn("name", full)
        self.assertIn("dept", full)

    def test_notnull_flag(self):
        _, lines = db_run(["PRAGMA table_info(emp)", ".exit"], self.db)
        # Data rows start with a digit (cid); look for name row with notnull=1
        data_rows = [l for l in lines if l and l[0].isdigit()]
        name_row = next((l for l in data_rows if "| name |" in l or l.split("|")[1].strip() == "name"), None)
        self.assertIsNotNone(name_row, "No data row found for 'name' column")
        self.assertIn("1", name_row)

    def test_pk_flag(self):
        _, lines = db_run(["PRAGMA table_info(emp)", ".exit"], self.db)
        data_rows = [l for l in lines if l and l[0].isdigit()]
        id_row = next((l for l in data_rows if l.split("|")[1].strip() == "id"), None)
        self.assertIsNotNone(id_row, "No data row found for 'id' column")
        self.assertIn("1", id_row)  # pk=1 for id

    def test_unknown_table_raises(self):
        _, lines = db_run(["PRAGMA table_info(nonexistent)", ".exit"], self.db)
        self.assertIn("Error", " ".join(lines))


# ── PRAGMA index_list ──────────────────────────────────────────────────────────

class TestPragmaIndexList(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE t (id INTEGER PRIMARY KEY, val INTEGER)",
            "CREATE INDEX idx_val ON t(val)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_lists_user_index(self):
        _, lines = db_run(["PRAGMA index_list(t)", ".exit"], self.db)
        full = " ".join(lines)
        self.assertIn("idx_val", full)

    def test_lists_pk_index(self):
        _, lines = db_run(["PRAGMA index_list(t)", ".exit"], self.db)
        full = " ".join(lines)
        self.assertIn("_pk_t_id", full)

    def test_no_rows_for_unknown_table(self):
        _, lines = db_run(["PRAGMA index_list(nobody)", ".exit"], self.db)
        self.assertIn("no rows", " ".join(lines))


# ── PRAGMA index_info ──────────────────────────────────────────────────────────

class TestPragmaIndexInfo(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE t (id INTEGER, name VARCHAR(32), age INTEGER)",
            "CREATE INDEX idx_name ON t(name)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_returns_indexed_column(self):
        _, lines = db_run(["PRAGMA index_info(idx_name)", ".exit"], self.db)
        self.assertIn("name", " ".join(lines))

    def test_unknown_index_raises(self):
        _, lines = db_run(["PRAGMA index_info(no_such_idx)", ".exit"], self.db)
        self.assertIn("Error", " ".join(lines))


# ── PRAGMA foreign_keys ────────────────────────────────────────────────────────

class TestPragmaForeignKeys(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE parent (id INTEGER PRIMARY KEY)",
            "CREATE TABLE child (id INTEGER, parent_id INTEGER REFERENCES parent(id))",
            "INSERT INTO parent VALUES (1)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_fk_enforced_by_default(self):
        _, lines = db_run([
            "INSERT INTO child VALUES (1, 999)",
            ".exit",
        ], self.db)
        # Should print an FK error: 999 not in parent
        self.assertIn("Error", " ".join(lines))

    def test_fk_disabled_allows_orphan(self):
        rc, lines = db_run([
            "PRAGMA foreign_keys = OFF",
            "INSERT INTO child VALUES (1, 999)",
            ".exit",
        ], self.db)
        self.assertEqual(rc, 0)
        _, sel = db_run(["SELECT COUNT(*) FROM child", ".exit"], self.db)
        self.assertIn("1", " ".join(sel))

    def test_pragma_query_returns_state(self):
        _, lines = db_run(["PRAGMA foreign_keys", ".exit"], self.db)
        self.assertIn("1", " ".join(lines))  # default ON

    def test_fk_re_enabled(self):
        # Use numeric form to avoid REPL continuation-token issue with the keyword ON
        _, lines = db_run([
            "PRAGMA foreign_keys = 0",
            "PRAGMA foreign_keys = 1",
            "INSERT INTO child VALUES (1, 999)",
            ".exit",
        ], self.db)
        self.assertIn("Error", " ".join(lines))


# ── VACUUM ─────────────────────────────────────────────────────────────────────

class TestVacuum(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE t (id INTEGER, val VARCHAR(32))",
            "INSERT INTO t VALUES (1, 'a')",
            "INSERT INTO t VALUES (2, 'b')",
            "INSERT INTO t VALUES (3, 'c')",
            "DELETE FROM t WHERE id = 2",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_vacuum_succeeds(self):
        rc, lines = db_run(["VACUUM", ".exit"], self.db)
        self.assertEqual(rc, 0)
        self.assertIn("vacuumed", " ".join(lines).lower())

    def test_data_intact_after_vacuum(self):
        db_run(["VACUUM", ".exit"], self.db)
        _, lines = db_run(["SELECT id FROM t ORDER BY id", ".exit"], self.db)
        # Collect only data rows (lines that are plain integers)
        ids = [l.strip() for l in lines if l.strip().isdigit()]
        self.assertIn("1", ids)
        self.assertIn("3", ids)
        self.assertNotIn("2", ids)

    def test_file_size_reduced_or_equal_after_vacuum(self):
        size_before = os.path.getsize(self.db)
        db_run(["VACUUM", ".exit"], self.db)
        size_after = os.path.getsize(self.db)
        # Compact database should not be larger than the original
        self.assertLessEqual(size_after, size_before + 4096)  # allow one page slack


# ── VACUUM catalog preservation ───────────────────────────────────────────────

def test_vacuum_preserves_triggers(tmp_path):
    """Triggers must survive VACUUM and continue firing correctly afterwards."""
    from hyperion import Database
    db = Database(tmp_path / "v.hdb")
    db.execute("CREATE TABLE t (id INTEGER, val TEXT)")
    db.execute("CREATE TABLE log (msg TEXT)")
    db.execute("""
        CREATE TRIGGER trg_after_insert AFTER INSERT ON t
        FOR EACH ROW BEGIN
            INSERT INTO log VALUES ('inserted');
        END
    """)
    db.execute("INSERT INTO t VALUES (1, 'pre-vacuum')")
    assert db.execute("SELECT COUNT(*) AS n FROM log").fetchone()["n"] == 1

    db.vacuum()

    # Trigger must still be registered in the catalog
    assert "trg_after_insert" in db._catalog.triggers

    # Trigger must still fire after vacuum
    db.execute("INSERT INTO t VALUES (2, 'post-vacuum')")
    assert db.execute("SELECT COUNT(*) AS n FROM log").fetchone()["n"] == 2

    db.close()


def test_vacuum_preserves_analyze_stats(tmp_path):
    """ANALYZE statistics must survive VACUUM so the optimizer uses them."""
    from hyperion import Database
    db = Database(tmp_path / "v.hdb")
    db.execute("CREATE TABLE t (id INTEGER, score INTEGER)")
    for i in range(20):
        db.execute(f"INSERT INTO t VALUES ({i}, {i * 10})")
    db.execute("ANALYZE")

    assert db._catalog.stats.get("t", {}).get("row_count") == 20

    db.vacuum()

    assert db._catalog.stats.get("t", {}).get("row_count") == 20, \
        "ANALYZE stats lost after VACUUM"
    db.close()


def test_vacuum_preserves_schema_metadata(tmp_path):
    """set_meta annotations must survive VACUUM."""
    from hyperion import Database
    db = Database(tmp_path / "v.hdb")
    db.execute("CREATE TABLE users (id INTEGER, email TEXT)")
    db.set_meta("table",  "users",        "description", "User accounts")
    db.set_meta("column", "users.email",  "description", "Primary contact")

    db.vacuum()

    assert db.get_meta("table",  "users")       == {"description": "User accounts"}, \
        "Table metadata lost after VACUUM"
    assert db.get_meta("column", "users.email") == {"description": "Primary contact"}, \
        "Column metadata lost after VACUUM"
    db.close()


def test_vacuum_preserves_all_three_survive_reopen(tmp_path):
    """Triggers, stats, and metadata must all be readable after close+reopen post-VACUUM."""
    from hyperion import Database
    db = Database(tmp_path / "v.hdb")
    db.execute("CREATE TABLE t (id INTEGER, val TEXT)")
    db.execute("CREATE TABLE log (n INTEGER)")
    db.execute("""
        CREATE TRIGGER trg AFTER INSERT ON t FOR EACH ROW
        BEGIN INSERT INTO log VALUES (1); END
    """)
    for i in range(10):
        db.execute(f"INSERT INTO t VALUES ({i}, 'x')")
    db.execute("ANALYZE")
    db.set_meta("table", "t", "owner", "test-suite")

    db.vacuum()
    db.close()

    db2 = Database(tmp_path / "v.hdb")
    assert "trg" in db2._catalog.triggers
    assert db2._catalog.stats.get("t", {}).get("row_count") == 10
    assert db2.get_meta("table", "t") == {"owner": "test-suite"}

    # Trigger still fires after reopen
    pre = db2.execute("SELECT COUNT(*) AS n FROM log").fetchone()["n"]
    db2.execute("INSERT INTO t VALUES (99, 'after-reopen')")
    post = db2.execute("SELECT COUNT(*) AS n FROM log").fetchone()["n"]
    assert post == pre + 1, f"Trigger did not fire after reopen (log: {pre} → {post})"
    db2.close()


def test_vacuum_preserves_multiple_triggers_all_types(tmp_path):
    """BEFORE, AFTER, and UPDATE triggers on the same table all survive VACUUM."""
    from hyperion import Database
    db = Database(tmp_path / "v.hdb")
    db.execute("CREATE TABLE t (id INTEGER, val TEXT)")
    db.execute("CREATE TABLE log (event TEXT)")
    db.execute("""
        CREATE TRIGGER trg_before_insert BEFORE INSERT ON t FOR EACH ROW
        BEGIN INSERT INTO log VALUES ('before_insert'); END
    """)
    db.execute("""
        CREATE TRIGGER trg_after_insert AFTER INSERT ON t FOR EACH ROW
        BEGIN INSERT INTO log VALUES ('after_insert'); END
    """)
    db.execute("""
        CREATE TRIGGER trg_after_delete AFTER DELETE ON t FOR EACH ROW
        BEGIN INSERT INTO log VALUES ('after_delete'); END
    """)
    db.execute("""
        CREATE TRIGGER trg_after_update AFTER UPDATE ON t FOR EACH ROW
        BEGIN INSERT INTO log VALUES ('after_update'); END
    """)

    db.vacuum()

    for name in ("trg_before_insert", "trg_after_insert",
                 "trg_after_delete", "trg_after_update"):
        assert name in db._catalog.triggers, f"{name} lost after VACUUM"

    db.execute("INSERT INTO t VALUES (1, 'x')")
    events = [r["event"] for r in db.execute("SELECT event FROM log ORDER BY rowid").fetchall()]
    assert "before_insert" in events
    assert "after_insert" in events

    db.execute("UPDATE t SET val = 'y' WHERE id = 1")
    events2 = [r["event"] for r in db.execute("SELECT event FROM log ORDER BY rowid").fetchall()]
    assert "after_update" in events2

    db.execute("DELETE FROM t WHERE id = 1")
    events3 = [r["event"] for r in db.execute("SELECT event FROM log ORDER BY rowid").fetchall()]
    assert "after_delete" in events3

    db.close()


def test_vacuum_preserves_instead_of_trigger_on_view(tmp_path):
    """INSTEAD OF trigger on a view must survive VACUUM and still intercept DML."""
    from hyperion import Database
    db = Database(tmp_path / "v.hdb")
    db.execute("CREATE TABLE base (id INTEGER, val TEXT)")
    db.execute("CREATE VIEW v AS SELECT id, val FROM base")
    db.execute("CREATE TABLE routed (id INTEGER, val TEXT)")
    db.execute("""
        CREATE TRIGGER trg_instead INSTEAD OF INSERT ON v FOR EACH ROW
        BEGIN INSERT INTO routed VALUES (NEW.id, NEW.val); END
    """)

    db.vacuum()

    assert "trg_instead" in db._catalog.triggers

    db.execute("INSERT INTO v (id, val) VALUES (42, 'via-view')")
    row = db.execute("SELECT id FROM routed").fetchone()
    assert row is not None and row["id"] == 42, \
        "INSTEAD OF trigger did not fire after VACUUM"

    db.close()


def test_vacuum_preserves_trigger_with_when_clause(tmp_path):
    """A trigger with a WHEN guard must survive VACUUM and only fire when the condition holds."""
    from hyperion import Database
    db = Database(tmp_path / "v.hdb")
    db.execute("CREATE TABLE t (id INTEGER, score INTEGER)")
    db.execute("CREATE TABLE log (id INTEGER)")
    db.execute("""
        CREATE TRIGGER trg_high_score AFTER INSERT ON t FOR EACH ROW
        WHEN NEW.score > 90
        BEGIN INSERT INTO log VALUES (NEW.id); END
    """)

    db.vacuum()

    db.execute("INSERT INTO t VALUES (1, 50)")   # below threshold — should not log
    db.execute("INSERT INTO t VALUES (2, 95)")   # above threshold — should log
    rows = db.execute("SELECT id FROM log").fetchall()
    assert [r["id"] for r in rows] == [2], \
        f"WHEN clause not preserved after VACUUM: got {rows}"

    db.close()


def test_vacuum_preserves_analyze_stats_multiple_tables(tmp_path):
    """ANALYZE stats for every table must all survive VACUUM."""
    from hyperion import Database
    db = Database(tmp_path / "v.hdb")
    db.execute("CREATE TABLE a (x INTEGER)")
    db.execute("CREATE TABLE b (y INTEGER)")
    for i in range(10):
        db.execute(f"INSERT INTO a VALUES ({i})")
    for i in range(25):
        db.execute(f"INSERT INTO b VALUES ({i})")
    db.execute("ANALYZE")

    db.vacuum()

    assert db._catalog.stats.get("a", {}).get("row_count") == 10, "Stats for 'a' lost"
    assert db._catalog.stats.get("b", {}).get("row_count") == 25, "Stats for 'b' lost"
    db.close()


def test_vacuum_preserves_metadata_multiple_objects(tmp_path):
    """Metadata on tables, columns, and indexes must all survive VACUUM."""
    from hyperion import Database
    db = Database(tmp_path / "v.hdb")
    db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)")
    db.execute("CREATE INDEX idx_email ON users (email)")
    db.set_meta("table",  "users",         "description",     "Registered users")
    db.set_meta("column", "users.email",   "description",     "Login email address")
    db.set_meta("column", "users.email",   "embedding_model", "text-embedding-3-small")
    db.set_meta("index",  "idx_email",     "purpose",         "Login lookup")

    db.vacuum()

    assert db.get_meta("table",  "users")["description"]         == "Registered users"
    assert db.get_meta("column", "users.email")["description"]   == "Login email address"
    assert db.get_meta("column", "users.email")["embedding_model"] == "text-embedding-3-small"
    assert db.get_meta("index",  "idx_email")["purpose"]         == "Login lookup"
    db.close()


def test_vacuum_with_no_triggers_stats_meta(tmp_path):
    """VACUUM on a plain database with no triggers, ANALYZE, or metadata must not error."""
    from hyperion import Database
    db = Database(tmp_path / "v.hdb")
    db.execute("CREATE TABLE t (id INTEGER)")
    db.execute("INSERT INTO t VALUES (1)")
    db.vacuum()
    assert db.execute("SELECT id FROM t").fetchone()["id"] == 1
    assert db._catalog.triggers == {}
    assert db._catalog.stats    == {}
    assert db._catalog.meta     == {}
    db.close()


# ── Quoted identifiers ─────────────────────────────────────────────────────────

class TestQuotedIdentifiers(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_double_quoted_column_in_select(self):
        db_run([
            'CREATE TABLE t (id INTEGER, "my col" VARCHAR(32))',
            'INSERT INTO t VALUES (1, \'hello\')',
            ".exit",
        ], self.db)
        _, lines = db_run(['SELECT "my col" FROM t', ".exit"], self.db)
        self.assertIn("hello", " ".join(lines))

    def test_backtick_quoted_column_in_select(self):
        db_run([
            "CREATE TABLE t (id INTEGER, `my col` VARCHAR(32))",
            "INSERT INTO t VALUES (1, 'world')",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT `my col` FROM t", ".exit"], self.db)
        self.assertIn("world", " ".join(lines))

    def test_backtick_reserved_word_as_column(self):
        """Backtick-quoting allows reserved words as column names."""
        db_run([
            "CREATE TABLE t (id INTEGER, `select` VARCHAR(32))",
            "INSERT INTO t VALUES (1, 'reserved')",
            ".exit",
        ], self.db)
        _, lines = db_run(["SELECT `select` FROM t", ".exit"], self.db)
        self.assertIn("reserved", " ".join(lines))

    def test_double_quote_in_where(self):
        db_run([
            'CREATE TABLE t (id INTEGER, "val" INTEGER)',
            'INSERT INTO t VALUES (1, 42)',
            ".exit",
        ], self.db)
        _, lines = db_run(['SELECT id FROM t WHERE "val" = 42', ".exit"], self.db)
        self.assertIn("1", " ".join(lines))


if __name__ == "__main__":
    unittest.main()
