import base64
import json
import struct
from dataclasses import dataclass, field

from .constants import PAGE_SIZE
from .schema import Schema


@dataclass
class TriggerMeta:
    table:       str
    timing:      str        # "BEFORE" or "AFTER"
    event:       str        # "INSERT", "UPDATE", "DELETE"
    update_cols: list       # UPDATE OF cols; empty = any column
    when_tokens: list       # WHEN expr tokens; empty = no condition
    body_tokens: list       # tokens between BEGIN and END


@dataclass
class TableMeta:
    schema:       Schema
    root_page:    int
    next_page:    int
    next_key:     int
    temporary:    bool = False
    storage_type: str  = "row"   # "row" or "column"


@dataclass
class IndexMeta:
    table_name: str
    columns:    list[str]
    root_page:  int
    next_page:  int
    unique:     bool = False

    @property
    def column_name(self) -> str:
        return self.columns[0]


@dataclass
class Catalog:
    tables:         dict[str, TableMeta]      = field(default_factory=dict)
    indexes:        dict[str, IndexMeta]      = field(default_factory=dict)
    views:          dict[str, str]            = field(default_factory=dict)
    next_free_page: int                       = 1   # page 0 = catalog
    free_pages:     list[int]                 = field(default_factory=list)
    # stats["table"] = {"row_count": N, "columns": {"col": {"ndv": K}}}
    stats:          dict[str, dict]           = field(default_factory=dict)
    triggers:       dict[str, TriggerMeta]    = field(default_factory=dict)
    # meta[object_type][object_name][key] = value
    # e.g. meta["table"]["users"]["description"] = "User account records"
    meta:           dict[str, dict]           = field(default_factory=dict)

    # ── Ops-serialization snippet cache ───────────────────────────────────────
    # Pre-computed JSON fragments per table/index so ops_to_bytes() only
    # re-serializes entries that actually changed (O(dirty) instead of O(all)).
    _t_snippets: dict[str, str] = field(default_factory=dict, repr=False,
                                        compare=False)
    _i_snippets: dict[str, str] = field(default_factory=dict, repr=False,
                                        compare=False)
    _ops_dirty_tables:  set     = field(default_factory=set,  repr=False,
                                        compare=False)
    _ops_dirty_indexes: set     = field(default_factory=set,  repr=False,
                                        compare=False)
    _ops_global_dirty:  bool    = field(default=True,         repr=False,
                                        compare=False)
    _ops_global_snippet: str    = field(default="",           repr=False,
                                        compare=False)
    # Stats are written to the ops blob (not the schema blob) so ANALYZE does
    # not trigger a schema page rewrite on every subsequent commit.
    _stats_dirty:   bool = field(default=True, repr=False, compare=False)
    _stats_snippet: str  = field(default="{}",  repr=False, compare=False)

    CATALOG_PAGE = 0

    def mark_table_ops_dirty(self, name: str) -> None:
        self._ops_dirty_tables.add(name)

    def mark_index_ops_dirty(self, name: str) -> None:
        self._ops_dirty_indexes.add(name)

    def mark_global_ops_dirty(self) -> None:
        self._ops_global_dirty = True

    def mark_stats_dirty(self) -> None:
        self._stats_dirty = True

    # ── Serialisation ─────────────────────────────────────────────────────────

    def schema_to_bytes(self) -> bytes:
        """Structural-only JSON: table/index definitions, views, triggers, meta.

        This blob changes only on DDL (CREATE/DROP TABLE/INDEX/VIEW/TRIGGER)
        and is therefore written to disk only when those operations occur —
        typically a tiny fraction of all commits.  ANALYZE stats are stored in
        the ops blob instead so they do not cause schema page rewrites.
        """
        return json.dumps({
            "tables": {
                n: {"schema": m.schema.to_dict(), "temporary": m.temporary,
                    "storage_type": m.storage_type}
                for n, m in self.tables.items() if not m.temporary
            },
            "indexes": {
                n: {"table_name": m.table_name, "columns": m.columns,
                    "unique": m.unique}
                for n, m in self.indexes.items()
            },
            "views":    self.views,
            "triggers": {
                n: {"table": m.table, "timing": m.timing, "event": m.event,
                    "update_cols": m.update_cols, "when_tokens": m.when_tokens,
                    "body_tokens": m.body_tokens}
                for n, m in self.triggers.items()
            },
            "meta": self.meta,
        }).encode()

    def ops_to_bytes(self) -> bytes:
        """Operational-state JSON: page counters and per-table/index runtime data.

        Uses a snippet cache to avoid O(n_tables) JSON re-encoding on every
        commit.  Only dirty entries (those whose ops fields actually changed)
        are re-serialized; all others reuse their cached JSON fragment.
        This keeps per-INSERT cost O(1) regardless of total table count.
        """
        # Re-encode only dirty table snippets
        for name in self._ops_dirty_tables:
            m = self.tables.get(name)
            if m is None or m.temporary:
                self._t_snippets.pop(name, None)
            else:
                self._t_snippets[name] = (
                    f'"{name}":{{\"root_page\":{m.root_page},'
                    f'\"next_page\":{m.next_page},\"next_key\":{m.next_key}}}'
                )
        self._ops_dirty_tables.clear()

        # Re-encode only dirty index snippets
        for name in self._ops_dirty_indexes:
            m = self.indexes.get(name)
            if m is None:
                self._i_snippets.pop(name, None)
            else:
                self._i_snippets[name] = (
                    f'"{name}":{{\"root_page\":{m.root_page},'
                    f'\"next_page\":{m.next_page}}}'
                )
        self._ops_dirty_indexes.clear()

        # Re-encode global state only when next_free_page / free_pages changed
        if self._ops_global_dirty:
            fp_bin = (struct.pack(f'>{len(self.free_pages)}I', *self.free_pages)
                      if self.free_pages else b'')
            fp_b64 = base64.b64encode(fp_bin).decode()
            self._ops_global_snippet = (
                f'"next_free_page":{self.next_free_page},'
                f'"free_pages_b64":"{fp_b64}"'
            )
            self._ops_global_dirty = False

        # Re-encode stats only when ANALYZE has run since the last flush
        if self._stats_dirty:
            self._stats_snippet = json.dumps(self.stats)
            self._stats_dirty = False

        table_part = ",".join(self._t_snippets.values())
        index_part = ",".join(self._i_snippets.values())
        return (
            f'{{{self._ops_global_snippet},'
            f'"stats":{self._stats_snippet},'
            f'"table_ops":{{{table_part}}},'
            f'"index_ops":{{{index_part}}}}}'
        ).encode()

    # ── Savepoint snapshots (lightweight, no JSON) ────────────────────────────

    def snap(self) -> dict:
        """Cheap in-memory snapshot for savepoints — avoids JSON serialization.

        Shallow-copies each TableMeta/IndexMeta so mutable operational fields
        (root_page, next_page, next_key) are captured at this instant.  Schema
        objects inside TableMeta are shared by reference; they are effectively
        immutable (DDL always creates new Schema/TableMeta rather than mutating
        the existing one), so sharing is safe.
        """
        return {
            "nfp":  self.next_free_page,
            "fp":   list(self.free_pages),
            "tbls": {n: TableMeta(m.schema, m.root_page, m.next_page,
                                  m.next_key, m.temporary, m.storage_type)
                     for n, m in self.tables.items()},
            "idxs": {n: IndexMeta(m.table_name, list(m.columns),
                                  m.root_page, m.next_page, m.unique)
                     for n, m in self.indexes.items()},
            "vws":  dict(self.views),
            "trgs": dict(self.triggers),
            "meta": {k: dict(v) for k, v in self.meta.items()},
        }

    def restore_snap(self, snap: dict) -> None:
        """Restore this catalog in-place to a previously taken snap()."""
        self.next_free_page = snap["nfp"]
        self.free_pages     = snap["fp"]
        self.tables.clear();   self.tables.update(snap["tbls"])
        self.indexes.clear();  self.indexes.update(snap["idxs"])
        self.views.clear();    self.views.update(snap["vws"])
        self.triggers.clear(); self.triggers.update(snap["trgs"])
        self.meta.clear();     self.meta.update(snap["meta"])
        # Invalidate snippet/dirty caches so ops_to_bytes rebuilds cleanly
        self._t_snippets.clear()
        self._i_snippets.clear()
        self._ops_dirty_tables.clear()
        self._ops_dirty_indexes.clear()
        self._ops_global_dirty = True
        self._stats_dirty      = True

    # ── Combined format (backward-compat load only) ────────────────────────────

    def to_bytes(self) -> bytes:
        """Combined JSON — retained for backward-compat load path only."""
        return json.dumps({
            "next_free_page": self.next_free_page,
            "free_pages":     self.free_pages,
            "tables": {
                n: {"schema": m.schema.to_dict(), "root_page": m.root_page,
                    "next_page": m.next_page, "next_key": m.next_key,
                    "storage_type": m.storage_type}
                for n, m in self.tables.items() if not m.temporary
            },
            "indexes": {
                n: {"table_name": m.table_name, "columns": m.columns,
                    "root_page": m.root_page, "next_page": m.next_page,
                    "unique": m.unique}
                for n, m in self.indexes.items()
            },
            "views": self.views,
            "stats": self.stats,
            "triggers": {
                n: {"table": m.table, "timing": m.timing, "event": m.event,
                    "update_cols": m.update_cols, "when_tokens": m.when_tokens,
                    "body_tokens": m.body_tokens}
                for n, m in self.triggers.items()
            },
            "meta": self.meta,
        }).encode()

    # ── Deserialisation ───────────────────────────────────────────────────────

    @classmethod
    def from_schema_and_ops_bytes(cls, schema_bytes: bytes,
                                  ops_bytes: bytes) -> "Catalog":
        """Reconstruct from the split schema + ops blobs (new on-disk format)."""
        raw_s = schema_bytes.rstrip(b"\x00")
        raw_o = ops_bytes.rstrip(b"\x00")
        d_s   = json.loads(raw_s.decode()) if raw_s else {}
        d_o   = json.loads(raw_o.decode()) if raw_o else {}

        table_ops = d_o.get("table_ops", {})
        index_ops = d_o.get("index_ops", {})

        tables: dict[str, TableMeta] = {}
        for n, t in d_s.get("tables", {}).items():
            ops = table_ops.get(n, {})
            tables[n] = TableMeta(
                Schema.from_dict(t["schema"]),
                root_page=ops.get("root_page", 0),
                next_page=ops.get("next_page", 0),
                next_key=ops.get("next_key",  1),
                temporary=t.get("temporary", False),
                storage_type=t.get("storage_type", "row"),
            )

        indexes: dict[str, IndexMeta] = {}
        for n, i in d_s.get("indexes", {}).items():
            ops = index_ops.get(n, {})
            indexes[n] = IndexMeta(
                table_name=i["table_name"],
                columns=i["columns"],
                root_page=ops.get("root_page", 0),
                next_page=ops.get("next_page", 0),
                unique=i.get("unique", False),
            )

        triggers = {
            n: TriggerMeta(t["table"], t["timing"], t["event"],
                           t.get("update_cols", []), t.get("when_tokens", []),
                           t.get("body_tokens", []))
            for n, t in d_s.get("triggers", {}).items()
        }

        # Stats live in the ops blob (new format).  Fall back to the schema blob
        # for databases written by older versions that stored stats in the schema.
        stats = d_o.get("stats") or d_s.get("stats", {})

        # Decode free-page list: new format uses compact binary (base64-encoded
        # packed uint32s); fall back to JSON array for older databases.
        if "free_pages_b64" in d_o:
            fp_bin = base64.b64decode(d_o["free_pages_b64"])
            n_fp   = len(fp_bin) // 4
            free_pages = list(struct.unpack(f'>{n_fp}I', fp_bin)) if n_fp else []
        else:
            free_pages = d_o.get("free_pages", [])

        cat = cls(
            tables=tables,
            indexes=indexes,
            views=d_s.get("views", {}),
            next_free_page=d_o.get("next_free_page", 1),
            free_pages=free_pages,
            stats=stats,
            triggers=triggers,
            meta=d_s.get("meta", {}),
        )
        # Initialise snippet cache and mark stats clean so the first ops flush
        # doesn't re-serialise unchanged stats.
        cat._stats_snippet = json.dumps(stats)
        cat._stats_dirty = False
        return cat

    @classmethod
    def from_bytes(cls, data: bytes) -> "Catalog":
        """Reconstruct from the old combined JSON blob (savepoints + old format)."""
        raw = data.rstrip(b"\x00")
        if not raw:
            return cls()
        d = json.loads(raw.decode())
        tables = {
            n: TableMeta(Schema.from_dict(t["schema"]), t["root_page"],
                         t["next_page"], t["next_key"],
                         storage_type=t.get("storage_type", "row"))
            for n, t in d.get("tables", {}).items()
        }
        indexes = {
            n: IndexMeta(i["table_name"],
                         i["columns"] if "columns" in i else [i["column_name"]],
                         i["root_page"], i["next_page"],
                         unique=i.get("unique", False))
            for n, i in d.get("indexes", {}).items()
        }
        triggers = {
            n: TriggerMeta(t["table"], t["timing"], t["event"],
                           t.get("update_cols", []), t.get("when_tokens", []),
                           t.get("body_tokens", []))
            for n, t in d.get("triggers", {}).items()
        }
        return cls(tables=tables, indexes=indexes,
                   views=d.get("views", {}),
                   next_free_page=d.get("next_free_page", 1),
                   free_pages=d.get("free_pages", []),
                   stats=d.get("stats", {}),
                   triggers=triggers,
                   meta=d.get("meta", {}))
