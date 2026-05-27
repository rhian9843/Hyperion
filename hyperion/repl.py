import struct
import sys
from pathlib import Path

from .database import Database
from .parser import parse, ParseError
from .executor import execute


def handle_meta(cmd: str, db: Database) -> bool | None:
    parts = cmd.strip().split()
    kw    = parts[0].lower()

    if kw == ".exit":
        return None

    if kw == ".tables":
        names = sorted(db.tables)
        print("\n".join(names) if names else "(no tables)")
        return True

    if kw == ".indexes":
        for n, m in sorted(db.indexes.items()):
            print(f"{n} ON {m.table_name}({', '.join(m.columns)})")
        if not db.indexes:
            print("(no indexes)")
        return True

    if kw == ".schema":
        if len(parts) < 2:
            print("Usage: .schema <table>")
            return True
        name = parts[1]
        if name not in db.tables:
            print(f"Error: no table '{name}'")
            return True
        schema  = db.tables[name].schema
        parts: list[str] = []
        for c in schema.columns:
            cdef = (f"{c.name} {c.type}" + (f"({c.size})" if c.type == "TEXT" else "")
                    + ("" if c.nullable else " NOT NULL")
                    + (" UNIQUE" if c.unique else "")
                    + (f" DEFAULT {c.default}" if c.default is not None else "")
                    + (f" CHECK ({c.check})" if c.check is not None else ""))
            parts.append(cdef)
        for fk in schema.foreign_keys:
            parts.append(
                f"FOREIGN KEY ({', '.join(fk.columns)}) "
                f"REFERENCES {fk.ref_table} ({', '.join(fk.ref_columns)})"
            )
        print(f"CREATE TABLE {name} ({', '.join(parts)})")
        return True

    print(f"Unrecognized command: '{cmd}'")
    return True


_CONTINUATION_TOKENS = frozenset({
    "SELECT", "FROM", "WHERE", "AND", "OR", "NOT",
    "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "OUTER", "CROSS", "NATURAL",
    "ON", "SET", "BY", "HAVING", "AS",
    "VALUES", "INSERT", "UPDATE", "DELETE",
    "CREATE", "DROP", "ALTER", "WITH",
    "IN", "LIKE", "IS", "BETWEEN",
})


def _needs_continuation(sql: str) -> bool:
    """Return True when the line clearly needs more input to form a complete statement."""
    stripped = sql.rstrip(";").strip()
    if not stripped:
        return False
    if stripped[-1] in (",", "("):
        return True
    last = stripped.rsplit(None, 1)[-1].upper()
    return last in _CONTINUATION_TOKENS


def _split_statements(text: str) -> list[str]:
    """Split SQL text on ';' outside string literals."""
    stmts: list[str] = []
    buf: list[str] = []
    in_str = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "'" and not in_str:
            in_str = True; buf.append(ch)
        elif ch == "'" and in_str:
            buf.append(ch)
            if i + 1 < len(text) and text[i + 1] == "'":
                buf.append(text[i + 1]); i += 2; continue
            in_str = False
        elif ch == ";" and not in_str:
            stmts.append("".join(buf)); buf = []
        else:
            buf.append(ch)
        i += 1
    if buf:
        stmts.append("".join(buf))
    return stmts


def repl(db: Database) -> None:
    buf: list[str] = []
    while True:
        try:
            text = input("H > " if not buf else "... ").strip()
        except KeyboardInterrupt:
            print()
            buf = []
            continue
        except EOFError:
            print()
            break
        if not text:
            if buf:
                buf = []   # empty line abandons incomplete buffer
            continue
        if text.startswith("."):
            if buf:
                buf = []
            if handle_meta(text, db) is None:
                break
            continue
        buf.append(text)
        if _needs_continuation(text):
            continue     # show "... " prompt for next line
        combined = " ".join(buf)
        buf = []
        for part in _split_statements(combined):
            part = part.strip()
            if not part:
                continue
            try:
                print(execute(parse(part), db))
            except (ParseError, RuntimeError, KeyError, struct.error) as e:
                print(f"Error: {e}")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m hyperion <database_file>")
        sys.exit(1)
    db = Database(sys.argv[1])
    try:
        repl(db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
