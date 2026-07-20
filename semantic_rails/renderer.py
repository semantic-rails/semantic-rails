"""SQL AST -> rendered SQL text.

Walks the :mod:`semantic_rails.sql_ast` tree and produces the human-
readable SQL string returned by ``compile`` and ``query``. Dialect-specific
function and keyword choices come from :mod:`semantic_rails.dialects` — this
module owns layout, indentation, identifier quoting, and CTE ordering.
"""

from __future__ import annotations

import re

from .sql_ast import (
    SqlBinary,
    SqlCall,
    SqlCallWithOrder,
    SqlCase,
    SqlCast,
    SqlCte,
    SqlDatePart,
    SqlExists,
    SqlExpr,
    SqlIdentifier,
    SqlIn,
    SqlIndexAccess,
    SqlInterval,
    SqlIsNull,
    SqlJsonExtract,
    SqlLiteral,
    SqlNamedArg,
    SqlParameterizedCall,
    SqlQuery,
    SqlSelect,
    SqlSetQuery,
    SqlStar,
    SqlTableFunction,
    SqlTableRef,
    SqlWindow,
    SqlWithinGroup,
    normalize_sql_binary_operator,
    normalize_sql_cast_type_name,
    normalize_sql_date_part,
    normalize_sql_function_name,
    normalize_sql_join_type,
    normalize_sql_set_operator,
    normalize_sql_sort_direction,
    normalize_sql_window_frame,
)

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED_WORDS = {
    "ALL",
    "AND",
    "AS",
    "BY",
    "CASE",
    "CROSS",
    "EXISTS",
    "FULL",
    "FROM",
    "GROUP",
    "HAVING",
    "IN",
    "INNER",
    "JOIN",
    "LEFT",
    "LIMIT",
    "NOT",
    "NULL",
    "ON",
    "OR",
    "ORDER",
    "OUTER",
    "RIGHT",
    "SELECT",
    "TRUE",
    "UNION",
    "WHERE",
    "WITH",
}


def _quote_ident(value: str) -> str:
    raw = str(value)
    if _IDENT_RE.match(raw) and raw.upper() not in _RESERVED_WORDS:
        return raw
    return '"' + raw.replace('"', '""') + '"'


# Operator precedence used to elide redundant parens around SqlBinary
# expressions. Higher number = binds tighter. Operators not listed
# render with parens (preserves existing behavior for any obscure op).
#
# Reference: SQL standard / Snowflake operator precedence.
_OP_PRECEDENCE: dict[str, int] = {
    "OR": 1,
    "AND": 2,
    # Boolean tests (4) — comparison group
    "=": 4,
    "!=": 4,
    "<": 4,
    ">": 4,
    "<=": 4,
    ">=": 4,
    "IS": 4,
    "IS NOT": 4,
    "IS DISTINCT FROM": 4,
    "IS NOT DISTINCT FROM": 4,
    "LIKE": 4,
    "NOT LIKE": 4,
    "ILIKE": 4,
    "NOT ILIKE": 4,
    # Additive
    "+": 5,
    "-": 5,
    # Multiplicative
    "*": 6,
    "/": 6,
    "%": 6,
}


def _binary_precedence(op: str) -> int:
    """Look up precedence for a normalized SqlBinary op. 0 = unknown
    (forces parentheses around the rendered expression)."""
    return _OP_PRECEDENCE.get(op, 0)


def render_expr(expr: SqlExpr, *, parent_precedence: int = 0) -> str:
    if isinstance(expr, SqlIdentifier):
        return ".".join(_quote_ident(part) for part in expr.parts)
    if isinstance(expr, SqlStar):
        if expr.qualifier:
            return f"{_quote_ident(expr.qualifier)}.*"
        return "*"
    if isinstance(expr, SqlLiteral):
        if expr.value is None:
            return "NULL"
        if isinstance(expr.value, bool):
            return "TRUE" if expr.value else "FALSE"
        if isinstance(expr.value, (int, float)):
            return str(expr.value)
        return "'" + str(expr.value).replace("'", "''") + "'"
    if isinstance(expr, SqlCall):
        function_name = normalize_sql_function_name(expr.name)
        if expr.distinct:
            args = ", ".join(render_expr(arg) for arg in expr.args)
            return f"{function_name}(DISTINCT {args})"
        return f"{function_name}(" + ", ".join(render_expr(arg) for arg in expr.args) + ")"
    if isinstance(expr, SqlBinary):
        op = normalize_sql_binary_operator(expr.op)
        my_prec = _binary_precedence(op)
        # Render left at our precedence (left-associative), right at our
        # precedence + 1 so right-children of the same operator still paren.
        # That keeps `a - (b - c)` distinct from `a - b - c` when authored.
        left_sql = (
            render_expr(expr.left, parent_precedence=my_prec)
            if my_prec > 0
            else render_expr(expr.left)
        )
        right_sql = (
            render_expr(expr.right, parent_precedence=my_prec + 1)
            if my_prec > 0
            else render_expr(expr.right)
        )
        rendered = f"{left_sql} {op} {right_sql}"
        # Paren when:
        #  - precedence is unknown (always wrap for safety), OR
        #  - our precedence is strictly less than parent's, OR
        #  - parent is at the same precedence boundary AND we need to be
        #    self-delimiting (e.g., right-child of same-prec op).
        if my_prec == 0 or my_prec < parent_precedence:
            return f"({rendered})"
        return rendered
    if isinstance(expr, SqlIn):
        operator = "NOT IN" if expr.negated else "IN"
        return (
            f"({render_expr(expr.expr)} {operator} ("
            + ", ".join(render_expr(value) for value in expr.values)
            + "))"
        )
    if isinstance(expr, SqlDatePart):
        return normalize_sql_date_part(expr.part)
    if isinstance(expr, SqlInterval):
        return f"INTERVAL ({render_expr(expr.value)}) {normalize_sql_date_part(expr.unit)}"
    if isinstance(expr, SqlExists):
        prefix = "NOT EXISTS" if expr.negated else "EXISTS"
        return f"{prefix} (\n{render_select(expr.query, flatten_ctes=False)}\n)"
    if isinstance(expr, SqlIsNull):
        return f"({render_expr(expr.expr)} IS NULL)"
    if isinstance(expr, SqlCase):
        parts = ["CASE"]
        for when in expr.whens:
            parts.append(f"WHEN {render_expr(when.condition)} THEN {render_expr(when.result)}")
        if expr.else_expr is not None:
            parts.append(f"ELSE {render_expr(expr.else_expr)}")
        parts.append("END")
        return " ".join(parts)
    if isinstance(expr, SqlCast):
        return f"CAST({render_expr(expr.expr)} AS {normalize_sql_cast_type_name(expr.type_name)})"
    if isinstance(expr, SqlWindow):
        over_parts: list[str] = []
        if expr.partition_by:
            over_parts.append(
                "PARTITION BY " + ", ".join(render_expr(item) for item in expr.partition_by)
            )
        if expr.order_by:
            order_terms = [
                f"{render_expr(item.expr)} {normalize_sql_sort_direction(item.direction)}"
                for item in expr.order_by
            ]
            over_parts.append("ORDER BY " + ", ".join(order_terms))
        if expr.frame:
            over_parts.append(normalize_sql_window_frame(expr.frame))
        return f"{render_expr(expr.function)} OVER (" + " ".join(over_parts) + ")"
    if isinstance(expr, SqlWithinGroup):
        order_sql = ", ".join(
            f"{render_expr(item.expr)} {normalize_sql_sort_direction(item.direction)}"
            for item in expr.order_by
        )
        return f"{render_expr(expr.function)} WITHIN GROUP (ORDER BY {order_sql})"
    if isinstance(expr, SqlCallWithOrder):
        function_name = normalize_sql_function_name(expr.call.name)
        args = ", ".join(render_expr(arg) for arg in expr.call.args)
        if expr.call.distinct:
            args = f"DISTINCT {args}"
        order_sql = ", ".join(
            f"{render_expr(item.expr)} {normalize_sql_sort_direction(item.direction)}"
            for item in expr.order_by
        )
        limit_sql = f" LIMIT {int(expr.limit)}" if expr.limit is not None else ""
        return f"{function_name}({args} ORDER BY {order_sql}{limit_sql})"
    if isinstance(expr, SqlIndexAccess):
        index_sql = render_expr(expr.index)
        if expr.wrapper:
            index_sql = f"{expr.wrapper}({index_sql})"
        return f"({render_expr(expr.expr)})[{index_sql}]"
    if isinstance(expr, SqlParameterizedCall):
        function_name = normalize_sql_function_name(expr.name)
        params = ", ".join(render_expr(param) for param in expr.params)
        args = ", ".join(render_expr(arg) for arg in expr.args)
        return f"{function_name}({params})({args})"
    if isinstance(expr, SqlJsonExtract):
        rendered = render_expr(expr.expr)
        for part in expr.path:
            rendered += ":" + _quote_ident(str(part))
        return f"{rendered}::STRING" if expr.as_text else rendered
    raise TypeError(f"Unsupported SQL expression: {type(expr)!r}")


def _render_named_arg(arg: SqlNamedArg) -> str:
    return f"{_quote_ident(arg.name)} => {render_expr(arg.value)}"


def _render_table_ref(table: SqlTableRef | SqlTableFunction) -> str:
    if isinstance(table, SqlTableFunction):
        function_name = normalize_sql_function_name(table.name)
        args = [render_expr(arg) for arg in table.args]
        args.extend(_render_named_arg(arg) for arg in table.named_args)
        rendered = f"{function_name}(" + ", ".join(args) + ")"
        if table.lateral:
            rendered = "LATERAL " + rendered
        if table.alias:
            rendered += f" AS {_quote_ident(table.alias)}"
            if table.columns:
                rendered += "(" + ", ".join(_quote_ident(column) for column in table.columns) + ")"
        return rendered
    rendered = ".".join(_quote_ident(part) for part in str(table.name).split("."))
    if table.alias:
        return f"{rendered} AS {_quote_ident(table.alias)}"
    return rendered


def _select_without_ctes(query: SqlSelect) -> SqlSelect:
    return SqlSelect(
        select=query.select,
        from_table=query.from_table,
        joins=query.joins,
        where=query.where,
        group_by=query.group_by,
        having=query.having,
        qualify=query.qualify,
        order_by=query.order_by,
        limit=query.limit,
        ctes=[],
        distinct=getattr(query, "distinct", False),
    )


def _flatten_ctes(query: SqlSelect) -> SqlSelect:
    def _walk_query(node: SqlQuery) -> tuple[SqlQuery, list[SqlCte]]:
        if isinstance(node, SqlSetQuery):
            queries: list[SqlSelect] = []
            hoisted: list[SqlCte] = []
            for item in node.queries:
                walked, nested = _walk_select(item)
                if not isinstance(walked, SqlSelect):
                    raise TypeError("Set query branches must render as SELECT statements")
                hoisted.extend(nested)
                queries.append(walked)
            return SqlSetQuery(operator=node.operator, queries=queries), hoisted
        return _walk_select(node)

    def _walk_select(node: SqlSelect) -> tuple[SqlSelect, list[SqlCte]]:
        hoisted: list[SqlCte] = []
        for cte in node.ctes:
            cte_query, nested = _walk_query(cte.query)
            hoisted.extend(nested)
            hoisted.append(SqlCte(name=cte.name, query=cte_query))
        return _select_without_ctes(node), hoisted

    root, ctes = _walk_select(query)
    return SqlSelect(
        select=root.select,
        from_table=root.from_table,
        joins=root.joins,
        where=root.where,
        group_by=root.group_by,
        having=root.having,
        qualify=root.qualify,
        order_by=root.order_by,
        limit=root.limit,
        ctes=ctes,
        distinct=getattr(root, "distinct", False),
    )


def render_select(query: SqlSelect, *, flatten_ctes: bool = True) -> str:
    if flatten_ctes:
        query = _flatten_ctes(query)
    chunks: list[str] = []
    if query.ctes:
        ctes = []
        for cte in query.ctes:
            ctes.append(
                f"{_quote_ident(cte.name)} AS (\n{render_query(cte.query, flatten_ctes=False)}\n)"
            )
        chunks.append("WITH " + ",\n".join(ctes))
    select_sql = ",\n  ".join(
        f"{render_expr(field.expression)} AS {_quote_ident(field.alias)}" for field in query.select
    )
    select_keyword = "SELECT DISTINCT" if getattr(query, "distinct", False) else "SELECT"
    chunks.append(f"{select_keyword}\n  {select_sql}")
    if query.from_table is not None:
        chunks.append(f"FROM {_render_table_ref(query.from_table)}")
    for join in query.joins:
        join_type = normalize_sql_join_type(join.join_type)
        join_sql = f"{join_type} JOIN {_render_table_ref(join.table)}"
        if join_type != "CROSS":
            if join.on is None:
                raise TypeError(f"{join_type} JOIN requires an ON expression")
            join_sql += f" ON {render_expr(join.on)}"
        chunks.append(join_sql)
    if query.where:
        chunks.append("WHERE\n  " + "\n  AND ".join(render_expr(item) for item in query.where))
    if query.group_by:
        chunks.append("GROUP BY\n  " + ",\n  ".join(render_expr(item) for item in query.group_by))
    if query.having:
        chunks.append("HAVING\n  " + "\n  AND ".join(render_expr(item) for item in query.having))
    if query.qualify:
        chunks.append("QUALIFY\n  " + "\n  AND ".join(render_expr(item) for item in query.qualify))
    if query.order_by:
        chunks.append(
            "ORDER BY\n  "
            + ",\n  ".join(
                f"{render_expr(order.expression)} {normalize_sql_sort_direction(order.direction)}"
                for order in query.order_by
            )
        )
    if query.limit is not None:
        chunks.append(f"LIMIT {query.limit}")
    return "\n".join(chunks)


def render_query(query: SqlQuery, *, flatten_ctes: bool = True) -> str:
    if isinstance(query, SqlSelect):
        return render_select(query, flatten_ctes=flatten_ctes)
    if isinstance(query, SqlSetQuery):
        operator = normalize_sql_set_operator(query.operator)
        return f"\n{operator}\n".join(
            render_select(item, flatten_ctes=flatten_ctes) for item in query.queries
        )
    raise TypeError(f"Unsupported SQL query: {type(query)!r}")


def render_select_for_profile(query: SqlSelect, profile: str | None = None) -> str:
    normalized = str(profile or "audit").strip().lower()
    if normalized == "debug":
        return render_select(query, flatten_ctes=False)
    rendered = render_select(query, flatten_ctes=True)
    if normalized == "compact":
        return re.sub(r"\s+", " ", rendered).strip()
    return rendered
