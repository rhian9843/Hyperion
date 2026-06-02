import struct
import sys
from pathlib import Path

from .database import Database
from .errors import HyperionError
from .parser import parse, ParseError
from .executor import execute, _format_rows, RowResult


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
    """Split SQL text on ';' outside string literals and BEGIN...END blocks."""
    stmts: list[str] = []
    buf: list[str] = []
    in_str = False
    begin_depth = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "'" and not in_str:
            in_str = True; buf.append(ch)
        elif ch == "'" and in_str:
            buf.append(ch)
            if i + 1 < n and text[i + 1] == "'":
                buf.append(text[i + 1]); i += 2; continue
            in_str = False
        elif ch == ";" and not in_str:
            if begin_depth == 0:
                stmts.append("".join(buf)); buf = []
            else:
                buf.append(ch)
        else:
            buf.append(ch)
            if not in_str:
                # Check for BEGIN / END keyword boundaries
                joined = "".join(buf)
                upper = joined.upper()
                # A keyword ends at the current position and is preceded by
                # a non-word character (or start of text).
                def _is_keyword_end(word: str) -> bool:
                    wl = len(word)
                    if not upper.endswith(word):
                        return False
                    pos = len(upper) - wl - 1
                    if pos >= 0 and (upper[pos].isalnum() or upper[pos] == '_'):
                        return False
                    nxt = i + 1
                    if nxt < n and (text[nxt].isalnum() or text[nxt] == '_'):
                        return False
                    return True
                if _is_keyword_end("BEGIN"):
                    # BEGIN TRANSACTION or BEGIN; → SQL transaction, not a block
                    rest = text[i + 1:].lstrip()
                    if (not rest.upper().startswith("TRANSACTION")
                            and not rest.startswith(";")):
                        begin_depth += 1
                elif _is_keyword_end("END") and begin_depth > 0:
                    begin_depth -= 1
        i += 1
    if buf:
        stmts.append("".join(buf))
    return stmts


_BANNER = """\
╔══════════════════════════════════════════════╗
║            Hyperion Database                 ║
║   Type '.help' for commands, '.exit' to quit ║
╚══════════════════════════════════════════════╝"""

_HELP = """\
Commands:
  .tables           List all tables
  .indexes          List all indexes
  .schema <table>   Show table definition
  .exit / .quit     Exit the REPL

Separate multiple statements with ;"""


def _print_result(result, /, *, fancy: bool = False) -> None:
    """Print the result of a single statement."""
    if isinstance(result, RowResult):
        print(_format_rows(result.rows, result.columns, fancy=fancy))
    elif result is not None:
        print(result)


def repl(db: Database) -> None:
    _tty = sys.stdout.isatty()
    if _tty:
        print(_BANNER)
        print()
    buf: list[str] = []
    while True:
        try:
            text = input("hyperion> " if not buf else "       -> ").strip()
        except KeyboardInterrupt:
            print()
            buf = []
            continue
        except EOFError:
            print()
            break
        if not text:
            if buf:
                buf = []
            continue
        if text.startswith("."):
            if buf:
                buf = []
            if text.lower() in (".help", ".h"):
                print(_HELP)
                continue
            if handle_meta(text, db) is None:
                if _tty:
                    print("Bye.")
                break
            continue
        buf.append(text)
        if _needs_continuation(text):
            continue
        combined = " ".join(buf)
        buf = []
        for part in _split_statements(combined):
            part = part.strip()
            if not part:
                continue
            try:
                result = execute(parse(part), db)
                _print_result(result, fancy=_tty)
            except (HyperionError, ParseError, RuntimeError, KeyError, struct.error) as e:
                print(f"Error: {e}")


def _strip_comments(sql: str) -> str:
    """Remove -- line comments from SQL text."""
    lines = []
    for line in sql.splitlines():
        idx = line.find("--")
        lines.append(line[:idx] if idx != -1 else line)
    return "\n".join(lines)


def run_sql_file(db: Database, path: str) -> None:
    """Execute every statement in a .sql file and print SELECT results."""
    try:
        sql = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        print(f"Error reading '{path}': {e}", file=sys.stderr)
        sys.exit(1)
    sql = _strip_comments(sql)
    for part in _split_statements(sql):
        part = part.strip()
        if not part:
            continue
        try:
            result = execute(parse(part), db)
            if isinstance(result, RowResult) and result.rows:
                print(_format_rows(result.rows, result.columns))
        except (HyperionError, ParseError, RuntimeError, KeyError, struct.error) as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m hyperion <database_file> [script.sql]")
        sys.exit(1)
    db = Database(sys.argv[1])
    try:
        if len(sys.argv) >= 3:
            run_sql_file(db, sys.argv[2])
            if db.in_transaction:
                db.commit()
        else:
            repl(db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
