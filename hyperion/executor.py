import re
import struct
import time
from collections import defaultdict
from typing import Any

from .errors import (HyperionError, NoSuchTableError, NoSuchIndexError, SchemaError,
                     ParseError, InternalError, DataError, ConstraintError,
                     QueryTimeoutError, ReadOnlyError, TooManyRowsError)


class RowResult:
    """Return type for executor ops that produce rows (PRAGMA, RETURNING, SELECT).

    Carries structured data so callers can choose between fetchable rows (cursor)
    and formatted display (REPL).  ``rowcount`` is -1 for read-only ops; for DML
    with RETURNING it is the number of rows affected.
    """
    __slots__ = ("rows", "columns", "rowcount")

    def __init__(self, rows: list, columns: list, rowcount: int = -1) -> None:
        self.rows     = rows
        self.columns  = columns
        self.rowcount = rowcount

    def __iter__(self):
        return iter(self.rows)

    _ROW_COUNT_RE = __import__("re").compile(r"^\((\d+) rows?\)$")

    def __contains__(self, item) -> bool:
        """Support `value in result` — checks exact match, string repr, and column names.

        Preserves backward compatibility with code that did ``"Alice" in execute(stmt, db)``
        when execute() used to return a formatted string.
        """
        item_str = str(item)
        # "(2 rows)" / "(1 row)" — row-count check
        m = self._ROW_COUNT_RE.match(item_str)
        if m and int(m.group(1)) == len(self.rows):
            return True
        # Column names (e.g. "id" in EXPLAIN result)
        if item_str in self.columns:
            return True
        for row in self.rows:
            for v in row.values():
                if v == item:
                    return True
                if item_str == str(v):
                    return True
                # "75000" matches 75000.0
                if isinstance(v, float) and v == int(v) and item_str == str(int(v)):
                    return True
                # substring match in string values (e.g. "SEARCH TABLE t" in EXPLAIN detail)
                if isinstance(v, str) and item_str in v:
                    return True
        return False

    def __eq__(self, other) -> bool:
        if isinstance(other, RowResult):
            return self.rows == other.rows and self.columns == other.columns
        if other == "(no rows)" and len(self.rows) == 0:
            return True
        return NotImplemented

    def __hash__(self):
        return id(self)

    def __len__(self) -> int:
        return len(self.rows)

    def __bool__(self) -> bool:
        return True  # non-empty result set or empty — both truthy as a result object

    def __repr__(self) -> str:
        return f"RowResult(columns={self.columns!r}, rows={len(self.rows)})"


class ReadOnlyError(RuntimeError):
    """Raised when a write operation is attempted on a read-only Database."""


class TooManyRowsError(RuntimeError):
    """Raised when a query result exceeds the configured max_rows limit."""


_WRITE_OPS = frozenset({
    "INSERT", "INSERT_SELECT", "UPDATE", "DELETE", "TRUNCATE",
    "CREATE_TABLE", "CREATE_TABLE_AS_SELECT", "DROP_TABLE",
    "CREATE_COLUMN_TABLE",
    "CREATE_PUBLICATION", "DROP_PUBLICATION",
    "CREATE_SUBSCRIPTION", "DROP_SUBSCRIPTION",
    "CREATE_INDEX", "DROP_INDEX",
    "CREATE_VIEW", "DROP_VIEW",
    "CREATE_TRIGGER", "DROP_TRIGGER",
    "ALTER_ADD_COLUMN", "ALTER_DROP_COLUMN",
    "ALTER_RENAME_COLUMN", "ALTER_RENAME_TABLE", "ALTER_ALTER_COLUMN",
    "ALTER_ENABLE_RLS", "ALTER_DISABLE_RLS",
    "CREATE_POLICY", "DROP_POLICY",
    "CREATE_EVENT", "DROP_EVENT", "ALTER_EVENT",
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
                         hyperion_schema_meta_rows as _hyperion_schema_meta_rows,
                         integrity_check as _integrity_check,
                         explain_plan as _explain_plan)
from .optimizer import (find_eq_index as _find_eq_index, probe_index as _probe_index,
                        optimize_join, invalidate_row_count as _invalidate_rc)
from .parser import _parse_tokens, _tokenize
from .schema import deserialize_row, serialize_row
from .constants import INTEGER, REAL, TEXT, DEFAULT_TEXT_SIZE
from .query import _project_row, _parse_agg as _q_parse_agg
from .triggers import (fire_triggers, has_triggers, has_instead_of,
                       scan_matching_rows, apply_update_row)
from .where import _instantiate_correlated

_DEFAULT_CONST_EXPRS = frozenset({
    "TRUE", "FALSE", "CURRENT_TIMESTAMP", "CURRENT_DATE", "CURRENT_TIME"
})

def _eval_default(val: Any) -> Any:
    """Evaluate a column default value, resolving SQL constants at insert time."""
    if not isinstance(val, str):
        return val
    if val.upper() in _DEFAULT_CONST_EXPRS or "(" in val or " " in val:
        try:
            return eval_expr(val, {})
        except Exception:
            pass
    return val


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
            # SQL default: ORDER BY with no explicit frame → ROWS UNBOUNDED PRECEDING TO CURRENT ROW
            if frame is None and ob_spec:
                frame = {"mode": "ROWS",
                         "lo": ("UNBOUNDED", "PRECEDING"),
                         "hi": ("CURRENT", "ROW")}
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
    # Derive canonical column names from base case (used when no explicit col aliases)
    base_keys: list[str] = list(working[0].keys()) if working else []
    for row in working:
        k = _row_key(row)
        if union_all or k not in seen:
            accumulated.append(row)
            seen.append(k)

    def _normalize_to_base(row: dict) -> dict:
        """Rename recursive step output columns to match base case column names."""
        if col_aliases or not base_keys:
            return row
        vals = list(row.values())
        return {base_keys[i]: vals[i] for i in range(min(len(base_keys), len(vals)))}

    max_iterations = 1000
    for _ in range(max_iterations):
        if not working:
            break
        step_ctes = {**ctes, cte_key: {"op": "INLINE_ROWS", "rows": working}}
        raw = _rows_for_stmt(cte_def["recursive"], db, step_ctes)
        working = [_normalize_to_base(_apply_aliases(r)) for r in raw]
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
    lat_sub     = join_info.get("lateral_subquery")

    # Normalise on_left/on_right so that on_left is always the column from the
    # LEFT combined rows and on_right is always the column from the RIGHT (new)
    # table.  The parser sets them by position in the ON "=" expression, which
    # can place the right-table column on the left side (e.g. ON i.col = o.col
    # where i is the new right table).
    if on_left and on_right:
        left_prefix  = on_left.split(".")[0]  if "." in on_left  else ""
        right_prefix = on_right.split(".")[0] if "." in on_right else ""
        # If the on_left prefix matches the right_alias, they need to be swapped.
        if left_prefix == right_alias and right_prefix != right_alias:
            on_left, on_right = on_right, on_left

    rcol = on_right.split(".")[-1] if on_right else None

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
        if rmeta.storage_type == "column":
            right_rows = [row for _, row in db._table_btree(rmeta).scan_rows()]
        else:
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
        if meta.storage_type == "column":
            for _, row in db._table_btree(meta).scan_rows():
                _check_timeout(db)
                raw.append(row)
        else:
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
            inst = {**_lat_sub,
                    "where": _instantiate_correlated(_lat_sub.get("where"), lr),
                    "_outer_row": lr}
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
    from .expr import _tls
    _tls.user_funcs = db._user_funcs
    _tls.user_aggs  = db._user_aggs
    _tls.eval_db    = db
    _check_timeout(db)
    ctes = {**(ctes or {}), **(stmt.get("ctes") or {})}
    _prev_active_ctes = getattr(db, '_active_ctes', None)
    if ctes:
        db._active_ctes = {**(_prev_active_ctes or {}), **ctes}
    try:
        return _rows_for_stmt_inner(stmt, db, ctes, op=stmt["op"])
    finally:
        if _prev_active_ctes is None:
            db.__dict__.pop('_active_ctes', None)
        else:
            db._active_ctes = _prev_active_ctes


def _rows_for_stmt_inner(stmt: dict, db: "Database", ctes: dict, op: str) -> list[dict]:
    if op == "INLINE_ROWS":
        return stmt["rows"]
    if op == "RECURSIVE_CTE":
        return _exec_recursive_cte(stmt, db, ctes)
    if op == "SELECT_NOFROM":
        col_aliases = stmt.get("col_aliases") or {}
        outer = stmt.get("_outer_row") or {}
        result = {col: eval_expr(col, outer) for col in (stmt.get("columns") or [])}
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
            if s.get("group_by") or any(_q_parse_agg(c) for c in stmt_cols if c != "*"):
                raw_stmt = {**s, "columns": None, "order_by": [], "limit": None, "offset": None}
                raw_rows = _exec_derived_table(raw_stmt, db, ctes)
                rows = _apply_groupby_agg(raw_rows, s.get("columns"), s.get("group_by"),
                                          s.get("having"), db)
                rows = _apply_order_limit(rows, s.get("order_by"), s.get("limit"), s.get("offset"))
            else:
                rows = _exec_derived_table(s, db, ctes)
        elif tbl == "_hyperion_master":
            rows = _exec_cte_select(s, {"op": "INLINE_ROWS",
                                        "rows": _hyperion_master_rows(db)}, db, ctes)
        elif tbl == "_hyperion_schema_meta":
            rows = _exec_cte_select(s, {"op": "INLINE_ROWS",
                                        "rows": _hyperion_schema_meta_rows(db)}, db, ctes)
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
        stmt_cols_j = s.get("columns") or []
        has_agg  = (group_by or (s.get("having") is not None) or any(
            _q_parse_agg(c) for c in stmt_cols_j if c != "*"))
        has_window_j = any(_WINDOW_RE.search(c) or _WINDOW_NAMED_RE.search(c)
                           for c in stmt_cols_j if c != "*")
        multi_cond_on = s.get("on_clause") is not None and s.get("on_left") is None
        has_lateral = (rtbl == "__lateral__" or s.get("lateral_subquery") is not None
                       or any(ej.get("lateral_subquery") for ej in (s.get("extra_joins") or [])))
        if (ltbl in ctes or rtbl in ctes or ltbl in db.views or rtbl in db.views
                or _JSON_EACH_RE.match(ltbl) or _JSON_EACH_RE.match(rtbl)
                or has_agg or has_window_j or multi_cond_on or has_lateral):
            raw_stmt = {**s, "columns": None, "order_by": [], "limit": None, "offset": None}
            raw_rows = _exec_in_memory_join(raw_stmt, db, ctes)
            for ej in (s.get("extra_joins") or []):
                raw_rows = _exec_extra_join(raw_rows, ej, db, ctes)
            if has_agg:
                raw_rows = _apply_groupby_agg(raw_rows, s.get("columns"),
                                              group_by, s.get("having"), db)
            elif has_window_j:
                nw_j = stmt.get("named_windows") or {}
                raw_rows = [_normalize_row(r) for r in raw_rows]
                raw_rows = _apply_window_functions(raw_rows, stmt_cols_j, nw_j)
                raw_rows = [_project_row(r, stmt_cols_j) for r in raw_rows] if stmt_cols_j else raw_rows
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
        rows  = _apply_set_op(stmt["set_op"], stmt.get("all", False), left, right)
        return _apply_order_limit(rows, stmt.get("order_by"), stmt.get("limit"), stmt.get("offset"))
    raise InternalError(f"Expected SELECT/JOIN/SET_OP, got '{op}'")


def _iter_rows_for_stmt(stmt: dict, db: "Database",
                        ctes: dict | None = None):
    """Streaming SELECT: yields rows one at a time without building a full list.

    For simple table scans (real table, no ORDER BY / GROUP BY / DISTINCT /
    aggregates / window functions / scalar subqueries) rows are yielded
    directly from the B-tree so the caller never holds the entire result set in
    memory.  All other query shapes fall back to the fully-materialised
    _rows_for_stmt path and then yield from that list.
    """
    from .expr import _tls as _expr_tls
    _expr_tls.user_funcs = db._user_funcs
    _expr_tls.user_aggs  = db._user_aggs
    _expr_tls.eval_db    = db
    _check_timeout(db)
    merged_ctes = {**(ctes or {}), **(stmt.get("ctes") or {})}
    if merged_ctes:
        _prev = getattr(db, '_active_ctes', None)
        db._active_ctes = {**(_prev or {}), **merged_ctes}
    op = stmt.get("op", "")

    if op == "SELECT" and not stmt.get("subquery_from"):
        tbl = stmt.get("table") or ""
        stmt_cols = stmt.get("columns") or []
        if (tbl
                and tbl not in merged_ctes
                and tbl not in db.views
                and tbl != "_hyperion_master"
                and tbl != "_hyperion_schema_meta"
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
            _tbl_tree = db._table_btree(meta)
            _row_iter = (_tbl_tree.scan_rows() if meta.storage_type == "column"
                         else ((rid, deserialize_row(schema, db._unpack_row_cell(raw)))
                               for rid, raw in _tbl_tree.scan()))
            rls = meta.rls_enabled and not db._is_superuser
            for _, row in _row_iter:
                _check_timeout(db)
                if where and not where.evaluate(row, db):
                    continue
                if rls and not db._rls_allowed(tbl, row):
                    continue
                if skipped < offset:
                    skipped += 1
                    continue
                if limit is not None and count >= limit:
                    return
                projected = _project_row(row, cols) if cols else row
                if col_aliases:
                    projected = {col_aliases.get(k, k): v
                                 for k, v in projected.items()}
                yield projected
                count += 1
            return

    yield from _rows_for_stmt(stmt, db, merged_ctes)


def _is_unique_index(idx_name: str, idx_meta, db: Database) -> bool:
    if idx_name.startswith("_pk_"):
        return True
    if idx_meta.unique:
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
        val = 1 if db.fk_enforcement else 0
        return RowResult([{"foreign_keys": val}], ["foreign_keys"])

    if name == "table_info":
        tname = stmt.get("arg") or ""
        if tname not in db.tables:
            cols = ["cid", "name", "type", "notnull", "dflt_value", "pk"]
            return RowResult([], cols)
        schema = db._meta(tname).schema
        pk_cols = set(schema.primary_key_columns or [])
        rows = []
        for cid, col in enumerate(schema.columns):
            is_pk = 1 if (col.primary_key or col.name in pk_cols) else 0
            rows.append({
                "cid": cid, "name": col.name, "type": col.type,
                "notnull": 0 if col.nullable else 1,
                "dflt_value": (f"'{col.default.replace(chr(39), chr(39) * 2)}'"
                               if isinstance(col.default, str) else col.default),
                "pk": is_pk,
            })
        cols = ["cid", "name", "type", "notnull", "dflt_value", "pk"]
        return RowResult(rows, cols)

    if name == "index_list":
        tname = stmt.get("arg") or ""
        rows = []
        for seq, (idx_name, idx_meta) in enumerate(
                (n, m) for n, m in db.indexes.items() if m.table_name == tname):
            rows.append({"seq": seq, "name": idx_name,
                         "unique": 1 if _is_unique_index(idx_name, idx_meta, db) else 0})
        return RowResult(rows, ["seq", "name", "unique"])

    if name == "index_info":
        idx_name = stmt.get("arg") or ""
        if idx_name not in db.indexes:
            raise NoSuchIndexError(f"No such index: '{idx_name}'")
        idx_meta = db.indexes[idx_name]
        schema = db._meta(idx_meta.table_name).schema
        col_cids = {c.name: i for i, c in enumerate(schema.columns)}
        rows = [{"seqno": i, "cid": col_cids.get(col, -1), "name": col}
                for i, col in enumerate(idx_meta.columns)]
        return RowResult(rows, ["seqno", "cid", "name"])

    if name == "integrity_check":
        results = _integrity_check(db)
        rows = [{"integrity_check": msg} for msg in results]
        return RowResult(rows, ["integrity_check"])

    raise ParseError(f"Unknown PRAGMA: '{name}'")


def _execute_analyze(stmt: dict, db: Database) -> str:
    """Scan tables and persist row count + per-column NDV statistics to the catalog."""
    target = stmt.get("table")
    tables_to_analyze = ([target] if target and target in db.tables
                         else list(db.tables.keys()))

    if target and target not in db.tables:
        raise NoSuchTableError(f"No such table: '{target}'")

    for tname in tables_to_analyze:
        meta   = db._meta(tname)
        schema = meta.schema
        col_names = [c.name for c in schema.columns]

        row_count = 0
        distinct: dict[str, set] = {c: set() for c in col_names}

        if meta.storage_type == "column":
            for _, row in db._table_btree(meta).scan_rows():
                row_count += 1
                for c in col_names:
                    val = row.get(c)
                    distinct[c].add(val if val is not None else _ANALYZE_NULL_SENTINEL)
        else:
            for _, raw in db._table_btree(meta).scan():
                row = deserialize_row(schema, db._unpack_row_cell(raw))
                row_count += 1
                for c in col_names:
                    val = row.get(c)
                    distinct[c].add(val if val is not None else _ANALYZE_NULL_SENTINEL)

        db._catalog.stats[tname] = {
            "row_count": row_count,
            "columns": {c: {"ndv": len(distinct[c])} for c in col_names},
        }
        db._catalog.mark_stats_dirty()

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

    from .expr import _tls
    _tls.user_funcs = db._user_funcs
    _tls.user_aggs  = db._user_aggs
    _tls.eval_db    = db

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
        if stmt.get("analyze"):
            return _exec_explain_analyze(stmt["stmt"], db)
        plan_rows = _explain_plan(stmt["stmt"], db)
        cols = ["id", "parent", "notused", "detail"]
        return RowResult(plan_rows, cols)

    if op == "VACUUM":
        return db.vacuum()

    if op == "SET_ISOLATION_LEVEL":
        db.set_isolation_level(stmt["level"])
        return f"Isolation level set to '{stmt['level']}'."

    if op == "SHOW_TRANSACTIONS":
        import time as _time
        import datetime as _dt
        if db.in_transaction:
            elapsed = _time.time() - (db._txn_start_time or _time.time())
            started = _dt.datetime.fromtimestamp(db._txn_start_time).strftime(
                "%Y-%m-%d %H:%M:%S") if db._txn_start_time else "unknown"
            rows = [{
                "isolation_level": db._isolation_level,
                "started_at":      started,
                "elapsed_s":       round(elapsed, 3),
                "status":          "active",
            }]
        else:
            rows = [{
                "isolation_level": db._isolation_level,
                "started_at":      None,
                "elapsed_s":       None,
                "status":          "idle",
            }]
        return RowResult(rows, ["isolation_level", "started_at", "elapsed_s", "status"])

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
            raise SchemaError(
                f"Cannot determine columns for INSERT on view '{tname}' "
                f"— specify column names explicitly")
    _IIOT_CONST = frozenset({"TRUE", "FALSE", "CURRENT_TIMESTAMP",
                              "CURRENT_DATE", "CURRENT_TIME"})
    count = 0
    for values in stmt["rows"]:
        if len(col_names) != len(values):
            raise DataError(
                f"Column/value mismatch: {len(col_names)} columns, {len(values)} values")
        parsed: dict[str, Any] = {}
        for n, v in zip(col_names, values):
            if v.upper() == "NULL":
                parsed[n] = None
            elif _is_single_string_literal(v):
                parsed[n] = v[1:-1].replace("''", "'")
            elif " " in v or v.upper() in _IIOT_CONST or "(" in v:
                parsed[n] = eval_expr(v, {})
            else:
                parsed[n] = v
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


_SCHEMA_META_COLS = ("object_type", "object_name", "key", "value")


def _meta_parse_val(v: str) -> str | None:
    """Unquote a parser string token to a Python string for _hyperion_schema_meta writes."""
    if v is None or v.upper() == "NULL":
        return None
    if _is_single_string_literal(v):
        return v[1:-1].replace("''", "'")
    return v


def _exec_meta_insert(stmt: dict, db: Database) -> str:
    col_names = list(stmt.get("col_names") or _SCHEMA_META_COLS)
    for values in stmt["rows"]:
        if len(col_names) != len(values):
            raise DataError(
                f"_hyperion_schema_meta expects {len(_SCHEMA_META_COLS)} columns, "
                f"got {len(values)}")
        row = {c: _meta_parse_val(v) for c, v in zip(col_names, values)}
        db.set_meta(row["object_type"], row["object_name"], row["key"], row["value"])
    n = len(stmt["rows"])
    return f"{n} row{'s' if n != 1 else ''} inserted."


def _exec_meta_update(stmt: dict, db: Database) -> str:
    rows = _hyperion_schema_meta_rows(db)
    where = stmt.get("where")
    if where:
        rows = [r for r in rows if where.evaluate(r, db)]
    for row in rows:
        new_row = dict(row)
        for col, val_str in stmt["assignments"].items():
            new_row[col] = _meta_parse_val(str(val_str)) if val_str is not None else None
        if new_row != row:
            db.delete_meta(row["object_type"], row["object_name"], row["key"])
            if new_row.get("value") is not None:
                db.set_meta(new_row["object_type"], new_row["object_name"],
                            new_row["key"], new_row["value"])
    n = len(rows)
    return f"{n} row{'s' if n != 1 else ''} updated."


def _exec_meta_delete(stmt: dict, db: Database) -> str:
    rows = _hyperion_schema_meta_rows(db)
    where = stmt.get("where")
    if where:
        rows = [r for r in rows if where.evaluate(r, db)]
    for row in rows:
        db.delete_meta(row["object_type"], row["object_name"], row["key"])
    n = len(rows)
    return f"{n} row{'s' if n != 1 else ''} deleted."


def _exec_create_table_as_select(stmt: dict, db: Database) -> str:
    from .schema import Schema, Column
    if stmt.get("if_not_exists") and stmt["name"] in db.tables:
        return f"Table '{stmt['name']}' already exists."
    rows = _rows_for_stmt(stmt["select"], db)
    if not rows:
        sel_cols = stmt["select"].get("columns") or []
        columns = [Column(c, TEXT, DEFAULT_TEXT_SIZE) for c in sel_cols if c != "*"]
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
    db.create_table(Schema(name=stmt["name"], columns=columns))
    for row in rows:
        db.insert(stmt["name"], row)
    n = len(rows)
    _invalidate_rc(db, stmt["name"])
    return f"Table '{stmt['name']}' created with {n} row{'s' if n != 1 else ''}."


def _exec_create_table(stmt: dict, db: Database) -> str:
    from .schema import Schema
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
        elif col.unique:
            uq_idx = f"_uq_{stmt['name']}_{col.name}"
            if uq_idx not in db.indexes:
                db.create_index(uq_idx, stmt["name"], [col.name], unique=True)
    if pk_cols and len(pk_cols) > 1:
        pk_idx = f"_pk_{stmt['name']}_{'_'.join(pk_cols)}"
        if pk_idx not in db.indexes:
            db.create_index(pk_idx, stmt["name"], pk_cols)
    return f"Table '{stmt['name']}' created."


def _exec_create_column_table(stmt: dict, db: Database) -> str:
    from .schema import Schema, Column
    if stmt.get("if_not_exists") and stmt["name"] in db.tables:
        return f"Table '{stmt['name']}' already exists."

    # Handle: CREATE COLUMN TABLE t AS SELECT ...
    if "select" in stmt:
        rows = _rows_for_stmt(stmt["select"], db)
        if not rows:
            sel_cols = stmt["select"].get("columns") or []
            columns = [Column(c, TEXT, DEFAULT_TEXT_SIZE) for c in sel_cols if c != "*"]
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
        db.create_table(Schema(name=stmt["name"], columns=columns), storage_type="column")
        for row in rows:
            db.insert(stmt["name"], row)
        n = len(rows)
        _invalidate_rc(db, stmt["name"])
        return f"Column table '{stmt['name']}' created with {n} row{'s' if n != 1 else ''}."

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
                    temporary=stmt.get("temporary", False),
                    storage_type="column")
    for col in stmt["columns"]:
        if col.primary_key:
            pk_idx = f"_pk_{stmt['name']}_{col.name}"
            if pk_idx not in db.indexes:
                db.create_index(pk_idx, stmt["name"], [col.name])
        elif col.unique:
            uq_idx = f"_uq_{stmt['name']}_{col.name}"
            if uq_idx not in db.indexes:
                db.create_index(uq_idx, stmt["name"], [col.name], unique=True)
    if pk_cols and len(pk_cols) > 1:
        pk_idx = f"_pk_{stmt['name']}_{'_'.join(pk_cols)}"
        if pk_idx not in db.indexes:
            db.create_index(pk_idx, stmt["name"], pk_cols)
    return f"Column table '{stmt['name']}' created."


def _exec_show_storage_format(stmt: dict, db: Database) -> "RowResult":
    rows = [
        {"table": name, "storage": meta.storage_type.upper()}
        for name, meta in sorted(db.tables.items())
        if not meta.temporary
    ]
    return RowResult(rows, ["table", "storage"])


def _exec_drop_table(stmt: dict, db: Database) -> str:
    if stmt.get("if_exists") and stmt["name"] not in db.tables:
        return f"Table '{stmt['name']}' does not exist."
    db.drop_table(stmt["name"])
    _invalidate_rc(db, stmt["name"])
    return f"Table '{stmt['name']}' dropped."


def _exec_create_view(stmt: dict, db: Database) -> str:
    db.create_view(stmt["name"], stmt["sql"],
                   if_not_exists=stmt.get("if_not_exists", False),
                   or_replace=stmt.get("or_replace", False))
    return f"View '{stmt['name']}' created."


def _exec_drop_view(stmt: dict, db: Database) -> str:
    db.drop_view(stmt["name"], if_exists=stmt.get("if_exists", False))
    return f"View '{stmt['name']}' dropped."


def _exec_alter_add_column(stmt: dict, db: Database) -> str:
    db.alter_add_column(stmt["table"], stmt["col"])
    return f"Column '{stmt['col'].name}' added to '{stmt['table']}'."


def _exec_alter_drop_column(stmt: dict, db: Database) -> str:
    db.alter_drop_column(stmt["table"], stmt["col_name"])
    return f"Column '{stmt['col_name']}' dropped from '{stmt['table']}'."


def _exec_alter_rename_column(stmt: dict, db: Database) -> str:
    db.alter_rename_column(stmt["table"], stmt["old_name"], stmt["new_name"])
    return f"Column '{stmt['old_name']}' renamed to '{stmt['new_name']}'."


def _exec_alter_rename_table(stmt: dict, db: Database) -> str:
    db.alter_rename_table(stmt["table"], stmt["new_name"])
    return f"Table '{stmt['table']}' renamed to '{stmt['new_name']}'."


def _exec_alter_alter_column(stmt: dict, db: Database) -> str:
    db.alter_column_type(stmt["table"], stmt["col_name"],
                         stmt["new_type"], stmt["new_size"])
    return (f"Column '{stmt['col_name']}' in '{stmt['table']}' "
            f"type changed to {stmt['new_type']}.")


def _exec_create_index(stmt: dict, db: Database) -> str:
    if stmt.get("if_not_exists") and stmt["idx_name"] in db.indexes:
        return f"Index '{stmt['idx_name']}' already exists."
    db.create_index(stmt["idx_name"], stmt["table"], stmt["cols"],
                    unique=stmt.get("unique", False))
    cols_str = ", ".join(stmt["cols"])
    return f"Index '{stmt['idx_name']}' created on {stmt['table']}({cols_str})."


def _exec_drop_index(stmt: dict, db: Database) -> str:
    try:
        db.drop_index(stmt["idx_name"])
    except (NoSuchIndexError, RuntimeError):
        if not stmt.get("if_exists"):
            raise
    return f"Index '{stmt['idx_name']}' dropped."


def _exec_create_trigger(stmt: dict, db: Database) -> str:
    from .catalog import TriggerMeta
    if stmt.get("if_not_exists") and stmt["name"] in db._catalog.triggers:
        return f"Trigger '{stmt['name']}' already exists."
    trig = TriggerMeta(stmt["table"], stmt["timing"], stmt["event"],
                       stmt.get("update_cols", []), stmt.get("when_tokens", []),
                       stmt.get("body_tokens", []))
    db.create_trigger(stmt["name"], trig)
    return f"Trigger '{stmt['name']}' created."


def _exec_drop_trigger(stmt: dict, db: Database) -> str:
    if stmt.get("if_exists") and stmt["name"] not in db._catalog.triggers:
        return f"Trigger '{stmt['name']}' does not exist."
    db.drop_trigger(stmt["name"])
    return f"Trigger '{stmt['name']}' dropped."


def _exec_insert(stmt: dict, db: Database) -> str:
    if stmt["table"] == "_hyperion_schema_meta":
        return _exec_meta_insert(stmt, db)
    if stmt["table"] not in db.tables and stmt["table"] in db.views:
        if not has_instead_of(db, stmt["table"], "INSERT"):
            raise SchemaError(
                f"Cannot insert into view '{stmt['table']}' without an INSTEAD OF trigger")
        return _exec_instead_of_insert(stmt, db)
    meta            = db._meta(stmt["table"])
    col_names       = stmt["col_names"] or [c.name for c in meta.schema.columns]
    for _cn in (stmt["col_names"] or []):
        _cm = next((c for c in meta.schema.columns if c.name == _cn), None)
        if _cm and _cm.is_generated:
            raise ConstraintError(f"Cannot assign to generated column '{_cn}'")
    conflict_action = stmt.get("conflict_action")
    on_conflict_set = stmt.get("on_conflict_set") or {}
    returning_cols  = stmt.get("returning")
    returned_rows: list[dict] = []
    _has_ins_trig = has_triggers(db, stmt["table"], "INSERT")
    for values in stmt["rows"]:
        if len(col_names) != len(values):
            raise DataError(
                f"Column/value mismatch: {len(col_names)} columns, {len(values)} values"
            )
        parsed: dict[str, Any] = {}
        for name, val in zip(col_names, values):
            if val.upper() == "NULL":
                parsed[name] = None
            elif _is_single_string_literal(val):
                parsed[name] = val[1:-1].replace("''", "'")
            elif " " in val or val.upper() in _DEFAULT_CONST_EXPRS or "(" in val:
                parsed[name] = eval_expr(val, {})
            else:
                parsed[name] = val
        for col in meta.schema.columns:
            if col.name not in parsed:
                parsed[col.name] = _eval_default(col.default)
        if _has_ins_trig:
            fire_triggers(db, stmt["table"], "BEFORE", "INSERT", parsed, None)
        if conflict_action == "IGNORE":
            try:
                row_out = db.insert(stmt["table"], parsed)
                if _has_ins_trig:
                    fire_triggers(db, stmt["table"], "AFTER", "INSERT", row_out, None)
                if returning_cols:
                    returned_rows.append(row_out)
            except (ConstraintError, RuntimeError) as _e:
                if isinstance(_e, ConstraintError) or any(
                        kw in str(_e) for kw in ("UNIQUE", "NOT NULL", "CHECK",
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
            except (ConstraintError, RuntimeError) as _e:
                if isinstance(_e, ConstraintError) or any(
                        kw in str(_e) for kw in ("UNIQUE", "NOT NULL", "CHECK")):
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
    _invalidate_rc(db, stmt["table"])
    if returning_cols:
        projected = [{c: r.get(c) for c in returning_cols} for r in returned_rows]
        return RowResult(projected, returning_cols, rowcount=n)
    return f"{n} row{'s' if n != 1 else ''} inserted."


def _exec_insert_select(stmt: dict, db: Database) -> str:
    src_rows = _rows_for_stmt(stmt["select"], db, stmt.get("ctes"))
    col_names = stmt.get("col_names")
    meta = db._meta(stmt["table"])
    target_cols = [c.name for c in meta.schema.columns]
    _has_ins_trig = has_triggers(db, stmt["table"], "INSERT")
    for src_row in src_rows:
        if col_names:
            row_vals = list(src_row.values())
            data: dict[str, Any] = {n: (row_vals[i] if i < len(row_vals) else None)
                                    for i, n in enumerate(col_names)}
        else:
            src_keys = list(src_row.keys())
            if all(k in target_cols for k in src_keys):
                data = dict(src_row)
            else:
                src_vals = list(src_row.values())
                data = {target_cols[i]: src_vals[i]
                        for i in range(min(len(target_cols), len(src_vals)))}
        for col in meta.schema.columns:
            if col.name not in data:
                data[col.name] = _eval_default(col.default)
        if _has_ins_trig:
            fire_triggers(db, stmt["table"], "BEFORE", "INSERT", data, None)
        row_out = db.insert(stmt["table"], data)
        if _has_ins_trig:
            fire_triggers(db, stmt["table"], "AFTER", "INSERT", row_out, None)
    n = len(src_rows)
    _invalidate_rc(db, stmt["table"])
    return f"{n} row{'s' if n != 1 else ''} inserted."


def _exec_select(stmt: dict, db: Database) -> RowResult:
    rows = _rows_for_stmt(stmt, db)
    cols = list(rows[0].keys()) if rows else []
    return RowResult(rows, cols)


def _exec_truncate(stmt: dict, db: Database) -> str:
    rows = db.delete(stmt["table"], None)
    n = len(rows)
    _invalidate_rc(db, stmt["table"])
    db._meta(stmt["table"]).next_key = 1
    return f"Table '{stmt['table']}' truncated ({n} rows deleted)."


def _exec_update(stmt: dict, db: Database) -> str:
    tname = stmt["table"]
    if tname == "_hyperion_schema_meta":
        return _exec_meta_update(stmt, db)
    if tname not in db.tables and tname in db.views:
        if not has_instead_of(db, tname, "UPDATE"):
            raise SchemaError(
                f"Cannot update view '{tname}' without an INSTEAD OF trigger")
        return _exec_instead_of_update(stmt, db)
    _update_conflict = (stmt.get("conflict_action") or "").upper()
    try:
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
    except ConstraintError:
        if _update_conflict != "IGNORE":
            raise
        rows = []
    n = len(rows)
    _invalidate_rc(db, tname)
    if stmt.get("returning"):
        ret_cols = stmt["returning"]
        return RowResult([{c: r.get(c) for c in ret_cols} for r in rows],
                         ret_cols, rowcount=n)
    return f"{n} row{'s' if n != 1 else ''} updated."


def _exec_delete(stmt: dict, db: Database) -> str:
    tname = stmt["table"]
    if tname == "_hyperion_schema_meta":
        return _exec_meta_delete(stmt, db)
    if tname not in db.tables and tname in db.views:
        if not has_instead_of(db, tname, "DELETE"):
            raise SchemaError(
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
    _invalidate_rc(db, tname)
    if stmt.get("returning"):
        ret_cols = stmt["returning"]
        return RowResult([{c: r.get(c) for c in ret_cols} for r in rows],
                         ret_cols, rowcount=n)
    return f"{n} row{'s' if n != 1 else ''} deleted."


def _exec_create_publication(stmt: dict, db: Database) -> str:
    name   = stmt["name"]
    tables = stmt.get("tables", [])
    db.create_publication(name, tables)
    desc = "FOR ALL TABLES" if not tables else f"FOR TABLE {', '.join(tables)}"
    return f"Publication '{name}' created ({desc})."


def _exec_drop_publication(stmt: dict, db: Database) -> str:
    db.drop_publication(stmt["name"], if_exists=stmt.get("if_exists", False))
    return f"Publication '{stmt['name']}' dropped."


def _exec_create_subscription(stmt: dict, db: Database) -> str:
    name   = stmt["name"]
    conn   = stmt["connection"]
    pub    = stmt["publication"]
    db.create_subscription(name, conn, pub)
    return f"Subscription '{name}' created (connecting to '{conn}', publication '{pub}')."


def _exec_drop_subscription(stmt: dict, db: Database) -> str:
    db.drop_subscription(stmt["name"], if_exists=stmt.get("if_exists", False))
    return f"Subscription '{stmt['name']}' dropped."


def _exec_show_publications(stmt: dict, db: Database) -> RowResult:
    rows = [
        {"name": p.name,
         "tables": ", ".join(p.tables) if p.tables else "(all tables)"}
        for p in db._catalog.publications.values()
    ]
    return RowResult(rows, ["name", "tables"])


def _exec_show_subscriptions(stmt: dict, db: Database) -> RowResult:
    rows = [
        {"name": s.name, "connection": s.connection,
         "publication": s.publication, "last_lsn": s.last_lsn}
        for s in db._catalog.subscriptions.values()
    ]
    return RowResult(rows, ["name", "connection", "publication", "last_lsn"])


def _exec_create_physical_subscription(stmt: dict, db: Database) -> str:
    db.create_physical_subscription(stmt["name"], stmt["connection"],
                                    if_not_exists=stmt.get("if_not_exists", False))
    return f"Physical subscription '{stmt['name']}' created."


def _exec_drop_physical_subscription(stmt: dict, db: Database) -> str:
    db.drop_physical_subscription(stmt["name"], if_exists=stmt.get("if_exists", False))
    return f"Physical subscription '{stmt['name']}' dropped."


def _exec_start_slave(stmt: dict, db: Database) -> str:
    return db.start_slave(stmt.get("name"))


def _exec_stop_slave(stmt: dict, db: Database) -> str:
    return db.stop_slave(stmt.get("name"))


def _exec_show_master_status(stmt: dict, db: Database) -> RowResult:
    rows = db.show_master_status()
    cols = ["binlog_pos", "db_size", "wal_size"]
    return RowResult(rows, cols)


def _exec_show_slave_status(stmt: dict, db: Database) -> RowResult:
    rows = db.show_slave_status()
    cols = ["name", "connection", "last_lsn", "status", "last_error", "last_sync"]
    return RowResult(rows, cols)


def _exec_show_binlog(stmt: dict, db: Database) -> RowResult:
    rows = db.show_binlog()
    cols = ["page_num", "catalog_lsn"]
    return RowResult(rows, cols)


# ── Event Scheduler handlers ─────────────────────────────────────────────────

def _exec_create_event(stmt: dict, db: Database) -> str:
    db.create_event(
        name=stmt["name"],
        schedule_type=stmt["schedule_type"],
        interval_seconds=stmt["interval_seconds"],
        at_time=stmt["at_time"],
        sql=stmt["sql"],
        if_not_exists=stmt.get("if_not_exists", False),
    )
    return f"Event '{stmt['name']}' created."


def _exec_drop_event(stmt: dict, db: Database) -> str:
    db.drop_event(stmt["name"], if_exists=stmt.get("if_exists", False))
    return f"Event '{stmt['name']}' dropped."


def _exec_alter_event(stmt: dict, db: Database) -> str:
    if stmt["action"] == "ENABLE":
        db.enable_event(stmt["name"])
    else:
        db.disable_event(stmt["name"])
    return f"Event '{stmt['name']}' {stmt['action'].lower()}d."


def _exec_show_events(stmt: dict, db: Database) -> RowResult:
    rows = db.show_events()
    cols = ["name", "schedule", "sql", "enabled", "last_run"]
    return RowResult(rows, cols)


def _exec_show_recovery_status(stmt: dict, db: Database) -> RowResult:
    from .pager import Pager, MemoryPager
    pager = db._pager
    if isinstance(pager, MemoryPager):
        row = {
            "wal_file":            "memory",
            "wal_exists":          False,
            "wal_size_bytes":      0,
            "recovery_applied":    False,
            "current_lsn":         0,
            "checkpoint_lsn":      0,
            "pages_since_checkpoint": 0,
        }
    else:
        wal_path = pager._path.with_suffix(".wal")
        wal_exists = wal_path.exists()
        wal_size = wal_path.stat().st_size if wal_exists else 0
        row = {
            "wal_file":            str(wal_path),
            "wal_exists":          wal_exists,
            "wal_size_bytes":      wal_size,
            "recovery_applied":    getattr(pager, "_recovery_applied", False),
            "current_lsn":         pager._phys_current_lsn,
            "checkpoint_lsn":      pager._phys_checkpoint_lsn,
            "pages_since_checkpoint": getattr(pager, "_pages_since_ckpt", 0),
        }
    cols = list(row.keys())
    return RowResult([row], cols)


# ── Row-Level Security handlers ───────────────────────────────────────────────

def _exec_alter_enable_rls(stmt: dict, db: Database) -> str:
    db.enable_rls(stmt["table"])
    return f"Row-level security enabled on '{stmt['table']}'."


def _exec_alter_disable_rls(stmt: dict, db: Database) -> str:
    db.disable_rls(stmt["table"])
    return f"Row-level security disabled on '{stmt['table']}'."


def _exec_create_policy(stmt: dict, db: Database) -> str:
    db.create_policy(stmt["name"], stmt["table"], stmt["using_expr"],
                     if_not_exists=stmt.get("if_not_exists", False))
    return f"Policy '{stmt['name']}' created."


def _exec_drop_policy(stmt: dict, db: Database) -> str:
    db.drop_policy(stmt["name"], stmt["table"],
                   if_exists=stmt.get("if_exists", False))
    return f"Policy '{stmt['name']}' dropped."


_DISPATCH: dict[str, Any] = {
    "ANALYZE":                  _execute_analyze,
    "CREATE_TABLE_AS_SELECT":   _exec_create_table_as_select,
    "CREATE_TABLE":             _exec_create_table,
    "CREATE_COLUMN_TABLE":      _exec_create_column_table,
    "SHOW_STORAGE_FORMAT":      _exec_show_storage_format,
    "DROP_TABLE":               _exec_drop_table,
    "CREATE_VIEW":              _exec_create_view,
    "DROP_VIEW":                _exec_drop_view,
    "ALTER_ADD_COLUMN":         _exec_alter_add_column,
    "ALTER_DROP_COLUMN":        _exec_alter_drop_column,
    "ALTER_RENAME_COLUMN":      _exec_alter_rename_column,
    "ALTER_RENAME_TABLE":       _exec_alter_rename_table,
    "ALTER_ALTER_COLUMN":       _exec_alter_alter_column,
    "CREATE_INDEX":             _exec_create_index,
    "DROP_INDEX":               _exec_drop_index,
    "CREATE_TRIGGER":           _exec_create_trigger,
    "DROP_TRIGGER":             _exec_drop_trigger,
    "CREATE_PUBLICATION":       _exec_create_publication,
    "DROP_PUBLICATION":         _exec_drop_publication,
    "CREATE_SUBSCRIPTION":      _exec_create_subscription,
    "DROP_SUBSCRIPTION":        _exec_drop_subscription,
    "SHOW_PUBLICATIONS":              _exec_show_publications,
    "SHOW_SUBSCRIPTIONS":             _exec_show_subscriptions,
    "CREATE_PHYSICAL_SUBSCRIPTION":   _exec_create_physical_subscription,
    "DROP_PHYSICAL_SUBSCRIPTION":     _exec_drop_physical_subscription,
    "START_SLAVE":                    _exec_start_slave,
    "STOP_SLAVE":                     _exec_stop_slave,
    "SHOW_MASTER_STATUS":             _exec_show_master_status,
    "SHOW_SLAVE_STATUS":              _exec_show_slave_status,
    "SHOW_BINLOG":                    _exec_show_binlog,
    "ALTER_ENABLE_RLS":               _exec_alter_enable_rls,
    "ALTER_DISABLE_RLS":              _exec_alter_disable_rls,
    "CREATE_POLICY":                  _exec_create_policy,
    "DROP_POLICY":                    _exec_drop_policy,
    "CREATE_EVENT":                   _exec_create_event,
    "DROP_EVENT":                     _exec_drop_event,
    "ALTER_EVENT":                    _exec_alter_event,
    "SHOW_EVENTS":                    _exec_show_events,
    "SHOW_RECOVERY_STATUS":           _exec_show_recovery_status,
    "INSERT":                   _exec_insert,
    "INSERT_SELECT":            _exec_insert_select,
    "SELECT":                   _exec_select,
    "SELECT_NOFROM":            _exec_select,
    "JOIN":                     _exec_select,
    "SET_OP":                   _exec_select,
    "TRUNCATE":                 _exec_truncate,
    "UPDATE":                   _exec_update,
    "DELETE":                   _exec_delete,
}


def _exec_explain_analyze(inner_stmt: dict, db: "Database") -> RowResult:
    """Run EXPLAIN ANALYZE: execute the statement, annotate plan nodes with
    actual row counts and elapsed time."""
    import time as _time
    import re as _re

    # Static plan first
    plan_rows = _explain_plan(inner_stmt, db)

    # Execute and time
    t0 = _time.perf_counter()
    actual_rows = 0
    try:
        result = _execute_inner(inner_stmt, db)
        if isinstance(result, RowResult):
            actual_rows = len(result.rows)
        elif isinstance(result, str):
            m = _re.search(r'\b(\d+)\b', result)
            if m:
                actual_rows = int(m.group(1))
    except Exception:
        pass
    elapsed_ms = round((_time.perf_counter() - t0) * 1000, 3)

    # Annotate: root node gets total rows + total time; sub-nodes get per-node 0
    # (deep per-node instrumentation would require executor refactor)
    for i, row in enumerate(plan_rows):
        if i == 0:
            row["actual_rows"]    = actual_rows
            row["actual_time_ms"] = elapsed_ms
        else:
            row["actual_rows"]    = 0
            row["actual_time_ms"] = 0.0
        row["detail"] = (
            f"{row['detail']} "
            f"(actual rows={row['actual_rows']}, "
            f"time={row['actual_time_ms']}ms)"
        )

    cols = ["id", "parent", "notused", "detail", "actual_rows", "actual_time_ms"]
    return RowResult(plan_rows, cols)


def _execute_inner(stmt: dict, db: Database) -> str:
    handler = _DISPATCH.get(stmt["op"])
    if handler is None:
        raise InternalError(f"Unknown op: {stmt['op']}")
    return handler(stmt, db)


_SCALAR_SQ_RE = re.compile(r'^\(\s*SELECT\b', re.IGNORECASE)


def _would_conflict(schema, existing: dict, new_row: dict,
                    db: "Database | None" = None) -> bool:
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
    # Check user-created UNIQUE indexes
    if db is not None:
        for idx_meta in db._catalog.indexes.values():
            if idx_meta.table_name != schema.name or not idx_meta.unique:
                continue
            new_vals = []
            for c in idx_meta.columns:
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
            ex_vals = [existing.get(c) for c in idx_meta.columns]
            if ex_vals == new_vals:
                return True
    return False


def _remove_conflicting_rows(db: "Database", meta, new_row: dict) -> None:
    """Delete all rows that would conflict with new_row on UNIQUE/PK constraints."""
    schema = meta.schema
    victims: list[tuple[int, dict]] = []
    if meta.storage_type == "column":
        for rowid, existing in db._table_btree(meta).scan_rows():
            if _would_conflict(schema, existing, new_row, db):
                victims.append((rowid, existing))
    else:
        for rowid, raw in db._table_btree(meta).scan():
            existing = deserialize_row(schema, db._unpack_row_cell(raw))
            if _would_conflict(schema, existing, new_row, db):
                victims.append((rowid, existing))
    if not victims:
        return
    victim_ids = {r for r, _ in victims}
    if meta.storage_type == "column":
        db._table_btree(meta).apply_deletes(victim_ids)
    else:
        db._table_btree(meta).delete(victim_ids)
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
    is_col  = (meta.storage_type == "column")
    _schema = meta.schema

    if is_col:
        scan_iter = ((rid, row) for rid, row in db._table_btree(meta).scan_rows())
    else:
        scan_iter = (
            (rid, deserialize_row(_schema, db._unpack_row_cell(raw)))
            for rid, raw in db._table_btree(meta).scan()
        )

    for rowid, existing in scan_iter:
        if not _would_conflict(_schema, existing, new_row, db):
            continue
        updated = dict(existing)
        for col_name, val in assignments.items():
            if isinstance(val, str) and val.lower().startswith("excluded."):
                src_col = val.split(".", 1)[1]
                updated[col_name] = new_row.get(src_col)
            else:
                col_obj = next((c for c in _schema.columns if c.name == col_name), None)
                if col_obj and col_obj.type == INTEGER:
                    try:
                        updated[col_name] = int(val)
                    except (ValueError, TypeError):
                        try: updated[col_name] = int(eval_expr(str(val), updated))
                        except Exception: updated[col_name] = val
                elif col_obj and col_obj.type == REAL:
                    try:
                        updated[col_name] = float(val)
                    except (ValueError, TypeError):
                        try: updated[col_name] = float(eval_expr(str(val), updated))
                        except Exception: updated[col_name] = val
                else:
                    try: updated[col_name] = eval_expr(str(val), updated)
                    except Exception: updated[col_name] = val
        if is_col:
            db._table_btree(meta).apply_updates({rowid: updated}, set(assignments.keys()))
        else:
            db._table_btree(meta).update(
                {rowid: db._pack_row_cell(serialize_row(_schema, updated))})
        for im in db._indexes_for(_schema.name):
            if not any(c in assignments for c in im.columns):
                continue
            col_types = [next(c.type for c in _schema.columns if c.name == n)
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


def _format_rows(rows: list[dict], requested_cols: list[str] | None, *, fancy: bool = False) -> str:
    if not rows:
        return "(no rows)"
    cols   = requested_cols if requested_cols else list(rows[0].keys())
    widths = {c: max(len(c), max(len(_cell_str(r.get(c))) for r in rows))
              for c in cols}
    n = len(rows)

    if fancy:
        def _pad(val: str, w: int) -> str:
            return val.ljust(w)
        top   = "┌" + "┬".join("─" * (w + 2) for w in (widths[c] for c in cols)) + "┐"
        hdr   = "│" + "│".join(f" {c.ljust(widths[c])} " for c in cols) + "│"
        mid   = "├" + "┼".join("─" * (w + 2) for w in (widths[c] for c in cols)) + "┤"
        rows_ = [
            "│" + "│".join(f" {_pad(_cell_str(r.get(c)), widths[c])} " for c in cols) + "│"
            for r in rows
        ]
        bot   = "└" + "┴".join("─" * (w + 2) for w in (widths[c] for c in cols)) + "┘"
        return "\n".join([top, hdr, mid, *rows_, bot, f"({n} row{'s' if n != 1 else ''})"])

    header = " | ".join(c.ljust(widths[c]) for c in cols)
    sep    = "-+-".join("-" * widths[c] for c in cols)
    body   = "\n".join(
        " | ".join(_cell_str(r.get(c)).ljust(widths[c]) for c in cols)
        for r in rows
    )
    return f"{header}\n{sep}\n{body}\n({n} row{'s' if n != 1 else ''})"
