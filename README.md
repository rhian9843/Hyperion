# Hyperion

An embedded relational database engine written in pure Python. Hyperion implements a substantial subset of SQL on top of a B-tree storage layer with a write-ahead log, page checksums, MVCC snapshot isolation, and a PEP 249-compatible API.

No C extensions. No external runtime dependencies. `pip install` and go.

---

## Features

### Storage
- **B-tree** page storage with 4 KB pages and overflow-page chains for large rows
- **Write-ahead log (WAL)** — crash-safe commits; WAL is checkpointed before every LOCK_EX release so concurrent readers always see a fully current main file
- **MVCC snapshot isolation** — copy-on-write pager; readers never block writers and never see uncommitted data
- **Per-page CRC-32 checksums** — silent corruption is detected on every page read
- **VACUUM** — compacts the database file, reclaims space from deleted rows
- **In-memory mode** — `Database(":memory:")` uses a dict-backed pager; no file I/O

### SQL — DDL
- `CREATE / DROP TABLE [IF NOT EXISTS]`, `CREATE TABLE AS SELECT`, `CREATE TEMP TABLE`
- `ALTER TABLE RENAME TO`, `RENAME COLUMN`, `ADD COLUMN`, `DROP COLUMN`
- `CREATE / DROP VIEW`, `CREATE / DROP INDEX [IF NOT EXISTS]`
- `CREATE / DROP TRIGGER` — `BEFORE / AFTER / INSTEAD OF`, `INSERT / UPDATE / DELETE`, `FOR EACH ROW`, `WHEN`, `RAISE(ABORT|FAIL|IGNORE|ROLLBACK, …)`
- Column constraints: `PRIMARY KEY` (single & composite), `AUTOINCREMENT`, `UNIQUE`, `NOT NULL`, `DEFAULT`, `CHECK`, `FOREIGN KEY … ON DELETE/UPDATE CASCADE/SET NULL`
- Generated / computed columns: `col AS (expr) STORED / VIRTUAL`
- Column types: `INTEGER`, `REAL`, `TEXT / VARCHAR(n)`, `BLOB`, `BOOLEAN`, `DATE`, `DATETIME / TIMESTAMP`, size aliases (`TINYINT`, `SMALLINT`, `BIGINT`)

### SQL — DML
- `INSERT` — single row, multi-row `VALUES`, `INSERT INTO … SELECT`, `INSERT OR REPLACE / IGNORE`, `ON CONFLICT DO NOTHING / UPDATE SET`, `RETURNING`
- `UPDATE` — `SET` expressions, `WHERE`, `LIMIT`, `RETURNING`
- `DELETE` — `WHERE`, `LIMIT`, `RETURNING`
- `TRUNCATE TABLE`

### SQL — Queries
- `SELECT` with `WHERE`, `GROUP BY`, `HAVING`, `ORDER BY` (name or positional, `NULLS FIRST/LAST`), `LIMIT / OFFSET`, `DISTINCT`
- **Joins** — `INNER`, `LEFT`, `RIGHT`, `FULL OUTER`, `CROSS`, `NATURAL`; multiple chained joins; multi-table implicit `FROM`
- **Subqueries** — scalar in `SELECT` list, derived table in `FROM`, correlated `WHERE … IN / EXISTS`
- **Set operations** — `UNION [ALL]`, `INTERSECT`, `EXCEPT`
- **CTEs** — `WITH … AS (…)`, `WITH RECURSIVE … UNION ALL`
- **Window functions** — `ROW_NUMBER`, `RANK`, `DENSE_RANK`, `NTILE`, `LAG`, `LEAD`, `FIRST_VALUE`, `LAST_VALUE`, `SUM/AVG/MIN/MAX/COUNT OVER (…)`; frame bounds; named `WINDOW` clause
- `LATERAL` join; multi-column row comparison `(a, b) IN (SELECT …)`
- `SELECT` without `FROM` — `SELECT 1 + 1`, `SELECT UPPER('hello')`

### SQL — Expressions & Functions
- Arithmetic, string concatenation (`||`), comparison, `BETWEEN`, `LIKE [ESCAPE]`, `GLOB`, `IN`, `EXISTS`, `IS [NOT] NULL`
- `CASE WHEN … THEN … ELSE … END`, `CAST`, `COALESCE`, `NULLIF`, `IFNULL`
- Boolean literals `TRUE / FALSE`; `CURRENT_TIMESTAMP / CURRENT_DATE / CURRENT_TIME`
- Aggregates: `COUNT`, `SUM`, `AVG`, `MIN`, `MAX`, `GROUP_CONCAT / STRING_AGG`, `COUNT(DISTINCT …)`
- String: `UPPER`, `LOWER`, `LENGTH`, `SUBSTR`, `TRIM`, `LTRIM`, `RTRIM`, `REPLACE`, `INSTR`, `PRINTF / FORMAT`
- Math: `ABS`, `ROUND`, `CEIL`, `FLOOR`, `MOD`, `RANDOM`, `RANDOMBLOB`
- Type: `TYPEOF`, `LAST_INSERT_ROWID`
- JSON: `json_extract`, `json_object`, `json_array`, `json_each`, `json_tree`

### SQL — Operational
- `BEGIN / COMMIT / ROLLBACK`
- `SAVEPOINT / RELEASE SAVEPOINT / ROLLBACK TO SAVEPOINT`
- `PRAGMA foreign_keys`, `PRAGMA table_info`, `PRAGMA index_list / index_info`, `PRAGMA integrity_check`
- `ANALYZE` — collects per-table/index statistics for the query optimizer
- `EXPLAIN / EXPLAIN QUERY PLAN`
- Quoted identifiers — `"col name"` or `` `col` ``

### Query Optimizer
- Cost-based join reordering for chains of inner equijoins
- Index seeks for equality and range predicates (`=`, `<`, `>`, `BETWEEN`)
- Index ORDER BY elimination — skips the sort step when the index already delivers rows in the right order
- Index LIMIT early termination — stops the scan as soon as `LIMIT` rows are collected
- Prepared statement plan cache — parse once, bind many times; `?` / `:name` placeholders keep the cache key stable across different parameter values

### Concurrency & Safety
- **File locking** — `flock` shared/exclusive protocol; non-blocking init trylock avoids blocking concurrent openers
- **Readers-writer lock** (`_RWLock`) — concurrent `SELECT` queries run in parallel; writes serialise only against each other; reentrant write prevents self-deadlock
- **Read-only mode** — `Database(path, readonly=True)` or `db.as_readonly()` context manager; any write raises immediately
- **Query timeout** — `execute(sql, timeout_ms=5000)` raises after the deadline
- **Max-rows guard** — `db.max_rows = 10_000` or per-call `execute(sql, max_rows=…)` raises before materialising a runaway result set
- **Page checksums** — every page carries a CRC-32; corruption is detected on read, not just at `PRAGMA integrity_check` time

### Python API
- **PEP 249** cursor interface — `execute`, `executemany`, `executescript`, `fetchone`, `fetchall`, `fetchmany`, `.description`, `.rowcount`, `.lastrowid`
- **Parameter binding** — positional `?` and named `:name` / `$name`
- **Context manager** — `with Database(":memory:") as db:` auto-closes; `with db:` wraps a transaction
- **Async API** — `AsyncDatabase` / `AsyncCursor` wraps every blocking call in `run_in_executor`; rows are buffered in chunks of 256 to amortise thread-pool overhead
- **Custom functions** — `db.create_function(name, n_args, fn)` and `db.create_aggregate(name, n_args, cls)`
- **Authorizer hook** — `db.set_authorizer(fn)` gates every operation; return `SQLITE_OK / SQLITE_DENY / SQLITE_IGNORE`
- **Schema metadata** — `db.set_meta / get_meta / delete_meta` attaches key-value tags to any catalog object (useful for LLM text-to-SQL context)
- **`db.iterdump()`** — yields SQL statements that recreate the full database
- **Structured exceptions** — `ParseError`, `NoSuchTableError`, `UniqueConstraintError`, `ForeignKeyConstraintError`, `TransactionError`, and more; all inherit from `HyperionError`
- **`db.row_factory`** — pluggable row format; defaults to `dict`

---

## Installation

```bash
git clone https://github.com/rhian9843/Hyperion.git
cd Hyperion
pip install -e .
```

No runtime dependencies. Python 3.10+ required.

---

## Quick Start

```python
from hyperion import Database

db = Database("mydb.hdb")

db.execute("""
    CREATE TABLE users (
        id   INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT    NOT NULL,
        age  INTEGER
    )
""")

db.execute("INSERT INTO users (name, age) VALUES (?, ?)", ("Alice", 30))
db.execute("INSERT INTO users (name, age) VALUES (?, ?)", ("Bob",   25))

rows = db.execute("SELECT * FROM users WHERE age > ?", (20,)).fetchall()
for row in rows:
    print(row["id"], row["name"])

db.close()
```

### In-Memory Database

```python
db = Database(":memory:")
db.execute("CREATE TABLE t (x INTEGER)")
db.execute("INSERT INTO t VALUES (1), (2), (3)")
print(db.execute("SELECT SUM(x) AS total FROM t").fetchone()["total"])  # 6
```

### Explicit Transactions

```python
db = Database("mydb.hdb")
db.begin()
try:
    db.execute("INSERT INTO users (name) VALUES ('Carol')")
    db.execute("INSERT INTO users (name) VALUES ('Dave')")
    db.commit()
except Exception:
    db.rollback()
    raise
```

### Savepoints

```python
db.begin()
db.execute("INSERT INTO t VALUES (1)")
db.savepoint("sp1")
db.execute("INSERT INTO t VALUES (2)")
db.rollback_to_savepoint("sp1")   # row 2 discarded, row 1 kept
db.commit()
```

### Async API

```python
import asyncio
from hyperion.async_db import AsyncDatabase

async def main():
    db = AsyncDatabase("mydb.hdb")
    await db.execute("CREATE TABLE t (id INTEGER, val TEXT)")
    await db.execute("INSERT INTO t VALUES (?, ?)", (1, "hello"))
    rows = await (await db.execute("SELECT * FROM t")).fetchall()
    print(rows)
    await db.close()

asyncio.run(main())
```

### Custom Functions

```python
import math

db.create_function("sqrt", 1, math.sqrt)
row = db.execute("SELECT sqrt(144) AS s").fetchone()
print(row["s"])  # 12.0
```

### Read-Only Mode

```python
# Open permanently read-only
ro_db = Database("mydb.hdb", readonly=True)

# Or restrict temporarily
with db.as_readonly():
    rows = db.execute("SELECT * FROM users").fetchall()
# writes allowed again outside the block
```

### Schema Metadata (for LLM agents)

```python
db.set_meta("table",  "users",       "description", "Registered application users")
db.set_meta("column", "users.email", "description", "Primary contact address, must be unique")

tags = db.get_meta("column", "users.email")
# {"description": "Primary contact address, must be unique"}
```

---

## Running Tests

```bash
pip install pytest
pytest tests/
```

The test suite has ~1 200 tests covering SQL correctness, storage, WAL crash recovery, concurrency, the async API, and the query optimizer.

---

## Project Structure

```
hyperion/
├── __init__.py         public API re-exports
├── database.py         Database class, _RWLock, connection lifecycle
├── pager.py            Pager (file) and MemoryPager (":memory:")
├── wal.py              Write-ahead log: frames, commit markers, checkpointing
├── btree.py            B-tree: insert, lookup, range scan, reverse scan
├── catalog.py          In-memory catalog: tables, indexes, views, triggers
├── schema.py           Column and Schema types
├── encoding.py         Row and index key serialisation / deserialisation
├── checksum.py         Per-page CRC-32 stamping and verification
├── parser.py           SQL parser → AST dict
├── executor.py         Statement dispatcher
├── query.py            SELECT / JOIN execution, optimizer integration
├── optimizer.py        Cost-based join reordering, index selection
├── dml.py              INSERT / UPDATE / DELETE / TRUNCATE
├── ddl.py              CREATE / DROP / ALTER TABLE, VIEW, INDEX, TRIGGER
├── constraints.py      FK, UNIQUE, NOT NULL, CHECK enforcement
├── triggers.py         Trigger firing and RAISE handling
├── expr.py             Expression evaluator (arithmetic, functions, CASE)
├── where.py            WhereClause predicate evaluation
├── introspect.py       PRAGMA, EXPLAIN, integrity_check
├── json_funcs.py       JSON scalar functions
├── cursor.py           PEP 249 Cursor, parameter binding, plan cache
├── async_db.py         AsyncDatabase and AsyncCursor
├── auth.py             Authorizer hook support
├── errors.py           Typed exception hierarchy
├── repl.py             Interactive REPL (python -m hyperion)
└── constants.py        Page size, cell layout, type constants
```

---

## REPL

```bash
python -m hyperion mydb.hdb
```

```
H > CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT);
H > INSERT INTO t VALUES (1, 'hello');
H > SELECT * FROM t;
{'id': 1, 'name': 'hello'}
H > .quit
```

---

## Limitations

- Single-process only — WAL-based file locking protects against corruption but there is no network protocol or server mode
- No `ALTER TABLE` column type changes
- Vector / ANN search, full-text search (BM25), and hybrid retrieval are not yet implemented
