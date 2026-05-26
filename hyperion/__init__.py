"""
Hyperion package — re-exports every public name so that
`import hyperion` works identically to the old monolithic hyperion.py.
"""

from .constants import PAGE_SIZE, INTEGER, REAL, TEXT, DEFAULT_TEXT_SIZE
from .schema import Column, ForeignKey, Schema, serialize_row, deserialize_row
from .btree import BTree
from .catalog import TableMeta, IndexMeta, Catalog
from .wal import WAL
from .pager import Pager
from .encoding import (
    _encode_index_key, _encode_composite_key,
    _make_index_key, _split_index_key,
    _IDX_KEY_SZ, _KEY_SIGN,
    _apply_order_limit, _apply_set_op,
)
from .database import Database
from .expr import eval_expr, is_expr
from .where import (
    WhereClause,
    _OUTER_REF_RE,
    _try_resolve_outer_ref,
    _instantiate_correlated,
    _exec_correlated_subquery,
)
from .parser import (
    ParseError,
    _TOKEN_RE, _AGG_RE, _ALIAS_BLOCKLIST,
    _tokenize, _parse_col_type,
    _parse_table_alias, _parse_agg,
    _extract_paren_tokens,
    _parse_one_condition, _parse_atom, _parse_and_group,
    _parse_where_expr, _parse_where, _parse_group_having, _parse_order_limit,
    parse, _parse_tokens,
)
from .executor import execute, _execute_inner, _rows_for_stmt, _format_rows
from .repl import handle_meta, repl, main
