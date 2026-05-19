import re

from .constants import INTEGER, REAL, TEXT, DEFAULT_TEXT_SIZE
from .schema import Column, ForeignKey, Schema
from .where import WhereClause


class ParseError(ValueError):
    pass


_TOKEN_RE = re.compile(r"'[^']*'|\"[^\"]*\"|\w+\([^)]*\)|[(),;*]|[^\s(),;*]+")

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
    return [t.strip("'\"") for t in _TOKEN_RE.findall(sql)]


def _parse_col_type(token: str) -> tuple[str, int]:
    u = token.upper()
    if u == INTEGER:  return INTEGER, 8
    if u == REAL:     return REAL,    8
    if u == TEXT:     return TEXT,    DEFAULT_TEXT_SIZE
    m = re.fullmatch(r"VARCHAR\((\d+)\)", u)
    if m:             return TEXT,    int(m.group(1))
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

    # col NOT IN (...)
    if op == "NOT":
        if pos + 2 < len(tokens) and tokens[pos + 2].upper() == "IN":
            if pos + 3 >= len(tokens) or tokens[pos + 3] != "(":
                raise ParseError("Expected ( after NOT IN")
            inner, new_pos = _extract_paren_tokens(tokens, pos + 3)
            if inner and inner[0].upper() == "SELECT":
                return WhereClause(col=col, op="NOT IN", val="__subquery__",
                                   subquery_ast=_parse_tokens(inner)), new_pos
            return WhereClause(col=col, op="NOT IN",
                               val=",".join(v for v in inner if v != ",")), new_pos
        _got = tokens[pos + 2] if pos + 2 < len(tokens) else ""
        raise ParseError(f"Expected IN after NOT, got '{_got}'")

    if op == "IN":
        if pos + 2 >= len(tokens) or tokens[pos + 2] != "(":
            raise ParseError("Expected ( after IN")
        inner, new_pos = _extract_paren_tokens(tokens, pos + 2)
        if inner and inner[0].upper() == "SELECT":
            return WhereClause(col=col, op="IN", val="__subquery__",
                               subquery_ast=_parse_tokens(inner)), new_pos
        return WhereClause(col=col, op="IN",
                           val=",".join(v for v in inner if v != ",")), new_pos

    if pos + 2 >= len(tokens):
        raise ParseError("Incomplete WHERE clause")

    # Scalar subquery: col OP (SELECT ...)
    if tokens[pos + 2] == "(" and pos + 3 < len(tokens) and tokens[pos + 3].upper() == "SELECT":
        if op not in {"=", "!=", "<", ">", "<=", ">="}:
            raise ParseError(f"Operator '{op}' not supported with scalar subquery")
        inner, new_pos = _extract_paren_tokens(tokens, pos + 2)
        return WhereClause(col=col, op=op, val="__subquery__",
                           subquery_ast=_parse_tokens(inner)), new_pos

    val = tokens[pos + 2]
    if op not in {"=", "!=", "<", ">", "<=", ">=", "LIKE"}:
        raise ParseError(f"Unknown operator: '{op}'")
    return WhereClause(col=col, op=op, val=val), pos + 3


def _parse_and_group(tokens: list[str], pos: int) -> tuple["WhereClause", int]:
    """Parse one AND-connected group of conditions."""
    clause, pos = _parse_one_condition(tokens, pos)
    while pos < len(tokens) and tokens[pos].upper() == "AND":
        next_cond, pos = _parse_one_condition(tokens, pos + 1)
        tail = clause
        while tail.and_clause:
            tail = tail.and_clause
        tail.and_clause = next_cond
    return clause, pos


def _parse_where(tokens: list[str], pos: int) -> tuple["WhereClause | None", int]:
    """Parse WHERE (AND-group) [OR (AND-group) ...]. Returns (clause, next_pos).
    AND binds tighter than OR (standard SQL precedence).
    """
    if pos >= len(tokens) or tokens[pos].upper() != "WHERE":
        return None, pos
    clause, pos = _parse_and_group(tokens, pos + 1)
    or_tail = clause
    while pos < len(tokens) and tokens[pos].upper() == "OR":
        next_group, pos = _parse_and_group(tokens, pos + 1)
        or_tail.or_clause = next_group
        or_tail = next_group
    return clause, pos


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
                       ) -> tuple[list[dict], int | None]:
    """Parse optional ORDER BY … LIMIT n starting at pos.
    Returns (order_by_list, limit).  order_by items: {"col": str, "desc": bool}.
    """
    order_by: list[dict] = []
    limit: int | None = None

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
            order_by.append({"col": col, "desc": desc})
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

    return order_by, limit


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

    if kw in ("BEGIN", "COMMIT", "ROLLBACK"):
        return {"op": kw}

    # CREATE TABLE / INDEX
    if kw == "CREATE":
        if len(t) < 2:
            raise ParseError("Expected TABLE or INDEX after CREATE")
        sub = t[1].upper()
        if sub == "TABLE":
            if len(t) < 4 or t[3] != "(":
                raise ParseError("Expected: CREATE TABLE <name> (...)")
            name = t[2]
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

            columns, fk_constraints, i = [], [], 4
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
                    ref_table_fk = t[i]; i += 1
                    ref_cols_fk, i = _parse_col_list_ct(i)
                    fk_constraints.append(ForeignKey(fk_cols, ref_table_fk, ref_cols_fk))
                    if i < len(t) and t[i] == ",":
                        i += 1
                    continue

                col_name = t[i]
                col_type, col_size = _parse_col_type(t[i + 1])
                i += 2
                nullable = True
                unique   = False
                default  = None
                check    = None
                while i < len(t) and t[i].upper() in (
                        "NOT", "UNIQUE", "DEFAULT", "CHECK", "REFERENCES"):
                    kw = t[i].upper()
                    if kw == "NOT":
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
                        default = t[i + 1]
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
                        ref_table_fk = t[i]; i += 1
                        if i < len(t) and t[i] == "(":
                            ref_cols_fk, i = _parse_col_list_ct(i)
                        else:
                            ref_cols_fk = [col_name]
                        fk_constraints.append(
                            ForeignKey([col_name], ref_table_fk, ref_cols_fk))
                columns.append(Column(col_name, col_type, col_size, nullable, unique,
                                      default, check))
                if i < len(t) and t[i] == ",":
                    i += 1
            return {"op": "CREATE_TABLE", "name": name, "columns": columns,
                    "foreign_keys": fk_constraints}
        if sub == "INDEX":
            # CREATE INDEX idx ON table(col1[, col2, ...])
            if len(t) < 5:
                raise ParseError("Expected: CREATE INDEX <name> ON <table>(<cols>)")
            idx_name = t[2]
            if t[3].upper() != "ON":
                raise ParseError("Expected ON")
            # Tokenizer may produce "table(col1,col2)" as one token or separate tokens
            m = re.fullmatch(r"(\w+)\(([^)]+)\)", t[4])
            if m:
                table = m.group(1)
                cols  = [c.strip() for c in m.group(2).split(",")]
            else:
                table = t[4]
                if len(t) < 7 or t[5] != "(":
                    raise ParseError("Expected (<col>) after table name")
                i, cols = 6, []
                while i < len(t) and t[i] != ")":
                    if t[i] != ",":
                        cols.append(t[i])
                    i += 1
            if not cols:
                raise ParseError("Expected at least one column in index")
            return {"op": "CREATE_INDEX", "idx_name": idx_name,
                    "table": table, "cols": cols}
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
            return {"op": "DROP_TABLE", "name": t[2]}
        if sub == "INDEX":
            return {"op": "DROP_INDEX", "idx_name": t[2]}
        raise ParseError(f"Expected TABLE or INDEX, got '{t[1]}'")

    # INSERT INTO
    if kw == "INSERT":
        if len(t) < 3 or t[1].upper() != "INTO":
            raise ParseError("Expected: INSERT INTO <table> ...")
        table, i = t[2], 3
        col_names: list[str] | None = None
        if i < len(t) and t[i] == "(":
            i += 1
            col_names = []
            while i < len(t) and t[i] != ")":
                if t[i] != ",":
                    col_names.append(t[i])
                i += 1
            i += 1
        if i >= len(t) or t[i].upper() != "VALUES":
            raise ParseError("Expected VALUES")
        i += 2
        values: list[str] = []
        while i < len(t) and t[i] != ")":
            if t[i] != ",":
                values.append(t[i])
            i += 1
        return {"op": "INSERT", "table": table, "col_names": col_names, "values": values}

    # SELECT (with optional INNER JOIN)
    if kw == "SELECT":
        i = 1
        distinct = i < len(t) and t[i].upper() == "DISTINCT"
        if distinct:
            i += 1
        cols = []
        while i < len(t) and t[i].upper() != "FROM":
            if t[i] != ",":
                cols.append(t[i])
            i += 1
        if i >= len(t):
            raise ParseError("Expected FROM")
        i += 1
        table = t[i]; i += 1
        left_alias, i = _parse_table_alias(t, i, table)
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
            where, i        = _parse_where(t, i)
            order_by, limit = _parse_order_limit(t, i)
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
                "where":        where,
                "order_by":     order_by,
                "limit":        limit,
            }
        where, i              = _parse_where(t, i)
        group_by, having, i   = _parse_group_having(t, i)
        order_by, limit       = _parse_order_limit(t, i)
        return {
            "op":       "SELECT",
            "table":    table,
            "columns":  None if cols == ["*"] else cols,
            "where":    where,
            "group_by": group_by or None,
            "having":   having,
            "order_by": order_by,
            "limit":    limit,
            "distinct": distinct,
        }

    # UPDATE
    if kw == "UPDATE":
        if len(t) < 4 or t[2].upper() != "SET":
            raise ParseError("Expected: UPDATE <table> SET col=val ...")
        table = t[1]; i = 3
        assignments: dict[str, str] = {}
        while i < len(t) and t[i].upper() != "WHERE":
            if t[i] == ",":
                i += 1
                continue
            token = t[i]
            if "=" in token:                        # col=val (no spaces)
                col, val = token.split("=", 1)
                assignments[col] = val
                i += 1
            elif i + 2 < len(t) and t[i + 1] == "=":  # col = val
                assignments[t[i]] = t[i + 2]
                i += 3
            else:
                raise ParseError(f"Expected col=val near '{token}'")
        where, _ = _parse_where(t, i)
        return {"op": "UPDATE", "table": table, "assignments": assignments,
                "where": where}

    # DELETE
    if kw == "DELETE":
        if len(t) < 3 or t[1].upper() != "FROM":
            raise ParseError("Expected: DELETE FROM <table> [WHERE ...]")
        where, _ = _parse_where(t, 3)
        return {"op": "DELETE", "table": t[2], "where": where}

    raise ParseError(f"Unrecognized statement: '{t[0]}'")
