"""Catalog introspection: _hyperion_master rows, SQL reconstruction, integrity_check."""
from typing import TYPE_CHECKING, Any

from .constants import TEXT, DEFAULT_TEXT_SIZE
from .schema import Column, Schema, deserialize_row

if TYPE_CHECKING:
    from .database import Database


# ── SQL reconstruction ────────────────────────────────────────────────────────

def _col_to_sql(col: Column) -> str:
    if col.type == TEXT and col.size != DEFAULT_TEXT_SIZE:
        type_str = f"VARCHAR({col.size})"
    else:
        type_str = col.type
    parts = [col.name, type_str]
    if not col.nullable and not col.primary_key:
        parts.append("NOT NULL")
    if col.primary_key:
        parts.append("PRIMARY KEY")
        if col.autoincrement:
            parts.append("AUTOINCREMENT")
    if col.unique and not col.primary_key:
        parts.append("UNIQUE")
    if col.default is not None:
        parts.append(f"DEFAULT {col.default}")
    if col.check:
        parts.append(f"CHECK ({col.check})")
    if col.is_generated:
        kw = "STORED" if col.generated_stored else "VIRTUAL"
        parts.append(f"AS ({col.generated_expr}) {kw}")
    return " ".join(parts)


def _schema_to_sql(tname: str, schema: Schema, temporary: bool = False) -> str:
    col_defs = [_col_to_sql(c) for c in schema.columns]

    if len(schema.primary_key_columns) > 1:
        col_defs.append(f"PRIMARY KEY ({', '.join(schema.primary_key_columns)})")

    for uc in schema.unique_constraints:
        col_defs.append(f"UNIQUE ({', '.join(uc)})")

    for fk in schema.foreign_keys:
        s = (f"FOREIGN KEY ({', '.join(fk.columns)}) "
             f"REFERENCES {fk.ref_table} ({', '.join(fk.ref_columns)})")
        if fk.on_delete not in ("RESTRICT", "NO ACTION"):
            s += f" ON DELETE {fk.on_delete}"
        if fk.on_update not in ("RESTRICT", "NO ACTION"):
            s += f" ON UPDATE {fk.on_update}"
        col_defs.append(s)

    temp = "TEMP " if temporary else ""
    return f"CREATE {temp}TABLE {tname} (\n  " + ",\n  ".join(col_defs) + "\n)"


def _trigger_to_sql(trig_name: str, meta: Any) -> str:
    """Reconstruct CREATE TRIGGER SQL from TriggerMeta."""
    timing = meta.timing
    event  = meta.event
    table  = meta.table
    of_clause = ""
    if event == "UPDATE" and meta.update_cols:
        of_clause = " OF " + ", ".join(meta.update_cols)
    when_clause = ""
    if meta.when_tokens:
        when_clause = " WHEN " + " ".join(meta.when_tokens)
    body = " ".join(meta.body_tokens)
    return (f"CREATE TRIGGER {trig_name} {timing} {event}{of_clause} "
            f"ON {table}{when_clause} FOR EACH ROW BEGIN {body} END")


# ── _hyperion_master rows ─────────────────────────────────────────────────────

def hyperion_master_rows(db: "Database") -> list[dict]:
    """Generate rows for the virtual _hyperion_master catalog table."""
    rows: list[dict] = []

    for tname, meta in db._catalog.tables.items():
        rows.append({
            "type":     "table",
            "name":     tname,
            "tbl_name": tname,
            "rootpage": meta.root_page,
            "sql":      _schema_to_sql(tname, meta.schema, meta.temporary),
        })

    for iname, imeta in db._catalog.indexes.items():
        cols = ", ".join(imeta.columns)
        rows.append({
            "type":     "index",
            "name":     iname,
            "tbl_name": imeta.table_name,
            "rootpage": imeta.root_page,
            "sql":      f"CREATE INDEX {iname} ON {imeta.table_name} ({cols})",
        })

    for vname, vsql in db._catalog.views.items():
        rows.append({
            "type":     "view",
            "name":     vname,
            "tbl_name": vname,
            "rootpage": 0,
            "sql":      f"CREATE VIEW {vname} AS {vsql}",
        })

    for trig_name, tmeta in db._catalog.triggers.items():
        rows.append({
            "type":     "trigger",
            "name":     trig_name,
            "tbl_name": tmeta.table,
            "rootpage": 0,
            "sql":      _trigger_to_sql(trig_name, tmeta),
        })

    return rows


# ── _hyperion_schema_meta rows ────────────────────────────────────────────────

def hyperion_schema_meta_rows(db: "Database") -> list[dict]:
    """Generate rows for the virtual _hyperion_schema_meta catalog table."""
    rows: list[dict] = []
    for obj_type, by_name in db._catalog.meta.items():
        for obj_name, tags in by_name.items():
            for key, value in tags.items():
                rows.append({
                    "object_type": obj_type,
                    "object_name": obj_name,
                    "key":         key,
                    "value":       value,
                })
    return rows


# ── integrity_check ───────────────────────────────────────────────────────────

def integrity_check(db: "Database") -> list[str]:
    """Scan all B-trees for structural integrity and page checksums.
    Returns ['ok'] or a list of error strings.
    """
    from .checksum import verify_page, CorruptPageError
    from .encoding import _idx_key_sz
    from .catalog import Catalog

    from .column_store import PAGE_COLUMN_HDR, PAGE_COLUMN_CHUNK

    errors: list[str] = []

    # ── Structural checks ─────────────────────────────────────────────────────

    for tname, meta in db._catalog.tables.items():
        prev_key: int | None = None
        try:
            if meta.storage_type == "column":
                # Page-type validation for HDR and chunk pages.
                hdr_pg = db._pager.read_page(meta.root_page)
                if hdr_pg[0] != PAGE_COLUMN_HDR:
                    errors.append(
                        f"table '{tname}': root page {meta.root_page} has "
                        f"type 0x{hdr_pg[0]:02x}, expected PAGE_COLUMN_HDR (0x10)"
                    )
                # Walk all chunk pages and check their type flag.
                for pn in db._collect_column_store_pages(meta.root_page):
                    if pn == meta.root_page:
                        continue
                    cp = db._pager.read_page(pn)
                    if cp[0] != PAGE_COLUMN_CHUNK:
                        errors.append(
                            f"table '{tname}': chunk page {pn} has "
                            f"type 0x{cp[0]:02x}, expected PAGE_COLUMN_CHUNK (0x11)"
                        )
                # Row-count consistency: header row_count vs rowid chain length.
                import struct as _s
                hdr_count = _s.unpack_from('<I', hdr_pg, 1)[0]
                actual_count = sum(1 for _ in db._table_btree(meta).scan_rows())
                if hdr_count != actual_count:
                    errors.append(
                        f"table '{tname}': header row_count={hdr_count} "
                        f"but rowid chain has {actual_count} entries"
                    )
                # Rowid ordering check.
                for key, _ in db._table_btree(meta).scan_rows():
                    if prev_key is not None and key <= prev_key:
                        errors.append(
                            f"table '{tname}': key ordering violation — "
                            f"key {key} follows {prev_key}"
                        )
                    prev_key = key
            else:
                for key, raw in db._table_btree(meta).scan():
                    if prev_key is not None and key <= prev_key:
                        errors.append(
                            f"table '{tname}': key ordering violation — "
                            f"key {key} follows {prev_key}"
                        )
                    prev_key = key
                    try:
                        deserialize_row(meta.schema, db._unpack_row_cell(raw))
                    except Exception as exc:
                        errors.append(f"table '{tname}': corrupt row at key {key}: {exc}")
        except Exception as exc:
            errors.append(f"table '{tname}': scan failed: {exc}")

    for iname, imeta in db._catalog.indexes.items():
        if imeta.table_name not in db._catalog.tables:
            errors.append(
                f"index '{iname}': references non-existent table '{imeta.table_name}'"
            )
            continue
        try:
            idx_btree = db._index_btree(imeta)
            prev_ikey: bytes | None = None
            for ikey, _ in idx_btree.scan():
                if prev_ikey is not None and ikey < prev_ikey:
                    errors.append(
                        f"index '{iname}': key ordering violation"
                    )
                prev_ikey = ikey
        except Exception as exc:
            errors.append(f"index '{iname}': B-tree scan failed: {exc}")

    # ── Page checksum scan ────────────────────────────────────────────────────
    # Collect every known non-free page and verify its CRC.

    known: set[int] = {Catalog.CATALOG_PAGE}
    known.update(db._catalog_extra)
    if db._catalog_ops_pn:
        known.add(db._catalog_ops_pn)
    known.update(db._catalog_ops_extra)
    for tmeta in db._catalog.tables.values():
        if tmeta.storage_type == "column":
            known.update(db._collect_column_store_pages(tmeta.root_page))
        else:
            known.update(db._collect_tree_pages(tmeta.root_page))
    for imeta in db._catalog.indexes.values():
        known.update(db._collect_tree_pages(imeta.root_page,
                                            key_sz=_idx_key_sz(len(imeta.columns))))
    free = set(db._catalog.free_pages)

    for pn in sorted(known - free):
        try:
            page = db._pager.read_page(pn)
            verify_page(page, pn)
        except CorruptPageError as exc:
            errors.append(str(exc))

    return errors if errors else ["ok"]


# ── EXPLAIN QUERY PLAN ────────────────────────────────────────────────────────

def explain_plan(stmt: dict, db: "Database") -> list[dict]:
    """Return EXPLAIN QUERY PLAN rows for the given parsed statement."""
    counter = [0]
    rows: list[dict] = []
    _plan_rows(stmt, db, rows, counter, parent=0)
    return rows


def _plan_rows(stmt: dict, db: "Database", out: list[dict],
               counter: list[int], parent: int) -> None:
    op = stmt.get("op", "")

    # Materialise any CTEs first
    for cte_name, cte_ast in (stmt.get("ctes") or {}).items():
        cte_id = counter[0]; counter[0] += 1
        out.append({"id": cte_id, "parent": parent, "notused": 0,
                    "detail": f"MATERIALIZE CTE {cte_name}"})
        _plan_rows(cte_ast, db, out, counter, parent=cte_id)

    my_id = counter[0]; counter[0] += 1

    if op == "SELECT_NOFROM":
        out.append({"id": my_id, "parent": parent, "notused": 0,
                    "detail": "MATERIALIZE constant row"})
        return

    if op == "SELECT":
        tbl = stmt.get("table") or ""
        sub = stmt.get("subquery_from")
        if sub:
            out.append({"id": my_id, "parent": parent, "notused": 0,
                        "detail": "MATERIALIZE derived table"})
            sub_id = counter[0]
            _plan_rows(sub, db, out, counter, parent=my_id)
            return
        if tbl in db._catalog.views:
            out.append({"id": my_id, "parent": parent, "notused": 0,
                        "detail": f"MATERIALIZE VIEW {tbl}"})
            return
        if tbl not in db._catalog.tables:
            out.append({"id": my_id, "parent": parent, "notused": 0,
                        "detail": f"SCAN {tbl}"})
            return
        where    = stmt.get("where")
        group_by = stmt.get("group_by")
        order_by = stmt.get("order_by")
        columns  = stmt.get("columns") or []
        idx_name = _pick_index_name(db, tbl, where)
        is_col   = (tbl in db._catalog.tables and
                    db._catalog.tables[tbl].storage_type == "column")
        if idx_name:
            detail = f"SEARCH TABLE {tbl} USING INDEX {idx_name}"
        else:
            col_tag = " [COLUMN STORE]" if is_col else ""
            detail  = f"SCAN TABLE {tbl}{col_tag}"
        if group_by:
            detail += f" (GROUP BY {', '.join(group_by)})"
        if order_by:
            order_cols = ", ".join(
                f"{o['col']} {'DESC' if o.get('desc') else 'ASC'}" for o in order_by
            )
            detail += f" (ORDER BY {order_cols})"
        if is_col and _is_aggregate_pushdown(columns, group_by):
            detail += " [AGGREGATE PUSHDOWN]"
        out.append({"id": my_id, "parent": parent, "notused": 0, "detail": detail})
        return

    if op == "JOIN":
        from .join_strategies import choose_strategy, NESTED_LOOP_THRESHOLD
        ltbl     = stmt.get("left_table", "")
        rtbl     = stmt.get("right_table", "")
        join_type = stmt.get("join_type", "INNER")
        on_left  = stmt.get("on_left")
        on_right = stmt.get("on_right")

        left_detail = _table_scan_detail(db, ltbl)
        out.append({"id": my_id, "parent": parent, "notused": 0, "detail": left_detail})

        right_id = counter[0]; counter[0] += 1
        right_col = on_right.split(".")[-1] if on_right else None
        use_inlj = (join_type == "INNER" and right_col is not None
                    and _find_index_for_col(db, rtbl, right_col) is not None)
        if use_inlj:
            idx = _find_index_for_col(db, rtbl, right_col)
            right_detail = (f"SEARCH TABLE {rtbl} USING INDEX {idx} "
                            f"({on_right or right_col}=?) [INLJ]")
        elif right_col and rtbl in db._catalog.tables:
            left_has_idx  = (on_left is not None
                             and _find_index_for_col(db, ltbl,
                                                     on_left.split(".")[-1]) is not None)
            right_has_idx = _find_index_for_col(db, rtbl, right_col) is not None
            ltbl_count    = _estimate_row_count(db, ltbl)
            rtbl_count    = _estimate_row_count(db, rtbl)
            strategy = choose_strategy(ltbl_count, rtbl_count,
                                       left_has_idx, right_has_idx, join_type)
            right_detail = f"SCAN TABLE {rtbl} [{strategy}]"
        else:
            right_detail = _table_scan_detail(db, rtbl)
        out.append({"id": right_id, "parent": my_id, "notused": 0,
                    "detail": right_detail})

        for ej in stmt.get("extra_joins") or []:
            ej_id    = counter[0]; counter[0] += 1
            ej_rtbl  = ej.get("right_table", "")
            ej_rcol  = (ej.get("on_right") or "").split(".")[-1] or None
            ej_jtype = ej.get("join_type", "INNER")
            ej_use_inlj = (ej_jtype == "INNER" and ej_rcol
                           and _find_index_for_col(db, ej_rtbl, ej_rcol) is not None)
            if ej_use_inlj:
                ej_idx = _find_index_for_col(db, ej_rtbl, ej_rcol)
                ej_detail = (f"SEARCH TABLE {ej_rtbl} USING INDEX {ej_idx} "
                             f"({ej_rcol}=?) [INLJ]")
            elif ej_rcol and ej_rtbl in db._catalog.tables:
                ej_right_has_idx = _find_index_for_col(db, ej_rtbl, ej_rcol) is not None
                ej_count = _estimate_row_count(db, ej_rtbl)
                ej_strategy = choose_strategy(NESTED_LOOP_THRESHOLD + 1, ej_count,
                                              False, ej_right_has_idx, ej_jtype)
                ej_detail = f"SCAN TABLE {ej_rtbl} [{ej_strategy}]"
            else:
                ej_detail = _table_scan_detail(db, ej_rtbl)
            out.append({"id": ej_id, "parent": my_id, "notused": 0,
                        "detail": ej_detail})
        return

    if op == "SET_OP":
        set_kw = stmt.get("set_op", "SET OPERATION")
        qual   = "" if stmt.get("all") else "DISTINCT "
        out.append({"id": my_id, "parent": parent, "notused": 0,
                    "detail": f"{set_kw} {qual}(two subqueries)"})
        _plan_rows(stmt["left"],  db, out, counter, parent=my_id)
        _plan_rows(stmt["right"], db, out, counter, parent=my_id)
        return

    # Fallback
    out.append({"id": my_id, "parent": parent, "notused": 0,
                "detail": f"EXECUTE {op}"})


def _estimate_row_count(db: "Database", tbl: str) -> int:
    """Return row count from ANALYZE stats, or threshold+1 if not yet analyzed."""
    from .join_strategies import NESTED_LOOP_THRESHOLD
    row_count = db._catalog.stats.get(tbl, {}).get("row_count", -1)
    if row_count >= 0:
        return row_count
    return NESTED_LOOP_THRESHOLD + 1


def _table_scan_detail(db: "Database", tbl: str) -> str:
    if tbl in db._catalog.views:
        return f"MATERIALIZE VIEW {tbl}"
    if tbl in db._catalog.tables:
        tag = " [COLUMN STORE]" if db._catalog.tables[tbl].storage_type == "column" else ""
        return f"SCAN TABLE {tbl}{tag}"
    return f"SCAN {tbl}"


def _pick_index_name(db: "Database", tbl: str,
                     where: Any) -> str | None:
    """Return an index name usable for this WHERE, or None."""
    if where is None:
        return None
    try:
        idx, _ = db._find_index_for_where(tbl, where)
        if idx:
            return next((n for n, m in db.indexes.items() if m is idx), None)
    except Exception:
        pass
    return None


def _find_index_for_col(db: "Database", tbl: str, col: str) -> str | None:
    """Return an index name on tbl.col, or None."""
    from .optimizer import find_eq_index
    idx = find_eq_index(db, tbl, col)
    if idx:
        return next((n for n, m in db.indexes.items() if m is idx), None)
    return None


def _is_aggregate_pushdown(columns: list[str],
                            group_by: list[str] | None) -> bool:
    """Return True if all selected columns would be handled by aggregate pushdown."""
    import re as _re
    _PAT = _re.compile(
        r'^(COUNT|MIN|MAX|SUM|AVG)\s*\(\s*(DISTINCT\s+)?(.+?)\s*\)$',
        _re.IGNORECASE,
    )
    group_set = set(group_by or [])
    for col in columns:
        if col in group_set:
            continue
        m = _PAT.match(col.strip())
        if not m:
            return False
        if m.group(2):           # DISTINCT — requires materialisation
            return False
    return bool(columns)         # at least one column selected
