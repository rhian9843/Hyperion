import re
from dataclasses import dataclass, field
from typing import Any

from .errors import NoSuchColumnError, InternalError
from .encoding import _apply_set_op
from .expr import eval_expr, is_expr


def _is_sql_constant(s: str) -> bool:
    """True when s is a SQL literal that can appear on the LHS of a condition.

    Covers numeric literals (int/float, optionally negative) and the SQL
    keyword constants TRUE, FALSE, and NULL.  Identifiers and expressions are
    intentionally excluded so typo column names still raise 'Unknown column'.
    """
    if s.upper() in ("TRUE", "FALSE", "NULL"):
        return True
    try:
        float(s)
        return True
    except (ValueError, TypeError):
        return False


@dataclass
class WhereClause:
    col:          str
    op:           str
    val:          str
    and_clause:   "WhereClause | None" = None
    or_clause:    "WhereClause | None" = None
    subquery_ast: "dict | None"        = None
    group_clause: "WhereClause | None" = None  # set when op == "GROUP"
    row_cols:     "list[str] | None"   = None  # multi-column (a,b) IN / = / !=
    _subq_cache:  dict = field(default_factory=dict, init=False,
                               repr=False, compare=False)

    # ── Public entry point ─────────────────────────────────────────────────────
    def evaluate(self, row: dict[str, Any], db: Any = None) -> bool:
        """Evaluate (self AND and_chain) OR or_clause with correct SQL precedence."""
        and_result = self._eval_and_chain(row, db)
        if not and_result and self.or_clause:
            return self.or_clause.evaluate(row, db)
        return and_result

    def _eval_and_chain(self, row: dict[str, Any], db: Any = None) -> bool:
        if not self._eval_atom(row, db):
            return False
        return self.and_clause._eval_and_chain(row, db) if self.and_clause else True

    def _eval_atom(self, row: dict[str, Any], db: Any = None) -> bool:
        # Parenthesized group or prefix NOT — evaluate the inner expression as a unit
        if self.op == "GROUP":
            return self.group_clause.evaluate(row, db)  # type: ignore[union-attr]
        if self.op == "NOT":
            return not self.group_clause.evaluate(row, db)  # type: ignore[union-attr]

        # EXISTS / NOT EXISTS — re-executed per outer row (supports correlation)
        if self.op in ("EXISTS", "NOT EXISTS"):
            sub_rows = _exec_correlated_subquery(self.subquery_ast, db, row)
            return bool(sub_rows) if self.op == "EXISTS" else not bool(sub_rows)

        # Multi-column row comparison: (col1, col2) IN/NOT IN (subquery) or (col1,col2) =/!= (v1,v2)
        if self.row_cols is not None:
            return self._eval_row_cmp(row, db)

        # col lookup — strip table/alias prefix when exact key absent, or evaluate as expr
        if self.col in row:
            cell = row[self.col]
        elif _is_sql_constant(self.col):
            # Numeric literal or SQL constant on the LHS (e.g. WHERE 1=0, WHERE 3.14 > x)
            cell = eval_expr(self.col, row)
        elif "." in self.col and self.col.split(".", 1)[1] in row:
            cell = row[self.col.split(".", 1)[1]]
        elif "." in self.col:
            # scan for any key whose bare name matches (e.g. t.col → table.col)
            bare = self.col.split(".", 1)[1]
            matches = [v for k, v in row.items() if k.split(".")[-1] == bare]
            if matches:
                cell = matches[0]
            elif is_expr(self.col):
                cell = eval_expr(self.col, row)
            else:
                raise NoSuchColumnError(f"Unknown column: '{self.col}'")
        else:
            # bare name → scan table-qualified keys
            matches = [v for k, v in row.items() if k.split(".")[-1] == self.col]
            if matches:
                cell = matches[0]
            elif is_expr(self.col):
                cell = eval_expr(self.col, row)
            else:
                raise NoSuchColumnError(f"Unknown column: '{self.col}'")

        if self.op == "IS NULL":     return cell is None
        if self.op == "IS NOT NULL": return cell is not None
        if cell is None:             return False

        if self.op in ("IN", "NOT IN"):
            if self.subquery_ast is not None:
                inst_where = _instantiate_correlated(
                    self.subquery_ast.get("where"), row)
                cache_key = _where_cache_key(inst_where)
                if cache_key not in self._subq_cache:
                    inst_stmt = {**self.subquery_ast, "where": inst_where}
                    self._subq_cache[cache_key] = _exec_subquery(inst_stmt, db)
                sub_rows = self._subq_cache[cache_key]
                fk = next(iter(sub_rows[0])) if sub_rows else None
                in_vals: list = [r[fk] for r in sub_rows] if fk else []
                result = cell in in_vals
                return result if self.op == "IN" else not result
            else:
                in_vals_str = [v.strip() for v in self.val.split(",")]
                has_null = any(v.upper() == "NULL" for v in in_vals_str)
                non_null = [v for v in in_vals_str if v.upper() != "NULL"]
                if isinstance(cell, int):
                    try:    result = any(cell == int(v) for v in non_null)
                    except ValueError: result = False
                elif isinstance(cell, float):
                    try:    result = any(cell == float(v) for v in non_null)
                    except ValueError: result = False
                else:
                    result = str(cell) in non_null
                if self.op == "IN":
                    return result
                # NOT IN: UNKNOWN (→ False) when list contains NULL and x doesn't match
                return not result and not has_null

        val: Any = self.val
        if self.subquery_ast is not None:
            sub_rows = _exec_correlated_subquery(self.subquery_ast, db, row)
            if not sub_rows:
                return False
            fk = next(iter(sub_rows[0]))
            val = sub_rows[0][fk]
        elif (isinstance(val, str) and "." in val
              and self.op not in ("LIKE", "GLOB", "IN", "NOT IN")
              and val in row):
            # Qualified column reference (e.g. b.id) in a merged join row — resolve it
            val = row[val]
        elif (isinstance(val, str) and val not in ("", "__subquery__")
              and self.op not in ("LIKE", "GLOB", "IN", "NOT IN") and is_expr(val)):
            val = eval_expr(val, row)

        if not isinstance(val, (int, float)) and isinstance(cell, (int, float)):
            try:
                val = type(cell)(val)
            except (ValueError, TypeError):
                return False
        match self.op:
            case "=":    return cell == val
            case "!=":   return cell != val
            case "<":    return cell < val
            case ">":    return cell > val
            case "<=":   return cell <= val
            case ">=":   return cell >= val
            case "LIKE":
                pattern_str = str(val)
                escape_ch = None
                if "\x00" in pattern_str:
                    pattern_str, escape_ch = pattern_str.split("\x00", 1)
                regex_parts: list[str] = []
                i_p = 0
                while i_p < len(pattern_str):
                    ch = pattern_str[i_p]
                    if escape_ch and ch == escape_ch and i_p + 1 < len(pattern_str):
                        i_p += 1
                        regex_parts.append(re.escape(pattern_str[i_p]))
                    elif ch == "%":
                        regex_parts.append(".*")
                    elif ch == "_":
                        regex_parts.append(".")
                    else:
                        regex_parts.append(re.escape(ch))
                    i_p += 1
                return bool(re.fullmatch("".join(regex_parts), str(cell), re.IGNORECASE))
            case "GLOB":
                regex = "".join(
                    ".*" if ch == "*" else "." if ch == "?" else re.escape(ch)
                    for ch in str(val)
                )
                return bool(re.fullmatch(regex, str(cell)))  # case-sensitive
        return False

    def _resolve_col_val(self, col: str, row: dict) -> Any:
        """Resolve a column name (possibly table-qualified) from a row dict."""
        if col in row:
            return row[col]
        if "." in col:
            bare = col.split(".", 1)[1]
            if bare in row:
                return row[bare]
            matches = [v for k, v in row.items() if k.split(".")[-1] == bare]
            if matches:
                return matches[0]
        else:
            matches = [v for k, v in row.items() if k.split(".")[-1] == col]
            if matches:
                return matches[0]
        return None

    def _eval_row_cmp(self, row: dict, db: Any) -> bool:
        """Evaluate (col1, col2, ...) IN/NOT IN/=/!= right-side."""
        cols = self.row_cols  # type: ignore[assignment]
        lvals = tuple(self._resolve_col_val(c, row) for c in cols)  # type: ignore[union-attr]
        op = self.op

        if op in ("IN", "NOT IN"):
            if self.subquery_ast is not None:
                sub_rows = _exec_correlated_subquery(self.subquery_ast, db, row)
                if not sub_rows:
                    return op == "NOT IN"
                fkeys = list(sub_rows[0].keys())
                rvals_set = {tuple(r.get(k) for k in fkeys) for r in sub_rows}
                return (lvals in rvals_set) if op == "IN" else (lvals not in rvals_set)
            # Literal list: val = "v1,v2,..."  (single-tuple IN for = form)
            rvals = tuple(v.strip().strip("'") for v in self.val.split("\x1f"))
            return (lvals == rvals) if op == "IN" else (lvals != rvals)

        # Literal tuple: val encoded as v1\x1fv2\x1f...
        rvals_raw = self.val.split("\x1f")

        def _coerce(cell: Any, raw: str) -> Any:
            if cell is None:
                return None
            if isinstance(cell, int):
                try:
                    return int(raw)
                except ValueError:
                    return raw
            if isinstance(cell, float):
                try:
                    return float(raw)
                except ValueError:
                    return raw
            return raw

        rvals = tuple(_coerce(lvals[i], rvals_raw[i]) for i in range(len(lvals)))
        match op:
            case "=":  return lvals == rvals
            case "!=": return lvals != rvals
            case "<":  return lvals < rvals  # type: ignore[operator]
            case ">":  return lvals > rvals  # type: ignore[operator]
            case "<=": return lvals <= rvals  # type: ignore[operator]
            case ">=": return lvals >= rvals  # type: ignore[operator]
        return False


# ── Correlated subquery helpers ────────────────────────────────────────────────

_OUTER_REF_RE = re.compile(r'^[A-Za-z_]\w*(\.[A-Za-z_]\w*)?$')


def _try_resolve_outer_ref(val: str, outer_row: dict) -> tuple[bool, Any]:
    """If val is a dot-qualified identifier or an exact key in outer_row, return its value.
    Returns (found, value).  Only resolves qualified names (e.g. 'e.id', 'emp.id')
    or bare names that are an exact match in outer_row — never rewrites plain literals.
    """
    if not _OUTER_REF_RE.match(val):
        return False, None
    if val in outer_row:
        return True, outer_row[val]
    if "." in val:
        col = val.split(".", 1)[1]
        if col in outer_row:
            return True, outer_row[col]
    return False, None


_FLIP_OP: dict[str, str] = {
    "<": ">", ">": "<", "<=": ">=", ">=": "<=", "=": "=", "!=": "!=",
}


def _instantiate_correlated(where: "WhereClause | None",
                             outer_row: dict) -> "WhereClause | None":
    """Return a copy of the WhereClause tree with outer column references substituted."""
    if where is None:
        return None
    new_col, new_val, new_op = where.col, where.val, where.op
    if where.subquery_ast is None:
        if where.val:
            found_r, resolved_r = _try_resolve_outer_ref(where.val, outer_row)
            if found_r:
                new_val = str(resolved_r) if resolved_r is not None else "NULL"
            elif "." in where.col and where.col in outer_row:
                # outer ref is on the left side with full qualified name: swap and flip
                new_col = where.val
                new_val = str(outer_row[where.col]) if outer_row[where.col] is not None else "NULL"
                new_op  = _FLIP_OP.get(where.op, where.op)
    return WhereClause(
        col=new_col, op=new_op, val=new_val,
        subquery_ast=where.subquery_ast,
        row_cols=where.row_cols,
        group_clause=_instantiate_correlated(where.group_clause, outer_row),
        and_clause=_instantiate_correlated(where.and_clause, outer_row),
        or_clause=_instantiate_correlated(where.or_clause, outer_row),
    )


def _where_cache_key(where: "WhereClause | None") -> tuple:
    """Convert a WhereClause tree to a hashable tuple for use as a cache key."""
    if where is None:
        return ()
    return (where.col, where.op, where.val,
            _where_cache_key(where.group_clause),
            _where_cache_key(where.and_clause),
            _where_cache_key(where.or_clause))


def _exec_subquery(stmt: "dict | None", db: Any) -> list[dict]:
    """Execute an already-instantiated subquery AST (no correlated substitution)."""
    if stmt is None or db is None:
        return []
    op = stmt["op"]
    where = stmt.get("where")
    if op == "SELECT":
        return db.select(stmt["table"], stmt["columns"], where,
                         stmt.get("order_by"), stmt.get("limit"),
                         stmt.get("group_by"), stmt.get("having"),
                         stmt.get("distinct", False), stmt.get("offset"))
    if op == "JOIN":
        return db.join(stmt["left_table"], stmt["right_table"],
                       stmt["on_left"], stmt["on_right"],
                       stmt["columns"], where,
                       stmt.get("order_by"), stmt.get("limit"),
                       stmt.get("join_type", "INNER"),
                       stmt.get("left_alias"), stmt.get("right_alias"),
                       stmt.get("offset"))
    if op == "SET_OP":
        left  = _exec_subquery(stmt["left"],  db)
        right = _exec_subquery(stmt["right"], db)
        return _apply_set_op(stmt["set_op"], stmt.get("all", False), left, right)
    raise InternalError(f"Expected SELECT/JOIN/SET_OP in subquery, got '{op}'")


def _exec_correlated_subquery(stmt: "dict | None", db: Any,
                               outer_row: dict) -> list[dict]:
    """Execute a subquery AST with outer_row as the correlation context.
    Substitutes any outer column references in the WHERE before running,
    so correlated subqueries (WHERE inner.col = outer.col) work correctly.
    """
    if stmt is None or db is None:
        return []
    op = stmt["op"]
    inst_where = _instantiate_correlated(stmt.get("where"), outer_row)
    if op == "SELECT":
        return db.select(stmt["table"], stmt["columns"], inst_where,
                         stmt.get("order_by"), stmt.get("limit"),
                         stmt.get("group_by"), stmt.get("having"),
                         stmt.get("distinct", False), stmt.get("offset"))
    if op == "JOIN":
        return db.join(stmt["left_table"], stmt["right_table"],
                       stmt["on_left"], stmt["on_right"],
                       stmt["columns"], inst_where,
                       stmt.get("order_by"), stmt.get("limit"),
                       stmt.get("join_type", "INNER"),
                       stmt.get("left_alias"), stmt.get("right_alias"),
                       stmt.get("offset"))
    if op == "SET_OP":
        left  = _exec_correlated_subquery(stmt["left"],  db, outer_row)
        right = _exec_correlated_subquery(stmt["right"], db, outer_row)
        return _apply_set_op(stmt["set_op"], stmt.get("all", False), left, right)
    raise InternalError(f"Expected SELECT/JOIN/SET_OP in subquery, got '{op}'")
