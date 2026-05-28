import re
import struct
import time
from collections import defaultdict
from typing import Any


class QueryTimeoutError(RuntimeError):
    """Raised when a query exceeds its allotted execution time."""


class ReadOnlyError(RuntimeError):
    """Raised when a write operation is attempted on a read-only Database."""


_WRITE_OPS = frozenset({
    "INSERT", "INSERT_SELECT", "UPDATE", "DELETE", "TRUNCATE",
    "CREATE_TABLE", "CREATE_TABLE_AS_SELECT", "DROP_TABLE",
    "CREATE_INDEX", "DROP_INDEX",
    "CREATE_VIEW", "DROP_VIEW",
    "CREATE_TRIGGER", "DROP_TRIGGER",
    "ALTER_ADD_COLUMN", "ALTER_DROP_COLUMN",
    "ALTER_RENAME_COLUMN", "ALTER_RENAME_TABLE",
    "ANALYZE", "VACUUM",
})


def _check_timeout(db: "Database") -> None:
    deadline = getattr(db, "_query_deadline", None)
    if deadline is not None and time.monotonic() > deadline:
        raise QueryTimeoutError("Query timed out")

from .database import Database
from .encoding import _apply_set_op, _apply_order_limit, _encode_composite_key, _make_index_key
from .expr import eval_expr, is_expr
from .json_funcs import json_each_rows as _json_each_rows
from .introspect import (hyperion_master_rows as _hyperion_master_rows,
                         integrity_check as _integrity_check,
                         explain_plan as _explain_plan)
from .optimizer import find_eq_index as _find_eq_index, probe_index as _probe_index, optimize_join
from .parser import _parse_tokens, _tokenize
from .schema import deserialize_row, serialize_row
from .constants import INTEGER, REAL, TEXT, DEFAULT_TEXT_SIZE
from .query import _project_row, _parse_agg as _q_parse_agg
from .triggers import (fire_triggers, has_triggers, has_instead_of,
                       scan_matching_rows, apply_update_row)
from .where import _instantiate_correlated

def _is_single_string_literal(val: str) -> bool:
    """True iff val is exactly one single-quoted SQL string (not a concat expression)."""
    if not (val.startswith("'") and val.endswith("'") and len(val) >= 2):
        return False
    i = 1
    while i < len(val) - 1:
        if val[i] == "'":
            if i + 1 < len(val) - 1 and val[i + 1] == "'":
                i += 2  # escaped ''
            else:
                return False  # quote closes before end → compound expression
        else:
            i += 1
    return True


# ── Window function helpers ────────────────────────────────────────────────────

_WINDOW_RE       = re.compile(r'\bOVER\s*\(',      re.IGNORECASE)
_WINDOW_NAMED_RE = re.compile(r'\bOVER\s+(\w+)\s*$', re.IGNORECASE)


def _parse_bound(s: str) -> tuple:
    s = s.strip().upper()
    if s == "UNBOUNDED PRECEDING": return ("UNBOUNDED", "PRECEDING")
    if s == "UNBOUNDED FOLLOWING": return ("UNBOUNDED", "FOLLOWING")
    if s == "CURRENT ROW":         return ("CURRENT",   "ROW")
    m = re.match(r'(\d+)\s+(PRECEDING|FOLLOWING)', s)
    if m: return (int(m.group(1)), m.group(2))
    return ("UNBOUNDED", "PRECEDING")


def _parse_frame_spec(text: str) -> dict:
    """Parse ROWS/RANGE BETWEEN X AND Y (or ROWS/RANGE X)."""
    uc = text.strip().upper()
    mode_m = re.match(r'(ROWS|RANGE|GROUPS)\s+', uc)
    mode = mode_m.group(1) if mode_m else "ROWS"
    rest = uc[mode_m.end():] if mode_m else uc
    if rest.startswith("BETWEEN"):
        rest = rest[len("BETWEEN"):].strip()
        and_pos = re.search(r'\bAND\b', rest)
        if and_pos:
            lo_str = rest[:and_pos.start()].strip()
            hi_str = rest[and_pos.end():].strip()
        else:
            lo_str = rest; hi_str = "CURRENT ROW"
    else:
        lo_str = rest; hi_str = "CURRENT ROW"
    return {"mode": mode, "lo": _parse_bound(lo_str), "hi": _parse_bound(hi_str)}


def _frame_slice(indices: list[int], pos: int, frame: dict) -> list[int]:
    """Return indices within the frame for the row at sorted position `pos`."""
    n = len(indices)

    def _to_pos(spec: tuple) -> int:
        kind = spec[0]
        if kind == "UNBOUNDED": return 0 if spec[1] == "PRECEDING" else n - 1
        if kind == "CURRENT":   return pos
        offset = kind  # numeric
        return max(0, pos - offset) if spec[1] == "PRECEDING" else min(n - 1, pos + offset)

    lo = _to_pos(frame["lo"])
    hi = _to_pos(frame["hi"])
    return indices[lo: hi + 1]


def _parse_window_col(expr: str) -> dict | None:
    """Parse 'fn(args) OVER (PARTITION BY … ORDER BY … [frame])'.
    Returns None if not a window expr."""
    m = _WINDOW_RE.search(expr)
    if not m:
        return None
    fn_part = expr[:m.start()].strip()
    rest = expr[m.end():]
    depth = 1; end = len(rest)
    for j, ch in enumerate(rest):
        if ch == "(":   depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0: end = j; break
    over_content = rest[:end].strip()

    fn_m = re.match(r'(\w+)\s*\(([^)]*)\)', fn_part)
    if not fn_m:
        return None
    fn_name  = fn_m.group(1).upper()
    fn_args  = [a.strip() for a in fn_m.group(2).split(",") if a.strip()]

    partition_by: list[str] = []
    order_by:     list[dict] = []
    frame:        dict | None = None
    uc = over_content.upper()
    pb_m = re.search(r'\bPARTITION\s+BY\b', uc)
    ob_m = re.search(r'\bORDER\s+BY\b',     uc)
    if pb_m:
        pb_end = ob_m.start() if ob_m else len(over_content)
        partition_by = [c.strip() for c in over_content[pb_m.end():pb_end].split(",") if c.strip()]
    if ob_m:
        ob_content = over_content[ob_m.end():]
        uc_ob = ob_content.upper()
        frame_m = re.search(r'\b(ROWS|RANGE|GROUPS)\b', uc_ob)
        ob_str = ob_content[:frame_m.start()].strip() if frame_m else ob_content.strip()
        if frame_m:
            frame = _parse_frame_spec(ob_content[frame_m.start():])
        for spec in ob_str.split(","):
            parts = spec.strip().split()
            if parts:
                desc = len(parts) > 1 and parts[1].upper() == "DESC"
                order_by.append({"col": parts[0], "desc": desc})
    return {"fn": fn_name, "args": fn_args,
            "partition_by": partition_by, "order_by": order_by, "frame": frame}


def _get_col_val(row: dict, col: str) -> Any:
    if col in row:
        return row[col]
    if "." in col:
        bare = col.split(".", 1)[1]
        if bare in row:
            return row[bare]
    return None


def _apply_one_window(rows: list[dict], col: str, wf: dict) -> None:
    fn          = wf["fn"]
    fn_args     = wf["args"]
    part_by     = wf["partition_by"]
    ob_spec     = wf["order_by"]

    groups: defaultdict[tuple, list[int]] = defaultdict(list)
    for i, row in enumerate(rows):
        pk = tuple(_get_col_val(row, p) for p in part_by) if part_by else ((),)
        groups[pk].append(i)

    for _, indices in groups.items():
        if ob_spec:
            for ob in reversed(ob_spec):
                c = ob["col"]
                non_null = [i for i in indices if _get_col_val(rows[i], c) is not None]
                null_i   = [i for i in indices if _get_col_val(rows[i], c) is None]
                try:
                    non_null.sort(key=lambda i, c=c: _get_col_val(rows[i], c),
                                  reverse=ob["desc"])
                except TypeError:
                    non_null.sort(key=lambda i, c=c: str(_get_col_val(rows[i], c)),
                                  reverse=ob["desc"])
                indices = non_null + null_i

        p_rows = [rows[i] for i in indices]

        if fn == "ROW_NUMBER":
            for pos, idx in enumerate(indices):
                rows[idx][col] = pos + 1

        elif fn in ("RANK", "DENSE_RANK"):
            rank = 1; dense_r = 1; tie_count = 0; prev_key = None
            for idx in indices:
                curr = tuple(_get_col_val(rows[idx], ob["col"]) for ob in ob_spec) if ob_spec else ()
                if prev_key is None:
                    rows[idx][col] = rank if fn == "RANK" else dense_r
                elif curr == prev_key:
                    tie_count += 1
                    rows[idx][col] = rank if fn == "RANK" else dense_r
                else:
                    rank += tie_count + 1; tie_count = 0; dense_r += 1
                    rows[idx][col] = rank if fn == "RANK" else dense_r
                prev_key = curr

        elif fn in ("LAG", "LEAD"):
            offset    = int(fn_args[1]) if len(fn_args) > 1 else 1
            default_v = eval_expr(fn_args[2], {}) if len(fn_args) > 2 else None
            tcol      = fn_args[0].strip() if fn_args else None
            for pos, idx in enumerate(indices):
                tpos = pos - offset if fn == "LAG" else pos + offset
                if 0 <= tpos < len(p_rows) and tcol:
                    rows[idx][col] = _get_col_val(p_rows[tpos], tcol)
                else:
                    rows[idx][col] = default_v

        elif fn == "NTILE":
            n = int(fn_args[0]) if fn_args else 1
            total = len(indices)
            for pos, idx in enumerate(indices):
                rows[idx][col] = (pos * n) // total + 1

        elif fn == "FIRST_VALUE":
            tcol  = fn_args[0].strip() if fn_args else None
            frame = wf.get("frame")
            if tcol:
                for pos, idx in enumerate(indices):
                    fr = _frame_slice(indices, pos, frame) if frame else indices
                    fv = _get_col_val(rows[fr[0]], tcol) if fr else None
                    rows[idx][col] = fv

        elif fn == "LAST_VALUE":
            tcol  = fn_args[0].strip() if fn_args else None
            frame = wf.get("frame")
            if tcol:
                for pos, idx in enumerate(indices):
                    fr = _frame_slice(indices, pos, frame) if frame else indices
                    lv = _get_col_val(rows[fr[-1]], tcol) if fr else None
                    rows[idx][col] = lv

        elif fn in ("SUM", "AVG", "MIN", "MAX", "COUNT"):
            tcol    = fn_args[0].strip() if fn_args else None
            is_star = not tcol or tcol == "*"
            frame   = wf.get("frame")
            if frame is not None:
                # Per-row frame: each row gets its own aggregate over its frame window
                for pos, idx in enumerate(indices):
                    fr_idxs = _frame_slice(indices, pos, frame)
                    fr_rows = [rows[i] for i in fr_idxs]
                    if fn == "COUNT" and is_star:
                        rows[idx][col] = len(fr_rows)
                    elif tcol:
                        nn = [v for r in fr_rows
                              if (v := _get_col_val(r, tcol)) is not None]
                        if fn == "SUM":   rows[idx][col] = sum(nn) if nn else None
                        elif fn == "MIN": rows[idx][col] = min(nn) if nn else None
                        elif fn == "MAX": rows[idx][col] = max(nn) if nn else None
                        elif fn == "AVG": rows[idx][col] = sum(nn)/len(nn) if nn else None
                        else:             rows[idx][col] = len(nn)
            elif fn == "COUNT" and is_star:
                agg = len(indices)
                for idx in indices:
                    rows[idx][col] = agg
            elif tcol:
                vals = [_get_col_val(p_rows[p], tcol) for p in range(len(p_rows))]
                nn = [v for v in vals if v is not None]
                if fn == "SUM":   agg = sum(nn) if nn else None
                elif fn == "MIN": agg = min(nn) if nn else None
                elif fn == "MAX": agg = max(nn) if nn else None
                elif fn == "AVG": agg = sum(nn) / len(nn) if nn else None
                else:             agg = len(nn)
                for idx in indices:
                    rows[idx][col] = agg


def _expand_named_window(col: str, named_windows: dict) -> str:
    """Replace 'fn() OVER w' with 'fn() OVER (window_spec)' for a named window ref."""
    m = _WINDOW_NAMED_RE.search(col)
    if m:
        name = m.group(1).upper()
        if name in named_windows:
            return col[:m.start()] + f"OVER ({named_windows[name]})"
    return col


def _apply_window_functions(rows: list[dict], cols: list[str],
                            named_windows: dict | None = None) -> list[dict]:
    """Compute any window-function columns and inject them into each row."""
    if not rows or not cols:
        return rows
    nw = named_windows or {}
    expanded = [_expand_named_window(c, nw) if c != "*" else c for c in cols]
    defs = [(orig, _parse_window_col(exp))
            for orig, exp in zip(cols, expanded) if orig != "*"]
    defs = [(c, w) for c, w in defs if w is not None]
    if not defs:
        return rows
    result = [dict(r) for r in rows]
    for c, wf in defs:
        _apply_one_window(result, c, wf)
    return result


def _exec_derived_table(stmt: dict, db: "Database",
                        ctes: dict | None = None) -> list[dict]:
    """Execute a SELECT whose FROM clause is a subquery (derived table)."""
    sub_rows = _rows_for_stmt(stmt["subquery_from"], db, ctes)
    alias = stmt.get("subquery_alias", "t")
    rows: list[dict] = []
    for row in sub_rows:
        merged = dict(row)
        merged.update({f"{alias}.{k}": v for k, v in row.items()})
        rows.append(merged)
    if stmt.get("where"):
        rows = [r for r in rows if stmt["where"].evaluate(r, db)]
    cols = stmt.get("columns")
    if cols:
        rows = [_project_row(r, cols) for r in rows]
    return _apply_order_limit(rows, stmt.get("order_by"),
                              stmt.get("limit"), stmt.get("offset"))


def _exec_recursive_cte(cte_def: dict, db: "Database", ctes: dict) -> list[dict]:
    """Execute a RECURSIVE CTE: seed with base, iterate recursive part until empty."""
    # Extract column aliases from name like "cnt(n, m)" → ["n", "m"]
    raw_name = cte_def["name"]
    cte_key  = raw_name.split("(")[0]
    col_aliases: list[str] = []
    m = __import__("re").search(r"\(([^)]+)\)", raw_name)
    if m:
        col_aliases = [c.strip() for c in m.group(1).split(",")]

    def _apply_aliases(row: dict) -> dict:
        if not col_aliases:
            return row
        vals = list(row.values())
        return {col_aliases[i]: vals[i] for i in range(min(len(col_aliases), len(vals)))}

    def _row_key(r: dict) -> tuple:
        return tuple(r.values())

    accumulated: list[dict] = []
    seen: list[tuple] = []
    union_all = cte_def.get("union_all", True)

    working = [_apply_aliases(r) for r in _rows_for_stmt(cte_def["base"], db, ctes)]
    for row in working:
        k = _row_key(row)
        if union_all or k not in seen:
            accumulated.append(row)
            seen.append(k)

    max_iterations = 1000
    for _ in range(max_iterations):
        if not working:
            break
        step_ctes = {**ctes, cte_key: {"op": "INLINE_ROWS", "rows": working}}
        raw = _rows_for_stmt(cte_def["recursive"], db, step_ctes)
        working = [_apply_aliases(r) for r in raw]
        new_rows: list[dict] = []
        for row in working:
            k = _row_key(row)
            if union_all or k not in seen:
                accumulated.append(row)
                seen.append(k)
                new_rows.append(row)
        working = new_rows if not union_all else working

    return accumulated


def _exec_cte_select(outer: dict, cte_ast: dict, db: "Database",
                     ctes: dict) -> list[dict]:
    """Execute a SELECT whose FROM table is a CTE name."""
    rows = _rows_for_stmt(cte_ast, db, ctes)
    if outer.get("where"):
        rows = [r for r in rows if outer["where"].evaluate(r, db)]
    # Order and limit on full rows (before projection so ORDER BY cols are available)
    rows = _apply_order_limit(rows, outer.get("order_by"),
                              outer.get("limit"), outer.get("offset"))
    cols = outer.get("columns")
    if cols:
        rows = [_project_row(r, cols) for r in rows]
    return rows


def _normalize_row(row: dict) -> dict:
    """Add bare-name aliases for table-qualified keys (users.name → also name)."""
    result = dict(row)
    for k, v in row.items():
        if "." in k:
            bare = k.split(".")[-1]
            if bare not in result:
                result[bare] = v
    return result


def _apply_groupby_agg(rows: list[dict], columns: list[str] | None,
                       group_by: list[str] | None,
                       having: Any,
                       db: "Database") -> list[dict]:
    """Apply GROUP BY + aggregation to an already-materialized row list."""
    # Normalize rows so aggregation sees both qualified and bare keys
    rows = [_normalize_row(r) for r in rows]
    select_cols = columns or (group_by or [])

    if not group_by:
        result: dict = db._compute_aggregates(rows, select_cols)
        return [result]

    buckets: dict[tuple, list[dict]] = {}
    for row in rows:
        key = tuple(row.get(gc) for gc in group_by)
        if key not in buckets:
            buckets[key] = []
        buckets[key].append(row)

    results: list[dict] = []
    for key, bucket_rows in buckets.items():
        result = {}
        for gc, kv in zip(group_by, key):
            result[gc] = kv
        result.update(db._compute_aggregates(bucket_rows, select_cols))
        if having and not having.evaluate(result, db):
            continue
        results.append({c: result[c] for c in select_cols if c in result}
                       if columns else result)
    return results


def _exec_extra_join(rows: list[dict], join_info: dict,
                     db: "Database", ctes: dict | None = None) -> list[dict]:
    """Apply one additional JOIN step in-memory against an already-joined row set."""
    right_table = join_info["right_table"]
    right_alias = join_info.get("right_alias") or right_table
    join_type   = join_info.get("join_type", "INNER")
    on_clause   = join_info.get("on_clause")
    on_left     = join_info.get("on_left")
    on_right    = join_info.get("on_right")
    rcol        = on_right.split(".")[-1] if on_right else None
    lat_sub     = join_info.get("lateral_subquery")

    # LATERAL: for each left row, re-execute the subquery with outer context
    if lat_sub is not None:
        result: list[dict] = []
        for lr in rows:
            inst = {**lat_sub, "where": _instantiate_correlated(lat_sub.get("where"), lr)}
            lat_rows = _rows_for_stmt(inst, db, ctes or {})
            if lat_rows:
                for rr in lat_rows:
                    merged = dict(lr)
                    merged.update({f"{right_alias}.{k}": v for k, v in rr.items()})
                    result.append(merged)
            elif join_type in ("LEFT", "FULL"):
                result.append(dict(lr))
        return result

    rmeta      = db._meta(right_table)
    right_null = {f"{right_alias}.{c.name}": None for c in rmeta.schema.columns}

    # INLJ only for simple single-equality ON with an index on the right column
    use_inlj = (join_type == "INNER" and rcol is not None and on_clause is not None
                and on_clause.and_clause is None and on_clause.or_clause is None
                and _find_eq_index(db, right_table, rcol) is not None)

    if not use_inlj:
        right_rows = [deserialize_row(rmeta.schema, db._unpack_row_cell(r))
                      for _, r in db._table_btree(rmeta).scan()]

    result: list[dict] = []
    matched_right: set[int] = set()

    for lr in rows:
        if on_clause is None and on_left is None:   # CROSS JOIN
            for rr in right_rows:  # type: ignore[possibly-undefined]
                merged = dict(lr)
                merged.update({f"{right_alias}.{k}": v for k, v in rr.items()})
                result.append(merged)
            continue

        if use_inlj:
            lval = lr.get(on_left) if on_left else None
            if lval is None and on_left:
                lval = lr.get(on_left.split(".")[-1])
            if lval is None:
                continue  # INNER JOIN: NULL never matches
            probed = _probe_index(db, right_table, rcol, lval)
            for rr in (probed or []):
                merged = dict(lr)
                merged.update({f"{right_alias}.{k}": v for k, v in rr.items()})
                result.append(merged)
        else:
            on_matched = False
            lcol = on_left.split(".")[-1] if on_left else None
            for j, rr in enumerate(right_rows):  # type: ignore[possibly-undefined]
                merged = dict(lr)
                merged.update({f"{right_alias}.{k}": v for k, v in rr.items()})
                if on_clause is not None:
                    if not on_clause.evaluate(merged, db):
                        continue
                else:
                    lval = lr.get(on_left) or lr.get(lcol)  # type: ignore[arg-type]
                    rval = rr.get(rcol)
                    if lval != rval:
                        continue
                on_matched = True
                matched_right.add(j)
                result.append(merged)
            if not on_matched and join_type in ("LEFT", "FULL"):
                merged = dict(lr)
                merged.update(right_null)
                result.append(merged)

    if not use_inlj and join_type in ("RIGHT", "FULL"):
        left_null = {k: None for k in (rows[0] if rows else {})}
        for j, rr in enumerate(right_rows):  # type: ignore[possibly-undefined]
            if j not in matched_right:
                merged = dict(left_null)
                merged.update({f"{right_alias}.{k}": v for k, v in rr.items()})
                result.append(merged)
    return result


_JSON_EACH_RE = re.compile(r'^(json_each|json_tree)\s*\((.+)\)\s*$', re.IGNORECASE | re.DOTALL)


def _materialize_table(tname: str, db: "Database", ctes: dict,
                        alias: str | None = None) -> list[dict]:
    """Return rows for a real table, CTE, view, or table-valued function."""
    m = _JSON_EACH_RE.match(tname)
    if m:
        args_str = m.group(2).strip()
        # Split on comma respecting nested parens/quotes
        parts: list[str] = []
        depth = 0; buf: list[str] = []; in_str = False; i = 0
        while i < len(args_str):
            ch = args_str[i]
            if in_str:
                buf.append(ch)
                if ch == "'":
                    if i + 1 < len(args_str) and args_str[i + 1] == "'":
                        buf.append(args_str[i + 1]); i += 2; continue
                    in_str = False
            elif ch == "'": in_str = True; buf.append(ch)
            elif ch == "(": depth += 1; buf.append(ch)
            elif ch == ")": depth -= 1; buf.append(ch)
            elif ch == "," and depth == 0: parts.append("".join(buf).strip()); buf = []
            else: buf.append(ch)
            i += 1
        if buf: parts.append("".join(buf).strip())
        json_val = eval_expr(parts[0], {}) if parts else None
        path     = eval_expr(parts[1], {}) if len(parts) >= 2 else "$"
        raw = _json_each_rows(json_val, str(path) if path else "$")
        if alias:
            return [{f"{alias}.{k}": v for k, v in row.items()} for row in raw]
        return raw

    if tname in ctes:
        cte_ast = ctes[tname]
        raw = _rows_for_stmt(cte_ast, db, ctes)
        # Apply the CTE's own column aliases so t.total resolves correctly
        cte_aliases = cte_ast.get("col_aliases") if isinstance(cte_ast, dict) else None
        if cte_aliases:
            raw = [{cte_aliases.get(k, k): v for k, v in row.items()} for row in raw]
    elif tname in db.views:
        view_ast = _parse_tokens(_tokenize(db.views[tname]))
        raw = _rows_for_stmt(view_ast, db, ctes)
    else:
        from .schema import deserialize_row
        meta = db._meta(tname)
        raw = []
        for _, r in db._table_btree(meta).scan():
            _check_timeout(db)
            raw.append(deserialize_row(meta.schema, db._unpack_row_cell(r)))
    if alias:
        return [{f"{alias}.{k}": v for k, v in row.items()} for row in raw]
    return raw


def _exec_in_memory_join(stmt: dict, db: "Database", ctes: dict) -> list[dict]:
    """Nested-loop join when one or both tables are CTEs or views."""
    ltbl   = stmt["left_table"]
    rtbl   = stmt["right_table"]
    lalias    = stmt.get("left_alias") or ltbl
    ralias    = stmt.get("right_alias") or rtbl
    on_clause = stmt.get("on_clause")
    join_type = stmt.get("join_type", "INNER")

    left_rows = _materialize_table(ltbl, db, ctes)
    _lat_sub = stmt.get("lateral_subquery")
    # Detect lateral TVF: right side may be correlated (e.g. json_each(t.col))
    _rtbl_is_tvf = bool(_JSON_EACH_RE.match(rtbl))
    right_rows_static = (None if (_rtbl_is_tvf or _lat_sub)
                         else _materialize_table(rtbl, db, ctes))

    def _right_rows_for(lr: dict) -> list[dict]:
        if _lat_sub is not None:
            inst = {**_lat_sub, "where": _instantiate_correlated(_lat_sub.get("where"), lr)}
            return _rows_for_stmt(inst, db, ctes)
        if not _rtbl_is_tvf:
            return right_rows_static  # type: ignore[return-value]
        # Lateral TVF: substitute column references in the TVF call from lr
        m = _JSON_EACH_RE.match(rtbl)
        assert m
        args_str = m.group(2).strip()
        resolved = eval_expr(args_str, lr)
        return _json_each_rows(resolved)

    result: list[dict] = []
    matched_right: set[int] = set()
    for lr in left_rows:
        _check_timeout(db)
        matched = False
        right_rows = _right_rows_for(lr)
        for ri, rr in enumerate(right_rows):
            # Build the merged row (with alias-prefixed keys) before evaluating ON
            merged = {**lr, **rr}
            for k, v in lr.items():
                merged[f"{lalias}.{k.split('.')[-1]}"] = v
            for k, v in rr.items():
                merged[f"{ralias}.{k.split('.')[-1]}"] = v
            if on_clause is None or on_clause.evaluate(merged, db):
                result.append(merged)
                matched = True
                matched_right.add(ri)
        if not matched and join_type in ("LEFT", "LEFT OUTER"):
            merged = dict(lr)
            for k, v in lr.items():
                merged[f"{lalias}.{k.split('.')[-1]}"] = v
            r_ref = right_rows_static[0] if right_rows_static else {}
            for k in r_ref:
                merged.setdefault(k, None)
                merged.setdefault(f"{ralias}.{k.split('.')[-1]}", None)
            result.append(merged)

    if join_type in ("RIGHT", "RIGHT OUTER") and right_rows_static is not None:
        for ri, rr in enumerate(right_rows_static):
            if ri not in matched_right:
                merged = dict(rr)
                for k, v in rr.items():
                    merged[f"{ralias}.{k.split('.')[-1]}"] = v
                result.append(merged)

    if stmt.get("where"):
        result = [r for r in result if stmt["where"].evaluate(r, db)]
    if stmt.get("columns"):
        result = [_project_row(r, stmt["columns"]) for r in result]
    return _apply_order_limit(result, stmt.get("order_by"),
                              stmt.get("limit"), stmt.get("offset"))


def _rows_for_stmt(stmt: dict, db: "Database",
                   ctes: dict | None = None) -> list[dict]:
    """Execute any SELECT-like statement and return its rows.

    This is the single authoritative SELECT execution path.  _execute_inner
    delegates all SELECT/JOIN/SET_OP ops here and just formats the result.
    """
    _check_timeout(db)
    ctes = {**(ctes or {}), **(stmt.get("ctes") or {})}
    op = stmt["op"]
    if op == "INLINE_ROWS":
        return stmt["rows"]
    if op == "RECURSIVE_CTE":
        return _exec_recursive_cte(stmt, db, ctes)
    if op == "SELECT_NOFROM":
        col_aliases = stmt.get("col_aliases") or {}
        result = {col: eval_expr(col, {}) for col in (stmt.get("columns") or [])}
        return [{col_aliases.get(k, k): v for k, v in result.items()}]
    if op == "SELECT":
        s = _resolve_alias_refs(stmt, stmt.get("col_aliases"))
        stmt_cols = stmt.get("columns") or []
        nw = stmt.get("named_windows") or {}
        has_window    = any(_WINDOW_RE.search(c) or _WINDOW_NAMED_RE.search(c)
                            for c in stmt_cols if c != "*")
        has_scalar_sq = any(_is_scalar_subquery_col(c) for c in stmt_cols)
        tbl = s.get("table") or ""
        if s.get("subquery_from"):
            rows = _exec_derived_table(s, db, ctes)
        elif tbl == "_hyperion_master":
            rows = _exec_cte_select(s, {"op": "INLINE_ROWS",
                                        "rows": _hyperion_master_rows(db)}, db, ctes)
        elif tbl in ctes:
            if s.get("group_by") or any(_q_parse_agg(c) for c in stmt_cols if c != "*"):
                raw_stmt = {**s, "columns": None, "order_by": [], "limit": None, "offset": None}
                raw_rows = _exec_cte_select(raw_stmt, ctes[tbl], db, ctes)
                rows = _apply_groupby_agg(raw_rows, s.get("columns"), s.get("group_by"),
                                          s.get("having"), db)
                rows = _apply_order_limit(rows, s.get("order_by"), s.get("limit"), s.get("offset"))
            else:
                rows = _exec_cte_select(s, ctes[tbl], db, ctes)
        elif tbl in db.views:
            view_ast = _parse_tokens(_tokenize(db.views[tbl]))
            if s.get("group_by") or any(_q_parse_agg(c) for c in stmt_cols if c != "*"):
                raw_stmt = {**s, "columns": None, "order_by": [], "limit": None, "offset": None}
                raw_rows = _exec_cte_select(raw_stmt, view_ast, db, ctes)
                rows = _apply_groupby_agg(raw_rows, s.get("columns"), s.get("group_by"),
                                          s.get("having"), db)
                rows = _apply_order_limit(rows, s.get("order_by"), s.get("limit"), s.get("offset"))
            else:
                rows = _exec_cte_select(s, view_ast, db, ctes)
        elif _JSON_EACH_RE.match(tbl):
            rows = _exec_cte_select(s, {"op": "INLINE_ROWS",
                                        "rows": _materialize_table(tbl, db, ctes)}, db, ctes)
        elif has_window or has_scalar_sq:
            all_rows = db.select(s["table"], None, s["where"], None, None,
                                 s.get("group_by"), s.get("having"),
                                 s.get("distinct", False), None)
            if has_scalar_sq:
                sq_cols = [c for c in stmt_cols if _is_scalar_subquery_col(c)]
                augmented = []
                for row in all_rows:
                    r = dict(row)
                    for sc in sq_cols:
                        r[sc] = _eval_scalar_subquery(sc, r, db, ctes)
                    augmented.append(r)
                all_rows = augmented
            if has_window:
                all_rows = _apply_window_functions(all_rows, stmt_cols, nw)
            all_rows = _apply_order_limit(all_rows, s.get("order_by"), s.get("limit"), s.get("offset"))
            rows = [_project_row(r, stmt_cols) for r in all_rows] if stmt_cols else all_rows
        else:
            col_aliases = stmt.get("col_aliases") or {}
            raw = db.select(s["table"], s["columns"], s["where"],
                            s.get("order_by"), s.get("limit"),
                            s.get("group_by"), s.get("having"),
                            s.get("distinct", False), s.get("offset"))
            if col_aliases:
                raw = [{col_aliases.get(k, k): v for k, v in row.items()} for row in raw]
            return raw
        rows, _ = _apply_aliases(rows, stmt.get("columns"), stmt.get("col_aliases"))
        return rows
    if op == "JOIN":
        s = _resolve_alias_refs(stmt, stmt.get("col_aliases"))
        ltbl = s.get("left_table", "")
        rtbl = s.get("right_table", "")
        group_by = s.get("group_by")
        has_agg  = (group_by or (s.get("having") is not None) or any(
            _q_parse_agg(c) for c in (s.get("columns") or []) if c != "*"))
        multi_cond_on = s.get("on_clause") is not None and s.get("on_left") is None
        has_lateral = (rtbl == "__lateral__" or s.get("lateral_subquery") is not None
                       or any(ej.get("lateral_subquery") for ej in (s.get("extra_joins") or [])))
        if (ltbl in ctes or rtbl in ctes or ltbl in db.views or rtbl in db.views
                or _JSON_EACH_RE.match(ltbl) or _JSON_EACH_RE.match(rtbl)
                or has_agg or multi_cond_on or has_lateral):
            raw_stmt = {**s, "columns": None, "order_by": [], "limit": None, "offset": None}
            raw_rows = _exec_in_memory_join(raw_stmt, db, ctes)
            for ej in (s.get("extra_joins") or []):
                raw_rows = _exec_extra_join(raw_rows, ej, db, ctes)
            if has_agg:
                raw_rows = _apply_groupby_agg(raw_rows, s.get("columns"),
                                              group_by, s.get("having"), db)
            elif s.get("columns"):
                raw_rows = [_project_row(_normalize_row(r), s["columns"]) for r in raw_rows]
            rows = _apply_order_limit(raw_rows, s.get("order_by"), s.get("limit"), s.get("offset"))
            rows, _ = _apply_aliases(rows, stmt.get("columns"), stmt.get("col_aliases"))
            return rows
        s = optimize_join(s, db)
        extra = s.get("extra_joins", [])
        if extra:
            rows = db.join(s["left_table"], s["right_table"],
                           s["on_left"], s["on_right"],
                           None, None,
                           join_type=s.get("join_type", "INNER"),
                           left_alias=s.get("left_alias"),
                           right_alias=s.get("right_alias"))
            for ej in extra:
                rows = _exec_extra_join(rows, ej, db, ctes)
            if s.get("where"):
                rows = [r for r in rows if s["where"].evaluate(r, db)]
            if s.get("columns"):
                rows = [_project_row(r, s["columns"]) for r in rows]
            rows = _apply_order_limit(rows, s.get("order_by"), s.get("limit"), s.get("offset"))
        else:
            rows = db.join(s["left_table"], s["right_table"],
                           s["on_left"], s["on_right"],
                           s["columns"], s["where"],
                           s.get("order_by"), s.get("limit"),
                           s.get("join_type", "INNER"),
                           s.get("left_alias"), s.get("right_alias"),
                           s.get("offset"))
        rows, _ = _apply_aliases(rows, stmt.get("columns"), stmt.get("col_aliases"))
        return rows
    if op == "SET_OP":
        left  = _rows_for_stmt(stmt["left"],  db, ctes)
        right = _rows_for_stmt(stmt["right"], db, ctes)
        return _apply_set_op(stmt["set_op"], stmt.get("all", False), left, right)
    raise RuntimeError(f"Expected SELECT/JOIN/SET_OP, got '{op}'")


def _iter_rows_for_stmt(stmt: dict, db: "Database",
                        ctes: dict | None = None):
    """Streaming SELECT: yields rows one at a time without building a full list.

    For simple table scans (real table, no ORDER BY / GROUP BY / DISTINCT /
    aggregates / window functions / scalar subqueries) rows are yielded
    directly from the B-tree so the caller never holds the entire result set in
    memory.  All other query shapes fall back to the fully-materialised
    _rows_for_stmt path and then yield from that list.
    """
    _check_timeout(db)
    merged_ctes = {**(ctes or {}), **(stmt.get("ctes") or {})}
    op = stmt.get("op", "")

    if op == "SELECT" and not stmt.get("subquery_from"):
        tbl = stmt.get("table") or ""
        stmt_cols = stmt.get("columns") or []
        if (tbl
                and tbl not in merged_ctes
                and tbl not in db.views
                and tbl != "_hyperion_master"
                and not _JSON_EACH_RE.match(tbl)
                and tbl in db._catalog.tables
                and not stmt.get("order_by")
                and not stmt.get("group_by")
                and not stmt.get("having")
                and not stmt.get("distinct", False)
                and not any(_q_parse_agg(c) for c in stmt_cols if c != "*")
                and not any(_WINDOW_RE.search(c) or _WINDOW_NAMED_RE.search(c)
                            for c in stmt_cols if c != "*")
                and not any(_is_scalar_subquery_col(c) for c in stmt_cols)):
            s = _resolve_alias_refs(stmt, stmt.get("col_aliases"))
            col_aliases = stmt.get("col_aliases") or {}
            meta = db._meta(tbl)
            schema = meta.schema
            where  = s.get("where")
            cols   = s.get("columns")
            limit  = s.get("limit")
            offset = s.get("offset") or 0
            skipped = 0
            count   = 0
            for _, raw in db._table_btree(meta).scan():
                _check_timeout(db)
                row = deserialize_row(schema, db._unpack_row_cell(raw))
                if where and not where.evaluate(row, db):
                    continue
                if skipped < offset:
                    skipped += 1
                    continue
                projected = _project_row(row, cols) if cols else row
                if col_aliases:
                    projected = {col_aliases.get(k, k): v
                                 for k, v in projected.items()}
                yield projected
                count += 1
                if limit is not None and count >= limit:
                    return
            return

    yield from _rows_for_stmt(stmt, db, merged_ctes)


def _is_unique_index(idx_name: str, idx_meta, db: Database) -> bool:
    if idx_name.startswith("_pk_"):
        return True
    tname = idx_meta.table_name
    if tname in db.tables:
        schema = db._meta(tname).schema
        for col in schema.columns:
            if col.name in idx_meta.columns and (col.unique or col.primary_key):
                return True
        for uc in schema.unique_constraints:
            if sorted(uc) == sorted(idx_meta.columns):
                return True
    return False


def _handle_pragma(stmt: dict, db: Database) -> str:
    name = stmt.get("name", "")

    if name == "foreign_keys":
        value = stmt.get("value", "").upper()
        if value in ("ON", "1", "TRUE"):
            db.fk_enforcement = True
            return "foreign_keys = 1"
        if value in ("OFF", "0", "FALSE"):
            db.fk_enforcement = False
            return "foreign_keys = 0"
        return f"foreign_keys = {1 if db.fk_enforcement else 0}"

    if name == "table_info":
        tname = stmt.get("arg") or ""
        if tname not in db.tables:
            raise RuntimeError(f"No such table: '{tname}'")
        schema = db._meta(tname).schema
        pk_cols = set(schema.primary_key_columns or [])
        rows = []
        for cid, col in enumerate(schema.columns):
            is_pk = 1 if (col.primary_key or col.name in pk_cols) else 0
            rows.append({
                "cid": cid, "name": col.name, "type": col.type,
                "notnull": 0 if col.nullable else 1,
                "dflt_value": col.default, "pk": is_pk,
            })
        return _format_rows(rows, ["cid", "name", "type", "notnull", "dflt_value", "pk"])

    if name == "index_list":
        tname = stmt.get("arg") or ""
        rows = []
        for seq, (idx_name, idx_meta) in enumerate(
                (n, m) for n, m in db.indexes.items() if m.table_name == tname):
            rows.append({"seq": seq, "name": idx_name,
                         "unique": 1 if _is_unique_index(idx_name, idx_meta, db) else 0})
        return _format_rows(rows, ["seq", "name", "unique"]) if rows else "(no rows)"

    if name == "index_info":
        idx_name = stmt.get("arg") or ""
        if idx_name not in db.indexes:
            raise RuntimeError(f"No such index: '{idx_name}'")
        idx_meta = db.indexes[idx_name]
        schema = db._meta(idx_meta.table_name).schema
        col_cids = {c.name: i for i, c in enumerate(schema.columns)}
        rows = [{"seqno": i, "cid": col_cids.get(col, -1), "name": col}
                for i, col in enumerate(idx_meta.columns)]
        return _format_rows(rows, ["seqno", "cid", "name"])

    if name == "integrity_check":
        results = _integrity_check(db)
        rows = [{"integrity_check": msg} for msg in results]
        return _format_rows(rows, ["integrity_check"])

    raise RuntimeError(f"Unknown PRAGMA: '{name}'")


def _execute_analyze(stmt: dict, db: Database) -> str:
    """Scan tables and persist row count + per-column NDV statistics to the catalog."""
    target = stmt.get("table")
    tables_to_analyze = ([target] if target and target in db.tables
                         else list(db.tables.keys()))

    if target and target not in db.tables:
        raise RuntimeError(f"No such table: '{target}'")

    for tname in tables_to_analyze:
        meta   = db._meta(tname)
        schema = meta.schema
        col_names = [c.name for c in schema.columns]

        row_count = 0
        distinct: dict[str, set] = {c: set() for c in col_names}

        for _, raw in db._table_btree(meta).scan():
            row = deserialize_row(schema, db._unpack_row_cell(raw))
            row_count += 1
            for c in col_names:
                val = row.get(c)
                # Use a hashable sentinel for None so it counts as a distinct value
                distinct[c].add(val if val is not None else _ANALYZE_NULL_SENTINEL)

        db._catalog.stats[tname] = {
            "row_count": row_count,
            "columns": {c: {"ndv": len(distinct[c])} for c in col_names},
        }

        # Refresh session row-count cache
        if hasattr(db, "_opt_row_counts"):
            db._opt_row_counts[tname] = row_count

    db._flush_catalog()

    n = len(tables_to_analyze)
    summary = ", ".join(tables_to_analyze) if n <= 5 else f"{n} tables"
    return f"Statistics collected for: {summary}."


_ANALYZE_NULL_SENTINEL = object()


def execute(stmt: dict, db: Database) -> str:
    op = stmt["op"]

    # Authorizer check (DML/DDL ops; SELECT ops are checked in Cursor.execute)
    if db._authorizer is not None:
        from .auth import check_authorizer, SQLITE_IGNORE
        if check_authorizer(db._authorizer, stmt) == SQLITE_IGNORE:
            return ""

    # Read-only guard
    if db._readonly and op in _WRITE_OPS:
        raise ReadOnlyError(
            f"Cannot execute {op} on a read-only database connection"
        )

    # Transaction control — never auto-wrapped
    if op == "BEGIN":
        db.begin()
        return "Transaction started."
    if op == "COMMIT":
        db.commit()
        return "Transaction committed."
    if op == "ROLLBACK":
        db.rollback()
        return "Transaction rolled back."
    if op == "SAVEPOINT":
        db.savepoint(stmt["name"])
        return f"Savepoint '{stmt['name']}' set."
    if op == "RELEASE_SAVEPOINT":
        db.release_savepoint(stmt["name"])
        return f"Savepoint '{stmt['name']}' released."
    if op == "ROLLBACK_TO_SAVEPOINT":
        db.rollback_to_savepoint(stmt["name"])
        return f"Rolled back to savepoint '{stmt['name']}'."

    if op == "PRAGMA":
        return _handle_pragma(stmt, db)

    if op == "EXPLAIN":
        plan_rows = _explain_plan(stmt["stmt"], db)
        cols = ["id", "parent", "notused", "detail"]
        return _format_rows(plan_rows, cols)

    if op == "VACUUM":
        return db.vacuum()

    # All other statements: auto-commit if not inside an explicit BEGIN
    auto = not db.in_transaction
    if auto:
        db.begin()
    try:
        result = _execute_inner(stmt, db)
    except Exception:
        if auto:
            db.rollback()
        raise
    if auto:
        db.commit()
    return result


def _view_rows(view_name: str, where: Any, db: Database) -> list[dict]:
    """Execute a view's SELECT and return rows matching where.

    Rows are normalized so bare column names (e.g. 'id') are available
    alongside table-qualified ones ('users.id'), allowing WHERE clauses
    and OLD.col references in trigger bodies to use simple column names.
    """
    view_ast = _parse_tokens(_tokenize(db.views[view_name]))
    raw_rows = _rows_for_stmt(view_ast, db)
    rows = []
    for row in raw_rows:
        nr = dict(row)
        for k, v in row.items():
            if "." in k:
                bare = k.split(".", 1)[1]
                if bare not in nr:
                    nr[bare] = v
        rows.append(nr)
    if where:
        rows = [r for r in rows if where.evaluate(r, db)]
    return rows


def _exec_instead_of_insert(stmt: dict, db: Database) -> str:
    tname = stmt["table"]
    col_names = stmt["col_names"]
    if not col_names:
        rows = _view_rows(tname, None, db)
        if rows:
            col_names = list(rows[0].keys())
        else:
            raise RuntimeError(
                f"Cannot determine columns for INSERT on view '{tname}' "
                f"— specify column names explicitly")
    count = 0
    for values in stmt["rows"]:
        if len(col_names) != len(values):
            raise RuntimeError(
                f"Column/value mismatch: {len(col_names)} columns, {len(values)} values")
        parsed: dict[str, Any] = {
            n: (None if v.upper() == "NULL" else v)
            for n, v in zip(col_names, values)
        }
        fire_triggers(db, tname, "INSTEAD OF", "INSERT", parsed, None)
        count += 1
    return f"{count} row{'s' if count != 1 else ''} inserted."


def _exec_instead_of_update(stmt: dict, db: Database) -> str:
    from .expr import eval_expr, is_expr as _is_expr
    tname = stmt["table"]
    old_rows = _view_rows(tname, stmt.get("where"), db)
    for old_row in old_rows:
        new_row = dict(old_row)
        for col, val_str in stmt["assignments"].items():
            if val_str is None or (isinstance(val_str, str) and val_str.upper() == "NULL"):
                new_row[col] = None
            elif _is_expr(str(val_str)):
                new_row[col] = eval_expr(str(val_str), old_row)
            else:
                new_row[col] = val_str
        fire_triggers(db, tname, "INSTEAD OF", "UPDATE", new_row, old_row)
    n = len(old_rows)
    return f"{n} row{'s' if n != 1 else ''} updated."


def _exec_instead_of_delete(stmt: dict, db: Database) -> str:
    tname = stmt["table"]
    old_rows = _view_rows(tname, stmt.get("where"), db)
    for old_row in old_rows:
        fire_triggers(db, tname, "INSTEAD OF", "DELETE", None, old_row)
    n = len(old_rows)
    return f"{n} row{'s' if n != 1 else ''} deleted."


def _execute_inner(stmt: dict, db: Database) -> str:
    from .schema import Schema
    op = stmt["op"]

    if op == "ANALYZE":
        return _execute_analyze(stmt, db)

    if op == "CREATE_TABLE_AS_SELECT":
        from .schema import Schema, Column
        if stmt.get("if_not_exists") and stmt["name"] in db.tables:
            return f"Table '{stmt['name']}' already exists."
        rows = _rows_for_stmt(stmt["select"], db)
        if not rows:
            sel_cols = stmt["select"].get("columns") or []
            columns = [Column(c, TEXT, DEFAULT_TEXT_SIZE)
                       for c in sel_cols if c != "*"]
        else:
            columns = []
            for col_name, val in rows[0].items():
                if isinstance(val, int):
                    columns.append(Column(col_name, INTEGER, 8))
                elif isinstance(val, float):
                    columns.append(Column(col_name, REAL, 8))
                else:
                    max_len = max(len(str(r.get(col_name) or "")) for r in rows)
                    columns.append(Column(col_name, TEXT, max(DEFAULT_TEXT_SIZE, max_len + 16)))
        from .schema import Schema
        db.create_table(Schema(name=stmt["name"], columns=columns))
        for row in rows:
            db.insert(stmt["name"], row)
        n = len(rows)
        return f"Table '{stmt['name']}' created with {n} row{'s' if n != 1 else ''}."

    if op == "CREATE_TABLE":
        if stmt.get("if_not_exists") and stmt["name"] in db.tables:
            return f"Table '{stmt['name']}' already exists."
        pk_cols = stmt.get("primary_key_columns") or []
        if pk_cols:
            for col in stmt["columns"]:
                if col.name in pk_cols:
                    col.nullable = False
            uc = list(stmt.get("unique_constraints") or [])
            if pk_cols not in uc:
                uc.append(pk_cols)
            stmt = {**stmt, "unique_constraints": uc}
        db.create_table(Schema(name=stmt["name"], columns=stmt["columns"],
                               foreign_keys=stmt.get("foreign_keys", []),
                               unique_constraints=stmt.get("unique_constraints", []),
                               primary_key_columns=pk_cols),
                        temporary=stmt.get("temporary", False))
        for col in stmt["columns"]:
            if col.primary_key:
                pk_idx = f"_pk_{stmt['name']}_{col.name}"
                if pk_idx not in db.indexes:
                    db.create_index(pk_idx, stmt["name"], [col.name])
        if pk_cols and len(pk_cols) > 1:
            pk_idx = f"_pk_{stmt['name']}_{'_'.join(pk_cols)}"
            if pk_idx not in db.indexes:
                db.create_index(pk_idx, stmt["name"], pk_cols)
        return f"Table '{stmt['name']}' created."

    if op == "DROP_TABLE":
        if stmt.get("if_exists") and stmt["name"] not in db.tables:
            return f"Table '{stmt['name']}' does not exist."
        db.drop_table(stmt["name"])
        return f"Table '{stmt['name']}' dropped."

    if op == "CREATE_VIEW":
        db.create_view(stmt["name"], stmt["sql"],
                       if_not_exists=stmt.get("if_not_exists", False),
                       or_replace=stmt.get("or_replace", False))
        return f"View '{stmt['name']}' created."

    if op == "DROP_VIEW":
        db.drop_view(stmt["name"], if_exists=stmt.get("if_exists", False))
        return f"View '{stmt['name']}' dropped."

    if op == "ALTER_ADD_COLUMN":
        db.alter_add_column(stmt["table"], stmt["col"])
        return f"Column '{stmt['col'].name}' added to '{stmt['table']}'."

    if op == "ALTER_DROP_COLUMN":
        db.alter_drop_column(stmt["table"], stmt["col_name"])
        return f"Column '{stmt['col_name']}' dropped from '{stmt['table']}'."

    if op == "ALTER_RENAME_COLUMN":
        db.alter_rename_column(stmt["table"], stmt["old_name"], stmt["new_name"])
        return f"Column '{stmt['old_name']}' renamed to '{stmt['new_name']}'."

    if op == "ALTER_RENAME_TABLE":
        db.alter_rename_table(stmt["table"], stmt["new_name"])
        return f"Table '{stmt['table']}' renamed to '{stmt['new_name']}'."

    if op == "CREATE_INDEX":
        if stmt.get("if_not_exists") and stmt["idx_name"] in db.indexes:
            return f"Index '{stmt['idx_name']}' already exists."
        db.create_index(stmt["idx_name"], stmt["table"], stmt["cols"])
        cols_str = ", ".join(stmt["cols"])
        return f"Index '{stmt['idx_name']}' created on {stmt['table']}({cols_str})."

    if op == "DROP_INDEX":
        try:
            db.drop_index(stmt["idx_name"])
        except RuntimeError:
            if not stmt.get("if_exists"):
                raise
        return f"Index '{stmt['idx_name']}' dropped."

    if op == "CREATE_TRIGGER":
        from .catalog import TriggerMeta
        if stmt.get("if_not_exists") and stmt["name"] in db._catalog.triggers:
            return f"Trigger '{stmt['name']}' already exists."
        trig = TriggerMeta(stmt["table"], stmt["timing"], stmt["event"],
                           stmt.get("update_cols", []), stmt.get("when_tokens", []),
                           stmt.get("body_tokens", []))
        db.create_trigger(stmt["name"], trig)
        return f"Trigger '{stmt['name']}' created."

    if op == "DROP_TRIGGER":
        if stmt.get("if_exists") and stmt["name"] not in db._catalog.triggers:
            return f"Trigger '{stmt['name']}' does not exist."
        db.drop_trigger(stmt["name"])
        return f"Trigger '{stmt['name']}' dropped."

    if op == "INSERT":
        if stmt["table"] not in db.tables and stmt["table"] in db.views:
            if not has_instead_of(db, stmt["table"], "INSERT"):
                raise RuntimeError(
                    f"Cannot insert into view '{stmt['table']}' without an INSTEAD OF trigger")
            return _exec_instead_of_insert(stmt, db)
        meta            = db._meta(stmt["table"])
        col_names       = stmt["col_names"] or [c.name for c in meta.schema.columns]
        conflict_action = stmt.get("conflict_action")
        on_conflict_set = stmt.get("on_conflict_set") or {}
        returning_cols  = stmt.get("returning")
        returned_rows: list[dict] = []
        _has_ins_trig = has_triggers(db, stmt["table"], "INSERT")
        for values in stmt["rows"]:
            if len(col_names) != len(values):
                raise RuntimeError(
                    f"Column/value mismatch: {len(col_names)} columns, {len(values)} values"
                )
            parsed: dict[str, Any] = {}
            _CONST_EXPRS = frozenset({"TRUE", "FALSE", "CURRENT_TIMESTAMP",
                                       "CURRENT_DATE", "CURRENT_TIME"})
            for name, val in zip(col_names, values):
                if val.upper() == "NULL":
                    parsed[name] = None
                elif _is_single_string_literal(val):
                    # Quoted string literal — unquote (handle '' escape sequences)
                    parsed[name] = val[1:-1].replace("''", "'")
                elif " " in val or val.upper() in _CONST_EXPRS:
                    # Multi-token expression (joined with spaces) or SQL constant
                    parsed[name] = eval_expr(val, {})
                else:
                    parsed[name] = val
            for col in meta.schema.columns:
                if col.name not in parsed:
                    parsed[col.name] = col.default
            if _has_ins_trig:
                fire_triggers(db, stmt["table"], "BEFORE", "INSERT", parsed, None)
            if conflict_action == "IGNORE":
                try:
                    row_out = db.insert(stmt["table"], parsed)
                    if _has_ins_trig:
                        fire_triggers(db, stmt["table"], "AFTER", "INSERT", row_out, None)
                    if returning_cols:
                        returned_rows.append(row_out)
                except RuntimeError as _e:
                    if any(kw in str(_e) for kw in ("UNIQUE", "NOT NULL", "CHECK",
                                                     "FOREIGN KEY", "constraint")):
                        pass
                    else:
                        raise
            elif conflict_action == "REPLACE":
                _remove_conflicting_rows(db, meta, parsed)
                row_out = db.insert(stmt["table"], parsed)
                if _has_ins_trig:
                    fire_triggers(db, stmt["table"], "AFTER", "INSERT", row_out, None)
                if returning_cols:
                    returned_rows.append(row_out)
            elif conflict_action == "UPDATE":
                try:
                    row_out = db.insert(stmt["table"], parsed)
                    if _has_ins_trig:
                        fire_triggers(db, stmt["table"], "AFTER", "INSERT", row_out, None)
                    if returning_cols:
                        returned_rows.append(row_out)
                except RuntimeError as _e:
                    if any(kw in str(_e) for kw in ("UNIQUE", "NOT NULL", "CHECK")):
                        _apply_on_conflict_update(db, meta, parsed, on_conflict_set)
                    else:
                        raise
            else:
                row_out = db.insert(stmt["table"], parsed)
                if _has_ins_trig:
                    fire_triggers(db, stmt["table"], "AFTER", "INSERT", row_out, None)
                if returning_cols:
                    returned_rows.append(row_out)
        n = len(stmt["rows"])
        if returning_cols:
            projected = [{c: r.get(c) for c in returning_cols} for r in returned_rows]
            return _format_rows(projected, returning_cols)
        return f"{n} row{'s' if n != 1 else ''} inserted."

    if op == "INSERT_SELECT":
        src_rows = _rows_for_stmt(stmt["select"], db, stmt.get("ctes"))
        col_names = stmt.get("col_names")
        meta = db._meta(stmt["table"])
        target_cols = [c.name for c in meta.schema.columns]
        _has_ins_trig2 = has_triggers(db, stmt["table"], "INSERT")
        for src_row in src_rows:
            if col_names:
                row_vals = list(src_row.values())
                data: dict[str, Any] = {n: (row_vals[i] if i < len(row_vals) else None)
                                        for i, n in enumerate(col_names)}
            else:
                src_keys = list(src_row.keys())
                # Use key matching when source keys align with target columns;
                # fall back to positional mapping otherwise (e.g. SELECT literals)
                if all(k in target_cols for k in src_keys):
                    data = dict(src_row)
                else:
                    src_vals = list(src_row.values())
                    data = {target_cols[i]: src_vals[i]
                            for i in range(min(len(target_cols), len(src_vals)))}
            for col in meta.schema.columns:
                if col.name not in data:
                    data[col.name] = col.default
            if _has_ins_trig2:
                fire_triggers(db, stmt["table"], "BEFORE", "INSERT", data, None)
            row_out = db.insert(stmt["table"], data)
            if _has_ins_trig2:
                fire_triggers(db, stmt["table"], "AFTER", "INSERT", row_out, None)
        n = len(src_rows)
        return f"{n} row{'s' if n != 1 else ''} inserted."

    if op in ("SELECT", "SELECT_NOFROM", "JOIN", "SET_OP"):
        rows = _rows_for_stmt(stmt, db)
        cols = list(rows[0].keys()) if rows else None
        return _format_rows(rows, cols)

    if op == "TRUNCATE":
        rows = db.delete(stmt["table"], None)
        n = len(rows)
        return f"Table '{stmt['table']}' truncated ({n} rows deleted)."

    if op == "UPDATE":
        tname = stmt["table"]
        if tname not in db.tables and tname in db.views:
            if not has_instead_of(db, tname, "UPDATE"):
                raise RuntimeError(
                    f"Cannot update view '{tname}' without an INSTEAD OF trigger")
            return _exec_instead_of_update(stmt, db)
        if has_triggers(db, tname, "UPDATE"):
            changed_cols = list(stmt["assignments"].keys())
            old_rows = scan_matching_rows(db, tname, stmt["where"])
            _upd_meta = db._meta(tname)
            for old_row in old_rows:
                new_row = apply_update_row(old_row, stmt["assignments"], _upd_meta.schema)
                fire_triggers(db, tname, "BEFORE", "UPDATE", new_row, old_row, changed_cols)
            rows = db.update(tname, stmt["assignments"], stmt["where"], stmt.get("limit"))
            for old_row in old_rows:
                new_row = apply_update_row(old_row, stmt["assignments"], _upd_meta.schema)
                fire_triggers(db, tname, "AFTER", "UPDATE", new_row, old_row, changed_cols)
        else:
            rows = db.update(tname, stmt["assignments"], stmt["where"], stmt.get("limit"))
        n = len(rows)
        if stmt.get("returning"):
            ret_cols = stmt["returning"]
            return _format_rows([{c: r.get(c) for c in ret_cols} for r in rows], ret_cols)
        return f"{n} row{'s' if n != 1 else ''} updated."

    if op == "DELETE":
        tname = stmt["table"]
        if tname not in db.tables and tname in db.views:
            if not has_instead_of(db, tname, "DELETE"):
                raise RuntimeError(
                    f"Cannot delete from view '{tname}' without an INSTEAD OF trigger")
            return _exec_instead_of_delete(stmt, db)
        if has_triggers(db, tname, "DELETE"):
            old_rows = scan_matching_rows(db, tname, stmt["where"])
            for old_row in old_rows:
                fire_triggers(db, tname, "BEFORE", "DELETE", None, old_row)
            rows = db.delete(tname, stmt["where"], stmt.get("limit"))
            for old_row in old_rows:
                fire_triggers(db, tname, "AFTER", "DELETE", None, old_row)
        else:
            rows = db.delete(tname, stmt["where"], stmt.get("limit"))
        n = len(rows)
        if stmt.get("returning"):
            ret_cols = stmt["returning"]
            return _format_rows([{c: r.get(c) for c in ret_cols} for r in rows], ret_cols)
        return f"{n} row{'s' if n != 1 else ''} deleted."

    raise RuntimeError(f"Unknown op: {op}")


_SCALAR_SQ_RE = re.compile(r'^\(\s*SELECT\b', re.IGNORECASE)


def _would_conflict(schema, existing: dict, new_row: dict) -> bool:
    """Return True if existing row conflicts with new_row on any UNIQUE/PK constraint."""
    for col in schema.columns:
        if not (col.unique or col.primary_key):
            continue
        new_val = new_row.get(col.name)
        if new_val is None:
            continue
        ex_val = existing.get(col.name)
        if col.type == INTEGER:
            try: new_val = int(new_val)
            except (ValueError, TypeError): pass
        elif col.type == REAL:
            try: new_val = float(new_val)
            except (ValueError, TypeError): pass
        if ex_val == new_val:
            return True
    for uc_cols in schema.unique_constraints:
        new_vals = []
        for c in uc_cols:
            v = new_row.get(c)
            col_obj = next((x for x in schema.columns if x.name == c), None)
            if v is not None and col_obj:
                if col_obj.type == INTEGER:
                    try: v = int(v)
                    except (ValueError, TypeError): pass
                elif col_obj.type == REAL:
                    try: v = float(v)
                    except (ValueError, TypeError): pass
            new_vals.append(v)
        if any(v is None for v in new_vals):
            continue
        if [existing.get(c) for c in uc_cols] == new_vals:
            return True
    return False


def _remove_conflicting_rows(db: "Database", meta, new_row: dict) -> None:
    """Delete all rows that would conflict with new_row on UNIQUE/PK constraints."""
    schema = meta.schema
    victims: list[tuple[int, dict]] = []
    for rowid, raw in db._table_btree(meta).scan():
        existing = deserialize_row(schema, db._unpack_row_cell(raw))
        if _would_conflict(schema, existing, new_row):
            victims.append((rowid, existing))
    if not victims:
        return
    db._table_btree(meta).delete({r for r, _ in victims})
    for im in db._indexes_for(schema.name):
        col_types = [next(c.type for c in schema.columns if c.name == n)
                     for n in im.columns]
        idx_keys: set[int] = set()
        for rowid, victim_row in victims:
            vals = [victim_row.get(n) for n in im.columns]
            if all(v is not None for v in vals):
                idx_keys.add(_make_index_key(
                    _encode_composite_key(vals, col_types), rowid))
        db._index_btree(im).delete(idx_keys)


def _apply_on_conflict_update(db: "Database", meta, new_row: dict,
                               assignments: dict[str, str]) -> None:
    """Find the conflicting row and apply SET assignments (excluded.col supported)."""
    import struct
    schema = meta.schema
    for rowid, raw in db._table_btree(meta).scan():
        existing = deserialize_row(schema, db._unpack_row_cell(raw))
        if not _would_conflict(schema, existing, new_row):
            continue
        updated = dict(existing)
        for col_name, val in assignments.items():
            if val.lower().startswith("excluded."):
                src_col = val.split(".", 1)[1]
                updated[col_name] = new_row.get(src_col)
            else:
                col_obj = next((c for c in schema.columns if c.name == col_name), None)
                if col_obj and col_obj.type == INTEGER:
                    try: updated[col_name] = int(val)
                    except (ValueError, TypeError): updated[col_name] = val
                elif col_obj and col_obj.type == REAL:
                    try: updated[col_name] = float(val)
                    except (ValueError, TypeError): updated[col_name] = val
                else:
                    updated[col_name] = val
        db._table_btree(meta).update({rowid: db._pack_row_cell(serialize_row(schema, updated))})
        for im in db._indexes_for(schema.name):
            if not any(c in assignments for c in im.columns):
                continue
            col_types = [next(c.type for c in schema.columns if c.name == n)
                         for n in im.columns]
            itree = db._index_btree(im)
            old_vals = [existing.get(n) for n in im.columns]
            new_vals = [updated.get(n) for n in im.columns]
            if all(v is not None for v in old_vals):
                itree.delete({_make_index_key(
                    _encode_composite_key(old_vals, col_types), rowid)})
            if all(v is not None for v in new_vals):
                itree.insert(
                    _make_index_key(_encode_composite_key(new_vals, col_types), rowid),
                    struct.pack("q", rowid))
        break


def _is_scalar_subquery_col(col: str) -> bool:
    return bool(_SCALAR_SQ_RE.match(col.strip()))


def _eval_scalar_subquery(col_expr: str, outer_row: dict,
                           db: "Database", ctes: dict) -> Any:
    """Execute a scalar subquery column and return its single value."""
    from .where import _instantiate_correlated
    inner_str = col_expr.strip()[1:-1].strip()   # strip outer ( )
    try:
        sub_ast = _parse_tokens(_tokenize(inner_str))
    except Exception:
        return None
    if sub_ast.get("where"):
        sub_ast = {**sub_ast,
                   "where": _instantiate_correlated(sub_ast["where"], outer_row)}
    rows = _rows_for_stmt(sub_ast, db, ctes)
    if not rows:
        return None
    return next(iter(rows[0].values()))


def _translate_where_aliases(where: Any, rev: dict[str, str]) -> Any:
    """Return a copy of the WhereClause tree with alias names replaced by raw column names."""
    if where is None:
        return None
    from .where import WhereClause
    return WhereClause(
        col=rev.get(where.col, where.col),
        op=where.op,
        val=where.val,
        subquery_ast=where.subquery_ast,
        group_clause=_translate_where_aliases(where.group_clause, rev),
        and_clause=_translate_where_aliases(where.and_clause, rev),
        or_clause=_translate_where_aliases(where.or_clause, rev),
    )


def _resolve_alias_refs(stmt: dict, col_aliases: dict[str, str] | None) -> dict:
    """Translate alias names and positional references in ORDER BY / GROUP BY / HAVING."""
    rev = {alias: raw for raw, alias in col_aliases.items()} if col_aliases else {}
    cols_list = stmt.get("columns") or []
    if not rev and not cols_list:
        return stmt
    stmt = dict(stmt)
    if stmt.get("order_by"):
        new_order = []
        for d in stmt["order_by"]:
            c = rev.get(d["col"], d["col"])       # alias → raw column
            try:                                   # positional: ORDER BY 1 → first col
                pos = int(c)
                if 1 <= pos <= len(cols_list):
                    c = cols_list[pos - 1]
            except (ValueError, TypeError):
                pass
            new_order.append({**d, "col": c})
        stmt["order_by"] = new_order
    if stmt.get("group_by"):
        stmt["group_by"] = [rev.get(c, c) for c in stmt["group_by"]]
    if stmt.get("having"):
        stmt["having"] = _translate_where_aliases(stmt["having"], rev)
    return stmt


def _apply_aliases(rows: list[dict], cols: list[str] | None,
                   aliases: dict[str, str] | None
                   ) -> tuple[list[dict], list[str] | None]:
    """Rename row keys and column list according to AS aliases."""
    if not aliases:
        return rows, cols
    rows = [{aliases.get(k, k): v for k, v in r.items()} for r in rows]
    if cols:
        cols = [aliases.get(c, c) for c in cols]
    return rows, cols


def _cell_str(v: Any) -> str:
    return "NULL" if v is None else str(v)


def _format_rows(rows: list[dict], requested_cols: list[str] | None) -> str:
    if not rows:
        return "(no rows)"
    cols   = requested_cols if requested_cols else list(rows[0].keys())
    widths = {c: max(len(c), max(len(_cell_str(r.get(c))) for r in rows))
              for c in cols}
    header = " | ".join(c.ljust(widths[c]) for c in cols)
    sep    = "-+-".join("-" * widths[c] for c in cols)
    body   = "\n".join(
        " | ".join(_cell_str(r.get(c)).ljust(widths[c]) for c in cols)
        for r in rows
    )
    n = len(rows)
    return f"{header}\n{sep}\n{body}\n({n} row{'s' if n != 1 else ''})"
