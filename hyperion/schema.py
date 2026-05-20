import struct
from dataclasses import dataclass, field
from typing import Any

from .constants import INTEGER, REAL, TEXT, _FIXED_FMTS, _FIXED_SIZES, DEFAULT_TEXT_SIZE


@dataclass
class Column:
    name:     str
    type:     str
    size:     int       = DEFAULT_TEXT_SIZE
    nullable: bool      = True
    unique:   bool      = False
    default:  str|None  = None
    check:    str|None  = None

    @property
    def fmt(self) -> str:
        return _FIXED_FMTS.get(self.type, f"{self.size}s")

    @property
    def byte_size(self) -> int:
        return _FIXED_SIZES.get(self.type, self.size)


@dataclass
class ForeignKey:
    columns:     list[str]   # child column(s)
    ref_table:   str         # parent table name
    ref_columns: list[str]   # parent column(s)


@dataclass
class Schema:
    name:         str
    columns:      list[Column]
    foreign_keys: list[ForeignKey] = field(default_factory=list)

    @property
    def row_format(self) -> str:
        return "=" + "".join(c.fmt for c in self.columns)

    @property
    def null_bitmap_size(self) -> int:
        return (len(self.columns) + 7) // 8

    @property
    def row_size(self) -> int:
        return self.null_bitmap_size + struct.calcsize(self.row_format)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "columns": [
                {"name": c.name, "type": c.type, "size": c.size,
                 "nullable": c.nullable, "unique": c.unique,
                 "default": c.default, "check": c.check}
                for c in self.columns
            ],
            "foreign_keys": [
                {"columns": fk.columns, "ref_table": fk.ref_table,
                 "ref_columns": fk.ref_columns}
                for fk in self.foreign_keys
            ],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Schema":
        cols = [
            Column(c["name"], c["type"], c.get("size", DEFAULT_TEXT_SIZE),
                   c.get("nullable", True), c.get("unique", False),
                   c.get("default"), c.get("check"))
            for c in d["columns"]
        ]
        fks = [
            ForeignKey(f["columns"], f["ref_table"], f["ref_columns"])
            for f in d.get("foreign_keys", [])
        ]
        return cls(name=d["name"], columns=cols, foreign_keys=fks)


def serialize_row(schema: Schema, row: dict[str, Any]) -> bytes:
    bitmap = bytearray(schema.null_bitmap_size)
    packed = []
    for i, col in enumerate(schema.columns):
        val = row.get(col.name)
        if val is None:
            if not col.nullable:
                raise RuntimeError(f"Column '{col.name}' is NOT NULL")
            bitmap[i // 8] |= 1 << (i % 8)
            if col.type == INTEGER: packed.append(0)
            elif col.type == REAL:  packed.append(0.0)
            else:                   packed.append(b"")
        else:
            if col.type == INTEGER:   packed.append(int(val))
            elif col.type == REAL:    packed.append(float(val))
            else:
                encoded = str(val).encode()
                if len(encoded) > col.size:
                    raise RuntimeError(
                        f"Value for '{col.name}' is {len(encoded)} bytes, "
                        f"exceeds VARCHAR({col.size})"
                    )
                packed.append(encoded)
    try:
        return bytes(bitmap) + struct.pack(schema.row_format, *packed)
    except struct.error as e:
        raise RuntimeError(str(e)) from e


def deserialize_row(schema: Schema, data: bytes) -> dict[str, Any]:
    bm   = data[:schema.null_bitmap_size]
    vals = struct.unpack(schema.row_format, data[schema.null_bitmap_size:])
    row: dict[str, Any] = {}
    for i, (col, val) in enumerate(zip(schema.columns, vals)):
        if bm[i // 8] & (1 << (i % 8)):
            row[col.name] = None
        elif col.type == TEXT:
            row[col.name] = val.rstrip(b"\x00").decode()
        else:
            row[col.name] = val
    return row
