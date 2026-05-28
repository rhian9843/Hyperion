import struct
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .cursor import Cursor

from .constants import (PAGE_SIZE, PAGE_CKSUM_SZ, ROW_CELL_SIZE, ROW_INLINE_CAP,
                        PAGE_OVERFLOW, OVERFLOW_HDR, OVERFLOW_DATA_SZ)
from .btree import BTree
from .catalog import Catalog, TableMeta, IndexMeta
from .pager import Pager, MemoryPager
from .encoding import _IDX_KEY_SZ
from .constraints import ConstraintsMixin
from .ddl import DDLMixin
from .dml import DMLMixin
from .query import QueryMixin

# Page-0 header: [next_schema_pn: 4][schema_chunk_len: 4][ops_pn: 4][magic: 4]
_CAT0_HDR    = 16
_CAT0_MAGIC  = 0xCAFEBABE           # distinguishes split format from old format
_CAT0_CHUNK  = PAGE_SIZE - _CAT0_HDR - PAGE_CKSUM_SZ  # 4076

# Extra schema/ops pages share the same compact header
_CAT_HDR     = 8                    # [next_pn: 4][chunk_len: 4]
_CAT_CHUNK   = PAGE_SIZE - _CAT_HDR - PAGE_CKSUM_SZ    # 4084


class _ReadOnlyContext:
    __slots__ = ("_db", "_prev")

    def __init__(self, db: "Database") -> None:
        self._db = db

    def __enter__(self) -> "Database":
        self._prev = self._db._readonly
        self._db._readonly = True
        return self._db

    def __exit__(self, *_) -> bool:
        self._db._readonly = self._prev
        return False


class Database(DDLMixin, DMLMixin, QueryMixin, ConstraintsMixin):
    def __init__(self, path: "Path | str", *, readonly: bool = False):
        if str(path) == ":memory:":
            self._pager: Pager | MemoryPager = MemoryPager()
        else:
            self._pager = Pager(Path(path))
        self._readonly = readonly
        (self._catalog,
         self._catalog_extra,
         self._catalog_ops_pn,
         self._catalog_ops_extra) = self._load_catalog()
        self._txn_depth      = 0
        # Each entry: (name, pages_snap, dirty_set, cat_bytes, cat_extra,
        #              ops_pn, ops_extra)
        self._savepoints: list[tuple] = []
        self.fk_enforcement  = True
        self.row_factory     = None   # callable(cursor, row_dict) -> Any; None = dict
        self._authorizer     = None   # callable(action, table, col, db, trigger) -> int
        self._plan_cache: dict[str, dict] = {}  # raw SQL template → parsed AST
        # Schema bytes cache: skip page writes when structure hasn't changed.
        # Ops are always written (they're small and change on every INSERT).
        self._schema_flushed_bytes: bytes = self._catalog.schema_to_bytes()
        # Coarse-grained reentrant lock: serialises all public API calls so that
        # two threads sharing one Database object don't corrupt _catalog,
        # _txn_depth, _savepoints, _plan_cache, or pager state.
        self._lock = threading.RLock()

    # ── Read-only toggle ──────────────────────────────────────────────────────

    @property
    def readonly(self) -> bool:
        return self._readonly

    @readonly.setter
    def readonly(self, value: bool) -> None:
        self._readonly = value

    def as_readonly(self):
        """Context manager: enforce read-only mode for the duration of the block.

        Restores the previous readonly state on exit regardless of exceptions.

        Usage::
            with db.as_readonly():
                agent.query(db)   # only SELECT allowed here
            db.execute("INSERT ...")  # writes allowed again
        """
        return _ReadOnlyContext(self)

    # ── Transaction control ────────────────────────────────────────────────────

    @property
    def in_transaction(self) -> bool:
        return self._txn_depth > 0

    def begin(self) -> None:
        with self._lock:
            if self._txn_depth > 0:
                raise RuntimeError("Transaction already active")
            self._pager.begin()
            self._txn_depth = 1

    def commit(self) -> None:
        with self._lock:
            if self._txn_depth == 0:
                raise RuntimeError("No active transaction")
            self._flush_catalog()
            self._pager.commit()
            self._txn_depth = 0

    def rollback(self) -> None:
        with self._lock:
            if self._txn_depth == 0:
                raise RuntimeError("No active transaction")
            self._savepoints.clear()
            self._pager.rollback()
            self._reload_catalog()
            self._txn_depth = 0

    # ── Savepoints ─────────────────────────────────────────────────────────────

    def savepoint(self, name: str) -> None:
        with self._lock:
            if self._txn_depth == 0:
                self._pager.begin()
                self._txn_depth = 1
            pages_snap = {n: bytes(self._pager._working[n])
                          for n in self._pager._dirty if n in self._pager._working}
            dirty_snap  = set(self._pager._dirty)
            cat_bytes   = self._catalog.to_bytes()
            cat_extra   = list(self._catalog_extra)
            ops_pn      = self._catalog_ops_pn
            ops_extra   = list(self._catalog_ops_extra)
            self._savepoints.append(
                (name, pages_snap, dirty_snap, cat_bytes, cat_extra, ops_pn, ops_extra))

    def release_savepoint(self, name: str) -> None:
        with self._lock:
            idx = self._find_savepoint(name)
            del self._savepoints[idx:]

    def rollback_to_savepoint(self, name: str) -> None:
        with self._lock:
            idx = self._find_savepoint(name)
            _, pages_snap, dirty_snap, cat_bytes, cat_extra, ops_pn, ops_extra = \
                self._savepoints[idx]
            del self._savepoints[idx + 1:]  # keep this savepoint alive (SQLite behaviour)
            # Evict pages added after the savepoint
            for pn in set(self._pager._dirty) - dirty_snap:
                self._pager._working.pop(pn, None)
            # Restore snapshotted working pages to savepoint state
            for pn, content in pages_snap.items():
                self._pager._working[pn] = bytearray(content)
            self._pager._dirty = set(dirty_snap)
            # Restore catalog and page-chain metadata
            self._catalog           = Catalog.from_bytes(cat_bytes)
            self._catalog_extra     = list(cat_extra)
            self._catalog_ops_pn    = ops_pn
            self._catalog_ops_extra = list(ops_extra)
            # Invalidate schema cache so the next commit forces a full schema write.
            self._schema_flushed_bytes = b""

    # ── Application-defined functions ──────────────────────────────────────────

    def create_function(self, name: str, n_args: int, fn) -> None:
        """Register a custom scalar function callable from SQL.

        Args:
            name:   SQL function name (case-insensitive).
            n_args: Number of expected arguments, or -1 for variadic.
            fn:     Callable invoked with evaluated SQL arguments.
        """
        with self._lock:
            from .expr import _USER_FUNCS
            _USER_FUNCS[name.upper()] = (n_args, fn)

    def create_aggregate(self, name: str, n_args: int, aggregate_class) -> None:
        """Register a custom aggregate function callable from SQL GROUP BY.

        aggregate_class must implement:
            __init__(self)      — called once per group
            step(self, *args)   — called once per row in the group
            finalize(self)      — called after all rows; returns the result

        Args:
            name:            SQL function name (case-insensitive).
            n_args:          Number of per-row arguments, or -1 for variadic.
            aggregate_class: Class implementing the aggregate protocol.
        """
        with self._lock:
            from .expr import _USER_AGGS
            _USER_AGGS[name.upper()] = (n_args, aggregate_class)

    # ── PEP 249 DB-API ────────────────────────────────────────────────────────

    def cursor(self) -> "Cursor":
        from .cursor import Cursor
        return Cursor(self)

    def execute(self, sql: str, params=None, timeout_ms: int | None = None) -> "Cursor":
        return self.cursor().execute(sql, params, timeout_ms=timeout_ms)

    def executemany(self, sql: str, params_seq) -> "Cursor":
        return self.cursor().executemany(sql, params_seq)

    def executescript(self, sql: str) -> "Cursor":
        return self.cursor().executescript(sql)

    def set_authorizer(self, fn) -> None:
        """Register an authorizer callback invoked before each SQL operation.

        fn(action_code, table, column, db_name, trigger_name) -> int
        Return SQLITE_OK (0) to allow, SQLITE_DENY (1) to raise an error,
        or SQLITE_IGNORE (2) to silently skip the operation.
        Pass None to remove the authorizer.
        """
        with self._lock:
            self._authorizer = fn

    def iterdump(self):
        """Yield SQL statements that recreate the full database (like sqlite3.iterdump)."""
        with self._lock:
            lines = list(self._iterdump_inner())
        yield from lines

    def _iterdump_inner(self):
        from .schema import deserialize_row
        from .introspect import _schema_to_sql, _trigger_to_sql
        from .cursor import _sql_literal

        yield "BEGIN TRANSACTION;"

        for tname, tmeta in self._catalog.tables.items():
            yield _schema_to_sql(tname, tmeta.schema, tmeta.temporary) + ";"
            for _, raw in self._table_btree(tmeta).scan():
                row = deserialize_row(tmeta.schema, self._unpack_row_cell(raw))
                cols = [c.name for c in tmeta.schema.columns if not c.is_generated]
                vals = ", ".join(_sql_literal(row.get(c)) for c in cols)
                yield f"INSERT INTO \"{tname}\" VALUES ({vals});"

        for iname, imeta in self._catalog.indexes.items():
            if iname.startswith("_pk_"):
                continue  # auto-created by CREATE TABLE PRIMARY KEY
            cols = ", ".join(imeta.columns)
            yield f"CREATE INDEX \"{iname}\" ON \"{imeta.table_name}\" ({cols});"

        for vname, vsql in self._catalog.views.items():
            yield f"CREATE VIEW \"{vname}\" AS {vsql};"

        for trig_name, tmeta in self._catalog.triggers.items():
            yield _trigger_to_sql(trig_name, tmeta) + ";"

        yield "COMMIT;"

    # ── Context manager ───────────────────────────────────────────────────────

    def __enter__(self) -> "Database":
        if not self.in_transaction:
            self.begin()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if exc_type is None:
            if self.in_transaction:
                self.commit()
        else:
            if self.in_transaction:
                self.rollback()
        return False

    def _find_savepoint(self, name: str) -> int:
        for i in range(len(self._savepoints) - 1, -1, -1):
            if self._savepoints[i][0] == name:
                return i
        raise RuntimeError(f"No such savepoint: '{name}'")

    def _load_catalog(self) -> "tuple[Catalog, list[int], int, list[int]]":
        """Load catalog from pages.  Returns (catalog, schema_extras, ops_pn, ops_extras).

        Detects old (combined-JSON) vs. new (split schema+ops) on-disk format via
        a magic constant at bytes 12-15 of page 0.
        """
        page0 = self._pager.read_page(Catalog.CATALOG_PAGE)
        magic = struct.unpack_from("I", page0, 12)[0]

        if magic == _CAT0_MAGIC:
            return self._load_catalog_split(page0)
        else:
            return self._load_catalog_legacy()

    def _load_catalog_split(self, page0: bytearray) \
            -> "tuple[Catalog, list[int], int, list[int]]":
        """Load new split-format catalog from page 0."""
        next_schema_pn = struct.unpack_from("I", page0, 0)[0]
        schema_len     = struct.unpack_from("I", page0, 4)[0]
        ops_pn         = struct.unpack_from("I", page0, 8)[0]

        # Schema chain
        schema_data    = bytes(page0[_CAT0_HDR: _CAT0_HDR + schema_len])
        schema_extras: list[int] = []
        pn = next_schema_pn
        while pn:
            page      = self._pager.read_page(pn)
            next_pn   = struct.unpack_from("I", page, 0)[0]
            chunk_len = struct.unpack_from("I", page, 4)[0]
            if chunk_len:
                schema_data += bytes(page[_CAT_HDR: _CAT_HDR + chunk_len])
            schema_extras.append(pn)
            pn = next_pn

        # Ops chain
        ops_data    = b""
        ops_extras: list[int] = []
        if ops_pn:
            pn = ops_pn
            first = True
            while pn:
                page      = self._pager.read_page(pn)
                next_pn   = struct.unpack_from("I", page, 0)[0]
                chunk_len = struct.unpack_from("I", page, 4)[0]
                if chunk_len:
                    ops_data += bytes(page[_CAT_HDR: _CAT_HDR + chunk_len])
                if not first:
                    ops_extras.append(pn)
                first = False
                pn = next_pn

        cat = Catalog.from_schema_and_ops_bytes(schema_data, ops_data)
        return cat, schema_extras, ops_pn, ops_extras

    def _load_catalog_legacy(self) -> "tuple[Catalog, list[int], int, list[int]]":
        """Load old combined-JSON catalog (backward compatibility)."""
        data: bytes = b""
        extras: list[int] = []
        pn = Catalog.CATALOG_PAGE
        while True:
            page      = self._pager.read_page(pn)
            next_pn   = struct.unpack_from("I", page, 0)[0]
            chunk_len = struct.unpack_from("I", page, 4)[0]
            if chunk_len:
                data += bytes(page[_CAT_HDR: _CAT_HDR + chunk_len])
            if next_pn == 0:
                break
            extras.append(next_pn)
            pn = next_pn
        return Catalog.from_bytes(data), extras, 0, []

    def _reload_catalog(self) -> None:
        (self._catalog,
         self._catalog_extra,
         self._catalog_ops_pn,
         self._catalog_ops_extra) = self._load_catalog()
        self._schema_flushed_bytes = self._catalog.schema_to_bytes()

    # ── Catalog flush — schema and ops written independently ──────────────────

    def _flush_catalog(self) -> None:
        """Flush catalog to pages.

        Schema pages: written only when structural definitions changed (DDL).
        Ops pages: written on every commit — small regardless of schema size.
        """
        # Ensure ops page is allocated before writing page 0 (schema flush
        # writes page 0, which must carry the correct ops_pn).
        if self._catalog_ops_pn == 0:
            self._catalog_ops_pn = self._alloc_page()

        new_schema = self._catalog.schema_to_bytes()
        if new_schema != self._schema_flushed_bytes:
            self._flush_schema(new_schema)
            self._schema_flushed_bytes = new_schema
        else:
            # Schema unchanged; still need to refresh page 0 when ops_pn was
            # just allocated above (its value changed from 0).
            if self._pager._in_txn:
                page0 = self._pager._working.get(Catalog.CATALOG_PAGE)
                if page0 is None or struct.unpack_from("I", page0, 8)[0] != self._catalog_ops_pn:
                    self._write_page0_header()

        # Always flush ops (cheap — proportional to n_tables, not schema depth)
        self._flush_ops()

    def _write_page0_header(self) -> None:
        """Write page 0's 16-byte header without touching the schema JSON chunk."""
        page           = self._pager.get_page(Catalog.CATALOG_PAGE)
        next_schema_pn = self._catalog_extra[0] if self._catalog_extra else 0
        # Preserve the existing schema chunk length in the header
        schema_len = struct.unpack_from("I", page, 4)[0]
        struct.pack_into("I", page, 0, next_schema_pn)
        struct.pack_into("I", page, 4, schema_len)
        struct.pack_into("I", page, 8, self._catalog_ops_pn)
        struct.pack_into("I", page, 12, _CAT0_MAGIC)
        self._pager.flush(Catalog.CATALOG_PAGE)

    def _flush_schema(self, payload: bytes) -> None:
        """Write schema JSON to the schema page chain (page 0 + schema_extras)."""
        for _ in range(4):
            # Page 0 holds _CAT0_CHUNK bytes; extra pages hold _CAT_CHUNK each
            n_needed = 1 + max(0, (len(payload) - _CAT0_CHUNK + _CAT_CHUNK - 1) // _CAT_CHUNK)
            n_have   = 1 + len(self._catalog_extra)
            if n_needed == n_have:
                break
            if n_needed > n_have:
                for _ in range(n_needed - n_have):
                    self._catalog_extra.append(self._alloc_page())
            else:
                freed = self._catalog_extra[n_needed - 1:]
                self._catalog_extra = self._catalog_extra[:n_needed - 1]
                for pn in freed:
                    self._free_page(pn)
            payload = self._catalog.schema_to_bytes()  # recompute after alloc/free

        # Build chunk list: first chunk is _CAT0_CHUNK, rest are _CAT_CHUNK
        chunks: list[bytes] = []
        if payload:
            chunks.append(payload[:_CAT0_CHUNK])
            rest = payload[_CAT0_CHUNK:]
            chunks += [rest[i: i + _CAT_CHUNK]
                       for i in range(0, len(rest), _CAT_CHUNK)]
        else:
            chunks = [b""]

        all_pns = [Catalog.CATALOG_PAGE] + self._catalog_extra
        while len(chunks) < len(all_pns):
            chunks.append(b"")

        for i, (pn, chunk) in enumerate(zip(all_pns, chunks)):
            page    = self._pager.get_page(pn)
            next_pn = all_pns[i + 1] if i + 1 < len(all_pns) else 0
            if pn == Catalog.CATALOG_PAGE:
                # 16-byte header on page 0
                struct.pack_into("I", page, 0,  next_pn)
                struct.pack_into("I", page, 4,  len(chunk))
                struct.pack_into("I", page, 8,  self._catalog_ops_pn)
                struct.pack_into("I", page, 12, _CAT0_MAGIC)
                page[_CAT0_HDR: _CAT0_HDR + len(chunk)] = chunk
                page[_CAT0_HDR + len(chunk):]            = bytearray(PAGE_SIZE - _CAT0_HDR - len(chunk))
            else:
                # 8-byte header on extra schema pages
                struct.pack_into("I", page, 0, next_pn)
                struct.pack_into("I", page, 4, len(chunk))
                page[_CAT_HDR: _CAT_HDR + len(chunk)] = chunk
                page[_CAT_HDR + len(chunk):]           = bytearray(PAGE_SIZE - _CAT_HDR - len(chunk))
            self._pager.flush(pn)

    def _flush_ops(self) -> None:
        """Write operational-state JSON to the ops page chain."""
        payload = self._catalog.ops_to_bytes()
        for _ in range(4):
            n_needed = max(1, (len(payload) + _CAT_CHUNK - 1) // _CAT_CHUNK)
            n_have   = 1 + len(self._catalog_ops_extra)
            if n_needed == n_have:
                break
            if n_needed > n_have:
                for _ in range(n_needed - n_have):
                    self._catalog_ops_extra.append(self._alloc_page())
            else:
                freed = self._catalog_ops_extra[n_needed - 1:]
                self._catalog_ops_extra = self._catalog_ops_extra[:n_needed - 1]
                for pn in freed:
                    self._free_page(pn)
            payload = self._catalog.ops_to_bytes()

        all_pns = [self._catalog_ops_pn] + self._catalog_ops_extra
        chunks  = [payload[i: i + _CAT_CHUNK]
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

    # ── Internal helpers (used by all mixins via self) ─────────────────────────

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
                stack.append(sibling)
                for i in range(n_cells):
                    rc = struct.unpack_from("I", page,
                                           BTree.HDR + i * int_cell + key_sz)[0]
                    stack.append(rc)
            else:
                if sibling:
                    stack.append(sibling)
        return pages

    def _table_btree(self, meta: TableMeta) -> BTree:
        return BTree(self._pager, meta.root_page, ROW_CELL_SIZE,
                     self._make_alloc(meta))

    def _index_btree(self, idx: IndexMeta) -> BTree:
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

    # ── Overflow page management ──────────────────────────────────────────────

    def _write_overflow(self, data: bytes) -> int:
        """Write data to a linked overflow page chain. Returns first page number."""
        first_page = 0
        prev_pn: int | None = None
        offset = 0
        while offset < len(data) or first_page == 0:
            pn = self._alloc_page()
            if first_page == 0:
                first_page = pn
            if prev_pn is not None:
                pg = self._pager.get_page(prev_pn)
                struct.pack_into("I", pg, 1, pn)
            chunk = data[offset: offset + OVERFLOW_DATA_SZ]
            pg = self._pager.get_page(pn)
            pg[0] = PAGE_OVERFLOW
            struct.pack_into("I", pg, 1, 0)             # next page = 0 (last for now)
            struct.pack_into("I", pg, 5, len(chunk))
            pg[OVERFLOW_HDR: OVERFLOW_HDR + len(chunk)] = chunk
            prev_pn = pn
            offset += OVERFLOW_DATA_SZ
            if offset >= len(data):
                break
        return first_page

    def _read_overflow(self, first_page: int, total_len: int) -> bytes:
        """Reassemble data from an overflow page chain."""
        result = bytearray()
        pn = first_page
        while pn and len(result) < total_len:
            pg       = self._pager.read_page(pn)
            data_len = struct.unpack_from("I", pg, 5)[0]
            result  += pg[OVERFLOW_HDR: OVERFLOW_HDR + data_len]
            pn       = struct.unpack_from("I", pg, 1)[0]
        return bytes(result[:total_len])

    def _free_overflow(self, first_page: int) -> None:
        """Free all pages in an overflow chain."""
        pn = first_page
        while pn:
            pg  = self._pager.read_page(pn)
            nxt = struct.unpack_from("I", pg, 1)[0]
            self._free_page(pn)
            pn  = nxt

    def _pack_row_cell(self, varlen: bytes) -> bytes:
        """Wrap variable-length row bytes into a fixed ROW_CELL_SIZE B-tree cell."""
        cell = bytearray(ROW_CELL_SIZE)
        if len(varlen) <= ROW_INLINE_CAP:
            cell[0] = 0                                  # inline
            struct.pack_into("I", cell, 1, len(varlen))
            struct.pack_into("I", cell, 5, 0)
            cell[9: 9 + len(varlen)] = varlen
        else:
            first_page = self._write_overflow(varlen)
            cell[0] = 1                                  # overflow
            struct.pack_into("I", cell, 1, len(varlen))
            struct.pack_into("I", cell, 5, first_page)
        return bytes(cell)

    def _unpack_row_cell(self, cell: bytes) -> bytes:
        """Extract variable-length row bytes from a ROW_CELL_SIZE B-tree cell."""
        is_overflow = cell[0]
        total_len   = struct.unpack_from("I", cell, 1)[0]
        if not is_overflow:
            return bytes(cell[9: 9 + total_len])
        first_page = struct.unpack_from("I", cell, 5)[0]
        return self._read_overflow(first_page, total_len)

    def _cell_is_overflow(self, cell: bytes) -> bool:
        return bool(cell[0])

    def _indexes_for(self, table: str) -> list[IndexMeta]:
        return [m for m in self._catalog.indexes.values()
                if m.table_name == table]

    @property
    def tables(self) -> dict[str, TableMeta]:
        return self._catalog.tables

    @property
    def indexes(self) -> dict[str, IndexMeta]:
        return self._catalog.indexes

    @property
    def views(self) -> dict[str, str]:
        return self._catalog.views

    def create_view(self, name: str, sql: str,
                    if_not_exists: bool = False,
                    or_replace: bool = False) -> None:
        with self._lock:
            if name in self._catalog.views:
                if or_replace:
                    pass  # overwrite below
                elif if_not_exists:
                    return
                else:
                    raise RuntimeError(f"View '{name}' already exists")
            self._catalog.views[name] = sql

    def drop_view(self, name: str, if_exists: bool = False) -> None:
        with self._lock:
            if name not in self._catalog.views:
                if if_exists:
                    return
                raise RuntimeError(f"No such view: '{name}'")
            del self._catalog.views[name]

    def close(self) -> None:
        with self._lock:
            temp_tables = [n for n, m in self._catalog.tables.items() if m.temporary]
            if temp_tables:
                self.begin()
                for name in temp_tables:
                    self.drop_table(name)
                self.commit()
            self._pager.close()

    def vacuum(self) -> str:
        """Rebuild the database file compactly, reclaiming space from deleted rows."""
        with self._lock:
            return self._vacuum_inner()

    def _vacuum_inner(self) -> str:
        import tempfile, shutil
        from .schema import deserialize_row

        if isinstance(self._pager, MemoryPager):
            return "Database vacuumed."

        if self._txn_depth > 0:
            raise RuntimeError("Cannot VACUUM inside a transaction")

        path = self._pager._path

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp_path = Path(f.name)
        tmp_path.unlink(missing_ok=True)

        new_db = Database(tmp_path)
        new_db.begin()
        for tname, tmeta in list(self._catalog.tables.items()):
            new_db.create_table(tmeta.schema)
            for _, raw in self._table_btree(tmeta).scan():
                row = deserialize_row(tmeta.schema, self._unpack_row_cell(raw))
                new_db.insert(tname, row)
        for idx_name, idx_meta in list(self._catalog.indexes.items()):
            if idx_name not in new_db._catalog.indexes:
                new_db.create_index(idx_name, idx_meta.table_name, idx_meta.columns)
        for vname, vsql in list(self._catalog.views.items()):
            new_db.create_view(vname, vsql)
        new_db.commit()
        new_db._pager.close()

        self._pager.close()
        shutil.move(str(tmp_path), str(path))

        self._pager = Pager(path)
        (self._catalog,
         self._catalog_extra,
         self._catalog_ops_pn,
         self._catalog_ops_extra) = self._load_catalog()
        self._schema_flushed_bytes = self._catalog.schema_to_bytes()
        self._txn_depth = 0
        self._savepoints.clear()
        return "Database vacuumed."
