import re

from .constants import INTEGER, REAL, TEXT, BLOB, DEFAULT_TEXT_SIZE
from .schema import Column, ForeignKey, Schema
from .where import WhereClause


class ParseError(ValueError):
    pass


_TOKEN_RE = re.compile(r"'(?:[^']|'')*'|\"[^\"]*\"|\w+\([^)]*\)|[(),;*]|[^\s(),;*]+")


def _unquote_token(t: str) -> str:
    """Strip outer single quotes from a SQL string-literal token."""
    if t.startswith("'") and t.endswith("'") and len(t) >= 2:
        return t[1:-1].replace("''", "'")
    return t

# Aggregate function detection
_AGG_RE = re.compile(r"^(COUNT|MIN|MAX|SUM|AVG)\(([^)]*)\)$", re.IGNORECASE)

# Keywords that cannot be bare table aliases
_ALIAS_BLOCKLIST = frozenset({
    "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "NATURAL", "JOIN", "ON", "AS",
    "WHERE", "GROUP", "ORDER", "LIMIT", "HAVING",
    "AND", "OR", "NOT", "IN", "IS", "LIKE", "SET", "FROM",
})


def _parse_table_alias(tokens: list[str], pos: int, table: str) -> tuple[str, int]:
    """Consume an optional [AS] alias after a table name.  Returns (alias, new_pos)."""
    if pos < len(tokens) and tokens[pos].upper() == "AS":
        pos += 1
        return tokens[pos], pos + 1
    if (pos < len(tokens)
            and tokens[pos] not in (",", "(", ")", ";", "=")
            and tokens[pos].upper() not in _ALIAS_BLOCKLIST):
        return tokens[pos], pos + 1
    return table, pos


def _parse_agg(col: str) -> tuple[str, str] | None:
    """If col is an aggregate call like MIN(id), return (FUNC_UPPER, arg). Else None."""
    m = _AGG_RE.match(col)
    return (m.group(1).upper(), m.group(2).strip()) if m else None


def _tokenize(sql: str) -> list[str]:
    tokens = []
    for t in _TOKEN_RE.findall(sql):
        if t.startswith('"') and t.endswith('"'):
            tokens.append(t[1:-1])   # double-quoted identifiers: strip quotes
        else:
            tokens.append(t)         # keep single-quoted strings as-is for expression building
    return tokens


def _parse_col_type(token: str) -> tuple[str, int]:
    u = token.upper()
    # Integer types and aliases
    if u in (INTEGER, "INT", "TINYINT", "SMALLINT", "MEDIUMINT", "BIGINT",
             "BOOLEAN", "BOOL"):
        return INTEGER, 8
    # Real types and aliases
    if u in (REAL, "FLOAT", "DOUBLE", "NUMERIC", "DECIMAL"):
        return REAL, 8
    # Text types
    if u in (TEXT, "VARCHAR", "NVARCHAR"):
        return TEXT, DEFAULT_TEXT_SIZE
    # Date/time types stored as ISO-8601 text
    if u == "DATE":
        return TEXT, 10
    if u in ("DATETIME", "TIMESTAMP"):
        return TEXT, 26
    # Binary types
    if u in (BLOB, "BYTES", "BINARY", "VARBINARY"):
        return BLOB, DEFAULT_TEXT_SIZE
    # Parameterized types
    m = re.fullmatch(r"VARCHAR\((\d+)\)", u)
    if m:             return TEXT,    int(m.group(1))
    m = re.fullmatch(r"NVARCHAR\((\d+)\)", u)
    if m:             return TEXT,    int(m.group(1))
    m = re.fullmatch(r"CHAR\((\d+)\)", u)
    if m:             return TEXT,    int(m.group(1))
    m = re.fullmatch(r"BLOB\((\d+)\)", u)
    if m:             return BLOB,    int(m.group(1))
    m = re.fullmatch(r"VARBINARY\((\d+)\)", u)
    if m:             return BLOB,    int(m.group(1))
    m = re.fullmatch(r"DECIMAL\([\d,\s]+\)", u)
    if m:             return REAL,    8
    m = re.fullmatch(r"NUMERIC\([\d,\s]+\)", u)
    if m:             return REAL,    8
    raise ParseError(f"Unknown column type: '{token}'")


def _extract_paren_tokens(tokens: list[str], pos: int) -> tuple[list[str], int]:
    """Given tokens[pos] == '(', extract inner tokens up to matching ')'.
    Returns (inner_tokens, pos_after_closing_paren).
    """
    if pos >= len(tokens) or tokens[pos] != "(":
        raise ParseError("Expected (")
    pos += 1
    depth = 1
    inner: list[str] = []
    while pos < len(tokens) and depth > 0:
        tok = tokens[pos]; pos += 1
        if tok == "(":
            depth += 1; inner.append(tok)
        elif tok == ")":
            depth -= 1
            if depth > 0: inner.append(tok)
        else:
            inner.append(tok)
    if depth != 0:
        raise ParseError("Unmatched ( in subquery")
    return inner, pos


def _parse_one_condition(tokens: list[str], pos: int) -> tuple["WhereClause", int]:
    """Parse a single condition starting at pos.
    Supports: col OP val, col IN (...), col NOT IN (...),
              EXISTS (SELECT ...), NOT EXISTS (SELECT ...),
              col OP (SELECT ...) scalar subquery.
    """
    if pos >= len(tokens):
        raise ParseError("Incomplete WHERE clause")

    # EXISTS (SELECT ...)
    if tokens[pos].upper() == "EXISTS":
        if pos + 1 >= len(tokens) or tokens[pos + 1] != "(":
            raise ParseError("Expected ( after EXISTS")
        inner, new_pos = _extract_paren_tokens(tokens, pos + 1)
        return WhereClause(col="", op="EXISTS", val="",
                           subquery_ast=_parse_tokens(inner)), new_pos

    # NOT EXISTS (SELECT ...) or NOT IN (...)
    if tokens[pos].upper() == "NOT":
        if pos + 1 >= len(tokens):
            raise ParseError("Expected EXISTS or IN after NOT")
        next_kw = tokens[pos + 1].upper()
        if next_kw == "EXISTS":
            if pos + 2 >= len(tokens) or tokens[pos + 2] != "(":
                raise ParseError("Expected ( after NOT EXISTS")
            inner, new_pos = _extract_paren_tokens(tokens, pos + 2)
            return WhereClause(col="", op="NOT EXISTS", val="",
                               subquery_ast=_parse_tokens(inner)), new_pos
        raise ParseError(f"Expected EXISTS after NOT, got '{tokens[pos + 1]}'")

    if pos + 1 >= len(tokens):
        raise ParseError("Incomplete WHERE clause")

    # CASE WHEN ... END on the left side of a comparison
    if tokens[pos].upper() == "CASE":
        case_parts: list[str] = []
        j = pos
        depth = 0
        while j < len(tokens):
            if tokens[j].upper() == "CASE":
                depth += 1
            elif tokens[j].upper() == "END":
                depth -= 1
                case_parts.append(tokens[j]); j += 1
                if depth == 0:
                    break
                continue
            case_parts.append(tokens[j]); j += 1
        if j >= len(tokens):
            raise ParseError("Incomplete WHERE clause after CASE expression")
        op = tokens[j].upper()
        if op not in {"=", "!=", "<", ">", "<=", ">=", "LIKE", "GLOB"}:
            raise ParseError(f"Unexpected operator after CASE: '{op}'")
        if j + 1 >= len(tokens):
            raise ParseError("Incomplete WHERE clause")
        return WhereClause(col=" ".join(case_parts), op=op,
                           val=_unquote_token(tokens[j + 1])), j + 2

    # Arithmetic / concat expression on the left side: col * factor > val, a || b = val
    _EXPR_OPS = {"+", "-", "*", "/", "%", "||"}
    if (pos + 1 < len(tokens) and tokens[pos + 1] in _EXPR_OPS):
        expr_parts = [tokens[pos]]
        j = pos + 1
        while j < len(tokens) and tokens[j] in _EXPR_OPS:
            expr_parts.append(tokens[j]); j += 1
            if j < len(tokens):
                expr_parts.append(tokens[j]); j += 1
        if j >= len(tokens):
            raise ParseError("Incomplete WHERE expression")
        op = tokens[j].upper()
        if op not in {"=", "!=", "<", ">", "<=", ">=", "LIKE", "GLOB"}:
            raise ParseError(f"Unknown operator: '{op}'")
        if j + 1 >= len(tokens):
            raise ParseError("Incomplete WHERE clause")
        return WhereClause(col=" ".join(expr_parts), op=op,
                           val=_unquote_token(tokens[j + 1])), j + 2

    col = tokens[pos]
    op  = tokens[pos + 1].upper()

    if op == "IS":
        if pos + 2 < len(tokens) and tokens[pos + 2].upper() == "NULL":
            return WhereClause(col=col, op="IS NULL", val=""), pos + 3
        if (pos + 3 < len(tokens)
                and tokens[pos + 2].upper() == "NOT"
                and tokens[pos + 3].upper() == "NULL"):
            return WhereClause(col=col, op="IS NOT NULL", val=""), pos + 4
        raise ParseError("Expected NULL or NOT NULL after IS")

    # col NOT IN (...) / col NOT BETWEEN lo AND hi
    if op == "NOT":
        if pos + 2 < len(tokens) and tokens[pos + 2].upper() == "IN":
            if pos + 3 >= len(tokens) or tokens[pos + 3] != "(":
                raise ParseError("Expected ( after NOT IN")
            inner, new_pos = _extract_paren_tokens(tokens, pos + 3)
            if inner and inner[0].upper() == "SELECT":
                return WhereClause(col=col, op="NOT IN", val="__subquery__",
                                   subquery_ast=_parse_tokens(inner)), new_pos
            return WhereClause(col=col, op="NOT IN",
                               val=",".join(_unquote_token(v) for v in inner
                                            if v != ",")), new_pos
        if pos + 2 < len(tokens) and tokens[pos + 2].upper() == "BETWEEN":
            if pos + 5 >= len(tokens) or tokens[pos + 4].upper() != "AND":
                raise ParseError("Expected: col NOT BETWEEN lo AND hi")
            lo_val = _unquote_token(tokens[pos + 3])
            hi_val = _unquote_token(tokens[pos + 5])
            # col NOT BETWEEN lo AND hi  ≡  (col < lo OR col > hi)
            inner = WhereClause(col=col, op="<", val=lo_val)
            inner.or_clause = WhereClause(col=col, op=">", val=hi_val)
            return WhereClause(col="", op="GROUP", val="",
                               group_clause=inner), pos + 6
        _got = tokens[pos + 2] if pos + 2 < len(tokens) else ""
        raise ParseError(f"Expected IN or BETWEEN after NOT, got '{_got}'")

    if op == "IN":
        if pos + 2 >= len(tokens) or tokens[pos + 2] != "(":
            raise ParseError("Expected ( after IN")
        inner, new_pos = _extract_paren_tokens(tokens, pos + 2)
        if inner and inner[0].upper() == "SELECT":
            return WhereClause(col=col, op="IN", val="__subquery__",
                               subquery_ast=_parse_tokens(inner)), new_pos
        return WhereClause(col=col, op="IN",
                           val=",".join(_unquote_token(v) for v in inner
                                        if v != ",")), new_pos

    if pos + 2 >= len(tokens):
        raise ParseError("Incomplete WHERE clause")

    # col BETWEEN lo AND hi  ≡  col >= lo AND col <= hi
    if op == "BETWEEN":
        if pos + 4 >= len(tokens) or tokens[pos + 3].upper() != "AND":
            raise ParseError("Expected: col BETWEEN lo AND hi")
        lo_val = _unquote_token(tokens[pos + 2])
        hi_val = _unquote_token(tokens[pos + 4])
        lo = WhereClause(col=col, op=">=", val=lo_val)
        lo.and_clause = WhereClause(col=col, op="<=", val=hi_val)
        return lo, pos + 5

    # Scalar subquery: col OP (SELECT ...)
    if tokens[pos + 2] == "(" and pos + 3 < len(tokens) and tokens[pos + 3].upper() == "SELECT":
        if op not in {"=", "!=", "<", ">", "<=", ">="}:
            raise ParseError(f"Operator '{op}' not supported with scalar subquery")
        inner, new_pos = _extract_paren_tokens(tokens, pos + 2)
        return WhereClause(col=col, op=op, val="__subquery__",
                           subquery_ast=_parse_tokens(inner)), new_pos

    val = _unquote_token(tokens[pos + 2])
    if op not in {"=", "!=", "<", ">", "<=", ">=", "LIKE", "GLOB"}:
        raise ParseError(f"Unknown operator: '{op}'")
    if op == "LIKE" and pos + 3 < len(tokens) and tokens[pos + 3].upper() == "ESCAPE":
        if pos + 4 >= len(tokens):
            raise ParseError("Expected escape character after ESCAPE")
        esc = _unquote_token(tokens[pos + 4])
        return WhereClause(col=col, op="LIKE", val=val + "\x00" + esc), pos + 5
    return WhereClause(col=col, op=op, val=val), pos + 3


def _parse_atom(tokens: list[str], pos: int) -> tuple["WhereClause", int]:
    """Parse a single condition or a parenthesized group `(expr)`."""
    if pos < len(tokens) and tokens[pos] == "(":
        inner, new_pos = _extract_paren_tokens(tokens, pos)
        inner_clause, _ = _parse_where_expr(inner, 0)
        return WhereClause(col="", op="GROUP", val="",
                           group_clause=inner_clause), new_pos
    # Prefix NOT: WHERE NOT col = 1  (NOT EXISTS is handled in _parse_one_condition)
    if pos < len(tokens) and tokens[pos].upper() == "NOT":
        nxt = pos + 1
        if nxt < len(tokens) and tokens[nxt].upper() != "EXISTS":
            inner, new_pos = _parse_atom(tokens, nxt)
            return WhereClause(col="", op="NOT", val="",
                               group_clause=inner), new_pos
    return _parse_one_condition(tokens, pos)


def _parse_and_group(tokens: list[str], pos: int) -> tuple["WhereClause", int]:
    """Parse one AND-connected group of atoms."""
    clause, pos = _parse_atom(tokens, pos)
    while pos < len(tokens) and tokens[pos].upper() == "AND":
        next_cond, pos = _parse_atom(tokens, pos + 1)
        tail = clause
        while tail.and_clause:
            tail = tail.and_clause
        tail.and_clause = next_cond
    return clause, pos


def _parse_where_expr(tokens: list[str], pos: int) -> tuple["WhereClause", int]:
    """Parse (AND-group) [OR (AND-group) ...] without the WHERE keyword.
    Called recursively from _parse_atom to handle parenthesized groups.
    """
    clause, pos = _parse_and_group(tokens, pos)
    or_tail = clause
    while pos < len(tokens) and tokens[pos].upper() == "OR":
        next_group, pos = _parse_and_group(tokens, pos + 1)
        or_tail.or_clause = next_group
        or_tail = next_group
    return clause, pos


def _parse_where(tokens: list[str], pos: int) -> tuple["WhereClause | None", int]:
    """Parse WHERE expr. AND binds tighter than OR (standard SQL precedence).
    Parenthesized groups override precedence: WHERE (a=1 OR b=2) AND c=3.
    """
    if pos >= len(tokens) or tokens[pos].upper() != "WHERE":
        return None, pos
    return _parse_where_expr(tokens, pos + 1)


def _parse_fk_action(t: list[str], i: int) -> tuple[str, int]:
    """Parse one of CASCADE | SET NULL | RESTRICT | NO ACTION at position i."""
    if i < len(t) and t[i].upper() == "CASCADE":
        return "CASCADE", i + 1
    if i + 1 < len(t) and t[i].upper() == "SET" and t[i + 1].upper() == "NULL":
        return "SET NULL", i + 2
    if i < len(t) and t[i].upper() == "RESTRICT":
        return "RESTRICT", i + 1
    if i + 1 < len(t) and t[i].upper() == "NO" and t[i + 1].upper() == "ACTION":
        return "NO ACTION", i + 2
    return "RESTRICT", i


def _parse_fk_ref_actions(t: list[str], i: int) -> tuple[str, str, int]:
    """Parse any ON DELETE / ON UPDATE clauses (in any order).
    Returns (on_delete, on_update, new_i).
    """
    on_delete = on_update = "RESTRICT"
    while i < len(t) and t[i].upper() == "ON":
        if i + 1 >= len(t) or t[i + 1].upper() not in ("DELETE", "UPDATE"):
            break
        event = t[i + 1].upper()
        i += 2
        action, i = _parse_fk_action(t, i)
        if event == "DELETE":
            on_delete = action
        else:
            on_update = action
    return on_delete, on_update, i


def _parse_group_having(tokens: list[str], pos: int
                        ) -> tuple[list[str], "WhereClause | None", int]:
    """Parse optional GROUP BY col[, col] [HAVING condition] starting at pos."""
    group_by: list[str] = []
    having: "WhereClause | None" = None
    if pos < len(tokens) and tokens[pos].upper() == "GROUP":
        pos += 1
        if pos >= len(tokens) or tokens[pos].upper() != "BY":
            raise ParseError("Expected BY after GROUP")
        pos += 1
        while pos < len(tokens) and tokens[pos].upper() not in ("HAVING", "ORDER", "LIMIT"):
            if tokens[pos] != ",":
                group_by.append(tokens[pos])
            pos += 1
    if pos < len(tokens) and tokens[pos].upper() == "HAVING":
        having, pos = _parse_and_group(tokens, pos + 1)
        or_tail = having
        while pos < len(tokens) and tokens[pos].upper() == "OR":
            next_group, pos = _parse_and_group(tokens, pos + 1)
            or_tail.or_clause = next_group
            or_tail = next_group
    return group_by, having, pos


def _parse_order_limit(tokens: list[str], pos: int
                       ) -> tuple[list[dict], int | None, int | None]:
    """Parse optional ORDER BY … LIMIT n OFFSET m starting at pos.
    Returns (order_by_list, limit, offset).  order_by items: {"col": str, "desc": bool}.
    """
    order_by: list[dict] = []
    limit:  int | None = None
    offset: int | None = None

    if pos < len(tokens) and tokens[pos].upper() == "ORDER":
        pos += 1
        if pos >= len(tokens) or tokens[pos].upper() != "BY":
            raise ParseError("Expected BY after ORDER")
        pos += 1
        while pos < len(tokens) and tokens[pos].upper() not in ("LIMIT",):
            col = tokens[pos]; pos += 1
            desc = False
            if pos < len(tokens) and tokens[pos].upper() in ("ASC", "DESC"):
                desc = tokens[pos].upper() == "DESC"
                pos += 1
            nulls_first: bool | None = None
            if pos < len(tokens) and tokens[pos].upper() == "NULLS":
                pos += 1
                if pos < len(tokens) and tokens[pos].upper() in ("FIRST", "LAST"):
                    nulls_first = tokens[pos].upper() == "FIRST"
                    pos += 1
            order_by.append({"col": col, "desc": desc, "nulls_first": nulls_first})
            if pos < len(tokens) and tokens[pos] == ",":
                pos += 1

    if pos < len(tokens) and tokens[pos].upper() == "LIMIT":
        pos += 1
        if pos >= len(tokens):
            raise ParseError("Expected integer after LIMIT")
        try:
            limit = int(tokens[pos]); pos += 1
        except ValueError:
            raise ParseError(f"Expected integer after LIMIT, got '{tokens[pos]}'")

    if pos < len(tokens) and tokens[pos].upper() == "OFFSET":
        pos += 1
        if pos >= len(tokens):
            raise ParseError("Expected integer after OFFSET")
        try:
            offset = int(tokens[pos]); pos += 1
        except ValueError:
            raise ParseError(f"Expected integer after OFFSET, got '{tokens[pos]}'")

    return order_by, limit, offset


def parse(sql: str) -> dict:
    return _parse_tokens(_tokenize(sql))


def _parse_tokens(t: list[str]) -> dict:
    if not t:
        raise ParseError("Empty statement")

    # Top-level UNION / INTERSECT / EXCEPT (skip tokens inside parentheses)
    depth = 0
    for idx, tok in enumerate(t):
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
        elif depth == 0 and tok.upper() in ("UNION", "INTERSECT", "EXCEPT"):
            set_op = tok.upper()
            all_flag = idx + 1 < len(t) and t[idx + 1].upper() == "ALL"
            right_start = idx + 2 if all_flag else idx + 1
            return {
                "op":     "SET_OP",
                "set_op": set_op,
                "all":    all_flag,
                "left":   _parse_tokens(t[:idx]),
                "right":  _parse_tokens(t[right_start:]),
            }

    kw = t[0].upper()

    if kw == "WITH":
        i = 1
        ctes: dict[str, dict] = {}
        while True:
            if i >= len(t):
                raise ParseError("Expected CTE name in WITH clause")
            cte_name = t[i]; i += 1
            if i >= len(t) or t[i].upper() != "AS":
                raise ParseError(f"Expected AS after CTE name '{cte_name}'")
            i += 1
            if i >= len(t) or t[i] != "(":
                raise ParseError("Expected ( for CTE body")
            inner, i = _extract_paren_tokens(t, i)
            ctes[cte_name] = _parse_tokens(inner)
            if i < len(t) and t[i] == ",":
                i += 1
            else:
                break
        if i >= len(t):
            raise ParseError("Expected query after WITH")
        main_ast = _parse_tokens(t[i:])
        main_ast["ctes"] = ctes
        return main_ast

    if kw == "BEGIN":
        return {"op": "BEGIN"}
    if kw == "COMMIT":
        return {"op": "COMMIT"}
    if kw == "ROLLBACK":
        i = 1
        if i < len(t) and t[i].upper() == "TRANSACTION":
            i += 1
        if i < len(t) and t[i].upper() == "TO":
            i += 1
            if i < len(t) and t[i].upper() == "SAVEPOINT":
                i += 1
            if i >= len(t):
                raise ParseError("Expected savepoint name after ROLLBACK TO")
            return {"op": "ROLLBACK_TO_SAVEPOINT", "name": t[i]}
        return {"op": "ROLLBACK"}

    if kw == "SAVEPOINT":
        if len(t) < 2:
            raise ParseError("Expected savepoint name after SAVEPOINT")
        return {"op": "SAVEPOINT", "name": t[1]}

    if kw == "RELEASE":
        i = 1
        if i < len(t) and t[i].upper() == "SAVEPOINT":
            i += 1
        if i >= len(t):
            raise ParseError("Expected savepoint name after RELEASE")
        return {"op": "RELEASE_SAVEPOINT", "name": t[i]}

    # CREATE TABLE / INDEX / VIEW  (also: CREATE OR REPLACE VIEW)
    if kw == "CREATE":
        if len(t) < 2:
            raise ParseError("Expected TABLE, INDEX, or VIEW after CREATE")
        or_replace_view = (
            len(t) >= 4
            and t[1].upper() == "OR"
            and t[2].upper() == "REPLACE"
            and t[3].upper() == "VIEW"
        )
        if or_replace_view:
            i = 4
            if_not_exists = False
            if i >= len(t):
                raise ParseError("Expected view name after CREATE OR REPLACE VIEW")
            view_name = t[i]; i += 1
            if i >= len(t) or t[i].upper() != "AS":
                raise ParseError("Expected AS after view name in CREATE OR REPLACE VIEW")
            i += 1
            select_sql = " ".join(t[i:])
            return {"op": "CREATE_VIEW", "name": view_name, "sql": select_sql,
                    "if_not_exists": False, "or_replace": True}
        sub = t[1].upper()
        if sub == "TABLE":
            i = 2
            if_not_exists = False
            if i < len(t) and t[i].upper() == "IF":
                if i + 2 < len(t) and t[i+1].upper() == "NOT" and t[i+2].upper() == "EXISTS":
                    if_not_exists = True; i += 3
                else:
                    raise ParseError("Expected NOT EXISTS after IF in CREATE TABLE")
            if i >= len(t):
                raise ParseError("Expected table name after CREATE TABLE")
            name = t[i]; i += 1
            if i < len(t) and t[i].upper() == "AS":
                i += 1
                select_ast = _parse_tokens(t[i:])
                return {"op": "CREATE_TABLE_AS_SELECT", "name": name,
                        "if_not_exists": if_not_exists, "select": select_ast}
            if i >= len(t) or t[i] != "(":
                raise ParseError("Expected: CREATE TABLE <name> (...) or AS SELECT ...")
            i += 1  # skip (

            def _parse_col_list_ct(pos: int) -> tuple[list[str], int]:
                if pos >= len(t) or t[pos] != "(":
                    raise ParseError("Expected ( for column list")
                pos += 1
                cols_ct: list[str] = []
                while pos < len(t) and t[pos] != ")":
                    if t[pos] != ",":
                        cols_ct.append(t[pos])
                    pos += 1
                if pos >= len(t):
                    raise ParseError("Unmatched ( in column list")
                return cols_ct, pos + 1

            columns, fk_constraints, uc_constraints = [], [], []
            pk_constraint: list[str] = []
            while i < len(t) and t[i] != ")":
                # Table-level: FOREIGN KEY (cols) REFERENCES ref_table (ref_cols)
                if t[i].upper() == "FOREIGN":
                    if i + 1 >= len(t) or t[i + 1].upper() != "KEY":
                        raise ParseError("Expected KEY after FOREIGN")
                    i += 2
                    fk_cols, i = _parse_col_list_ct(i)
                    if i >= len(t) or t[i].upper() != "REFERENCES":
                        raise ParseError("Expected REFERENCES after FOREIGN KEY (...)")
                    i += 1
                    if i >= len(t):
                        raise ParseError("Expected table name after REFERENCES")
                    token_ref = t[i]; i += 1
                    m_ref = re.fullmatch(r"(\w+)\(([^)]*)\)", token_ref)
                    if m_ref:
                        ref_table_fk = m_ref.group(1)
                        ref_cols_fk = [c.strip() for c in m_ref.group(2).split(",")
                                       if c.strip()]
                    else:
                        ref_table_fk = token_ref
                        ref_cols_fk, i = _parse_col_list_ct(i)
                    on_delete_action, on_update_action, i = _parse_fk_ref_actions(t, i)
                    fk_constraints.append(
                        ForeignKey(fk_cols, ref_table_fk, ref_cols_fk,
                                   on_delete_action, on_update_action))
                    if i < len(t) and t[i] == ",":
                        i += 1
                    continue

                # Table-level: PRIMARY KEY (col1, col2, ...)
                if (t[i].upper() == "PRIMARY"
                        and i + 1 < len(t) and t[i + 1].upper() == "KEY"):
                    i += 2
                    pk_constraint, i = _parse_col_list_ct(i)
                    if i < len(t) and t[i] == ",":
                        i += 1
                    continue

                # Table-level: UNIQUE (col1, col2, ...)
                if t[i].upper() == "UNIQUE" and i + 1 < len(t) and t[i + 1] == "(":
                    i += 1
                    uc_cols, i = _parse_col_list_ct(i)
                    uc_constraints.append(uc_cols)
                    if i < len(t) and t[i] == ",":
                        i += 1
                    continue

                col_name = t[i]
                col_type, col_size = _parse_col_type(t[i + 1])
                i += 2
                nullable      = True
                unique        = False
                default       = None
                check         = None
                primary_key   = False
                autoincrement = False
                while i < len(t) and t[i].upper() in (
                        "NOT", "UNIQUE", "DEFAULT", "CHECK", "REFERENCES",
                        "PRIMARY", "AUTOINCREMENT", "AUTO_INCREMENT"):
                    kw = t[i].upper()
                    if kw == "PRIMARY":
                        if i + 1 < len(t) and t[i + 1].upper() == "KEY":
                            primary_key = True; nullable = False; unique = True; i += 2
                        else:
                            raise ParseError("Expected KEY after PRIMARY")
                    elif kw in ("AUTOINCREMENT", "AUTO_INCREMENT"):
                        autoincrement = True; i += 1
                    elif kw == "NOT":
                        if i + 1 < len(t) and t[i + 1].upper() == "NULL":
                            nullable = False
                            i += 2
                        else:
                            raise ParseError("Expected NULL after NOT")
                    elif kw == "UNIQUE":
                        unique = True
                        i += 1
                    elif kw == "DEFAULT":
                        if i + 1 >= len(t):
                            raise ParseError("Expected value after DEFAULT")
                        default = _unquote_token(t[i + 1])
                        i += 2
                    elif kw == "CHECK":
                        if i + 1 >= len(t) or t[i + 1] != "(":
                            raise ParseError("Expected ( after CHECK")
                        i += 2  # skip CHECK and (
                        check_tokens: list[str] = []
                        depth = 1
                        while i < len(t) and depth > 0:
                            tok = t[i]; i += 1
                            if tok == "(":
                                depth += 1
                                check_tokens.append(tok)
                            elif tok == ")":
                                depth -= 1
                                if depth > 0:
                                    check_tokens.append(tok)
                            else:
                                check_tokens.append(tok)
                        if depth != 0:
                            raise ParseError("Unmatched ( in CHECK constraint")
                        check = " ".join(check_tokens)
                    elif kw == "REFERENCES":
                        i += 1
                        if i >= len(t):
                            raise ParseError("Expected table name after REFERENCES")
                        token_ref = t[i]; i += 1
                        m_ref = re.fullmatch(r"(\w+)\(([^)]*)\)", token_ref)
                        if m_ref:
                            ref_table_fk = m_ref.group(1)
                            ref_cols_fk = [c.strip() for c in m_ref.group(2).split(",")
                                           if c.strip()] or [col_name]
                        else:
                            ref_table_fk = token_ref
                            if i < len(t) and t[i] == "(":
                                ref_cols_fk, i = _parse_col_list_ct(i)
                            else:
                                ref_cols_fk = [col_name]
                        on_delete_action, on_update_action, i = \
                            _parse_fk_ref_actions(t, i)
                        fk_constraints.append(
                            ForeignKey([col_name], ref_table_fk, ref_cols_fk,
                                       on_delete_action, on_update_action))
                columns.append(Column(col_name, col_type, col_size, nullable, unique,
                                      default, check, primary_key, autoincrement))
                if i < len(t) and t[i] == ",":
                    i += 1
            return {"op": "CREATE_TABLE", "name": name, "columns": columns,
                    "foreign_keys": fk_constraints,
                    "unique_constraints": uc_constraints,
                    "primary_key_columns": pk_constraint,
                    "if_not_exists": if_not_exists}
        if sub == "VIEW":
            i = 2
            or_replace    = False
            if_not_exists = False
            if i < len(t) and t[i].upper() == "OR":
                if i + 1 < len(t) and t[i + 1].upper() == "REPLACE":
                    or_replace = True; i += 2
                else:
                    raise ParseError("Expected REPLACE after OR in CREATE VIEW")
            if i < len(t) and t[i].upper() == "IF":
                if i + 2 < len(t) and t[i + 1].upper() == "NOT" and t[i + 2].upper() == "EXISTS":
                    if_not_exists = True; i += 3
                else:
                    raise ParseError("Expected NOT EXISTS after IF in CREATE VIEW")
            if i >= len(t):
                raise ParseError("Expected view name after CREATE VIEW")
            view_name = t[i]; i += 1
            if i >= len(t) or t[i].upper() != "AS":
                raise ParseError("Expected AS after view name in CREATE VIEW")
            i += 1
            select_sql = " ".join(t[i:])
            return {"op": "CREATE_VIEW", "name": view_name, "sql": select_sql,
                    "if_not_exists": if_not_exists, "or_replace": or_replace}

        if sub == "INDEX":
            # CREATE [UNIQUE] INDEX [IF NOT EXISTS] idx ON table(col1[, col2, ...])
            i = 2
            if_not_exists = False
            if i < len(t) and t[i].upper() == "IF":
                if i + 2 < len(t) and t[i+1].upper() == "NOT" and t[i+2].upper() == "EXISTS":
                    if_not_exists = True; i += 3
                else:
                    raise ParseError("Expected NOT EXISTS after IF in CREATE INDEX")
            if i >= len(t):
                raise ParseError("Expected: CREATE INDEX <name> ON <table>(<cols>)")
            idx_name = t[i]; i += 1
            if i >= len(t) or t[i].upper() != "ON":
                raise ParseError("Expected ON")
            i += 1  # skip ON; remap t[4] access below to use i
            # Tokenizer may produce "table(col1,col2)" as one token or separate tokens
            if i >= len(t):
                raise ParseError("Expected table name after ON in CREATE INDEX")
            m = re.fullmatch(r"(\w+)\(([^)]+)\)", t[i])
            if m:
                table = m.group(1)
                cols  = [c.strip() for c in m.group(2).split(",")]
            else:
                table = t[i]; i += 1
                if i >= len(t) or t[i] != "(":
                    raise ParseError("Expected (<col>) after table name")
                i += 1; cols = []
                while i < len(t) and t[i] != ")":
                    if t[i] != ",":
                        cols.append(t[i])
                    i += 1
            if not cols:
                raise ParseError("Expected at least one column in index")
            return {"op": "CREATE_INDEX", "idx_name": idx_name,
                    "table": table, "cols": cols, "if_not_exists": if_not_exists}
        raise ParseError(f"Expected TABLE or INDEX, got '{t[1]}'")

    # ALTER TABLE
    if kw == "ALTER":
        if len(t) < 4 or t[1].upper() != "TABLE":
            raise ParseError("Expected: ALTER TABLE <name> ...")
        table = t[2]
        sub   = t[3].upper()
        if sub == "RENAME":
            if len(t) < 5:
                raise ParseError("Expected: RENAME TO <new> or RENAME COLUMN <old> TO <new>")
            if t[4].upper() == "TO":
                if len(t) < 6:
                    raise ParseError("Expected new table name after TO")
                return {"op": "ALTER_RENAME_TABLE", "table": table, "new_name": t[5]}
            if t[4].upper() == "COLUMN":
                if len(t) < 8 or t[6].upper() != "TO":
                    raise ParseError("Expected: RENAME COLUMN <old> TO <new>")
                return {"op": "ALTER_RENAME_COLUMN", "table": table,
                        "old_name": t[5], "new_name": t[7]}
            raise ParseError(f"Expected TO or COLUMN after RENAME, got '{t[4]}'")
        if sub == "ADD":
            if len(t) < 6 or t[4].upper() != "COLUMN":
                raise ParseError("Expected: ADD COLUMN <name> <type>")
            col_name = t[5]
            col_type, col_size = _parse_col_type(t[6])
            i = 7
            nullable = True
            if i < len(t) and t[i].upper() == "NOT":
                if i + 1 < len(t) and t[i + 1].upper() == "NULL":
                    nullable = False
                else:
                    raise ParseError("Expected NULL after NOT")
            return {"op": "ALTER_ADD_COLUMN", "table": table,
                    "col": Column(col_name, col_type, col_size, nullable)}
        if sub == "DROP":
            if len(t) < 6 or t[4].upper() != "COLUMN":
                raise ParseError("Expected: DROP COLUMN <name>")
            return {"op": "ALTER_DROP_COLUMN", "table": table, "col_name": t[5]}
        raise ParseError(f"Unknown ALTER TABLE operation: '{t[3]}'")

    # DROP TABLE / INDEX
    if kw == "DROP":
        if len(t) < 3:
            raise ParseError("Expected TABLE or INDEX after DROP")
        sub = t[1].upper()
        if sub == "TABLE":
            i = 2
            if_exists = False
            if i < len(t) and t[i].upper() == "IF":
                if i + 1 < len(t) and t[i + 1].upper() == "EXISTS":
                    if_exists = True; i += 2
                else:
                    raise ParseError("Expected EXISTS after IF in DROP TABLE")
            if i >= len(t):
                raise ParseError("Expected table name after DROP TABLE")
            return {"op": "DROP_TABLE", "name": t[i], "if_exists": if_exists}
        if sub == "INDEX":
            i = 2
            if_exists = False
            if i < len(t) and t[i].upper() == "IF":
                if i + 1 < len(t) and t[i + 1].upper() == "EXISTS":
                    if_exists = True; i += 2
                else:
                    raise ParseError("Expected EXISTS after IF")
            if i >= len(t):
                raise ParseError("Expected index name after DROP INDEX")
            return {"op": "DROP_INDEX", "idx_name": t[i], "if_exists": if_exists}
        if sub == "VIEW":
            i = 2
            if_exists = False
            if i < len(t) and t[i].upper() == "IF":
                if i + 1 < len(t) and t[i + 1].upper() == "EXISTS":
                    if_exists = True; i += 2
                else:
                    raise ParseError("Expected EXISTS after IF in DROP VIEW")
            if i >= len(t):
                raise ParseError("Expected view name after DROP VIEW")
            return {"op": "DROP_VIEW", "name": t[i], "if_exists": if_exists}
        raise ParseError(f"Expected TABLE, INDEX, or VIEW, got '{t[1]}'")

    # INSERT [OR REPLACE|IGNORE] INTO
    if kw == "INSERT":
        conflict_action = None
        i = 1
        if i < len(t) and t[i].upper() == "OR":
            i += 1
            if i < len(t) and t[i].upper() in ("REPLACE", "IGNORE"):
                conflict_action = t[i].upper(); i += 1
            else:
                raise ParseError(f"Expected REPLACE or IGNORE after INSERT OR")
        if i >= len(t) or t[i].upper() != "INTO":
            raise ParseError("Expected: INSERT INTO <table> ...")
        table, i = t[i + 1], i + 2
        col_names: list[str] | None = None
        if i < len(t) and t[i] == "(":
            i += 1
            col_names = []
            while i < len(t) and t[i] != ")":
                if t[i] != ",":
                    col_names.append(t[i])
                i += 1
            i += 1
        if i >= len(t):
            raise ParseError("Expected VALUES or SELECT")
        if t[i].upper() == "SELECT":
            select_ast = _parse_tokens(t[i:])
            return {"op": "INSERT_SELECT", "table": table,
                    "col_names": col_names, "select": select_ast}
        if t[i].upper() != "VALUES":
            raise ParseError("Expected VALUES or SELECT")
        i += 1
        rows: list[list[str]] = []
        while i < len(t) and t[i] == "(":
            i += 1
            row_vals: list[str] = []
            while i < len(t) and t[i] != ")":
                if t[i] != ",":
                    row_vals.append(_unquote_token(t[i]))
                i += 1
            i += 1  # skip ")"
            rows.append(row_vals)
            if i < len(t) and t[i] == ",":
                i += 1  # skip comma between row groups
        if not rows:
            raise ParseError("Expected at least one row of VALUES")
        # ON CONFLICT DO NOTHING / DO UPDATE SET ...
        on_conflict_set: dict[str, str] = {}
        if i < len(t) and t[i].upper() == "ON":
            if i + 1 < len(t) and t[i + 1].upper() == "CONFLICT":
                i += 2
                if i < len(t) and t[i] == "(":
                    i += 1
                    while i < len(t) and t[i] != ")":
                        i += 1
                    i += 1  # skip )
                if i < len(t) and t[i].upper() == "DO":
                    i += 1
                    if i < len(t) and t[i].upper() == "NOTHING":
                        conflict_action = "IGNORE"; i += 1
                    elif i < len(t) and t[i].upper() == "UPDATE":
                        i += 1
                        if i < len(t) and t[i].upper() == "SET":
                            i += 1
                            while i < len(t) and t[i] not in (";",):
                                if t[i] == ",":
                                    i += 1; continue
                                col_n = t[i]
                                if i + 2 < len(t) and t[i + 1] == "=":
                                    on_conflict_set[col_n] = _unquote_token(t[i + 2])
                                    i += 3
                                elif "=" in t[i]:
                                    parts = t[i].split("=", 1)
                                    on_conflict_set[parts[0]] = _unquote_token(parts[1])
                                    i += 1
                                else:
                                    break
                        conflict_action = "UPDATE"
        returning_i: list[str] = []
        if i < len(t) and t[i].upper() == "RETURNING":
            i += 1
            while i < len(t) and t[i] not in (";",):
                if t[i] != ",":
                    returning_i.append(t[i])
                i += 1
        return {"op": "INSERT", "table": table, "col_names": col_names, "rows": rows,
                "conflict_action": conflict_action, "on_conflict_set": on_conflict_set,
                "returning": returning_i or None}

    # SELECT (with optional INNER JOIN)
    if kw == "SELECT":
        i = 1
        distinct = i < len(t) and t[i].upper() == "DISTINCT"
        if distinct:
            i += 1
        cols: list[str] = []
        col_aliases: dict[str, str] = {}
        while i < len(t) and t[i].upper() != "FROM":
            if t[i] == ",":
                i += 1
                continue
            # Collect expression tokens (handles arithmetic, CASE WHEN, function calls,
            # scalar subqueries: paren depth prevents FROM inside (SELECT...) from breaking)
            expr_parts: list[str] = []
            case_depth = 0
            paren_depth = 0
            while i < len(t):
                tok = t[i]
                upper_tok = tok.upper()
                if tok == "(":
                    paren_depth += 1
                    expr_parts.append(tok); i += 1
                    continue
                if tok == ")":
                    paren_depth -= 1
                    expr_parts.append(tok); i += 1
                    continue
                if upper_tok == "CASE" and paren_depth == 0:
                    case_depth += 1
                    expr_parts.append(tok); i += 1
                    continue
                if upper_tok == "END" and case_depth > 0 and paren_depth == 0:
                    case_depth -= 1
                    expr_parts.append(tok); i += 1
                    if case_depth == 0:
                        break
                    continue
                if paren_depth == 0 and case_depth == 0 and (
                        upper_tok in ("FROM", "AS") or tok == ","):
                    break
                expr_parts.append(tok); i += 1
            col = " ".join(expr_parts)
            if not col:
                break
            if i < len(t) and t[i].upper() == "AS":
                i += 1
                if i < len(t) and t[i].upper() not in ("FROM",):
                    col_aliases[col] = t[i]; i += 1
            cols.append(col)
        if i >= len(t):
            return {
                "op": "SELECT_NOFROM",
                "columns": cols,
                "col_aliases": col_aliases or {},
            }
        i += 1

        # Subquery in FROM: SELECT ... FROM (SELECT ...) AS alias
        if i < len(t) and t[i] == "(":
            inner, i = _extract_paren_tokens(t, i)
            sub_ast = _parse_tokens(inner)
            alias = "t"
            if i < len(t) and t[i].upper() == "AS":
                i += 1
                if i < len(t): alias = t[i]; i += 1
            elif (i < len(t) and t[i] not in (",", ";", "(", ")")
                  and t[i].upper() not in _ALIAS_BLOCKLIST):
                alias = t[i]; i += 1
            where, i          = _parse_where(t, i)
            group_by, having, i = _parse_group_having(t, i)
            order_by, limit, offset = _parse_order_limit(t, i)
            return {
                "op": "SELECT", "table": None,
                "subquery_from": sub_ast, "subquery_alias": alias,
                "columns": None if cols == ["*"] else cols,
                "col_aliases": col_aliases,
                "where": where, "group_by": group_by or None,
                "having": having, "order_by": order_by,
                "limit": limit, "offset": offset, "distinct": distinct,
            }

        table = t[i]; i += 1
        left_alias, i = _parse_table_alias(t, i, table)

        # Multi-table implicit FROM: SELECT * FROM a, b WHERE a.id = b.id
        from_tables = [(table, left_alias)]
        while i < len(t) and t[i] == ",":
            i += 1
            nxt_tbl = t[i]; i += 1
            nxt_alias, i = _parse_table_alias(t, i, nxt_tbl)
            from_tables.append((nxt_tbl, nxt_alias))
        if len(from_tables) > 1:
            extra_implicit = [
                {"join_type": "CROSS", "right_table": tbl, "right_alias": ali,
                 "on_left": None, "on_right": None}
                for tbl, ali in from_tables[2:]
            ]
            where, i          = _parse_where(t, i)
            group_by, having, i = _parse_group_having(t, i)
            order_by, limit, offset = _parse_order_limit(t, i)
            return {
                "op": "JOIN", "join_type": "CROSS",
                "left_table":  from_tables[0][0], "left_alias":  from_tables[0][1],
                "right_table": from_tables[1][0], "right_alias": from_tables[1][1],
                "on_left": None, "on_right": None,
                "columns": None if cols == ["*"] else cols,
                "col_aliases": col_aliases,
                "where": where, "group_by": group_by or None,
                "having": having, "order_by": order_by,
                "limit": limit, "offset": offset, "distinct": distinct,
                "extra_joins": extra_implicit,
            }

        # Check for [INNER | LEFT | RIGHT | FULL [OUTER] | CROSS | NATURAL] JOIN
        join_type: str | None = None
        if i < len(t):
            kw2 = t[i].upper()
            if kw2 in ("LEFT", "RIGHT", "FULL"):
                join_type = kw2; i += 1
                if i < len(t) and t[i].upper() == "OUTER":
                    i += 1
                if i >= len(t) or t[i].upper() != "JOIN":
                    raise ParseError(f"Expected JOIN after {join_type} [OUTER]")
                i += 1
            elif kw2 == "INNER":
                if i + 1 >= len(t) or t[i + 1].upper() != "JOIN":
                    raise ParseError("Expected JOIN after INNER")
                join_type = "INNER"; i += 2
            elif kw2 == "CROSS":
                if i + 1 >= len(t) or t[i + 1].upper() != "JOIN":
                    raise ParseError("Expected JOIN after CROSS")
                join_type = "CROSS"; i += 2
            elif kw2 == "NATURAL":
                if i + 1 >= len(t) or t[i + 1].upper() != "JOIN":
                    raise ParseError("Expected JOIN after NATURAL")
                join_type = "NATURAL"; i += 2
            elif kw2 == "JOIN":
                join_type = "INNER"; i += 1
        if join_type is not None:
            right_table = t[i]; i += 1
            right_alias, i = _parse_table_alias(t, i, right_table)
            on_left = on_right = None
            if join_type not in ("CROSS", "NATURAL"):
                if i >= len(t) or t[i].upper() != "ON":
                    raise ParseError(f"Expected ON after {right_table} for {join_type} JOIN")
                i += 1
                on_left = t[i]; i += 1
                if i >= len(t) or t[i] != "=":
                    raise ParseError("Expected = in ON clause")
                i += 1
                on_right = t[i]; i += 1
            # Chain additional JOINs: FROM a JOIN b ON ... JOIN c ON ...
            _JOIN_KWS = frozenset({"INNER", "LEFT", "RIGHT", "FULL", "CROSS",
                                   "NATURAL", "JOIN"})
            extra_joins: list[dict] = []
            while i < len(t) and t[i].upper() in _JOIN_KWS:
                ej_type = "INNER"
                kw3 = t[i].upper()
                if kw3 in ("LEFT", "RIGHT", "FULL"):
                    ej_type = kw3; i += 1
                    if i < len(t) and t[i].upper() == "OUTER":
                        i += 1
                    if i >= len(t) or t[i].upper() != "JOIN":
                        raise ParseError(f"Expected JOIN after {ej_type}")
                    i += 1
                elif kw3 == "INNER":
                    i += 1
                    if i >= len(t) or t[i].upper() != "JOIN":
                        raise ParseError("Expected JOIN after INNER")
                    i += 1
                elif kw3 == "CROSS":
                    ej_type = "CROSS"; i += 1
                    if i >= len(t) or t[i].upper() != "JOIN":
                        raise ParseError("Expected JOIN after CROSS")
                    i += 1
                elif kw3 == "NATURAL":
                    ej_type = "NATURAL"; i += 1
                    if i >= len(t) or t[i].upper() != "JOIN":
                        raise ParseError("Expected JOIN after NATURAL")
                    i += 1
                else:  # bare JOIN
                    ej_type = "INNER"; i += 1
                ej_right = t[i]; i += 1
                ej_alias, i = _parse_table_alias(t, i, ej_right)
                ej_on_l = ej_on_r = None
                if ej_type not in ("CROSS", "NATURAL"):
                    if i >= len(t) or t[i].upper() != "ON":
                        raise ParseError(f"Expected ON after {ej_right}")
                    i += 1
                    ej_on_l = t[i]; i += 1
                    if i >= len(t) or t[i] != "=":
                        raise ParseError("Expected = in ON clause")
                    i += 1
                    ej_on_r = t[i]; i += 1
                extra_joins.append({"join_type": ej_type, "right_table": ej_right,
                                    "right_alias": ej_alias,
                                    "on_left": ej_on_l, "on_right": ej_on_r})
            where, i               = _parse_where(t, i)
            order_by, limit, offset = _parse_order_limit(t, i)
            return {
                "op":           "JOIN",
                "join_type":    join_type,
                "left_table":   table,
                "left_alias":   left_alias,
                "right_table":  right_table,
                "right_alias":  right_alias,
                "on_left":      on_left,
                "on_right":     on_right,
                "columns":      None if cols == ["*"] else cols,
                "col_aliases":  col_aliases,
                "where":        where,
                "order_by":     order_by,
                "limit":        limit,
                "offset":       offset,
                "extra_joins":  extra_joins,
            }
        where, i                    = _parse_where(t, i)
        group_by, having, i         = _parse_group_having(t, i)
        order_by, limit, offset     = _parse_order_limit(t, i)
        return {
            "op":         "SELECT",
            "table":      table,
            "columns":    None if cols == ["*"] else cols,
            "col_aliases": col_aliases,
            "where":      where,
            "group_by":   group_by or None,
            "having":     having,
            "order_by":   order_by,
            "limit":      limit,
            "offset":     offset,
            "distinct":   distinct,
        }

    # UPDATE
    if kw == "UPDATE":
        if len(t) < 4 or t[2].upper() != "SET":
            raise ParseError("Expected: UPDATE <table> SET col=val ...")
        table = t[1]; i = 3
        assignments: dict[str, str] = {}
        while i < len(t) and t[i].upper() not in ("WHERE", "LIMIT", "RETURNING"):
            if t[i] == ",":
                i += 1
                continue
            token = t[i]
            if "=" in token:                        # col=val (no spaces)
                col, val = token.split("=", 1)
                assignments[col] = _unquote_token(val)
                i += 1
            elif i + 2 < len(t) and t[i + 1] == "=":  # col = val
                assignments[t[i]] = _unquote_token(t[i + 2])
                i += 3
            else:
                raise ParseError(f"Expected col=val near '{token}'")
        where, pos = _parse_where(t, i)
        limit_u: int | None = None
        if pos < len(t) and t[pos].upper() == "LIMIT":
            pos += 1
            try:
                limit_u = int(t[pos]); pos += 1
            except (ValueError, IndexError):
                raise ParseError("Expected integer after LIMIT")
        returning_u: list[str] = []
        if pos < len(t) and t[pos].upper() == "RETURNING":
            pos += 1
            while pos < len(t) and t[pos] not in (";",):
                if t[pos] != ",":
                    returning_u.append(t[pos])
                pos += 1
        return {"op": "UPDATE", "table": table, "assignments": assignments,
                "where": where, "limit": limit_u,
                "returning": returning_u or None}

    # DELETE
    if kw == "DELETE":
        if len(t) < 3 or t[1].upper() != "FROM":
            raise ParseError("Expected: DELETE FROM <table> [WHERE ...]")
        where, pos = _parse_where(t, 3)
        limit_d: int | None = None
        if pos < len(t) and t[pos].upper() == "LIMIT":
            pos += 1
            try:
                limit_d = int(t[pos]); pos += 1
            except (ValueError, IndexError):
                raise ParseError("Expected integer after LIMIT")
        returning_d: list[str] = []
        if pos < len(t) and t[pos].upper() == "RETURNING":
            pos += 1
            while pos < len(t) and t[pos] not in (";",):
                if t[pos] != ",":
                    returning_d.append(t[pos])
                pos += 1
        return {"op": "DELETE", "table": t[2], "where": where,
                "limit": limit_d, "returning": returning_d or None}

    # TRUNCATE TABLE t
    if kw == "TRUNCATE":
        if len(t) < 3 or t[1].upper() != "TABLE":
            raise ParseError("Expected: TRUNCATE TABLE <name>")
        return {"op": "TRUNCATE", "table": t[2]}

    raise ParseError(f"Unrecognized statement: '{t[0]}'")
