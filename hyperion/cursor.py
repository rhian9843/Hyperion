"""PEP 249-compatible Cursor with parameter binding."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

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


# ── Rowcount parsing ──────────────────────────────────────────────────────────

_ROWCOUNT_RE = re.compile(r"^(\d+)\s+row", re.IGNORECASE)


def _rowcount_from_result(s: str) -> int:
    m = _ROWCOUNT_RE.match(s.strip())
    return int(m.group(1)) if m else -1


# ── Select-like ops ───────────────────────────────────────────────────────────

_SELECT_OPS = frozenset({"SELECT", "SELECT_NOFROM", "JOIN", "SET_OP",
                         "RECURSIVE_CTE", "INLINE_ROWS"})


# ── Cursor ────────────────────────────────────────────────────────────────────

class Cursor:
    """PEP 249-compatible cursor. Rows are returned as dicts (key = column name)."""

    def __init__(self, db: "Database") -> None:
        self._db = db
        self._result: list[dict] = []
        self._pos: int = 0
        self.description: tuple | None = None
        self.rowcount: int = -1
        self.lastrowid: int | None = None

    # ── Execution ─────────────────────────────────────────────────────────────

    def execute(self, sql: str, params=None) -> "Cursor":
        from .parser import parse
        from .executor import execute as _exec, _rows_for_stmt
        from .introspect import explain_plan

        bound = _bind_params(sql, params) if params is not None else sql
        stmt = parse(bound)
        op = stmt.get("op", "")

        if op in _SELECT_OPS:
            rows = _rows_for_stmt(stmt, self._db)
            self._set_select_result(rows)
        elif op == "EXPLAIN":
            rows = explain_plan(stmt["stmt"], self._db)
            self._set_select_result(rows)
        else:
            result_str = _exec(stmt, self._db)
            self._result = []
            self._pos = 0
            self.description = None
            self.rowcount = _rowcount_from_result(result_str)
            self.lastrowid = None

        return self

    def executemany(self, sql: str, params_seq) -> "Cursor":
        total = 0
        for params in params_seq:
            self.execute(sql, params)
            if self.rowcount >= 0:
                total += self.rowcount
        self.rowcount = total
        return self

    def executescript(self, sql: str) -> "Cursor":
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

    def fetchone(self) -> dict | None:
        if self._pos >= len(self._result):
            return None
        row = self._result[self._pos]
        self._pos += 1
        return row

    def fetchmany(self, size: int = 1) -> list[dict]:
        chunk = self._result[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk

    def fetchall(self) -> list[dict]:
        remaining = self._result[self._pos:]
        self._pos = len(self._result)
        return remaining

    def close(self) -> None:
        self._result = []
        self._pos = 0

    def __iter__(self) -> "Cursor":
        return self

    def __next__(self) -> dict:
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row

    # ── Internal ──────────────────────────────────────────────────────────────

    def _set_select_result(self, rows: list[dict]) -> None:
        self._result = rows
        self._pos = 0
        self.rowcount = -1
        if rows:
            self.description = tuple(
                (k, None, None, None, None, None, None) for k in rows[0]
            )
        else:
            self.description = None
