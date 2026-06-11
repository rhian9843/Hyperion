import struct
from typing import Any

from .errors import InternalError
from .constants import INTEGER, REAL, TEXT


def _encode_index_key(val: Any, col_type: str) -> int:
    """Encode a column value as a sort-preserving signed int64 B-tree key.

    INTEGER  — identity (already int64).
    REAL     — IEEE 754 bit-manipulation preserving float sort order.
    TEXT/VARCHAR — first 8 UTF-8 bytes, zero-padded, interpreted as a
                   big-endian uint64 then biased to signed int64.  This
                   preserves lexicographic byte order so range predicates
                   (>, >=, <, <=, BETWEEN) and ORDER BY work correctly.
                   Strings that share their first 8 bytes get the same key;
                   collisions are caught by post-scan row verification.
    """
    if col_type == INTEGER:
        return int(val)
    if col_type == REAL:
        raw = struct.unpack(">Q", struct.pack(">d", float(val)))[0]
        # Negative floats: XOR bits 0-62 to reverse ordering within negatives.
        # Positive floats: raw uint64 already sorts correctly as signed int64
        # because IEEE 754 exponent is in the high bits and max float < 2^63.
        encoded = raw ^ 0x7FFFFFFFFFFFFFFF if raw >> 63 else raw
        return struct.unpack(">q", struct.pack(">Q", encoded))[0]
    # TEXT / VARCHAR — prefix-encoded for sort-order preservation
    b = str(val).encode("utf-8")
    # Take first 8 bytes; zero-pad shorter strings (NUL sorts before any char)
    prefix = b[:8].ljust(8, b"\x00")
    unsigned = struct.unpack(">Q", prefix)[0]
    # Subtract 2^63 to map unsigned 0…2^64-1 → signed -2^63…2^63-1 while
    # preserving relative order; compatible with _make_index_key's +_KEY_SIGN bias.
    return unsigned - (1 << 63)


def _encode_composite_key(vals: list[Any], col_types: list[str]) -> "int | list[int]":
    """Encode column values as sort-preserving index keys.

    Single column  → returns a signed int64 (same as _encode_index_key).
    Multiple columns → returns a list[int] of per-column signed int64 keys.
                       Each column retains its full 64-bit precision, preserving
                       sort order exactly.  Pass the result to _make_index_key,
                       which is polymorphic and handles both forms.
    """
    if len(vals) == 1:
        return _encode_index_key(vals[0], col_types[0])
    return [_encode_index_key(v, t) for v, t in zip(vals, col_types)]


# Sentinels for open-ended range bounds.
_MAX_VAL_KEY = (1 << 63) - 1   # maximum signed int64 key
_MIN_VAL_KEY = -(1 << 63)      # minimum signed int64 key


def _composite_prefix_bounds(
    eq_vals: list[Any], eq_types: list[str],
    range_val: Any, range_type: str, op: str,
    n_total_cols: int,
) -> "tuple[list[int], list[int]]":
    """Return (lo_col_keys, hi_col_keys) for a prefix-equality + range scan.

    Both are lists of signed int64 keys — one per index column — ready to pass to
    _make_index_key.  The scan range is conservative (may include false positives);
    the caller must post-filter every returned row with the full WHERE predicate.

    eq_vals / eq_types  — leading equality columns in index column order.
    range_val / type    — the boundary value for the range column (op end).
    op                  — one of ">", ">=", "<", "<=".
    n_total_cols        — total columns in the index.
    """
    prefix_keys = [_encode_index_key(v, t) for v, t in zip(eq_vals, eq_types)]
    range_key   = _encode_index_key(range_val, range_type)
    remaining   = n_total_cols - len(eq_vals) - 1  # columns after the range column

    if op in (">=", ">"):
        # lo = prefix equality + range boundary (post-filter handles strict >)
        # hi = prefix equality + max possible value for range col + max for remainder
        lo_keys = prefix_keys + [range_key]   + [_MIN_VAL_KEY] * remaining
        hi_keys = prefix_keys + [_MAX_VAL_KEY] + [_MAX_VAL_KEY] * remaining
    else:  # "<", "<="
        lo_keys = prefix_keys + [_MIN_VAL_KEY] + [_MIN_VAL_KEY] * remaining
        hi_keys = prefix_keys + [range_key]    + [_MAX_VAL_KEY] * remaining

    return lo_keys, hi_keys


_IDX_KEY_SZ  = 16         # index B-tree key size for single-column indexes
_KEY_SIGN    = 1 << 63   # bias to convert signed int64 → unsigned for big-endian sort


def _idx_key_sz(n_cols: int) -> int:
    """Return the index B-tree key size for an index with n_cols columns.

    Each column occupies 8 bytes (full int64 precision) plus 8 bytes for rowid.
    Single-column indexes keep the historical _IDX_KEY_SZ = 16.
    """
    return (n_cols + 1) * 8


def _make_index_key(val_key: "int | list[int]", rowid: int) -> int:
    """Pack val_key(s) + rowid into an unsigned big-endian sort key integer.

    val_key may be:
    - a signed int64 (single-column index) → returns a 128-bit integer.
    - a list of signed int64s (multi-column index) → returns a (N+1)*64-bit integer.

    Each column key is biased by +2^63 to map the full signed range to unsigned
    while preserving sort order, so keys compare correctly as plain Python ints.
    """
    rowid_u = rowid & 0xFFFF_FFFF_FFFF_FFFF  # treat as unsigned so result stays non-negative
    if isinstance(val_key, list):
        result = 0
        for vk in val_key:
            result = (result << 64) | (vk + _KEY_SIGN)
        return (result << 64) | rowid_u
    return ((val_key + _KEY_SIGN) << 64) | rowid_u


def _split_index_key(composite: int) -> tuple[int, int]:
    rowid   = composite & 0xFFFFFFFFFFFFFFFF
    val_key = (composite >> 64) - _KEY_SIGN
    return val_key, rowid


def _apply_order_limit(rows: list[dict], order_by: list[dict] | None,
                       limit: int | None,
                       offset: int | None = None) -> list[dict]:
    """Sort rows by ORDER BY clauses (NULLs last), then apply OFFSET and LIMIT."""
    if order_by:
        from .expr import eval_expr, is_expr

        def _key_val(row: dict, col: str):
            if col in row:
                return row[col]
            # alias.col pattern: try the bare column name (json_each rows use
            # bare keys; the alias prefix is added later by _project_row)
            if "." in col:
                bare = col.split(".", 1)[1]
                if bare in row:
                    return row[bare]
            if is_expr(col):
                return eval_expr(col, row)
            return None

        def _collate_key(v, collation: str | None):
            if v is None:
                return v
            if collation == "NOCASE":
                return str(v).lower()
            if collation == "RTRIM":
                return str(v).rstrip()
            return v

        # Stable multi-key sort: apply keys in reverse order so the first
        # key ends up as the primary sort (Python sort is stable).
        for ob in reversed(order_by):
            col, desc = ob["col"], ob["desc"]
            collation  = ob.get("collate")
            nulls_first = ob.get("nulls_first")
            non_null = [r for r in rows if _key_val(r, col) is not None]
            null_rows = [r for r in rows if _key_val(r, col) is None]
            try:
                non_null.sort(
                    key=lambda r, c=col, coll=collation: _collate_key(_key_val(r, c), coll),
                    reverse=desc)
            except TypeError:
                non_null.sort(
                    key=lambda r, c=col, coll=collation: str(_collate_key(_key_val(r, c), coll)),
                    reverse=desc)
            rows = (null_rows + non_null) if nulls_first else (non_null + null_rows)
    if offset is not None:
        rows = rows[offset:]
    if limit is not None:
        rows = rows[:limit]
    return rows


def _apply_set_op(op: str, all_flag: bool,
                  left: list[dict], right: list[dict]) -> list[dict]:
    """Combine two row-lists with UNION / INTERSECT / EXCEPT semantics."""
    # SQL standard: output columns come from the leftmost SELECT; normalize
    # right-side rows to use left-side column names (positional alignment).
    if left and right:
        left_keys = list(left[0].keys())
        right_keys = list(right[0].keys())
        if left_keys != right_keys:
            def _remap(row: dict) -> dict:
                vals = list(row.values())
                return {left_keys[i]: vals[i] for i in range(min(len(left_keys), len(vals)))}
            right = [_remap(r) for r in right]

    def _key(row: dict) -> tuple:
        return tuple(row.values())

    if op == "UNION":
        if all_flag:
            return left + right
        seen: set[tuple] = set()
        out:  list[dict] = []
        for row in left + right:
            k = _key(row)
            if k not in seen:
                seen.add(k); out.append(row)
        return out

    if op == "INTERSECT":
        if all_flag:
            # Multiset: include min(left_count, right_count) copies
            counts: dict[tuple, int] = {}
            for r in right:
                k = _key(r); counts[k] = counts.get(k, 0) + 1
            used:  dict[tuple, int] = {}
            out = []
            for r in left:
                k = _key(r)
                used[k] = used.get(k, 0) + 1
                if used[k] <= counts.get(k, 0):
                    out.append(r)
            return out
        right_keys = {_key(r) for r in right}
        seen = set(); out = []
        for r in left:
            k = _key(r)
            if k in right_keys and k not in seen:
                seen.add(k); out.append(r)
        return out

    if op == "EXCEPT":
        if all_flag:
            # Multiset: include max(left_count - right_count, 0) copies
            counts = {}
            for r in right:
                k = _key(r); counts[k] = counts.get(k, 0) + 1
            out = []
            for r in left:
                k = _key(r)
                if counts.get(k, 0) > 0:
                    counts[k] -= 1
                else:
                    out.append(r)
            return out
        right_keys = {_key(r) for r in right}
        seen = set(); out = []
        for r in left:
            k = _key(r)
            if k not in right_keys and k not in seen:
                seen.add(k); out.append(r)
        return out

    raise InternalError(f"Unknown set operation: '{op}'")
