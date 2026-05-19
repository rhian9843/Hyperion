import json
import struct
from dataclasses import dataclass, field

from .constants import PAGE_SIZE
from .schema import Schema


@dataclass
class TableMeta:
    schema:    Schema
    root_page: int
    next_page: int
    next_key:  int


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
    tables:         dict[str, TableMeta]  = field(default_factory=dict)
    indexes:        dict[str, IndexMeta]  = field(default_factory=dict)
    next_free_page: int                   = 1   # page 0 = catalog
    free_pages:     list[int]             = field(default_factory=list)

    CATALOG_PAGE = 0

    def to_bytes(self) -> bytes:
        """Return raw JSON bytes (no padding). _flush_catalog handles page splitting."""
        return json.dumps({
            "next_free_page": self.next_free_page,
            "free_pages":     self.free_pages,
            "tables": {
                n: {"schema": m.schema.to_dict(), "root_page": m.root_page,
                    "next_page": m.next_page, "next_key": m.next_key}
                for n, m in self.tables.items()
            },
            "indexes": {
                n: {"table_name": m.table_name, "columns": m.columns,
                    "root_page": m.root_page, "next_page": m.next_page}
                for n, m in self.indexes.items()
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
        return cls(tables=tables, indexes=indexes,
                   next_free_page=d.get("next_free_page", 1),
                   free_pages=d.get("free_pages", []))
