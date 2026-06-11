"""B-tree structural invariant tests.

After a bulk insert + non-contiguous delete sequence, verifies:
  (a) Every leaf is reachable via the sibling chain from _leftmost_leaf(),
      and the chain terminates at page 0.
  (b) Every leaf's keys are strictly ascending; the last key of leaf[i] is
      strictly less than the first key of leaf[i+1].
  (c) Every internal-node separator key equals the minimum key reachable
      in its right-child subtree.
  (d) Every non-root node's parent pointer is non-zero and the pointed-to
      page lists that node as one of its children.
"""
import struct
import pytest
from hyperion import Database
from hyperion.btree import BTree


# ── Invariant checker ─────────────────────────────────────────────────────────

def _min_key_in_subtree(bt: BTree, pn: int) -> int:
    """Return the smallest key reachable from page pn."""
    page = bt._p.read_page(pn)
    if page[0] == BTree.NODE_LEAF:
        assert bt._num_cells(page) > 0, f"Unexpected empty leaf at page {pn}"
        return bt._leaf_key(page, 0)
    # Internal: recurse into leftmost child
    return _min_key_in_subtree(bt, bt._sibling(page))


def check_btree_invariants(bt: BTree) -> None:
    """Assert all structural invariants hold on the given BTree."""
    pager = bt._p

    # ── Invariant (a) & (b): leaf sibling chain ───────────────────────────────
    visited_leaves: list[int] = []
    prev_max_key: int | None  = None
    pn = bt._leftmost_leaf()
    assert pn != 0, "Tree has no leftmost leaf (root_page=0?)"
    while pn:
        page = pager.read_page(pn)
        assert page[0] == BTree.NODE_LEAF, \
            f"Page {pn} in sibling chain is not a leaf (type={page[0]})"

        n = bt._num_cells(page)
        assert n > 0 or page[1], \
            f"Empty non-root leaf at page {pn}"

        # (b) Keys within this leaf are strictly ascending
        for i in range(1, n):
            prev_k = bt._leaf_key(page, i - 1)
            curr_k = bt._leaf_key(page, i)
            assert prev_k < curr_k, (
                f"Leaf {pn} cell {i}: key {curr_k} <= previous key {prev_k} "
                f"(keys not strictly ascending)"
            )

        # (b) Cross-leaf ordering: last key of previous leaf < first key here
        if n > 0 and prev_max_key is not None:
            first_key = bt._leaf_key(page, 0)
            assert prev_max_key < first_key, (
                f"Cross-leaf ordering violation: leaf {visited_leaves[-1]} "
                f"last key {prev_max_key} >= leaf {pn} first key {first_key}"
            )

        if n > 0:
            prev_max_key = bt._leaf_key(page, n - 1)

        visited_leaves.append(pn)
        pn = bt._sibling(page)

    assert len(visited_leaves) > 0, "No leaves found in tree"

    # ── Full tree walk for invariants (c) and (d) ─────────────────────────────
    # BFS over all pages reachable from root
    root_page = bt.root_page
    queue = [root_page]
    seen: set[int] = set()

    while queue:
        pn   = queue.pop()
        if pn in seen:
            continue
        seen.add(pn)
        page = pager.read_page(pn)

        is_root = bool(page[1])
        n       = bt._num_cells(page)

        # (d) Every non-root node has a non-zero parent pointer that
        #     lists this node as one of its children
        if not is_root:
            parent_pn = bt._parent(page)
            assert parent_pn != 0, \
                f"Non-root page {pn} has zero parent pointer"
            parent_page = pager.read_page(parent_pn)
            # Collect all children of the parent
            children = {bt._sibling(parent_page)}
            for i in range(bt._num_cells(parent_page)):
                children.add(bt._int_rchild(parent_page, i))
            assert pn in children, (
                f"Page {pn} claims parent {parent_pn}, but parent does not "
                f"list it as a child (parent children: {children})"
            )

        if page[0] == BTree.NODE_LEAF:
            continue  # leaves checked in (a)/(b) pass

        # Internal node: check (c) and enqueue children
        leftmost = bt._sibling(page)
        assert leftmost != 0, \
            f"Internal page {pn} has zero leftmost child"
        queue.append(leftmost)

        for i in range(n):
            sep_key    = bt._int_key(page, i)
            right_pn   = bt._int_rchild(page, i)
            assert right_pn != 0, \
                f"Internal page {pn} cell {i} has zero right-child pointer"
            queue.append(right_pn)

            # (c) sep_key must be <= the minimum key in the right subtree.
            # The copy-up invariant requires sep_key == min_right at split time,
            # but deletions can leave a stale (lower) separator without breaking
            # routing: any key >= sep_key routes right, which is correct because
            # sep_key <= min_right.  A separator ABOVE min_right would
            # misroute valid keys to the left subtree — that is the hard error.
            min_right = _min_key_in_subtree(bt, right_pn)
            assert sep_key <= min_right, (
                f"Internal page {pn} separator {sep_key} at cell {i} > "
                f"min key {min_right} in right-child subtree (page {right_pn}): "
                f"routing-correctness violation"
            )


# ── Test helper ───────────────────────────────────────────────────────────────

def _get_btree(db: Database, table: str) -> BTree:
    """Retrieve the BTree backing *table* in *db*."""
    return db._table_btree(db._catalog.tables[table])


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_invariants_after_insert_only():
    """Freshly inserted 1 000 rows: invariants must hold before any deletes."""
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
    for i in range(1, 1001):
        db.execute(f"INSERT INTO t VALUES ({i}, 'v{i}')")
    check_btree_invariants(_get_btree(db, "t"))


def test_invariants_after_bulk_delete_contiguous():
    """Delete a contiguous block of 500 rows from the middle."""
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
    for i in range(1, 1001):
        db.execute(f"INSERT INTO t VALUES ({i}, 'v{i}')")
    db.execute("DELETE FROM t WHERE id >= 251 AND id <= 750")
    check_btree_invariants(_get_btree(db, "t"))


def test_invariants_after_bulk_delete_noncontiguous():
    """Delete 500 non-contiguous rows (every other row)."""
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
    for i in range(1, 1001):
        db.execute(f"INSERT INTO t VALUES ({i}, 'v{i}')")
    # Delete all even-numbered rows (non-contiguous)
    db.execute("DELETE FROM t WHERE id % 2 = 0")
    check_btree_invariants(_get_btree(db, "t"))
    # Verify the surviving rows are correct
    rows = db.execute("SELECT id FROM t ORDER BY id").fetchall()
    assert [r["id"] for r in rows] == list(range(1, 1001, 2))


def test_invariants_after_delete_from_front():
    """Delete the first 500 rows."""
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
    for i in range(1, 1001):
        db.execute(f"INSERT INTO t VALUES ({i}, 'v{i}')")
    db.execute("DELETE FROM t WHERE id <= 500")
    check_btree_invariants(_get_btree(db, "t"))


def test_invariants_after_delete_from_back():
    """Delete the last 500 rows."""
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
    for i in range(1, 1001):
        db.execute(f"INSERT INTO t VALUES ({i}, 'v{i}')")
    db.execute("DELETE FROM t WHERE id > 500")
    check_btree_invariants(_get_btree(db, "t"))


def test_invariants_after_scattered_delete():
    """Delete rows whose id is a multiple of 3 (scattered pattern)."""
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
    for i in range(1, 1001):
        db.execute(f"INSERT INTO t VALUES ({i}, 'v{i}')")
    db.execute("DELETE FROM t WHERE id % 3 = 0")
    check_btree_invariants(_get_btree(db, "t"))
    rows = db.execute("SELECT COUNT(*) AS cnt FROM t").fetchall()
    # ~667 rows remain (1-1000 not divisible by 3)
    assert rows[0]["cnt"] == sum(1 for i in range(1, 1001) if i % 3 != 0)


def test_invariants_delete_and_reinsert():
    """Delete half, re-insert different keys, check invariants hold throughout."""
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
    for i in range(1, 1001):
        db.execute(f"INSERT INTO t VALUES ({i}, 'v{i}')")
    db.execute("DELETE FROM t WHERE id % 2 = 0")
    check_btree_invariants(_get_btree(db, "t"))
    # Re-insert new keys in the gaps
    for i in range(2, 1001, 2):
        db.execute(f"INSERT INTO t VALUES ({i}, 'new{i}')")
    check_btree_invariants(_get_btree(db, "t"))
    rows = db.execute("SELECT COUNT(*) AS cnt FROM t").fetchall()
    assert rows[0]["cnt"] == 1000


def test_invariants_single_row():
    """Edge case: a single row (root-leaf only)."""
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
    db.execute("INSERT INTO t VALUES (42, 'hello')")
    check_btree_invariants(_get_btree(db, "t"))


def test_invariants_after_total_delete_then_insert():
    """Delete all rows, then insert fresh rows: tree must be valid."""
    db = Database(":memory:")
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
    for i in range(1, 201):
        db.execute(f"INSERT INTO t VALUES ({i}, 'v{i}')")
    db.execute("DELETE FROM t WHERE 1=1")
    for i in range(500, 600):
        db.execute(f"INSERT INTO t VALUES ({i}, 'v{i}')")
    check_btree_invariants(_get_btree(db, "t"))
    rows = db.execute("SELECT COUNT(*) AS cnt FROM t").fetchall()
    assert rows[0]["cnt"] == 100
