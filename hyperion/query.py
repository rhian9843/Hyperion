import re
import struct
from typing import Any

from .schema import deserialize_row
from .constants import TEXT
from .encoding import (
    _encode_index_key, _encode_composite_key,
    _make_index_key, _apply_order_limit,
)


def _parse_agg(col: str) -> tuple[str, str] | None:
    """If col is an aggregate call like MIN(id), return (FUNC_UPPER, arg). Else None."""
    _AGG_RE = re.compile(r"^(COUNT|MIN|MAX|SUM|AVG)\(([^)]*)\)$", re.IGNORECASE)
    m = _AGG_RE.match(col)
    return (m.group(1).upper(), m.group(2).strip()) if m else None


def _project_row(row: dict, columns: list[str]) -> dict:
    """Project a row to the requested columns, resolving table-qualified names (t.col → col)."""
    result = {}
    for col in columns:
        if col in row:
            result[col] = row[col]
        elif "." in col:
            bare = col.split(".", 1)[1]
            if bare in row:
                result[col] = row[bare]
            else:
                raise RuntimeError(f"Unknown column: '{col}'")
        else:
            raise RuntimeError(f"Unknown column: '{col}'")
    return result


class QueryMixin:
    """SELECT and JOIN methods mixed into Database."""

    _RANGE_OPS = {">", ">=", "<", "<="}

    def select(self, table: str, columns: list[str] | None,
               where: "WhereClause | None",
               order_by: list[dict] | None = None,
               limit: int | None = None,
               group_by: list[str] | None = None,
               having: "WhereClause | None" = None,
               distinct: bool = False,
               offset: int | None = None) -> list[dict[str, Any]]:
        meta = self._meta(table)
        if group_by:
            return self._group_by_select(meta, columns, where, group_by, having,
                                         order_by, limit, offset)
        if columns and any(_parse_agg(c) is not None for c in columns):
            return self._aggregate_select(meta, columns, where)
        if where and not order_by and limit is None and not distinct:
            idx, eq_cols = self._find_index_for_where(table, where)
            if idx:
                return self._index_select(meta, idx, eq_cols, columns)
            idx, range_cond = self._find_index_for_range(table, where)
            if idx:
                return self._index_range_select(meta, idx, range_cond, where, columns)
        schema  = meta.schema
        results = []
        seen: set[tuple] = set()
        for _, raw in self._table_btree(meta).scan():
            row = deserialize_row(schema, raw)
            if where and not where.evaluate(row, self):
                continue
            projected = _project_row(row, columns) if columns else row
            if distinct:
                key = tuple(projected.get(k) for k in (columns or list(row.keys())))
                if key in seen:
                    continue
                seen.add(key)
            results.append(projected)
        return _apply_order_limit(results, order_by, limit, offset)

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

    def _aggregate_select(self, meta, columns: list[str],
                          where: "WhereClause | None") -> list[dict[str, Any]]:
        schema = meta.schema
        rows: list[dict] = []
        for _, raw in self._table_btree(meta).scan():
            row = deserialize_row(schema, raw)
            if where and not where.evaluate(row, self):
                continue
            rows.append(row)
        return [self._compute_aggregates(rows, columns)]

    def _group_by_select(self, meta, columns: list[str] | None,
                         where: "WhereClause | None", group_by: list[str],
                         having: "WhereClause | None",
                         order_by: list[dict] | None,
                         limit: int | None,
                         offset: int | None = None) -> list[dict[str, Any]]:
        schema = meta.schema
        all_rows: list[dict] = []
        for _, raw in self._table_btree(meta).scan():
            row = deserialize_row(schema, raw)
            if where and not where.evaluate(row, self):
                continue
            all_rows.append(row)
        buckets: dict[tuple, list[dict]] = {}
        for row in all_rows:
            key = tuple(row.get(c) for c in group_by)
            if key not in buckets:
                buckets[key] = []
            buckets[key].append(row)
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
        return _apply_order_limit(results, order_by, limit, offset)

    def join(self, left_table: str, right_table: str,
             on_left: str | None, on_right: str | None,
             columns: list[str] | None,
             where: "WhereClause | None",
             order_by: list[dict] | None = None,
             limit: int | None = None,
             join_type: str = "INNER",
             left_alias: str | None = None,
             right_alias: str | None = None,
             offset: int | None = None) -> list[dict[str, Any]]:
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

        if join_type == "CROSS":
            for lr in left_rows:
                for rr in right_rows:
                    row = _emit(_merge(lr, rr))
                    if row is not None:
                        results.append(row)
            return _apply_order_limit(results, order_by, limit, offset)

        if join_type == "NATURAL":
            lcols  = {c.name for c in lmeta.schema.columns}
            rcols  = {c.name for c in rmeta.schema.columns}
            shared = sorted(lcols & rcols)
            for lr in left_rows:
                for rr in right_rows:
                    if any(lr.get(c) is None or rr.get(c) is None
                           or lr.get(c) != rr.get(c) for c in shared):
                        continue
                    row = _emit(_merge(lr, rr))
                    if row is not None:
                        results.append(row)
            return _apply_order_limit(results, order_by, limit)

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
            if not on_matched and join_type in ("LEFT", "FULL"):
                merged = {f"{la}.{k}": v for k, v in lr.items()}
                merged.update(right_null)
                row = _emit(merged)
                if row is not None:
                    results.append(row)

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

    # ── Index-accelerated select ───────────────────────────────────────────────

    def _index_select(self, meta, idx_meta,
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
            match = True
            for col_name, val in eq_cols.items():
                col_type = next(c.type for c in schema.columns if c.name == col_name)
                actual   = row.get(col_name)
                if col_type == TEXT:
                    if str(actual) != str(val):
                        match = False; break
                elif col_type == "INTEGER":
                    try:
                        if actual != int(val): match = False; break
                    except (ValueError, TypeError):
                        match = False; break
                elif col_type == "REAL":
                    try:
                        if actual != float(val): match = False; break
                    except (ValueError, TypeError):
                        match = False; break
            if match:
                results.append(_project_row(row, columns) if columns else row)
        return results

    def _find_index_for_where(self, table: str,
                              where: "WhereClause | None"
                              ) -> tuple:
        """Return (IndexMeta, eq_dict) if a pure AND-equality where chain matches an index."""
        if not where or where.or_clause is not None:
            return None, {}
        eq: dict[str, str] = {}
        cond = where
        while cond is not None:
            if cond.op != "=" or cond.or_clause is not None:
                return None, {}
            eq[cond.col] = cond.val
            cond = cond.and_clause
        for m in self._catalog.indexes.values():
            if m.table_name == table and set(m.columns) == set(eq.keys()):
                return m, eq
        return None, {}

    def _find_index_for_range(self, table: str, where: "WhereClause | None") -> tuple:
        """Return (IndexMeta, range_condition) for a range op on a single-column index."""
        if not where or where.or_clause is not None:
            return None, None
        meta = self._meta(table)
        cond = where
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

    def _index_range_select(self, meta, idx_meta, range_cond,
                            where: "WhereClause | None",
                            columns: list[str] | None) -> list[dict[str, Any]]:
        """Scan an index for range_cond entries, then post-filter with full WHERE."""
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
            lo, hi = _make_index_key(val_key, 0),              _MAX_KEY
        elif op == ">":
            lo, hi = _make_index_key(val_key, _MAX_ROWID) + 1, _MAX_KEY
        elif op == "<=":
            lo, hi = 0,                                         _make_index_key(val_key, _MAX_ROWID)
        else:  # "<"
            lo, hi = 0,                                         _make_index_key(val_key, 0) - 1

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
