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

- [ ] System catalog table — queryable `_hyperion_master` (equiv. of `sqlite_master`) exposing table/index/view definitions as rows; ORMs and tools depend on this
- [ ] `PRAGMA integrity_check` — verify B-tree structure and page consistency
- [ ] `EXPLAIN` / `EXPLAIN QUERY PLAN` — show the query execution plan; critical for debugging performance and verifying index usage

## Python DB-API / Convenience Layer

- [ ] PEP 249 cursor interface — `db.execute(sql)` / `db.executemany(sql, params)` / `db.executescript(sql)` returning cursor objects with `.fetchone()`, `.fetchall()`, `.fetchmany(n)`, `.rowcount`, `.description` (column name/type metadata)
- [ ] Parameter binding — positional `?` and named `:name` / `$name` placeholders so values are passed safely without string formatting (`db.execute("SELECT * FROM t WHERE id = ?", (1,))`)
- [ ] Context manager — `with Database(":memory:") as db:` auto-closes; `with db:` wraps an implicit transaction (commit on exit, rollback on exception)
- [ ] `db.row_factory` — pluggable row format; default tuple, built-ins for `dict` and named-access rows; user-assignable callable
- [ ] `db.set_authorizer(fn)` — callback invoked per SQL operation; return allow/deny/ignore to gate access (security hook, mirrors sqlite3)
- [ ] `db.iterdump()` — yield SQL statements that recreate the full database; useful for backup, migration, and test fixtures

## Code Quality

- [ ] Refactor `_parse_tokens` (1,420 lines) into per-statement parser functions — the monolithic function makes it hard to isolate bugs and the expressions-in-VALUES bug is a direct consequence of it
- [ ] Unify `_execute_inner` and `_rows_for_stmt` execution paths — JOIN+aggregation, CTE resolution, and GROUP BY fixes applied to one path must be manually mirrored to the other; the divergence is the root cause of the JOIN+GROUP BY and CTE+JOIN bugs
- [ ] Update module docstring — currently missing joins, aggregates, transactions, constraints, set operations, subqueries
