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

    def to_bytes(self) -> bytes:
        """Return raw JSON bytes (no padding). _flush_catalog handles page splitting."""
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

    @classmethod
    def from_bytes(cls, data: bytes) -> "Catalog":
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
