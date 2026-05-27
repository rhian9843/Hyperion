import struct
from dataclasses import dataclass, field
from typing import Any

from .constants import INTEGER, REAL, TEXT, BLOB, _FIXED_FMTS, _FIXED_SIZES, DEFAULT_TEXT_SIZE


@dataclass
class Column:
    name:            str
    type:            str
    size:            int       = DEFAULT_TEXT_SIZE
    nullable:        bool      = True
    unique:          bool      = False
    default:         str|None  = None
    check:           str|None  = None
    primary_key:     bool      = False
    autoincrement:   bool      = False
    generated_expr:  str|None  = None   # AS (expr) expression; None = regular column
    generated_stored: bool     = False  # True = STORED, False = VIRTUAL

    @property
    def is_generated(self) -> bool:
        return self.generated_expr is not None

    @property
    def fmt(self) -> str:
        # VIRTUAL generated columns are not stored in the row
        if self.is_generated and not self.generated_stored:
            return ""
        return _FIXED_FMTS.get(self.type, f"{self.size}s")

    @property
    def byte_size(self) -> int:
        if self.is_generated and not self.generated_stored:
            return 0
        return _FIXED_SIZES.get(self.type, self.size)


@dataclass
class ForeignKey:
    columns:     list[str]   # child column(s)
    ref_table:   str         # parent table name
    ref_columns: list[str]   # parent column(s)
    on_delete:   str = "RESTRICT"  # RESTRICT | CASCADE | SET NULL | NO ACTION
    on_update:   str = "RESTRICT"  # RESTRICT | CASCADE | SET NULL | NO ACTION


@dataclass
class Schema:
    name:                str
    columns:             list[Column]
    foreign_keys:        list[ForeignKey]  = field(default_factory=list)
    unique_constraints:  list[list[str]]   = field(default_factory=list)
    primary_key_columns: list[str]         = field(default_factory=list)

    @property
    def stored_columns(self) -> list["Column"]:
        """Columns that are physically stored in the row (excludes VIRTUAL generated)."""
        return [c for c in self.columns if not (c.is_generated and not c.generated_stored)]

    @property
    def row_format(self) -> str:
        return "=" + "".join(c.fmt for c in self.stored_columns)

    @property
    def null_bitmap_size(self) -> int:
        return (len(self.stored_columns) + 7) // 8

    @property
    def row_size(self) -> int:
        return self.null_bitmap_size + struct.calcsize(self.row_format)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "columns": [
                {"name": c.name, "type": c.type, "size": c.size,
                 "nullable": c.nullable, "unique": c.unique,
                 "default": c.default, "check": c.check,
                 "primary_key": c.primary_key, "autoincrement": c.autoincrement,
                 "generated_expr": c.generated_expr,
                 "generated_stored": c.generated_stored}
                for c in self.columns
            ],
            "foreign_keys": [
                {"columns": fk.columns, "ref_table": fk.ref_table,
                 "ref_columns": fk.ref_columns, "on_delete": fk.on_delete,
                 "on_update": fk.on_update}
                for fk in self.foreign_keys
            ],
            "unique_constraints": self.unique_constraints,
            "primary_key_columns": self.primary_key_columns,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Schema":
        cols = [
            Column(c["name"], c["type"], c.get("size", DEFAULT_TEXT_SIZE),
                   c.get("nullable", True), c.get("unique", False),
                   c.get("default"), c.get("check"),
                   c.get("primary_key", False), c.get("autoincrement", False),
                   c.get("generated_expr"), c.get("generated_stored", False))
            for c in d["columns"]
        ]
        fks = [
            ForeignKey(f["columns"], f["ref_table"], f["ref_columns"],
                       f.get("on_delete", "RESTRICT"), f.get("on_update", "RESTRICT"))
            for f in d.get("foreign_keys", [])
        ]
        ucs  = d.get("unique_constraints", [])
        pkcs = d.get("primary_key_columns", [])
        return cls(name=d["name"], columns=cols, foreign_keys=fks,
                   unique_constraints=ucs, primary_key_columns=pkcs)


def serialize_row(schema: Schema, row: dict[str, Any]) -> bytes:
    from .expr import eval_expr
    from .constants import INTEGER, REAL
    bitmap = bytearray(schema.null_bitmap_size)
    packed = []
    stored = schema.stored_columns
    # Build a typed copy of the row so eval_expr sees numeric types, not strings
    typed_row: dict[str, Any] = {}
    for c in stored:
        if c.is_generated:
            continue
        v = row.get(c.name)
        if v is not None:
            try:
                if c.type == INTEGER:
                    v = int(v)
                elif c.type == REAL:
                    v = float(v)
            except (ValueError, TypeError):
                pass
        typed_row[c.name] = v
    for i, col in enumerate(stored):
        # For STORED generated columns, compute the value from the expression
        if col.is_generated and col.generated_stored:
            val = eval_expr(col.generated_expr, typed_row)  # type: ignore[arg-type]
        else:
            val = row.get(col.name)
        if val is None:
            if not col.nullable:
                raise RuntimeError(f"Column '{col.name}' is NOT NULL")
            bitmap[i // 8] |= 1 << (i % 8)
            if col.type == INTEGER: packed.append(0)
            elif col.type == REAL:  packed.append(0.0)
            else:                   packed.append(b"")
        else:
            if col.type == INTEGER:
                packed.append(int(val))
            elif col.type == REAL:
                packed.append(float(val))
            elif col.type == BLOB:
                b = val if isinstance(val, (bytes, bytearray)) else str(val).encode()
                if len(b) > col.size:
                    raise RuntimeError(
                        f"Value for '{col.name}' is {len(b)} bytes, "
                        f"exceeds BLOB({col.size})"
                    )
                packed.append(bytes(b))
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
    from .expr import eval_expr
    bm   = data[:schema.null_bitmap_size]
    vals = struct.unpack(schema.row_format, data[schema.null_bitmap_size:])
    row: dict[str, Any] = {}
    for i, (col, val) in enumerate(zip(schema.stored_columns, vals)):
        if bm[i // 8] & (1 << (i % 8)):
            row[col.name] = None
        elif col.type == BLOB:
            row[col.name] = val.rstrip(b"\x00")
        elif col.type == TEXT:
            row[col.name] = val.rstrip(b"\x00").decode()
        else:
            row[col.name] = val
    # Inject VIRTUAL generated column values computed from the stored row
    for col in schema.columns:
        if col.is_generated and not col.generated_stored:
            try:
                row[col.name] = eval_expr(col.generated_expr, row)  # type: ignore[arg-type]
            except Exception:
                row[col.name] = None
    return row
