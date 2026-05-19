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


def repl(db: Database) -> None:
    while True:
        try:
            text = input("H > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text.startswith("."):
            if handle_meta(text, db) is None:
                break
            continue
        try:
            print(execute(parse(text), db))
        except (ParseError, RuntimeError, KeyError, struct.error) as e:
            print(f"Error: {e}")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m hyperion <database_file>")
        sys.exit(1)
    db = Database(Path(sys.argv[1]))
    try:
        repl(db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
