"""
Cost-based query optimizer for Hyperion.

Provides:
  - Row count estimation (stats-first, btree-scan fallback, session-cached)
  - Per-column NDV (number of distinct values) lookup from ANALYZE stats
  - Index availability lookup
  - Index-probe for INLJ (index-nested-loop join)
  - Join order optimisation for chains of INNER equijoins
"""

import math
import struct
from typing import Any

from .schema import deserialize_row
from .encoding import (_encode_composite_key, _make_index_key,
                       _encode_index_key, _MIN_VAL_KEY, _MAX_VAL_KEY)
from .constants import INTEGER, REAL


# ── Row count estimation ──────────────────────────────────────────────────────

def estimate_rows(db, table: str) -> int:
    """Return row count for table.

    Priority:
      1. Session cache (_opt_row_counts) — fastest.
      2. Persisted ANALYZE stats in catalog — avoids btree scan on cold start.
      3. Full btree scan — always correct, result is then cached for the session.
    """
    if not hasattr(db, "_opt_row_counts"):
        db._opt_row_counts = {}
    if table not in db._opt_row_counts:
        # Try persisted stats first
        catalog_stats = getattr(db._catalog, "stats", {})
        if table in catalog_stats and "row_count" in catalog_stats[table]:
            db._opt_row_counts[table] = catalog_stats[table]["row_count"]
        elif table not in db._catalog.tables:
            # CTE or view — no btree to scan; return a neutral estimate
            return 100
        else:
            meta = db._meta(table)
            if meta.storage_type == "column":
                import struct as _s
                hdr = db._pager.read_page(meta.root_page)
                db._opt_row_counts[table] = _s.unpack_from('<I', hdr, 1)[0]
            else:
                db._opt_row_counts[table] = sum(1 for _ in db._table_btree(meta).scan())
    return db._opt_row_counts[table]


def invalidate_row_count(db, table: str) -> None:
    """Drop the session row-count cache entry for table.

    Called after any DML that changes a table's row count so the next
    query against that table re-derives its estimate rather than using a
    stale value from before the write.
    """
    if hasattr(db, "_opt_row_counts"):
        db._opt_row_counts.pop(table, None)


# ── NDV (number of distinct values) ──────────────────────────────────────────

def get_ndv(db, table: str, col: str) -> int | None:
    """Return the number of distinct values for table.col from ANALYZE stats, or None."""
    col = col.split(".")[-1]
    catalog_stats = getattr(db._catalog, "stats", {})
    col_stats = catalog_stats.get(table, {}).get("columns", {}).get(col, {})
    return col_stats.get("ndv")


def _col_stats(db, table: str, col: str) -> dict:
    """Return the per-column stats dict from catalog (ndv, null_count, min_val, max_val, mcv, histogram)."""
    col = col.split(".")[-1]
    return (getattr(db._catalog, "stats", {})
            .get(table, {})
            .get("columns", {})
            .get(col, {}))


# ── Histogram-based selectivity ───────────────────────────────────────────────

def _histogram_range_sel(histogram: list, op: str, val) -> float:
    """Estimate selectivity for a single-sided range using an equi-depth histogram.

    histogram: list of [lo, hi, freq] triples from ANALYZE.
    Returns a fraction in [0.0, 1.0], or -1.0 if the histogram cannot be used.

    Boundary semantics:
      <  : strict — bucket [v, v] contributes nothing
      <= : inclusive — bucket [v, v] contributes fully
      >  : strict — bucket [v, v] contributes nothing
      >= : inclusive — bucket [v, v] contributes fully
    """
    try:
        v = float(val)
    except (TypeError, ValueError):
        return -1.0

    total = sum(freq for _, _, freq in histogram)
    if not total:
        return -1.0

    covered = 0.0
    for (blo, bhi, freq) in histogram:
        try:
            lo_f = float(blo)
            hi_f = float(bhi)
        except (TypeError, ValueError):
            return -1.0

        span = hi_f - lo_f

        if op == "<":
            if hi_f < v:
                covered += freq                              # bucket entirely below v
            elif lo_f < v:                                   # partial: [lo, v)
                covered += freq * (v - lo_f) / span if span > 0 else 0.0
        elif op == "<=":
            if hi_f <= v:
                covered += freq                              # bucket at or below v
            elif lo_f <= v:                                  # partial: [lo, v]
                covered += freq * (v - lo_f) / span if span > 0 else freq
        elif op == ">":
            if lo_f > v:
                covered += freq                              # bucket entirely above v
            elif hi_f > v:                                   # partial: (v, hi]
                covered += freq * (hi_f - v) / span if span > 0 else 0.0
        elif op == ">=":
            if lo_f >= v:
                covered += freq                              # bucket at or above v
            elif hi_f >= v:                                  # partial: [v, hi]
                covered += freq * (hi_f - v) / span if span > 0 else freq

    return covered / total


def estimate_selectivity(db, table: str, col: str, op: str, val) -> float:
    """Estimate fraction of rows in table satisfying col OP val in [0.0, 1.0].

    Priority for equality:   MCVs → 1/ndv.
    Priority for range ops:  equi-depth histogram → min/max linear interpolation.
    Returns 0.33 when no stats are available.
    """
    stats = _col_stats(db, table, col)
    if not stats:
        return 0.33

    row_count = (getattr(db._catalog, "stats", {})
                 .get(table, {})
                 .get("row_count", 1)) or 1
    ndv = stats.get("ndv") or 1

    if op == "=":
        for mcv_val, mcv_count in stats.get("mcv", []):
            if mcv_val == val:
                return mcv_count / row_count
        return 1.0 / ndv

    if op in ("<", "<=", ">", ">="):
        histogram = stats.get("histogram", [])
        if histogram:
            sel = _histogram_range_sel(histogram, op, val)
            if sel >= 0.0:
                return max(0.0, min(1.0, sel))
        # Fallback: linear interpolation using min/max
        min_v = stats.get("min_val")
        max_v = stats.get("max_val")
        if min_v is None or max_v is None:
            return 0.33
        try:
            v  = float(val)
            lo = float(min_v)
            hi = float(max_v)
        except (TypeError, ValueError):
            return 0.33
        if hi == lo:
            return 0.0 if op in ("<", ">") else 1.0
        span = hi - lo
        if op in (">", ">="):
            return max(0.0, min(1.0, (hi - v) / span))
        return max(0.0, min(1.0, (v - lo) / span))

    return 0.33


def estimate_range_selectivity(db, table: str, col: str, low, high) -> float:
    """Estimate fraction of rows satisfying low <= col <= high (BETWEEN).

    Uses equi-depth histogram when available; falls back to min/max linear
    interpolation.  Returns 0.33 when no stats exist.
    """
    stats = _col_stats(db, table, col)
    if not stats:
        return 0.33

    histogram = stats.get("histogram", [])
    if histogram:
        try:
            lo_f = float(low)
            hi_f = float(high)
        except (TypeError, ValueError):
            return 0.33
        if lo_f > hi_f:
            return 0.0

        total = sum(freq for _, _, freq in histogram)
        if not total:
            return 0.33

        covered = 0.0
        for (blo, bhi, freq) in histogram:
            try:
                blo_f = float(blo)
                bhi_f = float(bhi)
            except (TypeError, ValueError):
                return 0.33
            if bhi_f < lo_f or blo_f > hi_f:
                continue  # no overlap
            overlap_lo = max(blo_f, lo_f)
            overlap_hi = min(bhi_f, hi_f)
            span = bhi_f - blo_f
            frac = (overlap_hi - overlap_lo) / span if span > 0 else 1.0
            covered += freq * max(0.0, frac)

        return max(0.0, min(1.0, covered / total))

    # Fallback: linear min/max interpolation
    min_v = stats.get("min_val")
    max_v = stats.get("max_val")
    if min_v is None or max_v is None:
        return 0.33
    try:
        lo_f  = float(low)
        hi_f  = float(high)
        min_f = float(min_v)
        max_f = float(max_v)
    except (TypeError, ValueError):
        return 0.33
    span = max_f - min_f
    if span == 0:
        return 1.0 if min_f == lo_f else 0.0
    return max(0.0, min(1.0, (hi_f - lo_f) / span))


def estimate_row_count_with_where(db, table: str,
                                  conditions: list[tuple]) -> int:
    """Estimate rows satisfying all conditions via multiplicative selectivity.

    conditions: list of (col, op, val) tuples or (col, 'BETWEEN', (lo, hi)) tuples.
    """
    row_count = estimate_rows(db, table)
    sel = 1.0
    for item in conditions:
        col, op, val = item
        if op == "BETWEEN":
            lo, hi = val
            sel *= estimate_range_selectivity(db, table, col, lo, hi)
        else:
            sel *= estimate_selectivity(db, table, col, op, val)
    return max(1, int(row_count * sel))


# ── Index discovery ───────────────────────────────────────────────────────────

def find_eq_index(db, table: str, col: str):
    """Return an IndexMeta usable for equality lookup on table.col, or None."""
    col = col.split(".")[-1]
    for idx_meta in db.indexes.values():
        if idx_meta.table_name == table and idx_meta.columns and idx_meta.columns[0] == col:
            return idx_meta
    return None


# ── Index probe for INLJ ─────────────────────────────────────────────────────

def probe_index(db, right_table: str, right_col: str, val) -> list[dict] | None:
    """
    Probe right_table's index on right_col for rows where right_col = val.
    Returns a list of matching row dicts, or None if no usable index exists.
    """
    right_col = right_col.split(".")[-1]
    idx_meta = find_eq_index(db, right_table, right_col)
    if idx_meta is None:
        return None

    meta = db._meta(right_table)
    schema = meta.schema
    col_obj = next((c for c in schema.columns if c.name == right_col), None)
    if col_obj is None:
        return None

    try:
        if col_obj.type == INTEGER:
            val = int(val)
        elif col_obj.type == REAL:
            val = float(val)
        encoded = _encode_index_key(val, col_obj.type)
    except (ValueError, TypeError):
        return None

    n_extra = len(idx_meta.columns) - 1  # trailing columns in composite index
    if n_extra > 0:
        lo = _make_index_key([encoded] + [_MIN_VAL_KEY] * n_extra, 0)
        hi = _make_index_key([encoded] + [_MAX_VAL_KEY] * n_extra, 0xFFFFFFFFFFFFFFFF)
    else:
        lo = _make_index_key(encoded, 0)
        hi = _make_index_key(encoded, 0xFFFFFFFFFFFFFFFF)
    itree = db._index_btree(idx_meta)
    ptree = db._table_btree(meta)
    rows  = []
    for _, rowid_raw in itree.scan_range(lo, hi):
        rowid = struct.unpack("q", rowid_raw)[0]
        raw   = ptree.lookup(rowid)
        if raw is not None:
            rows.append(deserialize_row(schema, db._unpack_row_cell(raw)))
    return rows


# ── Cost model ────────────────────────────────────────────────────────────────

def _step_cost(db, left_count: int, right_table: str, right_col: str | None) -> float:
    """Estimate cost (abstract units) of one join step."""
    right_count = estimate_rows(db, right_table)
    if right_col and find_eq_index(db, right_table, right_col.split(".")[-1]):
        return left_count * (math.log2(max(right_count, 2)) + 1)
    return float(left_count * right_count)


def _output_estimate(db, left_count: int, right_table: str,
                     right_col: str | None) -> int:
    """Estimate join output row count using selectivity when available.

    Uses estimate_selectivity (MCV-aware) for equality joins when ANALYZE stats
    exist; falls back to 1/ndv, then geometric mean when there are no stats.
    """
    right_count = estimate_rows(db, right_table)
    if right_col:
        col = right_col.split(".")[-1]
        stats = _col_stats(db, right_table, col)
        if stats:
            sel = estimate_selectivity(db, right_table, col, "=", None)
            # estimate_selectivity returns 1/ndv for = with unknown val (no MCV hit on None)
            return max(1, int(left_count * right_count * sel))
        ndv = get_ndv(db, right_table, col)
        if ndv and ndv > 0:
            return max(1, int(left_count * right_count / ndv))
    return max(1, int(math.sqrt(left_count * right_count)))


# ── Join order optimiser ──────────────────────────────────────────────────────

def _talias(name: str, alias: str | None) -> str:
    return alias or name


def _left_alias_of(col: str | None) -> str | None:
    """Extract 'a' from 'a.id', or None if not qualified."""
    if col and "." in col:
        return col.split(".")[0]
    return None


_OUTER_JOIN_TYPES = frozenset({"LEFT", "LEFT OUTER", "RIGHT", "RIGHT OUTER",
                               "FULL", "FULL OUTER"})


def _reorder_inner_extras(
    seed_tables: list[tuple[str, str | None]],
    inner_extras: list[dict],
    db,
) -> list[dict] | None:
    """Greedy-reorder a list of INNER extra-join dicts given an already-placed seed set.

    seed_tables: tables already in the result (their aliases are 'placed' from the start).
    inner_extras: INNER extra-join dicts to reorder.
    Returns a reordered list, or None if reordering is impossible / produces no change.
    """
    if not inner_extras:
        return None

    # Collect table info
    tables   = list(seed_tables)  # fixed prefix (not reordered)
    ex_start = len(tables)        # index of first inner_extra table in `tables`
    ex_conds: list[tuple[str | None, str | None]] = []
    for ej in inner_extras:
        on_left = ej.get("on_left")
        if not on_left or "." not in on_left:
            return None  # unqualified — cannot reorder
        tables.append((ej["right_table"], ej.get("right_alias")))
        ex_conds.append((on_left, ej.get("on_right")))

    alias_to_idx: dict[str, int] = {}
    for i, (tname, talias) in enumerate(tables):
        alias_to_idx[_talias(tname, talias)] = i
        alias_to_idx[tname] = i

    # Build edges among the inner extras only (indices ex_start..n-1)
    edges: list[tuple[int, int, str, str]] = []
    for k, (on_left, on_right) in enumerate(ex_conds):
        right_idx = ex_start + k
        la        = _left_alias_of(on_left)
        left_idx  = alias_to_idx.get(la) if la else None
        if left_idx is None:
            return None
        edges.append((left_idx, right_idx,
                      on_left or "",
                      (on_right or "").split(".")[-1]))
        right_alias_str = _talias(*tables[right_idx])
        rev_ol  = f"{right_alias_str}.{(on_right or '').split('.')[-1]}"
        rev_or  = (on_left or "").split(".")[-1]
        edges.append((right_idx, left_idx, rev_ol, rev_or))

    # Greedy: start from all seed tables already placed, find best order for extras
    n_extras = len(inner_extras)
    placed   = set(range(ex_start))
    accum    = {_talias(*t) for t in seed_tables}
    # Estimate cost baseline: product of seed table sizes (rough)
    left_count = max(1, sum(estimate_rows(db, t[0]) for t in seed_tables))

    ordering: list[int] = []
    plan_conds: list[tuple[str | None, str | None]] = []

    for _ in range(n_extras):
        candidates: list[tuple[float, int, str, str]] = []
        for a_idx, b_idx, ol, or_ in edges:
            if a_idx not in placed or b_idx in placed:
                continue
            la = _left_alias_of(ol)
            if la and la not in accum:
                continue
            cost = _step_cost(db, left_count, tables[b_idx][0], or_)
            candidates.append((cost, b_idx, ol, or_))
        if not candidates:
            return None  # disconnected graph — cannot reorder
        candidates.sort(key=lambda x: x[0])
        _, nxt, ol, or_ = candidates[0]
        placed.add(nxt)
        ordering.append(nxt)
        plan_conds.append((ol, or_))
        accum.add(_talias(*tables[nxt]))
        left_count = _output_estimate(db, left_count, tables[nxt][0], or_ or None)

    # Check if ordering differs from original
    orig_order = list(range(ex_start, ex_start + n_extras))
    if ordering == orig_order:
        return None  # no change

    new_extras: list[dict] = []
    for k, idx in enumerate(ordering):
        orig_ej = inner_extras[idx - ex_start]
        ol_k, or_k = plan_conds[k]
        new_extras.append({**orig_ej, "on_left": ol_k, "on_right": or_k})
    return new_extras


def optimize_join(stmt: dict, db) -> dict:
    """
    Attempt to reorder a join statement's tables to minimise estimated cost.

    For pure INNER equijoin chains: full greedy reorder of all tables.
    For chains with an OUTER primary join: keep the primary pair fixed and
    greedy-reorder any subsequent INNER extra-joins.
    Returns the original stmt unchanged if no safe reorder is found.
    """
    extra     = stmt.get("extra_joins") or []
    join_type = stmt.get("join_type", "INNER")

    primary_is_outer = join_type in _OUTER_JOIN_TYPES

    # If primary is OUTER: keep primary pair fixed, reorder only INNER extra-joins
    if primary_is_outer:
        inner_extras = [ej for ej in extra if ej.get("join_type", "INNER") == "INNER"]
        non_inner    = [ej for ej in extra if ej.get("join_type", "INNER") != "INNER"]
        if not inner_extras:
            return stmt
        seed = [(stmt["left_table"],  stmt.get("left_alias")),
                (stmt["right_table"], stmt.get("right_alias"))]
        new_inner = _reorder_inner_extras(seed, inner_extras, db)
        if new_inner is None:
            return stmt
        # Interleave: non_inner extras keep their original relative positions
        # For simplicity, append non_inner extras after reordered inner ones
        new_stmt = dict(stmt)
        new_stmt["extra_joins"] = new_inner + non_inner
        return new_stmt

    # Original pure-INNER chain guard
    if join_type != "INNER":
        return stmt
    for ej in extra:
        if ej.get("join_type", "INNER") != "INNER":
            return stmt
    on_left_first = stmt.get("on_left")
    if not on_left_first or "." not in on_left_first:
        return stmt  # unqualified condition — skip

    # Build the table list and condition list
    # tables[0] = original left, tables[1] = original right, tables[2..] = extra joins
    tables: list[tuple[str, str | None]] = [
        (stmt["left_table"],  stmt.get("left_alias")),
        (stmt["right_table"], stmt.get("right_alias")),
    ]
    conds: list[tuple[str | None, str | None]] = [
        (stmt.get("on_left"), stmt.get("on_right")),
    ]
    for ej in extra:
        if not ej.get("on_left") or "." not in (ej["on_left"] or ""):
            return stmt  # unqualified extra condition — skip
        tables.append((ej["right_table"], ej.get("right_alias")))
        conds.append((ej.get("on_left"), ej.get("on_right")))

    n = len(tables)

    # alias → index mapping (both alias and table name for robustness)
    alias_to_idx: dict[str, int] = {}
    for i, (tname, talias) in enumerate(tables):
        alias_to_idx[_talias(tname, talias)] = i
        alias_to_idx[tname] = i

    # Build symmetric edge list: each condition can be traversed in either direction.
    # Edge: (from_idx, to_idx, on_left_col, on_right_col)
    edges: list[tuple[int, int, str, str]] = []
    for i, (on_left, on_right) in enumerate(conds):
        right_idx = i + 1
        la        = _left_alias_of(on_left)
        left_idx  = alias_to_idx.get(la) if la else None
        if left_idx is None:
            return stmt  # can't resolve — skip

        right_alias_str = _talias(*tables[right_idx])
        # Forward edge: left_idx → right_idx
        edges.append((left_idx, right_idx,
                       on_left or "",
                       (on_right or "").split(".")[-1]))
        # Reverse edge: right_idx → left_idx
        rev_on_left  = f"{right_alias_str}.{(on_right or '').split('.')[-1]}"
        rev_on_right = (on_left or "").split(".")[-1]
        edges.append((right_idx, left_idx, rev_on_left, rev_on_right))

    # Greedy planner: try every possible starting table, keep cheapest complete plan
    best_order:  list[int]                       | None = None
    best_conds:  list[tuple[str | None, str | None]] | None = None
    best_cost = float("inf")

    for start in range(n):
        placed     = {start}
        ordering   = [start]
        plan_conds: list[tuple[str | None, str | None]] = [(None, None)]
        accum      = {_talias(*tables[start])}
        left_count = estimate_rows(db, tables[start][0])
        total_cost = 0.0
        ok         = True

        for _ in range(n - 1):
            candidates: list[tuple[float, int, str, str]] = []
            for a_idx, b_idx, ol, or_ in edges:
                if a_idx not in placed or b_idx in placed:
                    continue
                la = _left_alias_of(ol)
                if la and la not in accum:
                    continue
                cost = _step_cost(db, left_count, tables[b_idx][0], or_)
                candidates.append((cost, b_idx, ol, or_))

            if not candidates:
                ok = False
                break

            candidates.sort(key=lambda x: x[0])
            step_cost, nxt, ol, or_ = candidates[0]
            total_cost += step_cost
            placed.add(nxt)
            ordering.append(nxt)
            plan_conds.append((ol, or_))
            accum.add(_talias(*tables[nxt]))
            # Use NDV-aware output estimate for accurate intermediate row counts
            left_count = _output_estimate(db, left_count, tables[nxt][0], or_ or None)

        if not ok:
            continue
        if total_cost < best_cost:
            best_cost  = total_cost
            best_order = list(ordering)
            best_conds = list(plan_conds)

    # If the greedy result is the original order, no change needed
    if best_order is None or best_order == list(range(n)):
        return stmt

    # Reconstruct stmt with reordered tables
    first_idx  = best_order[0]
    second_idx = best_order[1]
    ol1, or1   = best_conds[1]  # type: ignore[index]

    new_stmt = dict(stmt)
    new_stmt["left_table"]  = tables[first_idx][0]
    new_stmt["left_alias"]  = tables[first_idx][1]
    new_stmt["right_table"] = tables[second_idx][0]
    new_stmt["right_alias"] = tables[second_idx][1]
    new_stmt["on_left"]     = ol1
    new_stmt["on_right"]    = or1

    new_extra: list[dict] = []
    for k in range(2, n):
        tidx   = best_order[k]
        ol_k, or_k = best_conds[k]  # type: ignore[index]
        new_extra.append({
            "right_table": tables[tidx][0],
            "right_alias": tables[tidx][1],
            "on_left":     ol_k,
            "on_right":    or_k,
            "join_type":   "INNER",
        })
    new_stmt["extra_joins"] = new_extra
    return new_stmt
