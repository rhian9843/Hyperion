import struct
from typing import Any

from .schema import serialize_row, deserialize_row
from .constants import INTEGER, REAL
from .encoding import _encode_composite_key, _make_index_key


class DMLMixin:
    """DML methods (INSERT / UPDATE / DELETE) mixed into Database."""

    def insert(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
        meta   = self._meta(table)
        schema = meta.schema
        # Resolve AUTOINCREMENT columns: assign MAX(col)+1 when value is absent
        for col in schema.columns:
            if col.autoincrement and col.type == INTEGER and row.get(col.name) is None:
                max_val = 0
                for _, raw in self._table_btree(meta).scan():
                    v = deserialize_row(schema, raw).get(col.name)
                    if v is not None and int(v) > max_val:
                        max_val = int(v)
                row = {**row, col.name: max_val + 1}
        self._check_unique(meta, row)
        self._check_constraints(meta.schema, row)
        self._check_fk_child(meta.schema, row)
        rowid = meta.next_key
        meta.next_key += 1
        data  = serialize_row(meta.schema, row)
        self._table_btree(meta).insert(rowid, data)
        schema = meta.schema
        for idx_meta in self._indexes_for(table):
            vals = [row.get(n) for n in idx_meta.columns]
            if all(v is not None for v in vals):
                col_types = [next(c.type for c in schema.columns if c.name == n)
                             for n in idx_meta.columns]
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

        count = 0
        for rowid, raw in tree.scan():
            if limit is not None and count >= limit:
                break
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
            fks_ref = self._fks_referencing(table)
            if fks_ref:
                ref_cols_set = {c for fk in fks_ref for c in fk.ref_columns}
                if ref_cols_set & assignments.keys():
                    self._check_fk_parent(table, row, is_delete=False, new_row=new_row)
            updates[rowid] = serialize_row(schema, new_row)
            updated_rows.append(new_row)
            count += 1
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
        return updated_rows

    def delete(self, table: str, where: "WhereClause | None",
               limit: int | None = None) -> list[dict]:
        meta   = self._meta(table)
        schema = meta.schema
        tree   = self._table_btree(meta)
        idxs   = self._indexes_for(table)
        victims: list[tuple[int, dict]] = []
        count = 0
        for rowid, raw in tree.scan():
            if limit is not None and count >= limit:
                break
            row = deserialize_row(schema, raw)
            if not where or where.evaluate(row, self):
                victims.append((rowid, row))
                count += 1
        if not victims:
            return []
        for _, row in victims:
            self._check_fk_parent(table, row, is_delete=True)
        rowids = {r for r, _ in victims}
        tree.delete(rowids)
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
        return [row for _, row in victims]
