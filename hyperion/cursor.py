"""PEP 249-compatible Cursor with parameter binding."""
from __future__ import annotations

import itertools
import re
import time
from typing import TYPE_CHECKING, Any, Iterator

if TYPE_CHECKING:
    from .database import Database

# ── Parameter binding ─────────────────────────────────────────────────────────

def _sql_literal(val: Any) -> str:
    """Convert a Python value to a safe SQL literal string."""
    if val is None:
        return "NULL"
    if isinstance(val, bool):
        return "1" if val else "0"
    if isinstance(val, int):
        return str(val)
    if isinstance(val, float):
        return repr(val)
    if isinstance(val, bytes):
        return "X'" + val.hex() + "'"
    return "'" + str(val).replace("'", "''") + "'"


def _bind_params(sql: str, params: Any) -> str:
    """Substitute ? or :name/$name placeholders with safely-quoted SQL literals."""
    if params is None:
        return sql

    result: list[str] = []
    i = 0
    n = len(sql)

    def _scan_string(start: int) -> int:
        j = start + 1
        while j < n:
            if sql[j] == "'":
                if j + 1 < n and sql[j + 1] == "'":
                    j += 2
                else:
                    return j + 1
            else:
                j += 1
        return j

    if isinstance(params, dict):
        while i < n:
            ch = sql[i]
            if ch == "'":
                end = _scan_string(i)
                result.append(sql[i:end])
                i = end
            elif ch in (":", "$") and i + 1 < n and (sql[i + 1].isalpha() or sql[i + 1] == "_"):
                j = i + 1
                while j < n and (sql[j].isalnum() or sql[j] == "_"):
                    j += 1
                name = sql[i + 1:j]
                if name not in params:
                    raise ValueError(f"No value for named parameter '{name}'")
                result.append(_sql_literal(params[name]))
                i = j
            else:
                result.append(ch)
                i += 1
    else:
        params_list = list(params)
        pi = 0
        while i < n:
            ch = sql[i]
            if ch == "'":
                end = _scan_string(i)
                result.append(sql[i:end])
                i = end
            elif ch == "?":
                if pi >= len(params_list):
                    raise ValueError("Not enough parameters for SQL statement")
                result.append(_sql_literal(params_list[pi]))
                pi += 1
                i += 1
            else:
                result.append(ch)
                i += 1
        if pi < len(params_list):
            raise ValueError(
                f"Too many parameters: {len(params_list)} supplied, {pi} consumed"
            )

    return "".join(result)


# ── AST-level parameter binding ──────────────────────────────────────────────

def _unquote_str(s: str) -> str:
    """Strip outer single quotes (mirrors parser's _unquote_token)."""
    if s.startswith("'") and s.endswith("'") and len(s) >= 2:
        return s[1:-1].replace("''", "'")
    return s


def _bind_ast_params(stmt: dict, params) -> dict:
    """Return a shallow copy of stmt with '?' / ':name' placeholders filled in.

    Substitution rules per location:
      - WHERE / HAVING conditions (WhereClause.val): unquoted value so the
        existing coercion logic in _eval_atom sees the same string format as
        a parsed literal would produce.
      - INSERT rows (stmt['rows'] list): SQL-literal form so the executor's
        string-processing code can handle it identically to a parsed literal.
      - UPDATE assignments (stmt['assignments']): unquoted value, same as
        the parser's _unquote_token would produce.
    """
    from .where import WhereClause

    if isinstance(params, dict):
        param_dict = params
        pos_iter   = None
    else:
        param_dict = None
        pos_iter   = iter(list(params))

    def _next_param() -> Any:
        if pos_iter is None:
            raise ValueError("Use named parameters (:name) with a dict, not positional ?")
        return next(pos_iter)

    def _resolve_named(name: str) -> Any:
        key = name.lstrip(":$")
        if param_dict is None:
            raise ValueError(f"Named parameter '{name}' requires a dict of params")
        if key not in param_dict:
            raise ValueError(f"No value for named parameter '{name}'")
        return param_dict[key]

    def _is_placeholder(val: str) -> bool:
        return val == "?" or (
            param_dict is not None
            and isinstance(val, str)
            and len(val) > 1
            and val[0] in (":", "$")
        )

    def _sub_unquoted(val: str) -> str:
        """Return unquoted substituted value for WHERE / UPDATE contexts."""
        if not _is_placeholder(val):
            return val
        raw = _next_param() if val == "?" else _resolve_named(val)
        return _unquote_str(_sql_literal(raw))

    def _sub_row_val(val: str) -> str:
        """Return SQL-literal substituted value for INSERT rows."""
        if not _is_placeholder(val):
            return val
        raw = _next_param() if val == "?" else _resolve_named(val)
        return _sql_literal(raw)

    def _sub_where(where: "WhereClause | None") -> "WhereClause | None":
        if where is None:
            return None
        new_val = _sub_unquoted(where.val) if where.val else where.val
        return WhereClause(
            col=where.col, op=where.op, val=new_val,
            subquery_ast=where.subquery_ast,
            row_cols=where.row_cols,
            group_clause=_sub_where(where.group_clause),
            and_clause=_sub_where(where.and_clause),
            or_clause=_sub_where(where.or_clause),
        )

    new_stmt = dict(stmt)
    op = stmt.get("op", "")

    if op in ("INSERT", "INSERT_OR_REPLACE", "UPSERT"):
        if "rows" in stmt:
            new_stmt["rows"] = [[_sub_row_val(v) for v in row] for row in stmt["rows"]]
        new_stmt["where"] = _sub_where(stmt.get("where"))

    elif op == "UPDATE":
        if "assignments" in stmt:
            new_stmt["assignments"] = {
                col: _sub_unquoted(val) if isinstance(val, str) else val
                for col, val in stmt["assignments"].items()
            }
        new_stmt["where"] = _sub_where(stmt.get("where"))

    elif op == "DELETE":
        new_stmt["where"] = _sub_where(stmt.get("where"))

    else:
        # SELECT / JOIN / SET_OP / RECURSIVE_CTE / etc.
        new_stmt["where"]  = _sub_where(stmt.get("where"))
        new_stmt["having"] = _sub_where(stmt.get("having"))

    return new_stmt


# ── Rowcount parsing ──────────────────────────────────────────────────────────

_ROWCOUNT_RE = re.compile(r"^(\d+)\s+row", re.IGNORECASE)


def _rowcount_from_result(s: str) -> int:
    m = _ROWCOUNT_RE.match(s.strip())
    return int(m.group(1)) if m else -1


# ── Select-like ops ───────────────────────────────────────────────────────────

_SELECT_OPS = frozenset({"SELECT", "SELECT_NOFROM", "JOIN", "SET_OP",
                         "RECURSIVE_CTE", "INLINE_ROWS"})


# ── Column name inference for empty result sets ───────────────────────────────

def _infer_col_names(stmt: dict | None, db: "Database") -> list[str] | None:
    """Return column names from a SELECT AST when the result set is empty."""
    if stmt is None:
        return None
    op = stmt.get("op", "")
    cols = stmt.get("columns") or []
    aliases = stmt.get("col_aliases") or {}

    if op == "SELECT_NOFROM":
        return [aliases.get(c, c) for c in cols] or None

    if cols and cols != ["*"]:
        return [aliases.get(c, c) for c in cols] or None

    if cols == ["*"] or not cols:
        table = stmt.get("table")
        if table and hasattr(db, "_catalog") and table in db._catalog.tables:
            schema = db._catalog.tables[table].schema
            return [c.name for c in schema.columns]

    return None


# ── Row guard ─────────────────────────────────────────────────────────────────

def _guarded_iter(it, max_rows: int):
    """Yield rows from *it*, raising TooManyRowsError on the (max_rows+1)-th row."""
    from .executor import TooManyRowsError
    for n, row in enumerate(it):
        if n >= max_rows:
            raise TooManyRowsError(
                f"Query returned more than {max_rows} row{'s' if max_rows != 1 else ''} "
                f"(max_rows={max_rows})"
            )
        yield row


# ── Cursor ────────────────────────────────────────────────────────────────────

class Cursor:
    """PEP 249-compatible cursor. Rows are returned as dicts by default;
    set db.row_factory to change the format."""

    def __init__(self, db: "Database") -> None:
        self._db = db
        self._iter: Iterator[dict] | None = None
        self.description: tuple | None = None
        self.rowcount: int = -1
        self.lastrowid: int | None = None
        self.row_factory = db.row_factory  # snapshot at creation time

    # ── Execution ─────────────────────────────────────────────────────────────

    def execute(self, sql: str, params=None, timeout_ms: int | None = None,
               max_rows: int | None = None) -> "Cursor":
        with self._db._lock:
            return self._execute_inner(sql, params, timeout_ms, max_rows)

    def _execute_inner(self, sql: str, params=None,
                       timeout_ms: int | None = None,
                       max_rows: int | None = None) -> "Cursor":
        from .parser import parse
        from .executor import execute as _exec, _iter_rows_for_stmt, QueryTimeoutError
        from .introspect import explain_plan
        from .auth import check_authorizer, SQLITE_IGNORE
        from .expr import get_last_insert_rowid

        cache = self._db._plan_cache
        if sql not in cache:
            cache[sql] = parse(sql)
            # Simple size cap: evict oldest entries when cache grows large
            if len(cache) > 512:
                oldest = next(iter(cache))
                del cache[oldest]
        stmt = _bind_ast_params(cache[sql], params) if params is not None else cache[sql]
        op = stmt.get("op", "")

        if timeout_ms is not None:
            self._db._query_deadline = time.monotonic() + timeout_ms / 1000.0
        else:
            self._db._query_deadline = None

        # Per-query max_rows overrides connection-level; None means no limit.
        effective_max_rows = max_rows if max_rows is not None else self._db.max_rows

        try:
            # SELECT ops bypass executor.execute, so authorizer must be checked here
            if op in _SELECT_OPS and self._db._authorizer is not None:
                if check_authorizer(self._db._authorizer, stmt) == SQLITE_IGNORE:
                    self._iter = None
                    self.description = None; self.rowcount = -1
                    return self

            if op in _SELECT_OPS:
                self._set_select_result(
                    _iter_rows_for_stmt(stmt, self._db), stmt, effective_max_rows
                )
            elif op == "EXPLAIN":
                rows = explain_plan(stmt["stmt"], self._db)
                self._set_select_result(iter(rows))
            else:
                result_str = _exec(stmt, self._db)
                self._iter = None
                self.description = None
                self.rowcount = _rowcount_from_result(result_str)
                if op in ("INSERT", "INSERT_SELECT", "UPSERT"):
                    self.lastrowid = get_last_insert_rowid()
                else:
                    self.lastrowid = None
        finally:
            self._db._query_deadline = None

        return self

    def executemany(self, sql: str, params_seq) -> "Cursor":
        with self._db._lock:
            total = 0
            for params in params_seq:
                self._execute_inner(sql, params)
                if self.rowcount >= 0:
                    total += self.rowcount
            self.rowcount = total
            return self

    def executescript(self, sql: str) -> "Cursor":
        with self._db._lock:
            from .repl import _split_statements
            from .parser import parse
            from .executor import execute as _exec

            if self._db.in_transaction:
                self._db.commit()
            for part in _split_statements(sql):
                part = part.strip()
                if part:
                    _exec(parse(part), self._db)
            self._result = []
            self._pos = 0
            self.description = None
            self.rowcount = -1
            return self

    # ── Fetch ─────────────────────────────────────────────────────────────────

    def fetchone(self) -> Any:
        with self._db._lock:
            if self._iter is None:
                return None
            try:
                return self._apply_factory(next(self._iter))
            except StopIteration:
                self._iter = None
                return None

    def fetchmany(self, size: int = 1) -> list:
        with self._db._lock:
            if self._iter is None:
                return []
            rows: list = []
            for _ in range(size):
                try:
                    rows.append(self._apply_factory(next(self._iter)))
                except StopIteration:
                    self._iter = None
                    break
            return rows

    def fetchall(self) -> list:
        with self._db._lock:
            if self._iter is None:
                return []
            rows = [self._apply_factory(r) for r in self._iter]
            self._iter = None
            return rows

    def close(self) -> None:
        self._iter = None

    def __iter__(self) -> "Cursor":
        return self

    def __next__(self) -> Any:
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row

    # ── Internal ──────────────────────────────────────────────────────────────

    def _apply_factory(self, row: dict) -> Any:
        if self.row_factory is None:
            return row
        return self.row_factory(self, row)

    def _set_select_result(self, row_iter,
                           stmt: dict | None = None,
                           max_rows: int | None = None) -> None:
        """Store a row iterator; peek one row to populate description."""
        from .executor import TooManyRowsError

        it = iter(row_iter)
        try:
            first = next(it)
        except StopIteration:
            self._iter = None
            self.rowcount = -1
            col_names = _infer_col_names(stmt, self._db) if stmt is not None else None
            self.description = (
                tuple((k, None, None, None, None, None, None) for k in col_names)
                if col_names is not None else None
            )
            return
        col_names = list(first.keys())
        self.description = tuple(
            (k, None, None, None, None, None, None) for k in col_names
        )
        self.rowcount = -1
        if max_rows is not None:
            self._iter = _guarded_iter(itertools.chain([first], it), max_rows)
        else:
            self._iter = itertools.chain([first], it)
