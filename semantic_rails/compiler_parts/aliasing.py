from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace

from ..ir import LogicalPlan
from ..sql_ast import (
    SqlBinary,
    SqlCall,
    SqlCase,
    SqlCaseWhen,
    SqlCast,
    SqlCte,
    SqlExists,
    SqlField,
    SqlIdentifier,
    SqlIn,
    SqlInterval,
    SqlIsNull,
    SqlJoin,
    SqlOrder,
    SqlOrderTerm,
    SqlSelect,
    SqlWindow,
    SqlWithinGroup,
)


@dataclass(frozen=True)
class AliasRegistry:
    aliases: dict[str, str]

    @classmethod
    def for_plan(
        cls,
        plan: LogicalPlan,
        *,
        measure_aliases: Iterable[str],
        metric_filter_aliases: Iterable[str] = (),
    ) -> AliasRegistry:
        aliases: dict[str, str] = {}
        for group_count, alias in enumerate(list(plan.group_by), start=1):
            aliases[alias] = f"g{group_count}"
        if plan.time:
            time_alias = (
                plan.time["temporal_role"]
                if not plan.time.get("grain")
                else f"{plan.time['temporal_role']}__{plan.time['grain']}"
            )
            aliases[time_alias] = "t"
        for index, alias in enumerate(list(dict.fromkeys(measure_aliases)), start=1):
            aliases[alias] = f"m{index}"
        for index, alias in enumerate(list(dict.fromkeys(metric_filter_aliases)), start=1):
            aliases[alias] = f"p{index}"
        return cls(aliases=aliases)

    def internal(self, alias: str) -> str:
        return self.aliases.get(alias, alias)


def _alias_identifier(expr: SqlIdentifier, registry: AliasRegistry) -> SqlIdentifier:
    if not expr.parts:
        return expr
    return SqlIdentifier(parts=[*expr.parts[:-1], registry.internal(expr.parts[-1])])


def alias_expr(expr, registry: AliasRegistry):
    if isinstance(expr, SqlIdentifier):
        return _alias_identifier(expr, registry)
    if isinstance(expr, SqlBinary):
        return replace(
            expr, left=alias_expr(expr.left, registry), right=alias_expr(expr.right, registry)
        )
    if isinstance(expr, SqlCall):
        return replace(expr, args=[alias_expr(arg, registry) for arg in expr.args])
    if isinstance(expr, SqlIn):
        return replace(
            expr,
            expr=alias_expr(expr.expr, registry),
            values=[alias_expr(value, registry) for value in expr.values],
        )
    if isinstance(expr, SqlInterval):
        return replace(expr, value=alias_expr(expr.value, registry))
    if isinstance(expr, SqlExists):
        return replace(expr, query=alias_select(expr.query, registry, preserve_output_aliases=True))
    if isinstance(expr, SqlIsNull):
        return replace(expr, expr=alias_expr(expr.expr, registry))
    if isinstance(expr, SqlCase):
        return replace(
            expr,
            whens=[
                SqlCaseWhen(alias_expr(item.condition, registry), alias_expr(item.result, registry))
                for item in expr.whens
            ],
            else_expr=alias_expr(expr.else_expr, registry) if expr.else_expr is not None else None,
        )
    if isinstance(expr, SqlCast):
        return replace(expr, expr=alias_expr(expr.expr, registry))
    if isinstance(expr, SqlWindow):
        return replace(
            expr,
            function=alias_expr(expr.function, registry),
            partition_by=[alias_expr(item, registry) for item in expr.partition_by],
            order_by=[
                SqlOrderTerm(expr=alias_expr(item.expr, registry), direction=item.direction)
                for item in expr.order_by
            ],
        )
    if isinstance(expr, SqlWithinGroup):
        return replace(
            expr,
            function=alias_expr(expr.function, registry),
            order_by=[
                SqlOrderTerm(expr=alias_expr(item.expr, registry), direction=item.direction)
                for item in expr.order_by
            ],
        )
    return expr


def alias_select(
    select: SqlSelect, registry: AliasRegistry, *, preserve_output_aliases: bool = True
) -> SqlSelect:
    return SqlSelect(
        ctes=[
            SqlCte(
                name=cte.name,
                query=alias_select(
                    cte.query,  # type: ignore[arg-type]  # CTE queries are SqlSelect in this stage
                    registry,
                    preserve_output_aliases=False,
                ),
            )
            for cte in select.ctes
        ],
        select=[
            SqlField(
                alias_expr(field.expression, registry),
                field.alias if preserve_output_aliases else registry.internal(field.alias),
            )
            for field in select.select
        ],
        from_table=select.from_table,
        joins=[
            SqlJoin(join_type=join.join_type, table=join.table, on=alias_expr(join.on, registry))
            for join in select.joins
        ],
        where=[alias_expr(item, registry) for item in select.where],
        group_by=[alias_expr(item, registry) for item in select.group_by],
        having=[alias_expr(item, registry) for item in select.having],
        qualify=[alias_expr(item, registry) for item in select.qualify],
        order_by=[
            SqlOrder(expression=alias_expr(item.expression, registry), direction=item.direction)
            for item in select.order_by
        ],
        limit=select.limit,
        distinct=getattr(select, "distinct", False),
    )
