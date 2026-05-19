# ── Storage constants ──────────────────────────────────────────────────────────

PAGE_SIZE = 4096

# ── Column types ───────────────────────────────────────────────────────────────

INTEGER = "INTEGER"
REAL    = "REAL"
TEXT    = "TEXT"

_FIXED_FMTS  = {INTEGER: "q", REAL: "d"}
_FIXED_SIZES = {INTEGER: 8,   REAL: 8}
DEFAULT_TEXT_SIZE = 255
