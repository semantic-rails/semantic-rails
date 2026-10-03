from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..dialects import dialect_for_warehouse
from ..errors import SemanticLayerError
from ..expressions import (
    AggregateExpr,
    ArithmeticExpr,
    BooleanExpr,
    CallExpr,
    CaseExpr,
    ColumnRefExpr,
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
    validate_boolean_argument_count,
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
    build_comparison_condition,
    build_negation,
)
from .bind import _expression_alias
from .dependencies import _recipes, recipe_objects, record_leaf_reference
from .indexes import _entity_index, _measure_has_source_row_key, _measure_index, _recipe_index
from .namespacing import _namespace_sql_select
from .temporal import _period_to_date_period, _window_unit_to_rows

__all__ = [
    "_as_offset_window_expr",
    "_base_alias_ref",
    "_compile_offset_window_expr",
    "_compile_post_expr",
    "_expr_requires_dense_series",
    "_namespace_sql_select",
    "_summing_window_parts",
    "_window_partition_exprs",
]


def _base_alias_ref(alias: str, table_alias: str = "base") -> SqlIdentifier:
    record_leaf_reference(alias)
    return SqlIdentifier(parts=[table_alias, alias])


def _window_partition_exprs(table_alias: str, group_aliases: list[str]) -> list[SqlExpr]:
    return [SqlIdentifier(parts=[table_alias, alias]) for alias in dict.fromkeys(group_aliases)]


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


def _summing_window_parts(
    expr: SemanticExpr, config: PackageConfig, *, construct: str
) -> tuple[SemanticExpr, ...]:
    """One rule for binding and lowering: add or scale flows, or divide their windowed parts."""

    def numeric_literal(value: SemanticExpr) -> bool:
        return isinstance(value, LiteralExpr) and type(value.value) in {int, float}

    def resolve(value: SemanticExpr, seen: frozenset[str] = frozenset()) -> SemanticExpr:
        if not isinstance(value, MetricRecipeRefExpr):
            return value
        recipe = _recipe_index(config).get(value.metric_recipe)
        if recipe is None:
            raise SemanticLayerError("OBJECT_NOT_FOUND", f"Unknown metric '{value.metric_recipe}'")
        if recipe.id in seen:
            raise SemanticLayerError("ROLLUP_UNSAFE", f"Cyclic window input '{recipe.id}'")
        return resolve(recipe.expression, seen | {recipe.id})

    def additive(value: SemanticExpr) -> None:
        value = resolve(value)
        name = str(expr_to_dict(value).get("kind"))
        if isinstance(value, (MeasureRefExpr, AggregateExpr, ScopedAggregateExpr)):
            measure = _measure_index(config).get(value.measure)
            if measure is None:
                raise SemanticLayerError("OBJECT_NOT_FOUND", f"Unknown measure '{value.measure}'")
            aggregation = (value.aggregation or measure.default_aggregation).lower()
            allowed = {"additive": {"sum", "count"}, "event_count": {"count_distinct"}}.get(
                measure.measure_class, set()
            )
            if aggregation == "count_distinct":
                entity = _entity_index(config).get(measure.entity)
                counted = measure.expr
                if not (
                    isinstance(counted, ColumnRefExpr)
                    and (
                        measure.row_grain == [counted.column]
                        or (
                            entity is not None
                            and (entity.key or [entity.primary_key]) == [counted.column]
                            and _measure_has_source_row_key(measure, entity)
                        )
                    )
                    and counted.entity in {"", measure.entity}
                    and counted.table
                    in {"", measure.source_relation or (entity.table if entity else "")}
                ):
                    allowed = set()
            if (
                measure.additive
                and measure.accumulation.kind in {"", "flow", "event"}
                and aggregation in allowed
            ):
                return
            name = f"measure '{measure.id}' ({aggregation}, {measure.measure_class})"
        elif isinstance(value, ArithmeticExpr):
            if value.op in {"add", "subtract"}:
                additive(value.left)
                additive(value.right)
                return
            if value.op in {"multiply", "divide"} and numeric_literal(value.right):
                additive(value.left)
                return
            if value.op == "multiply" and numeric_literal(value.left):
                additive(value.right)
                return
            name = value.op
        hint = (
            "Ask for the ratio of the windowed additive parts, or query the measure's own "
            "aggregation without a summing window."
        )
        raise SemanticLayerError(
            "ROLLUP_UNSAFE",
            f"Input {name} cannot feed {construct}: its values do not add up across periods. {hint}",
            details={
                "unsupported_construct": "non_additive_window_input",
                "construct": construct,
                "input": name,
                "recovery_hints": [{"kind": "use_additive_window_parts", "message": hint}],
            },
        )

    resolved = resolve(expr)
    parts: tuple[SemanticExpr, ...]
    if isinstance(resolved, RatioExpr):
        parts = (resolved.numerator, resolved.denominator)
    elif (
        isinstance(resolved, ArithmeticExpr)
        and resolved.op == "divide"
        and not numeric_literal(resolved.right)
    ):
        parts = (resolved.left, resolved.right)
    else:
        parts = (resolved,)
    for part in parts:
        additive(part)
    return parts


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

    def compile_input(input_expr: SemanticExpr) -> SqlExpr:
        return _compile_post_expr(
            input_expr,
            config,
            time_alias=time_alias,
            group_aliases=group_aliases,
            query_grain=query_grain,
            table_alias=table_alias,
        )

    order_by = [SqlOrderTerm(expr=SqlIdentifier(parts=[table_alias, time_alias]), direction="ASC")]
    if expr.kind == "prior_period":
        base = compile_input(expr.input)
        offset_rows = _window_unit_to_rows(expr.unit, expr.value, query_grain)
        lag_partition_by = _window_partition_exprs(table_alias, group_aliases)
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
    # The one place every window compiles: a partition the query does not group by has no
    # column to partition on, so it is refused rather than left to fail in the warehouse.
    missing = [alias for alias in expr.partition_by if alias not in group_aliases]
    if missing:
        recipes = _recipes.get()
        owner = f" of metric '{recipes[-1]}'" if recipes else ""
        raise SemanticLayerError(
            "INVALID_QUERY",
            f"partition_by{owner} names {', '.join(missing)}, which the query does not "
            "group by; add it to group_by or remove it from partition_by",
            details={"partition_by_missing_from_group_by": missing},
        )
    partition_by: list[Any] = _window_partition_exprs(table_alias, group_aliases)
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
    # Keep the recipe's dependency ownership while expanding its ratio at this boundary.
    if isinstance(expr.input, MetricRecipeRefExpr):
        recipe = _recipe_index(config).get(expr.input.metric_recipe)
        if recipe is None:
            raise SemanticLayerError(
                "OBJECT_NOT_FOUND", f"Unknown metric '{expr.input.metric_recipe}'"
            )
        with recipe_objects(recipe.id):
            return _compile_offset_window_expr(
                replace(expr, input=recipe.expression),
                config,
                time_alias=time_alias,
                group_aliases=group_aliases,
                query_grain=query_grain,
                table_alias=table_alias,
            )
    parts = _summing_window_parts(expr.input, config, construct=expr.kind)

    def sum_part(part: SemanticExpr) -> SqlExpr:
        # Constants scale the completed total, rather than becoming another windowed part.
        if isinstance(part, ArithmeticExpr):
            if part.op in {"add", "subtract"}:
                return SqlBinary(
                    sum_part(part.left), "+" if part.op == "add" else "-", sum_part(part.right)
                )
            if part.op in {"multiply", "divide"} and isinstance(part.right, LiteralExpr):
                factor = compile_input(part.right)
                if part.op == "divide":
                    factor = SqlCall("NULLIF", [factor, SqlLiteral(0)])
                return SqlBinary(sum_part(part.left), "*" if part.op == "multiply" else "/", factor)
            if part.op == "multiply" and isinstance(part.left, LiteralExpr):
                return SqlBinary(compile_input(part.left), "*", sum_part(part.right))
        return SqlWindow(
            function=SqlCall("SUM", [compile_input(part)]),
            partition_by=partition_by,
            order_by=order_by,
            frame=frame or "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW",
        )

    sums = [sum_part(part) for part in parts]
    if len(sums) == 2:
        return SqlBinary(sums[0], "/", SqlCall("NULLIF", [sums[1], SqlLiteral(0)]))
    return sums[0]


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
    if isinstance(expr, (MeasureRefExpr, AggregateExpr)):
        return _base_alias_ref(_expression_alias(expr, config), table_alias=table_alias)
    if isinstance(expr, ScopedAggregateExpr):
        return _base_alias_ref(_expression_alias(expr, config), table_alias=table_alias)
    if isinstance(expr, MetricRecipeRefExpr):
        recipe = _recipe_index(config).get(expr.metric_recipe)
        if recipe is None:
            raise SemanticLayerError(
                "OBJECT_NOT_FOUND", f"Unknown metric recipe '{expr.metric_recipe}'"
            )
        with recipe_objects(recipe.id):
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
        return build_comparison_condition(
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
        validate_boolean_argument_count(expr.op, len(expr.args))
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
            return build_negation(rendered[0])
        current = rendered[0]
        for item in rendered[1:]:
            current = SqlBinary(current, op.upper(), item)
        return current
    if isinstance(expr, CallExpr):
        return dialect_for_warehouse(config.package.warehouse).scalar_call(
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
