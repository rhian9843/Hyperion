"""
Hyperion — an embedded relational database engine written in Python.

Storage
    B-tree pages managed by Pager (file) or MemoryPager (":memory:").
    WAL-based write-ahead logging provides crash-safe commits.

Schema / DDL
    CREATE / DROP TABLE, VIEW, INDEX, TRIGGER (BEFORE/AFTER/INSTEAD OF,
    INSERT/UPDATE/DELETE, FOR EACH ROW, RAISE, WHEN).
    Column types: INTEGER, REAL, TEXT, BLOB, BOOLEAN, DATE/DATETIME.
    Constraints: PRIMARY KEY (single & composite), AUTOINCREMENT, UNIQUE,
    NOT NULL, DEFAULT, CHECK, FOREIGN KEY (ON DELETE/UPDATE CASCADE/SET NULL).
    Computed/generated columns (STORED and VIRTUAL).
    ALTER TABLE (RENAME TABLE/COLUMN, ADD/DROP COLUMN).
    CREATE TABLE AS SELECT, CREATE TEMP TABLE.

DML
    INSERT (single & multi-row VALUES, INSERT INTO … SELECT, INSERT OR
    REPLACE/IGNORE, ON CONFLICT DO NOTHING/UPDATE SET, RETURNING).
    UPDATE (SET expressions, WHERE, LIMIT, RETURNING).
    DELETE (WHERE, LIMIT, RETURNING).  TRUNCATE TABLE.
    UPSERT.  ON DELETE/UPDATE CASCADE and SET NULL cascades.

Queries
    SELECT with WHERE, GROUP BY, HAVING, ORDER BY (column name or position,
    NULLS FIRST/LAST), LIMIT/OFFSET, DISTINCT.
    Joins: INNER, LEFT, RIGHT, FULL OUTER, CROSS, NATURAL; multiple chained
    JOINs; multi-table implicit FROM (comma syntax).
    Subqueries: scalar subquery in SELECT list, subquery in FROM (derived
    table), subquery in WHERE (IN / EXISTS / correlated).
    Set operations: UNION [ALL], INTERSECT, EXCEPT.
    CTEs: WITH … AS (…), WITH RECURSIVE (UNION ALL).
    Window functions: ROW_NUMBER, RANK, DENSE_RANK, NTILE, LAG, LEAD,
    FIRST_VALUE, LAST_VALUE, SUM/AVG/MIN/MAX/COUNT OVER (…).
    SELECT without FROM (e.g. SELECT 1+1, UPPER('hello')).

Expressions
    Arithmetic (+, -, *, /), string concatenation (||), comparison
    (=, <>, <, <=, >, >=), BETWEEN, LIKE [ESCAPE], GLOB, IN, NOT IN,
    EXISTS, IS [NOT] NULL, CASE WHEN … THEN … ELSE … END.
    CAST(x AS type), COALESCE, NULLIF, IFNULL.
    Boolean literals TRUE/FALSE; CURRENT_TIMESTAMP/CURRENT_DATE/CURRENT_TIME.
    Parenthesised groups; NOT prefix; aggregate DISTINCT modifier.

Aggregate / scalar functions
    COUNT, SUM, AVG, MIN, MAX, GROUP_CONCAT / STRING_AGG,
    COUNT(DISTINCT …) / SUM(DISTINCT …).
    String: UPPER, LOWER, LENGTH, SUBSTR, TRIM, LTRIM, RTRIM, REPLACE,
    INSTR, PRINTF / FORMAT.
    Math: ABS, ROUND, CEIL, FLOOR, MOD, RANDOM, RANDOMBLOB.
    Type: TYPEOF, NULLIF, COALESCE.
    JSON: json_extract, json_object, json_array, json_each, json_tree.
    Application-defined scalar and aggregate functions via
    db.create_function() / db.create_aggregate().

Transactions / savepoints
    BEGIN / COMMIT / ROLLBACK.
    SAVEPOINT / RELEASE SAVEPOINT / ROLLBACK TO SAVEPOINT.
    File locking (shared / exclusive) via WAL.

Indexes / optimizer
    CREATE [UNIQUE] INDEX … ON table(cols) (including expression indexes).
    ANALYZE collects per-table/index statistics.
    Cost-based query optimizer picks join order and index access paths.

Operational
    PRAGMA foreign_keys, table_info, index_list, index_info,
    integrity_check.
    VACUUM rebuilds the file compactly.
    EXPLAIN / EXPLAIN QUERY PLAN shows execution plan.
    Quoted identifiers ("name", `name`).
    In-memory databases: Database(":memory:").

Python DB-API 2.0 (PEP 249)
    db.cursor() / db.execute(sql[, params]) / db.executemany() /
    db.executescript().
    Cursor: fetchone(), fetchall(), fetchmany(n), rowcount, description.
    Parameter binding: positional ? and named :name / $name.
    Context manager: `with Database(…) as db:` (auto commit/rollback).
    db.row_factory — pluggable row format; built-ins: Row, dict_factory,
    tuple_factory.
    db.set_authorizer(fn) — per-operation access control hook.
    db.iterdump() — yield SQL statements that recreate the database.
    db.create_function() / db.create_aggregate() — register Python callables.

System catalog
    _hyperion_master table (equivalent of sqlite_master) exposes
    table/index/view/trigger definitions as queryable rows.
"""

from .constants import PAGE_SIZE, PAGE_CKSUM_SZ, PAGE_CKSUM_OFF, INTEGER, REAL, TEXT, DEFAULT_TEXT_SIZE
from .checksum import CorruptPageError, page_checksum, stamp_page, verify_page
from .schema import Column, ForeignKey, Schema, serialize_row, deserialize_row
from .btree import BTree
from .catalog import TableMeta, IndexMeta, TriggerMeta, Catalog
from .wal import WAL
from .pager import Pager, MemoryPager
from .encoding import (
    _encode_index_key, _encode_composite_key,
    _make_index_key, _split_index_key,
    _IDX_KEY_SZ, _KEY_SIGN,
    _apply_order_limit, _apply_set_op,
)
from .database import Database
from .cursor import Cursor, _bind_params, _sql_literal
from .row import Row, dict_factory, tuple_factory
from .auth import (
    SQLITE_OK, SQLITE_DENY, SQLITE_IGNORE,
    SQLITE_SELECT, SQLITE_INSERT, SQLITE_UPDATE, SQLITE_DELETE,
    SQLITE_CREATE_TABLE, SQLITE_DROP_TABLE,
    SQLITE_CREATE_INDEX, SQLITE_DROP_INDEX,
    SQLITE_READ, SQLITE_TRANSACTION,
)
from .expr import eval_expr, is_expr
from .where import (
    WhereClause,
    _OUTER_REF_RE,
    _try_resolve_outer_ref,
    _instantiate_correlated,
    _exec_correlated_subquery,
)
from .parser import (
    _TOKEN_RE, _AGG_RE, _ALIAS_BLOCKLIST,
    _tokenize, _parse_col_type,
    _parse_table_alias, _parse_agg,
    _extract_paren_tokens,
    _parse_one_condition, _parse_atom, _parse_and_group,
    _parse_where_expr, _parse_where, _parse_group_having, _parse_order_limit,
    parse, _parse_tokens,
)
from .errors import (
    HyperionError,
    ParseError,
    SchemaError, NoSuchTableError, NoSuchColumnError, NoSuchIndexError,
    TableExistsError, ColumnExistsError, IndexExistsError,
    ConstraintError, UniqueConstraintError, NotNullConstraintError,
    CheckConstraintError, ForeignKeyConstraintError,
    DataError, TransactionError, AuthorizationError, InternalError,
)
from .executor import execute, _execute_inner, _rows_for_stmt, _format_rows, QueryTimeoutError, ReadOnlyError, TooManyRowsError
from .optimizer import estimate_rows, find_eq_index, probe_index, optimize_join, get_ndv
from .triggers import (fire_triggers, has_triggers, has_instead_of,
                       scan_matching_rows, apply_update_row)
from .repl import handle_meta, repl, main
