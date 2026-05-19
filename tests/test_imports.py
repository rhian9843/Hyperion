import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

"""
test_imports.py
---------------
Verifies that:
  1. Every submodule in the hyperion package can be imported independently.
  2. Every public name that existed in the old hyperion.py is accessible
     from the top-level `import hyperion`.

Run:
    python test_imports.py
"""

import sys

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
errors = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global errors
    if ok:
        print(f"  {PASS} {label}")
    else:
        print(f"  {FAIL} {label}" + (f"  →  {detail}" if detail else ""))
        errors += 1


# ── 1. Each submodule imports cleanly ─────────────────────────────────────────

print("\n[ submodule imports ]")
submodules = [
    "hyperion.constants",
    "hyperion.schema",
    "hyperion.btree",
    "hyperion.catalog",
    "hyperion.wal",
    "hyperion.pager",
    "hyperion.encoding",
    "hyperion.database",
    "hyperion.where",
    "hyperion.parser",
    "hyperion.executor",
    "hyperion.repl",
]
for mod in submodules:
    try:
        __import__(mod)
        check(mod, True)
    except Exception as e:
        check(mod, False, str(e))

# ── 2. Top-level public names exist ───────────────────────────────────────────

print("\n[ top-level names ]")
import hyperion

public_names = [
    # constants
    "PAGE_SIZE", "INTEGER", "REAL", "TEXT", "DEFAULT_TEXT_SIZE",
    # schema
    "Column", "ForeignKey", "Schema", "serialize_row", "deserialize_row",
    # storage
    "BTree", "Catalog", "TableMeta", "IndexMeta", "WAL", "Pager",
    # encoding
    "_encode_index_key", "_encode_composite_key",
    "_make_index_key", "_split_index_key",
    "_IDX_KEY_SZ", "_KEY_SIGN",
    "_apply_order_limit", "_apply_set_op",
    # core
    "Database",
    # where
    "WhereClause", "_exec_correlated_subquery",
    "_instantiate_correlated", "_try_resolve_outer_ref",
    # parser
    "ParseError", "parse", "_tokenize", "_parse_tokens",
    "_parse_one_condition", "_parse_where",
    # executor
    "execute", "_execute_inner", "_rows_for_stmt", "_format_rows",
    # repl
    "handle_meta", "repl", "main",
]
for name in public_names:
    check(f"hyperion.{name}", hasattr(hyperion, name))

# ── 3. Basic smoke test ────────────────────────────────────────────────────────

print("\n[ smoke test ]")
import tempfile
from pathlib import Path

tmp = Path(tempfile.mktemp(suffix=".db"))
try:
    db = hyperion.Database(tmp)
    db.begin()
    db.create_table(hyperion.Schema("users", [
        hyperion.Column("id",   hyperion.INTEGER),
        hyperion.Column("name", hyperion.TEXT, 64),
    ]))
    db.insert("users", {"id": 1, "name": "Alice"})
    db.insert("users", {"id": 2, "name": "Bob"})
    db.commit()

    rows = db.select("users", None, None)
    check("insert + select returns 2 rows", len(rows) == 2)
    check("row data correct", rows[0]["name"] == "Alice")

    ast = hyperion.parse("SELECT * FROM users WHERE id = 2")
    check("parse() returns dict", isinstance(ast, dict))
    check("parse() op is SELECT", ast["op"] == "SELECT")

    result = hyperion.execute(ast, db)
    check("execute() returns string", isinstance(result, str))
    check("execute() output contains Bob", "Bob" in result)

    db.close()
    check("db.close() without error", True)
finally:
    tmp.unlink(missing_ok=True)
    tmp.with_suffix(".wal").unlink(missing_ok=True)

# ── Summary ────────────────────────────────────────────────────────────────────

print()
if errors == 0:
    print(f"{PASS} All checks passed.")
else:
    print(f"{FAIL} {errors} check(s) failed.")
    sys.exit(1)
