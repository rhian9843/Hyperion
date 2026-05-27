import re
import struct
from typing import Any

from .schema import Schema, ForeignKey, deserialize_row, serialize_row
from .constants import INTEGER, REAL
from .encoding import _encode_composite_key, _make_index_key


class ConstraintsMixin:
    """Constraint-checking methods mixed into Database."""

    def _check_unique(self, meta, row: dict[str, Any],
                      exclude_rowid: int | None = None) -> None:
        """Raise if any UNIQUE column or multi-column unique constraint is violated."""
        schema      = meta.schema
        unique_cols = [c for c in schema.columns if c.unique]
        mc_unique   = schema.unique_constraints  # list[list[str]]
        if not unique_cols and not mc_unique:
            return
        # Coerce single-column UNIQUE values to their storage types for comparison
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
            for uc_cols in mc_unique:
                new_vals = []
                for c in uc_cols:
                    v = row.get(c)
                    col_obj = next((col for col in schema.columns if col.name == c), None)
                    if v is not None and col_obj:
                        if col_obj.type == INTEGER:
                            try: v = int(v)
                            except (ValueError, TypeError): pass
                        elif col_obj.type == REAL:
                            try: v = float(v)
                            except (ValueError, TypeError): pass
                    new_vals.append(v)
                if any(v is None for v in new_vals):
                    continue  # NULL exempts from multi-col unique
                ex_vals = [existing.get(c) for c in uc_cols]
                if new_vals == ex_vals:
                    raise RuntimeError(
                        f"UNIQUE constraint failed: "
                        f"{schema.name}({', '.join(uc_cols)})"
                    )

    def _check_constraints(self, schema: Schema, row: dict[str, Any]) -> None:
        """Raise if any column CHECK expression evaluates to False for the given row."""
        from .parser import _tokenize, _parse_one_condition
        for col in schema.columns:
            if col.check is None:
                continue
            tokens = _tokenize(col.check)
            try:
                wc, _ = _parse_one_condition(tokens, 0)
            except Exception as e:
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

    def _fk_index_lookup(self, parent_meta, ref_cols: list[str],
                          vals: list) -> bool | None:
        """Return True/False if a suitable index exists, None if no index found."""
        parent_schema = parent_meta.schema
        idx_meta = None
        for m in self._catalog.indexes.values():
            if (m.table_name == parent_schema.name
                    and m.columns == list(ref_cols)):
                idx_meta = m
                break
        if idx_meta is None:
            return None
        col_types = []
        for col_name in idx_meta.columns:
            col_obj = next((c for c in parent_schema.columns
                            if c.name == col_name), None)
            if col_obj is None:
                return None
            col_types.append(col_obj.type)
        try:
            val_key = _encode_composite_key(vals, col_types)
        except (ValueError, TypeError):
            return None
        lo = _make_index_key(val_key, 0)
        hi = _make_index_key(val_key, 0xFFFFFFFFFFFFFFFF)
        for _ in self._index_btree(idx_meta).scan_range(lo, hi):
            return True
        return False

    def _check_fk_child(self, schema: Schema, row: dict[str, Any]) -> None:
        """Raise if any FK column values are not present in the referenced parent table."""
        if not getattr(self, "fk_enforcement", True):
            return
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
            found = self._fk_index_lookup(parent_meta, fk.ref_columns, vals)
            if found is None:  # no usable index — fall back to full scan
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

    def _check_fk_parent(self, table: str, old_row: dict[str, Any],
                          is_delete: bool = False,
                          new_row: dict[str, Any] | None = None) -> None:
        """Enforce FK constraints when a parent row is modified or deleted.

        On DELETE: applies fk.on_delete (RESTRICT, CASCADE, SET NULL, NO ACTION).
        On UPDATE: applies fk.on_update; for CASCADE, propagates new ref values to children.
        """
        if not getattr(self, "fk_enforcement", True):
            return
        for tname, tmeta in self.tables.items():
            for fk in tmeta.schema.foreign_keys:
                if fk.ref_table.lower() != table.lower():
                    continue
                ref_vals = [old_row.get(c) for c in fk.ref_columns]
                if any(v is None for v in ref_vals):
                    continue
                child_schema = tmeta.schema
                matching: list[tuple[int, dict]] = []
                for rowid, raw in self._table_btree(tmeta).scan():
                    child_row = deserialize_row(child_schema, raw)
                    if all(child_row.get(cc) == rv
                           for cc, rv in zip(fk.columns, ref_vals)):
                        matching.append((rowid, child_row))
                if not matching:
                    continue
                action = fk.on_delete if is_delete else fk.on_update
                if action in ("RESTRICT", "NO ACTION"):
                    raise RuntimeError(
                        f"FOREIGN KEY constraint failed: cannot modify '{table}' "
                        f"— row is referenced by '{tname}'"
                    )
                elif action == "CASCADE":
                    if is_delete:
                        victim_ids = {r for r, _ in matching}
                        self._table_btree(tmeta).delete(victim_ids)
                        for im in self._indexes_for(tname):
                            col_types = [next(c.type for c in child_schema.columns
                                             if c.name == n)
                                         for n in im.columns]
                            idx_keys: set[int] = set()
                            for rowid, child_row in matching:
                                vals = [child_row.get(n) for n in im.columns]
                                if all(v is not None for v in vals):
                                    idx_keys.add(_make_index_key(
                                        _encode_composite_key(vals, col_types), rowid))
                            self._index_btree(im).delete(idx_keys)
                    else:
                        # ON UPDATE CASCADE: propagate new ref column values to children
                        if new_row is not None:
                            new_ref_vals = [new_row.get(c) for c in fk.ref_columns]
                            upd: dict[int, bytes] = {}
                            for rowid, child_row in matching:
                                updated = dict(child_row)
                                for cc, nv in zip(fk.columns, new_ref_vals):
                                    updated[cc] = nv
                                upd[rowid] = serialize_row(child_schema, updated)
                            self._table_btree(tmeta).update(upd)
                elif action == "SET NULL":
                    upd_null: dict[int, bytes] = {}
                    for rowid, child_row in matching:
                        updated = dict(child_row)
                        for cc in fk.columns:
                            updated[cc] = None
                        upd_null[rowid] = serialize_row(child_schema, updated)
                    self._table_btree(tmeta).update(upd_null)
