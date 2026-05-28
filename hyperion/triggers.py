"""
Trigger firing logic for Hyperion.

Supports BEFORE/AFTER INSERT, UPDATE, DELETE triggers with:
  - FOR EACH ROW semantics
  - NEW.col / OLD.col row references in body and WHEN clause
  - WHEN condition guard
  - Cascading triggers (depth-limited to 32)
"""

import re
from typing import Any

from .errors import ConstraintError, InternalError
from .schema import deserialize_row
from .expr import eval_expr, is_expr

_ROW_REF_RE   = re.compile(r'^(NEW|OLD)\.(\w+)$', re.IGNORECASE)
_RAISE_RE     = re.compile(
    r"^RAISE\(\s*(ABORT|FAIL|ROLLBACK|IGNORE)\s*(?:,\s*'((?:[^']|'')*)'\s*)?\)$",
    re.IGNORECASE,
)
_MAX_TRIGGER_DEPTH = 32


class TriggerIgnore(Exception):
    """Raised by RAISE(IGNORE) — silently aborts the current trigger."""


def _sql_literal(val: Any) -> str:
    """Convert a Python value to a SQL literal token."""
    if val is None:
        return "NULL"
    if isinstance(val, str):
        return "'" + val.replace("'", "''") + "'"
    return str(val)


def _subst_row_refs(tokens: list[str],
                   new_row: dict | None,
                   old_row: dict | None) -> list[str]:
    """Replace NEW.col and OLD.col tokens with SQL literals."""
    result = []
    for tok in tokens:
        m = _ROW_REF_RE.match(tok)
        if m:
            which, col = m.group(1).upper(), m.group(2)
            row = new_row if which == "NEW" else old_row
            result.append(_sql_literal((row or {}).get(col)))
        else:
            result.append(tok)
    return result


def _build_row_ctx(new_row: dict | None, old_row: dict | None) -> dict:
    """Build a context dict for WHEN clause evaluation.

    Keys: bare column names (from new_row; old_row for DELETE),
          NEW.col, and OLD.col qualified names.
    """
    ctx: dict = {}
    if old_row:
        ctx.update(old_row)
        ctx.update({f"OLD.{k}": v for k, v in old_row.items()})
    if new_row:
        ctx.update(new_row)           # new values shadow old for bare names
        ctx.update({f"NEW.{k}": v for k, v in new_row.items()})
    return ctx


def _eval_when(when_tokens: list[str],
               new_row: dict | None,
               old_row: dict | None,
               db: Any) -> bool:
    """Evaluate a WHEN condition. Returns True if absent or condition holds."""
    if not when_tokens:
        return True
    from .parser import _parse_where_expr
    where, _ = _parse_where_expr(when_tokens, 0)
    if where is None:
        return True
    ctx = _build_row_ctx(new_row, old_row)
    try:
        return bool(where.evaluate(ctx, db))
    except Exception:
        return False


def _exec_body(body_tokens: list[str],
               new_row: dict | None,
               old_row: dict | None,
               db: Any,
               depth: int) -> None:
    """Execute each statement in the trigger body with NEW/OLD substituted."""
    from .parser import _parse_tokens
    from .executor import _execute_inner

    # Split body_tokens by ';' into individual statement token lists
    stmts: list[list[str]] = []
    current: list[str] = []
    for tok in body_tokens:
        if tok == ";":
            if current:
                stmts.append(current)
                current = []
        else:
            current.append(tok)
    if current:
        stmts.append(current)

    for toks in stmts:
        subst = _subst_row_refs(toks, new_row, old_row)
        if not subst:
            continue
        # Handle RAISE(type, 'msg') before parsing
        if len(subst) == 1:
            m = _RAISE_RE.match(subst[0])
            if m:
                kind = m.group(1).upper()
                msg  = m.group(2) or ""
                if kind == "IGNORE":
                    raise TriggerIgnore()
                raise ConstraintError(f"RAISE({kind}): {msg}")
        ast = _parse_tokens(subst)
        # Tag depth on db to allow recursive trigger detection
        db._trigger_depth = depth
        try:
            _execute_inner(ast, db)
        finally:
            db._trigger_depth = depth - 1


def _triggers_for(db: Any, table: str, timing: str, event: str,
                  changed_cols: list[str] | None = None):
    """Yield TriggerMeta objects that match table/timing/event.

    For UPDATE triggers with an OF list, only fire when at least one
    of the watched columns appears in changed_cols.
    """
    for trig in db._catalog.triggers.values():
        if trig.table != table or trig.timing != timing or trig.event != event:
            continue
        if event == "UPDATE" and trig.update_cols and changed_cols is not None:
            if not any(c in changed_cols for c in trig.update_cols):
                continue
        yield trig


def fire_triggers(db: Any,
                  table: str,
                  timing: str,
                  event: str,
                  new_row: dict | None,
                  old_row: dict | None,
                  changed_cols: list[str] | None = None) -> None:
    """Fire all matching triggers for a given DML event on a row."""
    depth = getattr(db, "_trigger_depth", 0)
    if depth >= _MAX_TRIGGER_DEPTH:
        raise InternalError(
            f"Trigger recursion limit ({_MAX_TRIGGER_DEPTH}) exceeded on '{table}'")

    for trig in _triggers_for(db, table, timing, event, changed_cols):
        if not _eval_when(trig.when_tokens, new_row, old_row, db):
            continue
        try:
            _exec_body(trig.body_tokens, new_row, old_row, db, depth + 1)
        except TriggerIgnore:
            pass


def has_triggers(db: Any, table: str, event: str) -> bool:
    """Return True if there are any triggers on table for the given event."""
    return (any(True for _ in _triggers_for(db, table, "BEFORE",     event)) or
            any(True for _ in _triggers_for(db, table, "AFTER",      event)) or
            any(True for _ in _triggers_for(db, table, "INSTEAD OF", event)))


def has_instead_of(db: Any, view: str, event: str) -> bool:
    """Return True if there are INSTEAD OF triggers on view for the given event."""
    return any(True for _ in _triggers_for(db, view, "INSTEAD OF", event))


def scan_matching_rows(db: Any, table: str, where: Any,
                       limit: int | None = None) -> list[dict]:
    """Return rows from table matching where (used to collect OLD rows for triggers)."""
    meta   = db._meta(table)
    schema = meta.schema
    rows: list[dict] = []
    count = 0
    for _, raw in db._table_btree(meta).scan():
        if limit is not None and count >= limit:
            break
        row = deserialize_row(schema, db._unpack_row_cell(raw))
        if not where or where.evaluate(row, db):
            rows.append(row)
            count += 1
    return rows


def apply_update_row(old_row: dict, assignments: dict, schema: Any) -> dict:
    """Compute the prospective new row from an UPDATE SET clause (for BEFORE/AFTER triggers)."""
    from .constants import INTEGER, REAL
    new_row = dict(old_row)
    for col, val_str in assignments.items():
        col_obj = next((c for c in schema.columns if c.name == col), None)
        if col_obj is None:
            continue
        if val_str is None or (isinstance(val_str, str) and val_str.upper() == "NULL"):
            new_row[col] = None
            continue
        # Evaluate expressions (e.g. col + 1, UPPER(col)) against the current row
        if is_expr(str(val_str)):
            new_row[col] = eval_expr(str(val_str), new_row)
            continue
        if col_obj.type == INTEGER:
            try:
                new_row[col] = int(val_str)
            except (ValueError, TypeError):
                new_row[col] = val_str
        elif col_obj.type == REAL:
            try:
                new_row[col] = float(val_str)
            except (ValueError, TypeError):
                new_row[col] = val_str
        else:
            new_row[col] = val_str
    return new_row
