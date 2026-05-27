"""Authorizer constants and helper (mirrors sqlite3 module)."""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    pass

# Return codes
SQLITE_OK     = 0
SQLITE_DENY   = 1
SQLITE_IGNORE = 2

# Action codes
SQLITE_CREATE_TABLE  = 1
SQLITE_CREATE_INDEX  = 14
SQLITE_DELETE        = 9
SQLITE_DROP_TABLE    = 11
SQLITE_DROP_INDEX    = 23
SQLITE_INSERT        = 18
SQLITE_READ          = 20
SQLITE_SELECT        = 21
SQLITE_TRANSACTION   = 22
SQLITE_UPDATE        = 23

# Map AST op → action code
_ACTION_MAP: dict[str, int] = {
    "SELECT":       SQLITE_SELECT,
    "SELECT_NOFROM": SQLITE_SELECT,
    "JOIN":         SQLITE_SELECT,
    "SET_OP":       SQLITE_SELECT,
    "RECURSIVE_CTE": SQLITE_SELECT,
    "INSERT":       SQLITE_INSERT,
    "UPSERT":       SQLITE_INSERT,
    "UPDATE":       SQLITE_UPDATE,
    "DELETE":       SQLITE_DELETE,
    "TRUNCATE":     SQLITE_DELETE,
    "CREATE_TABLE": SQLITE_CREATE_TABLE,
    "DROP_TABLE":   SQLITE_DROP_TABLE,
    "CREATE_INDEX": SQLITE_CREATE_INDEX,
    "DROP_INDEX":   SQLITE_DROP_INDEX,
    "BEGIN":        SQLITE_TRANSACTION,
    "COMMIT":       SQLITE_TRANSACTION,
    "ROLLBACK":     SQLITE_TRANSACTION,
}


def check_authorizer(fn: Callable, stmt: dict) -> int:
    """Call fn(action, table, None, None, None). Raises RuntimeError on DENY.

    Returns SQLITE_IGNORE when the caller should skip execution, SQLITE_OK otherwise.
    """
    op = stmt.get("op", "")
    action = _ACTION_MAP.get(op)
    if action is None:
        return SQLITE_OK
    table = stmt.get("table") or stmt.get("left_table") or ""
    result = fn(action, table, None, None, None)
    if result == SQLITE_DENY:
        msg = f"Access denied: {op}"
        if table:
            msg += f" on '{table}'"
        raise RuntimeError(msg)
    return result if result in (SQLITE_OK, SQLITE_IGNORE) else SQLITE_OK
