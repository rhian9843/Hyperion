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
    """Serialize a row to variable-length bytes (TLV format).

    Format: [null_bitmap] then for each stored non-null column:
      INTEGER/REAL: 8 bytes (struct 'q' or 'd')
      TEXT/BLOB/other: [uint32 length][length bytes]
    NULL columns contribute 0 bytes (bit set in null_bitmap).
    """
    from .expr import eval_expr
    from .constants import INTEGER, REAL, _FIXED_FMTS
    stored = schema.stored_columns
    bitmap = bytearray(schema.null_bitmap_size)
    parts: list[bytes] = []

    # Build typed copy so eval_expr sees numeric types
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
        if col.is_generated and col.generated_stored:
            val = eval_expr(col.generated_expr, typed_row)  # type: ignore[arg-type]
        else:
            val = row.get(col.name)

        if val is None:
            if not col.nullable:
                raise RuntimeError(f"Column '{col.name}' is NOT NULL")
            bitmap[i // 8] |= 1 << (i % 8)
            # NULL columns occupy no bytes in the payload
        elif col.type == INTEGER:
            try:
                parts.append(struct.pack("q", int(val)))
            except struct.error as e:
                raise RuntimeError(str(e)) from e
        elif col.type == REAL:
            parts.append(struct.pack("d", float(val)))
        else:
            # TEXT, BLOB, VARCHAR, and any other type: length-prefixed bytes
            if isinstance(val, (bytes, bytearray)):
                b = bytes(val)
            elif col.type == BLOB:
                s = str(val)
                if s.startswith("X'") and s.endswith("'"):
                    b = bytes.fromhex(s[2:-1])
                else:
                    b = s.encode()
            else:
                b = str(val).encode()
            # Enforce explicitly declared size limits (smaller than the default).
            # TEXT / VARCHAR without an explicit size allow unlimited storage.
            if col.size < DEFAULT_TEXT_SIZE and len(b) > col.size:
                raise RuntimeError(
                    f"Value for '{col.name}' is {len(b)} bytes, "
                    f"exceeds {col.type}({col.size})"
                )
            parts.append(struct.pack("I", len(b)))
            parts.append(b)

    return bytes(bitmap) + b"".join(parts)


def deserialize_row(schema: Schema, data: bytes) -> dict[str, Any]:
    """Deserialize variable-length row bytes into a column dict."""
    from .expr import eval_expr
    from .constants import INTEGER, REAL, TEXT, BLOB
    stored  = schema.stored_columns
    bm_size = schema.null_bitmap_size
    bm      = data[:bm_size]
    offset  = bm_size
    row: dict[str, Any] = {}

    for i, col in enumerate(stored):
        if bm[i // 8] & (1 << (i % 8)):
            row[col.name] = None
        elif col.type == INTEGER:
            row[col.name] = struct.unpack_from("q", data, offset)[0]
            offset += 8
        elif col.type == REAL:
            row[col.name] = struct.unpack_from("d", data, offset)[0]
            offset += 8
        else:
            # TEXT, BLOB, VARCHAR, etc.
            length = struct.unpack_from("I", data, offset)[0]
            offset += 4
            raw = data[offset: offset + length]
            offset += length
            if col.type == BLOB:
                row[col.name] = bytes(raw)
            else:
                row[col.name] = raw.decode()

    # Inject VIRTUAL generated column values
    for col in schema.columns:
        if col.is_generated and not col.generated_stored:
            try:
                row[col.name] = eval_expr(col.generated_expr, row)  # type: ignore[arg-type]
            except Exception:
                row[col.name] = None

    return row
