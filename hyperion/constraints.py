import re
from typing import Any

from .schema import Schema, ForeignKey, deserialize_row
from .constants import INTEGER, REAL


class ConstraintsMixin:
    """Constraint-checking methods mixed into Database."""

    def _check_unique(self, meta, row: dict[str, Any],
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
