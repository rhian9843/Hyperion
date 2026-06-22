"""LRU query result cache with TTL and per-table invalidation."""

from dataclasses import dataclass, field
from time import monotonic
from typing import Any

TTL_SECONDS = 30
MAX_ENTRIES  = 100


@dataclass
class _CacheEntry:
    rows:        list[dict]
    tables:      set[str]
    cached_at:   float
    last_access: float
    hit_count:   int = 0


class QueryCache:
    """LRU cache for SELECT result sets.

    Off by default.  Enable with ``SET CACHE ON``.
    Invalidated per-table on any write to a tracked table.
    """

    def __init__(self) -> None:
        self._enabled:       bool                     = False
        self._cache:         dict[str, _CacheEntry]   = {}
        self._total_hits:    int                       = 0
        self._total_misses:  int                       = 0
        self._ttl:           int                       = TTL_SECONDS
        self._max_size:      int                       = MAX_ENTRIES

    # ── public toggles ────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    def enable(self)  -> None: self._enabled = True
    def disable(self) -> None: self._enabled = False

    # ── cache operations ──────────────────────────────────────────────────────

    def get(self, key: str) -> "list[dict] | None":
        if not self._enabled:
            return None
        entry = self._cache.get(key)
        if entry is None:
            self._total_misses += 1
            return None
        now = monotonic()
        if now - entry.cached_at > self._ttl:
            del self._cache[key]
            self._total_misses += 1
            return None
        entry.last_access = now
        entry.hit_count  += 1
        self._total_hits  += 1
        return list(entry.rows)  # defensive copy

    def put(self, key: str, rows: list[dict], tables: set[str]) -> None:
        if not self._enabled:
            return
        if len(self._cache) >= self._max_size:
            # LRU eviction: drop entry with oldest last_access
            oldest = min(self._cache, key=lambda k: self._cache[k].last_access)
            del self._cache[oldest]
        now = monotonic()
        self._cache[key] = _CacheEntry(
            rows=list(rows), tables=tables,
            cached_at=now, last_access=now)

    def invalidate(self, table: str) -> int:
        """Remove all entries that reference *table*. Returns count evicted."""
        to_del = [k for k, e in self._cache.items() if table in e.tables]
        for k in to_del:
            del self._cache[k]
        return len(to_del)

    def clear(self) -> None:
        self._cache.clear()

    # ── status ────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        total    = self._total_hits + self._total_misses
        hit_rate = (f"{self._total_hits * 100 // total}%"
                    if total else "N/A")
        return {
            "status":      "ON" if self._enabled else "OFF",
            "entries":     len(self._cache),
            "max_entries": self._max_size,
            "ttl_seconds": self._ttl,
            "hits":        self._total_hits,
            "misses":      self._total_misses,
            "hit_rate":    hit_rate,
        }


# ── helpers used by cursor.py ─────────────────────────────────────────────────

def make_cache_key(raw_sql: str, params: Any) -> str:
    return raw_sql if params is None else f"{raw_sql}|{repr(params)}"


def extract_tables(stmt: dict) -> set[str]:
    """Return all physical table names referenced by *stmt* (for cache invalidation)."""
    tables: set[str] = set()
    op = stmt.get("op", "")

    # Primary / left table
    for key in ("table", "left_table"):
        t = stmt.get(key)
        if t and isinstance(t, str) and not t.startswith("("):
            tables.add(t)

    # Right table in JOIN
    rt = stmt.get("right_table")
    if rt and isinstance(rt, str):
        tables.add(rt)

    # Extra joins
    for ej in stmt.get("extra_joins") or []:
        et = ej.get("right_table")
        if et:
            tables.add(et)

    # FROM subquery — recurse
    from_sub = stmt.get("from_subquery")
    if isinstance(from_sub, dict):
        tables |= extract_tables(from_sub)

    # CTEs
    for cte in (stmt.get("ctes") or {}).values():
        if isinstance(cte, dict):
            tables |= extract_tables(cte)

    return tables
