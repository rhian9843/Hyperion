import struct
from typing import Any

from .constants import INTEGER, REAL, TEXT


def _encode_index_key(val: Any, col_type: str) -> int:
    """Encode a column value as a sort-preserving signed int64 B-tree key.

    INTEGER  — identity (already int64).
    REAL     — IEEE 754 bit-manipulation preserving float sort order.
    TEXT/VARCHAR — FNV-1a 64-bit hash (equality lookups only; collisions
                   are caught by the post-lookup row verification step).
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
    # TEXT / VARCHAR — FNV-1a 64-bit
    h = 14695981039346656037
    for b in str(val).encode("utf-8"):
        h = ((h ^ b) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return h if h < (1 << 63) else h - (1 << 64)


def _encode_composite_key(vals: list[Any], col_types: list[str]) -> int:
    """Encode a list of column values into a single int64 index key.
    For a single column, delegates to _encode_index_key (same result).
    For multiple columns, FNV-1a mixes the per-column encoded keys.
    """
    if len(vals) == 1:
        return _encode_index_key(vals[0], col_types[0])
    h = 14695981039346656037
    for val, col_type in zip(vals, col_types):
        k = _encode_index_key(val, col_type)
        for b in struct.pack(">q", k):
            h = ((h ^ b) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return h if h < (1 << 63) else h - (1 << 64)


_IDX_KEY_SZ  = 16         # index B-tree: 8-byte val_key + 8-byte rowid
_KEY_SIGN    = 1 << 63   # bias to convert signed int64 → unsigned for big-endian sort


def _make_index_key(val_key: int, rowid: int) -> int:
    """Pack (val_key: signed i64, rowid: u64) into an unsigned 128-bit Python int.

    Shifting val_key by 2^63 maps the full signed range to unsigned while
    preserving sort order, so composite keys compare correctly as plain ints.
    """
    return ((val_key + _KEY_SIGN) << 64) | rowid


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
            v = row.get(col)
            if v is None and col not in row and is_expr(col):
                v = eval_expr(col, row)
            return v

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

    raise RuntimeError(f"Unknown set operation: '{op}'")
