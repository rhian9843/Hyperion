#!/usr/bin/env python3
"""
Hyperion — a lightweight relational database engine.

Storage:  B+ tree, one tree per table / index.  Each page is 4 KB.
Keys:     auto-increment internal rowid (tables) or column value (indexes).
Nodes:    leaf nodes hold serialised rows; internal nodes hold key/child pairs.
Scans:    follow the singly-linked leaf chain for ordered full-table reads.
Delete:   lazy in-leaf compaction followed by borrow-or-merge rebalancing.
Indexes:  secondary B+ trees on INTEGER columns; auto-used for WHERE col = val.

Supported SQL
─────────────
  CREATE TABLE t (col TYPE, ...)       TYPE: INTEGER | REAL | TEXT | VARCHAR(n)
  DROP TABLE t
  INSERT INTO t VALUES (v, ...)
  INSERT INTO t (c1, c2) VALUES (v, ...)
  SELECT * FROM t [WHERE col OP val]
  SELECT c1, c2 FROM t [WHERE col OP val]
  SELECT ... FROM t1 INNER JOIN t2 ON t1.c = t2.c [WHERE col OP val]
  UPDATE t SET col=val [, col=val ...] [WHERE col OP val]
  DELETE FROM t [WHERE col OP val]
  CREATE INDEX idx ON table(col)
  DROP INDEX idx

  WHERE operators: =  !=  <  >  <=  >=  LIKE

Meta-commands
─────────────
  .tables              list all tables
  .schema <table>      show CREATE TABLE statement
  .indexes             list all indexes
  .exit
"""

import json
import os
import re
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

# ── Storage constants ──────────────────────────────────────────────────────────

PAGE_SIZE = 4096

# ── Column types ───────────────────────────────────────────────────────────────

INTEGER = "INTEGER"
REAL    = "REAL"
TEXT    = "TEXT"

_FIXED_FMTS  = {INTEGER: "q", REAL: "d"}
_FIXED_SIZES = {INTEGER: 8,   REAL: 8}
DEFAULT_TEXT_SIZE = 255


# ══════════════════════════════════════════════════════════════════════════════
# Schema
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Column:
    name:     str
    type:     str
    size:     int       = DEFAULT_TEXT_SIZE
    nullable: bool      = True
    unique:   bool      = False
    default:  str|None  = None
    check:    str|None  = None

    @property
    def fmt(self) -> str:
        return _FIXED_FMTS.get(self.type, f"{self.size}s")

    @property
    def byte_size(self) -> int:
        return _FIXED_SIZES.get(self.type, self.size)


@dataclass
class ForeignKey:
    columns:     list[str]   # child column(s)
    ref_table:   str         # parent table name
    ref_columns: list[str]   # parent column(s)


@dataclass
class Schema:
    name:         str
    columns:      list[Column]
    foreign_keys: list[ForeignKey] = field(default_factory=list)

    @property
    def row_format(self) -> str:
        return "=" + "".join(c.fmt for c in self.columns)

    @property
    def null_bitmap_size(self) -> int:
        return (len(self.columns) + 7) // 8

    @property
    def row_size(self) -> int:
        return self.null_bitmap_size + struct.calcsize(self.row_format)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "columns": [
                {"name": c.name, "type": c.type, "size": c.size,
                 "nullable": c.nullable, "unique": c.unique,
                 "default": c.default, "check": c.check}
                for c in self.columns
            ],
            "foreign_keys": [
                {"columns": fk.columns, "ref_table": fk.ref_table,
                 "ref_columns": fk.ref_columns}
                for fk in self.foreign_keys
            ],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Schema":
        cols = [
            Column(c["name"], c["type"], c.get("size", DEFAULT_TEXT_SIZE),
                   c.get("nullable", True), c.get("unique", False),
                   c.get("default"), c.get("check"))
            for c in d["columns"]
        ]
        fks = [
            ForeignKey(f["columns"], f["ref_table"], f["ref_columns"])
            for f in d.get("foreign_keys", [])
        ]
        return cls(name=d["name"], columns=cols, foreign_keys=fks)


def serialize_row(schema: Schema, row: dict[str, Any]) -> bytes:
    bitmap = bytearray(schema.null_bitmap_size)
    packed = []
    for i, col in enumerate(schema.columns):
        val = row.get(col.name)
        if val is None:
            if not col.nullable:
                raise RuntimeError(f"Column '{col.name}' is NOT NULL")
            bitmap[i // 8] |= 1 << (i % 8)
            if col.type == INTEGER: packed.append(0)
            elif col.type == REAL:  packed.append(0.0)
            else:                   packed.append(b"")
        else:
            if col.type == INTEGER:   packed.append(int(val))
            elif col.type == REAL:    packed.append(float(val))
            else:                     packed.append(str(val).encode())
    return bytes(bitmap) + struct.pack(schema.row_format, *packed)


def deserialize_row(schema: Schema, data: bytes) -> dict[str, Any]:
    bm   = data[:schema.null_bitmap_size]
    vals = struct.unpack(schema.row_format, data[schema.null_bitmap_size:])
    row: dict[str, Any] = {}
    for i, (col, val) in enumerate(zip(schema.columns, vals)):
        if bm[i // 8] & (1 << (i % 8)):
            row[col.name] = None
        elif col.type == TEXT:
            row[col.name] = val.rstrip(b"\x00").decode()
        else:
            row[col.name] = val
    return row


# ══════════════════════════════════════════════════════════════════════════════
# B+ Tree
# ══════════════════════════════════════════════════════════════════════════════
#
# Page layout (14-byte header, same for both node types):
#
#   Offset  Size  Field
#   ──────  ────  ────────────────────────────────────────────────────────────
#   0       1     node_type    (NODE_LEAF=0, NODE_INTERNAL=1)
#   1       1     is_root      (0 / 1)
#   2       4     parent_page  (uint32; 0 for root)
#   6       4     num_cells    (uint32)
#   10      4     sibling      (uint32)
#                   leaf     → next_leaf page  (0 = end of chain)
#                   internal → leftmost_child page
#   14      …     cells
#
# Leaf cell:     key int64  |  value bytes[row_size]
# Internal cell: key int64  |  right_child uint32
#
# Internal node child ordering:
#   sibling (leftmost_child) holds keys  <  cells[0].key
#   cells[i].right_child    holds keys  ≥  cells[i].key  (and < cells[i+1].key)
#   last cell's right_child holds keys  ≥  last cell's key
#
# children[k] means:
#   k=0 → sibling (leftmost), k=i+1 → cells[i].right_child
#
# Separator between children[k] and children[k+1] = cells[k].key
#
# Split strategy (root page number NEVER changes):
#   Root split  → both halves written to new pages; root rewritten as internal.
#   Other split → old page becomes left child; one new right page allocated.
#
# Rebalancing on delete:
#   After compaction, if a leaf is underfull (< leaf_max//2 cells):
#     1. Try borrowing a cell from a sibling (rotation).
#     2. If sibling is at minimum, merge + remove separator from parent.
#     3. Recurse up if parent is now underfull.
#     4. If root ends up with 0 cells, collapse tree by one level.
# ══════════════════════════════════════════════════════════════════════════════

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


# ══════════════════════════════════════════════════════════════════════════════
# Catalog
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class TableMeta:
    schema:    Schema
    root_page: int
    next_page: int
    next_key:  int


@dataclass
class IndexMeta:
    table_name: str
    columns:    list[str]
    root_page:  int
    next_page:  int

    @property
    def column_name(self) -> str:
        return self.columns[0]


@dataclass
class Catalog:
    tables:         dict[str, TableMeta]  = field(default_factory=dict)
    indexes:        dict[str, IndexMeta]  = field(default_factory=dict)
    next_free_page: int                   = 1   # page 0 = catalog
    free_pages:     list[int]             = field(default_factory=list)

    CATALOG_PAGE = 0

    def to_bytes(self) -> bytes:
        """Return raw JSON bytes (no padding). _flush_catalog handles page splitting."""
        return json.dumps({
            "next_free_page": self.next_free_page,
            "free_pages":     self.free_pages,
            "tables": {
                n: {"schema": m.schema.to_dict(), "root_page": m.root_page,
                    "next_page": m.next_page, "next_key": m.next_key}
                for n, m in self.tables.items()
            },
            "indexes": {
                n: {"table_name": m.table_name, "columns": m.columns,
                    "root_page": m.root_page, "next_page": m.next_page}
                for n, m in self.indexes.items()
            },
        }).encode()

    @classmethod
    def from_bytes(cls, data: bytes) -> "Catalog":
        raw = data.rstrip(b"\x00")
        if not raw:
            return cls()
        d = json.loads(raw.decode())
        tables = {
            n: TableMeta(Schema.from_dict(t["schema"]), t["root_page"],
                         t["next_page"], t["next_key"])
            for n, t in d.get("tables", {}).items()
        }
        indexes = {
            n: IndexMeta(i["table_name"],
                         i["columns"] if "columns" in i else [i["column_name"]],
                         i["root_page"], i["next_page"])
            for n, i in d.get("indexes", {}).items()
        }
        return cls(tables=tables, indexes=indexes,
                   next_free_page=d.get("next_free_page", 1),
                   free_pages=d.get("free_pages", []))


# ══════════════════════════════════════════════════════════════════════════════
# Write-Ahead Log
# ══════════════════════════════════════════════════════════════════════════════
#
# File layout  (<db>.wal):
#   Offset  Size  Field
#   0       4     Magic  b"HWAL"
#   4       1     Status  0=open  1=committed
#   5       3     Padding
#   8+      …     Frames:  page_num uint32  |  page_data bytes[PAGE_SIZE]
#
# Protocol:
#   begin()   → create .wal, write header (status=open)
#   commit()  → write dirty frames, fsync, set status=committed, fsync,
#               checkpoint frames into main file, delete .wal
#   rollback()→ close + delete .wal, evict dirty pages from cache
#   on open   → if .wal exists with status=committed, replay into main file
#               then delete; if uncommitted, delete (partial txn discarded)
# ══════════════════════════════════════════════════════════════════════════════

class WAL:
    MAGIC    = b"HWAL"
    HDR_SIZE = 8
    FRAME_SZ = 4 + PAGE_SIZE

    def __init__(self, path: Path):
        self._path = path
        self._file = open(path, "w+b")
        self._file.write(self.MAGIC + b"\x00\x00\x00\x00")
        self._file.flush()

    @classmethod
    def replay_if_exists(cls, wal_path: Path, db_file) -> None:
        if not wal_path.exists():
            return
        try:
            with open(wal_path, "rb") as wf:
                hdr = wf.read(cls.HDR_SIZE)
                if len(hdr) < cls.HDR_SIZE or hdr[:4] != cls.MAGIC or hdr[4] != 1:
                    return  # corrupt or uncommitted — discard
                while True:
                    frame = wf.read(cls.FRAME_SZ)
                    if len(frame) < cls.FRAME_SZ:
                        break
                    pn = struct.unpack_from("I", frame)[0]
                    db_file.seek(pn * PAGE_SIZE)
                    db_file.write(frame[4:])
                db_file.flush()
        finally:
            wal_path.unlink(missing_ok=True)

    def commit(self, dirty: dict[int, bytearray], db_file) -> None:
        for pn, data in dirty.items():
            self._file.write(struct.pack("I", pn) + bytes(data))
        self._file.seek(4)
        self._file.write(b"\x01")   # committed
        self._file.flush()
        try:
            os.fsync(self._file.fileno())
        except OSError:
            pass
        for pn, data in dirty.items():
            db_file.seek(pn * PAGE_SIZE)
            db_file.write(data)
        db_file.flush()
        self._file.close()
        self._path.unlink(missing_ok=True)

    def rollback(self) -> None:
        self._file.close()
        self._path.unlink(missing_ok=True)


# ══════════════════════════════════════════════════════════════════════════════
# Pager
# ══════════════════════════════════════════════════════════════════════════════

class Pager:
    def __init__(self, path: Path):
        wal_path = path.with_suffix(".wal")
        self._file = open(path, "r+b" if path.exists() else "w+b")
        WAL.replay_if_exists(wal_path, self._file)
        self._path  = path
        self._cache: dict[int, bytearray] = {}
        self._dirty: set[int] = set()
        self._wal:   WAL | None = None

    def get_page(self, num: int) -> bytearray:
        if num not in self._cache:
            page = bytearray(PAGE_SIZE)
            self._file.seek(num * PAGE_SIZE)
            chunk = self._file.read(PAGE_SIZE)
            page[: len(chunk)] = chunk
            self._cache[num] = page
        self._dirty.add(num)
        return self._cache[num]

    def flush(self, num: int) -> None:
        self._dirty.add(num)

    def begin(self) -> None:
        if self._wal is not None:
            raise RuntimeError("Transaction already active")
        self._dirty.clear()
        self._wal = WAL(self._path.with_suffix(".wal"))

    def commit(self) -> None:
        if self._wal is None:
            raise RuntimeError("No active transaction")
        dirty = {n: self._cache[n] for n in self._dirty if n in self._cache}
        self._wal.commit(dirty, self._file)
        self._wal = None
        self._dirty.clear()

    def rollback(self) -> None:
        if self._wal is None:
            raise RuntimeError("No active transaction")
        self._wal.rollback()
        self._wal = None
        for n in self._dirty:
            self._cache.pop(n, None)
        self._dirty.clear()

    def close(self) -> None:
        if self._wal is not None:
            self._wal.rollback()
            self._wal = None
        self._file.flush()
        self._file.close()


def _encode_index_key(val: Any, col_type: str) -> int:
    """Encode a column value as a sort-preserving signed int64 B-tree key.

    INTEGER  — identity (already int64).
    REAL     — IEEE 754 bit-manipulation preserving float sort order.
    TEXT/VARCHAR — FNV-1a 64-bit hash (equality lookups only; collisions
                   are caught by the post-lookup row verification step).
    """
    if col_type == INTEGER:
        return int(val)
    if col_type == REAL:
        raw = struct.unpack(">Q", struct.pack(">d", float(val)))[0]
        # Negative floats: XOR bits 0-62 to reverse ordering within negatives.
        # Positive floats: raw uint64 already sorts correctly as signed int64
        # because IEEE 754 exponent is in the high bits and max float < 2^63.
        encoded = raw ^ 0x7FFFFFFFFFFFFFFF if raw >> 63 else raw
        return struct.unpack(">q", struct.pack(">Q", encoded))[0]
    # TEXT / VARCHAR — FNV-1a 64-bit
    h = 14695981039346656037
    for b in str(val).encode("utf-8"):
        h = ((h ^ b) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return h if h < (1 << 63) else h - (1 << 64)


def _encode_composite_key(vals: list[Any], col_types: list[str]) -> int:
    """Encode a list of column values into a single int64 index key.
    For a single column, delegates to _encode_index_key (same result).
    For multiple columns, FNV-1a mixes the per-column encoded keys.
    """
    if len(vals) == 1:
        return _encode_index_key(vals[0], col_types[0])
    h = 14695981039346656037
    for val, col_type in zip(vals, col_types):
        k = _encode_index_key(val, col_type)
        for b in struct.pack(">q", k):
            h = ((h ^ b) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return h if h < (1 << 63) else h - (1 << 64)


_IDX_KEY_SZ  = 16         # index B-tree: 8-byte val_key + 8-byte rowid
_KEY_SIGN    = 1 << 63   # bias to convert signed int64 → unsigned for big-endian sort


def _make_index_key(val_key: int, rowid: int) -> int:
    """Pack (val_key: signed i64, rowid: u64) into an unsigned 128-bit Python int.

    Shifting val_key by 2^63 maps the full signed range to unsigned while
    preserving sort order, so composite keys compare correctly as plain ints.
    """
    return ((val_key + _KEY_SIGN) << 64) | rowid


def _split_index_key(composite: int) -> tuple[int, int]:
    rowid   = composite & 0xFFFFFFFFFFFFFFFF
    val_key = (composite >> 64) - _KEY_SIGN
    return val_key, rowid


def _apply_order_limit(rows: list[dict], order_by: list[dict] | None,
                       limit: int | None) -> list[dict]:
    """Sort rows by ORDER BY clauses (NULLs last), then apply LIMIT."""
    if order_by:
        # Stable multi-key sort: apply keys in reverse order so the first
        # key ends up as the primary sort (Python sort is stable).
        for ob in reversed(order_by):
            col, desc = ob["col"], ob["desc"]
            non_null = [r for r in rows if r.get(col) is not None]
            null_rows = [r for r in rows if r.get(col) is None]
            try:
                non_null.sort(key=lambda r, c=col: r[c], reverse=desc)
            except TypeError:
                non_null.sort(key=lambda r, c=col: str(r[c]), reverse=desc)
            rows = non_null + null_rows   # NULLs always last
    if limit is not None:
        rows = rows[:limit]
    return rows


# ══════════════════════════════════════════════════════════════════════════════
# Database
# ══════════════════════════════════════════════════════════════════════════════

# Each catalog page: next_page(4) | chunk_len(4) | JSON data (up to 4088 bytes)
_CAT_HDR   = 8
_CAT_CHUNK = PAGE_SIZE - _CAT_HDR


class Database:
    def __init__(self, path: Path):
        self._pager          = Pager(path)
        self._catalog, self._catalog_extra = self._load_catalog()
        self._txn_depth      = 0

    # ── Transaction control ────────────────────────────────────────────────────

    @property
    def in_transaction(self) -> bool:
        return self._txn_depth > 0

    def begin(self) -> None:
        if self._txn_depth > 0:
            raise RuntimeError("Transaction already active")
        self._pager.begin()
        self._txn_depth = 1

    def commit(self) -> None:
        if self._txn_depth == 0:
            raise RuntimeError("No active transaction")
        self._flush_catalog()
        self._pager.commit()
        self._txn_depth = 0

    def rollback(self) -> None:
        if self._txn_depth == 0:
            raise RuntimeError("No active transaction")
        self._pager.rollback()
        self._reload_catalog()
        self._txn_depth = 0

    def _load_catalog(self) -> tuple["Catalog", list[int]]:
        """Read catalog JSON from page 0 and any chained continuation pages.
        Page layout: next_page(uint32) | chunk_len(uint32) | JSON bytes.
        """
        data   = b""
        extras: list[int] = []
        pn     = Catalog.CATALOG_PAGE
        while True:
            page      = self._pager.get_page(pn)
            next_pn   = struct.unpack_from("I", page, 0)[0]
            chunk_len = struct.unpack_from("I", page, 4)[0]
            if chunk_len:
                data += bytes(page[_CAT_HDR: _CAT_HDR + chunk_len])
            if next_pn == 0:
                break
            extras.append(next_pn)
            pn = next_pn
        return Catalog.from_bytes(data), extras

    def _reload_catalog(self) -> None:
        self._catalog, self._catalog_extra = self._load_catalog()

    # ── DDL ────────────────────────────────────────────────────────────────────

    def create_table(self, schema: Schema) -> None:
        if schema.name in self._catalog.tables:
            raise RuntimeError(f"Table '{schema.name}' already exists")
        root = self._alloc_page()
        BTree.init_root_leaf(self._pager, root)
        self._catalog.tables[schema.name] = TableMeta(
            schema=schema, root_page=root,
            next_page=self._catalog.next_free_page, next_key=1,
        )

    def drop_table(self, name: str) -> None:
        meta = self._meta(name)
        for pn in self._collect_tree_pages(meta.root_page):
            self._free_page(pn)
        to_drop = [n for n, m in self._catalog.indexes.items()
                   if m.table_name == name]
        for n in to_drop:
            for pn in self._collect_tree_pages(self._catalog.indexes[n].root_page,
                                               key_sz=_IDX_KEY_SZ):
                self._free_page(pn)
            del self._catalog.indexes[n]
        del self._catalog.tables[name]

    def alter_add_column(self, table: str, col: Column) -> None:
        meta = self._meta(table)
        if any(c.name == col.name for c in meta.schema.columns):
            raise RuntimeError(f"Column '{col.name}' already exists in '{table}'")
        old_schema = meta.schema
        new_schema = Schema(old_schema.name, old_schema.columns + [col])
        self._rewrite_table(meta, old_schema, new_schema)

    def alter_drop_column(self, table: str, col_name: str) -> None:
        meta = self._meta(table)
        old_schema = meta.schema
        if not any(c.name == col_name for c in old_schema.columns):
            raise RuntimeError(f"Column '{col_name}' not found in '{table}'")
        if len(old_schema.columns) == 1:
            raise RuntimeError("Cannot drop the only column")
        new_cols = [c for c in old_schema.columns if c.name != col_name]
        new_schema = Schema(old_schema.name, new_cols)
        # Drop any index that references the removed column
        dead = [n for n, m in self._catalog.indexes.items()
                if m.table_name == table and col_name in m.columns]
        for n in dead:
            del self._catalog.indexes[n]
        self._rewrite_table(meta, old_schema, new_schema)

    def alter_rename_column(self, table: str, old_name: str, new_name: str) -> None:
        meta = self._meta(table)
        old_schema = meta.schema
        if not any(c.name == old_name for c in old_schema.columns):
            raise RuntimeError(f"Column '{old_name}' not found in '{table}'")
        if any(c.name == new_name for c in old_schema.columns):
            raise RuntimeError(f"Column '{new_name}' already exists in '{table}'")
        new_cols = [
            Column(new_name, c.type, c.size, c.nullable) if c.name == old_name else c
            for c in old_schema.columns
        ]
        # Rename only changes schema metadata — binary layout is identical
        meta.schema = Schema(old_schema.name, new_cols)
        for idx in self._catalog.indexes.values():
            if idx.table_name == table and old_name in idx.columns:
                idx.columns = [new_name if c == old_name else c for c in idx.columns]

    def alter_rename_table(self, old_name: str, new_name: str) -> None:
        if new_name in self._catalog.tables:
            raise RuntimeError(f"Table '{new_name}' already exists")
        meta = self._meta(old_name)
        meta.schema = Schema(new_name, meta.schema.columns)
        self._catalog.tables[new_name] = meta
        del self._catalog.tables[old_name]
        for idx in self._catalog.indexes.values():
            if idx.table_name == old_name:
                idx.table_name = new_name

    def _rewrite_table(self, meta: "TableMeta", old_schema: Schema,
                       new_schema: Schema) -> None:
        """Scan old tree, reserialize rows with new_schema, rebuild on a fresh root.
        Called when the binary row format changes (ADD/DROP COLUMN).
        Old pages become unreachable but that is acceptable (no free-page list yet).
        """
        old_tree  = BTree(self._pager, meta.root_page, old_schema.row_size,
                          self._make_alloc(meta))
        saved     = [(rowid, deserialize_row(old_schema, raw))
                     for rowid, raw in old_tree.scan()]
        old_pages = self._collect_tree_pages(meta.root_page)

        new_root = self._alloc_page()
        BTree.init_root_leaf(self._pager, new_root)
        meta.root_page = new_root
        meta.schema    = new_schema
        new_tree = self._table_btree(meta)
        for rowid, old_row in saved:
            new_row = {c.name: old_row.get(c.name) for c in new_schema.columns}
            new_tree.insert(rowid, serialize_row(new_schema, new_row))

        # Rebuild affected indexes (rowids are preserved; only root page changes)
        old_idx_pages: list[int] = []
        for idx in self._catalog.indexes.values():
            if idx.table_name != new_schema.name:
                continue
            if not all(any(c.name == col for c in new_schema.columns)
                       for col in idx.columns):
                continue
            old_idx_pages.extend(self._collect_tree_pages(idx.root_page,
                                                          key_sz=_IDX_KEY_SZ))
            idx_root = self._alloc_page()
            BTree.init_root_leaf(self._pager, idx_root)
            idx.root_page = idx_root
            itree     = self._index_btree(idx)
            col_types = [next(c.type for c in new_schema.columns if c.name == n)
                         for n in idx.columns]
            for rowid, old_row in saved:
                vals = [old_row.get(n) for n in idx.columns]
                if all(v is not None for v in vals):
                    itree.insert(
                        _make_index_key(_encode_composite_key(vals, col_types), rowid),
                        struct.pack("q", rowid))

        # Return old table and index pages to the free list
        for pn in old_pages + old_idx_pages:
            self._free_page(pn)

    def create_index(self, idx_name: str, table: str, cols: list[str]) -> None:
        if idx_name in self._catalog.indexes:
            raise RuntimeError(f"Index '{idx_name}' already exists")
        meta = self._meta(table)
        for col in cols:
            if not any(c.name == col for c in meta.schema.columns):
                raise RuntimeError(f"Column '{col}' not found in '{table}'")
        root = self._alloc_page()
        BTree.init_root_leaf(self._pager, root)
        idx_meta = IndexMeta(table_name=table, columns=cols,
                             root_page=root,
                             next_page=self._catalog.next_free_page)
        self._catalog.indexes[idx_name] = idx_meta
        # Back-fill existing rows
        tree      = self._table_btree(meta)
        itree     = self._index_btree(idx_meta)
        schema    = meta.schema
        col_types = [next(c.type for c in schema.columns if c.name == n) for n in cols]
        for rowid, raw in tree.scan():
            row  = deserialize_row(schema, raw)
            vals = [row.get(n) for n in cols]
            if all(v is not None for v in vals):
                itree.insert(
                    _make_index_key(_encode_composite_key(vals, col_types), rowid),
                    struct.pack("q", rowid))

    def drop_index(self, idx_name: str) -> None:
        if idx_name not in self._catalog.indexes:
            raise RuntimeError(f"Index '{idx_name}' does not exist")
        for pn in self._collect_tree_pages(self._catalog.indexes[idx_name].root_page,
                                           key_sz=_IDX_KEY_SZ):
            self._free_page(pn)
        del self._catalog.indexes[idx_name]

    # ── DML ────────────────────────────────────────────────────────────────────

    def _check_unique(self, meta: "TableMeta", row: dict[str, Any],
                      exclude_rowid: int | None = None) -> None:
        """Raise if any UNIQUE column in row duplicates an existing value."""
        schema      = meta.schema
        unique_cols = [c for c in schema.columns if c.unique]
        if not unique_cols:
            return
        # Coerce new-row values to their storage types for comparison
        typed: dict[str, Any] = {}
        for col in unique_cols:
            v = row.get(col.name)
            if v is None:
                typed[col.name] = None
            elif col.type == INTEGER:
                typed[col.name] = int(v)
            elif col.type == REAL:
                typed[col.name] = float(v)
            else:
                typed[col.name] = str(v)
        for rowid, raw in self._table_btree(meta).scan():
            if rowid == exclude_rowid:
                continue
            existing = deserialize_row(schema, raw)
            for col in unique_cols:
                v = typed[col.name]
                if v is not None and existing.get(col.name) == v:
                    raise RuntimeError(
                        f"UNIQUE constraint failed: {schema.name}.{col.name}"
                    )

    def _check_constraints(self, schema: Schema, row: dict[str, Any]) -> None:
        """Raise if any column CHECK expression evaluates to False for the given row."""
        for col in schema.columns:
            if col.check is None:
                continue
            tokens = _tokenize(col.check)
            try:
                wc, _ = _parse_one_condition(tokens, 0)
            except ParseError as e:
                raise RuntimeError(f"Invalid CHECK expression on '{col.name}': {e}")
            if not wc.evaluate(row, self):
                raise RuntimeError(
                    f"CHECK constraint failed: {schema.name}.{col.name} CHECK ({col.check})"
                )

    def _fks_referencing(self, table: str) -> list[ForeignKey]:
        """Return all ForeignKey objects from any table that reference `table`."""
        result: list[ForeignKey] = []
        for tmeta in self.tables.values():
            for fk in tmeta.schema.foreign_keys:
                if fk.ref_table.lower() == table.lower():
                    result.append(fk)
        return result

    def _check_fk_child(self, schema: Schema, row: dict[str, Any]) -> None:
        """Raise if any FK column values are not present in the referenced parent table."""
        for fk in schema.foreign_keys:
            vals = []
            for col_name in fk.columns:
                v = row.get(col_name)
                if v is not None:
                    col_obj = next((c for c in schema.columns if c.name == col_name), None)
                    if col_obj and col_obj.type == INTEGER:
                        v = int(v)
                    elif col_obj and col_obj.type == REAL:
                        v = float(v)
                vals.append(v)
            if any(v is None for v in vals):
                continue  # NULL FK values are permitted
            if fk.ref_table not in self.tables:
                raise RuntimeError(
                    f"Foreign key references unknown table '{fk.ref_table}'"
                )
            parent_meta = self._meta(fk.ref_table)
            parent_schema = parent_meta.schema
            found = False
            for _, raw in self._table_btree(parent_meta).scan():
                parent_row = deserialize_row(parent_schema, raw)
                if all(parent_row.get(rc) == v
                       for rc, v in zip(fk.ref_columns, vals)):
                    found = True
                    break
            if not found:
                raise RuntimeError(
                    f"FOREIGN KEY constraint failed: "
                    f"{schema.name}({', '.join(fk.columns)}) "
                    f"→ {fk.ref_table}({', '.join(fk.ref_columns)})"
                )

    def _check_fk_parent(self, table: str, old_row: dict[str, Any]) -> None:
        """Raise (RESTRICT) if any child row references `old_row` via a FK."""
        for tname, tmeta in self.tables.items():
            for fk in tmeta.schema.foreign_keys:
                if fk.ref_table.lower() != table.lower():
                    continue
                ref_vals = [old_row.get(c) for c in fk.ref_columns]
                if any(v is None for v in ref_vals):
                    continue
                child_schema = tmeta.schema
                for _, raw in self._table_btree(tmeta).scan():
                    child_row = deserialize_row(child_schema, raw)
                    if all(child_row.get(cc) == rv
                           for cc, rv in zip(fk.columns, ref_vals)):
                        raise RuntimeError(
                            f"FOREIGN KEY constraint failed: cannot modify '{table}' "
                            f"— row is referenced by '{tname}'"
                        )

    def insert(self, table: str, row: dict[str, Any]) -> None:
        meta  = self._meta(table)
        self._check_unique(meta, row)
        self._check_constraints(meta.schema, row)
        self._check_fk_child(meta.schema, row)
        rowid = meta.next_key
        meta.next_key += 1
        data  = serialize_row(meta.schema, row)
        self._table_btree(meta).insert(rowid, data)
        # Maintain indexes
        schema = meta.schema
        for idx_meta in self._indexes_for(table):
            vals = [row.get(n) for n in idx_meta.columns]
            if all(v is not None for v in vals):
                col_types = [next(c.type for c in schema.columns if c.name == n)
                             for n in idx_meta.columns]
                self._index_btree(idx_meta).insert(
                    _make_index_key(_encode_composite_key(vals, col_types), rowid),
                    struct.pack("q", rowid))

    def select(self, table: str, columns: list[str] | None,
               where: "WhereClause | None",
               order_by: list[dict] | None = None,
               limit: int | None = None,
               group_by: list[str] | None = None,
               having: "WhereClause | None" = None,
               distinct: bool = False) -> list[dict[str, Any]]:
        meta = self._meta(table)
        # GROUP BY mode: bucket rows, then aggregate per group
        if group_by:
            return self._group_by_select(meta, columns, where, group_by, having,
                                         order_by, limit)
        # Aggregate mode (no grouping): any column is a function call
        if columns and any(_parse_agg(c) is not None for c in columns):
            return self._aggregate_select(meta, columns, where)
        # Index-accelerated exact lookup (only when no ORDER BY / LIMIT / DISTINCT)
        if where and not order_by and limit is None and not distinct:
            idx, eq_cols = self._find_index_for_where(table, where)
            if idx:
                return self._index_select(meta, idx, eq_cols, columns)
            # Index-accelerated range scan (single-column INTEGER/REAL, no OR)
            idx, range_cond = self._find_index_for_range(table, where)
            if idx:
                return self._index_range_select(meta, idx, range_cond, where, columns)
        # Full scan
        schema  = meta.schema
        results = []
        seen: set[tuple] = set()
        for _, raw in self._table_btree(meta).scan():
            row = deserialize_row(schema, raw)
            if where and not where.evaluate(row, self):
                continue
            projected = {k: row[k] for k in columns} if columns else row
            if distinct:
                key = tuple(projected.get(k) for k in (columns or list(row.keys())))
                if key in seen:
                    continue
                seen.add(key)
            results.append(projected)
        return _apply_order_limit(results, order_by, limit)

    def _compute_aggregates(self, bucket_rows: list[dict],
                            columns: list[str]) -> dict[str, Any]:
        """Compute aggregate functions over bucket_rows for the given select columns."""
        result: dict[str, Any] = {}
        for col in columns:
            if col in result:
                continue
            agg = _parse_agg(col)
            if agg is None:
                result[col] = bucket_rows[0].get(col) if bucket_rows else None
                continue
            func, arg = agg
            if func == "COUNT":
                result[col] = (len(bucket_rows) if arg == "*"
                               else sum(1 for r in bucket_rows if r.get(arg) is not None))
            else:
                vals = [r[arg] for r in bucket_rows
                        if r.get(arg) is not None and arg in r]
                if not vals:
                    result[col] = None
                elif func == "MIN":  result[col] = min(vals)
                elif func == "MAX":  result[col] = max(vals)
                elif func == "SUM":  result[col] = sum(vals)
                elif func == "AVG":  result[col] = sum(vals) / len(vals)
        return result

    def _aggregate_select(self, meta: "TableMeta", columns: list[str],
                          where: "WhereClause | None") -> list[dict[str, Any]]:
        schema = meta.schema
        rows: list[dict] = []
        for _, raw in self._table_btree(meta).scan():
            row = deserialize_row(schema, raw)
            if where and not where.evaluate(row, self):
                continue
            rows.append(row)
        return [self._compute_aggregates(rows, columns)]

    def _group_by_select(self, meta: "TableMeta", columns: list[str] | None,
                         where: "WhereClause | None", group_by: list[str],
                         having: "WhereClause | None",
                         order_by: list[dict] | None,
                         limit: int | None) -> list[dict[str, Any]]:
        schema = meta.schema
        # Scan with WHERE filter
        all_rows: list[dict] = []
        for _, raw in self._table_btree(meta).scan():
            row = deserialize_row(schema, raw)
            if where and not where.evaluate(row, self):
                continue
            all_rows.append(row)
        # Bucket by GROUP BY key (preserving insertion order for stable output)
        buckets: dict[tuple, list[dict]] = {}
        for row in all_rows:
            key = tuple(row.get(c) for c in group_by)
            if key not in buckets:
                buckets[key] = []
            buckets[key].append(row)
        # Compute one result row per bucket
        select_cols = columns if columns else group_by
        results: list[dict] = []
        for key, bucket_rows in buckets.items():
            result: dict[str, Any] = {}
            for gc, kv in zip(group_by, key):
                result[gc] = kv
            result.update(self._compute_aggregates(bucket_rows, select_cols))
            if having and not having.evaluate(result, self):
                continue
            results.append({c: result[c] for c in select_cols if c in result}
                           if columns else result)
        return _apply_order_limit(results, order_by, limit)

    def join(self, left_table: str, right_table: str,
             on_left: str | None, on_right: str | None,
             columns: list[str] | None,
             where: "WhereClause | None",
             order_by: list[dict] | None = None,
             limit: int | None = None,
             join_type: str = "INNER",
             left_alias: str | None = None,
             right_alias: str | None = None) -> list[dict[str, Any]]:
        lmeta, rmeta = self._meta(left_table), self._meta(right_table)
        left_rows  = [deserialize_row(lmeta.schema, r)
                      for _, r in self._table_btree(lmeta).scan()]
        right_rows = [deserialize_row(rmeta.schema, r)
                      for _, r in self._table_btree(rmeta).scan()]

        la = left_alias  or left_table
        ra = right_alias or right_table

        left_null  = {f"{la}.{c.name}": None for c in lmeta.schema.columns}
        right_null = {f"{ra}.{c.name}": None for c in rmeta.schema.columns}

        def _merge(lr: dict, rr: dict) -> dict:
            m = {f"{la}.{k}": v for k, v in lr.items()}
            m.update({f"{ra}.{k}": v for k, v in rr.items()})
            return m

        def _project(merged: dict) -> dict:
            return {c: merged[c] for c in columns if c in merged} if columns else merged

        def _emit(merged: dict) -> dict | None:
            if where and not where.evaluate(merged, self):
                return None
            return _project(merged)

        results: list[dict] = []

        # ── CROSS JOIN ────────────────────────────────────────────────────────
        if join_type == "CROSS":
            for lr in left_rows:
                for rr in right_rows:
                    row = _emit(_merge(lr, rr))
                    if row is not None:
                        results.append(row)
            return _apply_order_limit(results, order_by, limit)

        # ── NATURAL JOIN ──────────────────────────────────────────────────────
        if join_type == "NATURAL":
            lcols  = {c.name for c in lmeta.schema.columns}
            rcols  = {c.name for c in rmeta.schema.columns}
            shared = sorted(lcols & rcols)
            for lr in left_rows:
                for rr in right_rows:
                    # NULL values do not match (SQL NULL semantics)
                    if any(lr.get(c) is None or rr.get(c) is None
                           or lr.get(c) != rr.get(c) for c in shared):
                        continue
                    row = _emit(_merge(lr, rr))
                    if row is not None:
                        results.append(row)
            return _apply_order_limit(results, order_by, limit)

        # ── ON-clause joins: INNER / LEFT / RIGHT / FULL ─────────────────────
        lcol = on_left.split(".")[-1]   # type: ignore[union-attr]
        rcol = on_right.split(".")[-1]  # type: ignore[union-attr]
        matched_right: set[int] = set()

        for lr in left_rows:
            on_matched = False
            for j, rr in enumerate(right_rows):
                if lr.get(lcol) != rr.get(rcol):
                    continue
                on_matched = True
                matched_right.add(j)
                row = _emit(_merge(lr, rr))
                if row is not None:
                    results.append(row)
            # LEFT / FULL: emit unmatched left row padded with right NULLs
            if not on_matched and join_type in ("LEFT", "FULL"):
                merged = {f"{la}.{k}": v for k, v in lr.items()}
                merged.update(right_null)
                row = _emit(merged)
                if row is not None:
                    results.append(row)

        # RIGHT / FULL: emit unmatched right rows padded with left NULLs
        if join_type in ("RIGHT", "FULL"):
            for j, rr in enumerate(right_rows):
                if j in matched_right:
                    continue
                merged = dict(left_null)
                merged.update({f"{ra}.{k}": v for k, v in rr.items()})
                row = _emit(merged)
                if row is not None:
                    results.append(row)

        return _apply_order_limit(results, order_by, limit)

    def update(self, table: str, assignments: dict[str, str],
               where: "WhereClause | None") -> int:
        meta   = self._meta(table)
        schema = meta.schema
        tree   = self._table_btree(meta)
        idxs   = self._indexes_for(table)
        updates: dict[int, bytes] = {}
        idx_ops: list[tuple] = []   # (im, old_key|None, new_key|None, rowid)

        for rowid, raw in tree.scan():
            row = deserialize_row(schema, raw)
            if where and not where.evaluate(row, self):
                continue
            new_row = dict(row)
            for col, val in assignments.items():
                col_obj = next((c for c in schema.columns if c.name == col), None)
                if col_obj is None:
                    raise RuntimeError(f"Column '{col}' not found")
                if col_obj.type == INTEGER:
                    new_row[col] = int(val)
                elif col_obj.type == REAL:
                    new_row[col] = float(val)
                else:
                    new_row[col] = val
            self._check_unique(meta, new_row, exclude_rowid=rowid)
            self._check_constraints(schema, new_row)
            self._check_fk_child(schema, new_row)
            # RESTRICT: if this table's FK-referenced columns are changing, check children
            fks_ref = self._fks_referencing(table)
            if fks_ref:
                ref_cols_set = {c for fk in fks_ref for c in fk.ref_columns}
                if ref_cols_set & assignments.keys():
                    self._check_fk_parent(table, row)
            updates[rowid] = serialize_row(schema, new_row)
            for im in idxs:
                if any(c in assignments for c in im.columns):
                    col_types = [next(ct.type for ct in schema.columns if ct.name == n)
                                 for n in im.columns]
                    old_vals = [row.get(c) for c in im.columns]
                    new_vals = [new_row.get(c) for c in im.columns]
                    old_k = (_make_index_key(_encode_composite_key(old_vals, col_types), rowid)
                             if all(v is not None for v in old_vals) else None)
                    new_k = (_make_index_key(_encode_composite_key(new_vals, col_types), rowid)
                             if all(v is not None for v in new_vals) else None)
                    idx_ops.append((im, old_k, new_k, rowid))

        tree.update(updates)
        for im, old_k, new_k, rowid in idx_ops:
            itree = self._index_btree(im)
            if old_k is not None:
                itree.delete({old_k})
            if new_k is not None:
                itree.insert(new_k, struct.pack("q", rowid))
        return len(updates)

    def delete(self, table: str, where: "WhereClause | None") -> int:
        meta   = self._meta(table)
        schema = meta.schema
        tree   = self._table_btree(meta)
        idxs   = self._indexes_for(table)
        # Collect (rowid, row) for matching rows
        victims: list[tuple[int, dict]] = []
        for rowid, raw in tree.scan():
            row = deserialize_row(schema, raw)
            if not where or where.evaluate(row, self):
                victims.append((rowid, row))
        if not victims:
            return 0
        for _, row in victims:
            self._check_fk_parent(table, row)
        rowids = {r for r, _ in victims}
        tree.delete(rowids)
        # Maintain indexes
        for im in idxs:
            col_types = [next(c.type for c in schema.columns if c.name == n)
                         for n in im.columns]
            itree     = self._index_btree(im)
            idx_keys: set[int] = set()
            for rowid, row in victims:
                vals = [row.get(n) for n in im.columns]
                if all(v is not None for v in vals):
                    idx_keys.add(
                        _make_index_key(_encode_composite_key(vals, col_types), rowid))
            itree.delete(idx_keys)
        return len(victims)

    # ── Index-accelerated select ───────────────────────────────────────────────

    def _index_select(self, meta: "TableMeta", idx_meta: "IndexMeta",
                      eq_cols: dict[str, str], columns: list[str] | None
                      ) -> list[dict[str, Any]]:
        schema    = meta.schema
        col_types = []
        for col_name in idx_meta.columns:
            col_obj = next((c for c in schema.columns if c.name == col_name), None)
            if col_obj is None:
                return []
            col_types.append(col_obj.type)
        vals = [eq_cols[n] for n in idx_meta.columns]
        try:
            val_key = _encode_composite_key(vals, col_types)
        except (ValueError, TypeError):
            return []
        # Range scan: all composite keys sharing this val_key prefix
        lo    = _make_index_key(val_key, 0)
        hi    = _make_index_key(val_key, 0xFFFFFFFFFFFFFFFF)
        itree = self._index_btree(idx_meta)
        ptree = self._table_btree(meta)
        results: list[dict] = []
        for _, rowid_raw in itree.scan_range(lo, hi):
            rowid = struct.unpack("q", rowid_raw)[0]
            raw   = ptree.lookup(rowid)
            if raw is None:
                continue
            row = deserialize_row(schema, raw)
            # Collision guard: TEXT FNV hashes may alias; verify all columns match
            match = True
            for col_name, val in eq_cols.items():
                col_type = next(c.type for c in schema.columns if c.name == col_name)
                actual   = row.get(col_name)
                if col_type == TEXT:
                    if str(actual) != str(val):
                        match = False; break
                elif col_type == INTEGER:
                    try:
                        if actual != int(val): match = False; break
                    except (ValueError, TypeError):
                        match = False; break
                elif col_type == REAL:
                    try:
                        if actual != float(val): match = False; break
                    except (ValueError, TypeError):
                        match = False; break
            if match:
                results.append({k: row[k] for k in columns} if columns else row)
        return results

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _meta(self, name: str) -> TableMeta:
        if name not in self._catalog.tables:
            raise RuntimeError(f"No such table: '{name}'")
        return self._catalog.tables[name]

    def _alloc_page(self) -> int:
        if self._catalog.free_pages:
            return self._catalog.free_pages.pop()
        pn = self._catalog.next_free_page
        self._catalog.next_free_page += 1
        return pn

    def _free_page(self, pn: int) -> None:
        self._catalog.free_pages.append(pn)

    def _collect_tree_pages(self, root: int, *, key_sz: int = 8) -> list[int]:
        """Walk a B+ tree and return all page numbers it occupies."""
        int_cell = key_sz + BTree.CHILD_SZ
        pages: list[int] = []
        visited: set[int] = set()
        stack = [root]
        while stack:
            pn = stack.pop()
            if pn == 0 or pn in visited:
                continue
            visited.add(pn)
            pages.append(pn)
            page = self._pager.get_page(pn)
            n_cells = struct.unpack_from("I", page, 6)[0]
            sibling  = struct.unpack_from("I", page, 10)[0]
            if page[0] == BTree.NODE_INTERNAL:
                stack.append(sibling)   # leftmost child
                for i in range(n_cells):
                    rc = struct.unpack_from("I", page,
                                           BTree.HDR + i * int_cell + key_sz)[0]
                    stack.append(rc)
            else:
                if sibling:
                    stack.append(sibling)
        return pages

    def _table_btree(self, meta: TableMeta) -> BTree:
        return BTree(self._pager, meta.root_page, meta.schema.row_size,
                     self._make_alloc(meta))

    def _index_btree(self, idx: IndexMeta) -> BTree:
        # Index leaf: key = 16-byte composite (val_key|rowid), value = rowid (8 bytes)
        return BTree(self._pager, idx.root_page, 8, self._make_idx_alloc(idx),
                     key_sz=_IDX_KEY_SZ)

    def _make_alloc(self, meta: TableMeta) -> Callable[[], int]:
        def alloc() -> int:
            pn = self._catalog.next_free_page
            self._catalog.next_free_page += 1
            meta.next_page = pn + 1
            return pn
        return alloc

    def _make_idx_alloc(self, idx: IndexMeta) -> Callable[[], int]:
        def alloc() -> int:
            pn = self._catalog.next_free_page
            self._catalog.next_free_page += 1
            idx.next_page = pn + 1
            return pn
        return alloc

    def _indexes_for(self, table: str) -> list[IndexMeta]:
        return [m for m in self._catalog.indexes.values()
                if m.table_name == table]

    def _find_index_for_where(self, table: str,
                              where: "WhereClause | None"
                              ) -> tuple["IndexMeta | None", dict[str, str]]:
        """Return (IndexMeta, eq_dict) if a pure AND-equality where chain matches an index.
        OR queries and non-equality operators cannot use an index and return (None, {}).
        """
        if not where or where.or_clause is not None:
            return None, {}
        eq: dict[str, str] = {}
        cond: WhereClause | None = where
        while cond is not None:
            if cond.op != "=" or cond.or_clause is not None:
                return None, {}
            eq[cond.col] = cond.val
            cond = cond.and_clause
        for m in self._catalog.indexes.values():
            if m.table_name == table and set(m.columns) == set(eq.keys()):
                return m, eq
        return None, {}

    _RANGE_OPS = {">", ">=", "<", "<="}

    def _find_index_for_range(self, table: str, where: "WhereClause | None"
                              ) -> tuple["IndexMeta | None", "WhereClause | None"]:
        """Return (IndexMeta, range_condition) for the first AND-chain condition that
        uses a range op on a single-column INTEGER/REAL index.  OR clauses block this.
        TEXT columns are excluded: their FNV hash encoding does not preserve order.
        """
        if not where or where.or_clause is not None:
            return None, None
        meta = self._meta(table)
        cond: WhereClause | None = where
        while cond is not None:
            if cond.op in self._RANGE_OPS and cond.or_clause is None:
                for m in self._catalog.indexes.values():
                    if m.table_name == table and m.columns == [cond.col]:
                        col_obj = next(
                            (c for c in meta.schema.columns if c.name == cond.col), None
                        )
                        if col_obj and col_obj.type != TEXT:
                            return m, cond
            cond = cond.and_clause
        return None, None

    def _index_range_select(self, meta: "TableMeta", idx_meta: "IndexMeta",
                            range_cond: "WhereClause", where: "WhereClause | None",
                            columns: list[str] | None) -> list[dict[str, Any]]:
        """Scan an index for all entries satisfying range_cond, then post-filter
        with the full WHERE clause (handles additional AND conditions).
        """
        schema   = meta.schema
        col_name = range_cond.col
        col_obj  = next(c for c in schema.columns if c.name == col_name)
        try:
            val_key = _encode_index_key(range_cond.val, col_obj.type)
        except (ValueError, TypeError):
            return []

        _MAX_ROWID = 0xFFFFFFFFFFFFFFFF
        _MAX_KEY   = (_MAX_ROWID << 64) | _MAX_ROWID

        op = range_cond.op
        if op == ">=":
            lo, hi = _make_index_key(val_key, 0),          _MAX_KEY
        elif op == ">":
            lo, hi = _make_index_key(val_key, _MAX_ROWID) + 1, _MAX_KEY
        elif op == "<=":
            lo, hi = 0,                                     _make_index_key(val_key, _MAX_ROWID)
        else:  # "<"
            lo, hi = 0,                                     _make_index_key(val_key, 0) - 1

        itree   = self._index_btree(idx_meta)
        ptree   = self._table_btree(meta)
        results: list[dict] = []
        for _, rowid_raw in itree.scan_range(lo, hi):
            rowid = struct.unpack("q", rowid_raw)[0]
            raw   = ptree.lookup(rowid)
            if raw is None:
                continue
            row = deserialize_row(schema, raw)
            if where and not where.evaluate(row, self):
                continue
            results.append({k: row[k] for k in columns} if columns else row)
        return results

    def _flush_catalog(self) -> None:
        """Write the catalog JSON across as many pages as needed.
        Iterates up to 4 times to stabilise page count after alloc/free changes the JSON.
        """
        for _ in range(4):
            payload  = self._catalog.to_bytes()
            n_needed = max(1, (len(payload) + _CAT_CHUNK - 1) // _CAT_CHUNK)
            n_have   = 1 + len(self._catalog_extra)
            if n_needed == n_have:
                break                              # stable — no page list change
            if n_needed > n_have:
                for _ in range(n_needed - n_have):
                    self._catalog_extra.append(self._alloc_page())
            else:
                freed = self._catalog_extra[n_needed - 1:]
                self._catalog_extra = self._catalog_extra[:n_needed - 1]
                for pn in freed:
                    self._free_page(pn)
        # Final write (re-serialise once more in case the loop changed free_pages)
        payload  = self._catalog.to_bytes()
        all_pns  = [Catalog.CATALOG_PAGE] + self._catalog_extra
        chunks   = [payload[i: i + _CAT_CHUNK]
                    for i in range(0, len(payload), _CAT_CHUNK)]
        while len(chunks) < len(all_pns):
            chunks.append(b"")
        for i, (pn, chunk) in enumerate(zip(all_pns, chunks)):
            page    = self._pager.get_page(pn)
            next_pn = all_pns[i + 1] if i + 1 < len(all_pns) else 0
            struct.pack_into("I", page, 0, next_pn)
            struct.pack_into("I", page, 4, len(chunk))
            page[_CAT_HDR: _CAT_HDR + len(chunk)] = chunk
            page[_CAT_HDR + len(chunk):]           = bytearray(PAGE_SIZE - _CAT_HDR - len(chunk))
            self._pager.flush(pn)

    @property
    def tables(self) -> dict[str, TableMeta]:
        return self._catalog.tables

    @property
    def indexes(self) -> dict[str, IndexMeta]:
        return self._catalog.indexes

    def close(self) -> None:
        self._pager.close()


# ══════════════════════════════════════════════════════════════════════════════
# WHERE clause
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class WhereClause:
    col:          str
    op:           str
    val:          str
    and_clause:   "WhereClause | None" = None
    or_clause:    "WhereClause | None" = None
    subquery_ast: "dict | None"        = None

    # ── Public entry point ─────────────────────────────────────────────────────
    def evaluate(self, row: dict[str, Any], db: Any = None) -> bool:
        """Evaluate (self AND and_chain) OR or_clause with correct SQL precedence."""
        and_result = self._eval_and_chain(row, db)
        if not and_result and self.or_clause:
            return self.or_clause.evaluate(row, db)
        return and_result

    def _eval_and_chain(self, row: dict[str, Any], db: Any = None) -> bool:
        if not self._eval_atom(row, db):
            return False
        return self.and_clause._eval_and_chain(row, db) if self.and_clause else True

    def _eval_atom(self, row: dict[str, Any], db: Any = None) -> bool:
        # EXISTS / NOT EXISTS — re-executed per outer row (supports correlation)
        if self.op in ("EXISTS", "NOT EXISTS"):
            sub_rows = _exec_correlated_subquery(self.subquery_ast, db, row)
            return bool(sub_rows) if self.op == "EXISTS" else not bool(sub_rows)

        # col lookup — strip table/alias prefix when exact key absent
        cell = row.get(self.col)
        if cell is None and "." in self.col:
            cell = row.get(self.col.split(".", 1)[1])

        if self.op == "IS NULL":     return cell is None
        if self.op == "IS NOT NULL": return cell is not None
        if cell is None:             return False

        if self.op in ("IN", "NOT IN"):
            if self.subquery_ast is not None:
                sub_rows = _exec_correlated_subquery(self.subquery_ast, db, row)
                fk = next(iter(sub_rows[0])) if sub_rows else None
                in_vals: list = [r[fk] for r in sub_rows] if fk else []
                result = cell in in_vals
            else:
                in_vals_str = [v.strip() for v in self.val.split(",")]
                if isinstance(cell, int):
                    try:    result = any(cell == int(v) for v in in_vals_str)
                    except ValueError: result = False
                elif isinstance(cell, float):
                    try:    result = any(cell == float(v) for v in in_vals_str)
                    except ValueError: result = False
                else:
                    result = str(cell) in in_vals_str
            return result if self.op == "IN" else not result

        val: Any = self.val
        if self.subquery_ast is not None:
            sub_rows = _exec_correlated_subquery(self.subquery_ast, db, row)
            if not sub_rows:
                return False
            fk = next(iter(sub_rows[0]))
            val = sub_rows[0][fk]

        if not isinstance(val, (int, float)) and isinstance(cell, (int, float)):
            try:
                val = type(cell)(val)
            except (ValueError, TypeError):
                return False
        match self.op:
            case "=":    return cell == val
            case "!=":   return cell != val
            case "<":    return cell < val
            case ">":    return cell > val
            case "<=":   return cell <= val
            case ">=":   return cell >= val
            case "LIKE":
                regex = "".join(
                    ".*" if ch == "%" else "." if ch == "_" else re.escape(ch)
                    for ch in str(val)
                )
                return bool(re.fullmatch(regex, str(cell), re.IGNORECASE))
        return False


# ══════════════════════════════════════════════════════════════════════════════
# Parser
# ══════════════════════════════════════════════════════════════════════════════

class ParseError(ValueError):
    pass

_TOKEN_RE = re.compile(r"'[^']*'|\"[^\"]*\"|\w+\([^)]*\)|[(),;*]|[^\s(),;*]+")

# Aggregate function detection
_AGG_RE = re.compile(r"^(COUNT|MIN|MAX|SUM|AVG)\(([^)]*)\)$", re.IGNORECASE)

# Keywords that cannot be bare table aliases
_ALIAS_BLOCKLIST = frozenset({
    "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "NATURAL", "JOIN", "ON", "AS",
    "WHERE", "GROUP", "ORDER", "LIMIT", "HAVING",
    "AND", "OR", "NOT", "IN", "IS", "LIKE", "SET", "FROM",
})


def _parse_table_alias(tokens: list[str], pos: int, table: str) -> tuple[str, int]:
    """Consume an optional [AS] alias after a table name.  Returns (alias, new_pos)."""
    if pos < len(tokens) and tokens[pos].upper() == "AS":
        pos += 1
        return tokens[pos], pos + 1
    if (pos < len(tokens)
            and tokens[pos] not in (",", "(", ")", ";", "=")
            and tokens[pos].upper() not in _ALIAS_BLOCKLIST):
        return tokens[pos], pos + 1
    return table, pos


def _parse_agg(col: str) -> tuple[str, str] | None:
    """If col is an aggregate call like MIN(id), return (FUNC_UPPER, arg). Else None."""
    m = _AGG_RE.match(col)
    return (m.group(1).upper(), m.group(2).strip()) if m else None


def _tokenize(sql: str) -> list[str]:
    return [t.strip("'\"") for t in _TOKEN_RE.findall(sql)]


def _parse_col_type(token: str) -> tuple[str, int]:
    u = token.upper()
    if u == INTEGER:  return INTEGER, 8
    if u == REAL:     return REAL,    8
    if u == TEXT:     return TEXT,    DEFAULT_TEXT_SIZE
    m = re.fullmatch(r"VARCHAR\((\d+)\)", u)
    if m:             return TEXT,    int(m.group(1))
    raise ParseError(f"Unknown column type: '{token}'")


def _extract_paren_tokens(tokens: list[str], pos: int) -> tuple[list[str], int]:
    """Given tokens[pos] == '(', extract inner tokens up to matching ')'.
    Returns (inner_tokens, pos_after_closing_paren).
    """
    if pos >= len(tokens) or tokens[pos] != "(":
        raise ParseError("Expected (")
    pos += 1
    depth = 1
    inner: list[str] = []
    while pos < len(tokens) and depth > 0:
        tok = tokens[pos]; pos += 1
        if tok == "(":
            depth += 1; inner.append(tok)
        elif tok == ")":
            depth -= 1
            if depth > 0: inner.append(tok)
        else:
            inner.append(tok)
    if depth != 0:
        raise ParseError("Unmatched ( in subquery")
    return inner, pos


def _parse_one_condition(tokens: list[str], pos: int) -> tuple["WhereClause", int]:
    """Parse a single condition starting at pos.
    Supports: col OP val, col IN (...), col NOT IN (...),
              EXISTS (SELECT ...), NOT EXISTS (SELECT ...),
              col OP (SELECT ...) scalar subquery.
    """
    if pos >= len(tokens):
        raise ParseError("Incomplete WHERE clause")

    # EXISTS (SELECT ...)
    if tokens[pos].upper() == "EXISTS":
        if pos + 1 >= len(tokens) or tokens[pos + 1] != "(":
            raise ParseError("Expected ( after EXISTS")
        inner, new_pos = _extract_paren_tokens(tokens, pos + 1)
        return WhereClause(col="", op="EXISTS", val="",
                           subquery_ast=_parse_tokens(inner)), new_pos

    # NOT EXISTS (SELECT ...) or NOT IN (...)
    if tokens[pos].upper() == "NOT":
        if pos + 1 >= len(tokens):
            raise ParseError("Expected EXISTS or IN after NOT")
        next_kw = tokens[pos + 1].upper()
        if next_kw == "EXISTS":
            if pos + 2 >= len(tokens) or tokens[pos + 2] != "(":
                raise ParseError("Expected ( after NOT EXISTS")
            inner, new_pos = _extract_paren_tokens(tokens, pos + 2)
            return WhereClause(col="", op="NOT EXISTS", val="",
                               subquery_ast=_parse_tokens(inner)), new_pos
        raise ParseError(f"Expected EXISTS after NOT, got '{tokens[pos + 1]}'")

    if pos + 1 >= len(tokens):
        raise ParseError("Incomplete WHERE clause")
    col = tokens[pos]
    op  = tokens[pos + 1].upper()

    if op == "IS":
        if pos + 2 < len(tokens) and tokens[pos + 2].upper() == "NULL":
            return WhereClause(col=col, op="IS NULL", val=""), pos + 3
        if (pos + 3 < len(tokens)
                and tokens[pos + 2].upper() == "NOT"
                and tokens[pos + 3].upper() == "NULL"):
            return WhereClause(col=col, op="IS NOT NULL", val=""), pos + 4
        raise ParseError("Expected NULL or NOT NULL after IS")

    # col NOT IN (...)
    if op == "NOT":
        if pos + 2 < len(tokens) and tokens[pos + 2].upper() == "IN":
            if pos + 3 >= len(tokens) or tokens[pos + 3] != "(":
                raise ParseError("Expected ( after NOT IN")
            inner, new_pos = _extract_paren_tokens(tokens, pos + 3)
            if inner and inner[0].upper() == "SELECT":
                return WhereClause(col=col, op="NOT IN", val="__subquery__",
                                   subquery_ast=_parse_tokens(inner)), new_pos
            return WhereClause(col=col, op="NOT IN",
                               val=",".join(v for v in inner if v != ",")), new_pos
        _got = tokens[pos + 2] if pos + 2 < len(tokens) else ""
        raise ParseError(f"Expected IN after NOT, got '{_got}'")

    if op == "IN":
        if pos + 2 >= len(tokens) or tokens[pos + 2] != "(":
            raise ParseError("Expected ( after IN")
        inner, new_pos = _extract_paren_tokens(tokens, pos + 2)
        if inner and inner[0].upper() == "SELECT":
            return WhereClause(col=col, op="IN", val="__subquery__",
                               subquery_ast=_parse_tokens(inner)), new_pos
        return WhereClause(col=col, op="IN",
                           val=",".join(v for v in inner if v != ",")), new_pos

    if pos + 2 >= len(tokens):
        raise ParseError("Incomplete WHERE clause")

    # Scalar subquery: col OP (SELECT ...)
    if tokens[pos + 2] == "(" and pos + 3 < len(tokens) and tokens[pos + 3].upper() == "SELECT":
        if op not in {"=", "!=", "<", ">", "<=", ">="}:
            raise ParseError(f"Operator '{op}' not supported with scalar subquery")
        inner, new_pos = _extract_paren_tokens(tokens, pos + 2)
        return WhereClause(col=col, op=op, val="__subquery__",
                           subquery_ast=_parse_tokens(inner)), new_pos

    val = tokens[pos + 2]
    if op not in {"=", "!=", "<", ">", "<=", ">=", "LIKE"}:
        raise ParseError(f"Unknown operator: '{op}'")
    return WhereClause(col=col, op=op, val=val), pos + 3


def _parse_and_group(tokens: list[str], pos: int) -> tuple["WhereClause", int]:
    """Parse one AND-connected group of conditions."""
    clause, pos = _parse_one_condition(tokens, pos)
    while pos < len(tokens) and tokens[pos].upper() == "AND":
        next_cond, pos = _parse_one_condition(tokens, pos + 1)
        tail = clause
        while tail.and_clause:
            tail = tail.and_clause
        tail.and_clause = next_cond
    return clause, pos


def _parse_where(tokens: list[str], pos: int) -> tuple["WhereClause | None", int]:
    """Parse WHERE (AND-group) [OR (AND-group) ...]. Returns (clause, next_pos).
    AND binds tighter than OR (standard SQL precedence).
    """
    if pos >= len(tokens) or tokens[pos].upper() != "WHERE":
        return None, pos
    clause, pos = _parse_and_group(tokens, pos + 1)
    or_tail = clause
    while pos < len(tokens) and tokens[pos].upper() == "OR":
        next_group, pos = _parse_and_group(tokens, pos + 1)
        or_tail.or_clause = next_group
        or_tail = next_group
    return clause, pos


def _parse_group_having(tokens: list[str], pos: int
                        ) -> tuple[list[str], "WhereClause | None", int]:
    """Parse optional GROUP BY col[, col] [HAVING condition] starting at pos."""
    group_by: list[str] = []
    having: "WhereClause | None" = None
    if pos < len(tokens) and tokens[pos].upper() == "GROUP":
        pos += 1
        if pos >= len(tokens) or tokens[pos].upper() != "BY":
            raise ParseError("Expected BY after GROUP")
        pos += 1
        while pos < len(tokens) and tokens[pos].upper() not in ("HAVING", "ORDER", "LIMIT"):
            if tokens[pos] != ",":
                group_by.append(tokens[pos])
            pos += 1
    if pos < len(tokens) and tokens[pos].upper() == "HAVING":
        having, pos = _parse_and_group(tokens, pos + 1)
        or_tail = having
        while pos < len(tokens) and tokens[pos].upper() == "OR":
            next_group, pos = _parse_and_group(tokens, pos + 1)
            or_tail.or_clause = next_group
            or_tail = next_group
    return group_by, having, pos


def _parse_order_limit(tokens: list[str], pos: int
                       ) -> tuple[list[dict], int | None]:
    """Parse optional ORDER BY … LIMIT n starting at pos.
    Returns (order_by_list, limit).  order_by items: {"col": str, "desc": bool}.
    """
    order_by: list[dict] = []
    limit: int | None = None

    if pos < len(tokens) and tokens[pos].upper() == "ORDER":
        pos += 1
        if pos >= len(tokens) or tokens[pos].upper() != "BY":
            raise ParseError("Expected BY after ORDER")
        pos += 1
        while pos < len(tokens) and tokens[pos].upper() not in ("LIMIT",):
            col = tokens[pos]; pos += 1
            desc = False
            if pos < len(tokens) and tokens[pos].upper() in ("ASC", "DESC"):
                desc = tokens[pos].upper() == "DESC"
                pos += 1
            order_by.append({"col": col, "desc": desc})
            if pos < len(tokens) and tokens[pos] == ",":
                pos += 1

    if pos < len(tokens) and tokens[pos].upper() == "LIMIT":
        pos += 1
        if pos >= len(tokens):
            raise ParseError("Expected integer after LIMIT")
        try:
            limit = int(tokens[pos]); pos += 1
        except ValueError:
            raise ParseError(f"Expected integer after LIMIT, got '{tokens[pos]}'")

    return order_by, limit


def parse(sql: str) -> dict:
    return _parse_tokens(_tokenize(sql))


def _parse_tokens(t: list[str]) -> dict:
    if not t:
        raise ParseError("Empty statement")

    # Top-level UNION / INTERSECT / EXCEPT (skip tokens inside parentheses)
    depth = 0
    for idx, tok in enumerate(t):
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
        elif depth == 0 and tok.upper() in ("UNION", "INTERSECT", "EXCEPT"):
            set_op = tok.upper()
            all_flag = idx + 1 < len(t) and t[idx + 1].upper() == "ALL"
            right_start = idx + 2 if all_flag else idx + 1
            return {
                "op":     "SET_OP",
                "set_op": set_op,
                "all":    all_flag,
                "left":   _parse_tokens(t[:idx]),
                "right":  _parse_tokens(t[right_start:]),
            }

    kw = t[0].upper()

    if kw in ("BEGIN", "COMMIT", "ROLLBACK"):
        return {"op": kw}

    # CREATE TABLE / INDEX
    if kw == "CREATE":
        if len(t) < 2:
            raise ParseError("Expected TABLE or INDEX after CREATE")
        sub = t[1].upper()
        if sub == "TABLE":
            if len(t) < 4 or t[3] != "(":
                raise ParseError("Expected: CREATE TABLE <name> (...)")
            name = t[2]
            def _parse_col_list_ct(pos: int) -> tuple[list[str], int]:
                if pos >= len(t) or t[pos] != "(":
                    raise ParseError("Expected ( for column list")
                pos += 1
                cols_ct: list[str] = []
                while pos < len(t) and t[pos] != ")":
                    if t[pos] != ",":
                        cols_ct.append(t[pos])
                    pos += 1
                if pos >= len(t):
                    raise ParseError("Unmatched ( in column list")
                return cols_ct, pos + 1

            columns, fk_constraints, i = [], [], 4
            while i < len(t) and t[i] != ")":
                # Table-level: FOREIGN KEY (cols) REFERENCES ref_table (ref_cols)
                if t[i].upper() == "FOREIGN":
                    if i + 1 >= len(t) or t[i + 1].upper() != "KEY":
                        raise ParseError("Expected KEY after FOREIGN")
                    i += 2
                    fk_cols, i = _parse_col_list_ct(i)
                    if i >= len(t) or t[i].upper() != "REFERENCES":
                        raise ParseError("Expected REFERENCES after FOREIGN KEY (...)")
                    i += 1
                    if i >= len(t):
                        raise ParseError("Expected table name after REFERENCES")
                    ref_table_fk = t[i]; i += 1
                    ref_cols_fk, i = _parse_col_list_ct(i)
                    fk_constraints.append(ForeignKey(fk_cols, ref_table_fk, ref_cols_fk))
                    if i < len(t) and t[i] == ",":
                        i += 1
                    continue

                col_name = t[i]
                col_type, col_size = _parse_col_type(t[i + 1])
                i += 2
                nullable = True
                unique   = False
                default  = None
                check    = None
                while i < len(t) and t[i].upper() in (
                        "NOT", "UNIQUE", "DEFAULT", "CHECK", "REFERENCES"):
                    kw = t[i].upper()
                    if kw == "NOT":
                        if i + 1 < len(t) and t[i + 1].upper() == "NULL":
                            nullable = False
                            i += 2
                        else:
                            raise ParseError("Expected NULL after NOT")
                    elif kw == "UNIQUE":
                        unique = True
                        i += 1
                    elif kw == "DEFAULT":
                        if i + 1 >= len(t):
                            raise ParseError("Expected value after DEFAULT")
                        default = t[i + 1]
                        i += 2
                    elif kw == "CHECK":
                        if i + 1 >= len(t) or t[i + 1] != "(":
                            raise ParseError("Expected ( after CHECK")
                        i += 2  # skip CHECK and (
                        check_tokens: list[str] = []
                        depth = 1
                        while i < len(t) and depth > 0:
                            tok = t[i]; i += 1
                            if tok == "(":
                                depth += 1
                                check_tokens.append(tok)
                            elif tok == ")":
                                depth -= 1
                                if depth > 0:
                                    check_tokens.append(tok)
                            else:
                                check_tokens.append(tok)
                        if depth != 0:
                            raise ParseError("Unmatched ( in CHECK constraint")
                        check = " ".join(check_tokens)
                    elif kw == "REFERENCES":
                        i += 1
                        if i >= len(t):
                            raise ParseError("Expected table name after REFERENCES")
                        ref_table_fk = t[i]; i += 1
                        if i < len(t) and t[i] == "(":
                            ref_cols_fk, i = _parse_col_list_ct(i)
                        else:
                            ref_cols_fk = [col_name]
                        fk_constraints.append(
                            ForeignKey([col_name], ref_table_fk, ref_cols_fk))
                columns.append(Column(col_name, col_type, col_size, nullable, unique,
                                      default, check))
                if i < len(t) and t[i] == ",":
                    i += 1
            return {"op": "CREATE_TABLE", "name": name, "columns": columns,
                    "foreign_keys": fk_constraints}
        if sub == "INDEX":
            # CREATE INDEX idx ON table(col1[, col2, ...])
            if len(t) < 5:
                raise ParseError("Expected: CREATE INDEX <name> ON <table>(<cols>)")
            idx_name = t[2]
            if t[3].upper() != "ON":
                raise ParseError("Expected ON")
            # Tokenizer may produce "table(col1,col2)" as one token or separate tokens
            m = re.fullmatch(r"(\w+)\(([^)]+)\)", t[4])
            if m:
                table = m.group(1)
                cols  = [c.strip() for c in m.group(2).split(",")]
            else:
                table = t[4]
                if len(t) < 7 or t[5] != "(":
                    raise ParseError("Expected (<col>) after table name")
                i, cols = 6, []
                while i < len(t) and t[i] != ")":
                    if t[i] != ",":
                        cols.append(t[i])
                    i += 1
            if not cols:
                raise ParseError("Expected at least one column in index")
            return {"op": "CREATE_INDEX", "idx_name": idx_name,
                    "table": table, "cols": cols}
        raise ParseError(f"Expected TABLE or INDEX, got '{t[1]}'")

    # ALTER TABLE
    if kw == "ALTER":
        if len(t) < 4 or t[1].upper() != "TABLE":
            raise ParseError("Expected: ALTER TABLE <name> ...")
        table = t[2]
        sub   = t[3].upper()
        if sub == "RENAME":
            if len(t) < 5:
                raise ParseError("Expected: RENAME TO <new> or RENAME COLUMN <old> TO <new>")
            if t[4].upper() == "TO":
                if len(t) < 6:
                    raise ParseError("Expected new table name after TO")
                return {"op": "ALTER_RENAME_TABLE", "table": table, "new_name": t[5]}
            if t[4].upper() == "COLUMN":
                if len(t) < 8 or t[6].upper() != "TO":
                    raise ParseError("Expected: RENAME COLUMN <old> TO <new>")
                return {"op": "ALTER_RENAME_COLUMN", "table": table,
                        "old_name": t[5], "new_name": t[7]}
            raise ParseError(f"Expected TO or COLUMN after RENAME, got '{t[4]}'")
        if sub == "ADD":
            if len(t) < 6 or t[4].upper() != "COLUMN":
                raise ParseError("Expected: ADD COLUMN <name> <type>")
            col_name = t[5]
            col_type, col_size = _parse_col_type(t[6])
            i = 7
            nullable = True
            if i < len(t) and t[i].upper() == "NOT":
                if i + 1 < len(t) and t[i + 1].upper() == "NULL":
                    nullable = False
                else:
                    raise ParseError("Expected NULL after NOT")
            return {"op": "ALTER_ADD_COLUMN", "table": table,
                    "col": Column(col_name, col_type, col_size, nullable)}
        if sub == "DROP":
            if len(t) < 6 or t[4].upper() != "COLUMN":
                raise ParseError("Expected: DROP COLUMN <name>")
            return {"op": "ALTER_DROP_COLUMN", "table": table, "col_name": t[5]}
        raise ParseError(f"Unknown ALTER TABLE operation: '{t[3]}'")

    # DROP TABLE / INDEX
    if kw == "DROP":
        if len(t) < 3:
            raise ParseError("Expected TABLE or INDEX after DROP")
        sub = t[1].upper()
        if sub == "TABLE":
            return {"op": "DROP_TABLE", "name": t[2]}
        if sub == "INDEX":
            return {"op": "DROP_INDEX", "idx_name": t[2]}
        raise ParseError(f"Expected TABLE or INDEX, got '{t[1]}'")

    # INSERT INTO
    if kw == "INSERT":
        if len(t) < 3 or t[1].upper() != "INTO":
            raise ParseError("Expected: INSERT INTO <table> ...")
        table, i = t[2], 3
        col_names: list[str] | None = None
        if i < len(t) and t[i] == "(":
            i += 1
            col_names = []
            while i < len(t) and t[i] != ")":
                if t[i] != ",":
                    col_names.append(t[i])
                i += 1
            i += 1
        if i >= len(t) or t[i].upper() != "VALUES":
            raise ParseError("Expected VALUES")
        i += 2
        values: list[str] = []
        while i < len(t) and t[i] != ")":
            if t[i] != ",":
                values.append(t[i])
            i += 1
        return {"op": "INSERT", "table": table, "col_names": col_names, "values": values}

    # SELECT (with optional INNER JOIN)
    if kw == "SELECT":
        i = 1
        distinct = i < len(t) and t[i].upper() == "DISTINCT"
        if distinct:
            i += 1
        cols = []
        while i < len(t) and t[i].upper() != "FROM":
            if t[i] != ",":
                cols.append(t[i])
            i += 1
        if i >= len(t):
            raise ParseError("Expected FROM")
        i += 1
        table = t[i]; i += 1
        left_alias, i = _parse_table_alias(t, i, table)
        # Check for [INNER | LEFT | RIGHT | FULL [OUTER] | CROSS | NATURAL] JOIN
        join_type: str | None = None
        if i < len(t):
            kw2 = t[i].upper()
            if kw2 in ("LEFT", "RIGHT", "FULL"):
                join_type = kw2; i += 1
                if i < len(t) and t[i].upper() == "OUTER":
                    i += 1
                if i >= len(t) or t[i].upper() != "JOIN":
                    raise ParseError(f"Expected JOIN after {join_type} [OUTER]")
                i += 1
            elif kw2 == "INNER":
                if i + 1 >= len(t) or t[i + 1].upper() != "JOIN":
                    raise ParseError("Expected JOIN after INNER")
                join_type = "INNER"; i += 2
            elif kw2 == "CROSS":
                if i + 1 >= len(t) or t[i + 1].upper() != "JOIN":
                    raise ParseError("Expected JOIN after CROSS")
                join_type = "CROSS"; i += 2
            elif kw2 == "NATURAL":
                if i + 1 >= len(t) or t[i + 1].upper() != "JOIN":
                    raise ParseError("Expected JOIN after NATURAL")
                join_type = "NATURAL"; i += 2
            elif kw2 == "JOIN":
                join_type = "INNER"; i += 1
        if join_type is not None:
            right_table = t[i]; i += 1
            right_alias, i = _parse_table_alias(t, i, right_table)
            on_left = on_right = None
            if join_type not in ("CROSS", "NATURAL"):
                if i >= len(t) or t[i].upper() != "ON":
                    raise ParseError(f"Expected ON after {right_table} for {join_type} JOIN")
                i += 1
                on_left = t[i]; i += 1
                if i >= len(t) or t[i] != "=":
                    raise ParseError("Expected = in ON clause")
                i += 1
                on_right = t[i]; i += 1
            where, i        = _parse_where(t, i)
            order_by, limit = _parse_order_limit(t, i)
            return {
                "op":           "JOIN",
                "join_type":    join_type,
                "left_table":   table,
                "left_alias":   left_alias,
                "right_table":  right_table,
                "right_alias":  right_alias,
                "on_left":      on_left,
                "on_right":     on_right,
                "columns":      None if cols == ["*"] else cols,
                "where":        where,
                "order_by":     order_by,
                "limit":        limit,
            }
        where, i              = _parse_where(t, i)
        group_by, having, i   = _parse_group_having(t, i)
        order_by, limit       = _parse_order_limit(t, i)
        return {
            "op":       "SELECT",
            "table":    table,
            "columns":  None if cols == ["*"] else cols,
            "where":    where,
            "group_by": group_by or None,
            "having":   having,
            "order_by": order_by,
            "limit":    limit,
            "distinct": distinct,
        }

    # UPDATE
    if kw == "UPDATE":
        if len(t) < 4 or t[2].upper() != "SET":
            raise ParseError("Expected: UPDATE <table> SET col=val ...")
        table = t[1]; i = 3
        assignments: dict[str, str] = {}
        while i < len(t) and t[i].upper() != "WHERE":
            if t[i] == ",":
                i += 1
                continue
            token = t[i]
            if "=" in token:                        # col=val (no spaces)
                col, val = token.split("=", 1)
                assignments[col] = val
                i += 1
            elif i + 2 < len(t) and t[i + 1] == "=":  # col = val
                assignments[t[i]] = t[i + 2]
                i += 3
            else:
                raise ParseError(f"Expected col=val near '{token}'")
        where, _ = _parse_where(t, i)
        return {"op": "UPDATE", "table": table, "assignments": assignments,
                "where": where}

    # DELETE
    if kw == "DELETE":
        if len(t) < 3 or t[1].upper() != "FROM":
            raise ParseError("Expected: DELETE FROM <table> [WHERE ...]")
        where, _ = _parse_where(t, 3)
        return {"op": "DELETE", "table": t[2], "where": where}

    raise ParseError(f"Unrecognized statement: '{t[0]}'")


# ══════════════════════════════════════════════════════════════════════════════
# Set-operation helpers
# ══════════════════════════════════════════════════════════════════════════════

_OUTER_REF_RE = re.compile(r'^[A-Za-z_]\w*(\.[A-Za-z_]\w*)?$')


def _try_resolve_outer_ref(val: str, outer_row: dict) -> tuple[bool, Any]:
    """If val is a dot-qualified identifier or an exact key in outer_row, return its value.
    Returns (found, value).  Only resolves qualified names (e.g. 'e.id', 'emp.id')
    or bare names that are an exact match in outer_row — never rewrites plain literals.
    """
    if not _OUTER_REF_RE.match(val):
        return False, None
    if val in outer_row:
        return True, outer_row[val]
    if "." in val:
        col = val.split(".", 1)[1]
        if col in outer_row:
            return True, outer_row[col]
    return False, None


def _instantiate_correlated(where: "WhereClause | None",
                             outer_row: dict) -> "WhereClause | None":
    """Return a copy of the WhereClause tree with outer column references substituted."""
    if where is None:
        return None
    new_val = where.val
    if where.val and where.subquery_ast is None:
        found, resolved = _try_resolve_outer_ref(where.val, outer_row)
        if found:
            new_val = str(resolved) if resolved is not None else "NULL"
    return WhereClause(
        col=where.col, op=where.op, val=new_val,
        subquery_ast=where.subquery_ast,
        and_clause=_instantiate_correlated(where.and_clause, outer_row),
        or_clause=_instantiate_correlated(where.or_clause, outer_row),
    )


def _exec_correlated_subquery(stmt: "dict | None", db: Any,
                               outer_row: dict) -> list[dict]:
    """Execute a subquery AST with outer_row as the correlation context.
    Substitutes any outer column references in the WHERE before running,
    so correlated subqueries (WHERE inner.col = outer.col) work correctly.
    """
    if stmt is None or db is None:
        return []
    op = stmt["op"]
    inst_where = _instantiate_correlated(stmt.get("where"), outer_row)
    if op == "SELECT":
        return db.select(stmt["table"], stmt["columns"], inst_where,
                         stmt.get("order_by"), stmt.get("limit"),
                         stmt.get("group_by"), stmt.get("having"),
                         stmt.get("distinct", False))
    if op == "JOIN":
        return db.join(stmt["left_table"], stmt["right_table"],
                       stmt["on_left"], stmt["on_right"],
                       stmt["columns"], inst_where,
                       stmt.get("order_by"), stmt.get("limit"),
                       stmt.get("join_type", "INNER"),
                       stmt.get("left_alias"), stmt.get("right_alias"))
    if op == "SET_OP":
        left  = _exec_correlated_subquery(stmt["left"],  db, outer_row)
        right = _exec_correlated_subquery(stmt["right"], db, outer_row)
        return _apply_set_op(stmt["set_op"], stmt.get("all", False), left, right)
    raise RuntimeError(f"Expected SELECT/JOIN/SET_OP in subquery, got '{op}'")


def _rows_for_stmt(stmt: dict, db: "Database") -> list[dict]:
    """Execute any SELECT-like statement and return its rows."""
    op = stmt["op"]
    if op == "SELECT":
        return db.select(stmt["table"], stmt["columns"], stmt["where"],
                         stmt.get("order_by"), stmt.get("limit"),
                         stmt.get("group_by"), stmt.get("having"),
                         stmt.get("distinct", False))
    if op == "JOIN":
        return db.join(stmt["left_table"], stmt["right_table"],
                       stmt["on_left"], stmt["on_right"],
                       stmt["columns"], stmt["where"],
                       stmt.get("order_by"), stmt.get("limit"),
                       stmt.get("join_type", "INNER"),
                       stmt.get("left_alias"), stmt.get("right_alias"))
    if op == "SET_OP":
        left  = _rows_for_stmt(stmt["left"],  db)
        right = _rows_for_stmt(stmt["right"], db)
        return _apply_set_op(stmt["set_op"], stmt.get("all", False), left, right)
    raise RuntimeError(f"Expected SELECT/JOIN/SET_OP, got '{op}'")


def _apply_set_op(op: str, all_flag: bool,
                  left: list[dict], right: list[dict]) -> list[dict]:
    """Combine two row-lists with UNION / INTERSECT / EXCEPT semantics."""
    def _key(row: dict) -> tuple:
        return tuple(row.values())

    if op == "UNION":
        if all_flag:
            return left + right
        seen: set[tuple] = set()
        out:  list[dict] = []
        for row in left + right:
            k = _key(row)
            if k not in seen:
                seen.add(k); out.append(row)
        return out

    if op == "INTERSECT":
        if all_flag:
            # Multiset: include min(left_count, right_count) copies
            counts: dict[tuple, int] = {}
            for r in right:
                k = _key(r); counts[k] = counts.get(k, 0) + 1
            used:  dict[tuple, int] = {}
            out = []
            for r in left:
                k = _key(r)
                used[k] = used.get(k, 0) + 1
                if used[k] <= counts.get(k, 0):
                    out.append(r)
            return out
        right_keys = {_key(r) for r in right}
        seen = set(); out = []
        for r in left:
            k = _key(r)
            if k in right_keys and k not in seen:
                seen.add(k); out.append(r)
        return out

    if op == "EXCEPT":
        if all_flag:
            # Multiset: include max(left_count - right_count, 0) copies
            counts = {}
            for r in right:
                k = _key(r); counts[k] = counts.get(k, 0) + 1
            out = []
            for r in left:
                k = _key(r)
                if counts.get(k, 0) > 0:
                    counts[k] -= 1
                else:
                    out.append(r)
            return out
        right_keys = {_key(r) for r in right}
        seen = set(); out = []
        for r in left:
            k = _key(r)
            if k not in right_keys and k not in seen:
                seen.add(k); out.append(r)
        return out

    raise RuntimeError(f"Unknown set operation: '{op}'")


# ══════════════════════════════════════════════════════════════════════════════
# Executor
# ══════════════════════════════════════════════════════════════════════════════

def execute(stmt: dict, db: Database) -> str:
    op = stmt["op"]

    # Transaction control — never auto-wrapped
    if op == "BEGIN":
        db.begin()
        return "Transaction started."
    if op == "COMMIT":
        db.commit()
        return "Transaction committed."
    if op == "ROLLBACK":
        db.rollback()
        return "Transaction rolled back."

    # All other statements: auto-commit if not inside an explicit BEGIN
    auto = not db.in_transaction
    if auto:
        db.begin()
    try:
        result = _execute_inner(stmt, db)
    except Exception:
        if auto:
            db.rollback()
        raise
    if auto:
        db.commit()
    return result


def _execute_inner(stmt: dict, db: Database) -> str:
    op = stmt["op"]

    if op == "CREATE_TABLE":
        db.create_table(Schema(name=stmt["name"], columns=stmt["columns"],
                               foreign_keys=stmt.get("foreign_keys", [])))
        return f"Table '{stmt['name']}' created."

    if op == "DROP_TABLE":
        db.drop_table(stmt["name"])
        return f"Table '{stmt['name']}' dropped."

    if op == "ALTER_ADD_COLUMN":
        db.alter_add_column(stmt["table"], stmt["col"])
        return f"Column '{stmt['col'].name}' added to '{stmt['table']}'."

    if op == "ALTER_DROP_COLUMN":
        db.alter_drop_column(stmt["table"], stmt["col_name"])
        return f"Column '{stmt['col_name']}' dropped from '{stmt['table']}'."

    if op == "ALTER_RENAME_COLUMN":
        db.alter_rename_column(stmt["table"], stmt["old_name"], stmt["new_name"])
        return f"Column '{stmt['old_name']}' renamed to '{stmt['new_name']}'."

    if op == "ALTER_RENAME_TABLE":
        db.alter_rename_table(stmt["table"], stmt["new_name"])
        return f"Table '{stmt['table']}' renamed to '{stmt['new_name']}'."

    if op == "CREATE_INDEX":
        db.create_index(stmt["idx_name"], stmt["table"], stmt["cols"])
        cols_str = ", ".join(stmt["cols"])
        return f"Index '{stmt['idx_name']}' created on {stmt['table']}({cols_str})."

    if op == "DROP_INDEX":
        db.drop_index(stmt["idx_name"])
        return f"Index '{stmt['idx_name']}' dropped."

    if op == "INSERT":
        meta      = db._meta(stmt["table"])
        col_names = stmt["col_names"] or [c.name for c in meta.schema.columns]
        values    = stmt["values"]
        if len(col_names) != len(values):
            raise RuntimeError(
                f"Column/value mismatch: {len(col_names)} columns, {len(values)} values"
            )
        parsed: dict[str, Any] = {}
        for name, val in zip(col_names, values):
            parsed[name] = None if val.upper() == "NULL" else val
        # Fill any omitted columns from DEFAULT, or NULL
        for col in meta.schema.columns:
            if col.name not in parsed:
                parsed[col.name] = col.default  # None if no DEFAULT
        db.insert(stmt["table"], parsed)
        return "1 row inserted."

    if op == "SELECT":
        rows = db.select(stmt["table"], stmt["columns"], stmt["where"],
                         stmt.get("order_by"), stmt.get("limit"),
                         stmt.get("group_by"), stmt.get("having"),
                         stmt.get("distinct", False))
        return _format_rows(rows, stmt["columns"])

    if op == "JOIN":
        rows = db.join(stmt["left_table"], stmt["right_table"],
                       stmt["on_left"], stmt["on_right"],
                       stmt["columns"], stmt["where"],
                       stmt.get("order_by"), stmt.get("limit"),
                       stmt.get("join_type", "INNER"),
                       stmt.get("left_alias"), stmt.get("right_alias"))
        return _format_rows(rows, stmt["columns"])

    if op == "SET_OP":
        rows = _rows_for_stmt(stmt, db)
        cols = stmt["left"].get("columns")
        return _format_rows(rows, cols)

    if op == "UPDATE":
        n = db.update(stmt["table"], stmt["assignments"], stmt["where"])
        return f"{n} row{'s' if n != 1 else ''} updated."

    if op == "DELETE":
        n = db.delete(stmt["table"], stmt["where"])
        return f"{n} row{'s' if n != 1 else ''} deleted."

    raise RuntimeError(f"Unknown op: {op}")


def _cell_str(v: Any) -> str:
    return "NULL" if v is None else str(v)


def _format_rows(rows: list[dict], requested_cols: list[str] | None) -> str:
    if not rows:
        return "(no rows)"
    cols   = requested_cols if requested_cols else list(rows[0].keys())
    widths = {c: max(len(c), max(len(_cell_str(r.get(c))) for r in rows))
              for c in cols}
    header = " | ".join(c.ljust(widths[c]) for c in cols)
    sep    = "-+-".join("-" * widths[c] for c in cols)
    body   = "\n".join(
        " | ".join(_cell_str(r.get(c)).ljust(widths[c]) for c in cols)
        for r in rows
    )
    n = len(rows)
    return f"{header}\n{sep}\n{body}\n({n} row{'s' if n != 1 else ''})"


# ══════════════════════════════════════════════════════════════════════════════
# Meta-commands
# ══════════════════════════════════════════════════════════════════════════════

def handle_meta(cmd: str, db: Database) -> bool | None:
    parts = cmd.strip().split()
    kw    = parts[0].lower()

    if kw == ".exit":
        return None

    if kw == ".tables":
        names = sorted(db.tables)
        print("\n".join(names) if names else "(no tables)")
        return True

    if kw == ".indexes":
        for n, m in sorted(db.indexes.items()):
            print(f"{n} ON {m.table_name}({', '.join(m.columns)})")
        if not db.indexes:
            print("(no indexes)")
        return True

    if kw == ".schema":
        if len(parts) < 2:
            print("Usage: .schema <table>")
            return True
        name = parts[1]
        if name not in db.tables:
            print(f"Error: no table '{name}'")
            return True
        schema  = db.tables[name].schema
        parts: list[str] = []
        for c in schema.columns:
            cdef = (f"{c.name} {c.type}" + (f"({c.size})" if c.type == TEXT else "")
                    + ("" if c.nullable else " NOT NULL")
                    + (" UNIQUE" if c.unique else "")
                    + (f" DEFAULT {c.default}" if c.default is not None else "")
                    + (f" CHECK ({c.check})" if c.check is not None else ""))
            parts.append(cdef)
        for fk in schema.foreign_keys:
            parts.append(
                f"FOREIGN KEY ({', '.join(fk.columns)}) "
                f"REFERENCES {fk.ref_table} ({', '.join(fk.ref_columns)})"
            )
        print(f"CREATE TABLE {name} ({', '.join(parts)})")
        return True

    print(f"Unrecognized command: '{cmd}'")
    return True


# ══════════════════════════════════════════════════════════════════════════════
# REPL
# ══════════════════════════════════════════════════════════════════════════════

def repl(db: Database) -> None:
    while True:
        try:
            text = input("H > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text.startswith("."):
            if handle_meta(text, db) is None:
                break
            continue
        try:
            print(execute(parse(text), db))
        except (ParseError, RuntimeError, KeyError, struct.error) as e:
            print(f"Error: {e}")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python hyperion.py <database_file>")
        sys.exit(1)
    db = Database(Path(sys.argv[1]))
    try:
        repl(db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
