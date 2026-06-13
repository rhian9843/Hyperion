import re
import struct
from typing import Any

from .errors import NoSuchColumnError
from .where import WhereClause

from .schema import deserialize_row
from .constants import TEXT
from .encoding import (
    _encode_index_key, _encode_composite_key,
    _make_index_key, _apply_order_limit, _composite_prefix_bounds,
)
from .expr import eval_expr, is_expr, _get_user_aggs
from .optimizer import find_eq_index, probe_index as _probe_index


_AGG_RE = re.compile(
    r"^(COUNT|MIN|MAX|SUM|AVG|GROUP_CONCAT|STRING_AGG)"
    r"\s*\(\s*(DISTINCT\s+)?(.+?)\s*\)$",
    re.IGNORECASE,
)

_NESTED_AGG_RE = re.compile(
    r'\b(COUNT|MIN|MAX|SUM|AVG)\s*\(\s*(DISTINCT\s+)?([^()]+?)\s*\)',
    re.IGNORECASE,
)

_USER_AGG_CALL_RE = re.compile(
    r'^(\w+)\(\s*(DISTINCT\s+)?(.+?)\s*\)$', re.IGNORECASE
)


def _parse_agg(col: str) -> tuple[str, str, bool] | None:
    """If col is an aggregate call, return (FUNC_UPPER, arg, distinct). Else None."""
    # Window functions look like AGG(...) OVER (...) — not a GROUP BY aggregate
    if re.search(r'\bOVER\s*\(', col, re.IGNORECASE):
        return None
    m = _AGG_RE.match(col)
    if m:
        return (m.group(1).upper(), m.group(3).strip(), bool(m.group(2)))
    # Check application-defined aggregates
    m2 = _USER_AGG_CALL_RE.match(col)
    if m2 and m2.group(1).upper() in _get_user_aggs():
        return (m2.group(1).upper(), m2.group(3).strip(), bool(m2.group(2)))
    return None


def _project_row(row: dict, columns: list[str]) -> dict:
    """Project a row to the requested columns, resolving qualified names and expressions."""
    result = {}
    for col in columns:
        if col in row:
            result[col] = row[col]
        elif "." in col:
            # table.col → try bare col, then alias-prefixed scan
            bare = col.split(".", 1)[1]
            if bare in row:
                result[col] = row[bare]
            else:
                matches = [v for k, v in row.items() if k.split(".")[-1] == bare]
                if matches:
                    result[col] = matches[0]
                else:
                    try:
                        result[col] = eval_expr(col, row)
                    except Exception:
                        raise NoSuchColumnError(f"Unknown column: '{col}'")
        else:
            # bare col → try table-qualified scan, then evaluate as expression/literal
            matches = [v for k, v in row.items() if k.split(".")[-1] == col]
            if matches:
                result[col] = matches[0]
            else:
                try:
                    result[col] = eval_expr(col, row)
                except Exception:
                    raise NoSuchColumnError(f"Unknown column: '{col}'")
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
        idx_satisfies     = False
        order_is_asc      = False
        order_is_desc     = False
        desc_via_rev_scan = False  # range scan already yielded DESC order
        if where:
            idx, eq_cols = self._find_index_for_where(table, where)
            idx_results: list[dict] | None = None
            if idx:
                idx_col = idx.columns[0] if len(idx.columns) == 1 else None
                # TEXT keys use 8-byte prefix encoding; strings sharing a prefix are in
                # rowid order within the same bucket, not lexicographic order.  Skipping
                # the sort for TEXT would give wrong results on prefix collisions.
                _idx_col_obj = next((c for c in meta.schema.columns if c.name == idx_col), None) if idx_col else None
                idx_col_sortable = _idx_col_obj is not None and _idx_col_obj.type != TEXT
                ob = order_by[0] if order_by and len(order_by) == 1 else None
                order_is_asc  = bool(ob and ob["col"] == idx_col and idx_col_sortable and not ob["desc"] and not ob.get("collate"))
                order_is_desc = bool(ob and ob["col"] == idx_col and idx_col_sortable and ob["desc"]     and not ob.get("collate"))
                idx_satisfies  = order_is_asc or order_is_desc or not order_by
                # Equality scan has no reverse path; DESC collects all then reverses
                # DISTINCT must collect all rows before dedup so early-stop is unsafe
                can_terminate = (order_is_asc or not order_by) and limit is not None and not distinct
                max_rows = ((limit or 0) + (offset or 0)) if can_terminate else None
                idx_results = self._index_select(meta, idx, eq_cols, columns, max_rows=max_rows)
            else:
                idx, range_cond = self._find_index_for_range(table, where)
                if idx:
                    idx_col = idx.columns[0] if len(idx.columns) == 1 else None
                    _idx_col_obj = next((c for c in meta.schema.columns if c.name == idx_col), None) if idx_col else None
                    idx_col_sortable = _idx_col_obj is not None and _idx_col_obj.type != TEXT
                    ob = order_by[0] if order_by and len(order_by) == 1 else None
                    order_is_asc  = bool(ob and ob["col"] == idx_col and idx_col_sortable and not ob["desc"] and not ob.get("collate"))
                    order_is_desc = bool(ob and ob["col"] == idx_col and idx_col_sortable and ob["desc"]     and not ob.get("collate"))
                    idx_satisfies  = order_is_asc or order_is_desc or not order_by
                    # Range scan supports reverse iteration; DESC can terminate early too
                    # DISTINCT must collect all rows before dedup so early-stop is unsafe
                    can_terminate = idx_satisfies and limit is not None and not distinct
                    max_rows = ((limit or 0) + (offset or 0)) if can_terminate else None
                    desc_via_rev_scan = order_is_desc
                    idx_results = self._index_range_select(
                        meta, idx, range_cond, where, columns, max_rows=max_rows,
                        reverse=order_is_desc)
                else:
                    # Prefix equality + range on a composite index
                    idx, eq_dict, range_cond = self._find_index_for_prefix_range(table, where)
                    if idx:
                        idx_results = self._index_prefix_range_select(
                            meta, idx, eq_dict, range_cond, where, columns)
            if idx_results is not None:
                if distinct:
                    seen_idx: set[tuple] = set()
                    deduped: list[dict] = []
                    for r in idx_results:
                        key = tuple(r.get(k) for k in (columns or list(r.keys())))
                        if key not in seen_idx:
                            seen_idx.add(key); deduped.append(r)
                    idx_results = deduped
                if idx_satisfies:
                    # Reverse only for equality+DESC (range DESC already came out reversed)
                    if order_is_desc and not desc_via_rev_scan:
                        idx_results.reverse()
                    if offset:
                        idx_results = idx_results[offset:]
                    if limit is not None:
                        idx_results = idx_results[:limit]
                    return idx_results
                return _apply_order_limit(idx_results, order_by, limit, offset)
        schema  = meta.schema
        results = []
        seen: set[tuple] = set()
        # Collect full rows first so ORDER BY can reference columns not in SELECT list.
        # Projection and DISTINCT dedup happen after sorting/limiting.
        _need_full = bool(order_by and columns)
        rls = meta.rls_enabled and not self._is_superuser
        if meta.storage_type == "column":
            for _, row in self._table_btree(meta).scan_rows():
                if where and not where.evaluate(row, self):
                    continue
                if rls and not self._rls_allowed(table, row):
                    continue
                results.append(row)
        else:
            for _, raw in self._table_btree(meta).scan():
                row = deserialize_row(schema, self._unpack_row_cell(raw))
                if where and not where.evaluate(row, self):
                    continue
                if rls and not self._rls_allowed(table, row):
                    continue
                results.append(row)
        results = _apply_order_limit(results, order_by, limit, offset)
        out = []
        for row in results:
            projected = _project_row(row, columns) if columns else row
            if distinct:
                key = tuple(projected.get(k) for k in (columns or list(row.keys())))
                if key in seen:
                    continue
                seen.add(key)
            out.append(projected)
        return out

    def _compute_aggregates(self, bucket_rows: list[dict],
                            columns: list[str]) -> dict[str, Any]:
        """Compute aggregate functions over bucket_rows for the given select columns."""
        result: dict[str, Any] = {}
        for col in columns:
            if col in result:
                continue
            agg = _parse_agg(col)
            if agg is None:
                # Defer columns containing nested aggregates to the second pass
                if _NESTED_AGG_RE.search(col):
                    continue
                _first = bucket_rows[0] if bucket_rows else {}
                _v = _first.get(col)
                if _v is None and col not in _first:
                    try:
                        _v = eval_expr(col, _first)
                    except Exception:
                        pass
                result[col] = _v
                continue
            func, arg, distinct = agg
            if func == "COUNT":
                if arg == "*":
                    result[col] = len(bucket_rows)
                else:
                    vals = [r.get(arg) for r in bucket_rows if r.get(arg) is not None]
                    if distinct:
                        vals = list(dict.fromkeys(vals))
                    result[col] = len(vals)
            elif func in ("GROUP_CONCAT", "STRING_AGG"):
                # Extract optional ORDER BY clause before parsing col/sep
                _gc_order_col: str | None = None
                _gc_order_desc = False
                _gc_arg = arg
                _m_gc_ob = re.search(
                    r'\bORDER\s+BY\s+(.+?)(?:\s+(ASC|DESC))?\s*$',
                    arg, re.IGNORECASE)
                if _m_gc_ob:
                    _gc_order_col = _m_gc_ob.group(1).strip()
                    _gc_order_desc = (_m_gc_ob.group(2) or "ASC").upper() == "DESC"
                    _gc_arg = arg[:_m_gc_ob.start()].rstrip().rstrip(",").rstrip()
                parts = [p.strip() for p in _gc_arg.split(",", 1)]
                col_name = parts[0]
                if len(parts) > 1:
                    sep_raw = parts[1].strip()
                    sep = sep_raw[1:-1] if (sep_raw.startswith("'")
                                            and sep_raw.endswith("'")) else sep_raw
                else:
                    sep = ","
                _gc_rows = bucket_rows
                if _gc_order_col:
                    def _gc_key(r: dict):
                        v = r.get(_gc_order_col)  # type: ignore[arg-type]
                        if v is None:
                            try:
                                v = eval_expr(_gc_order_col, r)  # type: ignore[arg-type]
                            except Exception:
                                pass
                        return (v is None, v)
                    _gc_rows = sorted(bucket_rows, key=_gc_key, reverse=_gc_order_desc)
                def _gc_val(r: dict) -> str | None:
                    v = r.get(col_name)
                    if v is None and col_name not in r:
                        try:
                            v = eval_expr(col_name, r)
                        except Exception:
                            return None
                    return str(v) if v is not None else None
                str_vals = [s for r in _gc_rows if (s := _gc_val(r)) is not None]
                if distinct:
                    str_vals = list(dict.fromkeys(str_vals))
                result[col] = sep.join(str_vals) if str_vals else None
            elif func in _get_user_aggs():
                _, agg_class = _get_user_aggs()[func]
                agg_obj = agg_class()
                for r in bucket_rows:
                    v = eval_expr(arg, r) if is_expr(arg) else r.get(arg)
                    agg_obj.step(v)
                result[col] = agg_obj.finalize()
            else:
                def _agg_val(r: dict, a: str):
                    if a in r:
                        return r[a]
                    try:
                        return eval_expr(a, r)
                    except Exception:
                        return None
                vals = [v for r in bucket_rows
                        if (v := _agg_val(r, arg)) is not None]
                if distinct:
                    vals = list(dict.fromkeys(vals))
                if not vals:
                    result[col] = None
                elif func == "MIN":  result[col] = min(vals)
                elif func == "MAX":  result[col] = max(vals)
                elif func == "SUM":  result[col] = sum(vals)
                elif func == "AVG":  result[col] = sum(vals) / len(vals)

        # Second pass: evaluate wrapper expressions containing nested aggregates
        # e.g. COALESCE(SUM(col), 0), ROUND(AVG(col), 2)
        for col in columns:
            if col in result:
                continue
            if not _NESTED_AGG_RE.search(col):
                continue
            def _agg_val2(r: dict, a: str):
                if a in r:
                    return r[a]
                if "." in a:
                    bare = a.split(".")[-1]
                    if bare in r:
                        return r[bare]
                    matches = [v for k, v in r.items() if k.split(".")[-1] == bare]
                    if matches:
                        return matches[0]
                try:
                    v = eval_expr(a, r)
                    return None if (isinstance(v, str) and v == a) else v
                except Exception:
                    return None
            def _replace_agg(m: re.Match) -> str:
                func2 = m.group(1).upper()
                arg2  = m.group(3).strip()
                raw2 = [v for r in bucket_rows
                        if (v := _agg_val2(r, arg2)) is not None]
                try:
                    vals2 = [float(v) for v in raw2]
                except (TypeError, ValueError):
                    vals2 = raw2
                if func2 == "COUNT": v2 = len(bucket_rows) if arg2 == "*" else len(vals2)
                elif func2 == "SUM": v2 = sum(vals2) if vals2 else None
                elif func2 == "MIN": v2 = min(vals2) if vals2 else None
                elif func2 == "MAX": v2 = max(vals2) if vals2 else None
                elif func2 == "AVG": v2 = sum(vals2) / len(vals2) if vals2 else None
                else: v2 = None
                return "NULL" if v2 is None else repr(v2)
            expanded = _NESTED_AGG_RE.sub(_replace_agg, col)
            try:
                result[col] = eval_expr(expanded, {})
            except Exception:
                result[col] = None

        return result

    def _aggregate_select(self, meta, columns: list[str],
                          where: "WhereClause | None") -> list[dict[str, Any]]:
        if meta.storage_type == "column":
            row_filter = ((lambda row, _w=where, _db=self: _w.evaluate(row, _db))
                          if where else None)
            result = self._table_btree(meta).scan_aggregate(columns, row_filter)
            if result is not None:
                return result

        schema = meta.schema
        rows: list[dict] = []
        if meta.storage_type == "column":
            for _, row in self._table_btree(meta).scan_rows():
                if where and not where.evaluate(row, self):
                    continue
                rows.append(row)
        else:
            for _, raw in self._table_btree(meta).scan():
                row = deserialize_row(schema, self._unpack_row_cell(raw))
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

        # Columnar GROUP BY pushdown: reads only GROUP BY + aggregate columns.
        if meta.storage_type == "column" and not where:
            select_cols = columns if columns else group_by
            pushed = self._table_btree(meta).scan_aggregate_grouped(
                group_by, select_cols)
            if pushed is not None:
                if having:
                    pushed = [r for r in pushed if having.evaluate(r, self)]
                return _apply_order_limit(pushed, order_by, limit, offset)

        all_rows: list[dict] = []
        if meta.storage_type == "column":
            for _, row in self._table_btree(meta).scan_rows():
                if where and not where.evaluate(row, self):
                    continue
                all_rows.append(row)
        else:
            for _, raw in self._table_btree(meta).scan():
                row = deserialize_row(schema, self._unpack_row_cell(raw))
                if where and not where.evaluate(row, self):
                    continue
                all_rows.append(row)
        def _gb_val(c: str, r: dict) -> Any:
            if c in r:
                return r[c]
            try:
                return eval_expr(c, r)
            except Exception:
                return None

        buckets: dict[tuple, list[dict]] = {}
        for row in all_rows:
            key = tuple(_gb_val(c, row) for c in group_by)
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
             offset: int | None = None,
             on_clause: "WhereClause | None" = None) -> list[dict[str, Any]]:
        lmeta, rmeta = self._meta(left_table), self._meta(right_table)
        if lmeta.storage_type == "column":
            left_rows = [row for _, row in self._table_btree(lmeta).scan_rows()]
        else:
            left_rows = [deserialize_row(lmeta.schema, self._unpack_row_cell(r))
                         for _, r in self._table_btree(lmeta).scan()]
        if rmeta.storage_type == "column":
            right_rows = [row for _, row in self._table_btree(rmeta).scan_rows()]
        else:
            right_rows = [deserialize_row(rmeta.schema, self._unpack_row_cell(r))
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
            return _project_row(merged, columns) if columns else merged

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

        # Determine effective match function
        if on_clause is not None and on_left is None:
            # Multi-condition ON: evaluate WhereClause on merged row
            use_inlj = False
            lcol = rcol = None
        else:
            raw_left  = on_left.split(".")[-1]   # type: ignore[union-attr]
            raw_right = on_right.split(".")[-1]  # type: ignore[union-attr]
            # Resolve which bare column belongs to which side by checking alias prefix.
            # ON may be written as "right_alias.col = left_alias.col" — detect the swap.
            left_prefix  = on_left.split(".")[0]  if "." in on_left  else ""  # type: ignore
            on_left_is_right = left_prefix in (ra, right_table)
            if on_left_is_right:
                lcol, rcol = raw_right, raw_left   # swapped: on_left refers to right table
            else:
                lcol, rcol = raw_left, raw_right
            use_inlj = (join_type in ("INNER", "LEFT", "LEFT OUTER")
                        and find_eq_index(self, right_table, rcol) is not None)

        def _on_match(lr: dict, rr: dict) -> bool:
            if on_clause is not None and on_left is None:
                return on_clause.evaluate(_merge(lr, rr), self)
            return lr.get(lcol) == rr.get(rcol)  # type: ignore[arg-type]

        if use_inlj:
            for lr in left_rows:
                val = lr.get(lcol)  # type: ignore[arg-type]
                if val is None:
                    if join_type in ("LEFT", "LEFT OUTER"):
                        # NULL key: no right-side match possible — emit null row
                        merged = {f"{la}.{k}": v for k, v in lr.items()}
                        merged.update(right_null)
                        row = _emit(merged)
                        if row is not None:
                            results.append(row)
                    continue
                probed = _probe_index(self, right_table, rcol, val)  # type: ignore[arg-type]
                if not probed:
                    if join_type in ("LEFT", "LEFT OUTER"):
                        merged = {f"{la}.{k}": v for k, v in lr.items()}
                        merged.update(right_null)
                        row = _emit(merged)
                        if row is not None:
                            results.append(row)
                    continue
                for rr in probed:
                    row = _emit(_merge(lr, rr))
                    if row is not None:
                        results.append(row)
        else:
            matched_right: set[int] = set()
            for lr in left_rows:
                on_matched = False
                for j, rr in enumerate(right_rows):
                    if not _on_match(lr, rr):
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
                      eq_cols: dict[str, str], columns: list[str] | None,
                      max_rows: int | None = None) -> list[dict[str, Any]]:
        schema    = meta.schema
        col_types = []
        for col_name in idx_meta.columns:
            col_obj = next((c for c in schema.columns if c.name == col_name), None)
            if col_obj is None:
                if is_expr(col_name):
                    col_types.append(TEXT)  # expression index: stored as TEXT
                else:
                    return []
            else:
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
            row = deserialize_row(schema, self._unpack_row_cell(raw))
            match = True
            for col_name, val in eq_cols.items():
                if is_expr(col_name):
                    actual   = eval_expr(col_name, row)
                    col_type = TEXT
                else:
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
                if max_rows is not None and len(results) >= max_rows:
                    break
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
                        if col_obj:
                            return m, cond
            cond = cond.and_clause
        return None, None

    def _index_range_select(self, meta, idx_meta, range_cond,
                            where: "WhereClause | None",
                            columns: list[str] | None,
                            max_rows: int | None = None,
                            reverse: bool = False) -> list[dict[str, Any]]:
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
        is_text    = col_obj.type == TEXT

        op = range_cond.op
        if op == ">=":
            lo, hi = _make_index_key(val_key, 0),              _MAX_KEY
        elif op == ">":
            if is_text:
                # Strings longer than 8 bytes that share the boundary's prefix
                # map to the same val_key but are lexicographically greater;
                # include the full same-prefix range and let post-filter exclude
                # entries that are not actually > val.
                lo, hi = _make_index_key(val_key, 0),          _MAX_KEY
            else:
                lo, hi = _make_index_key(val_key, _MAX_ROWID) + 1, _MAX_KEY
        elif op == "<=":
            lo, hi = 0,                                         _make_index_key(val_key, _MAX_ROWID)
        else:  # "<"
            lo, hi = 0,                                         _make_index_key(val_key, 0) - 1

        itree   = self._index_btree(idx_meta)
        ptree   = self._table_btree(meta)
        results: list[dict] = []
        scan_fn = itree.scan_range_reverse if reverse else itree.scan_range
        for _, rowid_raw in scan_fn(lo, hi):
            rowid = struct.unpack("q", rowid_raw)[0]
            raw   = ptree.lookup(rowid)
            if raw is None:
                continue
            row = deserialize_row(schema, self._unpack_row_cell(raw))
            if where and not where.evaluate(row, self):
                continue
            results.append(_project_row(row, columns) if columns else row)
            if max_rows is not None and len(results) >= max_rows:
                break
        return results

    def _find_index_for_prefix_range(self, table: str,
                                     where: "WhereClause | None") -> tuple:
        """Return (IndexMeta, eq_dict, range_cond) for prefix-equality + range on a composite index.

        Matches WHERE clauses of the form:
            col_0 = V_0 [AND col_1 = V_1 ...] AND col_N op range_val

        where col_0 … col_N-1 are the leading columns of a multi-column index
        with equality conditions and col_N is the next index column with a range op.
        Returns (None, {}, None) when no suitable index is found.
        """
        if not where or where.or_clause is not None:
            return None, {}, None
        eq: dict[str, str] = {}
        range_cond = None
        cond = where
        while cond is not None:
            if cond.or_clause is not None:
                return None, {}, None
            if cond.op == "=" and range_cond is None:
                eq[cond.col] = cond.val
            elif cond.op in self._RANGE_OPS and range_cond is None and cond.op != "BETWEEN":
                range_cond = cond
            else:
                return None, {}, None
            cond = cond.and_clause
        if range_cond is None or not eq:
            return None, {}, None
        for m in self._catalog.indexes.values():
            if m.table_name != table or len(m.columns) < len(eq) + 1:
                continue
            prefix = m.columns[:len(eq)]
            if set(prefix) != set(eq.keys()):
                continue
            if m.columns[len(eq)] == range_cond.col:
                return m, eq, range_cond
        return None, {}, None

    def _index_prefix_range_select(self, meta, idx_meta, eq_dict: dict,
                                   range_cond, where: "WhereClause | None",
                                   columns: list[str] | None,
                                   max_rows: int | None = None) -> list[dict[str, Any]]:
        """Scan a composite index for prefix-equality + range rows.

        Uses _composite_prefix_bounds to compute conservative scan bounds,
        then post-filters every returned row with the full WHERE predicate.
        """
        schema = meta.schema

        prefix_cols = idx_meta.columns[:len(eq_dict)]
        range_col   = idx_meta.columns[len(eq_dict)]
        n_total     = len(idx_meta.columns)

        def _col_type(name):
            col_obj = next((c for c in schema.columns if c.name == name), None)
            return col_obj.type if col_obj else TEXT

        eq_types    = [_col_type(c) for c in prefix_cols]
        range_type  = _col_type(range_col)
        # Values for prefix columns in index-column order
        eq_vals     = [eq_dict[c] for c in prefix_cols]

        try:
            lo_key, hi_key = _composite_prefix_bounds(
                eq_vals, eq_types, range_cond.val, range_type,
                range_cond.op, n_total,
            )
        except (ValueError, TypeError):
            return []

        lo = _make_index_key(lo_key, 0)
        hi = _make_index_key(hi_key, 0xFFFFFFFFFFFFFFFF)

        itree   = self._index_btree(idx_meta)
        ptree   = self._table_btree(meta)
        results: list[dict] = []
        for _, rowid_raw in itree.scan_range(lo, hi):
            rowid = struct.unpack("q", rowid_raw)[0]
            raw   = ptree.lookup(rowid)
            if raw is None:
                continue
            row = deserialize_row(schema, self._unpack_row_cell(raw))
            if where and not where.evaluate(row, self):
                continue
            results.append(_project_row(row, columns) if columns else row)
            if max_rows is not None and len(results) >= max_rows:
                break
        return results
