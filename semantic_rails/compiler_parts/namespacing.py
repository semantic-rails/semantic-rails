"""SQL CTE namespacing helper for post-aggregation lowering.

Extracted from :mod:`semantic_rails.compiler_parts.post_aggregation`
so that module stays under the 500-LOC ceiling. The public API is
unchanged — ``post_aggregation`` re-exports :func:`_namespace_sql_select`
so callers keep importing from there.
"""

from __future__ import annotations

from typing import Any

from ..sql_ast import (
    SqlBinary,
    SqlCall,
    SqlCallWithOrder,
    SqlCase,
    SqlCaseWhen,
    SqlCast,
    SqlCte,
    SqlExists,
    SqlField,
    SqlIdentifier,
    SqlIn,
    SqlIndexAccess,
    SqlInterval,
    SqlIsNull,
    SqlJoin,
    SqlLiteral,
    SqlOrder,
    SqlOrderTerm,
    SqlParameterizedCall,
    SqlSelect,
    SqlTableRef,
    SqlWindow,
    SqlWithinGroup,
)


def _namespace_sql_select(select: SqlSelect, prefix: str) -> SqlSelect:
    cte_names: set[str] = set()

    def _collect_ctes(node: SqlSelect) -> None:
        for cte in node.ctes:
            cte_names.add(cte.name)
            _collect_ctes(cte.query)  # type: ignore[arg-type]  # CTE queries are SqlSelect here

    _collect_ctes(select)
    rename = {name: f"{prefix}{name}" for name in cte_names}

    def _expr(expr: Any) -> Any:
        if isinstance(expr, SqlIdentifier):
            if expr.parts and expr.parts[0] in rename:
                return SqlIdentifier(parts=[rename[expr.parts[0]], *expr.parts[1:]])
            return expr
        if isinstance(expr, SqlLiteral):
            return expr
        if isinstance(expr, SqlCall):
            return SqlCall(
                name=expr.name, args=[_expr(arg) for arg in expr.args], distinct=expr.distinct
            )
        if isinstance(expr, SqlBinary):
            return SqlBinary(left=_expr(expr.left), op=expr.op, right=_expr(expr.right))
        if isinstance(expr, SqlIn):
            return SqlIn(
                expr=_expr(expr.expr),
                values=[_expr(value) for value in expr.values],
                negated=expr.negated,
            )
        if isinstance(expr, SqlInterval):
            return SqlInterval(value=_expr(expr.value), unit=expr.unit)
        if isinstance(expr, SqlExists):
            return SqlExists(query=_select(expr.query), negated=expr.negated)
        if isinstance(expr, SqlIsNull):
            return SqlIsNull(expr=_expr(expr.expr))
        if isinstance(expr, SqlCaseWhen):
            return SqlCaseWhen(condition=_expr(expr.condition), result=_expr(expr.result))
        if isinstance(expr, SqlCase):
            return SqlCase(
                whens=[_expr(item) for item in expr.whens],
                else_expr=_expr(expr.else_expr) if expr.else_expr is not None else None,
            )
        if isinstance(expr, SqlCast):
            return SqlCast(expr=_expr(expr.expr), type_name=expr.type_name)
        if isinstance(expr, SqlOrderTerm):
            return SqlOrderTerm(expr=_expr(expr.expr), direction=expr.direction)
        if isinstance(expr, SqlWindow):
            return SqlWindow(
                function=_expr(expr.function),
                partition_by=[_expr(item) for item in expr.partition_by],
                order_by=[_expr(item) for item in expr.order_by],
                frame=expr.frame,
            )
        if isinstance(expr, SqlWithinGroup):
            return SqlWithinGroup(
                function=_expr(expr.function), order_by=[_expr(item) for item in expr.order_by]
            )
        if isinstance(expr, SqlCallWithOrder):
            return SqlCallWithOrder(
                call=_expr(expr.call),
                order_by=[_expr(item) for item in expr.order_by],
                limit=expr.limit,
            )
        if isinstance(expr, SqlIndexAccess):
            return SqlIndexAccess(
                expr=_expr(expr.expr), index=_expr(expr.index), wrapper=expr.wrapper
            )
        if isinstance(expr, SqlParameterizedCall):
            return SqlParameterizedCall(
                name=expr.name,
                params=[_expr(param) for param in expr.params],
                args=[_expr(arg) for arg in expr.args],
            )
        return expr

    def _table(table: SqlTableRef) -> SqlTableRef:
        return SqlTableRef(name=rename.get(table.name, table.name), alias=table.alias)

    def _select(node: SqlSelect) -> SqlSelect:
        assert isinstance(node.from_table, SqlTableRef)  # compiled SQL uses SqlTableRef only
        return SqlSelect(
            select=[SqlField(_expr(field.expression), field.alias) for field in node.select],
            from_table=_table(node.from_table),
            joins=[
                SqlJoin(
                    join_type=join.join_type,
                    table=_table(join.table),  # type: ignore[arg-type]  # join tables are SqlTableRef here
                    on=_expr(join.on),
                )
                for join in node.joins
            ],
            where=[_expr(item) for item in node.where],
            group_by=[_expr(item) for item in node.group_by],
            having=[_expr(item) for item in node.having],
            qualify=[_expr(item) for item in node.qualify],
            order_by=[
                SqlOrder(expression=_expr(item.expression), direction=item.direction)
                for item in node.order_by
            ],
            limit=node.limit,
            ctes=[
                SqlCte(
                    name=rename.get(cte.name, cte.name),
                    query=_select(cte.query),  # type: ignore[arg-type]  # CTE queries are SqlSelect here
                )
                for cte in node.ctes
            ],
            distinct=getattr(node, "distinct", False),
        )

    return _select(select)
