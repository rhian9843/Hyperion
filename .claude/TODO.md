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
- [ ] `NOT` prefix operator — `WHERE NOT col = 1` (only NOT IN / NOT EXISTS work today)
- [ ] `BETWEEN x AND y`
- [ ] Column aliases — `SELECT id AS uid, name AS full_name FROM t` (alias must also flow into ORDER BY / GROUP BY / HAVING)
- [ ] `CASE WHEN ... THEN ... ELSE ... END` expressions
- [ ] `COALESCE(x, y, ...)` / `NULLIF(x, y)` / `IFNULL(x, y)`
- [ ] `CAST(x AS type)`
- [ ] Arithmetic expressions in SELECT and WHERE — `SELECT price * qty`, `WHERE price * 1.1 > 100`
- [ ] String concatenation operator — `SELECT first || ' ' || last`
- [ ] Expression evaluation in SELECT list — currently only bare column names are supported; functions and arithmetic resolve to nothing
- [ ] Fix `NOT IN` NULL semantics — `x NOT IN (1, NULL)` should be `UNKNOWN` per SQL standard

## Missing SQL — Queries

- [x] `OFFSET` (with LIMIT) — `SELECT ... LIMIT 10 OFFSET 20`
- [ ] Multiple JOINs — `FROM a JOIN b ON ... JOIN c ON ...` (parser exits after the first JOIN today)
- [ ] Multi-table implicit FROM — `SELECT * FROM a, b WHERE a.id = b.id` (parser accepts only one table name)
- [ ] `INSERT INTO ... SELECT ...` — bulk insert from a query result
- [x] Multi-row `INSERT` — `INSERT INTO t VALUES (1,'a'), (2,'b')`
- [ ] Subquery in `FROM` — `SELECT * FROM (SELECT ...) AS alias` (derived tables)
- [ ] CTE — `WITH cte AS (SELECT ...) SELECT ... FROM cte`
- [ ] Window functions — `ROW_NUMBER() OVER (...)`, `RANK()`, `LAG()`, etc.
- [ ] `SELECT` without `FROM` — `SELECT 1`, `SELECT UPPER('hello')`
- [ ] Batch statements — multiple `;`-separated statements in one `execute()` call
- [ ] Scalar subquery in SELECT list — `SELECT name, (SELECT COUNT(*) FROM orders WHERE user_id = u.id) FROM users u` (currently tokenized into garbage column names)
- [ ] Multi-line SQL in REPL — REPL reads one line per `input()` call; statements spanning multiple lines are silently dropped
- [ ] `ORDER BY` column position — `ORDER BY 1, 2` (positional reference)
- [ ] `NULLS FIRST` / `NULLS LAST` in ORDER BY — `ORDER BY col NULLS FIRST`
- [ ] `TRUE` / `FALSE` literals in expressions — `WHERE active = TRUE`
- [ ] `CURRENT_TIMESTAMP` / `CURRENT_DATE` / `CURRENT_TIME` scalar values

## Missing SQL — DDL / DML

- [ ] `CREATE TABLE IF NOT EXISTS` / `DROP TABLE IF EXISTS` / `CREATE INDEX IF NOT EXISTS`
- [ ] `PRIMARY KEY` constraint syntax — `id INTEGER PRIMARY KEY` (implies NOT NULL + UNIQUE; auto-generates a unique index)
- [ ] `AUTOINCREMENT` / `AUTO_INCREMENT`
- [ ] Multi-column table-level `UNIQUE (col1, col2)` constraint
- [ ] `CREATE TABLE ... AS SELECT ...`
- [ ] `UPSERT` — `INSERT OR REPLACE` / `INSERT OR IGNORE` / `ON CONFLICT`
- [ ] `TRUNCATE TABLE t`
- [ ] `ON DELETE CASCADE` / `ON DELETE SET NULL` for foreign keys
- [ ] `RETURNING` clause — `INSERT INTO t VALUES (...) RETURNING id`
- [ ] Views — `CREATE VIEW v AS SELECT ...` / `DROP VIEW`
- [ ] `SAVEPOINT` / `RELEASE SAVEPOINT` / `ROLLBACK TO SAVEPOINT`

## Missing SQL — Types

- [ ] `BLOB` / `BYTES` column type — variable-length binary storage
- [ ] `BOOLEAN` column type — stored as 0/1 INTEGER with TRUE/FALSE literals
- [ ] `DATE` / `DATETIME` / `TIMESTAMP` — stored as TEXT with ISO-8601 affinity (SQLite-style)
- [ ] Integer size aliases — `TINYINT`, `SMALLINT`, `BIGINT` mapped to INTEGER (SQLite-style type affinity)

## Missing SQL — String / Scalar Functions

- [ ] String functions — `UPPER`, `LOWER`, `LENGTH`, `SUBSTR`, `TRIM`, `LTRIM`, `RTRIM`
- [ ] `REPLACE(str, from, to)` / `INSTR(str, sub)` / `PRINTF` / `FORMAT`
- [ ] Math functions — `ABS`, `ROUND`, `CEIL`, `FLOOR`, `MOD`
- [ ] `RANDOM()` / `RANDOMBLOB(n)`
- [ ] `TYPEOF(x)` — returns the storage class of a value
- [ ] `NULLIF(x, y)` / `COALESCE` (listed above but also callable in SELECT list once expression evaluation works)
- [ ] `GROUP_CONCAT(col)` / `STRING_AGG(col, sep)` aggregate — `_AGG_RE` today only recognises COUNT/MIN/MAX/SUM/AVG

## Missing Operational SQL

- [ ] `PRAGMA foreign_keys = ON/OFF` — enable/disable FK enforcement at runtime
- [ ] `PRAGMA table_info(t)` — returns column metadata (name, type, notnull, dflt_value, pk)
- [ ] `PRAGMA index_list(t)` / `PRAGMA index_info(idx)` — index introspection
- [ ] `VACUUM` — rebuild database file to reclaim space from deleted rows and pages
- [ ] Quoted identifiers — `"column name"` or `` `column` `` for reserved words or names with spaces

## Missing — Concurrency & Safety

- [ ] File locking — shared/exclusive lock protocol so multiple connections to the same file do not corrupt the database
- [ ] In-memory databases — `Database(":memory:")` backed by a dict instead of file I/O; critical for testing and temporary workloads

## Missing — Query Execution

- [ ] Query optimizer / cost-based planner — today multi-join queries do nested full scans; a cost model is needed so the engine picks the cheapest join order and access path
- [ ] `ANALYZE` — collect per-table/index statistics (row count, distinct values) that the query optimizer can use
- [ ] `COUNT(DISTINCT col)` / `SUM(DISTINCT col)` — `_AGG_RE` today does not handle the DISTINCT modifier inside aggregate calls
- [ ] Expression indexes — `CREATE INDEX idx ON t(UPPER(col))` — index on a computed expression rather than a raw column

## Missing — DDL / Schema

- [ ] Triggers — `CREATE TRIGGER BEFORE/AFTER INSERT/UPDATE/DELETE ON t` with `FOR EACH ROW` body; required by many ORMs and audit-log patterns
- [ ] `CREATE TEMP TABLE` / `CREATE TEMPORARY TABLE` — session-scoped table that is automatically dropped on close
- [ ] Recursive CTEs — `WITH RECURSIVE cte AS (base UNION ALL recursive_step) SELECT ...` — needed for trees, graphs, and hierarchical data
- [ ] Generated / computed columns — `col INTEGER AS (expr) STORED` / `VIRTUAL`
- [ ] `COLLATE` clause — `ORDER BY name COLLATE NOCASE`; Unicode-aware and case-insensitive comparison

## Missing — Functions & Types

- [ ] JSON functions — `json_extract(col, '$.key')`, `json_object(...)`, `json_array(...)`, `json_each(...)` — modern apps embed JSON everywhere and LLM outputs are JSON
- [ ] Application-defined functions — Python API to register custom scalar and aggregate functions (`db.create_function(name, n_args, fn)`)

## Missing — Introspection

- [ ] System catalog table — queryable `_hyperion_master` (equiv. of `sqlite_master`) exposing table/index/view definitions as rows; ORMs and tools depend on this
- [ ] `PRAGMA integrity_check` — verify B-tree structure and page consistency

## Code Quality

- [ ] Refactor `_parse_tokens` (349 lines) into per-statement parser functions
- [ ] Update module docstring — currently missing joins, aggregates, transactions, constraints, set operations, subqueries
