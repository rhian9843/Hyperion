# ── Storage constants ──────────────────────────────────────────────────────────

PAGE_SIZE = 4096

# ── Row cell format ────────────────────────────────────────────────────────────
# Every table B-tree leaf stores a fixed ROW_CELL_SIZE-byte value per row.
# Layout: [1: is_overflow][4: total_len][4: first_overflow_page][ROW_INLINE_CAP: data]
# When is_overflow=0 the row bytes (up to ROW_INLINE_CAP) are stored inline.
# When is_overflow=1 all data lives in a linked overflow page chain.

ROW_INLINE_CAP = 191           # max bytes that fit inline in a cell
ROW_CELL_SIZE  = 200           # 9-byte header + 191 bytes inline data

# ── Overflow pages ─────────────────────────────────────────────────────────────
# Layout: [1: PAGE_OVERFLOW][4: next_page][4: bytes_on_page][data...]

PAGE_OVERFLOW    = 0x02
OVERFLOW_HDR     = 9
OVERFLOW_DATA_SZ = PAGE_SIZE - OVERFLOW_HDR   # 4087 bytes of payload per page

# ── Column types ───────────────────────────────────────────────────────────────

INTEGER = "INTEGER"
REAL    = "REAL"
TEXT    = "TEXT"
BLOB    = "BLOB"

_FIXED_FMTS  = {INTEGER: "q", REAL: "d"}
_FIXED_SIZES = {INTEGER: 8,   REAL: 8}
DEFAULT_TEXT_SIZE = 255
