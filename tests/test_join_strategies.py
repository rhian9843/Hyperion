"""Tests for Hash JOIN, Merge JOIN, and join strategy planner."""

import pytest
from hyperion import Database
from hyperion.join_strategies import choose_strategy, NESTED_LOOP_THRESHOLD


# ── planner unit tests ────────────────────────────────────────────────────────

class TestChooseStrategy:

    def test_small_tables_nested_loop(self):
        assert choose_strategy(5, 5, False, False, "INNER") == "NESTED_LOOP"

    def test_threshold_boundary_nested_loop(self):
        n = NESTED_LOOP_THRESHOLD - 1
        assert choose_strategy(n, n, False, False, "INNER") == "NESTED_LOOP"

    def test_one_large_side_hash_join(self):
        assert choose_strategy(100, 5, False, False, "INNER") == "HASH_JOIN"

    def test_both_large_no_index_hash_join(self):
        assert choose_strategy(100, 200, False, False, "INNER") == "HASH_JOIN"

    def test_both_indexed_inner_merge_join(self):
        assert choose_strategy(100, 200, True, True, "INNER") == "MERGE_JOIN"

    def test_both_indexed_left_join_hash_not_merge(self):
        assert choose_strategy(100, 200, True, True, "LEFT") == "HASH_JOIN"

    def test_both_indexed_full_join_hash_not_merge(self):
        assert choose_strategy(100, 200, True, True, "FULL") == "HASH_JOIN"

    def test_only_right_indexed_hash_join(self):
        assert choose_strategy(100, 200, False, True, "INNER") == "HASH_JOIN"

    def test_only_left_indexed_hash_join(self):
        assert choose_strategy(100, 200, True, False, "INNER") == "HASH_JOIN"


# ── helpers ───────────────────────────────────────────────────────────────────

def fresh_db(tmp_path):
    return Database(str(tmp_path / "test.hdb"))


def _make_tables(db, left_rows=15, right_rows=15):
    """Create two tables large enough to avoid NESTED_LOOP."""
    db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, cust_id INTEGER, amount INTEGER)")
    db.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT)")
    for i in range(1, right_rows + 1):
        db.execute("INSERT INTO customers VALUES (?, ?)", (i, f"Cust{i}"))
    for i in range(1, left_rows + 1):
        db.execute("INSERT INTO orders VALUES (?, ?, ?)", (i, (i % right_rows) + 1, i * 10))


# ── HASH JOIN correctness ─────────────────────────────────────────────────────

class TestHashJoin:

    def test_inner_join_all_rows_matched(self, tmp_path):
        db = fresh_db(tmp_path)
        _make_tables(db)
        rows = db.execute(
            "SELECT orders.id, customers.name FROM orders "
            "JOIN customers ON orders.cust_id = customers.id"
        ).fetchall()
        assert len(rows) == 15

    def test_inner_join_correct_values(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE a (id INTEGER PRIMARY KEY, aval INTEGER)")
        db.execute("CREATE TABLE b (id INTEGER PRIMARY KEY, bval INTEGER)")
        for i in range(1, 20):
            db.execute("INSERT INTO a VALUES (?, ?)", (i, i * 2))
        for i in range(1, 20):
            db.execute("INSERT INTO b VALUES (?, ?)", (i, i * 3))
        rows = db.execute(
            "SELECT a.aval, b.bval FROM a JOIN b ON a.id = b.id WHERE a.id = 5"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["aval"] == 10
        assert rows[0]["bval"] == 15

    def test_left_join_unmatched_left_rows_kept(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE left_t (id INTEGER PRIMARY KEY, lval TEXT)")
        db.execute("CREATE TABLE right_t (id INTEGER PRIMARY KEY, rval TEXT)")
        for i in range(1, 20):
            db.execute("INSERT INTO left_t VALUES (?, ?)", (i, f"L{i}"))
        for i in range(1, 10):
            db.execute("INSERT INTO right_t VALUES (?, ?)", (i, f"R{i}"))
        rows = db.execute(
            "SELECT left_t.lval, right_t.rval FROM left_t "
            "LEFT JOIN right_t ON left_t.id = right_t.id"
        ).fetchall()
        assert len(rows) == 19
        unmatched = [r for r in rows if r["rval"] is None]
        assert len(unmatched) == 10  # ids 10-19

    def test_no_matches_returns_empty_inner(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE x (id INTEGER PRIMARY KEY, k INTEGER)")
        db.execute("CREATE TABLE y (id INTEGER PRIMARY KEY, k INTEGER)")
        for i in range(1, 20):
            db.execute("INSERT INTO x VALUES (?, ?)", (i, i))
        for i in range(100, 120):
            db.execute("INSERT INTO y VALUES (?, ?)", (i, i))  # no overlap
        rows = db.execute(
            "SELECT x.id FROM x JOIN y ON x.k = y.k"
        ).fetchall()
        assert rows == []

    def test_duplicate_keys_cross_product(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE p (id INTEGER PRIMARY KEY, k INTEGER)")
        db.execute("CREATE TABLE q (id INTEGER PRIMARY KEY, k INTEGER)")
        for i in range(1, 20):
            db.execute("INSERT INTO p VALUES (?, 1)", (i,))
        for i in range(1, 20):
            db.execute("INSERT INTO q VALUES (?, 1)", (i,))
        rows = db.execute("SELECT p.id, q.id FROM p JOIN q ON p.k = q.k").fetchall()
        assert len(rows) == 19 * 19

    def test_right_join_unmatched_right_rows_kept(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE lt (id INTEGER PRIMARY KEY, lval TEXT)")
        db.execute("CREATE TABLE rt (id INTEGER PRIMARY KEY, rval TEXT)")
        for i in range(1, 10):
            db.execute("INSERT INTO lt VALUES (?, ?)", (i, f"L{i}"))
        for i in range(1, 20):
            db.execute("INSERT INTO rt VALUES (?, ?)", (i, f"R{i}"))
        rows = db.execute(
            "SELECT lt.lval, rt.id FROM lt RIGHT JOIN rt ON lt.id = rt.id"
        ).fetchall()
        assert len(rows) == 19
        unmatched = [r for r in rows if r["lval"] is None]
        assert len(unmatched) == 10  # ids 10-19


# ── MERGE JOIN correctness ────────────────────────────────────────────────────

class TestMergeJoin:

    def _make_indexed_tables(self, db, n=20):
        db.execute("CREATE TABLE emp (id INTEGER PRIMARY KEY, dept_id INTEGER)")
        db.execute("CREATE TABLE dept (id INTEGER PRIMARY KEY, name TEXT)")
        db.execute("CREATE INDEX idx_emp_dept ON emp(dept_id)")
        for i in range(1, n + 1):
            db.execute("INSERT INTO dept VALUES (?, ?)", (i, f"Dept{i}"))
        for i in range(1, n + 1):
            db.execute("INSERT INTO emp VALUES (?, ?)", (i, (i % n) + 1))

    def test_merge_join_inner_correct_count(self, tmp_path):
        db = fresh_db(tmp_path)
        self._make_indexed_tables(db)
        rows = db.execute(
            "SELECT emp.id, dept.name FROM emp JOIN dept ON emp.dept_id = dept.id"
        ).fetchall()
        assert len(rows) == 20

    def test_merge_join_produces_correct_pairs(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE aa (id INTEGER PRIMARY KEY, av INTEGER)")
        db.execute("CREATE TABLE bb (id INTEGER PRIMARY KEY, bv INTEGER)")
        db.execute("CREATE INDEX idx_aa_av ON aa(av)")
        db.execute("CREATE INDEX idx_bb_bv ON bb(bv)")
        for i in range(1, 20):
            db.execute("INSERT INTO aa VALUES (?, ?)", (i, i))
        for i in range(1, 20):
            db.execute("INSERT INTO bb VALUES (?, ?)", (i, i))
        rows = db.execute("SELECT aa.av, bb.bv FROM aa JOIN bb ON aa.av = bb.bv").fetchall()
        assert len(rows) == 19
        for r in rows:
            assert r["av"] == r["bv"]


# ── three-table join (extra_join path) ───────────────────────────────────────

class TestExtraJoin:

    def test_three_table_join_correct_count(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE a (id INTEGER PRIMARY KEY, b_id INTEGER)")
        db.execute("CREATE TABLE b (id INTEGER PRIMARY KEY, c_id INTEGER)")
        db.execute("CREATE TABLE c (id INTEGER PRIMARY KEY, name TEXT)")
        for i in range(1, 20):
            db.execute("INSERT INTO c VALUES (?, ?)", (i, f"C{i}"))
        for i in range(1, 20):
            db.execute("INSERT INTO b VALUES (?, ?)", (i, i))
        for i in range(1, 20):
            db.execute("INSERT INTO a VALUES (?, ?)", (i, i))
        rows = db.execute(
            "SELECT a.id, b.id, c.name FROM a "
            "JOIN b ON a.b_id = b.id "
            "JOIN c ON b.c_id = c.id"
        ).fetchall()
        assert len(rows) == 19

    def test_three_table_join_correct_values(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE a (id INTEGER PRIMARY KEY, b_id INTEGER)")
        db.execute("CREATE TABLE b (id INTEGER PRIMARY KEY, c_id INTEGER)")
        db.execute("CREATE TABLE c (id INTEGER PRIMARY KEY, name TEXT)")
        db.execute("INSERT INTO c VALUES (1, 'Alpha')")
        db.execute("INSERT INTO b VALUES (1, 1)")
        db.execute("INSERT INTO a VALUES (1, 1)")
        # Small tables → NESTED_LOOP but verifies correctness
        rows = db.execute(
            "SELECT a.id, c.name FROM a JOIN b ON a.b_id = b.id JOIN c ON b.c_id = c.id"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["name"] == "Alpha"


# ── EXPLAIN shows strategy ────────────────────────────────────────────────────

class TestExplainStrategy:

    def test_explain_join_contains_detail(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE a (id INTEGER PRIMARY KEY, b_id INTEGER)")
        db.execute("CREATE TABLE b (id INTEGER PRIMARY KEY, val TEXT)")
        rows = db.execute(
            "EXPLAIN QUERY PLAN SELECT a.id FROM a JOIN b ON a.b_id = b.id"
        ).fetchall()
        details = " ".join(r["detail"] for r in rows)
        assert "a" in details
        assert "b" in details

    def test_explain_inlj_shown_for_indexed_col(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, cid INTEGER)")
        db.execute("CREATE TABLE cust (id INTEGER PRIMARY KEY, name TEXT)")
        rows = db.execute(
            "EXPLAIN QUERY PLAN SELECT orders.id FROM orders "
            "JOIN cust ON orders.cid = cust.id"
        ).fetchall()
        details = " ".join(r["detail"] for r in rows)
        # cust.id is the primary key (indexed) — should show INLJ
        assert "INLJ" in details

    def test_explain_strategy_tag_present_when_analyzed(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE p (id INTEGER PRIMARY KEY, k INTEGER)")
        db.execute("CREATE TABLE q (id INTEGER PRIMARY KEY, k INTEGER)")
        for i in range(1, 20):
            db.execute("INSERT INTO p VALUES (?, ?)", (i, i))
        for i in range(1, 20):
            db.execute("INSERT INTO q VALUES (?, ?)", (i, i))
        db.execute("ANALYZE")
        rows = db.execute(
            "EXPLAIN QUERY PLAN SELECT p.id FROM p JOIN q ON p.k = q.k"
        ).fetchall()
        details = " ".join(r["detail"] for r in rows)
        # After ANALYZE with 19 rows each (> threshold), should show a strategy tag
        assert any(tag in details for tag in ("HASH_JOIN", "MERGE_JOIN", "NESTED_LOOP", "INLJ"))


# ── null handling ─────────────────────────────────────────────────────────────

class TestJoinNullHandling:

    def test_null_join_key_not_matched(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE a (id INTEGER PRIMARY KEY, k INTEGER)")
        db.execute("CREATE TABLE b (id INTEGER PRIMARY KEY, k INTEGER)")
        for i in range(1, 20):
            db.execute("INSERT INTO a VALUES (?, ?)", (i, None if i == 5 else i))
        for i in range(1, 20):
            db.execute("INSERT INTO b VALUES (?, ?)", (i, i))
        rows = db.execute("SELECT a.id FROM a JOIN b ON a.k = b.k").fetchall()
        ids = {r["id"] for r in rows}
        assert 5 not in ids  # NULL key should not match

    def test_left_join_null_key_row_kept(self, tmp_path):
        db = fresh_db(tmp_path)
        db.execute("CREATE TABLE a (id INTEGER PRIMARY KEY, ak INTEGER)")
        db.execute("CREATE TABLE b (id INTEGER PRIMARY KEY, bk INTEGER)")
        for i in range(1, 20):
            db.execute("INSERT INTO a VALUES (?, ?)", (i, None if i == 5 else i))
        for i in range(1, 20):
            db.execute("INSERT INTO b VALUES (?, ?)", (i, i))
        rows = db.execute("SELECT a.id, b.bk FROM a LEFT JOIN b ON a.ak = b.bk").fetchall()
        assert len(rows) == 19
        null_row = next(r for r in rows if r["id"] == 5)
        assert null_row["bk"] is None
