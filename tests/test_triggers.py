"""Tests for CREATE TRIGGER / DROP TRIGGER and trigger firing semantics."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hyperion.database import Database
from hyperion.errors import HyperionError
from hyperion.executor import execute
from hyperion.parser import parse


def sql(db: Database, stmt: str) -> str:
    return execute(parse(stmt), db)


def _make_db():
    return Database(":memory:")


class TestCreateDropTrigger(unittest.TestCase):
    def setUp(self):
        self.db = _make_db()
        sql(self.db, "CREATE TABLE t (id INTEGER, val TEXT)")
        sql(self.db, "CREATE TABLE log (action TEXT, v TEXT)")

    def test_create_trigger_returns_message(self):
        r = sql(self.db,
            "CREATE TRIGGER tr_ai AFTER INSERT ON t "
            "FOR EACH ROW BEGIN "
            "INSERT INTO log VALUES ('inserted', NEW.val); "
            "END"
        )
        self.assertIn("created", r)

    def test_duplicate_trigger_raises(self):
        sql(self.db,
            "CREATE TRIGGER tr_ai AFTER INSERT ON t "
            "FOR EACH ROW BEGIN INSERT INTO log VALUES ('x', NEW.val); END"
        )
        with self.assertRaises(HyperionError):
            sql(self.db,
                "CREATE TRIGGER tr_ai AFTER INSERT ON t "
                "FOR EACH ROW BEGIN INSERT INTO log VALUES ('x', NEW.val); END"
            )

    def test_create_trigger_if_not_exists(self):
        sql(self.db,
            "CREATE TRIGGER tr_ai AFTER INSERT ON t "
            "FOR EACH ROW BEGIN INSERT INTO log VALUES ('x', NEW.val); END"
        )
        r = sql(self.db,
            "CREATE TRIGGER IF NOT EXISTS tr_ai AFTER INSERT ON t "
            "FOR EACH ROW BEGIN INSERT INTO log VALUES ('x', NEW.val); END"
        )
        self.assertIn("already exists", r)

    def test_drop_trigger(self):
        sql(self.db,
            "CREATE TRIGGER tr_ai AFTER INSERT ON t "
            "FOR EACH ROW BEGIN INSERT INTO log VALUES ('x', NEW.val); END"
        )
        r = sql(self.db, "DROP TRIGGER tr_ai")
        self.assertIn("dropped", r)

    def test_drop_trigger_if_exists(self):
        r = sql(self.db, "DROP TRIGGER IF EXISTS no_such_trigger")
        self.assertIn("does not exist", r)

    def test_drop_trigger_missing_raises(self):
        with self.assertRaises(HyperionError):
            sql(self.db, "DROP TRIGGER no_such_trigger")

    def test_create_trigger_bad_table_raises(self):
        with self.assertRaises(HyperionError):
            sql(self.db,
                "CREATE TRIGGER tr AFTER INSERT ON nonexistent "
                "FOR EACH ROW BEGIN INSERT INTO log VALUES ('x', 'y'); END"
            )


class TestAfterInsertTrigger(unittest.TestCase):
    def setUp(self):
        self.db = _make_db()
        sql(self.db, "CREATE TABLE t (id INTEGER, val TEXT)")
        sql(self.db, "CREATE TABLE log (action TEXT, v TEXT)")
        sql(self.db,
            "CREATE TRIGGER tr_ai AFTER INSERT ON t "
            "FOR EACH ROW BEGIN "
            "INSERT INTO log VALUES ('inserted', NEW.val); "
            "END"
        )

    def test_after_insert_fires(self):
        sql(self.db, "INSERT INTO t VALUES (1, 'hello')")
        r = sql(self.db, "SELECT action, v FROM log")
        self.assertIn("inserted", r)
        self.assertIn("hello", r)

    def test_after_insert_fires_multi_row(self):
        sql(self.db, "INSERT INTO t VALUES (1, 'a'), (2, 'b'), (3, 'c')")
        r = sql(self.db, "SELECT COUNT(*) FROM log")
        self.assertIn("3", r)

    def test_after_insert_new_col(self):
        sql(self.db, "INSERT INTO t VALUES (42, 'world')")
        r = sql(self.db, "SELECT v FROM log WHERE action = 'inserted'")
        self.assertIn("world", r)


class TestBeforeInsertTrigger(unittest.TestCase):
    def setUp(self):
        self.db = _make_db()
        sql(self.db, "CREATE TABLE t (id INTEGER, val TEXT)")
        sql(self.db, "CREATE TABLE pre_log (v TEXT)")
        sql(self.db,
            "CREATE TRIGGER tr_bi BEFORE INSERT ON t "
            "FOR EACH ROW BEGIN "
            "INSERT INTO pre_log VALUES (NEW.val); "
            "END"
        )

    def test_before_insert_fires(self):
        sql(self.db, "INSERT INTO t VALUES (1, 'alpha')")
        r = sql(self.db, "SELECT v FROM pre_log")
        self.assertIn("alpha", r)


class TestAfterDeleteTrigger(unittest.TestCase):
    def setUp(self):
        self.db = _make_db()
        sql(self.db, "CREATE TABLE t (id INTEGER, val TEXT)")
        sql(self.db, "CREATE TABLE log (action TEXT, v TEXT)")
        sql(self.db,
            "CREATE TRIGGER tr_ad AFTER DELETE ON t "
            "FOR EACH ROW BEGIN "
            "INSERT INTO log VALUES ('deleted', OLD.val); "
            "END"
        )
        sql(self.db, "INSERT INTO t VALUES (1, 'foo'), (2, 'bar'), (3, 'baz')")

    def test_after_delete_fires(self):
        sql(self.db, "DELETE FROM t WHERE id = 1")
        r = sql(self.db, "SELECT v FROM log WHERE action = 'deleted'")
        self.assertIn("foo", r)

    def test_after_delete_fires_multiple(self):
        sql(self.db, "DELETE FROM t WHERE id > 1")
        r = sql(self.db, "SELECT COUNT(*) FROM log")
        self.assertIn("2", r)


class TestBeforeDeleteTrigger(unittest.TestCase):
    def setUp(self):
        self.db = _make_db()
        sql(self.db, "CREATE TABLE t (id INTEGER, val TEXT)")
        sql(self.db, "CREATE TABLE pre_del (v TEXT)")
        sql(self.db,
            "CREATE TRIGGER tr_bd BEFORE DELETE ON t "
            "FOR EACH ROW BEGIN "
            "INSERT INTO pre_del VALUES (OLD.val); "
            "END"
        )
        sql(self.db, "INSERT INTO t VALUES (1, 'x'), (2, 'y')")

    def test_before_delete_fires(self):
        sql(self.db, "DELETE FROM t WHERE id = 1")
        r = sql(self.db, "SELECT v FROM pre_del")
        self.assertIn("x", r)


class TestAfterUpdateTrigger(unittest.TestCase):
    def setUp(self):
        self.db = _make_db()
        sql(self.db, "CREATE TABLE t (id INTEGER, val TEXT)")
        sql(self.db, "CREATE TABLE log (old_v TEXT, new_v TEXT)")
        sql(self.db,
            "CREATE TRIGGER tr_au AFTER UPDATE ON t "
            "FOR EACH ROW BEGIN "
            "INSERT INTO log VALUES (OLD.val, NEW.val); "
            "END"
        )
        sql(self.db, "INSERT INTO t VALUES (1, 'before'), (2, 'keep')")

    def test_after_update_fires(self):
        sql(self.db, "UPDATE t SET val = 'after' WHERE id = 1")
        r = sql(self.db, "SELECT old_v, new_v FROM log")
        self.assertIn("before", r)
        self.assertIn("after", r)

    def test_after_update_old_value(self):
        sql(self.db, "UPDATE t SET val = 'changed' WHERE id = 2")
        r = sql(self.db, "SELECT old_v FROM log")
        self.assertIn("keep", r)


class TestBeforeUpdateTrigger(unittest.TestCase):
    def setUp(self):
        self.db = _make_db()
        sql(self.db, "CREATE TABLE t (id INTEGER, val TEXT)")
        sql(self.db, "CREATE TABLE pre_upd (old_v TEXT, new_v TEXT)")
        sql(self.db,
            "CREATE TRIGGER tr_bu BEFORE UPDATE ON t "
            "FOR EACH ROW BEGIN "
            "INSERT INTO pre_upd VALUES (OLD.val, NEW.val); "
            "END"
        )
        sql(self.db, "INSERT INTO t VALUES (1, 'orig')")

    def test_before_update_fires(self):
        sql(self.db, "UPDATE t SET val = 'new' WHERE id = 1")
        r = sql(self.db, "SELECT old_v, new_v FROM pre_upd")
        self.assertIn("orig", r)
        self.assertIn("new", r)


class TestWhenClause(unittest.TestCase):
    def setUp(self):
        self.db = _make_db()
        sql(self.db, "CREATE TABLE t (id INTEGER, val TEXT)")
        sql(self.db, "CREATE TABLE log (v TEXT)")

    def test_when_filters_rows(self):
        sql(self.db,
            "CREATE TRIGGER tr AFTER INSERT ON t "
            "FOR EACH ROW WHEN NEW.id > 2 BEGIN "
            "INSERT INTO log VALUES (NEW.val); "
            "END"
        )
        sql(self.db, "INSERT INTO t VALUES (1, 'skip'), (3, 'keep')")
        r = sql(self.db, "SELECT * FROM log")
        self.assertNotIn("skip", r)
        self.assertIn("keep", r)

    def test_when_on_delete(self):
        sql(self.db, "INSERT INTO t VALUES (1, 'a'), (2, 'b'), (3, 'c')")
        sql(self.db,
            "CREATE TRIGGER tr BEFORE DELETE ON t "
            "FOR EACH ROW WHEN OLD.id = 2 BEGIN "
            "INSERT INTO log VALUES (OLD.val); "
            "END"
        )
        sql(self.db, "DELETE FROM t")
        r = sql(self.db, "SELECT * FROM log")
        self.assertIn("b", r)
        self.assertNotIn("a", r)
        self.assertNotIn("c", r)


class TestCascadingTriggers(unittest.TestCase):
    def setUp(self):
        self.db = _make_db()
        sql(self.db, "CREATE TABLE a (id INTEGER)")
        sql(self.db, "CREATE TABLE b (id INTEGER)")
        sql(self.db, "CREATE TABLE c (id INTEGER)")

    def test_cascading_insert(self):
        sql(self.db,
            "CREATE TRIGGER tr_a AFTER INSERT ON a "
            "FOR EACH ROW BEGIN INSERT INTO b VALUES (NEW.id); END"
        )
        sql(self.db,
            "CREATE TRIGGER tr_b AFTER INSERT ON b "
            "FOR EACH ROW BEGIN INSERT INTO c VALUES (NEW.id); END"
        )
        sql(self.db, "INSERT INTO a VALUES (99)")
        ra = sql(self.db, "SELECT * FROM a")
        rb = sql(self.db, "SELECT * FROM b")
        rc = sql(self.db, "SELECT * FROM c")
        self.assertIn("99", ra)
        self.assertIn("99", rb)
        self.assertIn("99", rc)

    def test_recursion_limit(self):
        # Body inserts a fixed value so parsing always succeeds — real recursion
        sql(self.db,
            "CREATE TRIGGER tr_inf AFTER INSERT ON a "
            "FOR EACH ROW BEGIN INSERT INTO a VALUES (1); END"
        )
        with self.assertRaises(HyperionError) as ctx:
            sql(self.db, "INSERT INTO a VALUES (1)")
        self.assertIn("recursion limit", str(ctx.exception).lower())


class TestTriggerPersistence(unittest.TestCase):
    """Triggers survive a close/reopen cycle."""

    def test_persists_across_reopen(self):
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = f.name
        try:
            db1 = Database(path)
            sql(db1, "CREATE TABLE t (id INTEGER, v TEXT)")
            sql(db1, "CREATE TABLE log (v TEXT)")
            sql(db1,
                "CREATE TRIGGER tr AFTER INSERT ON t "
                "FOR EACH ROW BEGIN INSERT INTO log VALUES (NEW.v); END"
            )
            db1.close()

            db2 = Database(path)
            sql(db2, "INSERT INTO t VALUES (1, 'persisted')")
            r = sql(db2, "SELECT v FROM log")
            db2.close()
            self.assertIn("persisted", r)
        finally:
            os.unlink(path)


class TestMultipleTriggersSameEvent(unittest.TestCase):
    def setUp(self):
        self.db = _make_db()
        sql(self.db, "CREATE TABLE t (id INTEGER)")
        sql(self.db, "CREATE TABLE log (msg TEXT)")

    def test_multiple_triggers_all_fire(self):
        sql(self.db,
            "CREATE TRIGGER tr1 AFTER INSERT ON t "
            "FOR EACH ROW BEGIN INSERT INTO log VALUES ('tr1'); END"
        )
        sql(self.db,
            "CREATE TRIGGER tr2 AFTER INSERT ON t "
            "FOR EACH ROW BEGIN INSERT INTO log VALUES ('tr2'); END"
        )
        sql(self.db, "INSERT INTO t VALUES (1)")
        r = sql(self.db, "SELECT COUNT(*) FROM log")
        self.assertIn("2", r)


class TestUpdateOfFilter(unittest.TestCase):
    def setUp(self):
        self.db = _make_db()
        sql(self.db, "CREATE TABLE emp (id INTEGER, name TEXT, salary INTEGER)")
        sql(self.db, "CREATE TABLE log (msg TEXT)")
        sql(self.db, "INSERT INTO emp VALUES (1, 'alice', 100), (2, 'bob', 200)")

    def test_update_of_fires_when_col_changed(self):
        sql(self.db,
            "CREATE TRIGGER tr AFTER UPDATE OF salary ON emp "
            "FOR EACH ROW BEGIN INSERT INTO log VALUES ('salary changed'); END"
        )
        sql(self.db, "UPDATE emp SET salary = 999 WHERE id = 1")
        r = sql(self.db, "SELECT COUNT(*) FROM log")
        self.assertIn("1", r)

    def test_update_of_does_not_fire_when_other_col_changed(self):
        sql(self.db,
            "CREATE TRIGGER tr AFTER UPDATE OF salary ON emp "
            "FOR EACH ROW BEGIN INSERT INTO log VALUES ('salary changed'); END"
        )
        sql(self.db, "UPDATE emp SET name = 'carol' WHERE id = 1")
        r = sql(self.db, "SELECT * FROM log")
        self.assertEqual(r, "(no rows)")

    def test_update_of_multiple_cols(self):
        sql(self.db,
            "CREATE TRIGGER tr AFTER UPDATE OF salary, name ON emp "
            "FOR EACH ROW BEGIN INSERT INTO log VALUES ('changed'); END"
        )
        sql(self.db, "UPDATE emp SET name = 'dave' WHERE id = 1")
        r = sql(self.db, "SELECT COUNT(*) FROM log")
        self.assertIn("1", r)


class TestRaise(unittest.TestCase):
    def setUp(self):
        self.db = _make_db()
        sql(self.db, "CREATE TABLE t (id INTEGER, val INTEGER)")
        sql(self.db, "INSERT INTO t VALUES (1, 10)")

    def test_raise_abort_on_insert(self):
        sql(self.db,
            "CREATE TRIGGER tr BEFORE INSERT ON t "
            "FOR EACH ROW WHEN NEW.val < 0 BEGIN "
            "RAISE(ABORT, 'val must be non-negative'); "
            "END"
        )
        with self.assertRaises(HyperionError) as ctx:
            sql(self.db, "INSERT INTO t VALUES (2, -1)")
        self.assertIn("val must be non-negative", str(ctx.exception))

    def test_raise_abort_does_not_fire_when_condition_false(self):
        sql(self.db,
            "CREATE TRIGGER tr BEFORE INSERT ON t "
            "FOR EACH ROW WHEN NEW.val < 0 BEGIN "
            "RAISE(ABORT, 'val must be non-negative'); "
            "END"
        )
        sql(self.db, "INSERT INTO t VALUES (3, 5)")
        r = sql(self.db, "SELECT COUNT(*) FROM t")
        self.assertIn("2", r)

    def test_raise_ignore_silently_skips_trigger(self):
        sql(self.db,
            "CREATE TRIGGER tr BEFORE UPDATE ON t "
            "FOR EACH ROW WHEN NEW.val > 1000 BEGIN "
            "RAISE(IGNORE); "
            "END"
        )
        # RAISE(IGNORE) aborts the trigger silently; update still proceeds
        sql(self.db, "UPDATE t SET val = 9999 WHERE id = 1")
        r = sql(self.db, "SELECT val FROM t WHERE id = 1")
        self.assertIn("9999", r)

    def test_raise_fail(self):
        sql(self.db,
            "CREATE TRIGGER tr BEFORE INSERT ON t "
            "FOR EACH ROW BEGIN "
            "RAISE(FAIL, 'always fail'); "
            "END"
        )
        with self.assertRaises(HyperionError) as ctx:
            sql(self.db, "INSERT INTO t VALUES (99, 1)")
        self.assertIn("always fail", str(ctx.exception))


class TestInsteadOfTriggers(unittest.TestCase):
    def setUp(self):
        self.db = _make_db()
        sql(self.db, "CREATE TABLE users (id INTEGER, name TEXT)")
        sql(self.db, "CREATE TABLE addrs (user_id INTEGER, city TEXT)")
        sql(self.db, "CREATE VIEW user_city AS SELECT users.id, users.name, addrs.city FROM users JOIN addrs ON users.id = addrs.user_id")
        sql(self.db, "INSERT INTO users VALUES (1, 'alice'), (2, 'bob')")
        sql(self.db, "INSERT INTO addrs VALUES (1, 'london'), (2, 'paris')")

    def test_instead_of_insert_fires(self):
        sql(self.db,
            "CREATE TRIGGER tr_ins INSTEAD OF INSERT ON user_city "
            "FOR EACH ROW BEGIN "
            "INSERT INTO users VALUES (NEW.id, NEW.name); "
            "INSERT INTO addrs VALUES (NEW.id, NEW.city); "
            "END"
        )
        sql(self.db, "INSERT INTO user_city (id, name, city) VALUES (3, 'carol', 'berlin')")
        r_u = sql(self.db, "SELECT name FROM users WHERE id = 3")
        r_a = sql(self.db, "SELECT city FROM addrs WHERE user_id = 3")
        self.assertIn("carol", r_u)
        self.assertIn("berlin", r_a)

    def test_instead_of_delete_fires(self):
        sql(self.db,
            "CREATE TRIGGER tr_del INSTEAD OF DELETE ON user_city "
            "FOR EACH ROW BEGIN "
            "DELETE FROM users WHERE id = OLD.id; "
            "DELETE FROM addrs WHERE user_id = OLD.id; "
            "END"
        )
        sql(self.db, "DELETE FROM user_city WHERE id = 1")
        r_u = sql(self.db, "SELECT * FROM users")
        r_a = sql(self.db, "SELECT * FROM addrs")
        self.assertNotIn("alice", r_u)
        self.assertNotIn("london", r_a)
        self.assertIn("bob", r_u)

    def test_instead_of_update_fires(self):
        sql(self.db,
            "CREATE TRIGGER tr_upd INSTEAD OF UPDATE ON user_city "
            "FOR EACH ROW BEGIN "
            "UPDATE addrs SET city = NEW.city WHERE user_id = OLD.id; "
            "END"
        )
        sql(self.db, "UPDATE user_city SET city = 'tokyo' WHERE id = 1")
        r = sql(self.db, "SELECT city FROM addrs WHERE user_id = 1")
        self.assertIn("tokyo", r)

    def test_insert_on_view_without_instead_of_raises(self):
        with self.assertRaises(HyperionError):
            sql(self.db, "INSERT INTO user_city (id, name, city) VALUES (9, 'x', 'y')")

    def test_delete_on_view_without_instead_of_raises(self):
        with self.assertRaises(HyperionError):
            sql(self.db, "DELETE FROM user_city WHERE id = 1")

    def test_instead_of_on_table_raises(self):
        with self.assertRaises(HyperionError):
            sql(self.db,
                "CREATE TRIGGER tr INSTEAD OF INSERT ON users "
                "FOR EACH ROW BEGIN INSERT INTO addrs VALUES (NEW.id, 'x'); END"
            )


class TestExpressionAssignmentInTrigger(unittest.TestCase):
    def setUp(self):
        self.db = _make_db()
        sql(self.db, "CREATE TABLE t (id INTEGER, n INTEGER)")
        sql(self.db, "CREATE TABLE log (old_n INTEGER, new_n INTEGER)")
        sql(self.db,
            "CREATE TRIGGER tr AFTER UPDATE ON t "
            "FOR EACH ROW BEGIN "
            "INSERT INTO log VALUES (OLD.n, NEW.n); "
            "END"
        )
        sql(self.db, "INSERT INTO t VALUES (1, 10)")

    def test_expression_assignment_new_reflects_computed_value(self):
        sql(self.db, "UPDATE t SET n = n + 5 WHERE id = 1")
        r = sql(self.db, "SELECT old_n, new_n FROM log")
        self.assertIn("10", r)   # OLD.n
        self.assertIn("15", r)   # NEW.n = 10 + 5

    def test_expression_assignment_multiply(self):
        sql(self.db, "UPDATE t SET n = n * 2 WHERE id = 1")
        r = sql(self.db, "SELECT new_n FROM log")
        self.assertIn("20", r)   # NEW.n = 10 * 2


if __name__ == "__main__":
    unittest.main()
