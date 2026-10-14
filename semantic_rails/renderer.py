"""SQL AST -> rendered SQL text.

Walks the :mod:`semantic_rails.sql_ast` tree and produces the human-
readable SQL string returned by ``compile`` and ``query``. Dialect-specific
function and keyword choices come from :mod:`semantic_rails.dialects` — this
module owns layout, indentation, identifier quoting, and CTE ordering.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .operators import LIKE_ESCAPE
from .sql_ast import (
    SqlAggregateFilter,
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
    SqlLike,
    SqlLiteral,
    SqlNamedArg,
    SqlParameter,
    SqlParameterizedCall,
    SqlQuery,
    SqlSelect,
    SqlSetQuery,
    SqlTableFunction,
    SqlTableRef,
    SqlWindow,
    SqlWithinGroup,
    is_null_literal,
    normalize_sql_binary_operator,
    normalize_sql_cast_type_name,
    normalize_sql_date_part,
    normalize_sql_function_name,
    normalize_sql_join_type,
    normalize_sql_set_operator,
    normalize_sql_sort_direction,
    normalize_sql_window_frame,
)
from .sql_identifiers import quote_identifier

if TYPE_CHECKING:
    from .dialects import SqlDialect

# Which dialect's literal-quoting rules apply to the tree currently being
# rendered. String-literal quoting is not portable:
# Snowflake, BigQuery, ClickHouse and Databricks honour backslash escapes
# inside single-quoted literals and the rest do not, so the same value has to
# be escaped differently per warehouse. Every other dialect choice is already
# baked into the AST by the lowering pass, except typed boolean NULL casts
# (``SqlDialect.null_boolean_type``).
#
# It is a ContextVar rather than a parameter because `render_expr` recurses
# through ~35 internal call sites; threading an argument through all of them
# would add noise to every frame to serve one leaf node. Entry points set it
# via `use_dialect`.
_ACTIVE_DIALECT: ContextVar[SqlDialect | None] = ContextVar(
    "semantic_rails_render_dialect", default=None
)


@contextmanager
def use_dialect(dialect: SqlDialect | None) -> Iterator[None]:
    """Bind ``dialect``'s literal and cast rules for the duration of a render."""
    token = _ACTIVE_DIALECT.set(dialect)
    try:
        yield
    finally:
        _ACTIVE_DIALECT.reset(token)


def _render_string_literal(value: str) -> str:
    """Quote a string literal using the active dialect's rules.

    Falls back to standard-conforming escaping (double the quote, leave
    backslashes alone) when no dialect is bound — that is correct for the
    ANSI-conforming warehouses and is what a bare ``render_expr`` call in a
    test gets. Production rendering always binds a dialect;
    ``test_compile_binds_the_warehouse_dialect_for_literal_quoting`` guards
    that so this fallback cannot silently under-escape a real query.
    """
    dialect = _ACTIVE_DIALECT.get()
    if dialect is None:
        return "'" + value.replace("'", "''") + "'"
    return dialect.quote_string_literal(value)


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
    return quote_identifier(raw)


# The answer's own column names (the outermost SELECT's aliases). An output alias is always
# quoted where a SELECT defines it and wherever a reference reads that definition, so a keyword
# such as end, user or order is a name on every warehouse. A warehouse table's column keeps the
# selective quoting above even when it shares a name with an output alias, since Snowflake folds
# an unquoted name to upper case and Postgres to lower case.
_OUTPUT_ALIASES: ContextVar[frozenset[str]] = ContextVar("output_aliases", default=frozenset())


@dataclass(frozen=True)
class _Scope:
    """One SELECT's relations, by the name a reference reads each through (its alias, else its
    dotted name): a CTE's column names, or None for a warehouse table or a table function.
    ``ordered`` holds the SELECT's own column names while its ORDER BY renders, where a bare
    name reads them first."""

    relations: dict[tuple[str, ...], frozenset[str] | None]
    ordered: frozenset[str] = frozenset()


# Whether a reference reads a definition is decided as the warehouse resolves the reference: in
# its own SELECT's scope, then each enclosing one (innermost last). A relation name is a CTE only
# where that CTE is in view: a WITH's earlier CTEs, as a WITH without RECURSIVE reads them, so a
# table that shares its name with a later CTE, or its alias with a CTE's elsewhere, stays a table.
_SCOPES: ContextVar[tuple[_Scope, ...]] = ContextVar("sql_scopes", default=())
_CTES: ContextVar[Mapping[str, frozenset[str]]] = ContextVar("ctes_in_view")


@contextmanager
def _reading(scopes: tuple[_Scope, ...], ctes: Mapping[str, frozenset[str]]) -> Iterator[None]:
    scopes_token, ctes_token = _SCOPES.set(scopes), _CTES.set(ctes)
    try:
        yield
    finally:
        _CTES.reset(ctes_token)
        _SCOPES.reset(scopes_token)


def _alias_name(value: str) -> str:
    return quote_identifier(value) if value in _OUTPUT_ALIASES.get() else _quote_ident(value)


def _reads_output_alias(qualifier: tuple[str, ...], column: str) -> bool:
    """Whether ``column``, read through ``qualifier`` or bare, is an output alias a SELECT
    defines rather than a warehouse table's column. A qualified name no scope holds is a table's;
    a bare one, the SELECT's own column."""
    if column not in _OUTPUT_ALIASES.get():
        return False
    for scope in reversed(_SCOPES.get()):
        if qualifier:
            if qualifier in scope.relations:
                return scope.relations[qualifier] is not None
        elif column in scope.ordered or any(
            column in columns for columns in scope.relations.values() if columns is not None
        ):
            return True
        elif None in scope.relations.values():
            return False
    return not qualifier


def _scope(query: SqlSelect, ctes: Mapping[str, frozenset[str]]) -> _Scope:
    relations: dict[tuple[str, ...], frozenset[str] | None] = {}
    for table in [query.from_table, *(join.table for join in query.joins)]:
        if isinstance(table, SqlTableRef):
            name = (table.alias,) if table.alias else tuple(str(table.name).split("."))
            relations[name] = ctes.get(table.name)
        elif table is not None:
            relations[(table.alias or table.name,)] = None
    return _Scope(relations)


def _column_names(query: SqlQuery) -> frozenset[str]:
    first = query.queries[0] if isinstance(query, SqlSetQuery) else query
    return frozenset(field.alias for field in first.select)


@contextmanager
def quoting_output_aliases(query: SqlSelect) -> Iterator[None]:
    """Quote ``query``'s output aliases, and every reference to them, for one render."""
    aliases = _OUTPUT_ALIASES.set(frozenset(field.alias for field in query.select))
    try:
        yield
    finally:
        _OUTPUT_ALIASES.reset(aliases)


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


def _render_conjunct(expr: SqlExpr) -> str:
    """One item of an AND-joined clause list: an OR inside it keeps its parentheses."""
    return render_expr(expr, parent_precedence=_binary_precedence("AND"))


def render_expr(expr: SqlExpr, *, parent_precedence: int = 0) -> str:
    if isinstance(expr, SqlIdentifier):
        names = [_quote_ident(part) for part in expr.parts]
        *qualifier, column = expr.parts or [""]
        if _reads_output_alias(tuple(qualifier), column):
            names[-1] = quote_identifier(column)
        return ".".join(names)
    if isinstance(expr, SqlParameter):
        return "?"
    if isinstance(expr, SqlLiteral):
        if expr.value is None:
            return "NULL"
        if isinstance(expr.value, bool):
            return "TRUE" if expr.value else "FALSE"
        if isinstance(expr.value, (int, float)):
            return str(expr.value)
        dialect = _ACTIVE_DIALECT.get()
        if expr.temporal and dialect is not None:
            return dialect.temporal_literal(str(expr.value))
        return _render_string_literal(str(expr.value))
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
    if isinstance(expr, SqlLike):
        dialect = _ACTIVE_DIALECT.get()
        escape = (
            f" ESCAPE {_render_string_literal(LIKE_ESCAPE)}"
            if dialect is None or dialect.like_escape_clause
            else ""
        )
        fold = dialect.case_fold if dialect is not None else "LOWER({})"
        negated = " NOT" if expr.negated else ""
        value = fold.format(render_expr(expr.expr))
        pattern = fold.format(_render_string_literal(expr.pattern))
        return f"({value}{negated} LIKE {pattern}{escape})"
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
        type_name = normalize_sql_cast_type_name(expr.type_name)
        dialect = _ACTIVE_DIALECT.get()
        if type_name == "BOOLEAN" and is_null_literal(expr.expr) and dialect is not None:
            type_name = dialect.null_boolean_type
        return f"CAST({render_expr(expr.expr)} AS {type_name})"
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
        nulls = " IGNORE NULLS" if expr.ignore_nulls else ""
        return f"{function_name}({args}{nulls} ORDER BY {order_sql}{limit_sql})"
    if isinstance(expr, SqlAggregateFilter):
        return f"{render_expr(expr.call)} FILTER (WHERE {render_expr(expr.where)})"
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
        return replace(node, ctes=[]), hoisted

    root, ctes = _walk_select(query)
    return replace(root, ctes=ctes)


def render_select(query: SqlSelect, *, flatten_ctes: bool = True) -> str:
    if flatten_ctes:
        query = _flatten_ctes(query)
    chunks: list[str] = []
    outer, ctes_in_view = _SCOPES.get(), dict(_CTES.get({}))
    if query.ctes:
        ctes = []
        for cte in query.ctes:
            # A CTE reads the CTEs before it, never this SELECT's own relations.
            with _reading(outer, ctes_in_view):
                ctes.append(
                    f"{_quote_ident(cte.name)} AS (\n{render_query(cte.query, flatten_ctes=False)}\n)"
                )
            ctes_in_view[cte.name] = _column_names(cte.query)
        chunks.append("WITH " + ",\n".join(ctes))
    scope = _scope(query, ctes_in_view)
    with _reading((*outer, scope), ctes_in_view):
        chunks.extend(_render_clauses(query))
    if query.order_by:
        ordered = replace(scope, ordered=frozenset(field.alias for field in query.select))
        with _reading((*outer, ordered), ctes_in_view):
            chunks.append(
                "ORDER BY\n  "
                + ",\n  ".join(
                    f"{render_expr(order.expression)} {normalize_sql_sort_direction(order.direction)}"
                    + (" NULLS LAST" if order.nulls_last else "")
                    for order in query.order_by
                )
            )
    if query.limit is not None:
        chunks.append(f"LIMIT {query.limit}")
    return "\n".join(chunks)


def _render_clauses(query: SqlSelect) -> list[str]:
    """``query``'s SELECT list through its QUALIFY, in its own scope."""
    chunks: list[str] = []
    select_sql = ",\n  ".join(
        f"{render_expr(field.expression)} AS {_alias_name(field.alias)}" for field in query.select
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
        chunks.append("WHERE\n  " + "\n  AND ".join(_render_conjunct(item) for item in query.where))
    if query.group_by:
        chunks.append("GROUP BY\n  " + ",\n  ".join(render_expr(item) for item in query.group_by))
    if query.having:
        chunks.append(
            "HAVING\n  " + "\n  AND ".join(_render_conjunct(item) for item in query.having)
        )
    if query.qualify:
        chunks.append(
            "QUALIFY\n  " + "\n  AND ".join(_render_conjunct(item) for item in query.qualify)
        )
    return chunks


def render_query(query: SqlQuery, *, flatten_ctes: bool = True) -> str:
    if isinstance(query, SqlSelect):
        return render_select(query, flatten_ctes=flatten_ctes)
    if isinstance(query, SqlSetQuery):
        operator = normalize_sql_set_operator(query.operator)
        return f"\n{operator}\n".join(
            render_select(item, flatten_ctes=flatten_ctes) for item in query.queries
        )
    raise TypeError(f"Unsupported SQL query: {type(query)!r}")


def render_select_for_profile(
    query: SqlSelect, profile: str | None = None, *, dialect: SqlDialect | None = None
) -> str:
    """Render ``query`` at the requested SQL profile.

    ``dialect`` binds the warehouse's literal and cast rules for the whole
    render; callers compiling for a real warehouse must pass it. The compiler
    then calls ``dialect.prepare_query`` to finalize executable SQL and aliases.
    """
    with use_dialect(dialect), quoting_output_aliases(query):
        normalized = str(profile or "audit").strip().lower()
        if normalized == "debug":
            return render_select(query, flatten_ctes=False)
        rendered = render_select(query, flatten_ctes=True)
        if normalized == "compact":
            return re.sub(r"\s+", " ", rendered).strip()
        return rendered
