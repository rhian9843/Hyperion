# Hyperion — Work Backlog

## Bugs (silent wrong behaviour)

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
- [ ] WAL checkpointing — the WAL file is deleted immediately after every commit so there is no multi-transaction WAL efficiency; a checkpoint strategy (write-back on threshold, not per-commit) would reduce fsync pressure under write-heavy workloads
- [ ] Catalog scalability — the entire catalog (all table schemas, index metadata, ANALYZE stats, trigger definitions) is serialised as a single JSON blob and rewritten on every commit; this degrades linearly with the number of objects and is unsuitable once the schema grows large
- [ ] Thread safety — `Database._cache`, `_dirty`, `_txn_depth`, `_catalog`, and `_savepoints` are unsynchronised mutable state; two threads sharing one `Database` object will corrupt each other silently; Python async frameworks (FastAPI, LangChain, asyncio thread pool executors) routinely call synchronous I/O from worker threads — every agent that does this is a data corruption risk; requires a `threading.RLock` per `Database` instance at minimum
- [ ] Page checksums — no CRC or hash is stored on individual pages; a single bad write from an OS bug, disk firmware issue, or partial flush goes undetected; `PRAGMA integrity_check` catches structural B-tree violations but not bit-level corruption within a structurally-valid page; for a database storing embeddings and LLM outputs, silent corruption produces wrong answers with no signal

## LLM / Agent Layer Prerequisites

### Bugs that break agent workflows today

- [x] `cursor.description` is `None` on empty result sets — when a SELECT returns zero rows, `description` is set to `None` instead of the column metadata; an agent checking the schema of a table via a zero-row query gets nothing back (verified: `db.execute("SELECT * FROM t WHERE 1=0").description` returns `None`)
- [x] `cursor.lastrowid` is never populated — always `None` after INSERT regardless of `AUTOINCREMENT`; an agent that inserts a row and needs the generated key has no way to retrieve it without a separate `SELECT` call; `last_insert_rowid()` is also not implemented as a SQL function
- [x] `INTEGER PRIMARY KEY` does not alias the B-tree rowid — `lastrowid` returned the internal sequence counter (1, 2, 3…) instead of the user-supplied PK value; `last_insert_rowid()` was equally wrong; also wasted storage serialising the PK both as a column and as a separate B-tree key; fixed by using the column value directly as the rowid and keeping `next_key` as the high-water mark for auto-assignment

### Safety

- [x] Query timeout / cancellation — no mechanism to abort a query after a deadline; LLM-generated SQL can produce accidental cartesian joins or deep recursive CTEs that run indefinitely; needs a `timeout_ms` parameter on `execute()` and a cooperative check inside the execution loop
- [ ] Max result rows guard — no built-in limit on rows returned; an agent issuing `SELECT * FROM large_table` will materialise the entire table in memory with no warning; needs a configurable `max_rows` on the `Database` or cursor level that raises before fetching
- [ ] Read-only connection mode — no way to open a `Database` that is guaranteed never to write; LLM query agents should be able to operate in a mode where any INSERT / UPDATE / DELETE / DDL raises immediately rather than relying on the authorizer hook

### Usability for agents

- [ ] Structured error types — every error from the engine is a plain `RuntimeError` with a human-readable string; LLM agents need to distinguish parse errors (`ParseError`), constraint violations, type errors, and missing-table errors to self-correct without re-parsing an English message
- [ ] Async API — no `async def execute()` or asyncio support anywhere; agents built on asyncio/trio frameworks block their entire event loop on every database call; needs an `AsyncDatabase` / `AsyncCursor` wrapper or native coroutine execution path
- [ ] `executescript` discards SELECT results — when a script contains a SELECT statement the rows are silently dropped; an agent running a multi-statement script that includes a SELECT gets no data back
- [ ] Schema semantic metadata — no mechanism to attach descriptions or semantic tags to tables and columns; the LLM text-to-SQL layer can only infer meaning from names alone; a `_hyperion_schema_meta` system table with `(object_type, object_name, key, value)` rows would let the LLM layer read column descriptions, embedding model names, tenant boundary markers, and other context needed for accurate SQL generation

## Vector Database Prerequisites

> **Dependency order** — these items have a strict prerequisite chain and cannot be parallelised:
> `Variable-length row storage` → `VECTOR(n) type` → `ANN index (HNSW)` → `Hybrid search planner`
> Similarly on the agent side: `Thread safety` → `MVCC` → `Async API` (async safety depends on both).

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
