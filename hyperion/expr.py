"""SQL expression evaluator: arithmetic, CAST, COALESCE, NULLIF, IFNULL, CASE WHEN."""
import re
from datetime import datetime
from typing import Any

_ARITH_OPS = frozenset({"+", "-", "*", "/", "%", "||"})
_COMP_OPS  = frozenset({"=", "!=", "<", ">", "<=", ">="})

# Detect expressions that need evaluation (not a bare column name / simple literal)
_IS_EXPR_RE = re.compile(
    r'\|\|'                                        # string concat
    r'|[+\-*/%]'                                   # arithmetic
    r'|^\s*(CASE|COALESCE|NULLIF|IFNULL|CAST)\b'   # known keywords / functions
    r'|\b(TRUE|FALSE|CURRENT_TIMESTAMP|CURRENT_DATE|CURRENT_TIME)\b'
    r'|\w+\s*\(',                                  # any function call
    re.IGNORECASE | re.MULTILINE,
)

# Date/time strings look like arithmetic (due to '-') but are plain values.
_DATE_LITERAL_RE = re.compile(
    r'^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?Z?)?$'
)

def is_expr(s: str) -> bool:
    """Return True when s needs expression evaluation rather than a plain column lookup."""
    if _DATE_LITERAL_RE.match(s.strip()):
        return False
    return bool(_IS_EXPR_RE.search(s))


# ── Tokenizer ──────────────────────────────────────────────────────────────────

_TOK_RE = re.compile(
    r"'(?:[^']|'')*'"      # string literal
    r"|\|\|"               # string concat operator
    r"|[+\-*/%(),]"        # arithmetic operators, parentheses, comma
    r"|[<>!]=?"            # comparison operators
    r"|\d+\.\d+"           # float literal
    r"|\d+"                # integer literal
    r"|\w+(?:\.\w+)?"      # identifier (possibly table-qualified: t.col)
)

def _tokenize_expr(s: str) -> list[str]:
    return _TOK_RE.findall(s.strip())


# ── Atom resolver ──────────────────────────────────────────────────────────────

def _resolve_atom(token: str, row: dict) -> Any:
    """Resolve a single token to its Python value."""
    t = token.strip()
    upper = t.upper()
    if upper == "NULL":
        return None
    if upper == "TRUE":
        return 1
    if upper == "FALSE":
        return 0
    if upper == "CURRENT_TIMESTAMP":
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if upper == "CURRENT_DATE":
        return datetime.now().strftime("%Y-%m-%d")
    if upper == "CURRENT_TIME":
        return datetime.now().strftime("%H:%M:%S")
    if t.startswith("'") and t.endswith("'"):
        return t[1:-1].replace("''", "'")
    if t in row:
        return row[t]
    if "." in t:
        bare = t.split(".", 1)[1]
        if bare in row:
            return row[bare]
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        pass
    return t  # unquoted string literal (Hyperion allows bare strings in expressions)


# ── Recursive-descent arithmetic parser ───────────────────────────────────────

def _parse_add(toks: list[str], pos: int, row: dict) -> tuple[Any, int]:
    val, pos = _parse_mul(toks, pos, row)
    while pos < len(toks) and toks[pos] in ("+", "-", "||"):
        op = toks[pos]; pos += 1
        right, pos = _parse_mul(toks, pos, row)
        if val is None or right is None:
            val = None
        elif op == "+":
            val = val + right
        elif op == "-":
            val = val - right
        else:  # "||"
            val = str(val) + str(right)
    return val, pos


def _parse_mul(toks: list[str], pos: int, row: dict) -> tuple[Any, int]:
    val, pos = _parse_unary(toks, pos, row)
    while pos < len(toks) and toks[pos] in ("*", "/", "%"):
        op = toks[pos]; pos += 1
        right, pos = _parse_unary(toks, pos, row)
        if val is None or right is None:
            val = None
        elif op == "*":
            val = val * right
        elif op == "/" and right != 0:
            val = (val / right) if isinstance(val, float) or isinstance(right, float) \
                  else (val // right)
        elif op == "%":
            val = val % right
        else:
            val = None  # division by zero
    return val, pos


def _parse_unary(toks: list[str], pos: int, row: dict) -> tuple[Any, int]:
    if pos < len(toks) and toks[pos] == "-":
        val, pos = _parse_primary(toks, pos + 1, row)
        return ((-val) if val is not None else None), pos
    if pos < len(toks) and toks[pos] == "+":
        return _parse_primary(toks, pos + 1, row)
    return _parse_primary(toks, pos, row)


def _parse_primary(toks: list[str], pos: int, row: dict) -> tuple[Any, int]:
    if pos >= len(toks):
        return None, pos
    tok = toks[pos]

    if tok == "(":
        val, pos = _parse_add(toks, pos + 1, row)
        if pos < len(toks) and toks[pos] == ")":
            pos += 1
        return val, pos

    if tok.upper() == "CASE":
        return _eval_case_tokens(toks, pos, row)

    # Function call: identifier immediately followed by (
    if pos + 1 < len(toks) and toks[pos + 1] == "(":
        fname = tok.upper()
        depth = 0
        j = pos + 1
        args_start = pos + 2
        while j < len(toks):
            if toks[j] == "(":
                depth += 1
            elif toks[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        args_str = " ".join(toks[args_start:j])
        val = _eval_func(fname, args_str, row)
        return val, j + 1

    return _resolve_atom(tok, row), pos + 1


# ── Function evaluator ─────────────────────────────────────────────────────────

def _split_args(args_str: str) -> list[str]:
    """Split comma-separated function arguments respecting nested parentheses."""
    result: list[str] = []
    current: list[str] = []
    depth = 0
    for ch in args_str:
        if ch == "(":
            depth += 1; current.append(ch)
        elif ch == ")":
            depth -= 1; current.append(ch)
        elif ch == "," and depth == 0:
            result.append("".join(current).strip()); current = []
        else:
            current.append(ch)
    if current:
        result.append("".join(current).strip())
    return [a for a in result if a]


def _eval_func(fname: str, args_str: str, row: dict) -> Any:
    if fname == "CAST":
        m = re.match(r'(.+?)\s+AS\s+(\w+)\s*$', args_str, re.IGNORECASE)
        if not m:
            return eval_expr(args_str, row)
        val = eval_expr(m.group(1).strip(), row)
        typ = m.group(2).upper()
        if val is None:
            return None
        if typ in ("INTEGER", "INT", "BIGINT", "SMALLINT", "TINYINT"):
            try:
                return int(float(str(val)))
            except (ValueError, TypeError):
                return None
        if typ in ("REAL", "FLOAT", "DOUBLE"):
            try:
                return float(str(val))
            except (ValueError, TypeError):
                return None
        return str(val)  # TEXT / VARCHAR / anything else

    args = [eval_expr(a, row) for a in _split_args(args_str)]

    if fname == "COALESCE":
        return next((v for v in args if v is not None), None)

    if fname in ("IFNULL", "NVL"):
        if not args:
            return None
        return args[0] if args[0] is not None else (args[1] if len(args) > 1 else None)

    if fname == "NULLIF":
        if len(args) >= 2 and args[0] == args[1]:
            return None
        return args[0] if args else None

    return None  # unknown function → NULL


# ── CASE WHEN evaluator ────────────────────────────────────────────────────────

def _eval_case_tokens(toks: list[str], pos: int, row: dict) -> tuple[Any, int]:
    """Evaluate CASE [WHEN cond THEN val]... [ELSE val] END starting at pos.
    Returns (result_value, pos_after_END).
    """
    pos += 1  # skip CASE
    result: Any = None
    matched = False

    while pos < len(toks):
        kw = toks[pos].upper()
        if kw == "WHEN":
            pos += 1
            cond_toks: list[str] = []
            while pos < len(toks) and toks[pos].upper() != "THEN":
                cond_toks.append(toks[pos]); pos += 1
            pos += 1  # skip THEN
            result_toks: list[str] = []
            while pos < len(toks) and toks[pos].upper() not in ("WHEN", "ELSE", "END"):
                result_toks.append(toks[pos]); pos += 1
            if not matched and _eval_condition_tokens(cond_toks, row):
                result = eval_expr(" ".join(result_toks), row)
                matched = True
        elif kw == "ELSE":
            pos += 1
            else_toks: list[str] = []
            while pos < len(toks) and toks[pos].upper() != "END":
                else_toks.append(toks[pos]); pos += 1
            if not matched:
                result = eval_expr(" ".join(else_toks), row)
        elif kw == "END":
            pos += 1
            break
        else:
            pos += 1

    return result, pos


def _eval_condition_tokens(cond_toks: list[str], row: dict) -> bool:
    """Evaluate a simple comparison condition inside CASE WHEN."""
    for i, t in enumerate(cond_toks):
        if t in _COMP_OPS or (t == "!" and i + 1 < len(cond_toks) and cond_toks[i+1] == "="):
            lv = eval_expr(" ".join(cond_toks[:i]), row)
            rv = eval_expr(" ".join(cond_toks[i+1:]), row)
            if lv is None or rv is None:
                return False
            if isinstance(lv, (int, float)) and not isinstance(rv, (int, float)):
                try:
                    rv = type(lv)(str(rv))
                except (ValueError, TypeError):
                    return False
            match t:
                case "=":  return lv == rv
                case "!=": return lv != rv
                case "<":  return lv < rv
                case ">":  return lv > rv
                case "<=": return lv <= rv
                case ">=": return lv >= rv
    val = eval_expr(" ".join(cond_toks), row)
    return bool(val) if val is not None else False


# ── Main entry point ──────────────────────────────────────────────────────────

def eval_expr(expr: str, row: dict) -> Any:
    """Evaluate a SQL expression string against a row dict."""
    expr = expr.strip()
    if not expr:
        return None
    toks = _tokenize_expr(expr)
    if not toks:
        return None
    val, _ = _parse_add(toks, 0, row)
    return val
