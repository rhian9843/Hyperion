import struct
from pathlib import Path
from typing import Any, Callable

from .constants import PAGE_SIZE
from .btree import BTree
from .catalog import Catalog, TableMeta, IndexMeta
from .pager import Pager, MemoryPager
from .encoding import _IDX_KEY_SZ
from .constraints import ConstraintsMixin
from .ddl import DDLMixin
from .dml import DMLMixin
from .query import QueryMixin

_CAT_HDR   = 8
_CAT_CHUNK = PAGE_SIZE - _CAT_HDR


class Database(DDLMixin, DMLMixin, QueryMixin, ConstraintsMixin):
    def __init__(self, path: "Path | str"):
        if str(path) == ":memory:":
            self._pager: Pager | MemoryPager = MemoryPager()
        else:
            self._pager = Pager(Path(path))
        self._catalog, self._catalog_extra = self._load_catalog()
        self._txn_depth      = 0
        # Each entry: (name, page_snapshots, dirty_set, catalog_bytes, catalog_extra)
        self._savepoints: list[tuple] = []
        self.fk_enforcement  = True

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
        self._savepoints.clear()
        self._pager.rollback()
        self._reload_catalog()
        self._txn_depth = 0

    # ── Savepoints ─────────────────────────────────────────────────────────────

    def savepoint(self, name: str) -> None:
        if self._txn_depth == 0:
            self._pager.begin()
            self._txn_depth = 1
        pages_snap = {n: bytes(self._pager._cache[n])
                      for n in self._pager._dirty if n in self._pager._cache}
        dirty_snap = set(self._pager._dirty)
        cat_bytes  = self._catalog.to_bytes()
        cat_extra  = list(self._catalog_extra)
        self._savepoints.append((name, pages_snap, dirty_snap, cat_bytes, cat_extra))

    def release_savepoint(self, name: str) -> None:
        idx = self._find_savepoint(name)
        del self._savepoints[idx:]

    def rollback_to_savepoint(self, name: str) -> None:
        idx = self._find_savepoint(name)
        _, pages_snap, dirty_snap, cat_bytes, cat_extra = self._savepoints[idx]
        del self._savepoints[idx + 1:]  # keep this savepoint alive (SQLite behaviour)
        # Evict pages added after the savepoint
        for pn in set(self._pager._dirty) - dirty_snap:
            self._pager._cache.pop(pn, None)
        # Restore snapshotted page contents
        for pn, content in pages_snap.items():
            self._pager._cache[pn] = bytearray(content)
        self._pager._dirty = set(dirty_snap)
        # Restore catalog
        self._catalog      = Catalog.from_bytes(cat_bytes)
        self._catalog_extra = list(cat_extra)

    def _find_savepoint(self, name: str) -> int:
        for i in range(len(self._savepoints) - 1, -1, -1):
            if self._savepoints[i][0] == name:
                return i
        raise RuntimeError(f"No such savepoint: '{name}'")

    def _load_catalog(self) -> tuple["Catalog", list[int]]:
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

    def _flush_catalog(self) -> None:
        for _ in range(4):
            payload  = self._catalog.to_bytes()
            n_needed = max(1, (len(payload) + _CAT_CHUNK - 1) // _CAT_CHUNK)
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
        return BTree(self._pager, meta.root_page, meta.schema.row_size,
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
        if name in self._catalog.views:
            if or_replace:
                pass  # overwrite below
            elif if_not_exists:
                return
            else:
                raise RuntimeError(f"View '{name}' already exists")
        self._catalog.views[name] = sql

    def drop_view(self, name: str, if_exists: bool = False) -> None:
        if name not in self._catalog.views:
            if if_exists:
                return
            raise RuntimeError(f"No such view: '{name}'")
        del self._catalog.views[name]

    def close(self) -> None:
        temp_tables = [n for n, m in self._catalog.tables.items() if m.temporary]
        if temp_tables:
            self.begin()
            for name in temp_tables:
                self.drop_table(name)
            self.commit()
        self._pager.close()

    def vacuum(self) -> str:
        """Rebuild the database file compactly, reclaiming space from deleted rows."""
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
                row = deserialize_row(tmeta.schema, raw)
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
        self._catalog, self._catalog_extra = self._load_catalog()
        self._txn_depth = 0
        self._savepoints.clear()
        return "Database vacuumed."
