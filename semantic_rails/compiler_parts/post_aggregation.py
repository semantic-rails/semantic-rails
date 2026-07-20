from __future__ import annotations

from typing import Any

from ..dialects import dialect_for_warehouse
from ..errors import SemanticLayerError
from ..expressions import (
    AggregateExpr,
    ArithmeticExpr,
    BooleanExpr,
    CallExpr,
    CaseExpr,
    ComparisonExpr,
    ConversionExpr,
    CumulativeExpr,
    DateAddExpr,
    DistributionExpr,
    EntityValueExpr,
    InExpr,
    LiteralExpr,
    MeasureRefExpr,
    MetricPredicateExpr,
    MetricRecipeRefExpr,
    OffsetWindowExpr,
    PeriodToDateExpr,
    PriorPeriodExpr,
    RatioExpr,
    RollingExpr,
    ScopedAggregateExpr,
    SemanticExpr,
    expr_to_dict,
)
from ..schema import PackageConfig
from ..sql_ast import (
    SqlBinary,
    SqlCall,
    SqlCase,
    SqlCaseWhen,
    SqlExpr,
    SqlIdentifier,
    SqlIn,
    SqlLiteral,
    SqlOrderTerm,
    SqlWindow,
)
from .bind import _expression_alias
from .indexes import _recipe_index
from .namespacing import _namespace_sql_select
from .temporal import _period_to_date_period, _window_unit_to_rows

__all__ = [
    "_as_offset_window_expr",
    "_base_alias_ref",
    "_compile_offset_window_expr",
    "_compile_post_expr",
    "_expr_requires_dense_series",
    "_namespace_sql_select",
    "_window_partition_exprs",
]


def _base_alias_ref(alias: str, table_alias: str = "base") -> SqlIdentifier:
    return SqlIdentifier(parts=[table_alias, alias])


def _window_partition_exprs(
    table_alias: str, group_aliases: list[str], explicit: list[str]
) -> list[SqlExpr]:
    aliases = list(dict.fromkeys([*group_aliases, *explicit]))
    return [SqlIdentifier(parts=[table_alias, alias]) for alias in aliases]


def _as_offset_window_expr(expr: SemanticExpr) -> OffsetWindowExpr | None:
    if isinstance(expr, OffsetWindowExpr):
        return expr
    if isinstance(expr, CumulativeExpr):
        return OffsetWindowExpr(
            input=expr.input,
            kind="cumulative",
            aggregate="sum",
            frame="ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW",
            partition_by=list(expr.partition_by),
        )
    if isinstance(expr, RollingExpr):
        return OffsetWindowExpr(
            input=expr.input,
            kind="rolling",
            aggregate="sum",
            frame="rows",
            unit=expr.unit,
            value=expr.value,
            partition_by=list(expr.partition_by),
        )
    if isinstance(expr, PriorPeriodExpr):
        return OffsetWindowExpr(
            input=expr.input, kind="prior_period", aggregate="lag", unit=expr.unit, value=expr.value
        )
    if isinstance(expr, PeriodToDateExpr):
        return OffsetWindowExpr(
            input=expr.input,
            kind="period_to_date",
            aggregate="sum",
            frame="ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW",
            extra_partition="period",
            period=expr.period,
            partition_by=list(expr.partition_by),
        )
    return None


def _compile_offset_window_expr(
    expr: OffsetWindowExpr,
    config: PackageConfig,
    *,
    time_alias: str,
    group_aliases: list[str],
    query_grain: str,
    table_alias: str,
) -> SqlExpr:
    if not time_alias:
        raise SemanticLayerError(
            "INVALID_TEMPORAL_ROLE", f"{expr.kind} expressions require query.time"
        )
    if expr.kind in {"rolling", "prior_period", "period_to_date"} and not query_grain:
        raise SemanticLayerError(
            "INVALID_TEMPORAL_ROLE", f"{expr.kind} expressions require query.time with grain"
        )
    base = _compile_post_expr(
        expr.input,
        config,
        time_alias=time_alias,
        group_aliases=group_aliases,
        query_grain=query_grain,
        table_alias=table_alias,
    )
    order_by = [SqlOrderTerm(expr=SqlIdentifier(parts=[table_alias, time_alias]), direction="ASC")]
    if expr.kind == "prior_period":
        offset_rows = _window_unit_to_rows(expr.unit, expr.value, query_grain)
        lag_partition_by = _window_partition_exprs(table_alias, group_aliases, [])
        # Dialect hook for warehouses without a LAG window function
        # (e.g. ClickHouse 24.x, which only ships lagInFrame). Dialects
        # that define `window_lag` build their own equivalent window
        # expression; everyone else gets the portable LAG below.
        window_lag = getattr(dialect_for_warehouse(config.package.warehouse), "window_lag", None)
        if window_lag is not None:
            return window_lag(base, offset_rows, partition_by=lag_partition_by, order_by=order_by)
        return SqlWindow(
            function=SqlCall("LAG", [base, SqlLiteral(offset_rows)]),
            partition_by=lag_partition_by,
            order_by=order_by,
        )
    partition_by: list[Any] = _window_partition_exprs(table_alias, group_aliases, expr.partition_by)
    frame = expr.frame
    if expr.kind == "rolling":
        rows = _window_unit_to_rows(expr.unit, expr.value, query_grain)
        frame = f"ROWS BETWEEN {max(rows - 1, 0)} PRECEDING AND CURRENT ROW"
    if expr.kind == "period_to_date":
        dialect = dialect_for_warehouse(config.package.warehouse)
        period_anchor = dialect.date_trunc(
            _period_to_date_period(expr.period, query_grain),
            SqlIdentifier(parts=[table_alias, time_alias]),
        )
        partition_by = [*partition_by, period_anchor]
    return SqlWindow(
        function=SqlCall("SUM", [base]),
        partition_by=partition_by,
        order_by=order_by,
        frame=frame or "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW",
    )


def _compile_post_expr(
    expr: SemanticExpr,
    config: PackageConfig,
    *,
    time_alias: str = "",
    group_aliases: list[str] | None = None,
    query_grain: str = "",
    table_alias: str = "base",
) -> Any:
    group_aliases = list(group_aliases or [])
    if isinstance(expr, MeasureRefExpr):
        return _base_alias_ref(_expression_alias(expr, config), table_alias=table_alias)
    if isinstance(expr, AggregateExpr):
        return _base_alias_ref(
            _expression_alias(
                MeasureRefExpr(
                    measure=expr.measure,
                    aggregation=expr.aggregation,
                    temporal_role=expr.temporal_role,
                    parameters=dict(expr.parameters or {}),
                ),
                config,
            ),
            table_alias=table_alias,
        )
    if isinstance(expr, ScopedAggregateExpr):
        return _base_alias_ref(_expression_alias(expr, config), table_alias=table_alias)
    if isinstance(expr, MetricRecipeRefExpr):
        recipe = _recipe_index(config).get(expr.metric_recipe)
        if recipe is None:
            raise SemanticLayerError(
                "OBJECT_NOT_FOUND", f"Unknown metric recipe '{expr.metric_recipe}'"
            )
        return _compile_post_expr(
            recipe.expression,
            config,
            time_alias=time_alias,
            group_aliases=group_aliases,
            query_grain=query_grain,
            table_alias=table_alias,
        )
    if isinstance(expr, LiteralExpr):
        return SqlLiteral(expr.value)
    if isinstance(expr, ArithmeticExpr):
        op_map = {"add": "+", "subtract": "-", "multiply": "*", "divide": "/"}
        left = _compile_post_expr(
            expr.left,
            config,
            time_alias=time_alias,
            group_aliases=group_aliases,
            query_grain=query_grain,
            table_alias=table_alias,
        )
        right = _compile_post_expr(
            expr.right,
            config,
            time_alias=time_alias,
            group_aliases=group_aliases,
            query_grain=query_grain,
            table_alias=table_alias,
        )
        op = op_map.get(expr.op)
        if op is None:
            raise SemanticLayerError(
                "INVALID_EXPRESSION_AST", f"Unsupported arithmetic op '{expr.op}'"
            )
        if op == "/":
            # NULLIF(right, 0) returns NULL when right=0, and x / NULL = NULL,
            # so an outer CASE WHEN right = 0 THEN NULL is dead code.
            return SqlBinary(left, "/", SqlCall("NULLIF", [right, SqlLiteral(0)]))
        if expr.null_behavior == "coalesce_zero" and op in {"+", "-"}:
            left = SqlCall("COALESCE", [left, SqlLiteral(0)])
            right = SqlCall("COALESCE", [right, SqlLiteral(0)])
        return SqlBinary(left, op, right)
    if isinstance(expr, RatioExpr):
        left = _compile_post_expr(
            expr.numerator,
            config,
            time_alias=time_alias,
            group_aliases=group_aliases,
            query_grain=query_grain,
            table_alias=table_alias,
        )
        right = _compile_post_expr(
            expr.denominator,
            config,
            time_alias=time_alias,
            group_aliases=group_aliases,
            query_grain=query_grain,
            table_alias=table_alias,
        )
        return SqlBinary(left, "/", SqlCall("NULLIF", [right, SqlLiteral(0)]))
    if isinstance(expr, ComparisonExpr):
        return SqlBinary(
            _compile_post_expr(
                expr.left,
                config,
                time_alias=time_alias,
                group_aliases=group_aliases,
                query_grain=query_grain,
                table_alias=table_alias,
            ),
            expr.op,
            _compile_post_expr(
                expr.right,
                config,
                time_alias=time_alias,
                group_aliases=group_aliases,
                query_grain=query_grain,
                table_alias=table_alias,
            ),
        )
    if isinstance(expr, InExpr):
        return SqlIn(
            expr=_compile_post_expr(
                expr.expr,
                config,
                time_alias=time_alias,
                group_aliases=group_aliases,
                query_grain=query_grain,
                table_alias=table_alias,
            ),
            values=[
                _compile_post_expr(
                    value,
                    config,
                    time_alias=time_alias,
                    group_aliases=group_aliases,
                    query_grain=query_grain,
                    table_alias=table_alias,
                )
                for value in expr.values
            ],
            negated=expr.negated,
        )
    if isinstance(expr, BooleanExpr):
        op = expr.op.strip().lower()
        if op not in {"and", "or", "not"}:
            raise SemanticLayerError(
                "INVALID_EXPRESSION_AST",
                f"Unsupported boolean op '{expr.op}'",
                details={
                    "expression_kind": "boolean",
                    "received_value": expr.op,
                    "allowed": ["and", "or", "not"],
                },
            )
        if not expr.args:
            raise SemanticLayerError("INVALID_EXPRESSION_AST", "Boolean expressions require args")
        rendered = [
            _compile_post_expr(
                arg,
                config,
                time_alias=time_alias,
                group_aliases=group_aliases,
                query_grain=query_grain,
                table_alias=table_alias,
            )
            for arg in expr.args
        ]
        if op == "not":
            if len(rendered) != 1:
                raise SemanticLayerError(
                    "INVALID_EXPRESSION_AST",
                    f"Boolean 'not' expressions require exactly one arg, got {len(rendered)}",
                    details={
                        "expression_kind": "boolean",
                        "received_arg_count": len(rendered),
                        "recovery_hints": [
                            {
                                "code": "WRAP_NOT_ARGS",
                                "message": (
                                    "'not' is unary. Wrap multiple conditions in a "
                                    "single {kind: 'boolean', op: 'and', args: [...]} "
                                    "(or 'or') and pass that as the only arg."
                                ),
                            }
                        ],
                    },
                )
            # The SQL AST has no unary-NOT node (sql_ast.py is owned by the
            # renderer layer), so compose negation from existing nodes:
            # ``FALSE = arg`` has the same three-valued truth table as
            # ``NOT arg`` (TRUE -> FALSE, FALSE -> TRUE, NULL -> NULL).
            # The arg goes on the right so the renderer parenthesizes
            # same-precedence comparisons (``FALSE = (x > y)``) — chained
            # comparisons are non-associative in Snowflake/Postgres.
            return SqlBinary(SqlLiteral(False), "=", rendered[0])
        current = rendered[0]
        for item in rendered[1:]:
            current = SqlBinary(current, op.upper(), item)
        return current
    if isinstance(expr, CallExpr):
        return SqlCall(
            expr.name,
            [
                _compile_post_expr(
                    arg,
                    config,
                    time_alias=time_alias,
                    group_aliases=group_aliases,
                    query_grain=query_grain,
                    table_alias=table_alias,
                )
                for arg in expr.args
            ],
            distinct=expr.distinct,
        )
    if isinstance(expr, DateAddExpr):
        return dialect_for_warehouse(config.package.warehouse).date_add(
            expr.unit,
            _compile_post_expr(
                expr.value,
                config,
                time_alias=time_alias,
                group_aliases=group_aliases,
                query_grain=query_grain,
                table_alias=table_alias,
            ),
            _compile_post_expr(
                expr.date,
                config,
                time_alias=time_alias,
                group_aliases=group_aliases,
                query_grain=query_grain,
                table_alias=table_alias,
            ),
        )
    if isinstance(expr, CaseExpr):
        return SqlCase(
            whens=[
                SqlCaseWhen(
                    _compile_post_expr(
                        item.when,
                        config,
                        time_alias=time_alias,
                        group_aliases=group_aliases,
                        query_grain=query_grain,
                        table_alias=table_alias,
                    ),
                    _compile_post_expr(
                        item.then,
                        config,
                        time_alias=time_alias,
                        group_aliases=group_aliases,
                        query_grain=query_grain,
                        table_alias=table_alias,
                    ),
                )
                for item in expr.whens
            ],
            else_expr=_compile_post_expr(
                expr.else_expr,
                config,
                time_alias=time_alias,
                group_aliases=group_aliases,
                query_grain=query_grain,
                table_alias=table_alias,
            )
            if expr.else_expr is not None
            else None,
        )
    offset_expr = _as_offset_window_expr(expr)
    if offset_expr is not None:
        return _compile_offset_window_expr(
            offset_expr,
            config,
            time_alias=time_alias,
            group_aliases=group_aliases,
            query_grain=query_grain,
            table_alias=table_alias,
        )
    if isinstance(expr, MetricPredicateExpr):
        raise SemanticLayerError(
            "INVALID_METRIC_PREDICATE",
            "metric_predicate expressions cannot be compiled as projected values",
        )
    if isinstance(expr, EntityValueExpr):
        raise SemanticLayerError(
            "INVALID_QUERY", "entity_value expressions can only be used inside distribution"
        )
    if isinstance(expr, DistributionExpr):
        raise SemanticLayerError(
            "INVALID_QUERY", "distribution expressions require semantic DAG lowering"
        )
    if isinstance(expr, ConversionExpr):
        return _base_alias_ref(_expression_alias(expr, config), table_alias=table_alias)
    raise SemanticLayerError(
        "INVALID_QUERY", f"Unsupported expression kind '{expr_to_dict(expr)['kind']}'"
    )


def _expr_requires_dense_series(expr: SemanticExpr, config: PackageConfig) -> bool:
    if isinstance(expr, MetricRecipeRefExpr):
        recipe = _recipe_index(config).get(expr.metric_recipe)
        if recipe is None:
            raise SemanticLayerError(
                "OBJECT_NOT_FOUND", f"Unknown metric recipe '{expr.metric_recipe}'"
            )
        return _expr_requires_dense_series(recipe.expression, config)
    if isinstance(expr, (RollingExpr, PriorPeriodExpr)):
        return True
    if isinstance(expr, OffsetWindowExpr):
        if expr.kind in {"rolling", "prior_period"}:
            return True
        return _expr_requires_dense_series(expr.input, config)
    if isinstance(expr, (ArithmeticExpr, ComparisonExpr)):
        return _expr_requires_dense_series(expr.left, config) or _expr_requires_dense_series(
            expr.right, config
        )
    if isinstance(expr, InExpr):
        return _expr_requires_dense_series(expr.expr, config) or any(
            _expr_requires_dense_series(value, config) for value in expr.values
        )
    if isinstance(expr, BooleanExpr):
        return any(_expr_requires_dense_series(arg, config) for arg in expr.args)
    if isinstance(expr, CallExpr):
        return any(_expr_requires_dense_series(arg, config) for arg in expr.args)
    if isinstance(expr, DateAddExpr):
        return _expr_requires_dense_series(expr.value, config) or _expr_requires_dense_series(
            expr.date, config
        )
    if isinstance(expr, CaseExpr):
        return any(
            _expr_requires_dense_series(item.when, config)
            or _expr_requires_dense_series(item.then, config)
            for item in expr.whens
        ) or (expr.else_expr is not None and _expr_requires_dense_series(expr.else_expr, config))
    if isinstance(expr, CumulativeExpr):
        return _expr_requires_dense_series(expr.input, config)
    if isinstance(expr, PeriodToDateExpr):
        return _expr_requires_dense_series(expr.input, config)
    if isinstance(expr, MetricPredicateExpr):
        return False
    if isinstance(expr, ConversionExpr):
        return _expr_requires_dense_series(expr.base, config) or _expr_requires_dense_series(
            expr.converted, config
        )
    return False
