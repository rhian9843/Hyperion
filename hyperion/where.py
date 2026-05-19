import re
from dataclasses import dataclass
from typing import Any

from .encoding import _apply_set_op


@dataclass
class WhereClause:
    col:          str
    op:           str
    val:          str
    and_clause:   "WhereClause | None" = None
    or_clause:    "WhereClause | None" = None
    subquery_ast: "dict | None"        = None

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
        # EXISTS / NOT EXISTS — re-executed per outer row (supports correlation)
        if self.op in ("EXISTS", "NOT EXISTS"):
            sub_rows = _exec_correlated_subquery(self.subquery_ast, db, row)
            return bool(sub_rows) if self.op == "EXISTS" else not bool(sub_rows)

        # col lookup — strip table/alias prefix when exact key absent
        cell = row.get(self.col)
        if cell is None and "." in self.col:
            cell = row.get(self.col.split(".", 1)[1])

        if self.op == "IS NULL":     return cell is None
        if self.op == "IS NOT NULL": return cell is not None
        if cell is None:             return False

        if self.op in ("IN", "NOT IN"):
            if self.subquery_ast is not None:
                sub_rows = _exec_correlated_subquery(self.subquery_ast, db, row)
                fk = next(iter(sub_rows[0])) if sub_rows else None
                in_vals: list = [r[fk] for r in sub_rows] if fk else []
                result = cell in in_vals
            else:
                in_vals_str = [v.strip() for v in self.val.split(",")]
                if isinstance(cell, int):
                    try:    result = any(cell == int(v) for v in in_vals_str)
                    except ValueError: result = False
                elif isinstance(cell, float):
                    try:    result = any(cell == float(v) for v in in_vals_str)
                    except ValueError: result = False
                else:
                    result = str(cell) in in_vals_str
            return result if self.op == "IN" else not result

        val: Any = self.val
        if self.subquery_ast is not None:
            sub_rows = _exec_correlated_subquery(self.subquery_ast, db, row)
            if not sub_rows:
                return False
            fk = next(iter(sub_rows[0]))
            val = sub_rows[0][fk]

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
                regex = "".join(
                    ".*" if ch == "%" else "." if ch == "_" else re.escape(ch)
                    for ch in str(val)
                )
                return bool(re.fullmatch(regex, str(cell), re.IGNORECASE))
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


def _instantiate_correlated(where: "WhereClause | None",
                             outer_row: dict) -> "WhereClause | None":
    """Return a copy of the WhereClause tree with outer column references substituted."""
    if where is None:
        return None
    new_val = where.val
    if where.val and where.subquery_ast is None:
        found, resolved = _try_resolve_outer_ref(where.val, outer_row)
        if found:
            new_val = str(resolved) if resolved is not None else "NULL"
    return WhereClause(
        col=where.col, op=where.op, val=new_val,
        subquery_ast=where.subquery_ast,
        and_clause=_instantiate_correlated(where.and_clause, outer_row),
        or_clause=_instantiate_correlated(where.or_clause, outer_row),
    )


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
                         stmt.get("distinct", False))
    if op == "JOIN":
        return db.join(stmt["left_table"], stmt["right_table"],
                       stmt["on_left"], stmt["on_right"],
                       stmt["columns"], inst_where,
                       stmt.get("order_by"), stmt.get("limit"),
                       stmt.get("join_type", "INNER"),
                       stmt.get("left_alias"), stmt.get("right_alias"))
    if op == "SET_OP":
        left  = _exec_correlated_subquery(stmt["left"],  db, outer_row)
        right = _exec_correlated_subquery(stmt["right"], db, outer_row)
        return _apply_set_op(stmt["set_op"], stmt.get("all", False), left, right)
    raise RuntimeError(f"Expected SELECT/JOIN/SET_OP in subquery, got '{op}'")
