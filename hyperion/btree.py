import struct
from typing import Callable, Iterator

from .constants import PAGE_SIZE


class BTree:
    NODE_LEAF     = 0
    NODE_INTERNAL = 1

    HDR      = 14
    CHILD_SZ = 4

    def __init__(self, pager: "Pager", root_page: int, row_size: int,
                 alloc: Callable[[], int], *, key_sz: int = 8):
        self._p        = pager
        self.root_page = root_page
        self._rs       = row_size
        self._key_sz   = key_sz
        self._int_cell = key_sz + self.CHILD_SZ
        self._int_max  = (PAGE_SIZE - self.HDR) // self._int_cell
        self._lcs      = key_sz + row_size
        self._lmax     = (PAGE_SIZE - self.HDR) // self._lcs
        self._lmin     = self._lmax // 2
        self._imin     = self._int_max // 2
        self._alloc    = alloc

    # ── Public interface ───────────────────────────────────────────────────────

    def insert(self, key: int, value: bytes) -> None:
        self._leaf_insert(self._find_leaf(key), key, value)

    def lookup(self, key: int) -> bytes | None:
        """O(log n) point lookup by key. Returns raw value bytes or None."""
        page = self._p.get_page(self._find_leaf(key))
        for i in range(self._num_cells(page)):
            k = self._leaf_key(page, i)
            if k == key:
                return self._leaf_val(page, i)
            if k > key:
                break
        return None

    def scan(self) -> Iterator[tuple[int, bytes]]:
        """Yield (key, value) in key order via the leaf chain."""
        pn = self._leftmost_leaf()
        while pn:
            page = self._p.get_page(pn)
            for i in range(self._num_cells(page)):
                yield self._leaf_key(page, i), self._leaf_val(page, i)
            pn = self._sibling(page)

    def scan_range(self, lo: int, hi: int) -> Iterator[tuple[int, bytes]]:
        """Yield (key, value) for all entries with lo <= key <= hi."""
        pn = self._find_leaf(lo)
        while pn:
            page = self._p.get_page(pn)
            for i in range(self._num_cells(page)):
                k = self._leaf_key(page, i)
                if k > hi:
                    return
                if k >= lo:
                    yield k, self._leaf_val(page, i)
            pn = self._sibling(page)

    def update(self, updates: dict[int, bytes]) -> None:
        """In-place update rows by key.  updates = {key: new_value_bytes}."""
        for key, new_val in updates.items():
            page = self._p.get_page(self._find_leaf(key))
            for i in range(self._num_cells(page)):
                if self._leaf_key(page, i) == key:
                    off = self.HDR + i * self._lcs + self._key_sz
                    page[off: off + self._rs] = new_val
                    break

    def delete(self, keys: set[int]) -> int:
        """Remove all rows whose key is in *keys*.  Returns count deleted."""
        if not keys:
            return 0
        # Phase 1: compact every leaf (safe to do in chain order)
        deleted, pn = 0, self._leftmost_leaf()
        while pn:
            page     = self._p.get_page(pn)
            nxt      = self._sibling(page)
            before   = self._num_cells(page)
            self._compact_leaf(page, keys)
            deleted += before - self._num_cells(page)
            pn       = nxt
        # Phase 2: rebalance underfull leaves (restart from leftmost after each fix)
        changed = True
        while changed:
            changed, pn = False, self._leftmost_leaf()
            while pn:
                page = self._p.get_page(pn)
                nxt  = self._sibling(page)
                if not page[1] and self._num_cells(page) < self._lmin:
                    self._rebalance_leaf(pn)
                    changed = True
                    break
                pn = nxt
        return deleted

    # ── Key pack / unpack ─────────────────────────────────────────────────────

    def _pack_key(self, page: bytearray, off: int, key: int) -> None:
        if self._key_sz == 8:
            struct.pack_into("q", page, off, key)
        else:
            page[off: off + self._key_sz] = key.to_bytes(self._key_sz, "big")

    def _unpack_key(self, page: bytearray, off: int) -> int:
        if self._key_sz == 8:
            return struct.unpack_from("q", page, off)[0]
        return int.from_bytes(page[off: off + self._key_sz], "big")

    # ── Navigation ─────────────────────────────────────────────────────────────

    def _find_leaf(self, key: int) -> int:
        pn = self.root_page
        while True:
            page = self._p.get_page(pn)
            if page[0] == self.NODE_LEAF:
                return pn
            pn = self._sibling(page)   # start at leftmost child
            for i in range(self._num_cells(page)):
                if key >= self._int_key(page, i):
                    pn = self._int_rchild(page, i)
                else:
                    break

    def _leftmost_leaf(self) -> int:
        pn = self.root_page
        while True:
            page = self._p.get_page(pn)
            if page[0] == self.NODE_LEAF:
                return pn
            pn = self._sibling(page)

    # ── Leaf insert ────────────────────────────────────────────────────────────

    def _leaf_insert(self, pn: int, key: int, value: bytes) -> None:
        page = self._p.get_page(pn)
        n    = self._num_cells(page)
        if n < self._lmax:
            self._leaf_insert_nonfull(page, n, key, value)
        else:
            self._leaf_split(pn, key, value)

    def _leaf_insert_nonfull(self, page: bytearray, n: int,
                              key: int, value: bytes) -> None:
        pos = n
        for i in range(n):
            k = self._leaf_key(page, i)
            if key == k:
                raise RuntimeError(f"Duplicate key {key}")
            if key < k:
                pos = i
                break
        for i in range(n, pos, -1):
            s = self.HDR + (i - 1) * self._lcs
            d = self.HDR + i * self._lcs
            page[d: d + self._lcs] = page[s: s + self._lcs]
        off = self.HDR + pos * self._lcs
        self._pack_key(page, off, key)
        page[off + self._key_sz: off + self._lcs] = value
        self._set_num_cells(page, n + 1)

    def _leaf_split(self, pn: int, key: int, value: bytes) -> None:
        old  = self._p.get_page(pn)
        cells: list[tuple[int, bytes]] = [
            (self._leaf_key(old, i), self._leaf_val(old, i))
            for i in range(self._lmax)
        ]
        pos = self._lmax
        for i, (k, _) in enumerate(cells):
            if key == k:
                raise RuntimeError(f"Duplicate key {key}")
            if key < k:
                pos = i
                break
        cells.insert(pos, (key, value))
        mid       = (self._lmax + 1) // 2
        split_key = cells[mid][0]

        if old[1]:  # is_root: copy-both-halves, rewrite root as internal
            ln, rn = self._alloc(), self._alloc()
            left, right = self._p.get_page(ln), self._p.get_page(rn)
            self._init_leaf(left,  parent=pn, next_leaf=rn)
            self._init_leaf(right, parent=pn, next_leaf=0)
            self._write_leaf_cells(left,  cells[:mid])
            self._write_leaf_cells(right, cells[mid:])
            self._init_internal(old, parent=0, is_root=True, leftmost=ln)
            self._write_int_cells(old, [(split_key, rn)])
        else:
            rn    = self._alloc()
            right = self._p.get_page(rn)
            old_next = self._sibling(old)
            self._init_leaf(right, parent=self._parent(old), next_leaf=old_next)
            self._init_leaf(old,   parent=self._parent(old), next_leaf=rn, is_root=False)
            self._write_leaf_cells(old,   cells[:mid])
            self._write_leaf_cells(right, cells[mid:])
            self._int_insert(self._parent(old), split_key, rn)

    # ── Internal insert ────────────────────────────────────────────────────────

    def _int_insert(self, pn: int, key: int, right_child: int) -> None:
        page = self._p.get_page(pn)
        n    = self._num_cells(page)
        self._set_parent(self._p.get_page(right_child), pn)
        if n < self._int_max:
            self._int_insert_nonfull(page, n, key, right_child)
        else:
            self._int_split(pn, key, right_child)

    def _int_insert_nonfull(self, page: bytearray, n: int,
                             key: int, rc: int) -> None:
        pos = n
        for i in range(n):
            if key < self._int_key(page, i):
                pos = i
                break
        for i in range(n, pos, -1):
            s = self.HDR + (i - 1) * self._int_cell
            d = self.HDR + i * self._int_cell
            page[d: d + self._int_cell] = page[s: s + self._int_cell]
        off = self.HDR + pos * self._int_cell
        self._pack_key(page, off, key)
        struct.pack_into("I", page, off + self._key_sz, rc)
        self._set_num_cells(page, n + 1)

    def _int_split(self, pn: int, key: int, rc: int) -> None:
        old  = self._p.get_page(pn)
        n    = self._num_cells(old)
        cells: list[tuple[int, int]] = [
            (self._int_key(old, i), self._int_rchild(old, i))
            for i in range(n)
        ]
        leftmost = self._sibling(old)
        pos = n
        for i, (k, _) in enumerate(cells):
            if key < k:
                pos = i
                break
        cells.insert(pos, (key, rc))
        mid          = len(cells) // 2
        promoted_key = cells[mid][0]
        r_leftmost   = cells[mid][1]
        left_cells   = cells[:mid]
        right_cells  = cells[mid + 1:]

        if old[1]:  # is_root
            ln, rn = self._alloc(), self._alloc()
            left, right = self._p.get_page(ln), self._p.get_page(rn)
            self._init_internal(left,  parent=pn, is_root=False, leftmost=leftmost)
            self._init_internal(right, parent=pn, is_root=False, leftmost=r_leftmost)
            self._write_int_cells(left,  left_cells)
            self._write_int_cells(right, right_cells)
            self._reparent_children(left,  ln)
            self._reparent_children(right, rn)
            self._init_internal(old, parent=0, is_root=True, leftmost=ln)
            self._write_int_cells(old, [(promoted_key, rn)])
        else:
            rn     = self._alloc()
            right  = self._p.get_page(rn)
            parent = self._parent(old)
            self._init_internal(right, parent=parent, is_root=False, leftmost=r_leftmost)
            self._write_int_cells(right, right_cells)
            self._reparent_children(right, rn)
            self._init_internal(old, parent=parent, is_root=False, leftmost=leftmost)
            self._write_int_cells(old, left_cells)
            self._reparent_children(old, pn)
            self._int_insert(parent, promoted_key, rn)

    # ── Leaf rebalancing ───────────────────────────────────────────────────────

    def _rebalance_leaf(self, pn: int) -> None:
        page = self._p.get_page(pn)
        if page[1] or self._num_cells(page) >= self._lmin:
            return
        parent_num = self._parent(page)
        parent     = self._p.get_page(parent_num)
        k          = self._child_index(parent, pn)
        n          = self._num_cells(parent)

        if k < n:   # right sibling exists at children[k+1]
            rn    = self._get_child(parent, k + 1)
            right = self._p.get_page(rn)
            if self._num_cells(right) > self._lmin:
                self._borrow_right_leaf(pn, rn, parent, k)
                return
            self._merge_leaves(pn, rn, parent_num, parent, k)
            return

        if k > 0:   # left sibling exists at children[k-1]
            ln   = self._get_child(parent, k - 1)
            left = self._p.get_page(ln)
            if self._num_cells(left) > self._lmin:
                self._borrow_left_leaf(ln, pn, parent, k - 1)
                return
            self._merge_leaves(ln, pn, parent_num, parent, k - 1)

    def _borrow_right_leaf(self, ln: int, rn: int,
                            parent: bytearray, sep: int) -> None:
        left, right = self._p.get_page(ln), self._p.get_page(rn)
        nl = self._num_cells(left)
        # Append first cell of right to left
        bk = self._leaf_key(right, 0)
        bv = self._leaf_val(right, 0)
        off = self.HDR + nl * self._lcs
        self._pack_key(left, off, bk)
        left[off + self._key_sz: off + self._lcs] = bv
        self._set_num_cells(left, nl + 1)
        # Shift right's cells left
        nr = self._num_cells(right)
        right[self.HDR: self.HDR + (nr - 1) * self._lcs] = \
            right[self.HDR + self._lcs: self.HDR + nr * self._lcs]
        right[self.HDR + (nr - 1) * self._lcs: self.HDR + nr * self._lcs] = \
            bytearray(self._lcs)
        self._set_num_cells(right, nr - 1)
        # New separator = new first key of right
        self._pack_key(parent, self.HDR + sep * self._int_cell,
                       self._leaf_key(right, 0))

    def _borrow_left_leaf(self, ln: int, rn: int,
                           parent: bytearray, sep: int) -> None:
        left, right = self._p.get_page(ln), self._p.get_page(rn)
        nl, nr = self._num_cells(left), self._num_cells(right)
        # Shift right's cells right
        right[self.HDR + self._lcs: self.HDR + (nr + 1) * self._lcs] = \
            right[self.HDR: self.HDR + nr * self._lcs]
        # Prepend last cell of left to right
        loff = self.HDR + (nl - 1) * self._lcs
        right[self.HDR: self.HDR + self._lcs] = left[loff: loff + self._lcs]
        bk = self._leaf_key(left, nl - 1)
        left[loff: loff + self._lcs] = bytearray(self._lcs)
        self._set_num_cells(left, nl - 1)
        self._set_num_cells(right, nr + 1)
        # New separator = the key just moved (now right[0])
        self._pack_key(parent, self.HDR + sep * self._int_cell, bk)

    def _merge_leaves(self, ln: int, rn: int,
                      parent_num: int, parent: bytearray, sep: int) -> None:
        left, right = self._p.get_page(ln), self._p.get_page(rn)
        nl, nr = self._num_cells(left), self._num_cells(right)
        dst = self.HDR + nl * self._lcs
        left[dst: dst + nr * self._lcs] = \
            right[self.HDR: self.HDR + nr * self._lcs]
        self._set_num_cells(left, nl + nr)
        # Stitch leaf chain: left skips over right
        struct.pack_into("I", left, 10, self._sibling(right))
        self._remove_from_parent(parent_num, parent, sep)

    # ── Internal rebalancing ───────────────────────────────────────────────────

    def _rebalance_internal(self, pn: int) -> None:
        page = self._p.get_page(pn)
        if page[1] or self._num_cells(page) >= self._imin:
            return
        parent_num = self._parent(page)
        parent     = self._p.get_page(parent_num)
        k          = self._child_index(parent, pn)
        n          = self._num_cells(parent)

        if k < n:
            rn    = self._get_child(parent, k + 1)
            right = self._p.get_page(rn)
            if self._num_cells(right) > self._imin:
                self._borrow_right_int(pn, rn, parent, k)
                return
            self._merge_internals(pn, rn, parent_num, parent, k)
            return

        if k > 0:
            ln   = self._get_child(parent, k - 1)
            left = self._p.get_page(ln)
            if self._num_cells(left) > self._imin:
                self._borrow_left_int(ln, pn, parent, k - 1)
                return
            self._merge_internals(ln, pn, parent_num, parent, k - 1)

    def _borrow_right_int(self, ln: int, rn: int,
                           parent: bytearray, sep: int) -> None:
        left, right = self._p.get_page(ln), self._p.get_page(rn)
        nl, nr      = self._num_cells(left), self._num_cells(right)
        sep_key     = self._int_key(parent, sep)
        r_leftmost  = self._sibling(right)
        # Append (sep_key, r_leftmost) to left
        off = self.HDR + nl * self._int_cell
        self._pack_key(left, off, sep_key)
        struct.pack_into("I", left, off + self._key_sz, r_leftmost)
        self._set_num_cells(left, nl + 1)
        self._set_parent(self._p.get_page(r_leftmost), ln)
        # New separator = right's first key; right's new leftmost = right's first right_child
        new_sep      = self._int_key(right, 0)
        new_leftmost = self._int_rchild(right, 0)
        right[self.HDR: self.HDR + (nr - 1) * self._int_cell] = \
            right[self.HDR + self._int_cell: self.HDR + nr * self._int_cell]
        right[self.HDR + (nr - 1) * self._int_cell: self.HDR + nr * self._int_cell] = \
            bytearray(self._int_cell)
        struct.pack_into("I", right, 10, new_leftmost)
        self._set_num_cells(right, nr - 1)
        self._pack_key(parent, self.HDR + sep * self._int_cell, new_sep)

    def _borrow_left_int(self, ln: int, rn: int,
                          parent: bytearray, sep: int) -> None:
        left, right = self._p.get_page(ln), self._p.get_page(rn)
        nl, nr      = self._num_cells(left), self._num_cells(right)
        sep_key     = self._int_key(parent, sep)
        old_r_left  = self._sibling(right)
        last_key    = self._int_key(left, nl - 1)
        last_child  = self._int_rchild(left, nl - 1)
        # Shift right's cells right, prepend (sep_key, old_r_left)
        right[self.HDR + self._int_cell: self.HDR + (nr + 1) * self._int_cell] = \
            right[self.HDR: self.HDR + nr * self._int_cell]
        self._pack_key(right, self.HDR, sep_key)
        struct.pack_into("I", right, self.HDR + self._key_sz, old_r_left)
        struct.pack_into("I", right, 10, last_child)   # new leftmost of right
        self._set_parent(self._p.get_page(last_child), rn)
        self._set_num_cells(right, nr + 1)
        left[self.HDR + (nl - 1) * self._int_cell:
             self.HDR + nl * self._int_cell] = bytearray(self._int_cell)
        self._set_num_cells(left, nl - 1)
        self._pack_key(parent, self.HDR + sep * self._int_cell, last_key)

    def _merge_internals(self, ln: int, rn: int,
                         parent_num: int, parent: bytearray, sep: int) -> None:
        left, right = self._p.get_page(ln), self._p.get_page(rn)
        nl, nr      = self._num_cells(left), self._num_cells(right)
        sep_key     = self._int_key(parent, sep)
        r_leftmost  = self._sibling(right)
        # Append (sep_key, r_leftmost) then all of right's cells to left
        off = self.HDR + nl * self._int_cell
        self._pack_key(left, off, sep_key)
        struct.pack_into("I", left, off + self._key_sz, r_leftmost)
        self._set_parent(self._p.get_page(r_leftmost), ln)
        src = self.HDR
        dst = off + self._int_cell
        left[dst: dst + nr * self._int_cell] = right[src: src + nr * self._int_cell]
        for i in range(nr):
            self._set_parent(self._p.get_page(self._int_rchild(left, nl + 1 + i)), ln)
        self._set_num_cells(left, nl + 1 + nr)
        self._remove_from_parent(parent_num, parent, sep)

    def _remove_from_parent(self, parent_num: int,
                             parent: bytearray, sep: int) -> None:
        n = self._num_cells(parent)
        if sep < n - 1:
            src = self.HDR + (sep + 1) * self._int_cell
            dst = self.HDR + sep * self._int_cell
            parent[dst: dst + (n - sep - 1) * self._int_cell] = \
                parent[src: src + (n - sep - 1) * self._int_cell]
        parent[self.HDR + (n - 1) * self._int_cell:
               self.HDR + n * self._int_cell] = bytearray(self._int_cell)
        self._set_num_cells(parent, n - 1)
        if parent[1]:   # is_root
            if n - 1 == 0:
                self._collapse_root(parent_num, parent)
        elif n - 1 < self._imin:
            self._rebalance_internal(parent_num)

    def _collapse_root(self, root_num: int, root: bytearray) -> None:
        """Root has 0 cells (1 child left): copy that child into the root page."""
        only_child = self._sibling(root)
        child      = self._p.get_page(only_child)
        root[:]    = bytearray(child)
        root[1]    = 1   # mark as root
        struct.pack_into("I", root, 2, 0)
        if root[0] == self.NODE_INTERNAL:
            self._reparent_children(root, root_num)

    # ── Parent/child helpers ───────────────────────────────────────────────────

    def _child_index(self, parent: bytearray, child_pn: int) -> int:
        if self._sibling(parent) == child_pn:
            return 0
        for i in range(self._num_cells(parent)):
            if self._int_rchild(parent, i) == child_pn:
                return i + 1
        raise RuntimeError(f"Child {child_pn} not found in parent")

    def _get_child(self, parent: bytearray, k: int) -> int:
        return self._sibling(parent) if k == 0 else self._int_rchild(parent, k - 1)

    def _reparent_children(self, node: bytearray, node_pn: int) -> None:
        self._set_parent(self._p.get_page(self._sibling(node)), node_pn)
        for i in range(self._num_cells(node)):
            self._set_parent(self._p.get_page(self._int_rchild(node, i)), node_pn)

    # ── Node init helpers ──────────────────────────────────────────────────────

    def _init_leaf(self, page: bytearray, *, parent: int, next_leaf: int,
                   is_root: bool = False) -> None:
        page[:] = bytearray(PAGE_SIZE)
        page[0] = self.NODE_LEAF
        page[1] = int(is_root)
        struct.pack_into("I", page, 2, parent)
        struct.pack_into("I", page, 10, next_leaf)

    def _init_internal(self, page: bytearray, *, parent: int, is_root: bool,
                       leftmost: int) -> None:
        page[:] = bytearray(PAGE_SIZE)
        page[0] = self.NODE_INTERNAL
        page[1] = int(is_root)
        struct.pack_into("I", page, 2, parent)
        struct.pack_into("I", page, 10, leftmost)

    def _write_leaf_cells(self, page: bytearray,
                          cells: list[tuple[int, bytes]]) -> None:
        for i, (k, v) in enumerate(cells):
            off = self.HDR + i * self._lcs
            self._pack_key(page, off, k)
            page[off + self._key_sz: off + self._lcs] = v
        self._set_num_cells(page, len(cells))

    def _write_int_cells(self, page: bytearray,
                         cells: list[tuple[int, int]]) -> None:
        for i, (k, c) in enumerate(cells):
            off = self.HDR + i * self._int_cell
            self._pack_key(page, off, k)
            struct.pack_into("I", page, off + self._key_sz, c)
        self._set_num_cells(page, len(cells))

    # ── Leaf compaction ────────────────────────────────────────────────────────

    def _compact_leaf(self, page: bytearray, keys: set[int]) -> None:
        n, w = self._num_cells(page), 0
        for i in range(n):
            if self._leaf_key(page, i) not in keys:
                if w != i:
                    s = self.HDR + i * self._lcs
                    d = self.HDR + w * self._lcs
                    page[d: d + self._lcs] = page[s: s + self._lcs]
                w += 1
        page[self.HDR + w * self._lcs: self.HDR + n * self._lcs] = \
            bytearray((n - w) * self._lcs)
        self._set_num_cells(page, w)

    # ── Field accessors ────────────────────────────────────────────────────────

    def _num_cells(self, p: bytearray) -> int:
        return struct.unpack_from("I", p, 6)[0]

    def _set_num_cells(self, p: bytearray, n: int) -> None:
        struct.pack_into("I", p, 6, n)

    def _sibling(self, p: bytearray) -> int:
        return struct.unpack_from("I", p, 10)[0]

    def _parent(self, p: bytearray) -> int:
        return struct.unpack_from("I", p, 2)[0]

    def _set_parent(self, p: bytearray, v: int) -> None:
        struct.pack_into("I", p, 2, v)

    def _leaf_key(self, p: bytearray, i: int) -> int:
        return self._unpack_key(p, self.HDR + i * self._lcs)

    def _leaf_val(self, p: bytearray, i: int) -> bytes:
        off = self.HDR + i * self._lcs + self._key_sz
        return bytes(p[off: off + self._rs])

    def _int_key(self, p: bytearray, i: int) -> int:
        return self._unpack_key(p, self.HDR + i * self._int_cell)

    def _int_rchild(self, p: bytearray, i: int) -> int:
        return struct.unpack_from("I", p, self.HDR + i * self._int_cell + self._key_sz)[0]

    @classmethod
    def init_root_leaf(cls, pager: "Pager", pn: int) -> None:
        page    = pager.get_page(pn)
        page[:] = bytearray(PAGE_SIZE)
        page[0] = cls.NODE_LEAF
        page[1] = 1   # is_root
