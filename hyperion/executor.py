import re
import struct
from collections import defaultdict
from typing import Any

from .database import Database
from .encoding import _apply_set_op, _apply_order_limit, _encode_composite_key, _make_index_key
from .expr import eval_expr
from .parser import _parse_tokens, _tokenize
from .schema import deserialize_row, serialize_row
from .constants import INTEGER, REAL, TEXT, DEFAULT_TEXT_SIZE
from .query import _project_row

# ── Window function helpers ────────────────────────────────────────────────────

_WINDOW_RE = re.compile(r'\bOVER\s*\(', re.IGNORECASE)


def _parse_window_col(expr: str) -> dict | None:
    """Parse 'fn(args) OVER (PARTITION BY … ORDER BY …)'. Returns None if not a window expr."""
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
    uc = over_content.upper()
    pb_m = re.search(r'\bPARTITION\s+BY\b', uc)
    ob_m = re.search(r'\bORDER\s+BY\b',     uc)
    if pb_m:
        pb_end = ob_m.start() if ob_m else len(over_content)
        partition_by = [c.strip() for c in over_content[pb_m.end():pb_end].split(",") if c.strip()]
    if ob_m:
        for spec in over_content[ob_m.end():].split(","):
            parts = spec.strip().split()
            if parts:
                desc = len(parts) > 1 and parts[1].upper() == "DESC"
                order_by.append({"col": parts[0], "desc": desc})
    return {"fn": fn_name, "args": fn_args,
            "partition_by": partition_by, "order_by": order_by}


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
            tcol = fn_args[0].strip() if fn_args else None
            if tcol:
                fv = _get_col_val(p_rows[0], tcol)
                for idx in indices:
                    rows[idx][col] = fv

        elif fn == "LAST_VALUE":
            tcol = fn_args[0].strip() if fn_args else None
            if tcol:
                lv = _get_col_val(p_rows[-1], tcol)
                for idx in indices:
                    rows[idx][col] = lv

        elif fn in ("SUM", "AVG", "MIN", "MAX", "COUNT"):
            tcol   = fn_args[0].strip() if fn_args else None
            is_star = not tcol or tcol == "*"
            if fn == "COUNT" and is_star:
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


def _apply_window_functions(rows: list[dict], cols: list[str]) -> list[dict]:
    """Compute any window-function columns and inject them into each row."""
    if not rows or not cols:
        return rows
    defs = [(c, _parse_window_col(c)) for c in cols if c != "*"]
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


def _exec_extra_join(rows: list[dict], join_info: dict,
                     db: "Database") -> list[dict]:
    """Apply one additional JOIN step in-memory against an already-joined row set."""
    right_table = join_info["right_table"]
    right_alias = join_info.get("right_alias") or right_table
    join_type   = join_info.get("join_type", "INNER")
    on_left     = join_info.get("on_left")
    on_right    = join_info.get("on_right")

    rmeta       = db._meta(right_table)
    right_rows  = [deserialize_row(rmeta.schema, r)
                   for _, r in db._table_btree(rmeta).scan()]
    right_null  = {f"{right_alias}.{c.name}": None for c in rmeta.schema.columns}
    rcol        = on_right.split(".")[-1] if on_right else None

    result: list[dict] = []
    matched_right: set[int] = set()

    for lr in rows:
        if rcol is None:            # CROSS JOIN
            for rr in right_rows:
                merged = dict(lr)
                merged.update({f"{right_alias}.{k}": v for k, v in rr.items()})
                result.append(merged)
            continue

        lval = lr.get(on_left) if on_left else None
        if lval is None and on_left:
            lval = lr.get(on_left.split(".")[-1])

        on_matched = False
        for j, rr in enumerate(right_rows):
            if lval != rr.get(rcol):
                continue
            on_matched = True
            matched_right.add(j)
            merged = dict(lr)
            merged.update({f"{right_alias}.{k}": v for k, v in rr.items()})
            result.append(merged)
        if not on_matched and join_type in ("LEFT", "FULL"):
            merged = dict(lr)
            merged.update(right_null)
            result.append(merged)

    if join_type in ("RIGHT", "FULL"):
        left_null = {k: None for k in (rows[0] if rows else {})}
        for j, rr in enumerate(right_rows):
            if j not in matched_right:
                merged = dict(left_null)
                merged.update({f"{right_alias}.{k}": v for k, v in rr.items()})
                result.append(merged)
    return result


def _rows_for_stmt(stmt: dict, db: "Database",
                   ctes: dict | None = None) -> list[dict]:
    """Execute any SELECT-like statement and return its rows."""
    ctes = {**(ctes or {}), **(stmt.get("ctes") or {})}
    op = stmt["op"]
    if op == "SELECT_NOFROM":
        row: dict = {}
        return [{col: eval_expr(col, row) for col in (stmt.get("columns") or [])}]
    if op == "SELECT":
        if stmt.get("subquery_from"):
            return _exec_derived_table(stmt, db, ctes)
        tbl = stmt.get("table") or ""
        if tbl in ctes:
            return _exec_cte_select(stmt, ctes[tbl], db, ctes)
        if tbl in db.views:
            view_ast = _parse_tokens(_tokenize(db.views[tbl]))
            return _exec_cte_select(stmt, view_ast, db, ctes)
        return db.select(stmt["table"], stmt["columns"], stmt["where"],
                         stmt.get("order_by"), stmt.get("limit"),
                         stmt.get("group_by"), stmt.get("having"),
                         stmt.get("distinct", False), stmt.get("offset"))
    if op == "JOIN":
        extra = stmt.get("extra_joins", [])
        if extra:
            rows = db.join(stmt["left_table"], stmt["right_table"],
                           stmt["on_left"], stmt["on_right"],
                           None, None,
                           join_type=stmt.get("join_type", "INNER"),
                           left_alias=stmt.get("left_alias"),
                           right_alias=stmt.get("right_alias"))
            for ej in extra:
                rows = _exec_extra_join(rows, ej, db)
            if stmt.get("where"):
                rows = [r for r in rows if stmt["where"].evaluate(r, db)]
            if stmt.get("columns"):
                rows = [_project_row(r, stmt["columns"]) for r in rows]
            return _apply_order_limit(rows, stmt.get("order_by"),
                                      stmt.get("limit"), stmt.get("offset"))
        return db.join(stmt["left_table"], stmt["right_table"],
                       stmt["on_left"], stmt["on_right"],
                       stmt["columns"], stmt["where"],
                       stmt.get("order_by"), stmt.get("limit"),
                       stmt.get("join_type", "INNER"),
                       stmt.get("left_alias"), stmt.get("right_alias"),
                       stmt.get("offset"))
    if op == "SET_OP":
        left  = _rows_for_stmt(stmt["left"],  db, ctes)
        right = _rows_for_stmt(stmt["right"], db, ctes)
        return _apply_set_op(stmt["set_op"], stmt.get("all", False), left, right)
    raise RuntimeError(f"Expected SELECT/JOIN/SET_OP, got '{op}'")


def execute(stmt: dict, db: Database) -> str:
    op = stmt["op"]

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


def _execute_inner(stmt: dict, db: Database) -> str:
    from .schema import Schema
    op = stmt["op"]

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
                               primary_key_columns=pk_cols))
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

    if op == "INSERT":
        meta            = db._meta(stmt["table"])
        col_names       = stmt["col_names"] or [c.name for c in meta.schema.columns]
        conflict_action = stmt.get("conflict_action")
        on_conflict_set = stmt.get("on_conflict_set") or {}
        returning_cols  = stmt.get("returning")
        returned_rows: list[dict] = []
        for values in stmt["rows"]:
            if len(col_names) != len(values):
                raise RuntimeError(
                    f"Column/value mismatch: {len(col_names)} columns, {len(values)} values"
                )
            parsed: dict[str, Any] = {}
            for name, val in zip(col_names, values):
                parsed[name] = None if val.upper() == "NULL" else val
            for col in meta.schema.columns:
                if col.name not in parsed:
                    parsed[col.name] = col.default
            if conflict_action == "IGNORE":
                try:
                    row_out = db.insert(stmt["table"], parsed)
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
                if returning_cols:
                    returned_rows.append(row_out)
            elif conflict_action == "UPDATE":
                try:
                    row_out = db.insert(stmt["table"], parsed)
                    if returning_cols:
                        returned_rows.append(row_out)
                except RuntimeError as _e:
                    if any(kw in str(_e) for kw in ("UNIQUE", "NOT NULL", "CHECK")):
                        _apply_on_conflict_update(db, meta, parsed, on_conflict_set)
                    else:
                        raise
            else:
                row_out = db.insert(stmt["table"], parsed)
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
            db.insert(stmt["table"], data)
        n = len(src_rows)
        return f"{n} row{'s' if n != 1 else ''} inserted."

    if op == "SELECT_NOFROM":
        row: dict[str, Any] = {}
        result_row = {col: eval_expr(col, row) for col in (stmt.get("columns") or [])}
        rows, out_cols = _apply_aliases([result_row], stmt.get("columns"),
                                        stmt.get("col_aliases"))
        return _format_rows(rows, out_cols)

    if op == "SELECT":
        ctes = stmt.get("ctes") or {}
        s = _resolve_alias_refs(stmt, stmt.get("col_aliases"))
        stmt_cols = stmt.get("columns") or []
        has_window    = any(_WINDOW_RE.search(c) for c in stmt_cols if c != "*")
        has_scalar_sq = any(_is_scalar_subquery_col(c) for c in stmt_cols)
        tbl = s.get("table") or ""
        if s.get("subquery_from"):
            rows = _exec_derived_table(s, db, ctes)
        elif tbl in ctes:
            rows = _exec_cte_select(s, ctes[tbl], db, ctes)
        elif tbl in db.views:
            view_ast = _parse_tokens(_tokenize(db.views[tbl]))
            rows = _exec_cte_select(s, view_ast, db, ctes)
        elif has_window or has_scalar_sq:
            # Fetch all columns so window/scalar-sq can reference any column for correlation
            all_rows = db.select(s["table"], None, s["where"],
                                 None, None,
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
                all_rows = _apply_window_functions(all_rows, stmt_cols)
            rows = ([_project_row(r, stmt_cols) for r in all_rows]
                    if stmt_cols else all_rows)
            rows = _apply_order_limit(rows, s.get("order_by"),
                                      s.get("limit"), s.get("offset"))
        else:
            rows = db.select(s["table"], s["columns"], s["where"],
                             s.get("order_by"), s.get("limit"),
                             s.get("group_by"), s.get("having"),
                             s.get("distinct", False), s.get("offset"))
        rows, cols = _apply_aliases(rows, stmt["columns"], stmt.get("col_aliases"))
        return _format_rows(rows, cols)

    if op == "JOIN":
        s = _resolve_alias_refs(stmt, stmt.get("col_aliases"))
        extra = s.get("extra_joins", [])
        if extra:
            rows = db.join(s["left_table"], s["right_table"],
                           s["on_left"], s["on_right"],
                           None, None,
                           join_type=s.get("join_type", "INNER"),
                           left_alias=s.get("left_alias"),
                           right_alias=s.get("right_alias"))
            for ej in extra:
                rows = _exec_extra_join(rows, ej, db)
            if s.get("where"):
                rows = [r for r in rows if s["where"].evaluate(r, db)]
            if s.get("columns"):
                rows = [_project_row(r, s["columns"]) for r in rows]
            rows = _apply_order_limit(rows, s.get("order_by"),
                                      s.get("limit"), s.get("offset"))
        else:
            rows = db.join(s["left_table"], s["right_table"],
                           s["on_left"], s["on_right"],
                           s["columns"], s["where"],
                           s.get("order_by"), s.get("limit"),
                           s.get("join_type", "INNER"),
                           s.get("left_alias"), s.get("right_alias"),
                           s.get("offset"))
        rows, cols = _apply_aliases(rows, stmt["columns"], stmt.get("col_aliases"))
        return _format_rows(rows, cols)

    if op == "SET_OP":
        rows = _rows_for_stmt(stmt, db)
        cols = stmt["left"].get("columns")
        return _format_rows(rows, cols)

    if op == "TRUNCATE":
        rows = db.delete(stmt["table"], None)
        n = len(rows)
        return f"Table '{stmt['table']}' truncated ({n} rows deleted)."

    if op == "UPDATE":
        rows = db.update(stmt["table"], stmt["assignments"], stmt["where"],
                         stmt.get("limit"))
        n = len(rows)
        if stmt.get("returning"):
            ret_cols = stmt["returning"]
            return _format_rows([{c: r.get(c) for c in ret_cols} for r in rows], ret_cols)
        return f"{n} row{'s' if n != 1 else ''} updated."

    if op == "DELETE":
        rows = db.delete(stmt["table"], stmt["where"], stmt.get("limit"))
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
        existing = deserialize_row(schema, raw)
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
        existing = deserialize_row(schema, raw)
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
        db._table_btree(meta).update({rowid: serialize_row(schema, updated)})
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
