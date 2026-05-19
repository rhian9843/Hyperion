import struct
from typing import Any

from .database import Database
from .encoding import _apply_set_op


def _rows_for_stmt(stmt: dict, db: "Database") -> list[dict]:
    """Execute any SELECT-like statement and return its rows."""
    op = stmt["op"]
    if op == "SELECT":
        return db.select(stmt["table"], stmt["columns"], stmt["where"],
                         stmt.get("order_by"), stmt.get("limit"),
                         stmt.get("group_by"), stmt.get("having"),
                         stmt.get("distinct", False))
    if op == "JOIN":
        return db.join(stmt["left_table"], stmt["right_table"],
                       stmt["on_left"], stmt["on_right"],
                       stmt["columns"], stmt["where"],
                       stmt.get("order_by"), stmt.get("limit"),
                       stmt.get("join_type", "INNER"),
                       stmt.get("left_alias"), stmt.get("right_alias"))
    if op == "SET_OP":
        left  = _rows_for_stmt(stmt["left"],  db)
        right = _rows_for_stmt(stmt["right"], db)
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

    if op == "CREATE_TABLE":
        db.create_table(Schema(name=stmt["name"], columns=stmt["columns"],
                               foreign_keys=stmt.get("foreign_keys", [])))
        return f"Table '{stmt['name']}' created."

    if op == "DROP_TABLE":
        db.drop_table(stmt["name"])
        return f"Table '{stmt['name']}' dropped."

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
        db.create_index(stmt["idx_name"], stmt["table"], stmt["cols"])
        cols_str = ", ".join(stmt["cols"])
        return f"Index '{stmt['idx_name']}' created on {stmt['table']}({cols_str})."

    if op == "DROP_INDEX":
        db.drop_index(stmt["idx_name"])
        return f"Index '{stmt['idx_name']}' dropped."

    if op == "INSERT":
        meta      = db._meta(stmt["table"])
        col_names = stmt["col_names"] or [c.name for c in meta.schema.columns]
        values    = stmt["values"]
        if len(col_names) != len(values):
            raise RuntimeError(
                f"Column/value mismatch: {len(col_names)} columns, {len(values)} values"
            )
        parsed: dict[str, Any] = {}
        for name, val in zip(col_names, values):
            parsed[name] = None if val.upper() == "NULL" else val
        # Fill any omitted columns from DEFAULT, or NULL
        for col in meta.schema.columns:
            if col.name not in parsed:
                parsed[col.name] = col.default  # None if no DEFAULT
        db.insert(stmt["table"], parsed)
        return "1 row inserted."

    if op == "SELECT":
        rows = db.select(stmt["table"], stmt["columns"], stmt["where"],
                         stmt.get("order_by"), stmt.get("limit"),
                         stmt.get("group_by"), stmt.get("having"),
                         stmt.get("distinct", False))
        return _format_rows(rows, stmt["columns"])

    if op == "JOIN":
        rows = db.join(stmt["left_table"], stmt["right_table"],
                       stmt["on_left"], stmt["on_right"],
                       stmt["columns"], stmt["where"],
                       stmt.get("order_by"), stmt.get("limit"),
                       stmt.get("join_type", "INNER"),
                       stmt.get("left_alias"), stmt.get("right_alias"))
        return _format_rows(rows, stmt["columns"])

    if op == "SET_OP":
        rows = _rows_for_stmt(stmt, db)
        cols = stmt["left"].get("columns")
        return _format_rows(rows, cols)

    if op == "UPDATE":
        n = db.update(stmt["table"], stmt["assignments"], stmt["where"])
        return f"{n} row{'s' if n != 1 else ''} updated."

    if op == "DELETE":
        n = db.delete(stmt["table"], stmt["where"])
        return f"{n} row{'s' if n != 1 else ''} deleted."

    raise RuntimeError(f"Unknown op: {op}")


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
