"""
Query rewriter for Hyperion.

Ported from milansql query_rewriter.hpp (Phase 82) with additions for
Hyperion's AST and WhereClause structure.

Transformations applied in order:
  1. Remove always-true conditions  — WHERE 1=1 stripped
  2. Detect always-false conditions — WHERE 1=0 → zero-row sentinel
  3. Redundant condition elimination — col > 100 AND col > 50 → col > 100
  4. Note IN (SELECT ...) subqueries as JOIN candidates
"""
from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .where import WhereClause


# ── WhereClause helpers ───────────────────────────────────────────────────────

def _flatten_and_chain(wc: "WhereClause") -> list["WhereClause"]:
    """Return a flat list of the AND-chained WhereClause nodes.

    Stops at the first node that has an or_clause so OR branches are
    never touched by the rewriter.
    """
    nodes = []
    cur = wc
    while cur is not None:
        # Don't rewrite if any node in the chain has an OR branch — we can't
        # safely reassign or_clause without changing semantics.
        if cur.or_clause is not None:
            return [wc]  # bail: return original as single-element list
        nodes.append(cur)
        cur = cur.and_clause
    return nodes


def _rebuild_and_chain(nodes: list["WhereClause"]) -> "WhereClause | None":
    """Rebuild a linked AND chain from a flat list. Returns None for empty."""
    if not nodes:
        return None
    # Build right-to-left so and_clause pointers are correct
    result = dataclasses.replace(nodes[-1], and_clause=None)
    for node in reversed(nodes[:-1]):
        result = dataclasses.replace(node, and_clause=result)
    return result


def _where_to_str(wc: "WhereClause | None") -> str:
    """Best-effort SQL representation of a WhereClause tree."""
    if wc is None:
        return "(none)"
    parts = []
    cur = wc
    while cur is not None:
        if cur.op == "GROUP":
            inner = _where_to_str(cur.group_clause)
            parts.append(f"({inner})")
        elif cur.op in ("IN", "NOT IN") and cur.val == "__subquery__":
            parts.append(f"{cur.col} {cur.op} (SELECT ...)")
        else:
            parts.append(f"{cur.col} {cur.op} {cur.val}")
        cur = cur.and_clause
    expr = " AND ".join(parts)
    if wc.or_clause:
        expr = f"{expr} OR {_where_to_str(wc.or_clause)}"
    return expr


# ── Rewrite phases ────────────────────────────────────────────────────────────

def _remove_always_true(nodes: list["WhereClause"]) -> tuple[list, list[str]]:
    """Phase 1: strip trivially-true conditions like 1=1 and TRUE."""
    kept, notes = [], []
    for node in nodes:
        col, op, val = node.col.strip("'"), node.op, node.val.strip("'")
        always_true = (
            # 1=1, '1'='1'
            (col == "1" and op == "=" and val == "1") or
            # 0=0
            (col == "0" and op == "=" and val == "0") or
            # TRUE = TRUE (literal)
            (col.upper() == "TRUE" and op == "=" and val.upper() == "TRUE") or
            # col >= col (same identifier both sides — rare but valid)
            False
        )
        if always_true:
            notes.append(f"Removed always-true condition: {node.col} {op} {node.val}")
        else:
            kept.append(node)
    return kept, notes


def _check_always_false(nodes: list["WhereClause"]) -> tuple[bool, list[str]]:
    """Phase 2: detect conditions that can never be true (1=0, 1=2, etc.)."""
    notes = []
    for node in nodes:
        col, op, val = node.col.strip("'"), node.op, node.val.strip("'")
        always_false = (
            # 1=0, 1=2, literal numeric mismatch
            (_is_numeric(col) and op == "=" and _is_numeric(val)
             and float(col) != float(val)) or
            # FALSE = TRUE
            (col.upper() in ("FALSE", "0") and op == "="
             and val.upper() in ("TRUE", "1")) or
            (col.upper() in ("TRUE", "1") and op == "="
             and val.upper() in ("FALSE", "0"))
        )
        if always_false:
            notes.append(
                f"Always-false condition detected: {node.col} {op} {node.val} "
                f"— query returns 0 rows"
            )
            return True, notes
    return False, notes


def _remove_redundant_conditions(
    nodes: list["WhereClause"],
) -> tuple[list, list[str]]:
    """Phase 3: constant folding on same-column range predicates.

    Mirrors milansql removeRedundantConditions():
      col > 100 AND col > 50  →  col > 100  (keep more restrictive lower bound)
      col < 50  AND col < 100 →  col < 50   (keep more restrictive upper bound)
    """
    if len(nodes) < 2:
        return nodes, []

    n = len(nodes)
    redundant = [False] * n
    notes: list[str] = []

    for i in range(n):
        if redundant[i]:
            continue
        ci = nodes[i]
        if ci.op not in (">", ">=", "<", "<="):
            continue
        try:
            vi = float(ci.val)
        except (ValueError, TypeError):
            continue

        for j in range(n):
            if i == j or redundant[j]:
                continue
            cj = nodes[j]
            if ci.col != cj.col or ci.op != cj.op:
                continue
            try:
                vj = float(cj.val)
            except (ValueError, TypeError):
                continue

            if ci.op in (">", ">=") and vi > vj:
                redundant[j] = True
                notes.append(
                    f"Removed redundant condition: {cj.col} {cj.op} {cj.val} "
                    f"(superseded by {ci.col} {ci.op} {ci.val})"
                )
            elif ci.op in ("<", "<=") and vi < vj:
                redundant[j] = True
                notes.append(
                    f"Removed redundant condition: {cj.col} {cj.op} {cj.val} "
                    f"(superseded by {ci.col} {ci.op} {ci.val})"
                )

    kept = [nodes[i] for i in range(n) if not redundant[i]]
    return kept, notes


def _note_subqueries(nodes: list["WhereClause"]) -> list[str]:
    """Phase 4: note IN (SELECT ...) clauses as JOIN candidates (milansql phase A)."""
    notes = []
    for node in nodes:
        if node.op in ("IN", "NOT IN") and node.val == "__subquery__":
            sub = node.subquery_ast or {}
            sub_table = sub.get("table", "?")
            sub_cols = sub.get("columns", ["?"])
            sub_col = sub_cols[0] if sub_cols else "?"
            notes.append(
                f"{node.col} {node.op} (SELECT {sub_col} FROM {sub_table})"
                f" identified as JOIN candidate"
            )
    return notes


# ── Helper ────────────────────────────────────────────────────────────────────

def _is_numeric(s: str) -> bool:
    try:
        float(s)
        return True
    except (ValueError, TypeError):
        return False


# ── Public API ────────────────────────────────────────────────────────────────

class QueryRewriter:
    """Rule-based query rewriter applied before execution.

    Mirrors milansql QueryRewriter with extensions for WHERE 1=0 and
    Hyperion's linked-list WhereClause structure.
    """

    def __init__(self) -> None:
        self._enabled: bool = True   # on by default
        self.notes: list[str] = []
        self.original_where_str: str = ""

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, v: bool) -> None:
        self._enabled = v

    def rewrite(self, stmt: dict) -> dict:
        """Rewrite stmt dict in-place. Populates self.notes. Returns stmt."""
        self.notes = []
        if not self._enabled:
            return stmt

        op = stmt.get("op", "")
        if op not in ("SELECT", "JOIN", "SELECT_NOFROM"):
            return stmt

        where = stmt.get("where")
        self.original_where_str = _where_to_str(where)

        if where is None:
            return stmt

        and_chain = _flatten_and_chain(where)

        # Bail if we got a single-element list back due to OR branches
        if len(and_chain) == 1 and and_chain[0] is where and where.or_clause:
            return stmt

        # Phase 1: remove always-true
        and_chain, n1 = _remove_always_true(and_chain)
        self.notes.extend(n1)

        # Phase 2: always-false detection
        always_false, n2 = _check_always_false(and_chain)
        self.notes.extend(n2)
        if always_false:
            stmt["where_always_false"] = True
            stmt["where"] = _rebuild_and_chain(and_chain)
            return stmt

        # Phase 3: redundant condition elimination
        and_chain, n3 = _remove_redundant_conditions(and_chain)
        self.notes.extend(n3)

        # Phase 4: note subqueries
        n4 = _note_subqueries(and_chain)
        self.notes.extend(n4)

        stmt["where"] = _rebuild_and_chain(and_chain)
        return stmt
