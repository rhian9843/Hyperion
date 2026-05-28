import struct
from typing import Any

from .schema import Schema, serialize_row, deserialize_row
from .constants import INTEGER, REAL
from .encoding import _encode_composite_key, _make_index_key
from .expr import eval_expr, is_expr, _set_last_insert_rowid
from .ddl import _index_col_types


def _integer_pk_col(schema: Schema) -> str | None:
    """Return the single INTEGER PRIMARY KEY column name, or None.

    Returns None for composite PKs, non-integer PKs, or tables with no PK.
    When non-None, the column's value is used directly as the B-tree rowid
    (SQLite rowid-table aliasing semantics).

    Handles both inline syntax (id INTEGER PRIMARY KEY) and table-level
    syntax (PRIMARY KEY (id)) by checking col.primary_key or primary_key_columns.
    """
    # Collect all PK columns from either source
    pk_cols = [c for c in schema.columns if c.primary_key]
    if not pk_cols and schema.primary_key_columns:
        pk_cols = [c for c in schema.columns
                   if c.name in schema.primary_key_columns]
    if len(pk_cols) == 1 and pk_cols[0].type == INTEGER:
        return pk_cols[0].name
    return None


class DMLMixin:
    """DML methods (INSERT / UPDATE / DELETE) mixed into Database."""

    def insert(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
        meta   = self._meta(table)
        schema = meta.schema
        ipk    = _integer_pk_col(schema)

        if ipk:
            # INTEGER PRIMARY KEY: use the column value as the B-tree rowid.
            pk_val = row.get(ipk)
            if pk_val is None:
                # Auto-assign: next_key tracks max(pk)+1 across all inserts.
                pk_val = meta.next_key
                row = {**row, ipk: pk_val}
            else:
                pk_val = int(pk_val)
                row = {**row, ipk: pk_val}
            rowid = pk_val
            # Keep next_key ahead of the largest key ever inserted.
            if pk_val >= meta.next_key:
                meta.next_key = pk_val + 1
        else:
            # Non-IPK tables: resolve AUTOINCREMENT columns by scanning for MAX.
            for col in schema.columns:
                if col.autoincrement and col.type == INTEGER and row.get(col.name) is None:
                    max_val = 0
                    for _, raw in self._table_btree(meta).scan():
                        v = deserialize_row(schema, self._unpack_row_cell(raw)).get(col.name)
                        if v is not None and int(v) > max_val:
                            max_val = int(v)
                    row = {**row, col.name: max_val + 1}
            rowid = meta.next_key
            meta.next_key += 1

        self._check_unique(meta, row)
        self._check_constraints(meta.schema, row)
        self._check_fk_child(meta.schema, row)
        data  = self._pack_row_cell(serialize_row(meta.schema, row))
        self._table_btree(meta).insert(rowid, data)
        _set_last_insert_rowid(rowid)
        schema = meta.schema
        for idx_meta in self._indexes_for(table):
            vals = [eval_expr(n, row) if is_expr(n) else row.get(n)
                    for n in idx_meta.columns]
            if all(v is not None for v in vals):
                col_types = _index_col_types(idx_meta.columns, schema.columns)
                self._index_btree(idx_meta).insert(
                    _make_index_key(_encode_composite_key(vals, col_types), rowid),
                    struct.pack("q", rowid))
        return row

    def update(self, table: str, assignments: dict[str, str],
               where: "WhereClause | None",
               limit: int | None = None) -> list[dict]:
        meta   = self._meta(table)
        schema = meta.schema
        tree   = self._table_btree(meta)
        idxs   = self._indexes_for(table)
        updates:      dict[int, bytes] = {}
        idx_ops:      list[tuple]      = []
        updated_rows: list[dict]       = []

        old_overflow: list[int] = []  # first_page of overflow chains to free after update
        count = 0
        for rowid, raw in tree.scan():
            if limit is not None and count >= limit:
                break
            row = deserialize_row(schema, self._unpack_row_cell(raw))
            if where and not where.evaluate(row, self):
                continue
            new_row = dict(row)
            for col, val in assignments.items():
                col_obj = next((c for c in schema.columns if c.name == col), None)
                if col_obj is None:
                    raise RuntimeError(f"Column '{col}' not found")
                if val is None or (isinstance(val, str) and val.upper() == "NULL"):
                    new_row[col] = None
                    continue
                resolved = eval_expr(str(val), new_row) if is_expr(str(val)) else val
                if col_obj.type == INTEGER:
                    try:
                        new_row[col] = int(resolved)
                    except (ValueError, TypeError):
                        new_row[col] = resolved
                elif col_obj.type == REAL:
                    try:
                        new_row[col] = float(resolved)
                    except (ValueError, TypeError):
                        new_row[col] = resolved
                else:
                    new_row[col] = resolved
            self._check_unique(meta, new_row, exclude_rowid=rowid)
            self._check_constraints(schema, new_row)
            self._check_fk_child(schema, new_row)
            fks_ref = self._fks_referencing(table)
            if fks_ref:
                ref_cols_set = {c for fk in fks_ref for c in fk.ref_columns}
                if ref_cols_set & assignments.keys():
                    self._check_fk_parent(table, row, is_delete=False, new_row=new_row)
            if self._cell_is_overflow(raw):
                old_overflow.append(struct.unpack_from("I", raw, 5)[0])
            updates[rowid] = self._pack_row_cell(serialize_row(schema, new_row))
            updated_rows.append(new_row)
            count += 1
            for im in idxs:
                # Expression indexes always recompute (expr may depend on any column)
                affected = (any(is_expr(c) for c in im.columns)
                            or any(c in assignments for c in im.columns))
                if affected:
                    col_types = _index_col_types(im.columns, schema.columns)
                    old_vals = [eval_expr(c, row) if is_expr(c) else row.get(c)
                                for c in im.columns]
                    new_vals = [eval_expr(c, new_row) if is_expr(c) else new_row.get(c)
                                for c in im.columns]
                    old_k = (_make_index_key(_encode_composite_key(old_vals, col_types), rowid)
                             if all(v is not None for v in old_vals) else None)
                    new_k = (_make_index_key(_encode_composite_key(new_vals, col_types), rowid)
                             if all(v is not None for v in new_vals) else None)
                    idx_ops.append((im, old_k, new_k, rowid))

        tree.update(updates)
        for fp in old_overflow:
            self._free_overflow(fp)
        for im, old_k, new_k, rowid in idx_ops:
            itree = self._index_btree(im)
            if old_k is not None:
                itree.delete({old_k})
            if new_k is not None:
                itree.insert(new_k, struct.pack("q", rowid))
        return updated_rows

    def delete(self, table: str, where: "WhereClause | None",
               limit: int | None = None) -> list[dict]:
        meta   = self._meta(table)
        schema = meta.schema
        tree   = self._table_btree(meta)
        idxs   = self._indexes_for(table)
        victims: list[tuple[int, dict]] = []
        overflow_to_free: list[int] = []
        count = 0
        for rowid, raw in tree.scan():
            if limit is not None and count >= limit:
                break
            row = deserialize_row(schema, self._unpack_row_cell(raw))
            if not where or where.evaluate(row, self):
                victims.append((rowid, row))
                if self._cell_is_overflow(raw):
                    overflow_to_free.append(struct.unpack_from("I", raw, 5)[0])
                count += 1
        if not victims:
            return []
        for _, row in victims:
            self._check_fk_parent(table, row, is_delete=True)
        rowids = {r for r, _ in victims}
        tree.delete(rowids)
        for fp in overflow_to_free:
            self._free_overflow(fp)
        for im in idxs:
            col_types = _index_col_types(im.columns, schema.columns)
            itree     = self._index_btree(im)
            idx_keys: set[int] = set()
            for rowid, row in victims:
                vals = [eval_expr(n, row) if is_expr(n) else row.get(n)
                        for n in im.columns]
                if all(v is not None for v in vals):
                    idx_keys.add(
                        _make_index_key(_encode_composite_key(vals, col_types), rowid))
            itree.delete(idx_keys)
        return [row for _, row in victims]
