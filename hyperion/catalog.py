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
    schema:    Schema
    root_page: int
    next_page: int
    next_key:  int
    temporary: bool = False


@dataclass
class IndexMeta:
    table_name: str
    columns:    list[str]
    root_page:  int
    next_page:  int

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

    CATALOG_PAGE = 0

    # ── Serialisation ─────────────────────────────────────────────────────────

    def schema_to_bytes(self) -> bytes:
        """Structural-only JSON: table/index definitions, views, triggers, stats.

        This blob changes only on DDL (CREATE/DROP TABLE/INDEX/VIEW/TRIGGER,
        ANALYZE) and is therefore written to disk only when those operations
        occur — typically a tiny fraction of all commits.
        """
        return json.dumps({
            "tables": {
                n: {"schema": m.schema.to_dict(), "temporary": m.temporary}
                for n, m in self.tables.items() if not m.temporary
            },
            "indexes": {
                n: {"table_name": m.table_name, "columns": m.columns}
                for n, m in self.indexes.items()
            },
            "views":    self.views,
            "stats":    self.stats,
            "triggers": {
                n: {"table": m.table, "timing": m.timing, "event": m.event,
                    "update_cols": m.update_cols, "when_tokens": m.when_tokens,
                    "body_tokens": m.body_tokens}
                for n, m in self.triggers.items()
            },
        }).encode()

    def ops_to_bytes(self) -> bytes:
        """Operational-state JSON: page counters and per-table/index runtime data.

        This blob changes on every write (INSERT increments next_key; B-tree
        splits update root/next page).  It is intentionally tiny — O(n_tables)
        integers, independent of schema complexity — so writing it on every
        commit is cheap regardless of how many columns each table has.
        """
        return json.dumps({
            "next_free_page": self.next_free_page,
            "free_pages":     self.free_pages,
            "table_ops": {
                n: {"root_page": m.root_page,
                    "next_page": m.next_page,
                    "next_key":  m.next_key}
                for n, m in self.tables.items() if not m.temporary
            },
            "index_ops": {
                n: {"root_page": m.root_page, "next_page": m.next_page}
                for n, m in self.indexes.items()
            },
        }).encode()

    # ── Combined format (used by savepoints and backward-compat load) ─────────

    def to_bytes(self) -> bytes:
        """Combined JSON — used only for in-memory savepoint snapshots."""
        return json.dumps({
            "next_free_page": self.next_free_page,
            "free_pages":     self.free_pages,
            "tables": {
                n: {"schema": m.schema.to_dict(), "root_page": m.root_page,
                    "next_page": m.next_page, "next_key": m.next_key}
                for n, m in self.tables.items() if not m.temporary
            },
            "indexes": {
                n: {"table_name": m.table_name, "columns": m.columns,
                    "root_page": m.root_page, "next_page": m.next_page}
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
            )

        indexes: dict[str, IndexMeta] = {}
        for n, i in d_s.get("indexes", {}).items():
            ops = index_ops.get(n, {})
            indexes[n] = IndexMeta(
                table_name=i["table_name"],
                columns=i["columns"],
                root_page=ops.get("root_page", 0),
                next_page=ops.get("next_page", 0),
            )

        triggers = {
            n: TriggerMeta(t["table"], t["timing"], t["event"],
                           t.get("update_cols", []), t.get("when_tokens", []),
                           t.get("body_tokens", []))
            for n, t in d_s.get("triggers", {}).items()
        }

        return cls(
            tables=tables,
            indexes=indexes,
            views=d_s.get("views", {}),
            next_free_page=d_o.get("next_free_page", 1),
            free_pages=d_o.get("free_pages", []),
            stats=d_s.get("stats", {}),
            triggers=triggers,
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> "Catalog":
        """Reconstruct from the old combined JSON blob (savepoints + old format)."""
        raw = data.rstrip(b"\x00")
        if not raw:
            return cls()
        d = json.loads(raw.decode())
        tables = {
            n: TableMeta(Schema.from_dict(t["schema"]), t["root_page"],
                         t["next_page"], t["next_key"])
            for n, t in d.get("tables", {}).items()
        }
        indexes = {
            n: IndexMeta(i["table_name"],
                         i["columns"] if "columns" in i else [i["column_name"]],
                         i["root_page"], i["next_page"])
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
                   triggers=triggers)
