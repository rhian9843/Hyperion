"""
Join strategy planner and algorithms.

Strategy selection mirrors milansql's join_planner.hpp:
  - Both sides < THRESHOLD rows → NESTED_LOOP (caller handles directly)
  - Both join columns indexed + INNER join → MERGE_JOIN
  - Otherwise → HASH_JOIN

Hash join:   O(n + m)        — build hash table on right, probe with left
Merge join:  O(n log n + m log m) — sort-index both sides, two-pointer merge
             INNER only; LEFT/RIGHT/FULL fall back to hash join.
"""

from typing import Any, Callable

NESTED_LOOP_THRESHOLD = 10


# ── planner ───────────────────────────────────────────────────────────────────

def choose_strategy(left_count: int, right_count: int,
                    left_has_index: bool, right_has_index: bool,
                    join_type: str) -> str:
    """Return 'NESTED_LOOP', 'HASH_JOIN', or 'MERGE_JOIN'."""
    if left_count < NESTED_LOOP_THRESHOLD and right_count < NESTED_LOOP_THRESHOLD:
        return "NESTED_LOOP"
    if left_has_index and right_has_index and join_type == "INNER":
        return "MERGE_JOIN"
    return "HASH_JOIN"


# ── sort key (handles None, int/float coercion) ───────────────────────────────

def _sort_key(val: Any):
    if val is None:
        return (0,)
    try:
        return (1, float(str(val)), "")
    except (ValueError, TypeError):
        return (1, 0.0, str(val))


# ── hash join ─────────────────────────────────────────────────────────────────

def hash_join(
    left_rows:  list[dict],
    right_rows: list[dict],
    lcol: str,
    rcol: str,
    join_type: str,
    la: str,
    ra: str,
    emit_fn: Callable[[dict], "dict | None"],
    right_null: dict,
    left_null:  dict,
) -> list[dict]:
    """Hash join: build on right side, probe with left. O(n + m).

    Supports INNER, LEFT, RIGHT, and FULL join types.
    """
    def _merge(lr: dict, rr: dict) -> dict:
        m = {f"{la}.{k}": v for k, v in lr.items()}
        m.update({f"{ra}.{k}": v for k, v in rr.items()})
        return m

    # Build phase: index right rows by join-column value
    hash_map: dict[Any, list[tuple[int, dict]]] = {}
    for ri, rr in enumerate(right_rows):
        key = rr.get(rcol)
        hash_map.setdefault(key, []).append((ri, rr))

    results: list[dict] = []
    matched_right: set[int] = set()

    # Probe phase: for each left row look up hash map
    for lr in left_rows:
        lval   = lr.get(lcol)
        bucket = hash_map.get(lval, [])
        if bucket:
            for ri, rr in bucket:
                matched_right.add(ri)
                row = emit_fn(_merge(lr, rr))
                if row is not None:
                    results.append(row)
        elif join_type in ("LEFT", "LEFT OUTER", "FULL", "FULL OUTER"):
            merged = {f"{la}.{k}": v for k, v in lr.items()}
            merged.update(right_null)
            row = emit_fn(merged)
            if row is not None:
                results.append(row)

    if join_type in ("RIGHT", "RIGHT OUTER", "FULL", "FULL OUTER"):
        for ri, rr in enumerate(right_rows):
            if ri not in matched_right:
                merged = dict(left_null)
                merged.update({f"{ra}.{k}": v for k, v in rr.items()})
                row = emit_fn(merged)
                if row is not None:
                    results.append(row)

    return results


# ── merge join (INNER only) ───────────────────────────────────────────────────

def merge_join_inner(
    left_rows:  list[dict],
    right_rows: list[dict],
    lcol: str,
    rcol: str,
    la: str,
    ra: str,
    emit_fn: Callable[[dict], "dict | None"],
) -> list[dict]:
    """Sort-merge join for INNER equi-joins. O(n log n + m log m).

    Sorts index arrays (no data copy), then advances two pointers.
    On equal keys outputs the cross product of matching groups.
    """
    def _merge(lr: dict, rr: dict) -> dict:
        m = {f"{la}.{k}": v for k, v in lr.items()}
        m.update({f"{ra}.{k}": v for k, v in rr.items()})
        return m

    # Step 1: build sorted index arrays
    left_idx  = sorted(range(len(left_rows)),
                       key=lambda i: _sort_key(left_rows[i].get(lcol)))
    right_idx = sorted(range(len(right_rows)),
                       key=lambda i: _sort_key(right_rows[i].get(rcol)))

    results: list[dict] = []
    li, ri = 0, 0

    # Step 2: two-pointer merge
    while li < len(left_idx) and ri < len(right_idx):
        lv = _sort_key(left_rows[left_idx[li]].get(lcol))
        rv = _sort_key(right_rows[right_idx[ri]].get(rcol))

        if lv < rv:
            li += 1
            continue
        if lv > rv:
            ri += 1
            continue

        # Equal: find the extent of the right group for this key
        ri_end = ri + 1
        while (ri_end < len(right_idx)
               and _sort_key(right_rows[right_idx[ri_end]].get(rcol)) == lv):
            ri_end += 1

        # Step 3: cross product — advance li over all left rows with same key
        li_start = li
        while (li < len(left_idx)
               and _sort_key(left_rows[left_idx[li]].get(lcol)) == lv):
            lr = left_rows[left_idx[li]]
            for rj in range(ri, ri_end):
                rr = right_rows[right_idx[rj]]
                row = emit_fn(_merge(lr, rr))
                if row is not None:
                    results.append(row)
            li += 1

        ri = ri_end

    return results
