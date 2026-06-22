"""Parallel query execution: split table scans and aggregations across threads.

Design mirrors milansql's parallel_executor.hpp / executor/parallel_executor.hpp:
  - threshold (default 1000): minimum row count before parallelism activates
  - max_workers (default 4): thread count, clamped to os.cpu_count()
  - /*+ PARALLEL(N) */ query hint overrides max_workers for one statement

Parallel paths:
  1. parallel_filter  — apply a WHERE predicate concurrently across row chunks
  2. parallel_agg     — compute COUNT/SUM/MIN/MAX/AVG over row chunks, then merge
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from .where import WhereClause
    from .database import Database

_CPU_COUNT = max(os.cpu_count() or 1, 1)

DEFAULT_MAX_WORKERS = 4
DEFAULT_THRESHOLD   = 1000


@dataclass
class ParallelConfig:
    max_workers: int = DEFAULT_MAX_WORKERS
    threshold:   int = DEFAULT_THRESHOLD

    def effective_workers(self, hint: int | None = None) -> int:
        w = hint if hint is not None else self.max_workers
        return max(1, min(w, _CPU_COUNT))


# ── parallel WHERE filter ─────────────────────────────────────────────────────

def _filter_chunk(rows: list[dict], where: "WhereClause",
                  db: "Database") -> list[dict]:
    return [r for r in rows if where.evaluate(r, db)]


def parallel_filter(rows: list[dict], where: "WhereClause",
                    db: "Database", workers: int) -> list[dict]:
    """Filter *rows* with *where* predicate spread across *workers* threads.

    Falls back to single-threaded when rows < 2 or workers < 2.
    """
    n = len(rows)
    if workers <= 1 or n < 2:
        return [r for r in rows if where.evaluate(r, db)]

    workers = min(workers, n)
    chunk   = (n + workers - 1) // workers
    chunks  = [rows[i: i + chunk] for i in range(0, n, chunk)]

    result: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_filter_chunk, c, where, db) for c in chunks]
        for fut in futs:
            result.extend(fut.result())
    return result


# ── partial aggregate ─────────────────────────────────────────────────────────

@dataclass
class _PartialAgg:
    count:   int   = 0
    total:   float = 0.0
    min_val: Any   = None
    max_val: Any   = None
    has_val: bool  = False

    def update(self, v: Any) -> None:
        try:
            fv = float(v)
        except (TypeError, ValueError):
            self.count += 1
            return
        self.count  += 1
        self.total  += fv
        if not self.has_val or fv < self.min_val:
            self.min_val = fv
        if not self.has_val or fv > self.max_val:
            self.max_val = fv
        self.has_val = True


def _merge_partials(parts: list[_PartialAgg]) -> _PartialAgg:
    merged = _PartialAgg()
    for p in parts:
        merged.count += p.count
        merged.total += p.total
        if p.has_val:
            if not merged.has_val or p.min_val < merged.min_val:
                merged.min_val = p.min_val
            if not merged.has_val or p.max_val > merged.max_val:
                merged.max_val = p.max_val
            merged.has_val = True
    return merged


def _chunk_agg(rows: list[dict], col: str) -> _PartialAgg:
    p = _PartialAgg()
    for r in rows:
        v = r.get(col)
        if v is None:
            # try bare column name without table prefix
            for k, kv in r.items():
                if k.split(".")[-1] == col:
                    v = kv
                    break
        if v is not None:
            p.update(v)
    return p


def parallel_agg_column(rows: list[dict], col: str,
                         workers: int) -> _PartialAgg:
    """Compute COUNT/SUM/MIN/MAX for *col* across *workers* threads, then merge."""
    n = len(rows)
    if workers <= 1 or n < 2:
        p = _PartialAgg()
        for r in rows:
            v = r.get(col)
            if v is not None:
                p.update(v)
        return p

    workers = min(workers, n)
    chunk   = (n + workers - 1) // workers
    chunks  = [rows[i: i + chunk] for i in range(0, n, chunk)]

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs    = [ex.submit(_chunk_agg, c, col) for c in chunks]
        partials = [f.result() for f in futs]
    return _merge_partials(partials)


# ── high-level parallel aggregate dispatch ────────────────────────────────────

_SIMPLE_AGGS = {"COUNT", "SUM", "AVG", "MIN", "MAX"}


def parallel_compute_simple_aggs(rows: list[dict], columns: list[str],
                                  workers: int) -> "dict[str, Any] | None":
    """Compute simple (COUNT/SUM/AVG/MIN/MAX) aggregates in parallel.

    Returns a dict of {col_expr: value} for the columns it handled, or None
    if ANY column is not a simple aggregate (caller should fall back to
    sequential ``_compute_aggregates``).
    """
    import re
    _AGG_RE = re.compile(
        r'^(COUNT|SUM|AVG|MIN|MAX)\s*\(\s*(DISTINCT\s+)?(\*|[^)]+?)\s*\)\s*$',
        re.IGNORECASE)

    result: dict[str, Any] = {}
    for col in columns:
        m = _AGG_RE.match(col.strip())
        if m is None:
            return None   # non-simple column — caller must use sequential path
        func     = m.group(1).upper()
        distinct = bool(m.group(2))
        arg      = m.group(3).strip()

        if distinct:
            return None   # DISTINCT requires deduplication — skip parallel

        if func == "COUNT" and arg == "*":
            result[col] = len(rows)
            continue

        pa = parallel_agg_column(rows, arg, workers)
        if func == "COUNT":
            result[col] = pa.count
        elif func == "SUM":
            result[col] = pa.total if pa.has_val else None
        elif func == "AVG":
            result[col] = (pa.total / pa.count) if pa.count else None
        elif func == "MIN":
            result[col] = pa.min_val if pa.has_val else None
        elif func == "MAX":
            result[col] = pa.max_val if pa.has_val else None

    return result


# ── query hint parsing ────────────────────────────────────────────────────────

import re as _re
_HINT_RE = _re.compile(r'/\*\+\s*PARALLEL\s*\(\s*(\d+)\s*\)\s*\*/', _re.IGNORECASE)


def extract_parallel_hint(sql: str) -> "tuple[str, int | None]":
    """Strip ``/*+ PARALLEL(N) */`` hint from *sql*, return (clean_sql, N or None)."""
    m = _HINT_RE.search(sql)
    if m:
        n   = int(m.group(1))
        sql = sql[:m.start()] + sql[m.end():]
        return sql.strip(), n
    return sql, None
