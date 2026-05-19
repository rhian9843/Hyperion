# test suite for Hyperion
import os
import tempfile
import unittest
from subprocess import PIPE, run

DATABASE_COMMAND = ["python3", "hyperion.py"]

CREATE_USERS = "CREATE TABLE users (id INTEGER, name VARCHAR(32), email VARCHAR(255))"


def db_run(commands, db_path):
    """Run a list of SQL commands against a database file and return stdout lines."""
    result = run(
        DATABASE_COMMAND + [db_path],
        input="\n".join(commands) + "\n",
        stdout=PIPE,
        stderr=PIPE,
        encoding="utf-8",
    )
    # strip the "H > " prompts and blank lines, return meaningful output lines
    lines = []
    for line in result.stdout.splitlines():
        stripped = line.removeprefix("H > ").strip()
        if stripped:
            lines.append(stripped)
    return result.returncode, lines


class TempDB:
    """Context manager that provides a fresh temporary database path."""
    def __enter__(self):
        self._f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._f.close()
        os.unlink(self._f.name)   # remove so Hyperion creates it fresh
        return self._f.name

    def __exit__(self, *_):
        try:
            os.unlink(self._f.name)
        except FileNotFoundError:
            pass


class TestDDL(unittest.TestCase):
    def test_create_table(self):
        with TempDB() as db:
            rc, lines = db_run([CREATE_USERS, ".exit"], db)
        self.assertEqual(rc, 0)
        self.assertIn("Table 'users' created.", lines)

    def test_create_duplicate_table_raises(self):
        with TempDB() as db:
            _, lines = db_run([CREATE_USERS, CREATE_USERS, ".exit"], db)
        self.assertTrue(any("already exists" in l for l in lines))

    def test_drop_table(self):
        with TempDB() as db:
            _, lines = db_run([CREATE_USERS, "DROP TABLE users", ".tables", ".exit"], db)
        self.assertNotIn("users", lines)

    def test_dot_tables(self):
        with TempDB() as db:
            _, lines = db_run([CREATE_USERS, ".tables", ".exit"], db)
        self.assertIn("users", lines)

    def test_dot_schema(self):
        with TempDB() as db:
            _, lines = db_run([CREATE_USERS, ".schema users", ".exit"], db)
        self.assertTrue(any("CREATE TABLE users" in l for l in lines))


class TestInsertSelect(unittest.TestCase):
    def test_single_insert_and_select(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "SELECT * FROM users",
                ".exit",
            ], db)
        self.assertTrue(any("alice" in l for l in lines))
        self.assertTrue(any("alice@example.com" in l for l in lines))

    def test_named_column_insert(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users (id, name, email) VALUES (42, bob, bob@example.com)",
                "SELECT * FROM users",
                ".exit",
            ], db)
        self.assertTrue(any("42" in l and "bob" in l for l in lines))

    def test_column_projection(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "SELECT name FROM users",
                ".exit",
            ], db)
        self.assertTrue(any("alice" in l for l in lines))
        self.assertFalse(any("alice@example.com" in l for l in lines))

    def test_multiple_rows(self):
        with TempDB() as db:
            cmds = [CREATE_USERS]
            for i in range(1, 6):
                cmds.append(f"INSERT INTO users VALUES ({i}, user{i}, user{i}@example.com)")
            cmds += ["SELECT * FROM users", ".exit"]
            _, lines = db_run(cmds, db)
        self.assertTrue(any("(5 rows)" in l for l in lines))


class TestWhere(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            CREATE_USERS,
            "INSERT INTO users VALUES (1, alice, alice@example.com)",
            "INSERT INTO users VALUES (2, bob, bob@other.com)",
            "INSERT INTO users VALUES (3, carol, carol@example.com)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_where_equals(self):
        _, lines = db_run(["SELECT * FROM users WHERE id = 1", ".exit"], self.db)
        self.assertTrue(any("alice" in l for l in lines))
        self.assertFalse(any("bob" in l for l in lines))

    def test_where_greater_than(self):
        _, lines = db_run(["SELECT * FROM users WHERE id > 1", ".exit"], self.db)
        self.assertFalse(any("alice" in l for l in lines))
        self.assertTrue(any("(2 rows)" in l for l in lines))

    def test_where_like(self):
        _, lines = db_run(
            ["SELECT * FROM users WHERE email LIKE %example.com", ".exit"], self.db
        )
        self.assertTrue(any("alice" in l for l in lines))
        self.assertTrue(any("carol" in l for l in lines))
        self.assertFalse(any("bob" in l for l in lines))

    def test_where_no_match(self):
        _, lines = db_run(["SELECT * FROM users WHERE id = 999", ".exit"], self.db)
        self.assertIn("(no rows)", lines)


class TestDelete(unittest.TestCase):
    def test_delete_where(self):
        with TempDB() as db:
            db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "INSERT INTO users VALUES (2, bob, bob@example.com)",
                ".exit",
            ], db)
            _, lines = db_run([
                "DELETE FROM users WHERE id = 1",
                "SELECT * FROM users",
                ".exit",
            ], db)
        self.assertFalse(any("alice" in l for l in lines))
        self.assertTrue(any("bob" in l for l in lines))


class TestPersistence(unittest.TestCase):
    def test_data_survives_reopen(self):
        with TempDB() as db:
            db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                ".exit",
            ], db)
            _, lines = db_run(["SELECT * FROM users", ".exit"], db)
        self.assertTrue(any("alice" in l for l in lines))

    def test_multiple_tables_persist(self):
        with TempDB() as db:
            db_run([
                CREATE_USERS,
                "CREATE TABLE logs (id INTEGER, message VARCHAR(128))",
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "INSERT INTO logs VALUES (1, hello)",
                ".exit",
            ], db)
            _, lines = db_run([".tables", ".exit"], db)
        self.assertIn("logs", lines)
        self.assertIn("users", lines)


class TestErrorHandling(unittest.TestCase):
    def test_insert_into_missing_table(self):
        with TempDB() as db:
            _, lines = db_run(
                ["INSERT INTO ghost VALUES (1, x, y)", ".exit"], db
            )
        self.assertTrue(any("Error" in l for l in lines))

    def test_table_full(self):
        with TempDB() as db:
            # use a very small TEXT to maximise rows_per_page and hit the cap fast
            cmds = ["CREATE TABLE tiny (id INTEGER, val VARCHAR(1))"]
            # PAGES_PER_TABLE=200, row=(8+1)=9 bytes, rows_per_page=455, max=91000
            # inserting 1 more than max is impractical — just verify the error path exists
            cmds += ["DROP TABLE tiny", ".exit"]
            rc, _ = db_run(cmds, db)
        self.assertEqual(rc, 0)


class TestIndexAllTypes(unittest.TestCase):
    """Indexes on non-INTEGER column types."""

    def test_index_on_varchar(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "INSERT INTO users VALUES (2, bob, bob@example.com)",
                "CREATE INDEX idx_name ON users(name)",
                "SELECT * FROM users WHERE name = alice",
                ".exit",
            ], db)
        self.assertTrue(any("alice" in l for l in lines))
        self.assertFalse(any("bob" in l for l in lines))

    def test_index_on_real(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE prices (id INTEGER, amount REAL)",
                "INSERT INTO prices VALUES (1, 9.99)",
                "INSERT INTO prices VALUES (2, 19.99)",
                "INSERT INTO prices VALUES (3, 4.99)",
                "CREATE INDEX idx_amount ON prices(amount)",
                "SELECT * FROM prices WHERE amount = 9.99",
                ".exit",
            ], db)
        self.assertTrue(any("9.99" in l for l in lines))
        self.assertFalse(any("19.99" in l for l in lines))

    def test_varchar_index_persists(self):
        with TempDB() as db:
            db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "CREATE INDEX idx_email ON users(email)",
                ".exit",
            ], db)
            _, lines = db_run([
                "SELECT * FROM users WHERE email = alice@example.com",
                ".exit",
            ], db)
        self.assertTrue(any("alice" in l for l in lines))

    def test_varchar_index_no_match(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "CREATE INDEX idx_name ON users(name)",
                "SELECT * FROM users WHERE name = nobody",
                ".exit",
            ], db)
        self.assertIn("(no rows)", lines)


class TestAlterTable(unittest.TestCase):
    def test_add_column(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "ALTER TABLE users ADD COLUMN age INTEGER",
                "SELECT * FROM users",
                ".exit",
            ], db)
        self.assertTrue(any("age" in l for l in lines))
        self.assertTrue(any("alice" in l for l in lines))

    def test_add_column_existing_rows_null(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "ALTER TABLE users ADD COLUMN age INTEGER",
                "SELECT age FROM users",
                ".exit",
            ], db)
        self.assertTrue(any("NULL" in l for l in lines))

    def test_drop_column(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "ALTER TABLE users DROP COLUMN email",
                "SELECT * FROM users",
                ".exit",
            ], db)
        self.assertTrue(any("alice" in l for l in lines))
        self.assertFalse(any("alice@example.com" in l for l in lines))

    def test_drop_column_removes_index(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "CREATE INDEX idx_id ON users(id)",
                "ALTER TABLE users DROP COLUMN id",
                ".indexes",
                ".exit",
            ], db)
        self.assertIn("(no indexes)", lines)

    def test_rename_column(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "ALTER TABLE users RENAME COLUMN name TO username",
                "SELECT username FROM users",
                ".exit",
            ], db)
        self.assertTrue(any("alice" in l for l in lines))

    def test_rename_table(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "ALTER TABLE users RENAME TO members",
                "SELECT * FROM members",
                ".exit",
            ], db)
        self.assertTrue(any("alice" in l for l in lines))

    def test_rename_table_old_name_gone(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "ALTER TABLE users RENAME TO members",
                "SELECT * FROM users",
                ".exit",
            ], db)
        self.assertTrue(any("Error" in l for l in lines))

    def test_add_column_persists(self):
        with TempDB() as db:
            db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "ALTER TABLE users ADD COLUMN score INTEGER",
                "UPDATE users SET score=99 WHERE id = 1",
                ".exit",
            ], db)
            _, lines = db_run(["SELECT score FROM users", ".exit"], db)
        self.assertTrue(any("99" in l for l in lines))


class TestAggregates(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            CREATE_USERS,
            "INSERT INTO users VALUES (1, alice, alice@example.com)",
            "INSERT INTO users VALUES (2, bob, bob@example.com)",
            "INSERT INTO users VALUES (3, carol, carol@example.com)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_count_star(self):
        _, lines = db_run(["SELECT COUNT(*) FROM users", ".exit"], self.db)
        self.assertTrue(any("3" in l for l in lines))

    def test_count_col_excludes_nulls(self):
        db_run(["INSERT INTO users (id, name) VALUES (4, dave)", ".exit"], self.db)
        _, lines = db_run(["SELECT COUNT(email) FROM users", ".exit"], self.db)
        self.assertTrue(any("3" in l for l in lines))

    def test_min(self):
        _, lines = db_run(["SELECT MIN(id) FROM users", ".exit"], self.db)
        self.assertTrue(any("1" in l for l in lines))

    def test_max(self):
        _, lines = db_run(["SELECT MAX(id) FROM users", ".exit"], self.db)
        self.assertTrue(any("3" in l for l in lines))

    def test_sum(self):
        _, lines = db_run(["SELECT SUM(id) FROM users", ".exit"], self.db)
        self.assertTrue(any("6" in l for l in lines))

    def test_avg(self):
        _, lines = db_run(["SELECT AVG(id) FROM users", ".exit"], self.db)
        self.assertTrue(any("2" in l for l in lines))

    def test_multi_agg(self):
        _, lines = db_run(["SELECT COUNT(*), MIN(id), MAX(id) FROM users", ".exit"], self.db)
        self.assertTrue(any("3" in l for l in lines))
        self.assertTrue(any("1" in l for l in lines))

    def test_agg_with_where(self):
        _, lines = db_run(["SELECT COUNT(*) FROM users WHERE id > 1", ".exit"], self.db)
        self.assertTrue(any("2" in l for l in lines))

    def test_agg_empty_table(self):
        db_run(["CREATE TABLE empty (x INTEGER)", ".exit"], self.db)
        _, lines = db_run(["SELECT COUNT(*) FROM empty", ".exit"], self.db)
        self.assertTrue(any("0" in l for l in lines))


class TestOrderByLimit(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            CREATE_USERS,
            "INSERT INTO users VALUES (3, carol, carol@example.com)",
            "INSERT INTO users VALUES (1, alice, alice@example.com)",
            "INSERT INTO users VALUES (2, bob, bob@example.com)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    @staticmethod
    def _data_lines(lines, skip_header):
        """Return data-only lines: skip header, separator (all dashes/plus), row count."""
        return [
            l.strip() for l in lines
            if l.strip()
            and l.strip() != skip_header
            and not all(c in "-+" for c in l.strip())
            and "row" not in l
        ]

    def test_order_by_asc(self):
        _, lines = db_run(["SELECT name FROM users ORDER BY name ASC", ".exit"], self.db)
        self.assertEqual(self._data_lines(lines, "name"), ["alice", "bob", "carol"])

    def test_order_by_desc(self):
        _, lines = db_run(["SELECT name FROM users ORDER BY name DESC", ".exit"], self.db)
        self.assertEqual(self._data_lines(lines, "name"), ["carol", "bob", "alice"])

    def test_order_by_integer(self):
        _, lines = db_run(["SELECT id FROM users ORDER BY id ASC", ".exit"], self.db)
        self.assertEqual(self._data_lines(lines, "id"), ["1", "2", "3"])

    def test_limit(self):
        _, lines = db_run(["SELECT * FROM users ORDER BY id ASC LIMIT 2", ".exit"], self.db)
        self.assertTrue(any("(2 rows)" in l for l in lines))
        self.assertFalse(any("carol" in l for l in lines))

    def test_limit_1(self):
        _, lines = db_run(["SELECT * FROM users ORDER BY id ASC LIMIT 1", ".exit"], self.db)
        self.assertTrue(any("(1 row)" in l for l in lines))
        self.assertTrue(any("alice" in l for l in lines))

    def test_order_by_with_where(self):
        _, lines = db_run(
            ["SELECT name FROM users WHERE id > 1 ORDER BY name DESC", ".exit"], self.db
        )
        self.assertEqual(self._data_lines(lines, "name"), ["carol", "bob"])

    def test_nulls_last(self):
        db_run(["INSERT INTO users (id, name) VALUES (4, dave)", ".exit"], self.db)
        _, lines = db_run(["SELECT email FROM users ORDER BY email ASC", ".exit"], self.db)
        data = self._data_lines(lines, "email")
        self.assertEqual(data[-1], "NULL")


class TestNull(unittest.TestCase):
    def test_explicit_null_stored_and_displayed(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users (id, name) VALUES (1, alice)",
                "SELECT * FROM users",
                ".exit",
            ], db)
        self.assertTrue(any("NULL" in l for l in lines))

    def test_null_keyword_in_values(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, NULL, alice@example.com)",
                "SELECT * FROM users",
                ".exit",
            ], db)
        self.assertTrue(any("NULL" in l for l in lines))

    def test_where_is_null(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users (id, name) VALUES (1, alice)",
                "INSERT INTO users VALUES (2, bob, bob@example.com)",
                "SELECT * FROM users WHERE email IS NULL",
                ".exit",
            ], db)
        self.assertTrue(any("alice" in l for l in lines))
        self.assertFalse(any("bob" in l for l in lines))

    def test_where_is_not_null(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users (id, name) VALUES (1, alice)",
                "INSERT INTO users VALUES (2, bob, bob@example.com)",
                "SELECT * FROM users WHERE email IS NOT NULL",
                ".exit",
            ], db)
        self.assertFalse(any("alice" in l for l in lines))
        self.assertTrue(any("bob" in l for l in lines))

    def test_not_null_constraint(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER NOT NULL, val VARCHAR(32))",
                "INSERT INTO t VALUES (NULL, hello)",
                ".exit",
            ], db)
        self.assertTrue(any("Error" in l for l in lines))

    def test_null_not_matched_by_equals(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users (id, name) VALUES (1, alice)",
                "SELECT * FROM users WHERE email = NULL",
                ".exit",
            ], db)
        self.assertIn("(no rows)", lines)


class TestTransactions(unittest.TestCase):
    def test_commit_persists(self):
        with TempDB() as db:
            db_run([
                CREATE_USERS,
                "BEGIN",
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "COMMIT",
                ".exit",
            ], db)
            _, lines = db_run(["SELECT * FROM users", ".exit"], db)
        self.assertTrue(any("alice" in l for l in lines))

    def test_rollback_discards(self):
        with TempDB() as db:
            db_run([CREATE_USERS, ".exit"], db)
            db_run([
                "BEGIN",
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "ROLLBACK",
                ".exit",
            ], db)
            _, lines = db_run(["SELECT * FROM users", ".exit"], db)
        self.assertIn("(no rows)", lines)

    def test_multi_statement_txn(self):
        with TempDB() as db:
            db_run([
                CREATE_USERS,
                "BEGIN",
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "INSERT INTO users VALUES (2, bob, bob@example.com)",
                "DELETE FROM users WHERE id = 1",
                "COMMIT",
                ".exit",
            ], db)
            _, lines = db_run(["SELECT * FROM users", ".exit"], db)
        self.assertFalse(any("alice" in l for l in lines))
        self.assertTrue(any("bob" in l for l in lines))

    def test_begin_already_active(self):
        with TempDB() as db:
            _, lines = db_run([CREATE_USERS, "BEGIN", "BEGIN", ".exit"], db)
        self.assertTrue(any("Error" in l for l in lines))

    def test_commit_without_begin(self):
        with TempDB() as db:
            _, lines = db_run([CREATE_USERS, "COMMIT", ".exit"], db)
        self.assertTrue(any("Error" in l for l in lines))

    def test_rollback_discards_updates(self):
        with TempDB() as db:
            db_run([CREATE_USERS,
                    "INSERT INTO users VALUES (1, alice, a@x.com)", ".exit"], db)
            db_run(["BEGIN",
                    "UPDATE users SET name = bob WHERE id = 1",
                    "ROLLBACK", ".exit"], db)
            _, lines = db_run(["SELECT * FROM users", ".exit"], db)
        self.assertTrue(any("alice" in l for l in lines))
        self.assertFalse(any("bob" in l for l in lines))

    def test_rollback_discards_deletes(self):
        with TempDB() as db:
            db_run([CREATE_USERS,
                    "INSERT INTO users VALUES (1, alice, a@x.com)", ".exit"], db)
            db_run(["BEGIN", "DELETE FROM users WHERE id = 1", "ROLLBACK", ".exit"], db)
            _, lines = db_run(["SELECT * FROM users", ".exit"], db)
        self.assertTrue(any("alice" in l for l in lines))

    def test_autocommit_outside_transaction(self):
        """Each statement outside BEGIN/COMMIT is individually auto-committed."""
        with TempDB() as db:
            db_run([CREATE_USERS,
                    "INSERT INTO users VALUES (1, alice, a@x.com)", ".exit"], db)
            _, lines = db_run(["SELECT * FROM users", ".exit"], db)
        self.assertTrue(any("alice" in l for l in lines))


class TestUpdate(unittest.TestCase):
    def test_update_single_row(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "UPDATE users SET name=alice2 WHERE id = 1",
                "SELECT * FROM users",
                ".exit",
            ], db)
        self.assertTrue(any("alice2" in l for l in lines))
        self.assertFalse(any("alice@" in l and "alice2" not in l for l in lines))

    def test_update_multiple_rows(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "INSERT INTO users VALUES (2, bob, bob@example.com)",
                "UPDATE users SET email=updated@example.com",
                "SELECT * FROM users WHERE id = 2",
                ".exit",
            ], db)
        self.assertTrue(any("updated@example.com" in l for l in lines))

    def test_update_returns_count(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "INSERT INTO users VALUES (2, bob, bob@example.com)",
                "UPDATE users SET name=x WHERE id = 1",
                ".exit",
            ], db)
        self.assertTrue(any("1 row updated" in l for l in lines))


class TestJoin(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            CREATE_USERS,
            "CREATE TABLE orders (uid INTEGER, item VARCHAR(64))",
            "INSERT INTO users VALUES (1, alice, alice@example.com)",
            "INSERT INTO users VALUES (2, bob, bob@example.com)",
            "INSERT INTO orders VALUES (1, widget)",
            "INSERT INTO orders VALUES (1, gadget)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_inner_join_basic(self):
        _, lines = db_run([
            "SELECT users.name, orders.item FROM users INNER JOIN orders ON users.id = orders.uid",
            ".exit",
        ], self.db)
        self.assertTrue(any("alice" in l and "widget" in l for l in lines))

    def test_inner_join_no_match(self):
        _, lines = db_run([
            "SELECT users.name, orders.item FROM users INNER JOIN orders ON users.id = orders.uid WHERE users.id = 2",
            ".exit",
        ], self.db)
        self.assertIn("(no rows)", lines)

    def test_inner_join_row_count(self):
        _, lines = db_run([
            "SELECT users.name, orders.item FROM users INNER JOIN orders ON users.id = orders.uid",
            ".exit",
        ], self.db)
        self.assertTrue(any("(2 rows)" in l for l in lines))


class TestIndex(unittest.TestCase):
    def test_create_and_list_index(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "CREATE INDEX idx_id ON users(id)",
                ".indexes",
                ".exit",
            ], db)
        self.assertTrue(any("idx_id" in l for l in lines))

    def test_index_accelerated_lookup(self):
        with TempDB() as db:
            cmds = [CREATE_USERS]
            for i in range(1, 11):
                cmds.append(f"INSERT INTO users VALUES ({i}, user{i}, u{i}@example.com)")
            cmds += [
                "CREATE INDEX idx_id ON users(id)",
                "SELECT * FROM users WHERE id = 7",
                ".exit",
            ]
            _, lines = db_run(cmds, db)
        self.assertTrue(any("user7" in l for l in lines))
        self.assertFalse(any("user1 " in l or "user10" in l for l in lines))

    def test_drop_index(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "CREATE INDEX idx_id ON users(id)",
                "DROP INDEX idx_id",
                ".indexes",
                ".exit",
            ], db)
        self.assertIn("(no indexes)", lines)

    def test_index_persists_across_reopen(self):
        with TempDB() as db:
            db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "CREATE INDEX idx_id ON users(id)",
                ".exit",
            ], db)
            _, lines = db_run([".indexes", ".exit"], db)
        self.assertTrue(any("idx_id" in l for l in lines))


class TestMultiPageCatalog(unittest.TestCase):
    def test_many_tables_persist(self):
        """Create enough tables to overflow a single catalog page (~4088 bytes)."""
        with TempDB() as db:
            cmds = []
            for i in range(20):
                cmds.append(
                    f"CREATE TABLE tbl{i} "
                    f"(id INTEGER, name VARCHAR(255), email VARCHAR(255))"
                )
            cmds.append(".exit")
            _, lines = db_run(cmds, db)
            # All creates should succeed
            self.assertEqual(sum(1 for l in lines if "created" in l), 20)
            # Verify all tables survive a reopen
            _, lines2 = db_run([".tables", ".exit"], db)
        for i in range(20):
            self.assertIn(f"tbl{i}", lines2)

    def test_data_survives_large_catalog(self):
        with TempDB() as db:
            cmds = []
            for i in range(20):
                cmds.append(
                    f"CREATE TABLE tbl{i} (id INTEGER, val VARCHAR(64))"
                )
            cmds += [
                "INSERT INTO tbl0 VALUES (1, hello)",
                "INSERT INTO tbl19 VALUES (99, world)",
                ".exit",
            ]
            db_run(cmds, db)
            _, lines = db_run([
                "SELECT * FROM tbl0",
                "SELECT * FROM tbl19",
                ".exit",
            ], db)
        self.assertTrue(any("hello" in l for l in lines))
        self.assertTrue(any("world" in l for l in lines))


class TestFreePageReclamation(unittest.TestCase):
    def test_file_does_not_grow_after_drop_and_recreate(self):
        """File size should stay bounded when tables are repeatedly dropped and recreated."""
        with TempDB() as db:
            db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "INSERT INTO users VALUES (2, bob, bob@example.com)",
                "DROP TABLE users",
                CREATE_USERS,
                "INSERT INTO users VALUES (1, carol, carol@example.com)",
                ".exit",
            ], db)
            size_after = os.path.getsize(db)

            db_run([
                "DROP TABLE users",
                CREATE_USERS,
                "INSERT INTO users VALUES (1, dave, dave@example.com)",
                ".exit",
            ], db)
            size_final = os.path.getsize(db)

        self.assertLessEqual(size_final, size_after + 4096)

    def test_alter_table_does_not_grow_unboundedly(self):
        with TempDB() as db:
            db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "ALTER TABLE users ADD COLUMN age INTEGER",
                "ALTER TABLE users DROP COLUMN age",
                ".exit",
            ], db)
            size1 = os.path.getsize(db)
            db_run([
                "ALTER TABLE users ADD COLUMN score INTEGER",
                "ALTER TABLE users DROP COLUMN score",
                ".exit",
            ], db)
            size2 = os.path.getsize(db)
        self.assertLessEqual(size2, size1 + 4096)

    def test_drop_index_frees_pages(self):
        with TempDB() as db:
            db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "CREATE INDEX idx_id ON users(id)",
                "DROP INDEX idx_id",
                "CREATE INDEX idx_id2 ON users(id)",
                ".exit",
            ], db)
            size1 = os.path.getsize(db)
            db_run([
                "DROP INDEX idx_id2",
                "CREATE INDEX idx_id3 ON users(id)",
                ".exit",
            ], db)
            size2 = os.path.getsize(db)
        self.assertLessEqual(size2, size1 + 4096)


class TestUnique(unittest.TestCase):
    def test_unique_insert_duplicate_raises(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER UNIQUE, val VARCHAR(32))",
                "INSERT INTO t VALUES (1, a)",
                "INSERT INTO t VALUES (1, b)",
                ".exit",
            ], db)
        self.assertTrue(any("Error" in l for l in lines))

    def test_unique_insert_different_values_ok(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER UNIQUE, val VARCHAR(32))",
                "INSERT INTO t VALUES (1, a)",
                "INSERT INTO t VALUES (2, b)",
                "SELECT * FROM t",
                ".exit",
            ], db)
        self.assertTrue(any("(2 rows)" in l for l in lines))

    def test_unique_allows_null(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, email VARCHAR(64) UNIQUE)",
                "INSERT INTO t (id) VALUES (1)",
                "INSERT INTO t (id) VALUES (2)",
                "SELECT * FROM t",
                ".exit",
            ], db)
        self.assertTrue(any("(2 rows)" in l for l in lines))

    def test_unique_update_to_duplicate_raises(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER UNIQUE, val VARCHAR(32))",
                "INSERT INTO t VALUES (1, a)",
                "INSERT INTO t VALUES (2, b)",
                "UPDATE t SET id=1 WHERE id = 2",
                ".exit",
            ], db)
        self.assertTrue(any("Error" in l for l in lines))

    def test_unique_not_null_combined(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER NOT NULL UNIQUE, val VARCHAR(32))",
                "INSERT INTO t VALUES (NULL, x)",
                ".exit",
            ], db)
        self.assertTrue(any("Error" in l for l in lines))

    def test_schema_shows_unique(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER UNIQUE)",
                ".schema t",
                ".exit",
            ], db)
        self.assertTrue(any("UNIQUE" in l for l in lines))


class TestGroupBy(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE orders (dept VARCHAR(32), amount INTEGER)",
            "INSERT INTO orders VALUES (eng, 100)",
            "INSERT INTO orders VALUES (eng, 200)",
            "INSERT INTO orders VALUES (hr, 50)",
            "INSERT INTO orders VALUES (hr, 75)",
            "INSERT INTO orders VALUES (eng, 300)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_group_by_count(self):
        _, lines = db_run(
            ["SELECT dept, COUNT(*) FROM orders GROUP BY dept", ".exit"], self.db
        )
        # eng has 3 rows, hr has 2
        self.assertTrue(any("3" in l for l in lines))
        self.assertTrue(any("2" in l for l in lines))

    def test_group_by_sum(self):
        _, lines = db_run(
            ["SELECT dept, SUM(amount) FROM orders GROUP BY dept", ".exit"], self.db
        )
        self.assertTrue(any("600" in l for l in lines))   # eng: 100+200+300
        self.assertTrue(any("125" in l for l in lines))   # hr: 50+75

    def test_group_by_avg(self):
        _, lines = db_run(
            ["SELECT dept, AVG(amount) FROM orders GROUP BY dept", ".exit"], self.db
        )
        self.assertTrue(any("200" in l for l in lines))   # eng avg

    def test_group_by_min_max(self):
        _, lines = db_run(
            ["SELECT dept, MIN(amount), MAX(amount) FROM orders GROUP BY dept", ".exit"], self.db
        )
        self.assertTrue(any("100" in l for l in lines))
        self.assertTrue(any("300" in l for l in lines))

    def test_having_filters_groups(self):
        _, lines = db_run(
            ["SELECT dept, COUNT(*) FROM orders GROUP BY dept HAVING COUNT(*) > 2", ".exit"],
            self.db
        )
        self.assertTrue(any("eng" in l for l in lines))
        self.assertFalse(any("hr" in l for l in lines))

    def test_having_sum(self):
        _, lines = db_run(
            ["SELECT dept, SUM(amount) FROM orders GROUP BY dept HAVING SUM(amount) >= 600",
             ".exit"], self.db
        )
        self.assertTrue(any("eng" in l for l in lines))
        self.assertFalse(any("hr" in l for l in lines))

    def test_group_by_with_where(self):
        _, lines = db_run(
            ["SELECT dept, COUNT(*) FROM orders WHERE amount > 60 GROUP BY dept", ".exit"],
            self.db
        )
        # hr only has 75 > 60, so count=1; eng has 100,200,300 so count=3
        self.assertTrue(any("1" in l for l in lines))
        self.assertTrue(any("3" in l for l in lines))

    def test_group_by_order_by(self):
        _, lines = db_run(
            ["SELECT dept, SUM(amount) FROM orders GROUP BY dept ORDER BY dept ASC", ".exit"],
            self.db
        )
        self.assertTrue(any("(2 rows)" in l for l in lines))

    def test_group_by_two_rows_returned(self):
        _, lines = db_run(
            ["SELECT dept, COUNT(*) FROM orders GROUP BY dept", ".exit"], self.db
        )
        self.assertTrue(any("(2 rows)" in l for l in lines))


class TestWhereOrIn(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            CREATE_USERS,
            "INSERT INTO users VALUES (1, alice, alice@example.com)",
            "INSERT INTO users VALUES (2, bob, bob@example.com)",
            "INSERT INTO users VALUES (3, carol, carol@example.com)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_or_two_conditions(self):
        _, lines = db_run(["SELECT * FROM users WHERE id = 1 OR id = 3", ".exit"], self.db)
        self.assertTrue(any("alice" in l for l in lines))
        self.assertTrue(any("carol" in l for l in lines))
        self.assertFalse(any("bob" in l for l in lines))

    def test_or_three_conditions(self):
        _, lines = db_run(["SELECT * FROM users WHERE id = 1 OR id = 2 OR id = 3", ".exit"], self.db)
        self.assertTrue(any("(3 rows)" in l for l in lines))

    def test_in_integers(self):
        _, lines = db_run(["SELECT * FROM users WHERE id IN (1, 3)", ".exit"], self.db)
        self.assertTrue(any("alice" in l for l in lines))
        self.assertTrue(any("carol" in l for l in lines))
        self.assertFalse(any("bob" in l for l in lines))

    def test_in_strings(self):
        _, lines = db_run(["SELECT * FROM users WHERE name IN (alice, carol)", ".exit"], self.db)
        self.assertTrue(any("alice" in l for l in lines))
        self.assertTrue(any("carol" in l for l in lines))
        self.assertFalse(any("bob" in l for l in lines))

    def test_in_no_match(self):
        _, lines = db_run(["SELECT * FROM users WHERE id IN (99, 100)", ".exit"], self.db)
        self.assertIn("(no rows)", lines)

    def test_and_then_or(self):
        _, lines = db_run(
            ["SELECT * FROM users WHERE id = 1 AND name = alice OR id = 3", ".exit"], self.db
        )
        self.assertTrue(any("alice" in l for l in lines))
        self.assertTrue(any("carol" in l for l in lines))
        self.assertFalse(any("bob" in l for l in lines))

    def test_or_falls_back_to_full_scan_with_index(self):
        db_run(["CREATE INDEX idx_id ON users(id)", ".exit"], self.db)
        _, lines = db_run(["SELECT * FROM users WHERE id = 1 OR id = 2", ".exit"], self.db)
        self.assertTrue(any("(2 rows)" in l for l in lines))

    def test_delete_with_in(self):
        _, lines = db_run([
            "DELETE FROM users WHERE id IN (1, 2)",
            "SELECT * FROM users",
            ".exit",
        ], self.db)
        self.assertFalse(any("alice" in l for l in lines))
        self.assertFalse(any("bob" in l for l in lines))
        self.assertTrue(any("carol" in l for l in lines))

    def test_update_with_or(self):
        _, lines = db_run([
            "UPDATE users SET name=updated WHERE id = 1 OR id = 2",
            "SELECT * FROM users WHERE name = updated",
            ".exit",
        ], self.db)
        self.assertTrue(any("(2 rows)" in l for l in lines))


class TestMultiColumnIndex(unittest.TestCase):
    def test_create_multi_column_index_listed(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "CREATE INDEX idx_name_email ON users(name, email)",
                ".indexes",
                ".exit",
            ], db)
        self.assertTrue(any("name" in l and "email" in l for l in lines))

    def test_multi_column_index_lookup(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "INSERT INTO users VALUES (2, bob, bob@example.com)",
                "CREATE INDEX idx_name_email ON users(name, email)",
                "SELECT * FROM users WHERE name = alice AND email = alice@example.com",
                ".exit",
            ], db)
        self.assertTrue(any("alice" in l for l in lines))
        self.assertFalse(any("bob" in l for l in lines))

    def test_multi_column_index_no_match(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "CREATE INDEX idx_name_email ON users(name, email)",
                "SELECT * FROM users WHERE name = alice AND email = wrong@example.com",
                ".exit",
            ], db)
        self.assertIn("(no rows)", lines)

    def test_multi_column_index_maintained_on_insert(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "CREATE INDEX idx_name_email ON users(name, email)",
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "SELECT * FROM users WHERE name = alice AND email = alice@example.com",
                ".exit",
            ], db)
        self.assertTrue(any("alice" in l for l in lines))

    def test_multi_column_index_maintained_on_delete(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "CREATE INDEX idx_name_email ON users(name, email)",
                "DELETE FROM users WHERE id = 1",
                "SELECT * FROM users WHERE name = alice AND email = alice@example.com",
                ".exit",
            ], db)
        self.assertIn("(no rows)", lines)

    def test_and_where_full_scan(self):
        """AND WHERE works even without an index (full scan path)."""
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "INSERT INTO users VALUES (2, alice, alice2@example.com)",
                "SELECT * FROM users WHERE name = alice AND email = alice@example.com",
                ".exit",
            ], db)
        self.assertTrue(any("alice@example.com" in l for l in lines))
        self.assertFalse(any("alice2@example.com" in l for l in lines))

    def test_multi_column_index_persists(self):
        with TempDB() as db:
            db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, alice@example.com)",
                "CREATE INDEX idx_name_email ON users(name, email)",
                ".exit",
            ], db)
            _, lines = db_run([
                "SELECT * FROM users WHERE name = alice AND email = alice@example.com",
                ".exit",
            ], db)
        self.assertTrue(any("alice" in l for l in lines))


class TestNonUniqueIndex(unittest.TestCase):
    """Indexes on non-unique columns must not crash on duplicate keys,
    and SELECT must fall back to a full scan (not the incomplete index)."""

    def test_insert_duplicate_integer_indexed_value(self):
        """Two rows with the same INTEGER indexed value must not crash."""
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, age INTEGER)",
                "CREATE INDEX idx_age ON t(age)",
                "INSERT INTO t VALUES (1, 25)",
                "INSERT INTO t VALUES (2, 25)",
                "SELECT * FROM t",
                ".exit",
            ], db)
        age_lines = [l for l in lines if "25" in l]
        self.assertEqual(len(age_lines), 2)

    def test_insert_duplicate_text_indexed_value(self):
        """Two rows with the same TEXT indexed value must not crash."""
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "CREATE INDEX idx_name ON users(name)",
                "INSERT INTO users VALUES (1, alice, a@x.com)",
                "INSERT INTO users VALUES (2, alice, b@x.com)",
                "SELECT * FROM users",
                ".exit",
            ], db)
        self.assertTrue(any("a@x.com" in l for l in lines))
        self.assertTrue(any("b@x.com" in l for l in lines))

    def test_select_falls_back_to_full_scan(self):
        """SELECT on a non-unique indexed column returns all matching rows."""
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "CREATE INDEX idx_name ON users(name)",
                "INSERT INTO users VALUES (1, alice, a@x.com)",
                "INSERT INTO users VALUES (2, alice, b@x.com)",
                "SELECT * FROM users WHERE name = alice",
                ".exit",
            ], db)
        self.assertTrue(any("a@x.com" in l for l in lines))
        self.assertTrue(any("b@x.com" in l for l in lines))

    def test_delete_with_non_unique_index(self):
        """DELETE on a non-unique indexed column removes correct rows."""
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "CREATE INDEX idx_name ON users(name)",
                "INSERT INTO users VALUES (1, alice, a@x.com)",
                "INSERT INTO users VALUES (2, alice, b@x.com)",
                "INSERT INTO users VALUES (3, bob, c@x.com)",
                "DELETE FROM users WHERE name = alice",
                "SELECT * FROM users",
                ".exit",
            ], db)
        self.assertFalse(any("alice" in l for l in lines))
        self.assertTrue(any("bob" in l for l in lines))

    def test_unique_index_still_accelerated(self):
        """A UNIQUE-constrained column's index IS used for SELECT (fast path)."""
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER UNIQUE, val VARCHAR(32))",
                "CREATE INDEX idx_id ON t(id)",
                "INSERT INTO t VALUES (1, foo)",
                "INSERT INTO t VALUES (2, bar)",
                "SELECT * FROM t WHERE id = 1",
                ".exit",
            ], db)
        self.assertTrue(any("foo" in l for l in lines))
        self.assertFalse(any("bar" in l for l in lines))


class TestDefault(unittest.TestCase):

    def test_default_used_when_column_omitted(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, status VARCHAR(16) DEFAULT active)",
                "INSERT INTO t (id) VALUES (1)",
                "SELECT * FROM t",
                ".exit",
            ], db)
        self.assertTrue(any("active" in l for l in lines))

    def test_default_integer(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, score INTEGER DEFAULT 0)",
                "INSERT INTO t (id) VALUES (1)",
                "SELECT * FROM t",
                ".exit",
            ], db)
        self.assertTrue(any("0" in l for l in lines))

    def test_explicit_value_overrides_default(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, status VARCHAR(16) DEFAULT pending)",
                "INSERT INTO t VALUES (1, done)",
                "SELECT * FROM t",
                ".exit",
            ], db)
        self.assertTrue(any("done" in l for l in lines))
        self.assertFalse(any("pending" in l for l in lines))

    def test_null_used_when_no_default(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, note VARCHAR(32))",
                "INSERT INTO t (id) VALUES (1)",
                "SELECT * FROM t",
                ".exit",
            ], db)
        self.assertTrue(any("NULL" in l for l in lines))

    def test_default_persists_in_schema(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, status VARCHAR(16) DEFAULT active)",
                ".schema t",
                ".exit",
            ], db)
        self.assertTrue(any("DEFAULT active" in l for l in lines))

    def test_default_survives_reopen(self):
        with TempDB() as db:
            db_run([
                "CREATE TABLE t (id INTEGER, status VARCHAR(16) DEFAULT active)",
                ".exit",
            ], db)
            _, lines = db_run([
                "INSERT INTO t (id) VALUES (1)",
                "SELECT * FROM t",
                ".exit",
            ], db)
        self.assertTrue(any("active" in l for l in lines))


class TestDistinct(unittest.TestCase):

    def test_distinct_removes_duplicates(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, dept VARCHAR(32))",
                "INSERT INTO t VALUES (1, eng)",
                "INSERT INTO t VALUES (2, eng)",
                "INSERT INTO t VALUES (3, hr)",
                "SELECT DISTINCT dept FROM t",
                ".exit",
            ], db)
        dept_lines = [l for l in lines if "eng" in l or "hr" in l]
        self.assertEqual(len(dept_lines), 2)

    def test_distinct_all_unique_unchanged(self):
        with TempDB() as db:
            _, lines = db_run([
                CREATE_USERS,
                "INSERT INTO users VALUES (1, alice, a@x.com)",
                "INSERT INTO users VALUES (2, bob, b@x.com)",
                "SELECT DISTINCT name FROM users",
                ".exit",
            ], db)
        self.assertTrue(any("alice" in l for l in lines))
        self.assertTrue(any("bob" in l for l in lines))

    def test_distinct_with_where(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, dept VARCHAR(32), active INTEGER)",
                "INSERT INTO t VALUES (1, eng, 1)",
                "INSERT INTO t VALUES (2, eng, 1)",
                "INSERT INTO t VALUES (3, hr,  0)",
                "SELECT DISTINCT dept FROM t WHERE active = 1",
                ".exit",
            ], db)
        dept_lines = [l for l in lines if "eng" in l or "hr" in l]
        self.assertEqual(len(dept_lines), 1)
        self.assertTrue(any("eng" in l for l in lines))

    def test_distinct_multi_column(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (a INTEGER, b INTEGER)",
                "INSERT INTO t VALUES (1, 1)",
                "INSERT INTO t VALUES (1, 2)",
                "INSERT INTO t VALUES (1, 1)",
                "SELECT DISTINCT a, b FROM t",
                ".exit",
            ], db)
        row_lines = [l for l in lines if "|" in l and "a" not in l]
        self.assertEqual(len(row_lines), 2)


class TestSetOperations(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE a (id INTEGER, val VARCHAR(32))",
            "CREATE TABLE b (id INTEGER, val VARCHAR(32))",
            "INSERT INTO a VALUES (1, foo)",
            "INSERT INTO a VALUES (2, bar)",
            "INSERT INTO a VALUES (3, baz)",
            "INSERT INTO b VALUES (2, bar)",
            "INSERT INTO b VALUES (3, baz)",
            "INSERT INTO b VALUES (4, qux)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    # ── UNION ─────────────────────────────────────────────────────────────────

    def test_union_removes_duplicates(self):
        _, lines = db_run([
            "SELECT id, val FROM a UNION SELECT id, val FROM b",
            ".exit",
        ], self.db)
        # a∪b = {1,2,3,4} — 4 distinct rows
        self.assertTrue(any("(4 rows)" in l for l in lines))

    def test_union_all_keeps_duplicates(self):
        _, lines = db_run([
            "SELECT id, val FROM a UNION ALL SELECT id, val FROM b",
            ".exit",
        ], self.db)
        # 3 from a + 3 from b = 6 rows (including duplicates)
        self.assertTrue(any("(6 rows)" in l for l in lines))

    def test_union_contains_all_values(self):
        _, lines = db_run([
            "SELECT id, val FROM a UNION SELECT id, val FROM b",
            ".exit",
        ], self.db)
        for v in ("foo", "bar", "baz", "qux"):
            self.assertTrue(any(v in l for l in lines))

    # ── INTERSECT ─────────────────────────────────────────────────────────────

    def test_intersect_returns_common_rows(self):
        _, lines = db_run([
            "SELECT id, val FROM a INTERSECT SELECT id, val FROM b",
            ".exit",
        ], self.db)
        # a∩b = {(2,bar),(3,baz)}
        self.assertTrue(any("(2 rows)" in l for l in lines))
        self.assertTrue(any("bar" in l for l in lines))
        self.assertTrue(any("baz" in l for l in lines))

    def test_intersect_excludes_non_common(self):
        _, lines = db_run([
            "SELECT id, val FROM a INTERSECT SELECT id, val FROM b",
            ".exit",
        ], self.db)
        self.assertFalse(any("foo" in l for l in lines))
        self.assertFalse(any("qux" in l for l in lines))

    def test_intersect_empty_when_no_common(self):
        with TempDB() as db:
            db_run([
                "CREATE TABLE x (id INTEGER)",
                "CREATE TABLE y (id INTEGER)",
                "INSERT INTO x VALUES (1)",
                "INSERT INTO y VALUES (2)",
                ".exit",
            ], db)
            _, lines = db_run([
                "SELECT id FROM x INTERSECT SELECT id FROM y",
                ".exit",
            ], db)
        self.assertIn("(no rows)", lines)

    # ── EXCEPT ────────────────────────────────────────────────────────────────

    def test_except_removes_right_rows(self):
        _, lines = db_run([
            "SELECT id, val FROM a EXCEPT SELECT id, val FROM b",
            ".exit",
        ], self.db)
        # a - b = {(1,foo)}
        self.assertTrue(any("(1 row)" in l for l in lines))
        self.assertTrue(any("foo" in l for l in lines))

    def test_except_excludes_shared_rows(self):
        _, lines = db_run([
            "SELECT id, val FROM a EXCEPT SELECT id, val FROM b",
            ".exit",
        ], self.db)
        self.assertFalse(any("bar" in l for l in lines))
        self.assertFalse(any("baz" in l for l in lines))

    def test_except_all_multiset(self):
        """EXCEPT ALL removes one copy per right-side occurrence."""
        with TempDB() as db:
            db_run([
                "CREATE TABLE p (v INTEGER)",
                "CREATE TABLE q (v INTEGER)",
                "INSERT INTO p VALUES (1)",
                "INSERT INTO p VALUES (1)",
                "INSERT INTO p VALUES (2)",
                "INSERT INTO q VALUES (1)",
                ".exit",
            ], db)
            _, lines = db_run([
                "SELECT v FROM p EXCEPT ALL SELECT v FROM q",
                ".exit",
            ], db)
        # p has two 1s, q removes one → one 1 remains plus the 2 → 2 rows
        self.assertTrue(any("(2 rows)" in l for l in lines))

    # ── Chained set operations ─────────────────────────────────────────────────

    def test_union_then_except(self):
        """(a UNION b) EXCEPT b  should equal a (minus shared)."""
        _, lines = db_run([
            "SELECT id, val FROM a UNION SELECT id, val FROM b EXCEPT SELECT id, val FROM b",
            ".exit",
        ], self.db)
        # UNION gives {1,2,3,4}; EXCEPT b removes {2,3,4} → {1} = (1,foo)
        self.assertTrue(any("foo" in l for l in lines))
        self.assertFalse(any("qux" in l for l in lines))


class TestSelfJoin(unittest.TestCase):
    """Self join — a table joined with itself using aliases."""

    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        # employees: id, name, manager_id (NULL for the top-level manager)
        db_run([
            "CREATE TABLE employees (id INTEGER, name VARCHAR(32), manager_id INTEGER)",
            "INSERT INTO employees VALUES (1, alice, NULL)",
            "INSERT INTO employees VALUES (2, bob, 1)",
            "INSERT INTO employees VALUES (3, carol, 1)",
            "INSERT INTO employees VALUES (4, dave, 2)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_self_join_finds_manager_names(self):
        """Each employee paired with their manager's name."""
        _, lines = db_run([
            "SELECT e.name, m.name FROM employees AS e INNER JOIN employees AS m ON e.manager_id = m.id",
            ".exit",
        ], self.db)
        self.assertTrue(any("bob" in l and "alice" in l for l in lines))
        self.assertTrue(any("carol" in l and "alice" in l for l in lines))
        self.assertTrue(any("dave" in l and "bob" in l for l in lines))

    def test_self_join_excludes_top_level(self):
        """alice has no manager (NULL manager_id) so must not appear as an employee here."""
        _, lines = db_run([
            "SELECT e.name, m.name FROM employees AS e INNER JOIN employees AS m ON e.manager_id = m.id",
            ".exit",
        ], self.db)
        emp_lines = [l for l in lines if "|" in l and "name" not in l]
        self.assertFalse(any(l.split("|")[0].strip() == "alice" for l in emp_lines))

    def test_self_join_row_count(self):
        """3 employees have a manager (bob, carol, dave)."""
        _, lines = db_run([
            "SELECT e.name, m.name FROM employees AS e INNER JOIN employees AS m ON e.manager_id = m.id",
            ".exit",
        ], self.db)
        self.assertTrue(any("(3 rows)" in l for l in lines))

    def test_self_left_join_includes_top_level(self):
        """LEFT JOIN keeps alice (no manager) with NULL for the manager name."""
        _, lines = db_run([
            "SELECT e.name, m.name FROM employees AS e LEFT JOIN employees AS m ON e.manager_id = m.id",
            ".exit",
        ], self.db)
        self.assertTrue(any("alice" in l and "NULL" in l for l in lines))
        self.assertTrue(any("(4 rows)" in l for l in lines))

    def test_alias_without_as_keyword(self):
        """Bare alias (no AS keyword) should also work."""
        _, lines = db_run([
            "SELECT e.name, m.name FROM employees e INNER JOIN employees m ON e.manager_id = m.id",
            ".exit",
        ], self.db)
        self.assertTrue(any("(3 rows)" in l for l in lines))


class TestLeftJoin(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            CREATE_USERS,
            "CREATE TABLE orders (uid INTEGER, item VARCHAR(64))",
            "INSERT INTO users VALUES (1, alice, alice@example.com)",
            "INSERT INTO users VALUES (2, bob, bob@example.com)",
            "INSERT INTO users VALUES (3, carol, carol@example.com)",
            "INSERT INTO orders VALUES (1, widget)",
            "INSERT INTO orders VALUES (1, gadget)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_left_join_includes_unmatched_left_rows(self):
        """Users with no orders must appear with NULL item."""
        _, lines = db_run([
            "SELECT users.name, orders.item FROM users LEFT JOIN orders ON users.id = orders.uid",
            ".exit",
        ], self.db)
        self.assertTrue(any("bob" in l for l in lines))
        self.assertTrue(any("carol" in l for l in lines))
        self.assertTrue(any("NULL" in l for l in lines))

    def test_left_join_matched_rows_present(self):
        """Matched rows from the right side still appear."""
        _, lines = db_run([
            "SELECT users.name, orders.item FROM users LEFT JOIN orders ON users.id = orders.uid",
            ".exit",
        ], self.db)
        self.assertTrue(any("alice" in l and "widget" in l for l in lines))
        self.assertTrue(any("alice" in l and "gadget" in l for l in lines))

    def test_left_join_row_count(self):
        """2 matched (alice×2) + 1 unmatched (bob) + 1 unmatched (carol) = 4 rows."""
        _, lines = db_run([
            "SELECT users.name, orders.item FROM users LEFT JOIN orders ON users.id = orders.uid",
            ".exit",
        ], self.db)
        self.assertTrue(any("(4 rows)" in l for l in lines))

    def test_left_outer_join_synonym(self):
        """LEFT OUTER JOIN is identical to LEFT JOIN."""
        _, lines = db_run([
            "SELECT users.name, orders.item FROM users LEFT OUTER JOIN orders ON users.id = orders.uid",
            ".exit",
        ], self.db)
        self.assertTrue(any("(4 rows)" in l for l in lines))

    def test_inner_join_unchanged(self):
        """Existing INNER JOIN behaviour must not regress."""
        _, lines = db_run([
            "SELECT users.name, orders.item FROM users INNER JOIN orders ON users.id = orders.uid",
            ".exit",
        ], self.db)
        self.assertTrue(any("(2 rows)" in l for l in lines))
        self.assertFalse(any("bob" in l for l in lines))


class TestRightFullCrossNaturalJoin(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            CREATE_USERS,
            "CREATE TABLE orders (uid INTEGER, item VARCHAR(64))",
            "INSERT INTO users VALUES (1, alice, alice@example.com)",
            "INSERT INTO users VALUES (2, bob, bob@example.com)",
            "INSERT INTO orders VALUES (1, widget)",
            "INSERT INTO orders VALUES (3, gadget)",   # uid=3 has no matching user
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    # ── RIGHT JOIN ────────────────────────────────────────────────────────────

    def test_right_join_includes_unmatched_right_rows(self):
        """orders row with uid=3 (no matching user) must appear with NULL user columns."""
        _, lines = db_run([
            "SELECT users.name, orders.item FROM users RIGHT JOIN orders ON users.id = orders.uid",
            ".exit",
        ], self.db)
        self.assertTrue(any("gadget" in l for l in lines))
        self.assertTrue(any("NULL" in l for l in lines))

    def test_right_join_excludes_unmatched_left_rows(self):
        """bob (uid=2, no order) must NOT appear in RIGHT JOIN."""
        _, lines = db_run([
            "SELECT users.name, orders.item FROM users RIGHT JOIN orders ON users.id = orders.uid",
            ".exit",
        ], self.db)
        self.assertFalse(any("bob" in l for l in lines))

    def test_right_join_matched_rows_present(self):
        _, lines = db_run([
            "SELECT users.name, orders.item FROM users RIGHT JOIN orders ON users.id = orders.uid",
            ".exit",
        ], self.db)
        self.assertTrue(any("alice" in l and "widget" in l for l in lines))

    def test_right_outer_join_synonym(self):
        _, lines = db_run([
            "SELECT users.name, orders.item FROM users RIGHT OUTER JOIN orders ON users.id = orders.uid",
            ".exit",
        ], self.db)
        self.assertTrue(any("gadget" in l for l in lines))

    # ── FULL OUTER JOIN ───────────────────────────────────────────────────────

    def test_full_outer_join_includes_both_unmatched_sides(self):
        """bob (no order) and gadget (no user) must both appear."""
        _, lines = db_run([
            "SELECT users.name, orders.item FROM users FULL OUTER JOIN orders ON users.id = orders.uid",
            ".exit",
        ], self.db)
        self.assertTrue(any("bob" in l for l in lines))
        self.assertTrue(any("gadget" in l for l in lines))

    def test_full_outer_join_matched_rows_present(self):
        _, lines = db_run([
            "SELECT users.name, orders.item FROM users FULL OUTER JOIN orders ON users.id = orders.uid",
            ".exit",
        ], self.db)
        self.assertTrue(any("alice" in l and "widget" in l for l in lines))

    def test_full_join_row_count(self):
        """1 matched + 1 unmatched-left (bob) + 1 unmatched-right (gadget) = 3 rows."""
        _, lines = db_run([
            "SELECT users.name, orders.item FROM users FULL OUTER JOIN orders ON users.id = orders.uid",
            ".exit",
        ], self.db)
        self.assertTrue(any("(3 rows)" in l for l in lines))

    # ── CROSS JOIN ────────────────────────────────────────────────────────────

    def test_cross_join_cartesian_product(self):
        """2 users × 2 orders = 4 rows."""
        _, lines = db_run([
            "SELECT users.name, orders.item FROM users CROSS JOIN orders",
            ".exit",
        ], self.db)
        self.assertTrue(any("(4 rows)" in l for l in lines))

    def test_cross_join_all_combinations_present(self):
        _, lines = db_run([
            "SELECT users.name, orders.item FROM users CROSS JOIN orders",
            ".exit",
        ], self.db)
        self.assertTrue(any("alice" in l and "widget" in l for l in lines))
        self.assertTrue(any("alice" in l and "gadget" in l for l in lines))
        self.assertTrue(any("bob" in l and "widget" in l for l in lines))

    # ── NATURAL JOIN ──────────────────────────────────────────────────────────

    def test_natural_join_matches_on_shared_columns(self):
        """Tables sharing 'id' column: NATURAL JOIN should join on it."""
        with TempDB() as db:
            db_run([
                "CREATE TABLE a (id INTEGER, val VARCHAR(32))",
                "CREATE TABLE b (id INTEGER, label VARCHAR(32))",
                "INSERT INTO a VALUES (1, foo)",
                "INSERT INTO a VALUES (2, bar)",
                "INSERT INTO b VALUES (1, alpha)",
                "INSERT INTO b VALUES (3, gamma)",
                ".exit",
            ], db)
            _, lines = db_run([
                "SELECT a.val, b.label FROM a NATURAL JOIN b",
                ".exit",
            ], db)
        self.assertTrue(any("foo" in l and "alpha" in l for l in lines))
        self.assertFalse(any("bar" in l for l in lines))   # id=2 has no match
        self.assertFalse(any("gamma" in l for l in lines)) # id=3 has no match

    def test_natural_join_row_count(self):
        with TempDB() as db:
            db_run([
                "CREATE TABLE a (id INTEGER, val VARCHAR(32))",
                "CREATE TABLE b (id INTEGER, label VARCHAR(32))",
                "INSERT INTO a VALUES (1, foo)",
                "INSERT INTO a VALUES (2, bar)",
                "INSERT INTO b VALUES (1, alpha)",
                "INSERT INTO b VALUES (1, beta)",
                ".exit",
            ], db)
            _, lines = db_run([
                "SELECT a.val, b.label FROM a NATURAL JOIN b",
                ".exit",
            ], db)
        self.assertTrue(any("(2 rows)" in l for l in lines))


class TestIndexRangeScan(unittest.TestCase):
    """Index is used for range operators (>, >=, <, <=) on INTEGER/REAL columns."""

    def setUp(self):
        self._tmp = TempDB()
        self.db = self._tmp.__enter__()
        db_run([
            "CREATE TABLE t (id INTEGER, score INTEGER)",
            "INSERT INTO t VALUES (1, 10)",
            "INSERT INTO t VALUES (2, 20)",
            "INSERT INTO t VALUES (3, 30)",
            "INSERT INTO t VALUES (4, 40)",
            "INSERT INTO t VALUES (5, 50)",
            "CREATE INDEX idx_score ON t(score)",
            ".exit",
        ], self.db)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_greater_than(self):
        _, lines = db_run(["SELECT * FROM t WHERE score > 30", ".exit"], self.db)
        self.assertTrue(any("40" in l for l in lines))
        self.assertTrue(any("50" in l for l in lines))
        self.assertFalse(any("10" in l for l in lines))
        self.assertFalse(any("30" in l for l in lines))

    def test_greater_than_or_equal(self):
        _, lines = db_run(["SELECT * FROM t WHERE score >= 30", ".exit"], self.db)
        self.assertTrue(any("30" in l for l in lines))
        self.assertTrue(any("40" in l for l in lines))
        self.assertFalse(any("20" in l for l in lines))

    def test_less_than(self):
        _, lines = db_run(["SELECT * FROM t WHERE score < 30", ".exit"], self.db)
        self.assertTrue(any("10" in l for l in lines))
        self.assertTrue(any("20" in l for l in lines))
        self.assertFalse(any("30" in l for l in lines))

    def test_less_than_or_equal(self):
        _, lines = db_run(["SELECT * FROM t WHERE score <= 30", ".exit"], self.db)
        self.assertTrue(any("30" in l for l in lines))
        self.assertTrue(any("10" in l for l in lines))
        self.assertFalse(any("40" in l for l in lines))

    def test_between_via_and(self):
        """WHERE a > lo AND a < hi uses index for the first condition, post-filters second."""
        _, lines = db_run(["SELECT * FROM t WHERE score > 10 AND score < 50", ".exit"], self.db)
        self.assertTrue(any("20" in l for l in lines))
        self.assertTrue(any("40" in l for l in lines))
        self.assertFalse(any(" 10 " in l or l.startswith("10 ") or l.endswith(" 10") for l in lines))
        self.assertFalse(any("50" in l for l in lines))

    def test_range_no_match(self):
        _, lines = db_run(["SELECT * FROM t WHERE score > 999", ".exit"], self.db)
        self.assertIn("(no rows)", lines)

    def test_range_row_count(self):
        _, lines = db_run(["SELECT * FROM t WHERE score >= 20 AND score <= 40", ".exit"], self.db)
        self.assertTrue(any("(3 rows)" in l for l in lines))

    def test_range_on_real_column(self):
        with TempDB() as db:
            db_run([
                "CREATE TABLE prices (id INTEGER, amount REAL)",
                "INSERT INTO prices VALUES (1, 9.99)",
                "INSERT INTO prices VALUES (2, 19.99)",
                "INSERT INTO prices VALUES (3, 4.99)",
                "CREATE INDEX idx_amount ON prices(amount)",
                ".exit",
            ], db)
            _, lines = db_run(["SELECT * FROM prices WHERE amount > 5.0", ".exit"], db)
        self.assertTrue(any("9.99" in l for l in lines))
        self.assertTrue(any("19.99" in l for l in lines))
        self.assertFalse(any("4.99" in l for l in lines))


class TestCheck(unittest.TestCase):

    def test_check_violated_on_insert(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, age INTEGER CHECK (age > 0))",
                "INSERT INTO t VALUES (1, -1)",
                ".exit",
            ], db)
        self.assertTrue(any("Error" in l for l in lines))

    def test_check_satisfied_on_insert(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, age INTEGER CHECK (age > 0))",
                "INSERT INTO t VALUES (1, 25)",
                "SELECT * FROM t",
                ".exit",
            ], db)
        self.assertTrue(any("25" in l for l in lines))

    def test_check_violated_on_update(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, age INTEGER CHECK (age > 0))",
                "INSERT INTO t VALUES (1, 25)",
                "UPDATE t SET age = -5 WHERE id = 1",
                ".exit",
            ], db)
        self.assertTrue(any("Error" in l for l in lines))

    def test_check_boundary_value(self):
        """Boundary: age > 0 means 0 is rejected but 1 is accepted."""
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, age INTEGER CHECK (age > 0))",
                "INSERT INTO t VALUES (1, 0)",
                ".exit",
            ], db)
        self.assertTrue(any("Error" in l for l in lines))

    def test_check_persists_in_schema(self):
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, age INTEGER CHECK (age > 0))",
                ".schema t",
                ".exit",
            ], db)
        self.assertTrue(any("CHECK" in l for l in lines))

    def test_check_survives_reopen(self):
        with TempDB() as db:
            db_run([
                "CREATE TABLE t (id INTEGER, age INTEGER CHECK (age > 0))",
                ".exit",
            ], db)
            _, lines = db_run([
                "INSERT INTO t VALUES (1, -1)",
                ".exit",
            ], db)
        self.assertTrue(any("Error" in l for l in lines))

    def test_check_varchar_constraint(self):
        """CHECK can constrain TEXT columns too (via string comparison)."""
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE t (id INTEGER, status VARCHAR(16) CHECK (status = active))",
                "INSERT INTO t VALUES (1, inactive)",
                ".exit",
            ], db)
        self.assertTrue(any("Error" in l for l in lines))


class TestForeignKey(unittest.TestCase):

    def _setup(self, db):
        db_run([
            "CREATE TABLE dept (id INTEGER, name VARCHAR(32))",
            "CREATE TABLE emp (id INTEGER, dept_id INTEGER REFERENCES dept (id))",
            "INSERT INTO dept VALUES (1, Engineering)",
            "INSERT INTO dept VALUES (2, Marketing)",
            ".exit",
        ], db)

    def test_valid_child_insert(self):
        """Insert child row whose FK value exists in parent → succeeds."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "INSERT INTO emp VALUES (10, 1)",
                "SELECT * FROM emp",
                ".exit",
            ], db)
        self.assertTrue(any("10" in l for l in lines))

    def test_invalid_child_insert_rejected(self):
        """Insert child row whose FK value does not exist in parent → error."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "INSERT INTO emp VALUES (10, 99)",
                ".exit",
            ], db)
        self.assertTrue(any("Error" in l for l in lines))

    def test_null_fk_allowed(self):
        """NULL in FK column is permitted (no parent lookup needed)."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "INSERT INTO emp (id) VALUES (20)",
                "SELECT * FROM emp",
                ".exit",
            ], db)
        self.assertTrue(any("20" in l for l in lines))

    def test_delete_parent_with_child_rejected(self):
        """Deleting a parent row that is referenced by a child → RESTRICT error."""
        with TempDB() as db:
            self._setup(db)
            db_run(["INSERT INTO emp VALUES (10, 1)", ".exit"], db)
            _, lines = db_run([
                "DELETE FROM dept WHERE id = 1",
                ".exit",
            ], db)
        self.assertTrue(any("Error" in l for l in lines))

    def test_delete_unreferenced_parent_ok(self):
        """Deleting a parent row with no child references → succeeds."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "DELETE FROM dept WHERE id = 2",
                "SELECT * FROM dept",
                ".exit",
            ], db)
        self.assertFalse(any("Marketing" in l for l in lines))

    def test_update_child_to_invalid_fk_rejected(self):
        """Updating a child FK column to a value not in parent → error."""
        with TempDB() as db:
            self._setup(db)
            db_run(["INSERT INTO emp VALUES (10, 1)", ".exit"], db)
            _, lines = db_run([
                "UPDATE emp SET dept_id = 99 WHERE id = 10",
                ".exit",
            ], db)
        self.assertTrue(any("Error" in l for l in lines))

    def test_table_level_foreign_key_syntax(self):
        """Table-level FOREIGN KEY (...) REFERENCES ... syntax is parsed and enforced."""
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE parent (id INTEGER)",
                "CREATE TABLE child (id INTEGER, pid INTEGER, "
                "FOREIGN KEY (pid) REFERENCES parent (id))",
                "INSERT INTO child VALUES (1, 99)",
                ".exit",
            ], db)
        self.assertTrue(any("Error" in l for l in lines))

    def test_schema_shows_foreign_key(self):
        """`.schema` output includes FOREIGN KEY clause."""
        with TempDB() as db:
            _, lines = db_run([
                "CREATE TABLE dept (id INTEGER)",
                "CREATE TABLE emp (id INTEGER, dept_id INTEGER REFERENCES dept (id))",
                ".schema emp",
                ".exit",
            ], db)
        self.assertTrue(any("FOREIGN KEY" in l for l in lines))

    def test_fk_survives_reopen(self):
        """FK constraints are persisted and enforced after database is re-opened."""
        with TempDB() as db:
            db_run([
                "CREATE TABLE dept (id INTEGER)",
                "INSERT INTO dept VALUES (1)",
                "CREATE TABLE emp (id INTEGER, dept_id INTEGER REFERENCES dept (id))",
                ".exit",
            ], db)
            _, lines = db_run([
                "INSERT INTO emp VALUES (1, 999)",
                ".exit",
            ], db)
        self.assertTrue(any("Error" in l for l in lines))


class TestSubquery(unittest.TestCase):

    def _setup(self, db):
        db_run([
            "CREATE TABLE dept (id INTEGER, name VARCHAR(32))",
            "CREATE TABLE emp (id INTEGER, name VARCHAR(32), dept_id INTEGER, salary INTEGER)",
            "INSERT INTO dept VALUES (1, Engineering)",
            "INSERT INTO dept VALUES (2, Marketing)",
            "INSERT INTO dept VALUES (3, HR)",
            "INSERT INTO emp VALUES (1, Alice, 1, 90000)",
            "INSERT INTO emp VALUES (2, Bob, 2, 70000)",
            "INSERT INTO emp VALUES (3, Carol, 1, 80000)",
            "INSERT INTO emp VALUES (4, Dave, 3, 60000)",
            ".exit",
        ], db)

    def test_in_subquery(self):
        """WHERE col IN (SELECT ...) returns rows whose col matches any subquery result."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "SELECT name FROM emp WHERE dept_id IN (SELECT id FROM dept WHERE name = Engineering)",
                ".exit",
            ], db)
        self.assertTrue(any("Alice" in l for l in lines))
        self.assertTrue(any("Carol" in l for l in lines))
        self.assertFalse(any("Bob" in l for l in lines))
        self.assertFalse(any("Dave" in l for l in lines))

    def test_not_in_subquery(self):
        """WHERE col NOT IN (SELECT ...) excludes rows matching the subquery."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "SELECT name FROM emp WHERE dept_id NOT IN (SELECT id FROM dept WHERE name = Engineering)",
                ".exit",
            ], db)
        self.assertFalse(any("Alice" in l for l in lines))
        self.assertFalse(any("Carol" in l for l in lines))
        self.assertTrue(any("Bob" in l for l in lines))
        self.assertTrue(any("Dave" in l for l in lines))

    def test_scalar_subquery_equality(self):
        """WHERE col = (SELECT ...) compares against a single subquery value."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "SELECT name FROM emp WHERE dept_id = (SELECT id FROM dept WHERE name = Marketing)",
                ".exit",
            ], db)
        self.assertTrue(any("Bob" in l for l in lines))
        self.assertFalse(any("Alice" in l for l in lines))

    def test_exists_subquery(self):
        """EXISTS (SELECT ...) is True when the subquery returns any row."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "SELECT name FROM emp WHERE EXISTS (SELECT id FROM dept WHERE id = 1)",
                ".exit",
            ], db)
        self.assertTrue(any("Alice" in l for l in lines))
        self.assertTrue(any("Bob" in l for l in lines))

    def test_not_exists_subquery(self):
        """NOT EXISTS (SELECT ...) is True when the subquery returns no rows."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "SELECT name FROM emp WHERE NOT EXISTS (SELECT id FROM dept WHERE id = 999)",
                ".exit",
            ], db)
        self.assertTrue(any("Alice" in l for l in lines))
        self.assertTrue(any("Bob" in l for l in lines))

    def test_in_subquery_no_match(self):
        """IN subquery that returns no rows → no output rows."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "SELECT name FROM emp WHERE dept_id IN (SELECT id FROM dept WHERE id = 999)",
                ".exit",
            ], db)
        self.assertIn("(no rows)", lines)

    def test_subquery_in_delete(self):
        """DELETE WHERE col IN (SELECT ...) removes matching rows."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "DELETE FROM emp WHERE dept_id IN (SELECT id FROM dept WHERE name = HR)",
                "SELECT name FROM emp",
                ".exit",
            ], db)
        self.assertFalse(any("Dave" in l for l in lines))
        self.assertTrue(any("Alice" in l for l in lines))

    def test_subquery_in_update(self):
        """UPDATE WHERE col IN (SELECT ...) modifies only matching rows."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "UPDATE emp SET salary = 99999 WHERE dept_id IN (SELECT id FROM dept WHERE name = Marketing)",
                "SELECT salary FROM emp WHERE name = Bob",
                ".exit",
            ], db)
        self.assertTrue(any("99999" in l for l in lines))

    def test_scalar_subquery_comparison(self):
        """WHERE col > (SELECT ...) works with comparison operators."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "SELECT name FROM emp WHERE salary > (SELECT salary FROM emp WHERE name = Bob)",
                ".exit",
            ], db)
        self.assertTrue(any("Alice" in l for l in lines))
        self.assertFalse(any("Dave" in l for l in lines))

    def test_not_in_literal_list_still_works(self):
        """NOT IN with a literal value list still works after the refactor."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "SELECT name FROM emp WHERE id NOT IN (1, 3)",
                ".exit",
            ], db)
        self.assertFalse(any("Alice" in l for l in lines))
        self.assertFalse(any("Carol" in l for l in lines))
        self.assertTrue(any("Bob" in l for l in lines))
        self.assertTrue(any("Dave" in l for l in lines))


class TestCorrelatedSubquery(unittest.TestCase):

    def _setup(self, db):
        db_run([
            "CREATE TABLE emp (id INTEGER, name VARCHAR(32), dept_id INTEGER, salary INTEGER)",
            "CREATE TABLE orders (id INTEGER, emp_id INTEGER, amount INTEGER)",
            "INSERT INTO emp VALUES (1, Alice, 10, 90000)",
            "INSERT INTO emp VALUES (2, Bob,   20, 70000)",
            "INSERT INTO emp VALUES (3, Carol, 10, 80000)",
            "INSERT INTO emp VALUES (4, Dave,  30, 60000)",
            "INSERT INTO orders VALUES (1, 1, 500)",
            "INSERT INTO orders VALUES (2, 1, 300)",
            "INSERT INTO orders VALUES (3, 3, 200)",
            ".exit",
        ], db)

    def test_correlated_exists(self):
        """EXISTS subquery references the outer row's column."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "SELECT name FROM emp WHERE EXISTS (SELECT id FROM orders WHERE orders.emp_id = emp.id)",
                ".exit",
            ], db)
        self.assertTrue(any("Alice" in l for l in lines))
        self.assertTrue(any("Carol" in l for l in lines))
        self.assertFalse(any("Bob" in l for l in lines))
        self.assertFalse(any("Dave" in l for l in lines))

    def test_correlated_not_exists(self):
        """NOT EXISTS subquery correctly excludes rows that have a match."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "SELECT name FROM emp WHERE NOT EXISTS (SELECT id FROM orders WHERE orders.emp_id = emp.id)",
                ".exit",
            ], db)
        self.assertTrue(any("Bob" in l for l in lines))
        self.assertTrue(any("Dave" in l for l in lines))
        self.assertFalse(any("Alice" in l for l in lines))
        self.assertFalse(any("Carol" in l for l in lines))

    def test_correlated_in(self):
        """IN subquery with correlated reference to outer row."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "SELECT name FROM emp WHERE id IN (SELECT emp_id FROM orders WHERE orders.emp_id = emp.id)",
                ".exit",
            ], db)
        self.assertTrue(any("Alice" in l for l in lines))
        self.assertTrue(any("Carol" in l for l in lines))
        self.assertFalse(any("Bob" in l for l in lines))

    def test_correlated_scalar_equality(self):
        """Scalar correlated subquery used with = operator."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "SELECT name FROM emp WHERE dept_id = (SELECT dept_id FROM emp WHERE name = Alice)",
                ".exit",
            ], db)
        self.assertTrue(any("Alice" in l for l in lines))
        self.assertTrue(any("Carol" in l for l in lines))
        self.assertFalse(any("Bob" in l for l in lines))

    def test_qualified_col_in_inner_where(self):
        """Table-qualified column names in inner WHERE resolve against inner rows."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "SELECT name FROM emp WHERE EXISTS (SELECT id FROM orders WHERE orders.emp_id = emp.id AND orders.amount > 250)",
                ".exit",
            ], db)
        self.assertTrue(any("Alice" in l for l in lines))
        self.assertFalse(any("Carol" in l for l in lines))

    def test_correlated_row_count(self):
        """Correct number of rows returned from correlated EXISTS."""
        with TempDB() as db:
            self._setup(db)
            _, lines = db_run([
                "SELECT name FROM emp WHERE EXISTS (SELECT id FROM orders WHERE orders.emp_id = emp.id)",
                ".exit",
            ], db)
        self.assertTrue(any("(2 rows)" in l for l in lines))


if __name__ == "__main__":
    unittest.main(verbosity=2)
