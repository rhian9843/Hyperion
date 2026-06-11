# Hyperion — Work Backlog

## Bugs (silent wrong behaviour)

- [x] Fix `CREATE INDEX ON t(expr)` with multi-token expressions — `CREATE INDEX ON users (age / 10)` raised `NoSuchColumnError: Column '10' not found`; the fallback index column parser appended each token individually, splitting `age / 10` into `['age', '/', '10']`; fixed by collecting tokens until `,` or `)` and joining them as a single expression string

- [x] Fix silent column-miss in WHERE — `WHERE nonexistent = 1` returns zero rows instead of an error
- [x] Fix multi-row INSERT silently dropping extra rows — `VALUES (1,'a'), (2,'b')` only inserts the first tuple with no warning
- [x] Fix `struct.error` leaking on integer overflow — wrap as user-facing `RuntimeError`
- [x] Fix correlated subquery outer ref on left side — `outer.col = inner.col` fails; only the right side resolves today
- [x] Fix `LIMIT x OFFSET y` — OFFSET is parsed but silently ignored; rows are not skipped
- [x] Fix table-qualified column in SELECT projection — `SELECT t.id FROM t` raises a `KeyError`
- [x] Fix escaped single quotes in string literals — `'it''s fine'` tokenizes to `['it', 's fine']` because `_TOKEN_RE` uses `'[^']*'` which stops at the first `'`
- [x] Fix `SELECT id AS uid FROM t` — the column parser treats `AS` and `uid` as additional column names instead of recognising the alias; columns list becomes `['id', 'AS', 'uid']`
- [x] Fix `DROP INDEX IF EXISTS idx_name` — parser puts `IF` as the index name instead of skipping the `IF EXISTS` guard
- [x] Fix VARCHAR(n) silent truncation — inserting a value longer than the column size silently truncates instead of raising an error
- [x] Fix `CREATE UNIQUE INDEX` not enforcing uniqueness on INSERT/UPDATE — `CREATE UNIQUE INDEX idx ON t(col)` creates the index but duplicate values are silently accepted; only `col UNIQUE` column constraints and multi-column `UNIQUE(a,b)` constraints are checked today

## Performance

- [x] Fix `_check_fk_child` always doing a full parent scan — use index lookup when one exists on the referenced column
- [x] Fix `get_page` marking every read as dirty — split into read path / write path to avoid flushing unchanged pages on every commit
- [x] Cache non-correlated subquery results — `WHERE id IN (SELECT ...)` re-runs the inner query once per outer row even when the result never changes

## Missing SQL — WHERE / Expressions

- [x] Parenthesized WHERE groups — `WHERE (a = 1 OR b = 2) AND c = 3` (parser has no `(` grouping in conditions)
- [x] `NOT` prefix operator — `WHERE NOT col = 1` (only NOT IN / NOT EXISTS work today)
- [x] `BETWEEN x AND y`
- [x] Column aliases — `SELECT id AS uid, name AS full_name FROM t` (alias must also flow into ORDER BY / GROUP BY / HAVING)
- [x] `CASE WHEN ... THEN ... ELSE ... END` expressions
- [x] `COALESCE(x, y, ...)` / `NULLIF(x, y)` / `IFNULL(x, y)`
- [x] `CAST(x AS type)`
- [x] Arithmetic expressions in SELECT and WHERE — `SELECT price * qty`, `WHERE price * 1.1 > 100`
- [x] String concatenation operator — `SELECT first || ' ' || last`
- [x] Expression evaluation in SELECT list — currently only bare column names are supported; functions and arithmetic resolve to nothing
- [x] Fix `NOT IN` NULL semantics — `x NOT IN (1, NULL)` should be `UNKNOWN` per SQL standard
- [x] `GLOB` operator — case-sensitive wildcard matching (`*` = any string, `?` = any char); SQLite built-in alongside LIKE
- [x] `LIKE ... ESCAPE 'char'` — custom escape character for LIKE patterns; `LIKE '50\%' ESCAPE '\'` to match a literal percent

## Missing SQL — Queries

- [x] `OFFSET` (with LIMIT) — `SELECT ... LIMIT 10 OFFSET 20`
- [x] Multiple JOINs — `FROM a JOIN b ON ... JOIN c ON ...` (parser exits after the first JOIN today)
- [x] Multi-table implicit FROM — `SELECT * FROM a, b WHERE a.id = b.id` (parser accepts only one table name)
- [x] `INSERT INTO ... SELECT ...` — bulk insert from a query result
- [x] Multi-row `INSERT` — `INSERT INTO t VALUES (1,'a'), (2,'b')`
- [x] Subquery in `FROM` — `SELECT * FROM (SELECT ...) AS alias` (derived tables)
- [x] CTE — `WITH cte AS (SELECT ...) SELECT ... FROM cte`
- [x] Window functions — `ROW_NUMBER() OVER (...)`, `RANK()`, `LAG()`, etc.
- [x] `SELECT` without `FROM` — `SELECT 1`, `SELECT UPPER('hello')`
- [x] Batch statements — multiple `;`-separated statements in one `execute()` call
- [x] Scalar subquery in SELECT list — `SELECT name, (SELECT COUNT(*) FROM orders WHERE user_id = u.id) FROM users u` (currently tokenized into garbage column names)
- [x] Multi-line SQL in REPL — REPL reads one line per `input()` call; statements spanning multiple lines are silently dropped
- [x] `ORDER BY` column position — `ORDER BY 1, 2` (positional reference)
- [x] `NULLS FIRST` / `NULLS LAST` in ORDER BY — `ORDER BY col NULLS FIRST`
- [x] `TRUE` / `FALSE` literals in expressions — `WHERE active = TRUE`
- [x] `CURRENT_TIMESTAMP` / `CURRENT_DATE` / `CURRENT_TIME` scalar values

## Missing SQL — DDL / DML

- [x] `CREATE TABLE IF NOT EXISTS` / `DROP TABLE IF EXISTS` / `CREATE INDEX IF NOT EXISTS`
- [x] `PRIMARY KEY` constraint syntax — `id INTEGER PRIMARY KEY` (implies NOT NULL + UNIQUE; auto-generates a unique index)
- [x] `AUTOINCREMENT` / `AUTO_INCREMENT`
- [x] Multi-column table-level `UNIQUE (col1, col2)` constraint
- [x] `CREATE TABLE ... AS SELECT ...`
- [x] `UPSERT` — `INSERT OR REPLACE` / `INSERT OR IGNORE` / `ON CONFLICT`
- [x] `TRUNCATE TABLE t`
- [x] `ON DELETE CASCADE` / `ON DELETE SET NULL` for foreign keys
- [x] `ON UPDATE CASCADE` / `ON UPDATE SET NULL` for foreign keys — today only ON DELETE is planned; ON UPDATE is equally common
- [x] Composite `PRIMARY KEY (col1, col2)` — table-level multi-column primary key constraint; existing item only covers single-column `id INTEGER PRIMARY KEY`
- [x] `LIMIT` in `UPDATE` / `DELETE` — `DELETE FROM t WHERE x = 1 LIMIT 10`; SQLite supports this; useful for batched deletes
- [x] `RETURNING` clause — `INSERT INTO t VALUES (...) RETURNING id`
- [x] Views — `CREATE VIEW v AS SELECT ...` / `DROP VIEW`
- [x] `SAVEPOINT` / `RELEASE SAVEPOINT` / `ROLLBACK TO SAVEPOINT`

## Missing SQL — Types

- [x] `BLOB` / `BYTES` column type — variable-length binary storage
- [x] `BOOLEAN` column type — stored as 0/1 INTEGER with TRUE/FALSE literals
- [x] `DATE` / `DATETIME` / `TIMESTAMP` — stored as TEXT with ISO-8601 affinity (SQLite-style)
- [x] Integer size aliases — `TINYINT`, `SMALLINT`, `BIGINT` mapped to INTEGER (SQLite-style type affinity)

## Missing SQL — String / Scalar Functions

- [x] String functions — `UPPER`, `LOWER`, `LENGTH`, `SUBSTR`, `TRIM`, `LTRIM`, `RTRIM`
- [x] `REPLACE(str, from, to)` / `INSTR(str, sub)` / `PRINTF` / `FORMAT`
- [x] Math functions — `ABS`, `ROUND`, `CEIL`, `FLOOR`, `MOD`
- [x] `RANDOM()` / `RANDOMBLOB(n)`
- [x] `TYPEOF(x)` — returns the storage class of a value
- [x] `NULLIF(x, y)` / `COALESCE` (listed above but also callable in SELECT list once expression evaluation works)
- [x] `GROUP_CONCAT(col)` / `STRING_AGG(col, sep)` aggregate — `_AGG_RE` today only recognises COUNT/MIN/MAX/SUM/AVG

## Missing Operational SQL

- [x] `PRAGMA foreign_keys = ON/OFF` — enable/disable FK enforcement at runtime
- [x] `PRAGMA table_info(t)` — returns column metadata (name, type, notnull, dflt_value, pk)
- [x] `PRAGMA index_list(t)` / `PRAGMA index_info(idx)` — index introspection
- [x] `VACUUM` — rebuild database file to reclaim space from deleted rows and pages
- [x] Quoted identifiers — `"column name"` or `` `column` `` for reserved words or names with spaces

## Missing — Concurrency & Safety

- [x] File locking — shared/exclusive lock protocol so multiple connections to the same file do not corrupt the database
- [x] In-memory databases — `Database(":memory:")` backed by a dict instead of file I/O; critical for testing and temporary workloads

## Missing — Query Execution

- [x] Query optimizer / cost-based planner — today multi-join queries do nested full scans; a cost model is needed so the engine picks the cheapest join order and access path
- [x] `ANALYZE` — collect per-table/index statistics (row count, distinct values) that the query optimizer can use
- [x] `COUNT(DISTINCT col)` / `SUM(DISTINCT col)` — `_AGG_RE` today does not handle the DISTINCT modifier inside aggregate calls
- [x] Expression indexes — `CREATE INDEX idx ON t(UPPER(col))` — index on a computed expression rather than a raw column
- [x] Expressions in `INSERT INTO t VALUES (...)` — `VALUES (1 + 2, 'a' || 'b')` fails because the VALUES parser splits on commas before evaluating; operators are tokenized as separate values instead of expression parts
- [x] JOIN + GROUP BY / aggregation broken at top level — `SELECT name, COUNT(*) FROM users JOIN orders ON ... GROUP BY name` returns all NULLs; the `op == "JOIN"` path in `_execute_inner` calls `db.join()` directly which bypasses GROUP BY handling
- [x] CTE + JOIN column key conflict — `WITH j AS (SELECT u.name FROM users u JOIN orders o ON ...) SELECT name FROM j` fails because JOIN rows have table-qualified keys (`users.name`) but CTE projection expects bare names (`name`); alias stripping in `_exec_cte_select` doesn't strip table prefixes
- [x] CTE tables not resolved in top-level JOIN handler — `SELECT ... FROM cte1 JOIN cte2 ON ...` fails when parsed as `op == "JOIN"` at the top level; the CTE check only exists in `_rows_for_stmt`, not in `_execute_inner`'s JOIN branch

## Missing — DDL / Schema

- [x] Triggers — `CREATE TRIGGER BEFORE/AFTER INSERT/UPDATE/DELETE ON t` with `FOR EACH ROW` body; required by many ORMs and audit-log patterns
- [x] Trigger gap: `UPDATE OF col1, col2` filter — parsed and stored but `_triggers_for` never checks `update_cols`, so the trigger fires on every UPDATE regardless
- [x] Trigger gap: `RAISE(ABORT|FAIL|IGNORE|ROLLBACK, 'msg')` in trigger body — standard SQLite validation pattern; not parsed or executed today
- [x] Trigger gap: expression assignments in `apply_update_row` — `SET col = col + 1` stores the raw string instead of evaluating against the old row, so BEFORE/AFTER UPDATE triggers see the wrong `NEW.col` value
- [x] Trigger gap: `INSTEAD OF` triggers on views — redirect INSERT/UPDATE/DELETE on a view to the underlying base tables
- [x] `CREATE TEMP TABLE` / `CREATE TEMPORARY TABLE` — session-scoped table that is automatically dropped on close
- [x] Recursive CTEs — `WITH RECURSIVE cte AS (base UNION ALL recursive_step) SELECT ...` — needed for trees, graphs, and hierarchical data
- [x] Generated / computed columns — `col INTEGER AS (expr) STORED` / `VIRTUAL`
- [x] `COLLATE` clause — `ORDER BY name COLLATE NOCASE`; Unicode-aware and case-insensitive comparison

## Missing — Functions & Types

- [x] JSON functions — `json_extract(col, '$.key')`, `json_object(...)`, `json_array(...)`, `json_each(...)` — modern apps embed JSON everywhere and LLM outputs are JSON
- [x] Application-defined functions — Python API to register custom scalar and aggregate functions (`db.create_function(name, n_args, fn)`)

## Missing — Introspection

- [x] System catalog table — queryable `_hyperion_master` (equiv. of `sqlite_master`) exposing table/index/view definitions as rows; ORMs and tools depend on this
- [x] `PRAGMA integrity_check` — verify B-tree structure and page consistency
- [x] `EXPLAIN` / `EXPLAIN QUERY PLAN` — show the query execution plan; critical for debugging performance and verifying index usage

## Python DB-API / Convenience Layer

- [x] PEP 249 cursor interface — `db.execute(sql)` / `db.executemany(sql, params)` / `db.executescript(sql)` returning cursor objects with `.fetchone()`, `.fetchall()`, `.fetchmany(n)`, `.rowcount`, `.description` (column name/type metadata)
- [x] Parameter binding — positional `?` and named `:name` / `$name` placeholders so values are passed safely without string formatting (`db.execute("SELECT * FROM t WHERE id = ?", (1,))`)
- [x] Context manager — `with Database(":memory:") as db:` auto-closes; `with db:` wraps an implicit transaction (commit on exit, rollback on exception)
- [x] `db.row_factory` — pluggable row format; default tuple, built-ins for `dict` and named-access rows; user-assignable callable
- [x] `db.set_authorizer(fn)` — callback invoked per SQL operation; return allow/deny/ignore to gate access (security hook, mirrors sqlite3)
- [x] `db.iterdump()` — yield SQL statements that recreate the full database; useful for backup, migration, and test fixtures

## Code Quality

- [x] Refactor `_parse_tokens` (1,420 lines) into per-statement parser functions — the monolithic function makes it hard to isolate bugs and the expressions-in-VALUES bug is a direct consequence of it
- [x] Unify `_execute_inner` and `_rows_for_stmt` execution paths — JOIN+aggregation, CTE resolution, and GROUP BY fixes applied to one path must be manually mirrored to the other; the divergence is the root cause of the JOIN+GROUP BY and CTE+JOIN bugs
- [x] Update module docstring — currently missing joins, aggregates, transactions, constraints, set operations, subqueries

## SQL Layer Gaps

### Query / Parser

- [x] Multi-condition JOIN ON — `ON a.x = b.y AND a.z = b.w`; today the parser enforces a single `left = right` token pair and raises a parse error on anything more complex
- [x] Window function frame bounds — `ROWS BETWEEN N PRECEDING AND CURRENT ROW` / `RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW`; today only the default unbounded frame is supported
- [x] Named WINDOW clause — `SELECT ROW_NUMBER() OVER w ... WINDOW w AS (PARTITION BY x ORDER BY y)`; today every OVER must be fully inline
- [x] `LATERAL` join — `FROM t, LATERAL (SELECT ... WHERE s.id = t.id) AS sub`; needed for correlated table-valued subqueries in FROM
- [x] Multi-column row comparison — `WHERE (col1, col2) IN (SELECT a, b FROM t)` and `WHERE (col1, col2) = (val1, val2)`

### Indexes / Optimizer

- [x] Index bypass under ORDER BY / LIMIT / DISTINCT — both equality and range index scans are gated behind `not order_by and limit is None and not distinct` in `query.py:89`; a query like `SELECT * FROM t WHERE val = 10 ORDER BY id` skips the index on `val` entirely, scans the full table, then sorts in memory; fix requires the planner to use the index for the WHERE predicate and apply ORDER BY / LIMIT as a post-scan step, or to recognise when the index order satisfies the ORDER BY and skip the sort entirely
- [x] Index ORDER BY elimination — when the ORDER BY column matches the index column and direction is ASC, the index already delivers rows in sorted order; the post-scan sort is redundant and should be skipped; for DESC, scan the index in reverse order rather than sorting in memory
- [x] Index LIMIT early termination — when LIMIT is present and there is no ORDER BY on a different column, the index scan should stop as soon as `limit` rows are collected rather than fetching all matching rows first
- [x] Text index ordering — TEXT/VARCHAR index keys are FNV-1a hashes; range predicates (`WHERE name > 'M'`), `BETWEEN`, and `ORDER BY` with index all produce wrong or suboptimal results; text B-tree keys need to be prefix-encoded byte strings so sort order is preserved
- [x] Outer join optimisation — the cost-based join reorderer only runs on chains of INNER equijoins; LEFT / RIGHT / FULL OUTER joins are never reordered regardless of table sizes
- [x] Range predicate index use — `WHERE int_col > 100` never uses an index today; the optimizer only probes indexes for equality (`=`); `scan_range` exists on BTree but is never invoked from the query planner
- [x] True prepared statements — the current `_bind_params` substitutes values into the SQL string *before* parsing, so every call with different parameter values produces a different string and a guaranteed cache miss; a plan cache keyed on the raw template string (`"SELECT ... WHERE id = ?"`) is therefore a no-op for all parameterised queries; fix requires two-phase execution: (1) parse and plan the SQL with `?` placeholders intact and cache that plan, (2) bind actual values at execution time against the already-parsed plan; this also unblocks vector parameter binding — passing a float list as `?` currently serialises it to a string literal `'[0.1, 0.2, 0.3]'` that must be re-parsed at query time

### Storage

- [x] Variable-length row storage — TEXT and BLOB columns have a hard fixed maximum size (TEXT defaults to 255 bytes, page size is 4 096 bytes); rows that overflow a page cannot exist; this blocks storing large documents, JSON payloads, or any binary payload above ~4 000 bytes; requires an overflow-page mechanism (linked extra pages per row)
- [x] Streaming / iterator query results — all queries fully materialise `list[dict]` in memory before the first row is returned; a generator-based execution path is needed so large result sets can be consumed row-by-row without holding everything in RAM
- [x] MVCC / snapshot isolation — today reads are blocked by the exclusive flock held during writes (single-writer model); concurrent readers inside the same process see mid-transaction state; a proper snapshot or copy-on-write read path is needed for multi-connection safety
- [x] WAL checkpointing — the WAL file is deleted immediately after every commit so there is no multi-transaction WAL efficiency; a checkpoint strategy (write-back on threshold, not per-commit) would reduce fsync pressure under write-heavy workloads
- [x] Catalog scalability — the entire catalog (all table schemas, index metadata, ANALYZE stats, trigger definitions) is serialised as a single JSON blob and rewritten on every commit; this degrades linearly with the number of objects and is unsuitable once the schema grows large
- [x] Thread safety — `Database._cache`, `_dirty`, `_txn_depth`, `_catalog`, and `_savepoints` are unsynchronised mutable state; two threads sharing one `Database` object will corrupt each other silently; Python async frameworks (FastAPI, LangChain, asyncio thread pool executors) routinely call synchronous I/O from worker threads — every agent that does this is a data corruption risk; requires a `threading.RLock` per `Database` instance at minimum
- [x] Page checksums — no CRC or hash is stored on individual pages; a single bad write from an OS bug, disk firmware issue, or partial flush goes undetected; `PRAGMA integrity_check` catches structural B-tree violations but not bit-level corruption within a structurally-valid page; for a database storing embeddings and LLM outputs, silent corruption produces wrong answers with no signal

## LLM / Agent Layer Prerequisites

### Bugs that break agent workflows today

- [x] `cursor.description` is `None` on empty result sets — when a SELECT returns zero rows, `description` is set to `None` instead of the column metadata; an agent checking the schema of a table via a zero-row query gets nothing back (verified: `db.execute("SELECT * FROM t WHERE 1=0").description` returns `None`)
- [x] `cursor.lastrowid` is never populated — always `None` after INSERT regardless of `AUTOINCREMENT`; an agent that inserts a row and needs the generated key has no way to retrieve it without a separate `SELECT` call; `last_insert_rowid()` is also not implemented as a SQL function
- [x] `INTEGER PRIMARY KEY` does not alias the B-tree rowid — `lastrowid` returned the internal sequence counter (1, 2, 3…) instead of the user-supplied PK value; `last_insert_rowid()` was equally wrong; also wasted storage serialising the PK both as a column and as a separate B-tree key; fixed by using the column value directly as the rowid and keeping `next_key` as the high-water mark for auto-assignment

### Safety

- [x] Query timeout / cancellation — no mechanism to abort a query after a deadline; LLM-generated SQL can produce accidental cartesian joins or deep recursive CTEs that run indefinitely; needs a `timeout_ms` parameter on `execute()` and a cooperative check inside the execution loop
- [x] Max result rows guard — no built-in limit on rows returned; an agent issuing `SELECT * FROM large_table` will materialise the entire table in memory with no warning; needs a configurable `max_rows` on the `Database` or cursor level that raises before fetching
- [x] Read-only connection mode — no way to open a `Database` that is guaranteed never to write; LLM query agents should be able to operate in a mode where any INSERT / UPDATE / DELETE / DDL raises immediately rather than relying on the authorizer hook

### Usability for agents

- [x] Structured error types — every error from the engine is a plain `RuntimeError` with a human-readable string; LLM agents need to distinguish parse errors (`ParseError`), constraint violations, type errors, and missing-table errors to self-correct without re-parsing an English message
- [x] Async API — no `async def execute()` or asyncio support anywhere; agents built on asyncio/trio frameworks block their entire event loop on every database call; needs an `AsyncDatabase` / `AsyncCursor` wrapper or native coroutine execution path
- [x] `executescript` discards SELECT results — when a script contains a SELECT statement the rows are silently dropped; an agent running a multi-statement script that includes a SELECT gets no data back
- [x] Schema semantic metadata — no mechanism to attach descriptions or semantic tags to tables and columns; the LLM text-to-SQL layer can only infer meaning from names alone; a `_hyperion_schema_meta` system table with `(object_type, object_name, key, value)` rows would let the LLM layer read column descriptions, embedding model names, tenant boundary markers, and other context needed for accurate SQL generation

## Architectural Bottlenecks & Design Issues

- [x] Exclusive locking during Pager initialization — always attempts to acquire `LOCK_EX` for WAL recovery, blocking any concurrent connections from opening the file even for reading. Fails on read-only filesystems because file is opened in `"r+b"` mode. Needs a fix to skip `LOCK_EX` and use read-only file mode if database is opened as `readonly`.
- [x] Thread-pool hop overhead in Async API — offloads every individual `fetchone()` call to the thread pool executor. For large result sets, this causes massive context-switching and scheduling overhead. Fix by buffering rows in batches (e.g., 100 rows) or returning the full list when fetching, rather than hopping threads on every single row.
- [x] Page checksum validation loophole — computed CRC-32 checksums of exactly `0` are stored as `0`, which is treated as a legacy page and bypasses verification entirely. Corruption on pages with checksum `0` goes undetected. Fix by mapping computed `0` checksums to a non-zero value (e.g. `1`).
- [x] Monolithic connection lock blockage — connection uses a single reentrant lock (`threading.RLock`) for all cursor executes and fetches. A long-running query holds the lock for its entire execution, blocking concurrent threads using the same connection from performing lightweight metadata queries or introspection.

## Phase 1 Audit — Bugs & Gaps (Deep Analysis)

### Phase 2 Blockers — Must Fix Before Agent Layer

- [x] `_LAST_INSERT_ROWID` is a process-global variable — `expr.py:18` stores `_LAST_INSERT_ROWID` as a module-level global; concurrent inserts from two threads will race and `cursor.lastrowid` returns the wrong ID for the losing thread; any agent workflow that inserts a row and uses the returned ID to build a FK relationship produces corrupt data with no error; fix by moving last-insert-rowid state onto the `Database` instance so each connection has its own value
- [x] `VACUUM` silently drops triggers, ANALYZE stats, and schema metadata — `database.py:829–853` rebuilds the DB by copying tables/indexes/views but never copies `_catalog.triggers`, `_catalog.stats`, or `_catalog.meta`; after VACUUM all BEFORE/AFTER/INSTEAD OF triggers are gone, all ANALYZE statistics are gone (optimizer reverts to full-scan estimates), and all `set_meta` schema annotations are gone — the exact annotations the LLM layer relies on for query generation
- [x] `_USER_FUNCS` and `_USER_AGGS` are process-global dicts — `expr.py:14–15`; `db.create_function()` writes into a shared module-level registry; every `Database` instance in the same process shares the same custom function namespace; in a multi-tenant agent scenario a function registered for tenant A is callable from tenant B's queries; fix by moving the registries onto the `Database` instance
- [x] B-tree page allocators bypass the free-page list — `database.py:670–681`; `_make_alloc` and `_make_idx_alloc` (used on every B-tree split during INSERT) directly increment `next_free_page` without checking `free_pages`; pages freed by `drop_table` are never reused by subsequent inserts; an agent workload that repeatedly creates tables, bulk-loads data, and drops them grows the file indefinitely; fix by routing all allocation through `_alloc_page`

### Significant Weaknesses — Will Surface Under Agent Workloads

- [x] `_check_unique` does a full table scan on every INSERT — `constraints.py:35` scans every row to check UNIQUE constraints even when an index exists on the column; at 100k rows a bulk-load into a table with UNIQUE columns is O(n²); fix by using the index probe path (`optimizer.probe_index`) when a matching index exists
- [x] Optimizer row-count cache never invalidated by DML or DDL — `optimizer.py:31–44`; the `_opt_row_counts` dict is only populated on first access or ANALYZE; INSERTs, DELETEs, and DROP+recreate with the same table name never update it; after a significant data load the optimizer still uses the stale row count, producing wrong join order decisions; fix by invalidating the entry for a table after any write to it
- [x] Composite index keys use FNV-1a hashing — range queries silently degrade — `encoding.py:39–51`; multi-column indexes encode all column values into a single int64 via FNV-1a; hash values do not preserve sort order across column combinations, so range predicates and ORDER BY on composite indexes silently fall back to a full table scan with no warning; an agent generating `WHERE tenant_id = ? AND created_at > ?` on a `(tenant_id, created_at)` index gets no index benefit for the range component; fix requires prefix-encoded composite keys that preserve per-column sort order

## Known Limitations

- [x] Single-process only — WAL-based file locking protects against corruption but there is no network protocol or server mode; multiple processes cannot share a database over a socket; an agent workload that needs to expose the database to remote services or run the engine in a dedicated process must either embed it in-process or add a thin TCP/Unix-socket server layer
- [x] No `ALTER TABLE … ALTER COLUMN type` — column type changes are not supported; a column's declared type is fixed at creation time; workaround is `CREATE TABLE new AS SELECT CAST(col AS new_type) …` then `DROP TABLE old` and rename, but this loses indexes, triggers, and constraints on the affected table
- [x] `PRAGMA` / `RETURNING` / `EXPLAIN` results not fetchable — `execute()` returned a formatted string; `cursor.fetchall()` on PRAGMA/RETURNING/EXPLAIN returned `[]`; fixed by introducing `RowResult` so all data-producing ops are fetchable via the cursor

## Phase 1.5 — CLI & Developer Experience

- [x] Run `.sql` files from the CLI
- [x] REST API / HTTP server mode — `python -m hyperion http mydb.hdb --port 8080`; full feature parity with the embedded API

### Bugs Fixed in Phase 1.5

- [x] Fix `INSERT INTO t SELECT ..., literal, ... FROM ...` — literal constants in SELECT list treated as column names instead of values; fixed by routing through `eval_expr` in `_project_row`
- [x] Fix JOIN ON column side resolution — `ON right_alias.col = left_alias.col` resolved incorrectly when right table's column appeared on the left side of `=`; fixed by checking alias prefix to determine which side each column belongs to
- [x] Fix `SUM/MIN/MAX/AVG` of cross-table expressions in GROUP BY — `SUM(p.price * o.quantity)` returned NULL because aggregation used dict key lookup instead of `eval_expr` for expression arguments
- [x] Fix `UPDATE SET col = 'string-with-hyphen'` — string literal `'555-0001'` evaluated as arithmetic `555-1=554`; parser now preserves quotes on string literals in SET assignments so `eval_expr` correctly identifies them as strings
- [x] Fix `ALTER TABLE ADD COLUMN ... DEFAULT value` — default value not applied to existing rows; parser never parsed the DEFAULT clause in ADD COLUMN; fixed by parsing DEFAULT token and passing value through `_rewrite_table`
- [x] Fix chained CTEs — second CTE referencing first CTE in a WHERE IN subquery raised `No such table`; root cause: `_exec_subquery` in `where.py` called `db.select()` directly without any CTE context; fixed by threading `_active_ctes` onto the db object in `_rows_for_stmt` so subquery executors in `where.py` can resolve CTE names
- [x] Fix `WITH RECURSIVE` without explicit column aliases — `WITH RECURSIVE nums AS (SELECT 1 AS n UNION ALL SELECT n+1 FROM nums WHERE n<5)` raised `Unknown column: 'n'`; recursive step output kept key `"n + 1"` instead of `"n"` because `_apply_aliases` only fires when column aliases are declared in the CTE name (e.g. `cnt(n)`); fixed by normalising recursive step output columns to match base case column names
- [x] Fix trigger body split on `;` — `_split_statements` cut trigger bodies at every `;` inside `BEGIN...END`, leaving `END` as a bare unrecognised statement; fixed by tracking `BEGIN`/`END` depth in the splitter (while excluding `BEGIN TRANSACTION`)
- [x] Fix `COALESCE(SUM(...), 0)` returning NULL in GROUP BY — `_compute_aggregates` only recognised top-level aggregates; a function wrapped around an aggregate (e.g. `COALESCE(SUM(col), 0)`) was never computed; fixed with a second pass that substitutes inner aggregate results and re-evaluates the outer expression via `eval_expr`
- [x] Fix trigger `NEW.col` arithmetic — values in `new_row` at trigger fire time are Python strings (e.g. `"3"` not int `3`); `_sql_literal` wrapped them as quoted SQL strings (`'3'`), breaking expressions like `stock - NEW.quantity` with `int - str`; fixed by detecting numeric strings in `_sql_literal` and returning bare number tokens
- [x] Fix `ON CONFLICT DO UPDATE SET col = col + n` — parser only captured one token for upsert assignments, losing multi-token expressions like `qty + 3`; fixed by reading full token sequence until comma/semicolon
- [x] Fix `ON CONFLICT DO UPDATE` expression not evaluated — `_apply_on_conflict_update` tried `int(val)` which fails for expressions; fixed by falling back to `eval_expr(val, existing_row)`
- [x] Fix `_parse_agg` matching window functions as GROUP BY aggregates — `_AGG_RE` matched `SUM(x) OVER (...)` because lazy `.+?` stretched to last `)`; caused JOIN + window function queries to collapse all rows into one; fixed by rejecting columns containing `OVER (`
- [x] Fix window functions in JOIN queries silently dropped — `has_window` check only existed in SELECT path; JOIN path had no equivalent so LAG/LEAD/SUM OVER etc. were ignored; fixed by adding `has_window_j` detection and `_apply_window_functions` call in the JOIN code path
- [x] Fix `SUM(...) OVER (ORDER BY ...)` returning full-partition total — SQL default frame when ORDER BY present is `ROWS UNBOUNDED PRECEDING TO CURRENT ROW`; code treated missing frame as full-partition; fixed by applying the default cumulative frame when `ob_spec` is non-empty
- [x] Fix nested `CASE WHEN` returning wrong branch — branch token collector stopped at inner `WHEN`/`ELSE` keywords regardless of nesting depth; `THEN CASE WHEN ...` only collected `['CASE']`; fixed by tracking CASE/END depth in `_collect_case_branch_tokens`
- [x] Fix `=` operator dropped in `_tokenize_expr` — `_TOK_RE` matched `<=`, `>=`, `!=` but not bare `=`; CASE WHEN conditions like `dept = 'Eng'` tokenized without the operator, making every condition True; fixed by adding bare `=` as a token alternative in the regex
- [x] Fix `UNION ... ORDER BY` ignored — parser passed `t[right_start:]` (including `ORDER BY`) to the right SELECT parser which consumed it; outer UNION got `order_by: None`; fixed by stripping top-level ORDER BY/LIMIT/OFFSET from the right side and attaching them to the SET_OP node
- [x] Fix multi-source UNION column name mismatch — `SELECT name ... UNION SELECT customer ...` produced rows with mixed keys (`name` and `customer`); SQL standard requires all rows to use the leftmost SELECT's column names; fixed by remapping right-side rows to left-side keys in `_apply_set_op`
- [x] Fix function call on WHERE LHS — `WHERE UPPER(name) LIKE '%x%'` raised `Unknown operator: '('`; parser read `UPPER` as the column name and `(` as the operator; fixed by detecting `identifier(` pattern in `_parse_one_condition` and collecting the full function call expression before finding the comparison operator
- [x] Fix `STRFTIME` / `DATE` / `DATETIME` / `TIME` returning NULL — date functions not implemented in `_eval_func`, falling through to `return None`; added full implementation using Python's `datetime` module with modifier support (`+30 days`, `-1 month`, etc.)
- [x] Fix table-level `UNIQUE(col1, col2)` in CREATE TABLE parsed as a column — `_TOKEN_RE` matched `UNIQUE(student_id, course_id)` as a single token; the parser's UNIQUE constraint check compared exact string `"UNIQUE"` and missed the combined token; fixed by adding a `re.fullmatch` path that extracts column names from the single-token form
- [x] Fix inline `CHECK(expr)` in CREATE TABLE parsed as a garbage column — `_TOKEN_RE` matched `CHECK(amount > 0)` as a single token; the column constraint while-loop only matched bare `"CHECK"` keyword, so the combined token fell through; parser tried to use `CHECK(amount > 0)` as a column name, then `")"` as a type, raising `Unknown column type: ')'`; fixed by detecting `CHECK\s*\(` token in the loop and extracting the expression via `re.fullmatch`
- [x] Add `UPDATE OR IGNORE` support — `UPDATE OR IGNORE t SET col = val` raised `Expected: UPDATE <table> SET col=val`; parser expected `t[2] == "SET"` but `OR` and `IGNORE` occupied positions 1 and 2; fixed by detecting and skipping `OR <action>` tokens in `_parse_update` and passing `conflict_action` through to the executor; executor wraps the update in a try/except and silently skips rows that violate a constraint when `conflict_action == "IGNORE"`
- [x] Fix 3-table (and N-table) JOIN returning 0 rows — `_exec_extra_join` normalised `on_left`/`on_right` by literal position in the `ON a = b` expression, not by which alias belongs to the new right table; for `ON i.order_id = o.id` with right_alias `i`, `rcol` was resolved from `o.id` (wrong side), INLJ index lookup found the PK on `o.id` by accident, then `lr.get("i.order_id")` returned None on every left row, skipping all results; fixed by swapping `on_left`/`on_right` when `on_left`'s alias prefix matches `right_alias`
- [x] Fix string literal SELECT columns dropped in 2-table JOIN — `_project` inside `QueryMixin.join` used `{c: merged[c] for c in columns if c in merged}`, silently omitting columns not present as dict keys (e.g. `'Q3 label'` string literals, arithmetic, expressions); fixed by replacing the dict comprehension with a call to `_project_row` which falls back to `eval_expr` for missing columns
- [x] Fix string literal SELECT columns showing NULL in GROUP BY queries — `_compute_aggregates` resolved non-aggregate columns with `bucket_rows[0].get(col)` which returns None for literal strings not stored as row keys; fixed by adding an `eval_expr` fallback when the column is absent from the row
- [x] Fix `GROUP_CONCAT(col ORDER BY sort_col)` returning NULL — `ORDER BY sort_col` was left inside the arg string, making `col_name = "i.product ORDER BY i.price ASC"`, a key that doesn't exist in any row; fixed by stripping the `ORDER BY` clause from the arg before parsing column name and separator, then sorting `bucket_rows` by the extracted sort column before concatenating
- [x] Fix `LIMIT 0` returning 1 row — fast-path iterator checked `count >= limit` AFTER yielding, so LIMIT 0 emitted one row before stopping; fixed by moving the limit check before yield
- [x] Fix `OFFSET n` without `LIMIT` being ignored — ORDER BY column-parsing loop stopped only at `LIMIT` not `OFFSET`, causing `ORDER BY id OFFSET 3` to parse `OFFSET` and `3` as additional sort columns; `OFFSET` was also absent from `_ALIAS_BLOCKLIST` so `FROM t OFFSET 3` treated `OFFSET` as a table alias; both fixed by adding `"OFFSET"` to the ORDER BY stop tokens and to `_ALIAS_BLOCKLIST`
- [x] Fix `ORDER BY non_select_col LIMIT n` returning wrong rows — full-table-scan path projected rows to selected columns before sorting, so ORDER BY columns absent from SELECT list were silently NULL during sort; fixed by collecting full rows, sorting and limiting, then projecting at the end
- [x] Fix `SELECT COUNT(*) FROM (subquery) t` returning per-row NULLs — the `subquery_from` branch in `_rows_for_stmt_inner` called `_exec_derived_table` without detecting aggregates; the projection loop in `_exec_derived_table` called `_project_row` for each row with `COUNT(*)` which isn't a row key, returning NULL per row; fixed by adding an aggregate-detection branch that routes to `_apply_groupby_agg` (same as the CTE/view paths)
- [x] Fix string literal SELECT columns returning NULL when aggregate source has 0 rows — `_compute_aggregates` used `bucket_rows[0].get(col)` falling back to `None` when bucket is empty, even for constant literals like `'label'` that don't need row context; fixed by trying `eval_expr(col, {})` as fallback even for empty buckets
- [x] Fix function calls in `INSERT VALUES` not evaluated — single-token function calls like `UPPER('xyz')` and `ABS(-4.99)` have no space so the VALUES handler fell through to `else: parsed[name] = val`, storing the raw string; `ABS(-4.99)` then crashed `serialize_row` with `ValueError: could not convert string to float`; fixed by adding `"(" in val` to the `eval_expr` routing condition in `_execute_inner`
- [x] Fix `TRUNCATE TABLE` not resetting `AUTOINCREMENT` counter — `TRUNCATE` called `db.delete()` then returned without touching `meta.next_key`; subsequent inserts continued from the previous high-water mark instead of restarting from 1; fixed by resetting `db._meta(table).next_key = 1` after the delete in `executor.py`
- [x] Fix multi-level `ON DELETE CASCADE` not propagating past first child — `_check_fk_parent` deleted child B-tree rows directly without recursing into grandchildren first; deleting a customer cascade-deleted its orders but left order_items intact; fixed by calling `_check_fk_parent` recursively for each matched child row before deleting it from the B-tree in `constraints.py`
- [x] Fix `CURRENT_TIMESTAMP` / `CURRENT_DATE` / `CURRENT_TIME` column defaults stored as literal strings — default values assigned via `col.default` bypassed eval entirely; `DEFAULT CURRENT_TIMESTAMP` stored the string `"CURRENT_TIMESTAMP"` in the row; fixed by adding `_eval_default()` helper that routes SQL constant defaults through `eval_expr({})` at insert time; applied to both INSERT VALUES and INSERT SELECT default-fill paths in `executor.py`
- [x] Fix `LIMIT` in `UPDATE ... ORDER BY col LIMIT n` ignored — `_parse_update` checked for `LIMIT` immediately after WHERE but `ORDER BY col` tokens sat between them; `limit_u` stayed `None` and all matching rows were updated; fixed by skipping the optional `ORDER BY ...` clause before parsing `LIMIT` in `_parse_update`
- [x] Fix `LIMIT` in `DELETE ... ORDER BY col LIMIT n` ignored — same root cause as UPDATE: `_parse_delete` checked for `LIMIT` immediately after WHERE; `ORDER BY col` tokens prevented the LIMIT token from being reached; fixed by skipping the optional `ORDER BY ...` clause before parsing `LIMIT` in `_parse_delete`
- [x] Fix `TIME` column type not recognised — `_parse_col_type` handled `DATE`, `DATETIME`, `TIMESTAMP` but not `TIME`; raised `Unknown column type: 'TIME'`; fixed by adding `TIME` → `TEXT, 8` mapping in `parser.py`
- [x] Fix `SUM(CAST(col AS type))` / any aggregate with nested function-call arg returning NULL — when the arg to an aggregate contains `(`, the tokenizer produces `SUM ( CAST(col AS type) )` with spaces; `_AGG_RE` had no `\s*` between function name and `\(`; `_parse_agg` returned None; the column was never recognised as an aggregate; fixed by adding `\s*` before `\(` in `_AGG_RE`
- [x] Fix `GROUP BY expression` (e.g. `STRFTIME('%Y-%m', col)`) putting all rows in one bucket — `_group_by_select` used `row.get(c)` to build the bucket key; expression GROUP BY keys are never dict keys so all rows got key `(None,)` and collapsed into one bucket; fixed by falling back to `eval_expr(c, row)` when the key is not a direct column name
- [x] Fix `LENGTH(blob_col)` returning length of Python repr string instead of byte count — `_eval_func("LENGTH", ...)` called `str(args[0])` unconditionally, converting `b'hello world'` to the 14-char string `"b'hello world'"`; fixed by returning `len(args[0])` directly when the value is `bytes` or `bytearray`
- [x] Fix `COLLATE NOCASE` in WHERE equality (`WHERE name = 'bob' COLLATE NOCASE`) returning 0 rows — `_parse_one_condition` consumed `col op val` but left `COLLATE NOCASE` tokens dangling; `WhereClause` had no `collate` field so case-insensitive comparison was never applied; fixed by adding `collate` field to `WhereClause`, consuming `COLLATE <name>` in `_parse_one_condition`, and applying `casefold()` comparison in `_eval_atom` when `self.collate == "NOCASE"`
- [x] Fix INSERT into generated column silently succeeding — executor never validated that user-supplied `col_names` doesn't include generated columns; fixed by checking each user-supplied column name against `col.is_generated` in the INSERT handler and raising `ConstraintError` if found
- [x] Fix composite PK table JOIN returning 0 rows — `probe_index` in `optimizer.py` built lo/hi search keys using `_encode_composite_key([val], [type])` which produces a 128-bit (single-column) integer, but the composite PK index has `key_sz=24` and stores 192-bit keys (`col1 + col2 + rowid`); the 128-bit lo/hi were smaller than all actual keys so `scan_range` returned no rows; fixed by detecting `len(idx_meta.columns) > 1` and padding lo/hi with `_MIN_VAL_KEY`/`_MAX_VAL_KEY` for trailing columns
- [x] Fix `INSTEAD OF INSERT` trigger storing raw SQL literals — `_exec_instead_of_insert` built `parsed` dict with raw token values like `"'Grace'"` (with quotes) instead of unquoted strings; regular INSERT handler applied `_is_single_string_literal` unquoting but INSTEAD OF path didn't; fixed by applying the same token-parsing logic (NULL check, string unquote, eval_expr for expressions) in `_exec_instead_of_insert`
- [x] Fix `json_array(json_object(...))` double-encoding nested JSON — `json_array` passed args directly to `json.dumps`, which encoded already-serialized JSON strings as quoted strings; added `_maybe_json` helper that pre-parses string args starting with `{` or `[`; applied to both `json_array` and `json_object` value args to match SQLite JSONB subtype nesting behaviour
- [x] Fix `json_each` returning Python `True`/`False` for JSON booleans — `json_each_rows` stored raw Python bools in `value`/`atom`; SQLite returns `1`/`0`; fixed by converting bools to integers in `_cell` helper inside `json_each_rows`
- [x] Fix `json_each(fn(...))` in comma-join FROM not recognised — parser called `_collect_func_call` for the primary table but not for subsequent comma-joined tables; `json_each` was parsed as a bare table name; fixed by adding the same `_TABLE_VALUED_FUNCS` check in the comma-join loop
- [x] Fix `PRAGMA foreign_keys` (read, no value) returning a plain string instead of a fetchable RowResult — executor returned `f"foreign_keys = {val}"` string; fixed by returning `RowResult([{"foreign_keys": val}], ["foreign_keys"])` to match SQLite behaviour
- [x] Fix `PRAGMA table_info(missing_table)` raising `NoSuchTableError` — SQLite returns 0 rows for unknown tables; fixed by returning an empty `RowResult` with the correct column list instead of raising
- [x] Fix `col NOT LIKE pattern` raising `ParseError` — `_parse_one_condition` only handled `NOT IN` and `NOT BETWEEN`; added `NOT LIKE` and `NOT GLOB` (with ESCAPE support) by wrapping a LIKE/GLOB clause in a NOT group node
- [x] Fix LATERAL subquery column expressions not resolving outer row references — `SELECT_NOFROM` evaluated columns with `eval_expr(col, {})` (empty dict); LATERAL subquery passed outer row to `_instantiate_correlated` for WHERE but not for SELECT columns; fixed by storing `_outer_row` on the instantiated subquery AST and using it in `SELECT_NOFROM` evaluation
- [x] Fix `(a, b) = (v1, v2)` row comparison with NULLs returning wrong results — `_coerce` copied `None` from the cell to the target value, so `(NULL, 10) == (1, 10)` became `(None, 10) == (None, 10)` → True; fixed by coercing the raw value independently of cell type; added explicit NULL-in-either-operand → False guard for `=` and `!=` operators

### Audit Findings — Principal Engineer Review (2026-06-02)

#### Priority 1 — Data Correctness (must fix before Phase 2)

- [x] Fix `INT64_MIN` (-9223372036854775808) as primary key crashes with `OverflowError` — `btree.py:_pack_key` calls `key.to_bytes(self._key_sz, "big")` for non-8-byte keys; `_make_index_key(-2^63, -2^63)` returns a negative Python int because the `_KEY_SIGN` bias cancels out and the rowid component is negative; `int.to_bytes` without `signed=True` raises `OverflowError`; fix by masking the rowid to unsigned in `_make_index_key`: `(rowid & 0xFFFF_FFFF_FFFF_FFFF)` — verified to crash in production with extremal integer values
- [x] Fix dirty reads across explicit multi-statement transactions on shared connections — `Pager.read_page()` returns pages from `_working` (the uncommitted write buffer) whenever `self._in_txn is True`; `_in_txn` is a per-Pager attribute shared by all threads; the `_RWLock` is released between individual SQL statements in an explicit transaction so a concurrent SELECT acquires the read lock while `_in_txn is True` and reads uncommitted data; fix by gating `_working` access on whether the calling thread is the write-lock owner (store `_write_tid = threading.get_ident()` in `Pager.begin()` and check it in `read_page()`); verified: 7 consecutive dirty reads of an uncommitted value observed in controlled test before rollback
- [x] Fix WAL crash-recovery `replay_if_exists` missing `fsync` before WAL deletion — after applying committed pages, `wal.py:replay_if_exists` calls `db_file.flush()` but **not** `os.fsync(db_file.fileno())`; the WAL is then deleted in the `finally` block; if the OS crashes between `flush()` and the kernel writing pages to disk, the WAL is gone but the data is not durable — committed data is permanently lost; fix by adding `os.fsync(db_file.fileno())` (with `except OSError: pass`) immediately after `db_file.flush()` and before `wal_path.unlink()`
- [x] Add test: `INSERT INTO t VALUES (-9223372036854775808, 'x')` with INTEGER PRIMARY KEY must not raise — covers the INT64_MIN key overflow found above
- [x] Add test: dirty-read isolation — Thread A calls `db.begin()`, UPDATE, then sleeps; Thread B does SELECT on same connection; result must not contain Thread A's uncommitted value; rollback must restore original
- [x] Add test: WAL crash recovery fsync ordering — monkeypatch `os.fsync` to raise mid-replay; verify WAL is not deleted before main-file pages are durably written

#### Priority 2 — Durability & Reliability

- [x] Remove dead `CHECKPOINT_PAGES = 64` constant and `WAL.needs_checkpoint()` method, or implement lazy checkpointing — `needs_checkpoint()` is defined in `wal.py:81` but is **never called anywhere in the codebase**; `pager.commit()` unconditionally calls `self._wal.checkpoint()` on every commit making the threshold mechanism a no-op; either delete both (the WAL always-checkpoint behaviour is intentional) or implement lazy checkpointing: only call `checkpoint()` when `needs_checkpoint()` returns True and force a full checkpoint at `close()`, which would significantly improve bulk-insert throughput
- [x] Add test: concurrent explicit transactions — two threads each doing `BEGIN` / `INSERT` / `COMMIT` serially must not produce `TransactionError` inside a single thread; current `test_explicit_transaction_serialised` accepts `"already active"` silently, masking cross-thread state leakage
- [x] Add test: `CHECKPOINT_PAGES` threshold — verify `needs_checkpoint()` returns False below the threshold and True at/above it; verify checkpoint is triggered at the right boundary (currently untestable because the threshold is never checked)
- [x] Add test: multi-connection WAL replay — process A writes 100 rows and closes; a left-behind WAL is manually constructed; process B opens the same file and must replay the WAL and see all 100 rows; currently no test covers the two-process open scenario

#### Priority 3 — Performance

- [x] Optimize catalog ops flush — `ops_to_bytes()` serializes metadata for **all** tables on every commit regardless of which tables were touched; with 100 tables a single INSERT takes 3.8× longer than with 1 table; fix by tracking a dirty flag per `TableMeta`/`IndexMeta` and only serializing changed entries; or limit `ops_to_bytes()` to the single table touched by the current transaction
- [x] Fix plan cache eviction from FIFO to LRU — when the 512-entry cache is full, `cursor.py` evicts `oldest = next(iter(cache))` (insertion-order, not access-order); a workload with 513+ distinct query templates (common in AI/LLM applications) thrashes the cache and effectively disables it; fix by using `collections.OrderedDict` and calling `move_to_end(sql)` on cache hit before returning the cached plan
- [x] Add test: plan cache with 513 distinct query templates — verify that the 512nd+1 template evicts the least-recently-used entry, not the first-inserted one; verify the most-recently-used template is never evicted while less-used ones exist

#### Priority 4 — Code Quality & Latent Bugs

- [x] Replace `_TOKEN_RE` regex tokenizer with a proper depth-tracking lexer — `r'\w+\([^()]*\)'` only captures function calls with flat (non-nested) arguments; any SQL with nested functions (`ROUND(SUM(x), 2)`, `json_each(json_extract(col, '$.key'))`, `CAST(col AS INT)`) is not captured as a single token, causing downstream parsing failures; this root cause has already produced at least 6 separate bugs fixed in Phase 1.5 (SUM+CAST, json_each+json_extract, COALESCE+SUM, etc.) and more will surface; a 30-line depth-tracking lexer eliminates this entire class permanently
- [x] Decompose `_execute_inner` (330 lines, largest function in the codebase) into per-statement handlers — `executor.py:_execute_inner` handles every DML statement (INSERT, UPDATE, DELETE, TRUNCATE, CREATE, DROP, etc.) in a single monolithic function; each new SQL feature adds more branches; split into `_exec_insert`, `_exec_update`, `_exec_delete`, `_exec_truncate`, `_exec_ddl` etc. with a dispatch table; each handler becomes independently testable
- [x] Fix B-tree delete Phase-2 rebalance restart — after bulk delete the engine loops `while changed: pn = self._leftmost_leaf()` restarting the full leaf scan from leftmost after each merge; for k underfull leaves this is O(k × leaf_chain_length); replace with a single bottom-up merge pass that tracks candidate underfull leaves during the Phase-1 compact step instead of rediscovering them by re-scanning
- [x] Add test: tokenizer with deeply nested function calls — `SELECT ROUND(SUM(CAST(col AS REAL)), 2)` and `WHERE json_each(json_extract(json_extract(col, '$.a'), '$.b'))` must parse and execute without error; covers the recurring `_TOKEN_RE` nesting limitation
- [x] Add test: overflow page chain crash simulation — insert 10 large rows (each requiring 3 overflow pages), simulate a crash after page 5 of the last row's overflow chain by truncating the WAL mid-frame; reopen and verify: (a) the partially-written row is not visible (uncommitted), (b) the 9 complete rows are intact, (c) no dangling overflow page references exist after recovery
- [x] Add test: fuzz tokenizer with random SQL — generate 1000 random SQL strings mixing keywords, identifiers, numbers, and nested parens; verify that `parse()` either returns a valid AST or raises `ParseError`, never raises an unhandled internal exception such as `IndexError`, `KeyError`, or `AttributeError`
- [x] Add test: savepoint under concurrent reads — Thread A creates a savepoint, modifies data, and sleeps; Thread B reads concurrently; verify Thread B never sees Thread A's post-savepoint uncommitted modifications; complements the dirty-read test above but specifically targets the savepoint snapshot mechanism

### Audit Findings — Principal Engineer Review Round 2 (2026-06-02)

#### Priority 1 — Data Correctness (silent wrong-answer bugs)

- [x] Fix multi-transaction WAL staleness across multiple inter-connection commits — `pager.py` sets `_needs_wal_catchup = False` after the **first** WAL catch-up per connection lifetime; if Connection B commits a second transaction (lazy checkpoint, frames still in WAL) after Connection A's first catch-up, Connection A never applies those frames and silently reads stale data for the rest of its life; fix by removing the one-shot guard and calling `_apply_wal_to_cache()` at the top of **every** `begin()` for file-backed Pagers, conditioned on whether the WAL tail has advanced past the last-applied offset; cost is O(WAL_size) bounded to ~256 KB under the current checkpoint policy — negligible compared to the correctness risk
- [x] Fix plan cache race condition under concurrent reads — `_plan_cache` is a shared `OrderedDict` on the `Database` instance; the LRU `move_to_end()` call **mutates** the `OrderedDict` while inside a `_lock.read()` context; multiple concurrent readers call `move_to_end()` simultaneously, corrupting the doubly-linked list inside `OrderedDict`; Python's GIL prevents this in CPython today but not under sub-interpreters or PyPy, and the semantics are wrong regardless; fix by protecting all plan cache reads and writes with a dedicated `threading.Lock()` separate from `_RWLock`, or by moving cache mutation into the write-lock path
- [x] Fix FK cascade child-row scan missing index — `constraints.py:_check_fk_parent` always does an O(child_rows) full B-tree scan to find child rows that match the deleted/updated parent key (`for rowid, raw in self._table_btree(tmeta).scan(): ...`); `_fk_index_lookup` is only called for the parent-side existence check, not for locating child rows; bulk `DELETE` of 1 000 parent rows with a child table of 1 000 rows is O(1 000 × 1 000) = O(n²); fix by calling `probe_index(db, tname, fk.columns[0], ref_val)` when an index on the child FK column exists, and falling back to the full scan only when no index is present
- [x] Fix ANALYZE stats serialized in schema blob instead of ops blob — `catalog.py:schema_to_bytes()` includes `stats`; ANALYZE is not a DDL operation but every `ANALYZE` run causes `_schema_flushed_bytes` to change, forcing a full schema JSON rewrite on every subsequent commit indefinitely; move `stats` from `schema_to_bytes()` to `ops_to_bytes()` so ANALYZE does not trigger schema page rewrites; update `from_schema_and_ops_bytes()` to read stats from the ops blob

#### Priority 2 — Architectural Correctness (will produce cascading bugs in Phase 1.5+)

- [x] Decouple expression representation from string form — every SQL expression is stored as a raw string or `list[str]` token list and re-tokenized + re-parsed on every row touched; `eval_expr(string, row)` is called inside scan loops at 100k-row scale, calling `re.findall()` per invocation; trigger bodies are stored as token lists (lossy — original SQL unrecoverable); column binding happens at runtime not at parse time so `WHERE ghost_col = 1` silently returns zero rows instead of erroring at `execute()` time; introduce a minimal `Expr` AST hierarchy (`ColumnRef`, `Literal`, `BinaryOp`, `FuncCall`, `CaseExpr`, `CastExpr`) parsed once during `parse()` and evaluated via `expr.evaluate(row: dict) -> Any`; this eliminates the entire class of re-tokenization bugs that produced at least 12 of the 50+ Phase 1.5 fixes and unblocks parse-time type-checking for Phase 2

#### Priority 3 — Performance (will degrade non-linearly as schema / data grows)

- [x] Fix savepoint snapshots serializing the entire catalog — `database.py:savepoint()` calls `self._catalog.to_bytes()` which JSON-serializes all table schemas, triggers, views, stats, and meta into a Python string stored in `_savepoints`; for a schema with 50 tables and 20 triggers this is several hundred KB per savepoint; nested savepoints multiply this linearly; fix by implementing differential savepoints: snapshot only `TableMeta` operational fields (`root_page`, `next_page`, `next_key`) and the dirty working-page set — both already captured in `pages_snap` and `dirty_snap` — rather than re-serializing the entire Catalog object
- [x] Fix free-page list JSON serialization growing unbounded under churn — `catalog.py:ops_to_bytes()` serializes `free_pages: list[int]` via `json.dumps`; under heavy create/drop workloads (table churn in agent workloads) the free list can reach 10 000+ entries (~50 KB+ of JSON per commit); this is re-encoded on every commit where `next_free_page` or `free_pages` changed; fix by encoding the free list as a compact binary structure (e.g., a sorted array of uint32s packed with `struct`) rather than JSON, reducing serialization cost from O(n × text_digits) to O(n × 4 bytes)

#### Priority 4 — Test Coverage (bugs that the suite cannot currently detect)

- [x] Add test: multi-transaction WAL staleness across multiple commits — open Connection A (read-only intent, does not acquire LOCK_EX); Connection B commits Transaction 1 with lazy checkpoint (WAL has frames); Connection A begins Transaction 1 and syncs; Connection B commits Transaction 2 (WAL has more frames); Connection A begins Transaction 2; verify Connection A reads Connection B's Transaction 2 data correctly; this is the exact scenario the one-shot `_needs_wal_catchup` guard fails on
- [x] Add test: plan cache concurrent mutation — spawn 20 threads each executing SELECTs with rotating sets of 30 distinct query templates against the same `Database`; run for 2 seconds; verify no `KeyError`, `RuntimeError: dictionary changed size during iteration`, or wrong query results; this detects the `OrderedDict.move_to_end()` race described above
- [x] Add test: NULL propagation through nested function calls — verify `eval_expr` correctly returns `NULL` (Python `None`) for: `COALESCE(NULLIF(NULL, NULL), ABS(NULL))`, `ROUND(NULL, 2)`, `UPPER(NULL) || 'x'`, `CASE WHEN NULL THEN 1 ELSE 2 END`; verify none raises an unhandled exception; NULL semantics in nested expressions are the most common source of silent wrong-answer bugs
- [x] Add test: B-tree structural invariants after bulk insert + delete — after inserting 1 000 rows and deleting 500 non-contiguous rows: (a) verify every leaf is reachable from `_leftmost_leaf()` via the sibling chain and the chain terminates at 0; (b) verify every leaf's keys are strictly ascending; (c) verify every internal node's separator key equals the first key of its right child; (d) verify every non-root node's parent pointer is non-zero and points to a page that lists it as a child; this detects structural corruption that only surfaces much later as a wrong-answer B-tree lookup
- [x] Add test: FK cascade delete performance and correctness at scale — create a parent table with 1 000 rows and a child table with 1 000 rows (1:1 FK, `ON DELETE CASCADE`); delete all 1 000 parent rows in a single statement; verify (a) all 1 000 child rows are also deleted, (b) no FK violation errors are raised, (c) the operation completes in under 5 seconds; this detects the O(n²) cascade scan described in the analysis
- [x] Add test: schema persistence round-trip with 50 tables — create 50 tables each with 3 columns, 1 index, and 1 trigger; commit; close; reopen; verify (a) all 50 table schemas survive exactly, (b) all 50 indexes are present and functional (INSERT + probe), (c) all 50 triggers fire correctly, (d) ANALYZE stats survive if populated; this is the minimum schema size where catalog serialization bugs surface
- [x] Add test: savepoint memory correctness under large schema — create 100 tables; begin a transaction; take a savepoint; insert rows into 10 tables; rollback to savepoint; commit; verify all 100 table schemas are intact, the 10 tables have no rows, and the other 90 tables are unaffected; this detects catalog snapshot/restore bugs masked by small-schema tests
- [x] Add test: adversarial `eval_expr` boundary inputs — verify the expression evaluator handles without raising: integer division by zero (`10 / 0`), float division by zero (`10.0 / 0`), `%` modulo by zero, type mismatch in arithmetic (`'abc' + 1`), deeply nested `CASE WHEN` (5 levels), empty string as column name, expression referencing a column not in the row dict; all must return `NULL` or raise `DataError`, never an unhandled `TypeError`, `ZeroDivisionError`, or `AttributeError`

### Bugs Found During .sql Demo Run (2026-06-02)

#### Critical — Data Correctness

- [x] Fix `ROLLBACK TO SAVEPOINT` + `COMMIT` corrupts pager undo snapshot — after a transaction that uses `ROLLBACK TO SAVEPOINT` followed by `COMMIT`, any subsequent `BEGIN … ROLLBACK` on the same connection reverts ALL rows including data that existed before the second transaction began; root cause is that rolling back to a savepoint restores the pager's `_pages_snap` to the savepoint's snapshot, and the final `COMMIT` records that restored snapshot as the new "before" baseline, so the next transaction's undo reference points to the savepoint's pre-state rather than the committed state; verified with a minimal repro: three initial INSERTs → `BEGIN; UPDATE; SAVEPOINT sp; UPDATE; ROLLBACK TO sp; COMMIT` → later `BEGIN; INSERT; ROLLBACK` → table is empty; fix by ensuring `COMMIT` always advances the pager's undo baseline to the current committed page state, not the savepoint-restored snapshot

#### Correctness — Query Results

- [x] Fix `ORDER BY` on `json_each` object keys not sorting alphabetically — `SELECT … FROM json_each('{"name":"Alice","age":30,"active":true}') AS j ORDER BY j.key` returns rows in JSON insertion order (`name, age, active`) instead of alphabetical string order (`active, age, name`); integer array keys (0, 1, 2) sort correctly; only string object keys are affected; root cause is likely that `json_each` stores the row-source index as an integer internally and `ORDER BY j.key` sorts on that integer rather than the string key value; fix by ensuring `json_each` exposes string keys as TEXT so the sort comparison uses lexicographic ordering

#### Cosmetic / SQLite Compatibility

- [x] Fix float display precision — arithmetic on REAL values often produces IEEE 754 noise in output: `5 * 4.99 = 24.950000000000003`, `9.99 * 0.8 = 7.992000000000001`, `14.99 * 0.1 = 1.499...`; SQLite rounds these to the minimum significant representation before display; fix by formatting REAL output with Python's `repr`-style shortest-round-trip float formatting (e.g. `f"{v:.15g}"` or `str(round(v, 10))`) so `24.95` displays as `24.95`, not `24.950000000000003`

- [x] Fix table-qualified column names in result headers — `SELECT o.quantity` without an alias renders the column header as `o.quantity` instead of `quantity`; `SELECT sub.total_comp` renders as `sub.total_comp`; SQLite strips the table/alias prefix and shows only the bare column name; fix by stripping the `alias.` prefix from the column header when building `cursor.description` and the display table, unless a column alias (`AS name`) is explicitly given

- [ ] Fix NULL sort order — `ORDER BY col ASC` puts NULLs last; `ORDER BY col DESC` also puts NULLs last; SQLite treats NULL as less than every other value (NULLs first in ASC, last in DESC); the SQL standard leaves this implementation-defined but SQLite compatibility requires NULLs-first-in-ASC; `NULLS FIRST` / `NULLS LAST` override already works correctly — the default needs to match SQLite: NULLs first for ASC, NULLs last for DESC

- [ ] Fix CLI script execution continues past errors — when running a `.sql` file, any unhandled exception (FK violation, constraint error, etc.) aborts the entire script and skips remaining statements including teardown `DROP TABLE` blocks; SQLite's CLI prints the error and continues executing subsequent statements; fix by catching per-statement errors in the script runner, printing them, and continuing to the next statement rather than propagating the exception

- [ ] Fix column-level UNIQUE constraint not exposed in `_hyperion_master` — `CREATE TABLE t (email TEXT UNIQUE)` creates an implicit unique index but that index does not appear in `_hyperion_master` queries (`WHERE type = 'index'`) and is not used by EXPLAIN QUERY PLAN for equality lookups; only indexes created with explicit `CREATE [UNIQUE] INDEX` statements are tracked; fix by registering the auto-created unique index in the catalog's `indexes` dict (with a generated name like `_uq_t_email`) so it is visible in introspection and the optimizer can probe it

- [ ] Fix `PRAGMA table_info` default value quoting — string defaults are returned without quotes (`pending`) instead of as SQL literals (`'pending'`) as SQLite does; numeric defaults are already bare numbers which is correct; fix by wrapping text default values in single quotes in the `PRAGMA table_info` result rows so tools that parse the output (e.g. ORMs, schema diffing libraries) can distinguish a string default `'now'` from a function call `now()`

### Transactions

- [ ] `SELECT FOR UPDATE` — row-level locking within a transaction; `SELECT * FROM t WHERE id = 1 FOR UPDATE` acquires an exclusive lock on matched rows, blocking concurrent writers until `COMMIT` or `ROLLBACK`; required for safe read-modify-write patterns
- [ ] Transaction isolation levels — `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ|SERIALIZABLE`; current engine uses a readers-writer lock but exposes no user-visible isolation level; `SHOW TRANSACTIONS` lists active transactions with their isolation level and start time

### Network

- [ ] MySQL wire protocol server — `python -m hyperion mysql mydb.hdb --port 4406`; implements the MySQL client/server protocol so any MySQL-compatible client connects without a Hyperion-specific driver:
  - Server greeting / capability handshake — send server version string and capability flags; accept unauthenticated or password-bypass connections (`--skip-ssl`, empty password)
  - `COM_QUERY` — receive SQL string, execute against the database, return column-definition packets + row data packets + EOF/OK packet
  - `COM_PING` — respond with OK packet (keepalive)
  - `COM_QUIT` — close connection cleanly
  - Result set encoding — column count packet, one `ColumnDefinition41` packet per column (name, type, flags), one text-protocol row packet per result row, `EOF` to terminate
  - Error packet — `ERR_Packet` with SQL state and typed message on any exception
  - `COM_INIT_DB` — handle `USE database` command sent by MySQL clients on connection or schema switch
  - Compatible clients: `mysql` CLI (`mysql -h 127.0.0.1 -P 4406 -u root --skip-ssl`), `mysql-connector-python`, `PyMySQL`, SQLAlchemy MySQL dialect
- [ ] Web Dashboard — browser UI served at `GET /` by the HTTP server; shows database stats, table list, schema browser, and an interactive SQL query editor; no external JS dependencies (single self-contained HTML page)
- [ ] DSN connection strings — `hyperion://host:port/dbname` format parsed by a `connect(dsn=...)` helper; standard format for ORMs and connection pool libraries
- [ ] Server-side connection pooling — configurable pool size and max queue depth on the TCP server; reuses cursors across requests rather than spawning a new thread per connection; `SHOW PROCESSLIST` lists active connections with current query, user, and elapsed time

### Analytics — Column Store

- [ ] `CREATE COLUMN TABLE` — alternative storage layout where each column is stored as a contiguous array rather than row-by-row; enables vectorised `SUM/AVG/COUNT/MIN/MAX` scans that skip irrelevant columns entirely
- [ ] Columnar aggregate scans — when the query touches only a subset of columns and the table is a column table, scan only those column arrays; `GROUP BY` fallback to row-store path when needed
- [ ] `SHOW STORAGE FORMAT` — introspection command returning `ROW` or `COLUMN` for each table

### Replication

- [ ] Logical replication — `CREATE PUBLICATION pub FOR TABLE t1, t2` on the primary; `CREATE SUBSCRIPTION sub CONNECTION '...' PUBLICATION pub` on the replica; changes are streamed as an append-only change log and applied on the subscriber
- [ ] Physical replication — binary-level WAL streaming from primary to replica with auto-sync every 500ms; auto-reconnect on connection loss; replica runs in read-only mode (any write raises `ReadOnlyError`); `SHOW MASTER STATUS`, `SHOW SLAVE STATUS`, `SHOW BINLOG`, `START SLAVE`, `STOP SLAVE`; replica can be promoted to primary on failure

### Row-Level Security

- [ ] `ENABLE ROW LEVEL SECURITY` / `DISABLE ROW LEVEL SECURITY` per table — when enabled, all queries against the table are filtered by active policies; superuser-level connections bypass RLS
- [ ] `CREATE POLICY name ON table USING (expr)` — defines a filter expression applied transparently to every `SELECT`, `UPDATE`, and `DELETE` on the table; multiple policies are OR-combined
- [ ] `CURRENT_USER_ID()` scalar function — returns the active user identity set via `db.set_user(id)`; used inside policy expressions for per-tenant row filtering

### Event Scheduler

- [ ] `CREATE EVENT name ON SCHEDULE EVERY n SECOND|MINUTE|HOUR|DAY DO sql` — registers a background job that fires on the given interval; event definitions persist in the catalog
- [ ] `CREATE EVENT name ON SCHEDULE AT timestamp DO sql` — one-shot event fires once at the given datetime then auto-drops
- [ ] `SHOW EVENTS` / `DROP EVENT` — list and remove scheduled events
- [ ] `ALTER EVENT name ENABLE|DISABLE` — pause or resume a scheduled event without dropping it
- [ ] Background event loop — a daemon thread in `Database` checks due events and executes them; honors the readers-writer lock so events never corrupt concurrent queries

### Functions — Missing

- [ ] Regex functions — `REGEXP_REPLACE(str, pattern, replacement)`, `REGEXP_EXTRACT(str, pattern)`, `REGEXP` / `RLIKE` infix operators for pattern matching in `WHERE` clauses; backed by Python `re` module; no external dependency
- [ ] Date manipulation functions — `NOW()` (alias for `CURRENT_TIMESTAMP`), `DATEDIFF(date1, date2)` returns days between two dates, `DATE_ADD(date, INTERVAL n UNIT)` / `DATE_SUB(date, INTERVAL n UNIT)` for date arithmetic, `DATE_FORMAT(date, format)` for strftime-style formatting; MySQL-compatible signatures
- [ ] `TIME` standalone data type — `HH:MM:SS` storage separate from `DATE` and `DATETIME`; already have `CURRENT_TIME` scalar but no `TIME` column type

### Introspection — Missing

- [ ] `EXPLAIN ANALYZE` — runs the query and annotates the execution plan with actual row counts, loop iterations, and elapsed time per node; complements `EXPLAIN` (estimated plan) and `EXPLAIN QUERY PLAN` (textual plan) with real execution statistics
- [ ] `SHOW RECOVERY STATUS` — reports the current WAL state: last committed LSN, whether a recovery replay occurred on startup, WAL file size, and checkpoint timestamp; useful for diagnosing crash recovery
- [ ] `SHOW MATERIALIZED VIEWS` — lists all materialized views with their name, defining query, last refresh timestamp, and row count; complements `SHOW TABLES` and `INFORMATION_SCHEMA`
- [ ] `SHOW LOGICAL LOG` — display the last N entries from the logical replication change log; shows table name, operation (INSERT/UPDATE/DELETE), and column values; useful for debugging replication lag

### Adaptive Query Optimizer

- [ ] Automatic query rewriting — simplify trivially true/false conditions before planning (`WHERE 1=1` → strip, `WHERE 1=0` → empty scan); normalize redundant `AND`/`OR` combinations; rewrite `WHERE col IN (SELECT ...)` to an equivalent JOIN when the subquery is non-correlated and the planner estimates the join path is cheaper
- [ ] `EXPLAIN REWRITTEN` — show the query as it looks after the rewriter has transformed it, before the planner runs; lets developers see exactly what optimizations were applied and verify the rewriter is not changing query semantics
- [ ] Access statistics tracking — record per-table and per-index scan counts, hit rates, and last-used timestamps in the catalog; updated on every query execution
- [ ] `SHOW INDEX SUGGESTIONS` — analyse access statistics and current schema to recommend missing indexes; output lists candidate columns, estimated selectivity, and projected query speedup
- [ ] `SHOW QUERY STATS` — per-table query frequency and column filter counts
- [ ] `SHOW PROFILES` / `PROFILE ON|OFF` — per-query execution timing; `SHOW PROFILE FOR QUERY n` shows breakdown for a specific query
- [ ] Hash JOIN and Merge JOIN strategies — planner selects `NESTED_LOOP` for small tables, `HASH_JOIN` for large unsorted inputs, `MERGE_JOIN` when both sides are index-ordered; current engine only does nested loop
- [ ] Query result cache — LRU cache of recent `SELECT` results with a configurable TTL; cache key is the normalised SQL + params; invalidated on any write to a referenced table; `SHOW CACHE STATUS`, `SET CACHE ON|OFF`
- [ ] Parallel query execution — split table scans and aggregations across multiple CPU threads on the same machine; `SET MAX_PARALLEL_WORKERS n` controls thread count; `SET PARALLEL_THRESHOLD n` sets minimum row count before parallelism kicks in; `/*+ PARALLEL(N) */` query hint forces a specific degree; `SHOW PARALLEL STATUS`; planner chooses parallel path automatically for large scans

### DDL — Advanced Schema

- [ ] Schemas / namespaces — `CREATE SCHEMA s`, `DROP SCHEMA s`, `USE s`; tables qualified as `schema.table`; default schema is `public`; required for multi-tenant isolation at the schema level
- [ ] Materialized views — `CREATE MATERIALIZED VIEW name AS SELECT ...`; result is physically stored and queryable like a table; `REFRESH MATERIALIZED VIEW name` re-executes the query and replaces stored rows; `DROP MATERIALIZED VIEW`
- [ ] Stored procedures — `CREATE PROCEDURE name(params) BEGIN ... END`; `CALL name(args)`; `DROP PROCEDURE`; body supports local variables, `IF/ELSE`, `LOOP/LEAVE`, and `CURSOR` declarations for row-by-row processing; `SHOW PROCEDURES`
- [ ] Named prepared statements — SQL-level `PREPARE stmt FROM 'SELECT ... WHERE id = ?'`; `EXECUTE stmt USING val`; `DEALLOCATE PREPARE stmt`; complements the existing Python-level `?` binding with a session-scoped statement handle
- [ ] Table partitioning — `CREATE TABLE t (...) PARTITION BY RANGE|LIST|HASH (col)`; rows routed to the correct partition on insert; queries with matching predicates scan only relevant partitions; `SHOW PARTITIONS`; `DROP PARTITION`
- [ ] Table inheritance — `CREATE TABLE child INHERITS (parent)`; child inherits all parent columns; `SELECT * FROM parent` includes child rows; `SELECT * FROM ONLY parent` excludes them; `SHOW INHERITANCE`

### Security — User Management

- [ ] `CREATE USER 'name' IDENTIFIED BY 'password'` / `DROP USER` / `SHOW USERS` — per-user identity stored in the catalog; passwords hashed (SHA-256)
- [ ] `GRANT SELECT|INSERT|UPDATE|DELETE|ALL ON table TO user` / `REVOKE` / `SHOW GRANTS FOR user` — table-level privilege enforcement; any operation by a user without the required privilege raises `AuthorizationError`

### Concurrency

- [ ] `LOCK TABLE t READ|WRITE` / `UNLOCK TABLES` / `SHOW LOCKS` — explicit advisory table locks; `READ` allows concurrent reads, blocks writes; `WRITE` blocks all other access; complements the existing RWLock with user-visible locking
- [ ] Buffer pool manager — configurable LRU page cache with dirty-page tracking and write-behind flushing; `SET BUFFER_POOL_SIZE n` (in MB); `SHOW BUFFER POOL STATUS` returns hit rate, dirty page count, eviction count; `FLUSH BUFFER POOL` forces all dirty pages to disk; reduces I/O under read-heavy workloads by keeping hot pages in memory

### Operational

- [ ] `BACKUP DATABASE TO 'file.sql'` / `BACKUP TABLE t TO 'file.sql'` — SQL-dump backup with schema + data; header includes timestamp and version metadata; complements `iterdump()` with a CLI-accessible SQL command
- [ ] `RESTORE DATABASE FROM 'file.sql'` — replay a backup file against the current database; drops existing tables that conflict before recreating
- [ ] `LOAD DATA INFILE 'path' INTO TABLE t SEPARATOR ',' SKIP HEADER` — bulk CSV import with auto-separator detection (comma, semicolon, tab) and quoted-field handling
- [ ] `SELECT * FROM t INTO OUTFILE 'path' SEPARATOR ','` — export query results to CSV
- [ ] `LISTEN channel` / `NOTIFY channel, 'payload'` / `UNLISTEN channel` — lightweight pub/sub messaging between connections; notifications delivered to all listeners on the named channel; `SHOW LISTEN` lists active subscriptions; useful for cache invalidation and real-time agent coordination
- [ ] `BENCHMARK 'SELECT ...' [n]` — run a statement n times and report total and per-iteration timing; useful for regression testing query performance
- [ ] `SHOW VARIABLES` — list all configurable runtime settings and their current values
- [ ] `INFORMATION_SCHEMA.TABLES` and `INFORMATION_SCHEMA.COLUMNS` virtual tables — standard SQL information schema views; `INFORMATION_SCHEMA.TABLES` returns `(TABLE_NAME, TABLE_ROWS, TABLE_TYPE)`; `INFORMATION_SCHEMA.COLUMNS` returns `(TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_KEY)`
- [ ] `DESCRIBE table` / `SHOW CREATE TABLE table` — MySQL-compatible aliases for schema introspection; `DESCRIBE` returns column name, type, nullable, key, default; `SHOW CREATE TABLE` returns the full `CREATE TABLE` statement that would recreate the table
- [ ] `SHOW TABLES` / `SHOW DATABASES` / `SHOW SCHEMAS` — MySQL-compatible aliases for listing objects; currently requires `SELECT name FROM _hyperion_master` or the Python API

### GPU / MPS Acceleration

- [ ] Hardware detection at startup — probe for CUDA (NVIDIA), MPS (Apple Silicon), and ROCm (AMD) availability using optional libraries (`cupy`, `torch`, `mlx`); if none present, silently fall back to CPU; expose detected backend via `SHOW VARIABLES` (`gpu_backend = cuda|mps|rocm|none`)
- [ ] GPU-accelerated column store scans — when a query targets a `COLUMN TABLE`, transfer column arrays to GPU memory and execute `SUM`, `AVG`, `COUNT`, `MIN`, `MAX` as massively parallel reductions; fall back to CPU path for row-store tables or when GPU unavailable
- [ ] GPU-accelerated aggregations — `GROUP BY` aggregations on large row-store tables offloaded to GPU when table exceeds `gpu_threshold` rows (configurable via `SET GPU_THRESHOLD n`); GPU builds hash-grouped partial aggregates, CPU merges
- [ ] GPU-accelerated hash joins — when both sides of a join exceed `gpu_threshold`, build the hash table on GPU memory and probe in parallel; orders of magnitude faster than CPU nested-loop for large equijoins
- [ ] GPU-accelerated sorting — `ORDER BY` on large result sets offloaded to GPU parallel sort (bitonic sort / radix sort); CPU sort retained for small results where GPU transfer overhead exceeds compute savings
- [ ] MPS backend (Apple Silicon) — uses `mlx` or `torch` MPS device; zero-copy transfers via unified memory on M-series chips where CPU and GPU share the same physical RAM; automatic selection when running on macOS with Apple Silicon
- [ ] `SHOW GPU STATUS` — report backend name, device name, total VRAM, used VRAM, current `gpu_threshold`, and whether GPU is actively being used
- [ ] Phase 2 bridge — GPU backend reused for vector similarity operators (`<->`, `<=>`, `<#>`) and HNSW index construction/search in Phase 2; scoped here so the acceleration layer is in place before vector workloads arrive

### Spatial

- [ ] `POINT(lat, lng)` column type — stores a 2D geographic coordinate as two REAL values
- [ ] `ST_DISTANCE(p1, p2)` — Haversine distance in km between two POINT values
- [ ] `ST_WITHIN(point, center, radius_km)` — returns true if point is within radius of center; enables geo-radius queries without full table scans
- [ ] `ST_X(point)` / `ST_Y(point)` — extract latitude / longitude from a POINT value
- [ ] `ST_ASTEXT(point)` — return WKT string representation `POINT(lat lng)`
- [ ] `CREATE SPATIAL INDEX idx ON t(col)` — index on a POINT column to accelerate `ST_WITHIN` queries

## Phase 1.75 — C Extension Hot Path

> **Goal** — keep 95% of the codebase in Python; rewrite only the innermost loop functions that appear at the top of a profiler trace as a thin C extension (`_hyperion_core.so`). Users still `pip install hyperion` — the extension compiles on install via `setup.py`. No new runtime dependencies.

### Profiling & Baseline

- [ ] Establish benchmark suite — scripts that measure row encode/decode throughput, B-tree lookup latency, bulk insert speed, and full table scan speed at 100k / 1M / 10M rows; results recorded as baseline before any C work begins
- [ ] Profile-guided targeting — run the benchmark suite under `cProfile` / `py-spy` to confirm which functions dominate; only rewrite functions that account for >10% of total query time

### C Extension — Core Functions

- [ ] `encode_row(row_dict, schema) → bytes` — pack a Python dict into the fixed-width binary row format; replaces the pure-Python implementation in `encoding.py`; called on every INSERT and UPDATE
- [ ] `decode_row(buf, schema) → dict` — unpack raw page bytes back into a Python dict; replaces the pure-Python deserialisation path; called on every row read during scans and lookups
- [ ] `btree_compare_keys(a: bytes, b: bytes) → int` — low-level byte-level key comparison used in every B-tree traversal; eliminates per-comparison Python overhead in the innermost search loop
- [ ] `btree_search_page(page_bytes, key: bytes) → int` — binary search within a single 4KB B-tree page; returns the cell offset of the matching or nearest key; replaces the Python loop in `btree.py`
- [ ] `page_checksum(page_bytes) → int` — CRC-32 computation for page integrity; currently calls Python `struct` and `zlib`; a C version eliminates interpreter overhead on every page read and write

### Build & Integration

- [ ] `setup.py` / `pyproject.toml` C extension target — defines the `_hyperion_core` extension module with optional build; if a C compiler is unavailable, falls back to pure-Python implementations transparently with a warning
- [ ] Pure-Python fallback shim — each C function has a Python equivalent behind a `try: from _hyperion_core import X` / `except ImportError: X = _python_X` guard; the rest of the codebase calls the name, never the module directly
- [ ] CI build matrix — compile and test the extension on Linux (gcc), macOS (clang / Apple Silicon), and Windows (MSVC) via GitHub Actions; pure-Python fallback tested in the same matrix

### Validation

- [ ] Correctness test suite — run the full existing test suite against the C extension build; any divergence from pure-Python results is a bug in the C code, not an acceptable trade-off
- [ ] Benchmark regression gate — re-run the baseline benchmark suite after each C function is introduced; document speedup per function; flag any function where the C version is slower than Python (likely a marshalling overhead issue)

## Phase 2

> **Dependency order** — strict prerequisite chain:
> `VECTOR(n) type` → `Bulk vector insert` → `ANN index (HNSW)` → `Vector similarity operators` → `Hybrid search planner`

### Storage

- [ ] `VECTOR(n)` column type — a first-class column type that stores an n-dimensional float32 vector as `n × 4` bytes; declared dimensionality must be enforced on write; dimensionality must be visible in `PRAGMA table_info`; requires the variable-length row storage fix as a prerequisite for `n > ~900`
- [ ] Bulk vector insert — inserting embeddings individually through the standard INSERT path (B-tree insert + constraint check + trigger + index update per row) is impractical at 100k+ vectors; needs a batch-optimised write path that amortises the per-row overhead across a bulk operation

### Search

- [ ] Vector similarity operators — `<->` (L2 / Euclidean distance), `<=>` (cosine similarity), `<#>` (dot product) as SQL infix operators usable in `ORDER BY` and `WHERE` for exact brute-force similarity scan; e.g. `SELECT id FROM docs ORDER BY embedding <=> query_vec LIMIT 10`
- [ ] ANN index (HNSW or IVF) — an approximate nearest neighbour index structure for sub-linear vector search; the current B-tree is unsuitable for high-dimensional vectors; HNSW (Hierarchical Navigable Small World) is the standard choice; the index structure needs its own storage format outside the row B-tree
- [ ] Hybrid search planner — a single query that applies SQL predicate filters AND ranks by vector similarity without materialising the full table; the planner must understand how to combine an ANN probe result set with a WHERE clause pushdown so that filters run before or alongside the ANN scan, not after

### Full-Text Search

- [ ] Inverted index (FTS) — an in-engine inverted index mapping terms to `(rowid, frequency)` posting lists; required for keyword search over text columns; the B-tree index only supports equality and range on raw values, not tokenised term lookup; storage must be efficient for large vocabularies across millions of documents
- [ ] BM25 / TF-IDF scoring — once an inverted index exists, a `bm25(col, query)` scoring function and `MATCH` operator so queries like `SELECT * FROM docs WHERE body MATCH 'neural network' ORDER BY bm25(body, 'neural network') DESC` work; BM25 is the standard baseline for keyword retrieval in production RAG systems
- [ ] Hybrid retrieval query — combine FTS BM25 score and vector similarity score in a single query with configurable weighting (`alpha * bm25_score + (1-alpha) * cosine_score`); this is the core retrieval primitive for production RAG and requires the planner to understand both index types simultaneously
