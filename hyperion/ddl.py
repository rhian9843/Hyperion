import struct
from typing import Any

from .schema import Schema, Column, ForeignKey, serialize_row, deserialize_row
from .btree import BTree
from .catalog import TableMeta, IndexMeta, TriggerMeta
from .encoding import _encode_composite_key, _make_index_key, _IDX_KEY_SZ
from .expr import eval_expr, is_expr
from .constants import TEXT


def _index_col_types(cols: list[str], schema_columns) -> list[str]:
    """Return the storage type for each index column. Expressions default to TEXT."""
    types = []
    for col in cols:
        if is_expr(col):
            types.append(TEXT)
        else:
            types.append(next(c.type for c in schema_columns if c.name == col))
    return types


class DDLMixin:
    """DDL methods (CREATE / DROP / ALTER) mixed into Database."""

    def create_table(self, schema: Schema, temporary: bool = False) -> None:
        if schema.name in self._catalog.tables:
            raise RuntimeError(f"Table '{schema.name}' already exists")
        root = self._alloc_page()
        BTree.init_root_leaf(self._pager, root)
        self._catalog.tables[schema.name] = TableMeta(
            schema=schema, root_page=root,
            next_page=self._catalog.next_free_page, next_key=1,
            temporary=temporary,
        )

    def drop_table(self, name: str) -> None:
        meta = self._meta(name)
        import struct as _s
        for _, raw in self._table_btree(meta).scan():
            if self._cell_is_overflow(raw):
                self._free_overflow(_s.unpack_from("I", raw, 5)[0])
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

    def _rewrite_table(self, meta: TableMeta, old_schema: Schema,
                       new_schema: Schema) -> None:
        """Scan old tree, reserialize rows with new_schema, rebuild on a fresh root."""
        from .constants import ROW_CELL_SIZE
        old_tree   = BTree(self._pager, meta.root_page, ROW_CELL_SIZE,
                           self._make_alloc(meta))
        old_scanned = list(old_tree.scan())
        old_overflow_pages: list[int] = []
        saved: list[tuple] = []
        for rowid, raw in old_scanned:
            if self._cell_is_overflow(raw):
                import struct as _s
                old_overflow_pages.append(_s.unpack_from("I", raw, 5)[0])
            saved.append((rowid, deserialize_row(old_schema, self._unpack_row_cell(raw))))
        old_pages = self._collect_tree_pages(meta.root_page)

        new_root = self._alloc_page()
        BTree.init_root_leaf(self._pager, new_root)
        meta.root_page = new_root
        meta.schema    = new_schema
        new_tree = self._table_btree(meta)
        for rowid, old_row in saved:
            new_row = {c.name: old_row.get(c.name) for c in new_schema.columns}
            new_tree.insert(rowid, self._pack_row_cell(serialize_row(new_schema, new_row)))

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

        for pn in old_pages + old_idx_pages:
            self._free_page(pn)
        for fp in old_overflow_pages:
            self._free_overflow(fp)

    def create_index(self, idx_name: str, table: str, cols: list[str]) -> None:
        if idx_name in self._catalog.indexes:
            raise RuntimeError(f"Index '{idx_name}' already exists")
        meta = self._meta(table)
        for col in cols:
            if not is_expr(col) and not any(c.name == col for c in meta.schema.columns):
                raise RuntimeError(f"Column '{col}' not found in '{table}'")
        root = self._alloc_page()
        BTree.init_root_leaf(self._pager, root)
        idx_meta = IndexMeta(table_name=table, columns=cols,
                             root_page=root,
                             next_page=self._catalog.next_free_page)
        self._catalog.indexes[idx_name] = idx_meta
        tree      = self._table_btree(meta)
        itree     = self._index_btree(idx_meta)
        schema    = meta.schema
        col_types = _index_col_types(cols, schema.columns)
        for rowid, raw in tree.scan():
            row  = deserialize_row(schema, self._unpack_row_cell(raw))
            vals = [eval_expr(n, row) if is_expr(n) else row.get(n) for n in cols]
            if all(v is not None for v in vals):
                itree.insert(
                    _make_index_key(_encode_composite_key(vals, col_types), rowid),
                    struct.pack("q", rowid))

    def create_trigger(self, name: str, trigger: TriggerMeta) -> None:
        if name in self._catalog.triggers:
            raise RuntimeError(f"Trigger '{name}' already exists")
        if trigger.timing == "INSTEAD OF":
            if trigger.table not in self._catalog.views:
                raise RuntimeError(
                    f"INSTEAD OF triggers can only be created on views, "
                    f"not '{trigger.table}'")
        else:
            if trigger.table not in self._catalog.tables:
                raise RuntimeError(f"No such table: '{trigger.table}'")
        self._catalog.triggers[name] = trigger

    def drop_trigger(self, name: str) -> None:
        if name not in self._catalog.triggers:
            raise RuntimeError(f"Trigger '{name}' does not exist")
        del self._catalog.triggers[name]

    def drop_index(self, idx_name: str) -> None:
        if idx_name not in self._catalog.indexes:
            raise RuntimeError(f"Index '{idx_name}' does not exist")
        for pn in self._collect_tree_pages(self._catalog.indexes[idx_name].root_page,
                                           key_sz=_IDX_KEY_SZ):
            self._free_page(pn)
        del self._catalog.indexes[idx_name]
