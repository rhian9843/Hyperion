"""Column-oriented storage for Hyperion.

Layout
------
Root page  (PAGE_COLUMN_HDR = 0x10)
    [0]      page type = 0x10
    [1-4]    row_count (uint32 LE)
    [5-6]    num_cols  (uint16 LE)  — includes _rowid pseudo-column at index 0
    [7-8]    reserved
    [9 + i*44 … +43]  directory entry for column i:
        [+0 ..+3]  first_chunk  (uint32 LE)
        [+4 ..+7]  last_chunk   (uint32 LE)
        [+8 ..+11] last_pos     (uint32 LE) — byte offset where next entry goes
        [+12]      name_len     (uint8)
        [+13..+43] name         (31 bytes, zero-padded)
    [4092-4095]  CRC-32

Chunk page (PAGE_COLUMN_CHUNK = 0x11)
    [0]      page type = 0x11
    [1-4]    next_chunk (uint32 LE), 0 = end of chain
    [5-8]    entry_count (uint32 LE)
    [9-4091] entry data
    [4092-4095]  CRC-32

Entry encoding
    NULL           : \x00  (1 byte)
    INTEGER non-null: \x01 + int64  (9 bytes)
    REAL    non-null: \x01 + float64 (9 bytes)
    TEXT/BLOB inline: \x01 + uint32_len + data  (5 + N bytes, N <= 4078)
    TEXT/BLOB overflow: \x02 + uint32_first_page + uint32_total_len  (9 bytes)

Overflow pages use the identical format to database.py overflow pages
(PAGE_OVERFLOW = 0x02).
"""
from __future__ import annotations

import re
import struct
from typing import TYPE_CHECKING, Any, Callable, Iterator

from .checksum import stamp_page
from .constants import (
    PAGE_SIZE, PAGE_CKSUM_OFF,
    INTEGER, REAL, TEXT, BLOB,
    PAGE_OVERFLOW, OVERFLOW_HDR, OVERFLOW_DATA_SZ,
)
from .schema import Schema, serialize_row, deserialize_row

if TYPE_CHECKING:
    from .pager import Pager

# ── Page-type constants ────────────────────────────────────────────────────────

PAGE_COLUMN_HDR   = 0x10
PAGE_COLUMN_CHUNK = 0x11

# ── Layout constants ───────────────────────────────────────────────────────────

HDR_PREFIX_SZ  = 9          # flag(1) + row_count(4) + num_cols(2) + reserved(2)
HDR_ENTRY_SZ   = 44         # first_chunk(4) + last_chunk(4) + last_pos(4) + name_len(1) + name(31)
CHUNK_HDR_SZ   = 9          # flag(1) + next_chunk(4) + entry_count(4)
CHUNK_DATA_END = PAGE_CKSUM_OFF   # = 4092

# Maximum bytes of TEXT/BLOB data that can be stored inline in a fresh chunk page.
# Any value longer than this MUST use overflow pages.
MAX_INLINE_STR = CHUNK_DATA_END - CHUNK_HDR_SZ - 5   # = 4078

# Maximum number of columns a column store table can have (_rowid included).
HDR_MAX_COLS = (CHUNK_DATA_END - HDR_PREFIX_SZ) // HDR_ENTRY_SZ  # = 92

# ── Regex used by scan_aggregate ──────────────────────────────────────────────

_AGG_PAT = re.compile(
    r'^(COUNT|MIN|MAX|SUM|AVG)\s*\(\s*(DISTINCT\s+)?(.+?)\s*\)$',
    re.IGNORECASE,
)


# ── Module-level helpers (no self) ────────────────────────────────────────────

def _encode_entry(value: Any, col_type: str, pager: "Pager",
                  alloc_fn: Callable[[], int]) -> bytes:
    if value is None:
        return b'\x00'
    if col_type == INTEGER:
        return b'\x01' + struct.pack('<q', int(value))
    if col_type == REAL:
        return b'\x01' + struct.pack('<d', float(value))
    # TEXT or BLOB
    if isinstance(value, (bytes, bytearray)):
        data = bytes(value)
    else:
        data = str(value).encode('utf-8')
    if len(data) <= MAX_INLINE_STR:
        return b'\x01' + struct.pack('<I', len(data)) + data
    # Overflow
    first_page = _write_col_overflow(pager, alloc_fn, data)
    return b'\x02' + struct.pack('<I', first_page) + struct.pack('<I', len(data))


def _decode_entry(buf: bytes | bytearray, pos: int,
                  col_type: str, pager: "Pager") -> tuple[Any, int]:
    flag = buf[pos]; pos += 1
    if flag == 0x00:
        return None, pos
    if col_type == INTEGER:
        v = struct.unpack_from('<q', buf, pos)[0]
        return v, pos + 8
    if col_type == REAL:
        v = struct.unpack_from('<d', buf, pos)[0]
        return v, pos + 8
    # TEXT or BLOB
    if flag == 0x01:
        length = struct.unpack_from('<I', buf, pos)[0]; pos += 4
        raw = bytes(buf[pos:pos + length])
        val = raw.decode('utf-8', errors='replace') if col_type == TEXT else raw
        return val, pos + length
    if flag == 0x02:
        first_page = struct.unpack_from('<I', buf, pos)[0]; pos += 4
        total_len  = struct.unpack_from('<I', buf, pos)[0]; pos += 4
        raw = _read_col_overflow(pager, first_page, total_len)
        val = raw.decode('utf-8', errors='replace') if col_type == TEXT else raw
        return val, pos
    raise ValueError(f"Unknown entry flag {flag:#x} at offset {pos - 1}")


def _entry_size_from_buf(buf: bytes | bytearray, pos: int, col_type: str) -> int:
    flag = buf[pos]
    if flag == 0x00:
        return 1
    if col_type in (INTEGER, REAL):
        return 9
    if flag == 0x01:
        length = struct.unpack_from('<I', buf, pos + 1)[0]
        return 5 + length
    if flag == 0x02:
        return 9
    raise ValueError(f"Unknown entry flag {flag:#x}")


def _write_col_overflow(pager: "Pager", alloc_fn: Callable[[], int],
                        data: bytes) -> int:
    first_page = 0
    prev_pn: int | None = None
    offset = 0
    while offset < len(data) or first_page == 0:
        pn = alloc_fn()
        if first_page == 0:
            first_page = pn
        if prev_pn is not None:
            pg = pager.get_page(prev_pn)
            struct.pack_into('<I', pg, 1, pn)
            stamp_page(pg)
        chunk = data[offset:offset + OVERFLOW_DATA_SZ]
        pg = pager.get_page(pn)
        pg[0] = PAGE_OVERFLOW
        struct.pack_into('<I', pg, 1, 0)
        struct.pack_into('<I', pg, 5, len(chunk))
        pg[OVERFLOW_HDR:OVERFLOW_HDR + len(chunk)] = chunk
        stamp_page(pg)
        prev_pn = pn
        offset += OVERFLOW_DATA_SZ
        if offset >= len(data):
            break
    return first_page


def _read_col_overflow(pager: "Pager", first_page: int, total_len: int) -> bytes:
    result = bytearray()
    pn = first_page
    while pn and len(result) < total_len:
        pg = pager.read_page(pn)
        data_len = struct.unpack_from('<I', pg, 5)[0]
        result += pg[OVERFLOW_HDR:OVERFLOW_HDR + data_len]
        pn = struct.unpack_from('<I', pg, 1)[0]
    return bytes(result[:total_len])


def _free_col_overflow(pager: "Pager", free_fn: Callable[[int], None],
                       first_page: int) -> None:
    pn = first_page
    while pn:
        pg  = pager.read_page(pn)
        nxt = struct.unpack_from('<I', pg, 1)[0]
        free_fn(pn)
        pn = nxt


# ── ColumnStore ───────────────────────────────────────────────────────────────

class ColumnStore:
    """Column-oriented storage for a single Hyperion table.

    Column index 0 is always the _rowid pseudo-column (INTEGER).
    Column indices 1..N correspond to schema.stored_columns.
    """

    def __init__(self, pager: "Pager", root: int, schema: Schema,
                 alloc_fn: Callable[[], int],
                 free_fn: Callable[[int], None]) -> None:
        self._pager    = pager
        self._root     = root
        self._schema   = schema
        self._alloc    = alloc_fn
        self._free_page = free_fn
        sc = schema.stored_columns
        self._col_names: list[str] = [c.name for c in sc]
        self._col_types: dict[str, str] = {c.name: c.type for c in sc}

    # ── Construction ──────────────────────────────────────────────────────────

    @classmethod
    def init_root(cls, pager: "Pager", root_page: int, schema: Schema,
                  alloc_fn: Callable[[], int]) -> None:
        """Write empty column store header to root_page; allocate first chunk per column."""
        sc = schema.stored_columns
        all_names = ["_rowid"] + [c.name for c in sc]
        all_types = [INTEGER]  + [c.type  for c in sc]
        num_cols  = len(all_names)

        if num_cols > HDR_MAX_COLS:
            raise ValueError(
                f"Column store supports at most {HDR_MAX_COLS - 1} data columns, "
                f"got {num_cols - 1}")

        hdr_pg = pager.get_page(root_page)
        hdr_pg[:PAGE_SIZE] = bytearray(PAGE_SIZE)
        hdr_pg[0] = PAGE_COLUMN_HDR
        struct.pack_into('<I', hdr_pg, 1, 0)           # row_count = 0
        struct.pack_into('<H', hdr_pg, 5, num_cols)    # num_cols

        for i, (cname, _) in enumerate(zip(all_names, all_types)):
            chunk_pn = alloc_fn()
            chunk_pg = pager.get_page(chunk_pn)
            chunk_pg[:PAGE_SIZE] = bytearray(PAGE_SIZE)
            chunk_pg[0] = PAGE_COLUMN_CHUNK
            struct.pack_into('<I', chunk_pg, 1, 0)
            struct.pack_into('<I', chunk_pg, 5, 0)
            stamp_page(chunk_pg)

            off = HDR_PREFIX_SZ + i * HDR_ENTRY_SZ
            struct.pack_into('<I', hdr_pg, off,     chunk_pn)   # first_chunk
            struct.pack_into('<I', hdr_pg, off + 4, chunk_pn)   # last_chunk
            struct.pack_into('<I', hdr_pg, off + 8, CHUNK_HDR_SZ)  # last_pos
            nb = cname.encode('utf-8')[:31]
            hdr_pg[off + 12] = len(nb)
            hdr_pg[off + 13:off + 13 + len(nb)] = nb

        stamp_page(hdr_pg)

    # ── Append helpers ────────────────────────────────────────────────────────

    def _append_entry(self, col_idx: int, value: Any, col_type: str,
                      hdr_pg: bytearray) -> None:
        entry = _encode_entry(value, col_type, self._pager, self._alloc)
        off = HDR_PREFIX_SZ + col_idx * HDR_ENTRY_SZ

        last_chunk = struct.unpack_from('<I', hdr_pg, off + 4)[0]
        last_pos   = struct.unpack_from('<I', hdr_pg, off + 8)[0]

        if last_pos + len(entry) > CHUNK_DATA_END:
            new_chunk = self._alloc()
            nc_pg = self._pager.get_page(new_chunk)
            nc_pg[:PAGE_SIZE] = bytearray(PAGE_SIZE)
            nc_pg[0] = PAGE_COLUMN_CHUNK
            struct.pack_into('<I', nc_pg, 1, 0)
            struct.pack_into('<I', nc_pg, 5, 0)
            stamp_page(nc_pg)

            old_pg = self._pager.get_page(last_chunk)
            struct.pack_into('<I', old_pg, 1, new_chunk)
            stamp_page(old_pg)

            struct.pack_into('<I', hdr_pg, off + 4, new_chunk)
            struct.pack_into('<I', hdr_pg, off + 8, CHUNK_HDR_SZ)
            last_chunk = new_chunk
            last_pos   = CHUNK_HDR_SZ

        chunk_pg = self._pager.get_page(last_chunk)
        chunk_pg[last_pos:last_pos + len(entry)] = entry
        ec = struct.unpack_from('<I', chunk_pg, 5)[0] + 1
        struct.pack_into('<I', chunk_pg, 5, ec)
        struct.pack_into('<I', hdr_pg, off + 8, last_pos + len(entry))
        stamp_page(chunk_pg)

    # ── Core DML ──────────────────────────────────────────────────────────────

    def insert_row(self, rowid: int, row: dict) -> None:
        hdr_pg = self._pager.get_page(self._root)
        rc = struct.unpack_from('<I', hdr_pg, 1)[0]
        struct.pack_into('<I', hdr_pg, 1, rc + 1)

        self._append_entry(0, rowid, INTEGER, hdr_pg)
        for i, cname in enumerate(self._col_names):
            self._append_entry(i + 1, row.get(cname), self._col_types[cname], hdr_pg)

        stamp_page(hdr_pg)

    def scan_rows(self) -> Iterator[tuple[int, dict]]:
        """Yield (rowid, row_dict) for every row. No serialization."""
        hdr_pg    = self._pager.read_page(self._root)
        row_count = struct.unpack_from('<I', hdr_pg, 1)[0]
        if row_count == 0:
            return

        rowids    = self._read_col_entries(0, INTEGER)
        col_data  = {
            cname: self._read_col_entries(i + 1, self._col_types[cname])
            for i, cname in enumerate(self._col_names)
        }

        for idx, rowid in enumerate(rowids):
            yield rowid, {cname: col_data[cname][idx] for cname in self._col_names}

    def apply_updates(self, updates: dict[int, dict],
                      affected_cols: set[str]) -> None:
        """Rewrite only the column chains that were modified."""
        if not updates:
            return

        # Map rowid → physical index via _rowid column
        rowid_list = self._read_col_entries(0, INTEGER)
        rid_to_idx: dict[int, int] = {rid: i for i, rid in enumerate(rowid_list)}

        hdr_pg = self._pager.get_page(self._root)

        for i, cname in enumerate(self._col_names):
            if cname not in affected_cols:
                continue
            col_idx  = i + 1
            col_type = self._col_types[cname]
            values   = self._read_col_entries(col_idx, col_type)
            for rowid, new_row in updates.items():
                phys = rid_to_idx.get(rowid)
                if phys is not None:
                    values[phys] = new_row.get(cname)
            self._rewrite_col_chain(col_idx, col_type, values, hdr_pg)

        stamp_page(hdr_pg)

    def apply_deletes(self, rowids_to_delete: set[int]) -> None:
        """Rewrite all column chains omitting deleted rows."""
        if not rowids_to_delete:
            return

        rowid_list = self._read_col_entries(0, INTEGER)
        keep = [i for i, rid in enumerate(rowid_list)
                if rid not in rowids_to_delete]

        hdr_pg = self._pager.get_page(self._root)

        # Rewrite _rowid column
        self._rewrite_col_chain(
            0, INTEGER, [rowid_list[i] for i in keep], hdr_pg)

        # Rewrite each data column
        for i, cname in enumerate(self._col_names):
            col_idx  = i + 1
            col_type = self._col_types[cname]
            values   = self._read_col_entries(col_idx, col_type)
            self._rewrite_col_chain(col_idx, col_type, [values[i] for i in keep], hdr_pg)

        struct.pack_into('<I', hdr_pg, 1, len(keep))
        stamp_page(hdr_pg)

    # ── Aggregate pushdown ────────────────────────────────────────────────────

    def scan_aggregate(self, columns: list[str],
                       row_filter: "Callable[[dict], bool] | None" = None,
                       ) -> list[dict] | None:
        """Streaming aggregates, optionally filtered by a WHERE predicate.

        When row_filter is None (no WHERE) and the query is COUNT(*), uses the
        O(1) header read.  When row_filter is provided, scans via scan_rows() and
        accumulates in O(1) memory — never builds a matching-rows list.

        Returns a single-element list or None if pushdown is not applicable
        (DISTINCT, unknown column, non-aggregate expression).
        """
        # Parse and validate all aggregate specs up front.
        specs: list[tuple[str, str, str]] = []  # (expr, FUNC, arg)
        for expr in columns:
            m = _AGG_PAT.match(expr.strip())
            if not m:
                return None
            func, distinct_kw, arg = (m.group(1).upper(),
                                      m.group(2),
                                      m.group(3).strip())
            if distinct_kw:
                return None
            if func != "COUNT" or arg != "*":
                col_idx, _ = self._col_index_and_type(arg)
                if col_idx is None:
                    return None
            specs.append((expr, func, arg))

        # Fast path: no WHERE — COUNT(*) is O(1), others stream chunk pages directly.
        if row_filter is None:
            result: dict[str, Any] = {}
            for expr, func, arg in specs:
                if func == "COUNT" and arg == "*":
                    hdr_pg = self._pager.read_page(self._root)
                    result[expr] = struct.unpack_from('<I', hdr_pg, 1)[0]
                else:
                    col_idx, col_type = self._col_index_and_type(arg)
                    result[expr] = self._stream_agg(col_idx, col_type, func)
            return [result]

        # Filtered path: scan_rows() for WHERE evaluation, accumulate inline.
        totals:  dict[str, float] = {e: 0.0 for e, f, _ in specs if f in ("SUM", "AVG")}
        counts:  dict[str, int]   = {e: 0   for e, _, _ in specs}
        extrema: dict[str, Any]   = {e: None for e, f, _ in specs if f in ("MIN", "MAX")}

        for _, row in self.scan_rows():
            if not row_filter(row):
                continue
            for expr, func, arg in specs:
                if func == "COUNT" and arg == "*":
                    counts[expr] += 1
                else:
                    v = row.get(arg)
                    if v is not None:
                        counts[expr] += 1
                        if func in ("SUM", "AVG"):
                            totals[expr] += float(v)
                        elif func == "MIN":
                            if extrema[expr] is None or v < extrema[expr]:
                                extrema[expr] = v
                        elif func == "MAX":
                            if extrema[expr] is None or v > extrema[expr]:
                                extrema[expr] = v

        result = {}
        for expr, func, arg in specs:
            if func == "COUNT":
                result[expr] = counts[expr]
            elif func == "SUM":
                result[expr] = totals[expr] if counts[expr] > 0 else None
            elif func == "AVG":
                result[expr] = totals[expr] / counts[expr] if counts[expr] > 0 else None
            else:
                result[expr] = extrema[expr]
        return [result]

    def _col_index_and_type(self, col_name: str) -> tuple[int | None, str | None]:
        for i, cname in enumerate(self._col_names):
            if cname == col_name:
                return i + 1, self._col_types[cname]
        return None, None

    def _stream_agg(self, col_idx: int, col_type: str, func: str) -> Any:
        hdr_pg     = self._pager.read_page(self._root)
        off        = HDR_PREFIX_SZ + col_idx * HDR_ENTRY_SZ
        first_chunk = struct.unpack_from('<I', hdr_pg, off)[0]

        total    = 0.0
        count    = 0
        extremum = None
        pn       = first_chunk
        while pn:
            pg    = self._pager.read_page(pn)
            n     = struct.unpack_from('<I', pg, 5)[0]
            next_ = struct.unpack_from('<I', pg, 1)[0]
            pos   = CHUNK_HDR_SZ
            for _ in range(n):
                value, pos = _decode_entry(pg, pos, col_type, self._pager)
                if value is not None:
                    count += 1
                    if func in ('SUM', 'AVG'):
                        total += float(value)
                    elif func == 'MIN':
                        if extremum is None or value < extremum:
                            extremum = value
                    elif func == 'MAX':
                        if extremum is None or value > extremum:
                            extremum = value
            pn = next_

        if func == 'COUNT':  return count
        if func == 'SUM':    return total if count > 0 else None
        if func == 'AVG':    return total / count if count > 0 else None
        return extremum  # MIN or MAX

    def scan_aggregate_grouped(self, group_cols: list[str],
                               columns: list[str]) -> list[dict] | None:
        """Streaming GROUP BY aggregates reading only the needed column chains.

        Only reads the GROUP BY columns and aggregate argument columns — all other
        columns are skipped entirely. Returns None if any selected column is not
        a GROUP BY key or a pushdown-eligible aggregate (no DISTINCT, no expressions,
        known functions only).
        """
        # Classify every selected column as a GROUP BY key or an aggregate spec.
        group_col_set = set(group_cols)
        agg_specs: list[tuple[str, str, str]] = []  # (expr, FUNC, arg_or_star)
        for col in columns:
            if col in group_col_set:
                continue  # GROUP BY key — handled separately
            m = _AGG_PAT.match(col.strip())
            if not m:
                return None  # expression or alias we can't push down
            func, distinct_kw, arg = (m.group(1).upper(),
                                      m.group(2),
                                      m.group(3).strip())
            if distinct_kw:
                return None
            agg_specs.append((col, func, arg))

        # Read GROUP BY column chains (only these columns).
        group_chains: list[list] = []
        for gc in group_cols:
            col_idx, col_type = self._col_index_and_type(gc)
            if col_idx is None:
                return None
            group_chains.append(self._read_col_entries(col_idx, col_type))

        if not group_chains:
            return None
        n_rows = len(group_chains[0])

        # Build buckets: group_key_tuple → list of physical row indices.
        buckets: dict[tuple, list[int]] = {}
        for i in range(n_rows):
            key = tuple(chain[i] for chain in group_chains)
            if key not in buckets:
                buckets[key] = []
            buckets[key].append(i)

        # Pre-read each distinct aggregate argument column (deduplicated).
        agg_col_data: dict[int, list] = {}
        for _, func, arg in agg_specs:
            if func == "COUNT" and arg == "*":
                continue
            col_idx, col_type = self._col_index_and_type(arg)
            if col_idx is None:
                return None
            if col_idx not in agg_col_data:
                agg_col_data[col_idx] = self._read_col_entries(col_idx, col_type)

        # Assemble one result row per bucket.
        results: list[dict] = []
        for key, indices in buckets.items():
            row: dict[str, Any] = {}
            for gc, kv in zip(group_cols, key):
                row[gc] = kv
            for expr, func, arg in agg_specs:
                if func == "COUNT" and arg == "*":
                    row[expr] = len(indices)
                else:
                    col_idx, _ = self._col_index_and_type(arg)
                    col_vals = agg_col_data[col_idx]
                    vals = [col_vals[i] for i in indices if col_vals[i] is not None]
                    if func == "COUNT":
                        row[expr] = len(vals)
                    elif func == "SUM":
                        row[expr] = sum(float(v) for v in vals) if vals else None
                    elif func == "AVG":
                        row[expr] = (sum(float(v) for v in vals) / len(vals)
                                     if vals else None)
                    elif func == "MIN":
                        row[expr] = min(vals) if vals else None
                    elif func == "MAX":
                        row[expr] = max(vals) if vals else None
                    else:
                        return None
            results.append(row)

        return results

    # ── BTree-compatible scan (for vacuum / iterdump / ANALYZE) ───────────────

    def scan(self) -> Iterator[tuple[int, bytes]]:
        """Yield (rowid, packed_cell) — same interface as BTree.scan()."""
        from .database import Database  # avoid circular at import time
        from .constants import ROW_CELL_SIZE, ROW_INLINE_CAP
        for rowid, row in self.scan_rows():
            raw = serialize_row(self._schema, row)
            cell = bytearray(ROW_CELL_SIZE)
            if len(raw) <= ROW_INLINE_CAP:
                cell[0] = 0
                struct.pack_into('I', cell, 1, len(raw))
                struct.pack_into('I', cell, 5, 0)
                cell[9:9 + len(raw)] = raw
            else:
                first_page = _write_col_overflow(
                    self._pager, self._alloc, raw)
                cell[0] = 1
                struct.pack_into('I', cell, 1, len(raw))
                struct.pack_into('I', cell, 5, first_page)
            yield rowid, bytes(cell)

    # ── Page management ───────────────────────────────────────────────────────

    def drop(self) -> None:
        """Free all pages owned by this column store."""
        hdr_pg   = self._pager.read_page(self._root)
        num_cols = struct.unpack_from('<H', hdr_pg, 5)[0]

        for col_idx in range(num_cols):
            off   = HDR_PREFIX_SZ + col_idx * HDR_ENTRY_SZ
            first = struct.unpack_from('<I', hdr_pg, off)[0]
            ctype = (INTEGER if col_idx == 0
                     else self._col_types.get(self._col_names[col_idx - 1], TEXT))
            self._free_col_chain(first, ctype)

        self._free_page(self._root)

    def _free_col_chain(self, first_chunk: int, col_type: str) -> None:
        pn = first_chunk
        while pn:
            pg    = self._pager.read_page(pn)
            next_ = struct.unpack_from('<I', pg, 1)[0]
            count = struct.unpack_from('<I', pg, 5)[0]
            if col_type not in (INTEGER, REAL):
                pos = CHUNK_HDR_SZ
                for _ in range(count):
                    if pg[pos] == 0x02:
                        ofp = struct.unpack_from('<I', pg, pos + 1)[0]
                        _free_col_overflow(self._pager, self._free_page, ofp)
                    pos += _entry_size_from_buf(pg, pos, col_type)
            self._free_page(pn)
            pn = next_

    def _rewrite_col_chain(self, col_idx: int, col_type: str,
                           values: list, hdr_pg: bytearray) -> None:
        off = HDR_PREFIX_SZ + col_idx * HDR_ENTRY_SZ
        old_first = struct.unpack_from('<I', hdr_pg, off)[0]
        self._free_col_chain(old_first, col_type)

        new_first = self._alloc()
        pg = self._pager.get_page(new_first)
        pg[:PAGE_SIZE] = bytearray(PAGE_SIZE)
        pg[0] = PAGE_COLUMN_CHUNK
        struct.pack_into('<I', pg, 1, 0)
        struct.pack_into('<I', pg, 5, 0)
        stamp_page(pg)

        struct.pack_into('<I', hdr_pg, off,     new_first)
        struct.pack_into('<I', hdr_pg, off + 4, new_first)
        struct.pack_into('<I', hdr_pg, off + 8, CHUNK_HDR_SZ)

        for v in values:
            self._append_entry(col_idx, v, col_type, hdr_pg)

    def _read_col_entries(self, col_idx: int, col_type: str) -> list:
        hdr_pg     = self._pager.read_page(self._root)
        off        = HDR_PREFIX_SZ + col_idx * HDR_ENTRY_SZ
        first_chunk = struct.unpack_from('<I', hdr_pg, off)[0]

        entries = []
        pn      = first_chunk
        while pn:
            pg    = self._pager.read_page(pn)
            count = struct.unpack_from('<I', pg, 5)[0]
            next_ = struct.unpack_from('<I', pg, 1)[0]
            pos   = CHUNK_HDR_SZ
            for _ in range(count):
                value, pos = _decode_entry(pg, pos, col_type, self._pager)
                entries.append(value)
            pn = next_
        return entries
